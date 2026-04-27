#!/usr/bin/env python3
"""shape_phase_classifier — rule-based phase class per gpu_dag signature.

Operates per callback (F vertex). For each unique shape signature
of the callback's gpu_dags, assigns one class:

  shutdown : the signature whose latest invocation is at or near
             the very end of the trace window for this callback
  init     : signature whose CPU API mix scores as initialization
             work (cuda-runtime init-signal tokens), corroborated by
             H2D-only memcpys. Score must clear a threshold AND
             memcpys must be H2D-only (or no memcpys). H2D-only
             alone is NOT enough — it would mis-flag trivial
             early-return paths that happen to do a single upload.
  dominant : highest-count signature
  anomaly  : structural outlier on n_cpu / n_sm / n_ce, by ANY of:
             (a) classic mean ± k·σ (k=sigma)
             (b) robust modified z-score |0.6745·(x-median)/MAD| > k
                 (catches outliers even when σ is inflated by other
                 outliers — heavy-tailed distributions)
             (c) trivially-small total: total_nodes < median_total / r
                 (catches early-return / no-op shapes that mean+σ
                 misses because trivial shapes pull σ very little)
  variant  : everything else (default fallback)

Priority order (highest wins):
  shutdown > init > dominant > anomaly > variant

The classifier needs per-invocation timestamps to detect shutdown.
It re-runs the per-window DAG extraction so that
  (invocation_idx, time, signature)
is reconstructed; the existing graph_store gpu_dags loses this
mapping when it groups invocations by signature.

Usage:
  python3 shape_phase_classifier.py <session> <F#> [--sigma 2.0]
                                    [--shutdown-tail 0.05]

Sigma:
  sigma=2.0 → ~5% of a normal sample falls outside; conservative.
  sigma=1.0 → ~32% outside; aggressive, surfaces small variations
              as anomalies.
  Default 2.0 in keeping with standard outlier conventions; with
  small samples (a callback may have 3–30 sigs) try 1.0 for paper
  visualizations of "interesting variation".

shutdown-tail:
  Fraction of the last invocations to consider as "the wind-down
  window". Default 0.05 means: any signature whose latest invocation
  falls in the final 5% of the callback's invocation timeline gets
  the shutdown label IF it does not appear earlier (otherwise it is
  ongoing, not a shutdown shape). Set to 0 to require strictly the
  single last invocation.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_store
from gpu_attribution import (find_sqlite_for_pid, load_process_cuda,
                             load_callback_windows,
                             compute_stream_ownership, sync_release_times,
                             attribute_runtime)
from gpu_task_graph import _build_invocation_dag, _dag_signature
from model_improved import connect_mongo


# ---------------------------------------------------------------------------
# Initialization-signal scoring
# (mirrors notes/phase_detector.txt § "init_score", scaled for shape-level
# rather than period-level use; weak signals like cudaMalloc dominate at
# this granularity since strong signals like cuInit only appear in the
# process startup period and never inside callback windows.)
# ---------------------------------------------------------------------------

INIT_SIGNALS = [
    # (token,                       threshold, weight)
    # threshold = None → per-occurrence (per-count weak signal)
    # threshold = N    → if count >= N, add weight (threshold-gated hard signal)
    ("cuInit",                       1,    100),
    ("cuLibraryLoadData",            1,    100),
    ("cuDevicePrimaryCtxRetain",     1,    100),
    ("cuCtxCreate",                  1,    100),
    ("cudaGetDeviceProperties",      1,     20),
    ("cuModuleGetLoadingMode",       1,     20),
    ("cuModuleLoadFatBinary",        1,     10),
    ("cuLibraryUnload",              1,     10),
    ("cuGetProcAddress",           100,     50),
    ("cudaMalloc",                None,      5),
    ("cudaStreamCreate",          None,      3),
    ("cudaEventCreate",           None,      1),
    ("cudaEventCreateWithFlags",  None,      1),
]

INIT_HARD_THRESHOLD = 100   # cuInit etc.
INIT_SOFT_THRESHOLD = 20    # alloc-heavy + props lookup, needs corroboration


def _init_score(cpu_names):
    counts = {}
    for n in cpu_names:
        for tok, _t, _w in INIT_SIGNALS:
            if tok in n:
                counts[tok] = counts.get(tok, 0) + 1
    score = 0
    for tok, threshold, weight in INIT_SIGNALS:
        c = counts.get(tok, 0)
        if threshold is None:
            score += weight * c
        elif c >= threshold:
            score += weight
    return score, counts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_f_vertex(G, f_id):
    if f_id in G.nodes:
        return G.nodes[f_id]["v"]
    raise SystemExit(f"F#{f_id} not found in graph")


def _walk_to_pid(G, f_id):
    """Walk parent chain F -> E -> N -> EX (which has pid)."""
    child_parent = {}
    for nid, data in G.nodes(data=True):
        for c in data["v"].Z_v:
            child_parent[c] = nid
    cur = f_id
    while cur in child_parent:
        cur = child_parent[cur]
        if G.nodes[cur]["v"].t_v == "EX":
            return G.nodes[cur]["v"].A_v.get("pid")
    return None


def _extract_invocations(session, f_id, nsys_dir=None):
    """Re-extract per-invocation (start_ns, end_ns, signature, dag) tuples
    for a callback by replaying the gpu_task_graph extraction flow
    (stream-bridge attribution) and keeping the invocation→signature
    mapping that gpu_task_graph drops when grouping by signature.
    """
    G = graph_store.load_graph(session, "__main__")
    v = _find_f_vertex(G, f_id)
    cb_addr = v.A_v.get("cb_addr")
    if not cb_addr:
        raise SystemExit(f"F#{f_id} has no cb_addr — not a runtime callback?")

    pid = _walk_to_pid(G, f_id)
    if pid is None:
        raise SystemExit(f"F#{f_id} has no parent EX with pid")

    nsys_dir = nsys_dir or os.path.expanduser(f"~/fish_traces/{session}/nsys")
    sqlite_path = find_sqlite_for_pid(nsys_dir, pid)
    if not sqlite_path:
        raise SystemExit(f"No nsys sqlite for pid={pid} in {nsys_dir}")

    print(f"[phase] F#{f_id} cb_addr={cb_addr} pid={pid}")
    print(f"[phase] sqlite: {os.path.basename(sqlite_path)}")

    mongo = connect_mongo(session, role=None)
    cuda = load_process_cuda(sqlite_path, pid)
    windows = load_callback_windows(mongo, pid)
    target_idxs = [i for i, w in enumerate(windows) if w["cb_addr"] == cb_addr]
    print(f"[phase] all-cb windows: {len(windows)}, F#{f_id} windows: "
          f"{len(target_idxs)}, cuda runtime rows: {len(cuda['runtime'])}")

    # Stream-bridge attribution (same as gpu_task_graph)
    claims, corr_to_stream = compute_stream_ownership(cuda, windows)
    release_times = sync_release_times(cuda)
    attr = attribute_runtime(cuda, windows, claims, corr_to_stream,
                              release_times, gpu_producing_only=True)

    gpu_runtime = [r for r in cuda["runtime"]
                    if r["correlationId"] in corr_to_stream]
    assert len(gpu_runtime) == len(attr["per_row_owner_cb"])

    # Group rows by owning cb_idx, but only for our target cb_addr
    target_set = set(target_idxs)
    rows_by_cb = {}
    for r, cb_idx in zip(gpu_runtime, attr["per_row_owner_cb"]):
        if cb_idx is not None and cb_idx in target_set:
            rows_by_cb.setdefault(cb_idx, []).append(r)

    kernels_by_corr = {r["correlationId"]: r for r in cuda["kernels"]}
    memcpys_by_corr = {r["correlationId"]: r for r in cuda["memcpys"]}
    memsets_by_corr = {r["correlationId"]: r for r in cuda["memsets"]}

    invocations = []
    for cb_idx in sorted(rows_by_cb):
        calls = rows_by_cb[cb_idx]
        w = windows[cb_idx]
        dag = _build_invocation_dag(
            calls, kernels_by_corr, memcpys_by_corr, memsets_by_corr)
        if not dag["nodes"]:
            continue
        sig = _dag_signature(dag)
        invocations.append({
            "start_ns": w["cb_start_ns"],
            "end_ns": w["cb_end_ns"],
            "signature": sig,
            "dag": dag,
        })
    return invocations, v


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

def _per_sig_stats(invocations):
    """Aggregate per-signature counts, time bounds, and node-type tallies.

    Also computes H2D / D2H memcpy counts by inspecting node names.
    """
    by_sig = {}
    for inv in invocations:
        s = inv["signature"]
        d = inv["dag"]
        if s not in by_sig:
            by_sig[s] = {
                "count": 0,
                "first_ns": inv["start_ns"],
                "last_ns": inv["end_ns"],
                "n_cpu": 0, "n_sm": 0, "n_ce": 0,
                "h2d": 0, "d2h": 0, "other_mc": 0,
            }
        e = by_sig[s]
        e["count"] += 1
        e["first_ns"] = min(e["first_ns"], inv["start_ns"])
        e["last_ns"] = max(e["last_ns"], inv["end_ns"])
        # CPU/SM/CE counts (canonical from dag entry on first sight)
        if e["n_cpu"] == 0 and e["n_sm"] == 0 and e["n_ce"] == 0:
            for n in d["nodes"]:
                if n["pe"] == "CPU": e["n_cpu"] += 1
                elif n["pe"] == "SM": e["n_sm"] += 1
                elif n["pe"] == "CE": e["n_ce"] += 1
        # H2D / D2H tally — direction stored as memcpy_H2D / memcpy_D2H name
        for n in d["nodes"]:
            if n["pe"] != "CE": continue
            nm = n.get("name", "")
            if nm == "memcpy_H2D":  e["h2d"] += 1
            elif nm == "memcpy_D2H": e["d2h"] += 1
            elif nm.startswith("memcpy_"):     e["other_mc"] += 1
            elif nm == "memset":               pass  # not a memcpy
        # We only need direction tally per UNIQUE shape; skip duplicates
        # (the loop above re-counts per invocation, but that doesn't
        # affect H2D-only test). Keep it simple — count direction only
        # for the FIRST occurrence by clearing on first-set:
    # Recompute h2d/d2h + init_score cleanly: only over the canonical
    # first dag per sig (one shape = one set of stats, regardless of how
    # many invocations share it).
    seen = set()
    canonical = {}
    for inv in invocations:
        s = inv["signature"]
        if s in seen: continue
        seen.add(s)
        canonical[s] = inv["dag"]
    for s, d in canonical.items():
        h2d = d2h = other = 0
        for n in d["nodes"]:
            if n["pe"] != "CE": continue
            nm = n.get("name", "")
            if nm == "memcpy_H2D":  h2d += 1
            elif nm == "memcpy_D2H": d2h += 1
            elif nm.startswith("memcpy_"):     other += 1
        by_sig[s]["h2d"] = h2d
        by_sig[s]["d2h"] = d2h
        by_sig[s]["other_mc"] = other
        cpu_names = [n["name"] for n in d["nodes"] if n["pe"] == "CPU"]
        score, _ = _init_score(cpu_names)
        by_sig[s]["init_score"] = score
    return by_sig


def _classify(by_sig, total_invocations, sigma=2.0, shutdown_tail=0.05):
    """Return {sig → class} and a stats dict for diagnostics."""
    if not by_sig:
        return {}, {}

    sigs = list(by_sig.keys())

    # Anomaly: combine three tests on n_cpu / n_sm / n_ce.
    axes = ["n_cpu", "n_sm", "n_ce"]
    axis_stats = {}
    for ax in axes:
        vals = [by_sig[s][ax] for s in sigs]
        m = statistics.mean(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        med = statistics.median(vals)
        # Median absolute deviation (around median)
        mad = statistics.median([abs(v - med) for v in vals])
        axis_stats[ax] = {"mean": m, "stdev": sd, "median": med,
                          "mad": mad,
                          "min": min(vals), "max": max(vals)}

    # Trivial-small: total node count < median_total / TRIVIAL_RATIO.
    # Robust against heavy-tailed distributions where σ is inflated.
    TRIVIAL_RATIO = 5
    totals = [by_sig[s]["n_cpu"] + by_sig[s]["n_sm"] + by_sig[s]["n_ce"]
              for s in sigs]
    median_total = statistics.median(totals) if totals else 0
    trivial_threshold = median_total / TRIVIAL_RATIO

    def is_anomaly(s):
        # (a) mean ± k·σ
        for ax in axes:
            v = by_sig[s][ax]
            m = axis_stats[ax]["mean"]
            sd = axis_stats[ax]["stdev"]
            if sd > 0 and abs(v - m) > sigma * sd:
                return True
        # (b) modified z-score (robust): |0.6745·(x-median)/MAD| > k
        for ax in axes:
            v = by_sig[s][ax]
            med = axis_stats[ax]["median"]
            mad = axis_stats[ax]["mad"]
            if mad > 0 and abs(0.6745 * (v - med) / mad) > sigma:
                return True
        # (c) trivially small total
        total = by_sig[s]["n_cpu"] + by_sig[s]["n_sm"] + by_sig[s]["n_ce"]
        if median_total > 0 and total < trivial_threshold:
            return True
        return False

    # Dominant: highest count (ties broken by signature lex order)
    dominant_sig = max(sigs, key=lambda s: (by_sig[s]["count"], s))

    # Init: combine init-score (cuda-runtime init-signal tokens in the
    # CPU node mix) with H2D-only as supporting signal.
    #   hard  : init_score >= INIT_HARD_THRESHOLD               (100)
    #   soft  : init_score >= INIT_SOFT_THRESHOLD AND H2D-only  (20)
    # H2D-only alone is NOT enough — would mis-flag trivial early-return
    # paths that happen to do a single upload.
    init_sigs = set()
    for s in sigs:
        e = by_sig[s]
        h2d_only = e["h2d"] > 0 and e["d2h"] == 0 and e["other_mc"] == 0
        no_mc    = e["h2d"] == 0 and e["d2h"] == 0 and e["other_mc"] == 0
        if e["init_score"] >= INIT_HARD_THRESHOLD:
            init_sigs.add(s)
        elif e["init_score"] >= INIT_SOFT_THRESHOLD and (h2d_only or no_mc):
            init_sigs.add(s)

    # Shutdown: signature whose latest invocation is in the final
    # `shutdown_tail` fraction of the callback timeline.
    all_last = sorted([(by_sig[s]["last_ns"], s) for s in sigs])
    if not all_last:
        shutdown_sigs = set()
    else:
        global_last = all_last[-1][0]
        global_first = min(by_sig[s]["first_ns"] for s in sigs)
        span = max(global_last - global_first, 1)
        cutoff_ns = global_last - span * shutdown_tail
        shutdown_sigs = set()
        # Strict mode (shutdown_tail==0): only the single last sig
        if shutdown_tail == 0:
            shutdown_sigs.add(all_last[-1][1])
        else:
            for s in sigs:
                if by_sig[s]["last_ns"] >= cutoff_ns and \
                   by_sig[s]["first_ns"] >= cutoff_ns:
                    # Only counts as shutdown if it ALSO did not start
                    # earlier — otherwise it's a recurring shape that
                    # happens to last late.
                    shutdown_sigs.add(s)

    # Apply priority: shutdown > init > dominant > anomaly > variant
    classes = {}
    for s in sigs:
        if s in shutdown_sigs:
            classes[s] = "shutdown"
        elif s in init_sigs:
            classes[s] = "init"
        elif s == dominant_sig:
            classes[s] = "dominant"
        elif is_anomaly(s):
            classes[s] = "anomaly"
        else:
            classes[s] = "variant"

    return classes, {
        "axis_stats": axis_stats,
        "dominant_sig": dominant_sig,
        "init_sigs": init_sigs,
        "shutdown_sigs": shutdown_sigs,
        "sigma": sigma,
        "shutdown_tail": shutdown_tail,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("f_id", type=int, help="F vertex id")
    ap.add_argument("--sigma", type=float, default=2.0)
    ap.add_argument("--shutdown-tail", type=float, default=0.05)
    ap.add_argument("--nsys-dir", default=None)
    args = ap.parse_args()

    invocations, v = _extract_invocations(
        args.session, args.f_id, nsys_dir=args.nsys_dir)
    if not invocations:
        raise SystemExit("No invocations recovered for this callback.")

    by_sig = _per_sig_stats(invocations)
    classes, diag = _classify(by_sig, len(invocations),
                              sigma=args.sigma,
                              shutdown_tail=args.shutdown_tail)

    print()
    print(f"=== F#{args.f_id} — {v.A_v.get('label','')[:80]} ===")
    print(f"invocations: {len(invocations)}, distinct signatures: {len(by_sig)}")
    print(f"sigma={args.sigma}, shutdown_tail={args.shutdown_tail}")
    print()
    print("axis stats (across signatures):")
    for ax, st in diag["axis_stats"].items():
        print(f"  {ax}: mean={st['mean']:.1f}  stdev={st['stdev']:.1f}  "
              f"median={st['median']:.0f}  mad={st['mad']:.1f}  "
              f"min={st['min']}  max={st['max']}")
    print()

    # Sort by count desc, then by class
    rows = sorted(by_sig.items(),
                  key=lambda kv: (-kv[1]["count"], kv[0]))
    print(f"{'sig':<14} {'class':<9} {'cnt':>3} "
          f"{'CPU':>4} {'SM':>4} {'CE':>4} "
          f"{'H2D':>4} {'D2H':>4} {'init':>5}  "
          f"{'first(s)':>9} {'last(s)':>9}")
    t0_global = min(by_sig[s]["first_ns"] for s in by_sig)
    for s, e in rows:
        cls = classes[s]
        first_s = (e["first_ns"] - t0_global) / 1e9
        last_s = (e["last_ns"] - t0_global) / 1e9
        print(f"{s:<14} {cls:<9} {e['count']:>3} "
              f"{e['n_cpu']:>4} {e['n_sm']:>4} {e['n_ce']:>4} "
              f"{e['h2d']:>4} {e['d2h']:>4} {e['init_score']:>5}  "
              f"{first_s:>9.3f} {last_s:>9.3f}")

    # Class summary
    cls_counts = Counter(classes.values())
    print()
    print("class summary:")
    for c in ["shutdown", "init", "dominant", "anomaly", "variant"]:
        print(f"  {c:<9} {cls_counts.get(c, 0):>3}")


if __name__ == "__main__":
    main()
