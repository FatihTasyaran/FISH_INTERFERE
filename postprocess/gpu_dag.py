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


def build_typed_dag(api_rows, kernels_by_corr, memcpys_by_corr):
    """Build a Typed DAG (CPU/SM/CE nodes + precedence edges) from one
    invocation's CUDA activity. Inputs:
      api_rows          list of cuda_runtime rows sorted by time (the thread's CPU driver calls)
      kernels_by_corr   {correlationId: gpu_kernels row}
      memcpys_by_corr   {correlationId: gpu_memcpy row}
    Returns dict with 'nodes' + 'edges'.
    """
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

        # Match kernel
        krow = kernels_by_corr.get(corr)
        mrow = memcpys_by_corr.get(corr)
        gpu_node_idx = None
        if krow is not None:
            gpu_node_idx = len(nodes)
            # Approximate number of SMs from launch config: total threads / 32 (warp),
            # ceiled → how many warp-sized chunks the kernel launched.
            gx, gy, gz = (int(krow.get(k) or 1) for k in ("gridX", "gridY", "gridZ"))
            bx, by, bz = (int(krow.get(k) or 1) for k in ("blockX", "blockY", "blockZ"))
            blocks = gx * gy * gz
            threads_per_block = bx * by * bz
            nodes.append({
                "idx": gpu_node_idx,
                "pe": "SM",
                "name": krow.get("kernel_short_name") or krow.get("kernel_name") or "<anon>",
                "blocks": blocks,
                "threads_per_block": threads_per_block,
                "stream": int(krow.get("streamId") or 0),
                "duration_ns": int(krow.get("duration_ns") or 0),
            })
            edges.append((cpu_idx, gpu_node_idx, "launch"))
        elif mrow is not None:
            gpu_node_idx = len(nodes)
            copy_kind = int(mrow.get("copyKind") or 0)
            # 1 = H2D, 2 = D2H, 3 = D2D, 8 = HtoH (rough mapping per CUPTI)
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
            # Sync memcpy: per PDF §4.7, the CPU blocks until the CE finishes,
            # so a second CPU node should chain from the CE. We fold this into
            # the next CPU node's cpu_order edge naturally — the GPU work is
            # a "hole" in CPU time that the algorithm's duration accounting
            # already covers. (Keep simple for MVP.)

        # Within-stream FIFO ordering (A4): connect to previous GPU node on same stream
        if gpu_node_idx is not None and "stream" in nodes[gpu_node_idx]:
            s = nodes[gpu_node_idx]["stream"]
            if s in stream_last:
                edges.append((stream_last[s], gpu_node_idx, "stream_fifo"))
            stream_last[s] = gpu_node_idx

    return {"nodes": nodes, "edges": edges}


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

    # 2. Detect periods via sync-boundary pattern
    periods = detect_periods(times_ns, api_names, gap_ns)
    productive = filter_productive(periods, api_names, min_gpu_ops=min_gpu_ops)
    log(f"  periods (sync-boundary, gap>{gap_ms}ms): {len(periods):,} total "
        f"→ {len(productive):,} productive (≥{min_gpu_ops} GPU ops)")
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
    log(f"  kernels={len(kernels_by_corr):,}  memcpys={len(memcpys_by_corr):,}  indexed in {time.time()-t1:.1f}s")

    # 4. Build per-invocation DAG, collect by signature
    t2 = time.time()
    unique = {}   # sig → {count, dag (template), c_min/c_max per node}
    for start, end in periods:
        api_rows = api_rows_raw[start:end+1]
        dag = build_typed_dag(api_rows, kernels_by_corr, memcpys_by_corr)
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

    # 5. Summary list: one dict per unique DAG
    out = []
    for sig, info in sorted(unique.items(), key=lambda kv: -kv[1]["count"]):
        # Attach BCET/WCET into the template nodes
        tmpl_nodes = []
        for i, node in enumerate(info["template"]["nodes"]):
            tmpl_nodes.append({
                **{k: v for k, v in node.items() if k != "duration_ns"},
                "c_min_ns": info["c_min"][i],
                "c_max_ns": info["c_max"][i],
            })
        out.append({
            "signature": sig,
            "count": info["count"],
            "nodes": tmpl_nodes,
            "edges": info["template"]["edges"],
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
    ap.add_argument("--dry-run", action="store_true",
                    help="Print DAG summary, do not persist to graph_store")
    args = ap.parse_args()

    # 1. Load graph, find oort F vertices
    G = graph_store.load_graph(args.session, args.scope)
    oort_ids = [nid for nid, d in G.nodes(data=True)
                if d["v"].t_v == "F" and d["v"].A_v.get("ptype") == "oort"
                and d["v"].A_v.get("gpu_node")]
    log(f"scope {args.scope!r}: {len(oort_ids)} oort F vertices with GPU flag")

    # The oort's "container" = the role; for the __composed__ scope we need
    # to look at each oort's original container. For now, allow --scope role
    # directly — caller passes the per-container scope.
    container = args.scope
    if container == graph_store.COMPOSED_SCOPE:
        log("WARN: running on __composed__ scope is not supported for DAG "
            "extraction (need per-container context). Pass the role scope.")
        return

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
        for i, d in enumerate(dags[:5]):
            n_cpu = sum(1 for n in d["nodes"] if n["pe"] == "CPU")
            n_sm  = sum(1 for n in d["nodes"] if n["pe"] == "SM")
            n_ce  = sum(1 for n in d["nodes"] if n["pe"] == "CE")
            log(f"    [{i}] sig={d['signature']}  count={d['count']:,}  "
                f"nodes={len(d['nodes'])} (CPU={n_cpu}, SM={n_sm}, CE={n_ce})  "
                f"edges={len(d['edges'])}")

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
