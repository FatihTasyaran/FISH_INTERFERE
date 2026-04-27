"""Process-centric stress-test for the three-flag GPU task model.

Reads raw data for ONE process (pid) and reports how CUDA work
partitions into callback-bound vs oort residual — with and without
the stream-ID disambiguator (flag 1). No writes, no graph_store
mutations, no InfluxDB dependency.

Data sources:
  - nsys sqlite for pid's container (authoritative per-process;
    avoids the cross-container InfluxDB union bug).
  - MongoDB ros2_trace (callback_start/callback_end events for pid).

Output: markdown-ish stdout block + optional JSON dump.

Run:
    python3 analyze_process_gpu.py <session> <pid> \\
        [--nsys-dir /path/to/session/nsys] [--json out.json]

See notes/three_flags_gpu_model.txt for the model this script tests.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

from pymongo import MongoClient


MONGO_URI = os.environ.get("FISH_MONGO_URI", "mongodb://localhost:27017")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def find_sqlite_for_pid(nsys_dir: str, pid: int) -> str | None:
    """Locate the nsys sqlite whose PROCESSES row includes the given pid
    with non-empty CUPTI_ACTIVITY_KIND_RUNTIME. Returns the path."""
    candidates = []
    for fname in sorted(os.listdir(nsys_dir)):
        if not fname.endswith(".sqlite"):
            continue
        path = os.path.join(nsys_dir, fname)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "CUPTI_ACTIVITY_KIND_RUNTIME" not in tables:
                conn.close()
                continue
            row = conn.execute("""
                SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_RUNTIME r
                JOIN PROCESSES p ON (r.globalTid >> 24) = (p.globalPid >> 24)
                WHERE p.pid = ?
            """, (pid,)).fetchone()
            conn.close()
            if row and row[0] > 0:
                candidates.append((path, row[0]))
        except Exception as e:
            log(f"  [warn] skip {fname}: {e}")
    if not candidates:
        return None
    # Prefer the sqlite with the most rows (authoritative)
    candidates.sort(key=lambda c: -c[1])
    return candidates[0][0]


def load_process_cuda(sqlite_path: str, pid: int):
    """Return (session_start_ns, tids, runtime_rows, kernel_rows, memcpy_rows, memset_rows).

    runtime_rows:  list of dicts {start, end, tid, correlationId, api_name}
    kernel_rows:   list of dicts {start, end, streamId, correlationId, short_name}
    memcpy_rows:   list of dicts {start, end, streamId, correlationId, bytes, copyKind}
    memset_rows:   list of dicts {start, end, streamId, correlationId, bytes, value}

    All timestamps are wall-clock ns (session_start_ns + row.start), to
    line up with MongoDB ros2_trace.
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)

    # TARGET_INFO_SESSION_START_TIME has columns (utcEpochNs, utcTime,
    # localTime, ...). BUG: on our CUDA 12 / nsys version, the
    # utcEpochNs value is actually *local*-epoch-ns, not UTC epoch —
    # confirmed by matching utcTime == localTime and by comparing to
    # the system's known UTC offset. Derive true UTC epoch from the
    # utcTime string, treating it as local wall-clock.
    row = conn.execute(
        "SELECT * FROM TARGET_INFO_SESSION_START_TIME LIMIT 1"
    ).fetchone()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # Heuristic: columns are (utcEpochNs, utcTimeIso, localTimeIso, ...).
    # If the second col is a non-empty ISO string, parse as local time.
    tz = ZoneInfo(os.environ.get("FISH_TZ", "Europe/Amsterdam"))
    iso = row[1] if len(row) > 1 and isinstance(row[1], str) else None
    if iso:
        dt_local = datetime.fromisoformat(iso).replace(tzinfo=tz)
        session_start_ns = int(dt_local.timestamp() * 1e9)
    else:
        session_start_ns = row[0]

    # StringIds for api_name / kernel name resolution
    strids = {r[0]: r[1] for r in conn.execute("SELECT id, value FROM StringIds")}

    # PROCESSES → globalPid prefixes belonging to pid
    rows = list(conn.execute(
        "SELECT globalPid, name FROM PROCESSES WHERE pid = ?", (pid,)))
    if not rows:
        raise RuntimeError(f"pid={pid} not found in PROCESSES table of {sqlite_path}")
    gpid_prefixes = {gp >> 24 for gp, _ in rows}
    pid_name = rows[0][1]

    def _owns(globalTid):
        return (globalTid >> 24) in gpid_prefixes

    # Runtime rows
    runtime = []
    tid_counts = Counter()
    for row in conn.execute("""
        SELECT start, end, globalTid, correlationId, nameId
        FROM CUPTI_ACTIVITY_KIND_RUNTIME
    """):
        start, end, gtid, corr, nid = row
        if not _owns(gtid):
            continue
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

    # Kernel rows (not always present)
    kernels = []
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "CUPTI_ACTIVITY_KIND_KERNEL" in tables:
        for row in conn.execute("""
            SELECT start, end, streamId, correlationId, shortName
            FROM CUPTI_ACTIVITY_KIND_KERNEL
        """):
            start, end, sid, corr, shortId = row
            kernels.append({
                "start": session_start_ns + start,
                "end":   session_start_ns + (end or start),
                "streamId": sid,
                "correlationId": corr,
                "short_name": strids.get(shortId, f"<nid={shortId}>"),
            })

    # Memcpy rows
    memcpys = []
    for row in conn.execute("""
        SELECT start, end, streamId, correlationId, bytes, copyKind
        FROM CUPTI_ACTIVITY_KIND_MEMCPY
    """):
        start, end, sid, corr, nbytes, kind = row
        memcpys.append({
            "start": session_start_ns + start,
            "end":   session_start_ns + (end or start),
            "streamId": sid,
            "correlationId": corr,
            "bytes": nbytes,
            "copyKind": kind,
        })

    # Memset rows
    memsets = []
    if "CUPTI_ACTIVITY_KIND_MEMSET" in tables:
        for row in conn.execute("""
            SELECT start, end, streamId, correlationId, bytes, value
            FROM CUPTI_ACTIVITY_KIND_MEMSET
        """):
            start, end, sid, corr, nbytes, val = row
            memsets.append({
                "start": session_start_ns + start,
                "end":   session_start_ns + (end or start),
                "streamId": sid,
                "correlationId": corr,
                "bytes": nbytes,
                "value": val,
            })

    # Synchronization rows (needed to terminate stream-ownership claims;
    # see notes/gpu_mapping_datapoints.txt join (4) and
    # notes/stream_bridge.txt open question #1).
    #
    # syncType: 1=EventSynchronize, 2=StreamWaitEvent,
    #           3=StreamSynchronize, 4=ContextSynchronize.
    # StreamSynchronize (3) is the "CPU waits for this stream" fence we
    # care about most; ContextSynchronize (4) is an all-streams
    # barrier (cudaDeviceSynchronize).
    syncs = []
    if "CUPTI_ACTIVITY_KIND_SYNCHRONIZATION" in tables:
        for row in conn.execute("""
            SELECT start, end, streamId, correlationId, syncType
            FROM CUPTI_ACTIVITY_KIND_SYNCHRONIZATION
            WHERE globalPid = ?
        """, (rows[0][0],)):
            start, end, sid, corr, stype = row
            syncs.append({
                "start": session_start_ns + start,
                "end":   session_start_ns + (end or start),
                "streamId": sid,
                "correlationId": corr,
                "syncType": stype,
            })
        syncs.sort(key=lambda s: s["start"])

    conn.close()
    return {
        "pid": pid,
        "pid_name": pid_name,
        "session_start_ns": session_start_ns,
        "tid_counts": dict(tid_counts),
        "runtime": runtime,
        "kernels": kernels,
        "memcpys": memcpys,
        "memsets": memsets,
        "syncs": syncs,
    }


