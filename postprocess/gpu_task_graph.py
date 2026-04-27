"""gpu_task_graph.py — process-centric GPU task-graph extraction.

Runs AFTER model_improved.py. For each GPU-profiled process in the
graph_store graph, applies the stream-bridge + SYNC-aware attribution
(notes/stream_bridge.txt) to assign CUDA activity to ros2 callbacks,
then derives:

  - per callback-F-vertex (gpu_node=True, ptype!=oort):
      gpu_dags → [ {signature, count, phase, nodes, edges, streams}, ... ]
      stored as F.A_v["gpu_dags"] in graph_store.

  - per process (anchored on the EX vertex of that pid):
      stream_wait_counts      → {streamId: n}  (deterministic)
      stream_dag_edges        → [{src, dst, count}, ...]  (heuristic)
      callback_bound_fraction → float (reporting stat)
      oort_residual_fraction  → float
      stored as EX.A_v["gpu_task_graph"] in graph_store.

Data sources:
  - nsys sqlite for each gpu-profiled pid (authoritative; TZ-safe via
    gpu_attribution._session_start_ns_tz_safe, avoiding ISSUE-002)
  - MongoDB ros2_trace for callback_start/callback_end events

This replaces gpu_dag.py's sync-boundary oort-only path for rclcpp
workloads. gpu_dag.py stays in place for pure-oort (no-callback) cases
like yolo_py.

Run:
    python3 gpu_task_graph.py <session> <scope> \\
        [--nsys-dir /path/to/session/nsys] [--dry-run]

See notes/three_flags_gpu_model.txt, notes/stream_bridge.txt,
notes/gpu_mapping_datapoints.txt, notes/paper_important_points.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from collections import Counter, defaultdict

from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_store
import gpu_attribution as ga


MONGO_URI = os.environ.get("FISH_MONGO_URI", "mongodb://localhost:27017")


def log(msg):
    print(f"[gpu_task_graph] {msg}", flush=True)


# ---------------------------------------------------------------------------
# DAG construction per callback invocation
# ---------------------------------------------------------------------------

def _build_invocation_dag(calls, kernels_by_corr, memcpys_by_corr,
                           memsets_by_corr):
    """Build a Typed DAG (CPU/SM/CE nodes + edges) from one callback
    invocation's host API calls. Returns {nodes, edges, streams}."""
    nodes = []; edges = []
    stream_last = {}
    prev_cpu = None
    streams_used = set()

    for api in calls:
        corr = api["correlationId"]
        cpu_idx = len(nodes)
        nodes.append({"idx": cpu_idx, "pe": "CPU",
                      "name": api["api_name"], "corr_id": corr})
        if prev_cpu is not None:
            edges.append((prev_cpu, cpu_idx, "cpu_order"))
        prev_cpu = cpu_idx

        krow = kernels_by_corr.get(corr)
        mrow = memcpys_by_corr.get(corr)
        msrow = memsets_by_corr.get(corr)
        gpu_idx = None
        if krow is not None:
            gpu_idx = len(nodes)
            nodes.append({"idx": gpu_idx, "pe": "SM",
                          "name": krow.get("short_name", "<anon>"),
                          "stream": int(krow["streamId"] or 0)})
            edges.append((cpu_idx, gpu_idx, "launch"))
            streams_used.add(krow["streamId"])
        elif mrow is not None:
            gpu_idx = len(nodes)
            kind = int(mrow.get("copyKind") or 0)
            direction = {1:"H2D",2:"D2H",3:"D2D",8:"H2H"}.get(kind, f"k{kind}")
            nodes.append({"idx": gpu_idx, "pe": "CE",
                          "name": f"memcpy_{direction}",
                          "stream": int(mrow["streamId"] or 0)})
            edges.append((cpu_idx, gpu_idx, "launch"))
            streams_used.add(mrow["streamId"])
        elif msrow is not None:
            gpu_idx = len(nodes)
            nodes.append({"idx": gpu_idx, "pe": "SM",
                          "name": "memset_fill",
                          "stream": int(msrow["streamId"] or 0)})
            edges.append((cpu_idx, gpu_idx, "launch"))
            streams_used.add(msrow["streamId"])

        if gpu_idx is not None:
            s = nodes[gpu_idx]["stream"]
            if s in stream_last:
                edges.append((stream_last[s], gpu_idx, "stream_fifo"))
            stream_last[s] = gpu_idx

    return {"nodes": nodes, "edges": edges,
            "streams": sorted(int(s) for s in streams_used if s is not None)}


