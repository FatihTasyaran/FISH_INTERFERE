"""GPU attribution library — process-centric stream-bridge & SYNC analysis.

Reusable functions for attributing CUDA host API calls to ros2 callback
windows via the three-flag model (notes/three_flags_gpu_model.txt) and
the stream-bridge algorithm (notes/stream_bridge.txt).

Callers (as of 2026-04-20):
  - postprocess/analyze_process_gpu.py  (stress-test CLI, rerun-free)
  - postprocess/gpu_task_graph.py       (main pipeline, writes graph_store)

Data-source convention:
  Per-process nsys sqlite is the authoritative source for CUDA activity
  and SYNC rows (InfluxDB's `cuda_runtime` / `gpu_sync` measurements
  concatenate across nsys files under a single container tag and can
  cross-contaminate pids — see notes/known_issues.txt ISSUE-002 on
  the related epoch-offset quirk).

  MongoDB ros2_trace is the source for callback windows.

Public API:
  find_sqlite_for_pid(nsys_dir, pid)      → sqlite path or None
  load_process_cuda(sqlite_path, pid)     → dict of runtime/kernels/…/syncs
  load_callback_windows(mongo_db, pid)    → list of window dicts
  compute_stream_ownership(cuda, windows) → (claims, corr_to_stream)
  sync_release_times(cuda)                → {streamId: [release_ns…]}
  attribute_runtime(cuda, windows, claims, corr_to_stream, release_times)
      → per-row attribution dict with counts of
          callback_bound_time / callback_bound_stream /
          oort_post_release / oort (gpu-producing subset)
  precedence_edges_from_waits(cuda)       → Counter[dst_stream] (deterministic)
  stream_precedence_edges_heuristic(cuda) → (Counter[(src, dst)], unresolved)

Philosophy:
  - Pure functions. No argparse, no I/O outside sqlite / pymongo.
  - All outputs are plain Python dicts / Counters that serialise to JSON
    and round-trip cleanly into graph_store F.A_v attributes.
  - Analyses are per-process; callers are responsible for iterating
    pids.
"""
from __future__ import annotations

import bisect
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def find_sqlite_for_pid(nsys_dir: str, pid: int) -> str | None:
    """Return the nsys sqlite path that actually observed pid's CUDA
    activity (i.e. whose CUPTI_ACTIVITY_KIND_RUNTIME has rows under
    PROCESSES.pid == pid). Returns None if none match.

    Prefers the sqlite with the most matching rows when multiple report
    activity — an ancestral process may appear in more than one
    session's PROCESSES snapshot but only its owning session has full
    CUPTI records.
    """
    candidates = []
    if not os.path.isdir(nsys_dir):
        return None
    for fname in sorted(os.listdir(nsys_dir)):
        if not fname.endswith(".sqlite"):
            continue
        path = os.path.join(nsys_dir, fname)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "CUPTI_ACTIVITY_KIND_RUNTIME" not in tables:
                conn.close(); continue
            row = conn.execute("""
                SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_RUNTIME r
                JOIN PROCESSES p ON (r.globalTid >> 24) = (p.globalPid >> 24)
                WHERE p.pid = ?
            """, (pid,)).fetchone()
            conn.close()
            if row and row[0] > 0:
                candidates.append((path, row[0]))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[1])
    return candidates[0][0]