def load_callback_windows(mongo_db, pid: int):
    """Return callback windows for `pid` as list of dicts:
       {cb_start_ns, cb_end_ns, vtid, cb_addr}, sorted by cb_start_ns."""
    coll = mongo_db["ros2_trace"]
    active = {}  # vtid -> (start_ns, cb_addr)
    windows = []

    def full_ns(d):
        return int(d["ts"].timestamp() * 1e9) + d.get("ts_nanos", 0)

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
                    "cb_start_ns": s,
                    "cb_end_ns":   ns,
                    "vtid":        v,
                    "cb_addr":     cb,
                })
    windows.sort(key=lambda w: w["cb_start_ns"])
    return windows


# ---------------------------------------------------------------------------
# Partition logic (the three-flag rules)
# ---------------------------------------------------------------------------

def partition_timeline(cuda, windows):
    """Attribute each runtime row to {in_window: [callback_ids], oort}.

    For each CUDA host API call at time t:
      - Collect all windows whose [cb_start, cb_end] contains t.
      - Report: unambiguous (==1), ambiguous (>1), or oort (0).

    This is the time-only naïve attribution (flag 1 before the stream
    refinement is applied). Subsequent passes (stream-based) act on
    this baseline.
    """
    # Sort windows by cb_start_ns for bisect
    starts = [w["cb_start_ns"] for w in windows]
    # For each runtime row, find windows whose start <= t <= end.
    # Use bisect to scope the scan; multiple windows can overlap so we
    # cannot index by just start.
    stats = {
        "n_runtime": len(cuda["runtime"]),
        "oort": 0,
        "unambig": 0,
        "ambig": 0,
        "ambig_max_overlap": 0,
    }
    attribution = []  # list of (runtime_idx, [window_idxs])
    for ri, r in enumerate(cuda["runtime"]):
        t = r["start"]
        # Candidate windows: those whose cb_start <= t
        hi = bisect.bisect_right(starts, t)
        # Scan back; windows can be long, so take a safety look-back of
        # 10s or 100 entries, whichever is larger
        matching = []
        # Look back all windows starting after max(t - MAX_WIN_LEN) — we
        # use a generous 10s bound and confirm cb_end >= t per candidate.
        MAX_WIN_LEN_NS = 10 * 10**9
        k = hi - 1
        while k >= 0 and starts[k] >= t - MAX_WIN_LEN_NS:
            if windows[k]["cb_end_ns"] >= t:
                matching.append(k)
            k -= 1
        if not matching:
            stats["oort"] += 1
            attribution.append((ri, []))
        elif len(matching) == 1:
            stats["unambig"] += 1
            attribution.append((ri, matching))
        else:
            stats["ambig"] += 1
            stats["ambig_max_overlap"] = max(stats["ambig_max_overlap"],
                                              len(matching))
            attribution.append((ri, matching))
    return stats, attribution