def _dag_signature(dag):
    node_sig = tuple((n["pe"], n["name"]) for n in dag["nodes"])
    edge_sig = tuple(sorted((u, v, lbl) for u, v, lbl in dag["edges"]))
    return hashlib.sha1(repr((node_sig, edge_sig)).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Per-process extraction
# ---------------------------------------------------------------------------

def extract_for_process(session, pid, *, nsys_dir, mongo_db,
                         gpu_pids_from_graph=None):
    """Run the full attribution + DAG extraction for one pid. Returns a
    dict with per-callback-cb_addr DAG summaries and process-level
    metrics. No graph_store writes — caller persists.
    """
    sqlite_path = ga.find_sqlite_for_pid(nsys_dir, pid)
    if sqlite_path is None:
        log(f"  pid={pid}: no sqlite with CUDA activity, skipping")
        return None

    log(f"  pid={pid}: sqlite={os.path.basename(sqlite_path)}")
    t0 = time.time()
    cuda = ga.load_process_cuda(sqlite_path, pid)
    windows = ga.load_callback_windows(mongo_db, pid)
    log(f"  pid={pid}: runtime={len(cuda['runtime'])} kernels={len(cuda['kernels'])} "
        f"windows={len(windows)}  ({time.time()-t0:.1f}s)")

    if not windows:
        log(f"  pid={pid}: no callback windows — use gpu_dag.py oort path instead")
        return {"pid": pid, "no_callbacks": True,
                "runtime_total": len(cuda["runtime"])}

    # Stream-bridge attribution
    claims, corr_to_stream = ga.compute_stream_ownership(cuda, windows)
    release_times = ga.sync_release_times(cuda)
    attr = ga.attribute_runtime(cuda, windows, claims, corr_to_stream,
                                 release_times, gpu_producing_only=True)

    # Indexed lookup tables for per-invocation DAG construction
    kernels_by_corr = {r["correlationId"]: r for r in cuda["kernels"]}
    memcpys_by_corr = {r["correlationId"]: r for r in cuda["memcpys"]}
    memsets_by_corr = {r["correlationId"]: r for r in cuda["memsets"]}

    # Group runtime rows by owning callback (cb_idx).
    # Note: attr["per_row_owner_cb"] is index-aligned with the gpu-producing
    # subset of cuda["runtime"]. Rebuild per-row owner by iterating only
    # GPU-producing rows in order.
    gpu_runtime = [r for r in cuda["runtime"]
                    if r["correlationId"] in corr_to_stream]
    assert len(gpu_runtime) == len(attr["per_row_owner_cb"])
    rows_by_cb = defaultdict(list)
    for r, cb_idx in zip(gpu_runtime, attr["per_row_owner_cb"]):
        if cb_idx is not None:
            rows_by_cb[cb_idx].append(r)

    # Per cb_addr: collect unique shapes across invocations
    per_cb_addr = defaultdict(lambda: {
        "invocations": 0,
        "shapes": {},  # sig → {count, template, streams}
        "streams": set(),
    })
    for cb_idx, calls in rows_by_cb.items():
        cb_addr = windows[cb_idx]["cb_addr"]
        dag = _build_invocation_dag(calls, kernels_by_corr,
                                     memcpys_by_corr, memsets_by_corr)
        if not dag["nodes"]:
            continue
        sig = _dag_signature(dag)
        info = per_cb_addr[cb_addr]
        info["invocations"] += 1
        info["streams"].update(dag["streams"])
        sh = info["shapes"].setdefault(sig, {
            "count": 0, "template": dag, "streams": set(),
        })
        sh["count"] += 1
        sh["streams"].update(dag["streams"])

    # Compact shapes into list form
    cb_dags = {}
    for cb_addr, info in per_cb_addr.items():
        shapes = []
        for sig, sh in sorted(info["shapes"].items(),
                               key=lambda kv: -kv[1]["count"]):
            t = sh["template"]
            n_cpu = sum(1 for n in t["nodes"] if n["pe"] == "CPU")
            n_sm  = sum(1 for n in t["nodes"] if n["pe"] == "SM")
            n_ce  = sum(1 for n in t["nodes"] if n["pe"] == "CE")
            shapes.append({
                "signature": sig,
                "count": sh["count"],
                "n_cpu": n_cpu, "n_sm": n_sm, "n_ce": n_ce,
                "streams": sorted(list(sh["streams"])),
                "nodes": t["nodes"],
                "edges": t["edges"],
            })
        cb_dags[cb_addr] = {
            "invocations": info["invocations"],
            "streams": sorted(list(info["streams"])),
            "shapes": shapes,
        }

    # Process-level: deterministic wait counts + heuristic DAG edges
    wait_counts = ga.precedence_edges_from_waits(cuda)
    dag_edges, dag_unresolved = ga.stream_precedence_edges_heuristic(cuda)

    n = attr["n"] or 1
    return {
        "pid": pid,
        "pid_name": cuda["pid_name"],
        "runtime_total": len(cuda["runtime"]),
        "gpu_producing_total": attr["n"],
        "callback_bound_time_frac":   attr["callback_bound_time"] / n,
        "callback_bound_stream_frac": attr["callback_bound_stream"] / n,
        "oort_post_release_frac":     attr["oort_post_release"] / n,
        "oort_frac":                  attr["oort"] / n,
        "stream_wait_counts": {str(k): v for k, v in wait_counts.items()},
        "stream_dag_edges": [
            {"src": int(src), "dst": int(dst), "count": cnt}
            for (src, dst), cnt in dag_edges.most_common()
        ],
        "stream_dag_unresolved": dag_unresolved,
        "sync_types": {str(k): v for k, v in
                        Counter(s["syncType"] for s in cuda["syncs"]).items()},
        "cb_dags": cb_dags,
    }


# ---------------------------------------------------------------------------
# Graph-store persistence
# ---------------------------------------------------------------------------

def persist_to_graph_store(session, scope, process_result, *, dry_run=False):
    """Write the process_result back into graph_store:
      - per callback cb_addr: find matching F vertex (ptype=cpu, cb_addr)
        and set F.A_v["gpu_dags"] = shapes
      - per process: find the EX vertex (pid match) and set
        EX.A_v["gpu_task_graph"] = process-level metrics
    """
    if process_result.get("no_callbacks"):
        log(f"  pid={process_result['pid']}: no callbacks, skipping persist")
        return 0

    G = graph_store.load_graph(session, scope)
    pid = process_result["pid"]
    updates = 0

    # Map callback cb_addr (hex string from ros2_trace) to F vertex id
    cb_to_fid = {}
    ex_fid = None
    for nid, data in G.nodes(data=True):
        v = data["v"]
        if v.t_v == "F":
            cb = v.A_v.get("cb_addr")
            if cb and cb != "NA":
                cb_to_fid[cb] = nid
        elif v.t_v == "EX" and v.A_v.get("pid") == pid:
            ex_fid = nid

    for cb_addr, info in process_result["cb_dags"].items():
        fid = cb_to_fid.get(cb_addr)
        if fid is None:
            continue
        shapes = info["shapes"]
        if dry_run:
            log(f"    [dry-run] F#{fid} cb={cb_addr} → {len(shapes)} shapes, "
                f"{info['invocations']} invocations")
            continue
        graph_store.update_node(
            session, scope, fid,
            attrs={"gpu_dags": shapes,
                   "gpu_invocations": info["invocations"],
                   "gpu_streams": info["streams"]},
            actor="gpu_task_graph",
            note=f"pid={pid} shapes={len(shapes)} inv={info['invocations']}",
        )
        updates += 1

    if ex_fid is not None:
        meta = {
            "pid_name": process_result["pid_name"],
            "runtime_total": process_result["runtime_total"],
            "gpu_producing_total": process_result["gpu_producing_total"],
            "callback_bound_time_frac": process_result["callback_bound_time_frac"],
            "callback_bound_stream_frac": process_result["callback_bound_stream_frac"],
            "oort_post_release_frac": process_result["oort_post_release_frac"],
            "oort_frac": process_result["oort_frac"],
            "stream_wait_counts": process_result["stream_wait_counts"],
            "stream_dag_edges": process_result["stream_dag_edges"],
            "stream_dag_unresolved": process_result["stream_dag_unresolved"],
            "sync_types": process_result["sync_types"],
        }
        if dry_run:
            log(f"    [dry-run] EX#{ex_fid} → gpu_task_graph meta "
                f"({len(meta)} fields)")
        else:
            graph_store.update_node(
                session, scope, ex_fid,
                attrs={"gpu_task_graph": meta},
                actor="gpu_task_graph",
                note=f"process-level metrics for pid={pid}",
            )
            updates += 1
    return updates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", help="MongoDB session DB (= graph_store db_name)")
    ap.add_argument("scope", help="Graph scope (role or __main__)")
    ap.add_argument("--nsys-dir", default=None,
                    help="Override nsys directory (default: "
                         "~/fish_traces/<session>/nsys)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but do not persist to graph_store")
    args = ap.parse_args()

    nsys_dir = args.nsys_dir or os.path.expanduser(
        f"~/fish_traces/{args.session}/nsys")
    if not os.path.isdir(nsys_dir):
        log(f"ERROR: nsys dir not found: {nsys_dir}")
        sys.exit(1)

    G = graph_store.load_graph(args.session, args.scope)

    # Find gpu-profiled pids: EX vertices whose pid is in
    # nsys_profiled_pids(). We read the graph's existing flags: any F
    # marked gpu_node=True has a pid reachable via its parent EX.
    gpu_pids = set()
    for nid, data in G.nodes(data=True):
        v = data["v"]
        if v.t_v == "F" and v.A_v.get("gpu_node"):
            # walk up the parent chain via children-map (inverse)
            pass  # we use EX-level instead below
    # Simpler: every EX whose pid has any F.gpu_node=True descendant
    ex_pids = {}
    child_parent = {}
    for nid, data in G.nodes(data=True):
        v = data["v"]
        for c in v.Z_v:
            child_parent[c] = nid
        if v.t_v == "EX" and v.A_v.get("pid") is not None:
            ex_pids[v.A_v["pid"]] = nid
    # Find pids with any gpu_node=True F descendant
    for nid, data in G.nodes(data=True):
        v = data["v"]
        if v.t_v != "F" or not v.A_v.get("gpu_node"):
            continue
        cur = nid
        while cur in child_parent:
            cur = child_parent[cur]
            if G.nodes[cur]["v"].t_v == "EX":
                pid = G.nodes[cur]["v"].A_v.get("pid")
                if pid is not None:
                    gpu_pids.add(pid)
                break

    if not gpu_pids:
        log("No gpu-profiled pids found in graph; nothing to do.")
        return
    log(f"gpu-profiled pids: {sorted(gpu_pids)}")

    mongo = MongoClient(MONGO_URI)[args.session]

    total_updates = 0
    for pid in sorted(gpu_pids):
        try:
            result = extract_for_process(args.session, pid,
                                           nsys_dir=nsys_dir, mongo_db=mongo)
        except Exception as e:
            log(f"  pid={pid}: ERROR {e}")
            continue
        if result is None:
            continue
        updates = persist_to_graph_store(args.session, args.scope, result,
                                           dry_run=args.dry_run)
        total_updates += updates
        log(f"  pid={pid}: updates={updates}  "
            f"cbs={len(result.get('cb_dags', {}))}")

    log(f"Total graph_store updates: {total_updates}"
        + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