def _session_start_ns_tz_safe(conn, tz_name: str | None = None) -> int:
    """Compute the session start time as UTC epoch ns, working around the
    nsys 'utcEpochNs actually is local-epoch-ns' misnomer on our toolchain
    (notes/known_issues.txt ISSUE-002).

    Strategy: parse the ISO-string columns (utcTime / localTime) as local
    wall-clock in the given timezone (default $FISH_TZ or Europe/Amsterdam)
    and convert. Fall back to raw utcEpochNs if strings are missing.
    """
    row = conn.execute(
        "SELECT * FROM TARGET_INFO_SESSION_START_TIME LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    tz = ZoneInfo(tz_name or os.environ.get("FISH_TZ", "Europe/Amsterdam"))
    iso = row[1] if len(row) > 1 and isinstance(row[1], str) else None
    if iso:
        try:
            dt_local = datetime.fromisoformat(iso).replace(tzinfo=tz)
            return int(dt_local.timestamp() * 1e9)
        except ValueError:
            pass
    return int(row[0]) if row[0] is not None else 0


def load_process_cuda(sqlite_path: str, pid: int, *, tz_name: str | None = None):
    """Load all CUDA-side data for one process from an nsys sqlite.

    Returns a dict with:
      pid, pid_name, session_start_ns, tid_counts,
      runtime: [{start, end, tid, correlationId, api_name}, …],
      kernels: [{start, end, streamId, correlationId, short_name}, …],
      memcpys: [{start, end, streamId, correlationId, bytes, copyKind}, …],
      memsets: [{start, end, streamId, correlationId, bytes, value}, …],
      syncs:   [{start, end, streamId, correlationId, syncType}, …],

    All timestamps are wall-clock ns in the system timezone (see
    _session_start_ns_tz_safe). Rows are filtered to the given pid via
    PROCESSES.globalPid prefix — no cross-pid contamination.
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    session_start_ns = _session_start_ns_tz_safe(conn, tz_name=tz_name)
    strids = {r[0]: r[1] for r in conn.execute("SELECT id, value FROM StringIds")}

    proc_rows = list(conn.execute(
        "SELECT globalPid, name FROM PROCESSES WHERE pid = ?", (pid,)))
    if not proc_rows:
        raise RuntimeError(f"pid={pid} not found in PROCESSES of {sqlite_path}")
    gpid_prefixes = {gp >> 24 for gp, _ in proc_rows}
    pid_name = proc_rows[0][1]
    owning_gpid = proc_rows[0][0]

    def _owns(gtid):
        return (gtid >> 24) in gpid_prefixes

    runtime = []
    tid_counts = Counter()
    for start, end, gtid, corr, nid in conn.execute(
        "SELECT start, end, globalTid, correlationId, nameId "
        "FROM CUPTI_ACTIVITY_KIND_RUNTIME"
    ):
        if not _owns(gtid): continue
        tid = gtid & 0xFFFFFF
        runtime.append({
            "start": session_start_ns + start,
            "end":   session_start_ns + (end or start),
            "tid":   tid,
            "correlationId": corr,
            "api_name": strids.get(nid, f"<nid={nid}>"),
        })
        tid_counts[tid] += 1
    runtime.sort(key=lambda r: r["start"])

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    kernels = []
    if "CUPTI_ACTIVITY_KIND_KERNEL" in tables:
        for start, end, sid, corr, shortId in conn.execute(
            "SELECT start, end, streamId, correlationId, shortName "
            "FROM CUPTI_ACTIVITY_KIND_KERNEL"
        ):
            kernels.append({
                "start": session_start_ns + start,
                "end":   session_start_ns + (end or start),
                "streamId": sid, "correlationId": corr,
                "short_name": strids.get(shortId, f"<nid={shortId}>"),
            })

    memcpys = []
    for start, end, sid, corr, nbytes, kind in conn.execute(
        "SELECT start, end, streamId, correlationId, bytes, copyKind "
        "FROM CUPTI_ACTIVITY_KIND_MEMCPY"
    ):
        memcpys.append({
            "start": session_start_ns + start,
            "end":   session_start_ns + (end or start),
            "streamId": sid, "correlationId": corr,
            "bytes": nbytes, "copyKind": kind,
        })

    memsets = []
    if "CUPTI_ACTIVITY_KIND_MEMSET" in tables:
        for start, end, sid, corr, nbytes, val in conn.execute(
            "SELECT start, end, streamId, correlationId, bytes, value "
            "FROM CUPTI_ACTIVITY_KIND_MEMSET"
        ):
            memsets.append({
                "start": session_start_ns + start,
                "end":   session_start_ns + (end or start),
                "streamId": sid, "correlationId": corr,
                "bytes": nbytes, "value": val,
            })

    syncs = []
    if "CUPTI_ACTIVITY_KIND_SYNCHRONIZATION" in tables:
        for start, end, sid, corr, stype in conn.execute(
            "SELECT start, end, streamId, correlationId, syncType "
            "FROM CUPTI_ACTIVITY_KIND_SYNCHRONIZATION "
            "WHERE globalPid = ?", (owning_gpid,)
        ):
            syncs.append({
                "start": session_start_ns + start,
                "end":   session_start_ns + (end or start),
                "streamId": sid, "correlationId": corr,
                "syncType": stype,
            })
        syncs.sort(key=lambda s: s["start"])

    conn.close()
    return {
        "pid": pid, "pid_name": pid_name,
        "session_start_ns": session_start_ns,
        "tid_counts": dict(tid_counts),
        "runtime": runtime, "kernels": kernels,
        "memcpys": memcpys, "memsets": memsets, "syncs": syncs,
    }


def load_callback_windows(mongo_db, pid: int):
    """Return [{cb_start_ns, cb_end_ns, vtid, cb_addr}, …] for pid,
    sorted by cb_start_ns. Pairs ros2:callback_start with ros2:callback_end
    by the next end on the same vtid.
    """
    coll = mongo_db["ros2_trace"]
    def full_ns(d):
        return int(d["ts"].timestamp() * 1e9) + d.get("ts_nanos", 0)
    active = {}
    windows = []
    for d in coll.find(
        {"event": {"$in": ["ros2:callback_start", "ros2:callback_end"]},
         "vpid": pid},
        {"ts": 1, "ts_nanos": 1, "event": 1, "vtid": 1,
         "payload.callback": 1}
    ).sort("ts", 1):
        ns = full_ns(d)
        v = d["vtid"]
        if d["event"] == "ros2:callback_start":
            active[v] = (ns, d["payload"]["callback"])
        else:
            if v in active:
                s, cb = active.pop(v)
                windows.append({
                    "cb_start_ns": s, "cb_end_ns": ns,
                    "vtid": v, "cb_addr": cb,
                })
    windows.sort(key=lambda w: w["cb_start_ns"])
    return windows


# ---------------------------------------------------------------------------
# Stream-bridge core
# ---------------------------------------------------------------------------

def compute_stream_ownership(cuda, windows):
    """Phase 1 of the stream-bridge algorithm. Returns (claims, corr_to_stream):
      claims[i] = set of streamIds claimed by windows[i]
      corr_to_stream: {correlationId → streamId} across kernel/memcpy/memset
    """
    corr_to_stream = {}
    for r in cuda["kernels"]:  corr_to_stream[r["correlationId"]] = r["streamId"]
    for r in cuda["memcpys"]:  corr_to_stream[r["correlationId"]] = r["streamId"]
    for r in cuda["memsets"]:  corr_to_stream[r["correlationId"]] = r["streamId"]

    rt_by_tid = defaultdict(list)
    for r in cuda["runtime"]:
        rt_by_tid[r["tid"]].append(r)
    for lst in rt_by_tid.values():
        lst.sort(key=lambda r: r["start"])

    claims = []
    for w in windows:
        tid = w["vtid"]
        rt = rt_by_tid.get(tid, [])
        if not rt:
            claims.append(set()); continue
        starts = [r["start"] for r in rt]
        a = bisect.bisect_left(starts, w["cb_start_ns"])
        b = bisect.bisect_right(starts, w["cb_end_ns"])
        streams = set()
        for r in rt[a:b]:
            s = corr_to_stream.get(r["correlationId"])
            if s is not None:
                streams.add(s)
        claims.append(streams)
    return claims, corr_to_stream


def sync_release_times(cuda):
    """Per-stream release timestamps from CPU-blocking syncs.

      syncType 3 (StreamSynchronize)  → release streamId
      syncType 4 (ContextSynchronize) → release every stream observed
      syncType 1 (EventSynchronize)   → release streamId (best-effort)
      syncType 2 (StreamWaitEvent)    → NOT a release (GPU-internal)

    Returns {streamId: [ns…]} sorted.
    """
    syncs = cuda.get("syncs") or []
    if not syncs:
        return {}
    all_streams = set(s["streamId"] for s in syncs if s.get("streamId") is not None)
    for r in cuda["kernels"]:  all_streams.add(r["streamId"])
    for r in cuda["memcpys"]:  all_streams.add(r["streamId"])
    for r in cuda["memsets"]:  all_streams.add(r["streamId"])

    out = defaultdict(list)
    for s in syncs:
        t = s["start"]
        st = s["syncType"]
        sid = s.get("streamId")
        if st == 3 and sid is not None:   out[sid].append(t)
        elif st == 4:
            for k in all_streams:         out[k].append(t)
        elif st == 1 and sid is not None: out[sid].append(t)
        # st == 2 is not a release
    for k in out:
        out[k].sort()
    return out


def attribute_runtime(cuda, windows, claims, corr_to_stream, release_times,
                      *, gpu_producing_only=True):
    """Phase 2+3: attribute each runtime row per stream-bridge + SYNC.

    Returns dict with keys:
      n: total runtime rows considered
      callback_bound_time:   via Rule A (same-tid time window)
      callback_bound_stream: via Rule B (stream ownership)
      oort_post_release:     a release invalidated the claim
      oort:                  never had a claim
      per_row_owner_cb:  list index-aligned with cuda["runtime"] giving
                         the owning callback idx (or None if oort).
                         Only populated for non-filtered rows.
    gpu_producing_only:  if True, skip rows whose correlationId doesn't
                         map to any GPU activity (they're host-only
                         ops like cudaMalloc, cuModuleLoad).
    """
    stream_claim_times = defaultdict(list)
    for cb_idx, streams in enumerate(claims):
        cb_start = windows[cb_idx]["cb_start_ns"]
        for s in streams:
            stream_claim_times[s].append((cb_start, cb_idx))
    for lst in stream_claim_times.values():
        lst.sort()

    win_by_tid = defaultdict(list)
    for cb_idx, w in enumerate(windows):
        win_by_tid[w["vtid"]].append((w["cb_start_ns"], w["cb_end_ns"], cb_idx))
    for lst in win_by_tid.values():
        lst.sort()

    def _in_own_win(r):
        wlist = win_by_tid.get(r["tid"], [])
        if not wlist: return None
        t = r["start"]
        starts = [w[0] for w in wlist]
        hi = bisect.bisect_right(starts, t)
        k = hi - 1
        while k >= 0 and wlist[k][0] >= t - 10 * 10**9:
            if wlist[k][1] >= t:
                return wlist[k][2]
            k -= 1
        return None

    stats = {"n": 0, "callback_bound_time": 0, "callback_bound_stream": 0,
             "oort_post_release": 0, "oort": 0,
             "per_row_owner_cb": []}

    for r in cuda["runtime"]:
        if gpu_producing_only and r["correlationId"] not in corr_to_stream:
            continue
        stats["n"] += 1
        owner = _in_own_win(r)
        if owner is not None:
            stats["callback_bound_time"] += 1
            stats["per_row_owner_cb"].append(owner)
            continue
        sid = corr_to_stream.get(r["correlationId"])
        if sid is None:
            stats["oort"] += 1
            stats["per_row_owner_cb"].append(None)
            continue
        claim_list = stream_claim_times.get(sid, [])
        starts = [c[0] for c in claim_list]
        k = bisect.bisect_right(starts, r["start"]) - 1
        if k < 0:
            stats["oort"] += 1
            stats["per_row_owner_cb"].append(None)
            continue
        claim_ns, cb_idx = claim_list[k]
        releases = release_times.get(sid, [])
        rk = bisect.bisect_right(releases, r["start"]) - 1
        if rk >= 0 and releases[rk] >= claim_ns:
            stats["oort_post_release"] += 1
            stats["per_row_owner_cb"].append(None)
        else:
            stats["callback_bound_stream"] += 1
            stats["per_row_owner_cb"].append(cb_idx)
    return stats


# ---------------------------------------------------------------------------
# Inter-stream precedence (task-DAG edges)
# ---------------------------------------------------------------------------

def precedence_edges_from_waits(cuda):
    """Deterministic: per-destination wait counts from syncType=2 rows.

    Returns Counter[streamId → wait_count]. This is directly from the
    CUPTI_ACTIVITY_KIND_SYNCHRONIZATION table; no inference.
    """
    syncs = cuda.get("syncs") or []
    counter = Counter()
    for s in syncs:
        if s["syncType"] == 2 and s.get("streamId") is not None:
            counter[s["streamId"]] += 1
    return counter


def stream_precedence_edges_heuristic(cuda, lookback_max: int = 1000):
    """HEURISTIC src→dst stream edges from StreamWaitEvent rows.

    Because nsys leaves eventId=-1 on wait rows and cudaEventRecord
    calls are typically inside closed-source libs (TRT, ORT), the
    source stream of each wait cannot be read directly. We infer:
    src = the stream (≠ dst) whose kernel/memcpy/memset ENDED most
    recently before the wait, within the last `lookback_max` GPU-side
    activities globally.

    Returns (Counter[(src, dst) → n], unresolved: int).
    Paper: report NODE weights (precedence_edges_from_waits) as data;
    report EDGE set (this function) with a provenance caveat.
    """
    syncs = cuda.get("syncs") or []
    if not syncs:
        return Counter(), 0
    gpu_ends = []
    for r in cuda["kernels"]:  gpu_ends.append((r["end"], r["streamId"]))
    for r in cuda["memcpys"]:  gpu_ends.append((r["end"], r["streamId"]))
    for r in cuda["memsets"]:  gpu_ends.append((r["end"], r["streamId"]))
    gpu_ends.sort()
    ends = [g[0] for g in gpu_ends]
    sids = [g[1] for g in gpu_ends]

    edge_counts = Counter()
    unresolved = 0
    for s in syncs:
        if s["syncType"] != 2: continue
        dst = s.get("streamId")
        if dst is None: continue
        k = bisect.bisect_left(ends, s["start"]) - 1
        tries = 0
        while k >= 0 and tries < lookback_max:
            if sids[k] != dst and sids[k] is not None:
                edge_counts[(sids[k], dst)] += 1
                break
            k -= 1
            tries += 1
        else:
            unresolved += 1
    return edge_counts, unresolved
