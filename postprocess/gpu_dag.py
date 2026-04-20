"""GPU DAG extraction — Typed DAG per PDF §4.5 (CPU/SM/CE nodes).

Operates on an ALREADY-extracted graph in graph_store, does NOT re-run
model_improved. For each F vertex that represents GPU-capable work
(gpu_node=True — oort threads today, callback-anchored GPU work later),
detect per-invocation CUDA activity, build a Typed DAG, collect unique
DAG shapes across invocations, and persist back to F.A_v['gpu_dags'].

Run:
    python3 gpu_dag.py <session> <scope>
Example:
    python3 gpu_dag.py fish_compose_20260419_161633 aircraft

For the oort case (no callback anchor), periods are detected by gap
thresholding on the thread's CUDA API timestamps. Default threshold:
10ms — tuneable via --gap-ms.

See notes/EXPECTED_OBSERVED.txt §8 (oort pattern) + PDF §4.7 (algorithm).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from collections import defaultdict

import numpy as np
from influxdb_client_3 import InfluxDBClient3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_store


INFLUX_HOST = "http://127.0.0.1:8181"
INFLUX_TOKEN_FILE = os.path.expanduser("~/inf.tok")
INFLUX_DB = "fish"


def log(msg):
    print(f"[gpu_dag] {msg}", flush=True)


def load_token():
    with open(INFLUX_TOKEN_FILE) as f:
        for line in f:
            if line.startswith("Token:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError(f"token not found in {INFLUX_TOKEN_FILE}")


def connect_influx():
    return InfluxDBClient3(host=INFLUX_HOST, token=load_token(), database=INFLUX_DB)


# ---------------------------------------------------------------------------
# Period (invocation) detection
# ---------------------------------------------------------------------------

_SYNC_API_MARKERS = ("cudaStreamSynchronize", "cudaDeviceSynchronize",
                     "cudaEventSynchronize")


def _is_sync(api_name: str) -> bool:
    return any(marker in api_name for marker in _SYNC_API_MARKERS)


def detect_periods(times_ns, api_names, gap_threshold_ns=5_000_000):
    """Pattern-based period detection.

    A period boundary is a stream/device/event synchronize call whose
    gap to the NEXT CUDA API call exceeds ``gap_threshold_ns`` — i.e.
    the host went idle after synchronizing, which for yolo-style
    periodic workloads means "waiting for the next frame". Small-gap
    syncs (ORT's internal sync pairs, tight polling) are ignored —
    they stay inside one period.

    Returns a list of (start_idx, end_idx_inclusive) covering the
    ops between consecutive boundaries.

    For yolo_py in AAS, 1964 out of 1980 invocations have exactly 550
    ops between boundaries, confirming the pattern is robust.
    """
    n = len(times_ns)
    if n == 0:
        return []

    # Sync indices followed by a >gap_threshold gap
    boundaries = []
    for i in range(n - 1):
        if _is_sync(api_names[i]) and (times_ns[i+1] - times_ns[i]) > gap_threshold_ns:
            boundaries.append(i)
    if not boundaries:
        return [(0, n - 1)]

    periods = []
    # First period: from start up to the first boundary sync (inclusive)
    periods.append((0, boundaries[0]))
    for a, b in zip(boundaries, boundaries[1:]):
        periods.append((a + 1, b))
    # Trailing period after the last boundary (if any work follows)
    if boundaries[-1] + 1 <= n - 1:
        periods.append((boundaries[-1] + 1, n - 1))
    return periods


def filter_productive(periods, api_names, min_gpu_ops=2):
    """Drop periods that contain fewer than ``min_gpu_ops`` GPU-side
    ops (kernel launches or memcpys). These are idle/polling spans with
    no real compute — they'd otherwise pollute the unique-DAG set."""
    out = []
    for start, end in periods:
        gpu_op_count = 0
        for i in range(start, end + 1):
            a = api_names[i]
            if ("LaunchKernel" in a or "Memcpy" in a or "Memset" in a):
                gpu_op_count += 1
                if gpu_op_count >= min_gpu_ops:
                    break
        if gpu_op_count >= min_gpu_ops:
            out.append((start, end))
    return out


# ---------------------------------------------------------------------------
# DAG construction
# ---------------------------------------------------------------------------

def _async_memcpy(api_name):
    """Is this memcpy call async? (Affects DAG shape per PDF §4.7.)"""
    name = api_name.lower()
    return "async" in name


def build_typed_dag(api_rows, kernels_by_corr, memcpys_by_corr,
                    memsets_by_corr=None):
    """Build a Typed DAG (CPU/SM/CE nodes + precedence edges) from one
    invocation's CUDA activity. Inputs:
      api_rows          list of cuda_runtime rows sorted by time
                        (the thread's CPU driver calls)
      kernels_by_corr   {correlationId: gpu_kernels row}
      memcpys_by_corr   {correlationId: gpu_memcpy row}
      memsets_by_corr   {correlationId: gpu_memset row}  (Volta+: fill
                        kernels run on SM hardware, so these produce
                        SM nodes like regular kernels)
    Returns dict with 'nodes' + 'edges'.
    """
    if memsets_by_corr is None:
        memsets_by_corr = {}
    nodes = []
    edges = []

    # Map stream → last GPU-side node index (for within-stream FIFO edges)
    stream_last = {}

    # Link sequential CPU calls (same thread) via issuance-order edges
    prev_cpu = None

    for api in api_rows:
        api_name = api["api_name"]
        corr = api["correlationId"]
        # CPU node
        cpu_idx = len(nodes)
        nodes.append({
            "idx": cpu_idx,
            "pe": "CPU",
            "name": api_name,
            "corr_id": corr,
            "duration_ns": int(api.get("duration_ns") or 0),
        })
        if prev_cpu is not None:
            edges.append((prev_cpu, cpu_idx, "cpu_order"))
        prev_cpu = cpu_idx

        # Match to GPU-side activity via correlation id
        krow = kernels_by_corr.get(corr)
        mrow = memcpys_by_corr.get(corr)
        msrow = memsets_by_corr.get(corr)
        gpu_node_idx = None
        if krow is not None:
            gpu_node_idx = len(nodes)
            gx, gy, gz = (int(krow.get(k) or 1) for k in ("gridX", "gridY", "gridZ"))
            bx, by, bz = (int(krow.get(k) or 1) for k in ("blockX", "blockY", "blockZ"))
            nodes.append({
                "idx": gpu_node_idx,
                "pe": "SM",
                "name": krow.get("kernel_short_name") or krow.get("kernel_name") or "<anon>",
                "blocks": gx * gy * gz,
                "threads_per_block": bx * by * bz,
                "stream": int(krow.get("streamId") or 0),
                "duration_ns": int(krow.get("duration_ns") or 0),
            })
            edges.append((cpu_idx, gpu_node_idx, "launch"))
        elif mrow is not None:
            gpu_node_idx = len(nodes)
            copy_kind = int(mrow.get("copyKind") or 0)
            direction = {1: "H2D", 2: "D2H", 3: "D2D", 8: "H2H"}.get(copy_kind, f"k{copy_kind}")
            nodes.append({
                "idx": gpu_node_idx,
                "pe": "CE",
                "name": f"memcpy_{direction}",
                "direction": direction,
                "bytes": int(mrow.get("bytes") or 0),
                "stream": int(mrow.get("streamId") or 0),
                "duration_ns": int(mrow.get("duration_ns") or 0),
                "async": _async_memcpy(api_name),
            })
            edges.append((cpu_idx, gpu_node_idx, "launch"))
        elif msrow is not None:
            # cudaMemset on Volta+/Ampere+ is implemented as a fill kernel
            # on SM hardware — NOT a copy-engine DMA operation.
            # See notes/cuda_api_model.txt G1.
            gpu_node_idx = len(nodes)
            nodes.append({
                "idx": gpu_node_idx,
                "pe": "SM",
                "name": f"memset_fill(value={msrow.get('value', 0)})",
                "bytes": int(msrow.get("bytes") or 0),
                "fill_value": int(msrow.get("value") or 0),
                "stream": int(msrow.get("streamId") or 0),
                "duration_ns": int(msrow.get("duration_ns") or 0),
            })
            edges.append((cpu_idx, gpu_node_idx, "launch"))

        # Within-stream FIFO ordering (A4): connect to previous GPU node on same stream
        if gpu_node_idx is not None and "stream" in nodes[gpu_node_idx]:
            s = nodes[gpu_node_idx]["stream"]
            if s in stream_last:
                edges.append((stream_last[s], gpu_node_idx, "stream_fifo"))
            stream_last[s] = gpu_node_idx

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Phase classification (G5, see notes/phase_detector.txt)
# ---------------------------------------------------------------------------

# API name substring → init-signature weight.
# Each entry: (substring, threshold, weight_if_threshold_met).
# When threshold is None, the count is multiplied directly by the weight.
_INIT_SIGNALS = [
    # "Hard" init-only — presence of any single one flips the verdict.
    ("cuInit",                         1,    100),
    ("cuLibraryLoadData",              1,    100),
    ("cuDevicePrimaryCtxRetain",       1,    100),
    ("cuCtxCreate",                    1,    100),
    # Strong but not process-exclusive.
    ("cudaGetDeviceProperties",        1,     20),
    ("cuModuleGetLoadingMode",         1,     20),
    # Massive symbol-resolution burst.
    ("cuGetProcAddress",             100,     50),
    # Per-count weak signals (may occur dynamically in pathological apps).
    ("cudaMalloc",                  None,      5),
    ("cudaStreamCreate",            None,      3),
    ("cudaEventCreate",             None,      1),
]


def init_score(api_names_slice) -> int:
    """Compute the init-phase score for a slice of api_names (one period)."""
    counts = {}
    for name in api_names_slice:
        for token, _thresh, _w in _INIT_SIGNALS:
            if token in name:
                counts[token] = counts.get(token, 0) + 1
    score = 0
    for token, thresh, weight in _INIT_SIGNALS:
        c = counts.get(token, 0)
        if thresh is None:
            score += weight * c
        elif c >= thresh:
            score += weight
    return score


# Thresholds from notes/phase_detector.txt
_INIT_THRESHOLD   = 100
_WARMUP_THRESHOLD = 20
_WARMUP_MAX_POS   = 3    # warmup candidates must be within first N periods
_TAIL_MAX_POS    = 3     # tail candidates must be within last N periods
_TAIL_MIN_NODES  = 10    # minimum length to call a shape a TAIL prefix
_TAIL_MIN_MATCH_RATIO = 0.95  # fraction of tail's ops that must prefix-match
                               # (the trailing mismatch is typically final
                               # sync ops inserted by a cleanup/signal handler)


def _node_seq(dag):
    return [(n["pe"], n["name"]) for n in dag["nodes"]]


def classify_phases(periods, api_names, all_dags):
    """Return a list of phase labels, one per period, matching periods[].

    Labels:
      INIT, WARMUP, STEADY, TAIL, COALESCED(<N>), UNKNOWN.

    See notes/phase_detector.txt for the full algorithm specification
    including rationale for each threshold.
    """
    n = len(periods)
    if n == 0:
        return [], None, None

    # 1. Score every period + pre-compute signature sequences
    period_info = []
    for i, (s, e) in enumerate(periods):
        score = init_score(api_names[s:e+1])
        dag = all_dags[i]
        period_info.append({
            "idx": i,
            "score": score,
            "dag": dag,
            "sig": dag_signature(dag),
        })

    # 2. Dominant shape from "pure productive" subset
    productive = [p for p in period_info if p["score"] < _WARMUP_THRESHOLD]
    if productive:
        sig_counts = {}
        for p in productive:
            sig_counts[p["sig"]] = sig_counts.get(p["sig"], 0) + 1
        dom_sig = max(sig_counts, key=sig_counts.get)
        dom_dag = next(p["dag"] for p in productive if p["sig"] == dom_sig)
    else:
        dom_sig, dom_dag = None, None
    dom_seq = _node_seq(dom_dag) if dom_dag else []

    # 3. Label each period
    labels = []
    for p in period_info:
        if p["score"] >= _INIT_THRESHOLD:
            labels.append("INIT")
            continue
        if dom_sig and p["sig"] == dom_sig:
            labels.append("STEADY")
            continue

        p_seq = _node_seq(p["dag"])

        # COALESCED: exact N× repetition of the dominant sequence
        if (dom_seq and len(p_seq) > len(dom_seq)
                and len(p_seq) % len(dom_seq) == 0):
            N = len(p_seq) // len(dom_seq)
            if all(p_seq[k] == dom_seq[k % len(dom_seq)]
                   for k in range(len(p_seq))):
                labels.append(f"COALESCED({N})")
                continue

        # TAIL: short, late, fuzzy prefix of dominant (allows trailing
        # cleanup-sync ops to differ — SIGTERM handlers typically call
        # cudaStreamSynchronize on the way out, which doesn't match the
        # next dominant kernel launch at that position).
        if (dom_seq
                and p["idx"] >= n - _TAIL_MAX_POS
                and _TAIL_MIN_NODES <= len(p_seq) < len(dom_seq)):
            match_len = 0
            for k in range(len(p_seq)):
                if p_seq[k] == dom_seq[k]:
                    match_len += 1
                else:
                    break
            # Everything past the match must be sync-only (cleanup) to call TAIL.
            trailing_all_sync = all(
                "Synchronize" in p_seq[k][1] for k in range(match_len, len(p_seq)))
            if (match_len / len(p_seq) >= _TAIL_MIN_MATCH_RATIO
                    and trailing_all_sync):
                labels.append("TAIL")
                continue

        # WARMUP: weak init signal + early position
        if (_WARMUP_THRESHOLD <= p["score"] < _INIT_THRESHOLD
                and p["idx"] < _WARMUP_MAX_POS):
            labels.append("WARMUP")
            continue

        labels.append("UNKNOWN")

    return labels, dom_sig, dom_dag


def dag_signature(dag):
    """Canonical fingerprint of DAG shape (ignores durations / bytes).

    Sequence of (pe, name) in the order nodes were added (which is CUDA
    issuance order = topological order since every edge goes forward),
    plus the sorted edge set. Returns a hex digest suitable as a map key.
    """
    node_sig = tuple((n["pe"], n["name"]) for n in dag["nodes"])
    edge_sig = tuple(sorted((u, v, lbl) for u, v, lbl in dag["edges"]))
    s = repr((node_sig, edge_sig)).encode()
    return hashlib.sha1(s).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Per-F extraction driver
# ---------------------------------------------------------------------------

def extract_for_oort(db_name, scope, f_id, tid, container, *,
                    gap_ms=5.0, min_gpu_ops=2, influx=None):
    """Extract the set of unique GPU DAGs for one oort F vertex and
    return a summary suitable for F.A_v['gpu_dags'].

    Periods are delimited by SYNC boundaries: a cudaStreamSynchronize /
    cudaDeviceSynchronize call followed by a >= gap_ms idle. Idle-only
    (no GPU work) periods are filtered out via min_gpu_ops threshold.
    """
    if influx is None:
        influx = connect_influx()

    gap_ns = int(gap_ms * 1_000_000)

    # 1. Pull all cuda_runtime rows for this (session, container, tid), ordered by time.
    # Separate timestamp pull (numpy-native int64) avoids pyarrow's
    # microsecond-conversion issue for nanosecond-resolution timestamps.
    t0 = time.time()
    tbl = influx.query(f"""
        SELECT time, api_name, "correlationId", duration_ns
        FROM cuda_runtime
        WHERE session='{db_name}' AND container='{container}' AND tid={tid}
        ORDER BY time
    """)
    times_ns = tbl.column("time").to_numpy().astype("int64")
    # Drop time column before to_pylist (datetime conversion would lose ns precision)
    api_rows_raw = tbl.drop("time").to_pylist()
    api_names = [r["api_name"] for r in api_rows_raw]
    log(f"  cuda_runtime: {len(api_rows_raw):,} rows loaded in {time.time()-t0:.1f}s")

    # 2. Detect periods via sync-boundary pattern.
    # Keep BOTH the full period list (for phase classification —
    # the INIT phase rarely meets the min_gpu_ops filter) and the
    # productive-only list used for DAG aggregation.
    all_periods = detect_periods(times_ns, api_names, gap_ns)
    productive = filter_productive(all_periods, api_names, min_gpu_ops=min_gpu_ops)
    log(f"  periods (sync-boundary, gap>{gap_ms}ms): {len(all_periods):,} total "
        f"→ {len(productive):,} productive (≥{min_gpu_ops} GPU ops)")
    # For phase classification we want both — INIT phase may have only
    # 1 SM event (min_gpu_ops=2 would drop it). Use a looser filter.
    phase_periods = filter_productive(all_periods, api_names, min_gpu_ops=1)
    periods = productive

    # 3. Pull all kernels + memcpys ONCE (by correlation_id); index in memory
    t1 = time.time()
    kern_tbl = influx.query(f"""
        SELECT "correlationId", "streamId", kernel_name, kernel_short_name,
               "gridX", "gridY", "gridZ", "blockX", "blockY", "blockZ",
               duration_ns
        FROM gpu_kernels
        WHERE session='{db_name}' AND container='{container}'
    """).to_pylist()
    kernels_by_corr = {r["correlationId"]: r for r in kern_tbl}

    memcpy_tbl = influx.query(f"""
        SELECT "correlationId", "streamId", "copyKind", bytes, duration_ns
        FROM gpu_memcpy
        WHERE session='{db_name}' AND container='{container}'
    """).to_pylist()
    memcpys_by_corr = {r["correlationId"]: r for r in memcpy_tbl}

    # gpu_memset may not exist in older traces — tolerate query failure
    memsets_by_corr = {}
    try:
        memset_tbl = influx.query(f"""
            SELECT "correlationId", "streamId", bytes, value, duration_ns
            FROM gpu_memset
            WHERE session='{db_name}' AND container='{container}'
        """).to_pylist()
        memsets_by_corr = {r["correlationId"]: r for r in memset_tbl}
    except Exception as e:
        log(f"  gpu_memset unavailable ({e}) — continuing without memset SM nodes")

    log(f"  kernels={len(kernels_by_corr):,}  memcpys={len(memcpys_by_corr):,}  "
        f"memsets={len(memsets_by_corr):,}  indexed in {time.time()-t1:.1f}s")

    # 4. Build per-invocation DAG, collect by signature
    t2 = time.time()
    unique = {}   # sig → {count, dag (template), c_min/c_max per node}
    for start, end in periods:
        api_rows = api_rows_raw[start:end+1]
        dag = build_typed_dag(api_rows, kernels_by_corr, memcpys_by_corr,
                              memsets_by_corr=memsets_by_corr)
        if not dag["nodes"]:
            continue
        sig = dag_signature(dag)
        if sig not in unique:
            # Initialize with this DAG's durations as c_min/c_max templates
            durs = [n["duration_ns"] for n in dag["nodes"]]
            unique[sig] = {
                "count": 0,
                "template": dag,
                "c_min": list(durs),
                "c_max": list(durs),
            }
        info = unique[sig]
        info["count"] += 1
        for i, node in enumerate(dag["nodes"]):
            d = int(node["duration_ns"])
            if d < info["c_min"][i]:
                info["c_min"][i] = d
            if d > info["c_max"][i]:
                info["c_max"][i] = d
    log(f"  built DAGs for {len(periods):,} invocations in {time.time()-t2:.1f}s "
        f"→ {len(unique)} unique shape(s)")

    # 5. Phase classification (G5 — see notes/phase_detector.txt)
    # Build per-phase_period DAG list in the same order as phase_periods
    # so classify_phases can line things up. The phase_periods include
    # INIT (which was dropped from `periods` by the stricter filter),
    # so we rebuild their DAGs here.
    phase_dags = []
    for start, end in phase_periods:
        api_rows = api_rows_raw[start:end+1]
        dag = build_typed_dag(api_rows, kernels_by_corr, memcpys_by_corr,
                              memsets_by_corr=memsets_by_corr)
        phase_dags.append(dag)

    phase_labels, dom_sig, _ = classify_phases(
        phase_periods, api_names, phase_dags)

    # Per-unique-signature phase tally — helps downstream analysis
    # pick "the STEADY DAG" versus INIT / TAIL / etc.
    sig_to_phases = {}
    for lbl, dag in zip(phase_labels, phase_dags):
        sg = dag_signature(dag)
        sig_to_phases.setdefault(sg, {}).setdefault(lbl, 0)
        sig_to_phases[sg][lbl] += 1

    log(f"  phase classification: " + ", ".join(
        f"{lbl}={phase_labels.count(lbl)}"
        for lbl in ("INIT", "WARMUP", "STEADY", "TAIL", "UNKNOWN")
        if phase_labels.count(lbl) > 0
    ) + (f", COALESCED={sum(1 for l in phase_labels if l.startswith('COALESCED'))}"
         if any(l.startswith("COALESCED") for l in phase_labels) else ""))

    # 6. Summary list: one dict per unique DAG, with phase attribution
    out = []
    for sig, info in sorted(unique.items(), key=lambda kv: -kv[1]["count"]):
        tmpl_nodes = []
        for i, node in enumerate(info["template"]["nodes"]):
            tmpl_nodes.append({
                **{k: v for k, v in node.items() if k != "duration_ns"},
                "c_min_ns": info["c_min"][i],
                "c_max_ns": info["c_max"][i],
            })
        phases = sig_to_phases.get(sig, {})
        # Phase with max count wins as the dominant label for this shape
        primary_phase = (max(phases, key=phases.get)
                         if phases else "UNKNOWN")
        out.append({
            "signature": sig,
            "count": info["count"],
            "phase": primary_phase,
            "phase_counts": phases,
            "nodes": tmpl_nodes,
            "edges": info["template"]["edges"],
        })

    # Also append any signature that appears ONLY in phase_periods (e.g.
    # INIT) but not in `unique` because it was filtered out of productive.
    for sig, phases in sig_to_phases.items():
        if any(d["signature"] == sig for d in out):
            continue
        # Find first phase_dag matching this sig for a template
        tmpl_dag = next(d for d, s in zip(phase_dags,
                                          [dag_signature(x) for x in phase_dags])
                        if s == sig)
        total_count = sum(phases.values())
        primary_phase = max(phases, key=phases.get)
        out.append({
            "signature": sig,
            "count": total_count,
            "phase": primary_phase,
            "phase_counts": phases,
            "nodes": tmpl_dag["nodes"],
            "edges": tmpl_dag["edges"],
        })

    return out


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Extract GPU DAGs for oort F vertices in a stored graph")
    ap.add_argument("session", help="MongoDB session DB (= graph_store db_name)")
    ap.add_argument("scope", help="Graph scope (role name or __composed__)")
    ap.add_argument("--gap-ms", type=float, default=5.0,
                    help="Gap after a sync that marks a period boundary "
                         "(default 5ms; yolo 30FPS has ~30ms idle between frames)")
    ap.add_argument("--min-gpu-ops", type=int, default=2,
                    help="Filter out periods with fewer than N GPU ops "
                         "(kernel launches + memcpys) — drops idle spans")
    ap.add_argument("--container", default=None,
                    help="InfluxDB container tag override (default: scope; "
                         "for '__main__' scope falls back to session name)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print DAG summary, do not persist to graph_store")
    args = ap.parse_args()

    # 1. Load graph, find oort F vertices
    G = graph_store.load_graph(args.session, args.scope)
    oort_ids = [nid for nid, d in G.nodes(data=True)
                if d["v"].t_v == "F" and d["v"].A_v.get("ptype") == "oort"
                and d["v"].A_v.get("gpu_node")]
    log(f"scope {args.scope!r}: {len(oort_ids)} oort F vertices with GPU flag")

    # The oort's "container" tag in InfluxDB points to the role name
    # for multi-container sessions. For standalone sessions (scope
    # __main__), ingest.py tags rows with container=<session-name>.
    # For __composed__ scope we'd need per-oort container, not yet done.
    if args.scope == graph_store.COMPOSED_SCOPE:
        log("WARN: running on __composed__ scope is not supported for DAG "
            "extraction (need per-container context). Pass the role scope.")
        return
    if args.container:
        container = args.container
    elif args.scope == graph_store.STANDALONE_SCOPE:
        container = args.session
    else:
        container = args.scope

    influx = connect_influx()
    any_updated = False

    for f_id in oort_ids:
        v = G.nodes[f_id]["v"]
        tid = v.A_v.get("thread_tid")
        if tid is None:
            log(f"  F#{f_id} {v.A_v['label']}: no thread_tid, skipping")
            continue
        log(f"F#{f_id} {v.A_v['label']}  (tid={tid})")
        dags = extract_for_oort(args.session, args.scope, f_id, int(tid),
                                container, gap_ms=args.gap_ms,
                                min_gpu_ops=args.min_gpu_ops, influx=influx)

        if not dags:
            log("  no DAGs extracted (no CUDA activity on this tid)")
            continue

        log(f"  DAG shapes: {len(dags)}")
        for i, d in enumerate(dags):
            n_cpu = sum(1 for n in d["nodes"] if n["pe"] == "CPU")
            n_sm  = sum(1 for n in d["nodes"] if n["pe"] == "SM")
            n_ce  = sum(1 for n in d["nodes"] if n["pe"] == "CE")
            phase = d.get("phase", "?")
            log(f"    [{i}] {phase:12s} sig={d['signature']}  "
                f"count={d['count']:,}  nodes={len(d['nodes'])} "
                f"(CPU={n_cpu}, SM={n_sm}, CE={n_ce})  edges={len(d['edges'])}")

        if not args.dry_run:
            graph_store.update_node(
                args.session, args.scope, f_id,
                attrs={"gpu_dags": dags},
                actor="gpu_dag",
                note=f"{len(dags)} unique DAG shape(s); "
                     f"total invocations={sum(d['count'] for d in dags)}",
            )
            log(f"  saved gpu_dags to graph_store "
                f"(db={args.session} scope={args.scope} node={f_id})")
            any_updated = True

    if any_updated and args.scope != graph_store.COMPOSED_SCOPE:
        # Also propagate to composed graph if the F vertex exists there
        # under the remapped id.
        log("note: composed-scope graph also stores the same F under a "
            "remapped id — run `fish_compose --from-cache` to refresh it.")


if __name__ == "__main__":
    main()