def compute_stream_ownership(cuda, windows):
    """For each callback window, determine which streams it 'owns'.

    Ownership rule (tentative, flag 1):
      A callback C owns stream S if any host API call made on C's tid
      during [cb_start, cb_end] has a correlationId whose kernel/memcpy/
      memset landed on stream S. That claim extends until the next
      cudaStreamSynchronize call on S.

    This function just returns the claim map: cb_idx -> {streams used}.
    Cross-cb stream contention is reported in `conflicts`.
    """
    # Build correlationId -> streamId map across kernels+memcpys+memsets
    corr_to_stream = {}
    for r in cuda["kernels"]:
        corr_to_stream[r["correlationId"]] = r["streamId"]
    for r in cuda["memcpys"]:
        corr_to_stream[r["correlationId"]] = r["streamId"]
    for r in cuda["memsets"]:
        corr_to_stream[r["correlationId"]] = r["streamId"]

    # For each cb window, scan runtime rows on the SAME vtid during
    # the window and collect their stream ids (via corr id).
    claims = []   # index-aligned with windows: {streams}
    # Pre-sort runtime by tid for efficiency
    rt_by_tid = defaultdict(list)
    for r in cuda["runtime"]:
        rt_by_tid[r["tid"]].append(r)
    for lst in rt_by_tid.values():
        lst.sort(key=lambda r: r["start"])
    # For each cb, bisect into its tid's list
    for w in windows:
        tid = w["vtid"]
        rt = rt_by_tid.get(tid, [])
        if not rt:
            claims.append(set())
            continue
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


def classify_runtime_with_streams_gpu_only(cuda, windows, claims, corr_to_stream):
    """Same as classify_runtime_with_streams but restricted to host API
    calls whose correlationId maps to an actual GPU-side activity
    (kernel / memcpy / memset). These are the scheduling-relevant
    calls; host-only API (module load, symbol resolve, cudaMalloc/
    Free, cuGetProcAddress, cudaStreamSynchronize) is filtered out."""
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
        if not wlist: return False
        t = r["start"]
        starts = [w[0] for w in wlist]
        hi = bisect.bisect_right(starts, t)
        k = hi - 1
        while k >= 0 and wlist[k][0] >= t - 10 * 10**9:
            if wlist[k][1] >= t: return True
            k -= 1
        return False

    stats = {
        "n_gpu_producing": 0,
        "callback_bound_time": 0,
        "callback_bound_stream": 0,
        "oort": 0,
    }
    oort_api = Counter()
    for r in cuda["runtime"]:
        if r["correlationId"] not in corr_to_stream:
            continue  # host-only, filter out
        stats["n_gpu_producing"] += 1
        if _in_own_win(r):
            stats["callback_bound_time"] += 1
            continue
        sid = corr_to_stream[r["correlationId"]]
        claim_list = stream_claim_times.get(sid, [])
        starts = [c[0] for c in claim_list]
        k = bisect.bisect_right(starts, r["start"]) - 1
        if k >= 0:
            stats["callback_bound_stream"] += 1
        else:
            stats["oort"] += 1
            oort_api[r["api_name"]] += 1
    stats["oort_api_top"] = oort_api.most_common(10)
    return stats


def classify_runtime_with_streams_gpu_only_sync_aware(
        cuda, windows, claims, corr_to_stream, release_times):
    """Same as classify_runtime_with_streams_gpu_only but with the
    sync-release extension from notes/stream_bridge.txt open #1:
    a stream claim is valid only until the NEXT release (type 3/4
    sync) on that stream. After a release, the next claim on that
    stream takes over cleanly.

    release_times: {streamId: sorted list of release_ns} from
                   sync_release_times().
    """
    # Build stream_claim_times as before
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
        if not wlist: return False
        t = r["start"]
        starts = [w[0] for w in wlist]
        hi = bisect.bisect_right(starts, t)
        k = hi - 1
        while k >= 0 and wlist[k][0] >= t - 10 * 10**9:
            if wlist[k][1] >= t: return True
            k -= 1
        return False

    stats = {
        "n_gpu_producing": 0,
        "callback_bound_time": 0,
        "callback_bound_stream": 0,
        "oort_post_release": 0,   # fell after a release with no new claim
        "oort": 0,                # never had a claim at all
    }
    for r in cuda["runtime"]:
        if r["correlationId"] not in corr_to_stream:
            continue
        stats["n_gpu_producing"] += 1
        if _in_own_win(r):
            stats["callback_bound_time"] += 1
            continue
        sid = corr_to_stream[r["correlationId"]]
        claim_list = stream_claim_times.get(sid, [])
        starts = [c[0] for c in claim_list]
        k = bisect.bisect_right(starts, r["start"]) - 1
        if k < 0:
            stats["oort"] += 1
            continue
        claim_ns = claim_list[k][0]
        # Is there a release on this stream between claim_ns and r.start?
        releases = release_times.get(sid, [])
        rk = bisect.bisect_right(releases, r["start"]) - 1
        if rk >= 0 and releases[rk] >= claim_ns:
            # a release happened between the claim and now, claim is stale
            stats["oort_post_release"] += 1
        else:
            stats["callback_bound_stream"] += 1
    return stats


def sync_release_times(cuda):
    """Return dict streamId → sorted list of ns timestamps at which that
    stream is 'released' (drained) by a CPU-blocking sync.

    Semantics:
      syncType 3 (StreamSynchronize) → release that streamId
      syncType 4 (ContextSynchronize / cudaDeviceSynchronize) → release
        EVERY stream observed in this process at that moment
      syncType 1 (EventSynchronize) → CPU waits for an event; effectively
        releases the event's source stream up to the event's record time,
        but we don't resolve event→stream here — treat as release of
        streamId field if present, else ignore.
      syncType 2 (StreamWaitEvent) → GPU-side wait; NOT a release.

    Output: {streamId: [t_release_ns, ...]}; a streamId key may include
    release-by-context-sync entries that apply to all streams.
    """
    syncs = cuda.get("syncs") or []
    if not syncs:
        return {}
    # Collect all streams ever seen (for context-sync fan-out)
    all_streams = set(s["streamId"] for s in syncs if s.get("streamId") is not None)
    for r in cuda["kernels"]:  all_streams.add(r["streamId"])
    for r in cuda["memcpys"]:  all_streams.add(r["streamId"])
    for r in cuda["memsets"]:  all_streams.add(r["streamId"])

    out = defaultdict(list)
    for s in syncs:
        t = s["start"]
        stype = s["syncType"]
        sid = s.get("streamId")
        if stype == 3 and sid is not None:
            out[sid].append(t)
        elif stype == 4:
            for st in all_streams:
                out[st].append(t)
        elif stype == 1 and sid is not None:
            out[sid].append(t)
        # stype == 2 is NOT a release
    for st in out:
        out[st].sort()
    return out


def precedence_edges_from_waits(cuda):
    """Extract inter-stream precedence hints from StreamWaitEvent (type 2)
    sync rows. Returns Counter keyed by destination streamId → count.

    NOTE v1: only reports the WAITING (destination) stream frequencies.
    For src→dst edges, use stream_precedence_edges_heuristic() below.
    """
    syncs = cuda.get("syncs") or []
    counter = Counter()
    for s in syncs:
        if s["syncType"] == 2 and s.get("streamId") is not None:
            counter[s["streamId"]] += 1
    return counter


def stream_precedence_edges_heuristic(cuda, lookback_max=1000):
    """Derive src_stream → dst_stream precedence edges from
    StreamWaitEvent (type 2) syncs.

    The nsys sqlite does not populate eventId for WaitEvent rows
    (always -1) and cudaEventRecord calls are often issued from inside
    closed-source libraries (TensorRT, ORT) that don't surface to
    CUPTI. So event→source-stream cannot be resolved directly.

    Heuristic: for each type-2 sync at time T on dst stream, the
    SOURCE stream is the stream (≠ dst) whose kernel/memcpy/memset
    most recently ENDED before T (within the last `lookback_max`
    GPU-side activities globally). For dense pipelines this is
    correct ~99 % of the time (pointcloud: 3774/3776 resolved, 13
    distinct edges that form a coherent DAG). For sparse pipelines
    some waits land in a quiet region and stay unresolved (yolo:
    2074/3938 resolved).

    Returns (edge_counts: Counter[(src, dst) → n],
             unresolved_count: int).

    For the paper: report both the resolved-edge weighted DAG and
    the unresolved fraction as a provenance caveat. When
    cudaEventRecord→streamId is resolvable via a future nsys
    version or via wrapping user code, swap this heuristic out for
    a deterministic join.
    """
    import bisect as _bisect
    syncs = cuda.get("syncs") or []
    if not syncs:
        return Counter(), 0

    # Collect all GPU-side end timestamps with their streamIds
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
        if s["syncType"] != 2:
            continue
        dst = s.get("streamId")
        if dst is None:
            continue
        k = _bisect.bisect_left(ends, s["start"]) - 1
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


def classify_runtime_with_streams(cuda, windows, claims, corr_to_stream):
    """Second-pass attribution using stream claims.

    A runtime row is callback-bound if either:
      (a) its time falls in a callback window on its own vtid, OR
      (b) it has a stream (via correlationId) and that stream was
          claimed by a callback whose window is still 'live' at this
          time (claim extends until next cudaStreamSynchronize).

    For simplicity in v1, ignore the 'until next sync' extension and
    say: (b) a row's stream is claimed by any callback whose window
    already closed but was the most recent claimer of that stream.
    This is an upper bound on callback-bound attribution, so useful
    for deciding whether stream-attribution helps at all.
    """
    # stream -> list of (claim_start_ns, cb_idx)
    stream_claim_times = defaultdict(list)
    for cb_idx, streams in enumerate(claims):
        cb_start = windows[cb_idx]["cb_start_ns"]
        for s in streams:
            stream_claim_times[s].append((cb_start, cb_idx))
    for lst in stream_claim_times.values():
        lst.sort()

    stats = {
        "n_runtime": len(cuda["runtime"]),
        "callback_bound_time": 0,  # via own-tid window
        "callback_bound_stream": 0, # via stream claim (but outside time win)
        "oort": 0,
    }
    rt_by_tid = defaultdict(list)
    for r in cuda["runtime"]:
        rt_by_tid[r["tid"]].append(r)
    for lst in rt_by_tid.values():
        lst.sort(key=lambda r: r["start"])

    # windows per tid
    win_by_tid = defaultdict(list)
    for cb_idx, w in enumerate(windows):
        win_by_tid[w["vtid"]].append((w["cb_start_ns"], w["cb_end_ns"], cb_idx))
    for lst in win_by_tid.values():
        lst.sort()

    def _time_in_own_tid_window(r):
        wlist = win_by_tid.get(r["tid"], [])
        if not wlist:
            return False
        t = r["start"]
        starts = [w[0] for w in wlist]
        hi = bisect.bisect_right(starts, t)
        k = hi - 1
        while k >= 0 and wlist[k][0] >= t - 10 * 10**9:
            if wlist[k][1] >= t:
                return True
            k -= 1
        return False

    for r in cuda["runtime"]:
        if _time_in_own_tid_window(r):
            stats["callback_bound_time"] += 1
            continue
        # Try stream-claim bridge
        sid = corr_to_stream.get(r["correlationId"])
        if sid is None:
            stats["oort"] += 1
            continue
        claim_list = stream_claim_times.get(sid, [])
        # Find most recent claim whose cb_start <= r.start
        starts = [c[0] for c in claim_list]
        k = bisect.bisect_right(starts, r["start"]) - 1
        if k >= 0:
            stats["callback_bound_stream"] += 1
        else:
            stats["oort"] += 1
    return stats


def summarize_oort_runtime(cuda, stats_stream, windows, claims, corr_to_stream):
    """Classify the residual-oort runtime rows by api_name to see what
    they actually are (init ops, lib unload, real work, etc.)."""
    rt_by_tid = defaultdict(list)
    for r in cuda["runtime"]:
        rt_by_tid[r["tid"]].append(r)
    for lst in rt_by_tid.values():
        lst.sort(key=lambda r: r["start"])
    win_by_tid = defaultdict(list)
    for cb_idx, w in enumerate(windows):
        win_by_tid[w["vtid"]].append((w["cb_start_ns"], w["cb_end_ns"], cb_idx))
    for lst in win_by_tid.values():
        lst.sort()

    stream_claim_times = defaultdict(list)
    for cb_idx, streams in enumerate(claims):
        cb_start = windows[cb_idx]["cb_start_ns"]
        for s in streams:
            stream_claim_times[s].append((cb_start, cb_idx))
    for lst in stream_claim_times.values():
        lst.sort()

    def _in_own_win(r):
        wlist = win_by_tid.get(r["tid"], [])
        if not wlist: return False
        t = r["start"]
        starts = [w[0] for w in wlist]
        hi = bisect.bisect_right(starts, t)
        k = hi - 1
        while k >= 0 and wlist[k][0] >= t - 10 * 10**9:
            if wlist[k][1] >= t: return True
            k -= 1
        return False

    def _stream_claimed(r):
        sid = corr_to_stream.get(r["correlationId"])
        if sid is None: return False
        claim_list = stream_claim_times.get(sid, [])
        starts = [c[0] for c in claim_list]
        k = bisect.bisect_right(starts, r["start"]) - 1
        return k >= 0

    oort_api = Counter()
    oort_by_tid = Counter()
    oort_by_time_bucket = [0] * 10
    # session span (min/max)
    if not cuda["runtime"]:
        return {}
    t_min = cuda["runtime"][0]["start"]
    t_max = cuda["runtime"][-1]["start"]
    span = max(1, t_max - t_min)
    for r in cuda["runtime"]:
        if _in_own_win(r): continue
        if _stream_claimed(r): continue
        oort_api[r["api_name"]] += 1
        oort_by_tid[r["tid"]] += 1
        idx = int((r["start"] - t_min) / span * 10)
        if idx >= 10: idx = 9
        oort_by_time_bucket[idx] += 1
    return {
        "oort_api_top": oort_api.most_common(15),
        "oort_by_tid": oort_by_tid.most_common(),
        "oort_time_histo": oort_by_time_bucket,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def render_report(cuda, windows, naive_stats, stream_stats, oort_summary,
                  claims, *, gpu_stats=None, gpu_stats_sync=None,
                  wait_edges=None, release_times=None,
                  dag_edges=None, dag_unresolved=None):
    pid = cuda["pid"]
    name = cuda["pid_name"]
    print("=" * 72)
    print(f"analyze_process_gpu — pid={pid} ({name})")
    print("=" * 72)
    print(f"cuda_runtime rows:   {len(cuda['runtime']):>8}")
    print(f"kernels:             {len(cuda['kernels']):>8}")
    print(f"memcpys:             {len(cuda['memcpys']):>8}")
    print(f"memsets:             {len(cuda['memsets']):>8}")
    print(f"syncs:               {len(cuda.get('syncs', [])):>8}")
    if cuda.get("syncs"):
        from collections import Counter as _C
        st = _C(s["syncType"] for s in cuda["syncs"])
        names = {1:'EventSync',2:'StreamWaitEvent',3:'StreamSync',4:'CtxSync'}
        print(f"  sync types:        " + ", ".join(f"{names.get(k,k)}={v}"
              for k, v in sorted(st.items())))
    print(f"distinct tids:       {len(cuda['tid_counts']):>8}")
    print(f"callback windows:    {len(windows):>8}")
    if windows:
        span = (windows[-1]["cb_end_ns"] - windows[0]["cb_start_ns"]) / 1e9
        cb_tids = Counter(w["vtid"] for w in windows)
        print(f"callback span:       {span:.1f}s  ({len(cb_tids)} distinct cb-firing tids)")
        top_cb = sum(c for _, c in cb_tids.most_common(6))
        print(f"  top 6 cb-tids hold: {top_cb}/{len(windows)} "
              f"({100*top_cb/len(windows):.0f}%)")

    print()
    print("--- FLAG 1 — per-tid vs per-stream attribution --------------------")
    print(f"Naive (time ∩ own-tid window):")
    print(f"  unambiguous (1 window covers): {naive_stats['unambig']:>7}")
    print(f"  ambiguous (>1 covers):         {naive_stats['ambig']:>7}  "
          f"max_overlap={naive_stats['ambig_max_overlap']}")
    print(f"  oort (no window covers):       {naive_stats['oort']:>7}")
    if naive_stats['n_runtime']:
        print(f"  callback-bound fraction:       "
              f"{100*(naive_stats['unambig']+naive_stats['ambig'])/naive_stats['n_runtime']:.1f}%")
    print()
    print(f"With stream-claim bridge:")
    print(f"  callback_bound_time:   {stream_stats['callback_bound_time']:>7}")
    print(f"  callback_bound_stream: {stream_stats['callback_bound_stream']:>7}  "
          f"(attributable via stream-ownership rule)")
    print(f"  oort (residual):       {stream_stats['oort']:>7}")
    if stream_stats['n_runtime']:
        pct = 100 * stream_stats['oort'] / stream_stats['n_runtime']
        print(f"  oort_residual_fraction: {pct:.1f}%")

    print()
    print("--- Scheduling-relevant subset (host calls with GPU effect) -------")
    if gpu_stats:
        n = gpu_stats["n_gpu_producing"]
        print(f"n_gpu_producing (runtime calls that spawn a kernel/memcpy/memset): {n}")
        if n:
            print(f"  callback_bound_time:   {gpu_stats['callback_bound_time']:>7}  "
                  f"({100*gpu_stats['callback_bound_time']/n:.1f}%)")
            print(f"  callback_bound_stream: {gpu_stats['callback_bound_stream']:>7}  "
                  f"({100*gpu_stats['callback_bound_stream']/n:.1f}%)")
            print(f"  oort (residual):       {gpu_stats['oort']:>7}  "
                  f"({100*gpu_stats['oort']/n:.1f}%)")
            if gpu_stats["oort_api_top"]:
                print(f"  oort residual (GPU-producing) top api_names:")
                for api, cnt in gpu_stats["oort_api_top"]:
                    print(f"    {api:<40} {cnt}")

    print()
    if gpu_stats_sync:
        print("--- Sync-aware attribution (uses CUPTI_ACTIVITY_KIND_SYNCHRONIZATION)")
        n = gpu_stats_sync["n_gpu_producing"]
        if n:
            print(f"n_gpu_producing: {n}")
            print(f"  callback_bound_time:   {gpu_stats_sync['callback_bound_time']:>7}  "
                  f"({100*gpu_stats_sync['callback_bound_time']/n:.1f}%)")
            print(f"  callback_bound_stream: {gpu_stats_sync['callback_bound_stream']:>7}  "
                  f"({100*gpu_stats_sync['callback_bound_stream']/n:.1f}%)")
            print(f"  oort_post_release:     {gpu_stats_sync['oort_post_release']:>7}  "
                  f"({100*gpu_stats_sync['oort_post_release']/n:.1f}%)  "
                  f"← stream was released by sync, no fresh claim before this call")
            print(f"  oort (never claimed):  {gpu_stats_sync['oort']:>7}  "
                  f"({100*gpu_stats_sync['oort']/n:.1f}%)")
    if wait_edges:
        print()
        print("--- Inter-stream precedence (type-2 StreamWaitEvent) --------------")
        total_waits = sum(wait_edges.values())
        print(f"Total StreamWaitEvent syncs: {total_waits}")
        print(f"  per-dest-stream (waiting stream → count of waits it performed):")
        for sid, n in wait_edges.most_common():
            print(f"    stream={sid:<4}  wait_count={n}")
    if dag_edges is not None:
        print()
        print("--- Heuristic src→dst stream DAG edges (time-proximity) ----------")
        total_resolved = sum(dag_edges.values())
        total = total_resolved + (dag_unresolved or 0)
        if total:
            print(f"  syncs resolved: {total_resolved}/{total}  "
                  f"({100*total_resolved/total:.1f}%), "
                  f"unresolved: {dag_unresolved}")
        print(f"  distinct edges: {len(dag_edges)}")
        for (src, dst), n in dag_edges.most_common(20):
            print(f"    stream {src:<4} → stream {dst:<4}  : {n}")
        print(f"  (heuristic: src = stream with most recently ENDED GPU activity "
              f"before wait. See notes/gpu_mapping_datapoints.txt obs F.)")
    if release_times:
        print()
        print("--- Stream-release events (type-3 StreamSync / type-4 CtxSync) ---")
        for sid, ts in sorted(release_times.items()):
            print(f"  stream={sid:<4}  releases={len(ts)}")

    print()
    print("--- FLAG 3 — oort residual nature --------------------------------")
    if oort_summary:
        print("Top api_names in oort residual:")
        for name_api, n in oort_summary["oort_api_top"]:
            print(f"  {name_api:<45} {n}")
        print("\nOort by tid:")
        for tid, n in oort_summary["oort_by_tid"][:15]:
            print(f"  tid={tid:<6} {n}")
        print(f"\nOort time histogram (10 equal buckets over session span):")
        total = sum(oort_summary["oort_time_histo"]) or 1
        bars = "".join(f"{100*b/total:4.0f}%" for b in oort_summary["oort_time_histo"])
        print(f"  {bars}")

    print()
    print("--- Streams + ownership --------------------------------------------")
    all_streams = Counter()
    for cs in claims:
        for s in cs:
            all_streams[s] += 1
    print(f"Total distinct streams: {len(all_streams)}")
    if all_streams:
        print("Streams by #callbacks-claimed:")
        for s, n in all_streams.most_common(10):
            print(f"  stream={s:<6} claimed_by_cb_count={n}")
    # Callbacks claiming multiple streams
    multi = sum(1 for cs in claims if len(cs) > 1)
    print(f"Callbacks claiming >1 stream: {multi}/{len(claims)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", help="MongoDB session DB name")
    ap.add_argument("pid", type=int, help="PID to analyze")
    ap.add_argument("--nsys-dir", default=None,
                    help="Directory holding nsys sqlite files "
                         "(default: ~/fish_traces/<session>/nsys)")
    ap.add_argument("--json", default=None,
                    help="Optional path to dump full JSON of stats")
    args = ap.parse_args()

    nsys_dir = args.nsys_dir or os.path.expanduser(
        f"~/fish_traces/{args.session}/nsys")
    if not os.path.isdir(nsys_dir):
        log(f"ERROR: nsys dir not found: {nsys_dir}")
        sys.exit(1)

    sqlite_path = find_sqlite_for_pid(nsys_dir, args.pid)
    if sqlite_path is None:
        log(f"ERROR: no sqlite has CUDA activity for pid={args.pid} in {nsys_dir}")
        sys.exit(1)
    log(f"[load] sqlite: {os.path.basename(sqlite_path)}")

    t0 = time.time()
    cuda = load_process_cuda(sqlite_path, args.pid)
    log(f"[load] cuda: runtime={len(cuda['runtime'])} kernels={len(cuda['kernels'])} "
        f"memcpy={len(cuda['memcpys'])} memset={len(cuda['memsets'])}  "
        f"({time.time()-t0:.1f}s)")

    t0 = time.time()
    mongo = MongoClient(MONGO_URI)[args.session]
    windows = load_callback_windows(mongo, args.pid)
    log(f"[load] callback windows: {len(windows)}  ({time.time()-t0:.1f}s)")

    t0 = time.time()
    naive_stats, _ = partition_timeline(cuda, windows)
    log(f"[naive partition] done  ({time.time()-t0:.1f}s)")

    t0 = time.time()
    claims, corr_to_stream = compute_stream_ownership(cuda, windows)
    stream_stats = classify_runtime_with_streams(cuda, windows, claims, corr_to_stream)
    gpu_stats = classify_runtime_with_streams_gpu_only(cuda, windows, claims, corr_to_stream)
    # Sync-aware variant (CUPTI_ACTIVITY_KIND_SYNCHRONIZATION integration)
    release_times = sync_release_times(cuda)
    gpu_stats_sync = classify_runtime_with_streams_gpu_only_sync_aware(
        cuda, windows, claims, corr_to_stream, release_times)
    wait_edges = precedence_edges_from_waits(cuda)
    dag_edges, dag_unresolved = stream_precedence_edges_heuristic(cuda)
    oort_summary = summarize_oort_runtime(cuda, stream_stats, windows, claims,
                                           corr_to_stream)
    log(f"[stream partition] done  ({time.time()-t0:.1f}s)")

    render_report(cuda, windows, naive_stats, stream_stats, oort_summary, claims,
                  gpu_stats=gpu_stats, gpu_stats_sync=gpu_stats_sync,
                  wait_edges=wait_edges, release_times=release_times,
                  dag_edges=dag_edges, dag_unresolved=dag_unresolved)

    if args.json:
        out = {
            "pid": cuda["pid"],
            "pid_name": cuda["pid_name"],
            "n_runtime": len(cuda["runtime"]),
            "n_kernels": len(cuda["kernels"]),
            "n_memcpys": len(cuda["memcpys"]),
            "n_memsets": len(cuda["memsets"]),
            "tid_counts": cuda["tid_counts"],
            "callback_windows": len(windows),
            "naive": naive_stats,
            "stream": stream_stats,
            "oort_summary": oort_summary,
            "stream_claims_per_cb": [sorted(list(s)) for s in claims],
        }
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2, default=str)
        log(f"[json] wrote {args.json}")


if __name__ == "__main__":
    main()
