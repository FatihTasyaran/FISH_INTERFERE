#!/usr/bin/env python3
"""FISH ingest — PostgreSQL + TimescaleDB backend.

Standalone (no MongoDB, no InfluxDB) rewrite of ingest.py. Reads a FISH
session_dir (`ros2/` LTTng CTF + `nsys/*.sqlite` + `snapshot/` ros2 CLI dumps +
`fishlog/` daemon log + `launch_components.json`) and writes everything into
the `fish` PG database via COPY for speed.

Timestamp normalization (fixes Magic 1+2+3 once at ingest time):
  - LTTng `ts` (datetime, second precision) + `ts_nanos` → `ts_ns BIGINT`
  - nsys relative `start` + tz-corrected session-start → absolute UTC `ts_ns`
  - nsys `utcEpochNs` local-epoch-ns misnomer (ISSUE-002) — use tz-aware ISO
    string parsing to get true UTC ns

Layout (one row per trace event / sqlite row, no MongoDB documents).

Usage:
  python3 -m postprocess.ingest_pg <session_dir>
  python3 -m postprocess.ingest_pg <session_dir> --session-id=foo \
                                                 --tz=Europe/Amsterdam
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_store


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BATCH_SIZE = 50_000
DEFAULT_TZ = os.environ.get("FISH_TZ", "Europe/Amsterdam")


def log(msg: str):
    print(f"[FISH ingest_pg] {msg}", flush=True)


# ----------------------------------------------------------------------------
# Host + session registration
# ----------------------------------------------------------------------------

def _read_first_colon_field(path: str, prefix: str) -> str | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(prefix):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def gather_host_info() -> dict:
    cpu_model = _read_first_colon_field("/proc/cpuinfo", "model name")
    ram_kb = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_kb = int(line.split()[1])
                    break
    except OSError:
        pass
    return {
        "host_name": socket.gethostname(),
        "arch": platform.machine(),
        "kernel_version": platform.release(),
        "ros_distro": os.environ.get("ROS_DISTRO"),
        "cpu_model": cpu_model,
        "cpu_cores": os.cpu_count(),
        "total_ram_kb": ram_kb,
    }


def session_start_ts(session_dir: str, tz_name: str) -> datetime:
    """Derive the trace date from the session dir basename.

    Convention: fish_YYYYMMDD_HHMMSS → tz-aware datetime at midnight that day.
    """
    name = os.path.basename(session_dir.rstrip("/"))
    m = re.search(r"(\d{8})_(\d{6})", name)
    tz = ZoneInfo(tz_name)
    if m:
        d, t = m.group(1), m.group(2)
        return datetime(int(d[0:4]), int(d[4:6]), int(d[6:8]),
                        int(t[0:2]), int(t[2:4]), int(t[4:6]), tzinfo=tz)
    return datetime.now(tz)


# ----------------------------------------------------------------------------
# 1. process_tree (ps_tree.txt → process_tree + process_tree_threads)
# ----------------------------------------------------------------------------

def ingest_process_tree(session_id: str, session_dir: str, captured_ts_ns: int):
    path = os.path.join(session_dir, "snapshot", "ps_tree.txt")
    if not os.path.isfile(path):
        log("  process_tree: ps_tree.txt missing, skip")
        return

    with open(path) as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    if not lines:
        return

    headers = re.split(r"\s+", lines[0].strip())
    maxsplit = len(headers) - 1

    processes: dict[int, dict] = {}
    deferred: list[dict] = []
    for ln in lines[1:]:
        parts = re.split(r"\s+", ln.strip(), maxsplit=maxsplit)
        if len(parts) < len(headers):
            continue
        row = {}
        for h, v in zip(headers, parts):
            if h.upper() not in {"USER", "CMD"}:
                try:
                    row[h] = int(v)
                except ValueError:
                    row[h] = v
            else:
                row[h] = v
        pid = row.get("PID")
        lwp = row.get("LWP")
        if pid is None or lwp is None:
            continue
        if pid == lwp:
            processes[pid] = {
                "PID": pid, "PPID": row.get("PPID"), "LWP": lwp,
                "UID": row.get("UID"), "USER": row.get("USER"),
                "CMD": row.get("CMD", ""),
                "_threads": [],
            }
        elif pid in processes:
            processes[pid]["_threads"].append({"LWP": lwp, "CMD": row.get("CMD", "")})
        else:
            deferred.append(row)
    for row in deferred:
        pid = row.get("PID")
        if pid in processes:
            processes[pid]["_threads"].append(
                {"LWP": row.get("LWP"), "CMD": row.get("CMD", "")}
            )

    proc_rows = []
    thread_rows = []
    for p in processes.values():
        proc_rows.append((
            captured_ts_ns, session_id,
            p["PID"], p.get("PPID"), p["LWP"],
            p.get("UID"), p.get("USER"), p["CMD"],
        ))
        for t in p["_threads"]:
            thread_rows.append((
                session_id, p["PID"], t["LWP"], t["CMD"], captured_ts_ns,
            ))

    n1 = pg_store.copy_many(
        "process_tree",
        ["ts_ns", "session_id", "pid", "ppid", "lwp", "uid", "username", "cmd"],
        proc_rows,
    )
    n2 = pg_store.copy_many(
        "process_tree_threads",
        ["session_id", "pid", "lwp", "cmd", "captured_ts_ns"],
        thread_rows,
    )
    log(f"  process_tree: {n1} processes, {n2} threads")


# ----------------------------------------------------------------------------
# 2. node_info (node_info_all.json → node_info + node_endpoints)
# ----------------------------------------------------------------------------

_RE_SECTION = re.compile(r"^\s{2,}([A-Za-z ]+):\s*$")
_RE_ENTRY = re.compile(r"^\s{4,}(\S+?)(?:\s*:\s*(\S.*\S|\S)?)?\s*$")
_SECTION_TO_KIND = {
    "subscribers": "sub",
    "publishers": "pub",
    "service servers": "srv_server",
    "service clients": "srv_client",
    "action servers": "act_server",
    "action clients": "act_client",
}


def _parse_node_info_text(text: str) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {k: {} for k in _SECTION_TO_KIND.values()}
    current_kind = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        m_sec = _RE_SECTION.match(line)
        if m_sec:
            current_kind = _SECTION_TO_KIND.get(m_sec.group(1).strip().lower())
            continue
        if current_kind:
            m_ent = _RE_ENTRY.match(line)
            if m_ent and m_ent.group(1).startswith("/"):
                out[current_kind][m_ent.group(1)] = (
                    m_ent.group(2).strip() if m_ent.group(2) else None
                )
    return out


def ingest_node_info(session_id: str, session_dir: str, captured_ts_ns: int):
    path = os.path.join(session_dir, "snapshot", "node_info_all.json")
    if not os.path.isfile(path):
        log("  node_info: node_info_all.json missing, skip")
        return
    with open(path) as f:
        raw = json.load(f)

    info_rows: list[tuple] = []
    endpoint_rows: list[tuple] = []
    for node_name, text in raw.items():
        info_rows.append((session_id, node_name, captured_ts_ns))
        parsed = _parse_node_info_text(text)
        for kind, mapping in parsed.items():
            for topic, msg_type in mapping.items():
                endpoint_rows.append((
                    session_id, node_name, kind, topic, msg_type
                ))

    with pg_store.get_cursor() as (conn, cur):
        # node_info has UNIQUE (session_id, node_full_name) — use ON CONFLICT
        psycopg2_extras_execute_values(
            cur,
            "INSERT INTO node_info (session_id, node_full_name, captured_ts_ns) "
            "VALUES %s ON CONFLICT (session_id, node_full_name) DO NOTHING",
            info_rows,
        )
        # node_endpoints — same idempotency
        psycopg2_extras_execute_values(
            cur,
            "INSERT INTO node_endpoints (session_id, node_full_name, endpoint_kind, "
            "topic_or_service, message_type) VALUES %s "
            "ON CONFLICT (session_id, node_full_name, endpoint_kind, topic_or_service) DO NOTHING",
            endpoint_rows,
        )
        conn.commit()
    log(f"  node_info: {len(info_rows)} nodes, {len(endpoint_rows)} endpoints")


# ----------------------------------------------------------------------------
# 3. topic_info + topic_hz
# ----------------------------------------------------------------------------

def ingest_topic_info(session_id: str, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "topic_info.json")
    if not os.path.isfile(path):
        log("  topic_info: missing, skip")
        return
    with open(path) as f:
        raw = json.load(f)
    rows = [
        (session_id, topic, info.get("type"),
         info.get("publisher_count"), info.get("subscription_count"))
        for topic, info in raw.items()
    ]
    with pg_store.get_cursor() as (conn, cur):
        psycopg2_extras_execute_values(
            cur,
            "INSERT INTO topic_info (session_id, topic, type, publisher_count, "
            "subscription_count) VALUES %s "
            "ON CONFLICT (session_id, topic) DO NOTHING",
            rows,
        )
        conn.commit()
    log(f"  topic_info: {len(rows)} rows")


def ingest_topic_hz(session_id: str, session_dir: str, captured_ts_ns: int):
    path = os.path.join(session_dir, "snapshot", "topic_hz.json")
    if not os.path.isfile(path):
        log("  topic_hz: missing, skip")
        return
    with open(path) as f:
        raw = json.load(f)
    rows = []
    for topic, info in raw.items():
        rows.append((
            captured_ts_ns, session_id, topic,
            info.get("average_rate"), info.get("min_period"),
            info.get("max_period"), info.get("std_dev"),
            info.get("window_size"),
        ))
    pg_store.copy_many(
        "topic_hz",
        ["ts_ns", "session_id", "topic", "average_rate",
         "min_period", "max_period", "std_dev", "window_size"],
        rows,
    )
    log(f"  topic_hz: {len(rows)} rows")


# ----------------------------------------------------------------------------
# 4. component_list + node_list
# ----------------------------------------------------------------------------

def ingest_component_list(session_id: str, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "component_list.txt")
    if not os.path.isfile(path):
        log("  component_list: missing, skip")
        return
    with open(path) as f:
        content = f.read().strip()
    if content.startswith("#"):
        with pg_store.get_cursor() as (conn, cur):
            cur.execute(
                "INSERT INTO component_list (session_id, is_empty, note) "
                "VALUES (%s, true, %s)",
                (session_id, content),
            )
            conn.commit()
        log("  component_list: empty marker")
        return

    rows: list[tuple] = []
    current_container = None
    RE_CHILD = re.compile(r"^\s*(?P<index>\d+)\s+(?P<path>/\S+)\s*$")
    for line in content.splitlines():
        if not line.strip():
            continue
        m = RE_CHILD.match(line)
        if m:
            if current_container is not None:
                rows.append((
                    session_id, current_container,
                    int(m.group("index")), m.group("path"),
                ))
        else:
            current_container = line.strip()

    with pg_store.get_cursor() as (conn, cur):
        psycopg2_extras_execute_values(
            cur,
            "INSERT INTO component_list (session_id, container_name, child_index, "
            "child_node) VALUES %s "
            "ON CONFLICT (session_id, container_name, child_index) DO NOTHING",
            rows,
        )
        conn.commit()
    log(f"  component_list: {len(rows)} children")


def ingest_node_list(session_id: str, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "node_list.txt")
    if not os.path.isfile(path):
        log("  node_list: missing, skip")
        return
    with open(path) as f:
        nodes = [ln.strip() for ln in f if ln.strip()]
    rows = [(session_id, n) for n in nodes]
    with pg_store.get_cursor() as (conn, cur):
        psycopg2_extras_execute_values(
            cur,
            "INSERT INTO node_list (session_id, node) VALUES %s "
            "ON CONFLICT (session_id, node) DO NOTHING",
            rows,
        )
        conn.commit()
    log(f"  node_list: {len(rows)} nodes")


# ----------------------------------------------------------------------------
# 5. fish_events (fishlog/fish_events_*.log)
# ----------------------------------------------------------------------------

def ingest_fish_events(session_id: str, session_dir: str, session_start_ns: int):
    log_dir = os.path.join(session_dir, "fishlog")
    if not os.path.isdir(log_dir):
        log("  fish_events: fishlog/ missing, skip")
        return

    rows = []
    for fn in sorted(os.listdir(log_dir)):
        if not (fn.startswith("fish_events_") and fn.endswith(".log")):
            continue
        with open(os.path.join(log_dir, fn)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                monotonic_ns = int(parts[0])
                # The daemon log's monotonic_ns is CLOCK_MONOTONIC (process
                # boot relative); to align with absolute wall-clock we
                # piggy-back on session_start_ns (best-effort: monotonic offset
                # ~= session start). Stored verbatim too for cross-LTTng
                # correlation.
                ts_ns = session_start_ns + monotonic_ns
                action = parts[1]
                killed = int(parts[2]) if parts[2] != "-" else None
                resur = int(parts[3]) if parts[3] != "-" else None
                proc_name = parts[4]
                cmd = parts[5] if len(parts) > 5 else None
                rows.append((
                    ts_ns, session_id, monotonic_ns, action, proc_name, cmd,
                    killed, resur,
                ))

    pg_store.copy_many(
        "fish_events",
        ["ts_ns", "session_id", "monotonic_ns", "action", "process_name", "cmd",
         "killed_pid", "resurrected_pid"],
        rows,
    )
    log(f"  fish_events: {len(rows)} events")


# ----------------------------------------------------------------------------
# 6. ros2_trace (babeltrace2 streaming → COPY)
# ----------------------------------------------------------------------------

def _split_brace_groups(text: str) -> list[str]:
    groups, buf = [], []
    i, n = 0, len(text)
    in_str = escape = False
    depth = 0
    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True; buf.append(ch); i += 1; continue
        if ch == "{":
            if depth == 0:
                buf = []
            else:
                buf.append(ch)
            depth += 1; i += 1; continue
        if ch == "}":
            depth -= 1
            if depth <= 0:
                depth = 0
                groups.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
            i += 1; continue
        if depth > 0:
            buf.append(ch)
        i += 1
    return groups


_LINE_RE = re.compile(
    r'^\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d{9})\]\s+\((?P<delta>[+0-9.\-]+)\)\s+'
    r'(?P<host>\S+)\s+(?P<event>[^:]+:[^:]+):\s+(?P<rest>.*)$',
    re.DOTALL,
)
_KV_RE = re.compile(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|0x[0-9A-Fa-f]+|\d+)', re.DOTALL)
_ARRAY_RE = re.compile(r'(\w+)\s*=\s*\[([^\]]*)\]')
_ARRAY_ELEM_RE = re.compile(r'\[\d+\]\s*=\s*(\d+)')


def _parse_kv_group(text: str) -> dict:
    out: dict = {}
    for k, arr_body in _ARRAY_RE.findall(text):
        elems = _ARRAY_ELEM_RE.findall(arr_body)
        if elems:
            out[k] = [int(e) for e in elems]
    for k, v in _KV_RE.findall(text):
        if k in out:
            continue
        if v.startswith('"') and v.endswith('"'):
            val = v[1:-1].replace(r'\"', '"').replace(r"\n", "\n").replace(r"\\", "\\")
            out[k] = val
        elif v.startswith(("0x", "0X")):
            out[k] = v
        else:
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def _parse_trace_line(line: str, trace_date: datetime) -> tuple | None:
    m = _LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None

    groups = _split_brace_groups(m.group("rest"))
    cpu = _parse_kv_group(groups[0]) if len(groups) > 0 else {}
    proc = _parse_kv_group(groups[1]) if len(groups) > 1 else {}
    payload = _parse_kv_group(groups[2]) if len(groups) > 2 else {}

    hms = m.group("ts")
    hh, mm, ss = int(hms[0:2]), int(hms[3:5]), int(hms[6:8])
    nanos = int(hms[9:])

    ts_sec = trace_date.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    ts_ns = int(ts_sec.timestamp()) * 1_000_000_000 + nanos

    return (
        ts_ns,                       # ts_ns
        m.group("event"),            # event
        int(cpu.get("cpu_id", 0) or 0),  # cpu_id
        int(proc.get("vpid", 0) or 0),  # vpid
        int(proc.get("vtid", 0) or 0),  # vtid
        m.group("host"),             # host_name
        proc.get("procname", ""),    # procname
        payload,                     # payload (dict → JSONB)
    )


def ingest_ros2_trace(session_id: str, session_dir: str, trace_date: datetime):
    trace_root = os.path.join(session_dir, "ros2")
    if not os.path.isdir(trace_root):
        log("  ros2_trace: ros2/ missing, skip")
        return
    ctf_dirs = []
    for root, _, files in os.walk(trace_root):
        if "metadata" in files:
            ctf_dirs.append(root)
    if not ctf_dirs:
        log("  ros2_trace: no LTTng CTF directories found, skip")
        return

    err_name = f"bt2_{os.path.basename(session_dir)}_stderr.log"
    try:
        bt2_err_path = os.path.join(session_dir, "babeltrace2_stderr.log")
        open(bt2_err_path, "a").close()
    except (PermissionError, OSError):
        bt2_err_path = os.path.join("/tmp", err_name)
    log(f"  ros2_trace: streaming babeltrace2 (BATCH_SIZE={BATCH_SIZE})")

    paths_str = " ".join(f'"{d}"' for d in ctf_dirs)
    bt2_err = open(bt2_err_path, "w")
    proc = subprocess.Popen(
        f"babeltrace2 {paths_str}",
        shell=True, stdout=subprocess.PIPE, stderr=bt2_err,
        text=True, bufsize=1,
    )

    cols = ["ts_ns", "session_id", "event", "cpu_id", "vpid", "vtid",
            "host_name", "procname", "payload"]

    inserted = 0
    lines_read = 0
    buf: list[tuple] = []
    t_start = time.time()
    last_report = t_start

    with pg_store.get_cursor() as (conn, cur):
        conn.autocommit = False
        try:
            for line in proc.stdout:
                lines_read += 1
                row = _parse_trace_line(line, trace_date)
                if row is None:
                    continue
                # Inject session_id as the 2nd column
                buf.append((row[0], session_id, *row[1:]))
                if len(buf) >= BATCH_SIZE:
                    inserted += pg_store.copy_rows(cur, "ros2_trace", cols, buf)
                    buf.clear()
                    now = time.time()
                    if now - last_report >= 5.0:
                        rate = inserted / (now - t_start) if now > t_start else 0
                        log(f"    {inserted:,} rows, {lines_read:,} lines ({rate:,.0f}/s)")
                        last_report = now
            if buf:
                inserted += pg_store.copy_rows(cur, "ros2_trace", cols, buf)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    proc.wait()
    bt2_err.close()
    if proc.returncode != 0:
        with open(bt2_err_path) as f:
            tail = f.read()[-400:]
        log(f"  babeltrace2 rc={proc.returncode}: ...{tail}")
    elapsed = time.time() - t_start
    rate = inserted / elapsed if elapsed > 0 else 0
    log(f"  ros2_trace: {inserted:,} events in {elapsed:.1f}s ({rate:,.0f}/s)")


# ----------------------------------------------------------------------------
# 7. nsys sqlite → PG (parallel per-table)
# ----------------------------------------------------------------------------

def _corrected_session_start_ns(conn: sqlite3.Connection, tz_name: str) -> int:
    """ISSUE-002 fix: nsys utcEpochNs is local-epoch-ns (not UTC).

    Parse the ISO-string columns (utcTime/localTime) as tz-aware local
    wall-clock and convert to UTC ns. Fallback to raw utcEpochNs.
    """
    row = conn.execute(
        "SELECT utcEpochNs, systemClockNs, utcTime, localTime "
        "FROM TARGET_INFO_SESSION_START_TIME LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    iso = row[2] or row[3]
    if iso:
        try:
            dt_local = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(tz_name))
            return int(dt_local.timestamp() * 1e9)
        except ValueError:
            pass
    return int(row[0]) if row[0] is not None else 0


# nsys CUPTI table → PG hypertable spec
_NSYS_TABLE_SPECS = [
    dict(
        sqlite_table="CUPTI_ACTIVITY_KIND_KERNEL",
        pg_table="gpu_kernels",
        columns=["ts_ns", "session_id", "duration_ns", "kernel_name",
                 "kernel_short_name", "correlation_id", "context_id",
                 "device_id", "stream_id", "global_pid",
                 "grid_x", "grid_y", "grid_z",
                 "block_x", "block_y", "block_z",
                 "registers_per_thread",
                 "static_smem_bytes", "dynamic_smem_bytes",
                 "container", "source"],
        sql_columns="""start, "end", correlationId, contextId, deviceId,
                       streamId, globalPid, gridX, gridY, gridZ,
                       blockX, blockY, blockZ, registersPerThread,
                       staticSharedMemory, dynamicSharedMemory,
                       demangledName, shortName""",
        # Index of each column in sqlite SELECT result row
        builder="kernel",
    ),
    dict(
        sqlite_table="CUPTI_ACTIVITY_KIND_MEMCPY",
        pg_table="gpu_memcpy",
        columns=["ts_ns", "session_id", "duration_ns", "bytes", "copy_kind",
                 "correlation_id", "context_id", "device_id", "stream_id",
                 "global_pid", "container", "source"],
        sql_columns="""start, "end", correlationId, contextId, deviceId,
                       streamId, globalPid, bytes, copyKind""",
        builder="memcpy",
    ),
    dict(
        sqlite_table="CUPTI_ACTIVITY_KIND_MEMSET",
        pg_table="gpu_memset",
        columns=["ts_ns", "session_id", "duration_ns", "bytes", "value",
                 "mem_kind", "correlation_id", "context_id", "device_id",
                 "stream_id", "global_pid", "container", "source"],
        sql_columns="""start, "end", correlationId, contextId, deviceId,
                       streamId, globalPid, bytes, value, memKind""",
        builder="memset",
    ),
    dict(
        sqlite_table="CUPTI_ACTIVITY_KIND_SYNCHRONIZATION",
        pg_table="gpu_sync",
        columns=["ts_ns", "session_id", "duration_ns", "sync_type",
                 "event_id", "event_sync_id", "correlation_id",
                 "context_id", "device_id", "stream_id", "global_pid",
                 "container", "source"],
        sql_columns="""start, "end", correlationId, contextId, deviceId,
                       streamId, globalPid, syncType, eventId, eventSyncId""",
        builder="sync",
    ),
    dict(
        sqlite_table="CUPTI_ACTIVITY_KIND_RUNTIME",
        pg_table="cuda_runtime",
        columns=["ts_ns", "session_id", "duration_ns", "api_name",
                 "return_value", "correlation_id", "callchain_id",
                 "event_class", "global_tid", "tid", "container", "source"],
        sql_columns="""start, "end", correlationId, eventClass, globalTid,
                       returnValue, nameId, callchainId""",
        builder="runtime",
    ),
    dict(
        sqlite_table="CUPTI_ACTIVITY_KIND_OVERHEAD",
        pg_table="gpu_overhead",
        columns=["ts_ns", "session_id", "duration_ns", "overhead_type",
                 "overhead_name", "correlation_id", "event_class",
                 "global_tid", "tid", "container", "source"],
        sql_columns="""start, "end", correlationId, eventClass, globalTid,
                       overheadType, nameId""",
        builder="overhead",
    ),
    dict(
        sqlite_table="CUDA_GPU_MEMORY_USAGE_EVENTS",
        pg_table="gpu_mem_usage",
        columns=["ts_ns", "session_id", "bytes", "mem_kind",
                 "memory_operation_type", "correlation_id", "context_id",
                 "device_id", "global_pid", "container", "source"],
        sql_columns="""start, correlationId, contextId, deviceId, globalPid,
                       bytes, memKind, memoryOperationType""",
        builder="mem_usage",
    ),
]


def _kernel_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, end, corr, ctx, dev, stream, gpid, gx, gy, gz, bx, by, bz, regs, ssmem, dsmem, dname, sname = row
    if start is None: return None
    return (
        epoch_offset + start, session_id,
        (end - start) if end is not None else None,
        strings.get(dname), strings.get(sname),
        corr, ctx, dev, stream, gpid,
        gx, gy, gz, bx, by, bz,
        regs, ssmem, dsmem,
        container, source_tag,
    )


def _memcpy_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, end, corr, ctx, dev, stream, gpid, b, ck = row
    if start is None: return None
    return (
        epoch_offset + start, session_id,
        (end - start) if end is not None else None,
        b, ck, corr, ctx, dev, stream, gpid, container, source_tag,
    )


def _memset_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, end, corr, ctx, dev, stream, gpid, b, val, mk = row
    if start is None: return None
    return (
        epoch_offset + start, session_id,
        (end - start) if end is not None else None,
        b, val, mk, corr, ctx, dev, stream, gpid, container, source_tag,
    )


def _sync_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, end, corr, ctx, dev, stream, gpid, st, eid, esi = row
    if start is None: return None
    return (
        epoch_offset + start, session_id,
        (end - start) if end is not None else None,
        st, eid, esi, corr, ctx, dev, stream, gpid, container, source_tag,
    )


def _runtime_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, end, corr, evt_class, gtid, rv, name_id, cc_id = row
    if start is None: return None
    return (
        epoch_offset + start, session_id,
        (end - start) if end is not None else None,
        strings.get(name_id), rv, corr, cc_id, evt_class,
        gtid, (gtid & 0xFFFFFF) if gtid is not None else None,
        container, source_tag,
    )


def _overhead_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, end, corr, evt_class, gtid, oh_type, name_id = row
    if start is None: return None
    return (
        epoch_offset + start, session_id,
        (end - start) if end is not None else None,
        oh_type, strings.get(name_id), corr, evt_class,
        gtid, (gtid & 0xFFFFFF) if gtid is not None else None,
        container, source_tag,
    )


def _mem_usage_builder(row, session_id, epoch_offset, strings, source_tag, container):
    start, corr, ctx, dev, gpid, b, mk, mot = row
    if start is None: return None
    return (
        epoch_offset + start, session_id, b, mk, mot,
        corr, ctx, dev, gpid, container, source_tag,
    )


_BUILDERS = {
    "kernel": _kernel_builder,
    "memcpy": _memcpy_builder,
    "memset": _memset_builder,
    "sync": _sync_builder,
    "runtime": _runtime_builder,
    "overhead": _overhead_builder,
    "mem_usage": _mem_usage_builder,
}


def _ingest_one_nsys_table(spec: dict, sqlite_path: str, session_id: str,
                            epoch_offset: int, strings: dict,
                            source_tag: str, container: str) -> int:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        # Quick existence check
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {spec['sqlite_table']}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        if n == 0:
            return 0
        builder = _BUILDERS[spec["builder"]]

        def row_gen():
            for row in conn.execute(
                f"SELECT {spec['sql_columns']} FROM {spec['sqlite_table']}"
            ):
                out = builder(row, session_id, epoch_offset, strings, source_tag, container)
                if out is not None:
                    yield out

        return pg_store.copy_many(spec["pg_table"], spec["columns"], row_gen())
    finally:
        conn.close()


def _ingest_callchains(sqlite_path: str, session_id: str,
                       session_start_ns: int, container: str, strings: dict) -> int:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        try:
            n = conn.execute("SELECT COUNT(*) FROM CUDA_CALLCHAINS").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        if n == 0:
            return 0

        def row_gen():
            for r in conn.execute(
                "SELECT id, symbol, module, unresolved, originalIP, stackDepth "
                "FROM CUDA_CALLCHAINS"
            ):
                cc_id, symbol_id, mod_id, unresolved, original_ip, stack_depth = r
                sym = strings.get(symbol_id, hex(int(original_ip or 0)))
                mod = strings.get(mod_id, "")
                ts_ns = session_start_ns + int(cc_id) * 1000 + int(stack_depth or 0)
                yield (
                    ts_ns, session_id, int(cc_id), int(stack_depth or 0),
                    sym, int(original_ip or 0), mod, bool(unresolved), container,
                )

        return pg_store.copy_many(
            "cuda_callchain",
            ["ts_ns", "session_id", "callchain_id", "stack_depth",
             "symbol", "original_ip", "module", "unresolved", "container"],
            row_gen(),
        )
    finally:
        conn.close()


def _ingest_nvtx(sqlite_path: str, session_id: str, epoch_offset: int,
                 source_tag: str, container: str) -> int:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        try:
            n = conn.execute("SELECT COUNT(*) FROM NVTX_EVENTS").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        if n == 0:
            return 0

        def row_gen():
            for r in conn.execute(
                "SELECT start, \"end\", domainId, eventType, text FROM NVTX_EVENTS"
            ):
                start, end, domain, evt_type, text = r
                if start is None:
                    continue
                yield (
                    epoch_offset + start, session_id,
                    (end - start) if end is not None else None,
                    domain, evt_type, text, container, source_tag,
                )

        return pg_store.copy_many(
            "nvtx_events",
            ["ts_ns", "session_id", "duration_ns", "domain_id", "event_type",
             "text", "container", "source"],
            row_gen(),
        )
    finally:
        conn.close()


def _ingest_nsys_metadata(sqlite_path: str, session_id: str):
    """Static metadata tables → flat PG tables (gpu_info, cuda_*, system_env,
    nsys_enums). Idempotent — DELETE+INSERT per (session_id, ...).
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # gpu_info
        try:
            rows = list(conn.execute("SELECT * FROM TARGET_INFO_GPU"))
            if rows:
                with pg_store.get_cursor() as (pg_conn, cur):
                    cur.execute("DELETE FROM gpu_info WHERE session_id = %s",
                                (session_id,))
                    for r in rows:
                        d = {k: r[k] for k in r.keys()}
                        cur.execute(
                            """INSERT INTO gpu_info (
                                session_id, gpu_id, name, uuid, chip_name,
                                bus_location, is_discrete, total_memory_bytes,
                                memory_bandwidth_bps, constant_memory_bytes,
                                l2_cache_bytes, clock_rate_hz, sm_count,
                                threads_per_warp, async_engines, max_warps_per_sm,
                                max_blocks_per_sm, max_threads_per_block,
                                max_registers_per_block, max_shmem_per_block,
                                max_shmem_per_block_optin, max_shmem_per_sm,
                                max_registers_per_sm,
                                compute_major, compute_minor, sm_major, sm_minor,
                                extras
                            ) VALUES (
                                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                            ) ON CONFLICT (session_id, gpu_id) DO NOTHING""",
                            (session_id, d.get("id"), d.get("name"), d.get("uuid"),
                             d.get("chipName"), d.get("busLocation"),
                             bool(d.get("isDiscrete")), d.get("totalMemory"),
                             d.get("memoryBandwidth"), d.get("constantMemory"),
                             d.get("l2CacheSize"), d.get("clockRate"),
                             d.get("smCount"), d.get("threadsPerWarp"),
                             d.get("asyncEngines"), d.get("maxWarpsPerSm"),
                             d.get("maxBlocksPerSm"), d.get("maxThreadsPerBlock"),
                             d.get("maxRegistersPerBlock"), d.get("maxShmemPerBlock"),
                             d.get("maxShmemPerBlockOptin"), d.get("maxShmemPerSm"),
                             d.get("maxRegistersPerSm"),
                             d.get("computeMajor"), d.get("computeMinor"),
                             d.get("smMajor"), d.get("smMinor"),
                             json.dumps({k: v for k, v in d.items()
                                         if k not in {"id", "name", "uuid", "chipName",
                                                      "busLocation", "isDiscrete",
                                                      "totalMemory", "memoryBandwidth",
                                                      "constantMemory", "l2CacheSize",
                                                      "clockRate", "smCount",
                                                      "threadsPerWarp", "asyncEngines",
                                                      "maxWarpsPerSm", "maxBlocksPerSm",
                                                      "maxThreadsPerBlock",
                                                      "maxRegistersPerBlock",
                                                      "maxShmemPerBlock",
                                                      "maxShmemPerBlockOptin",
                                                      "maxShmemPerSm",
                                                      "maxRegistersPerSm",
                                                      "computeMajor", "computeMinor",
                                                      "smMajor", "smMinor"}},
                                        default=str)),
                        )
                    pg_conn.commit()
        except sqlite3.OperationalError:
            pass

        # cuda_session
        try:
            r = conn.execute(
                "SELECT utcEpochNs, systemClockNs, utcTime, localTime "
                "FROM TARGET_INFO_SESSION_START_TIME LIMIT 1"
            ).fetchone()
            if r is not None:
                utc_ns, sys_ns, utc_str, local_str = r
                corrected = utc_ns
                if utc_str:
                    try:
                        dt = datetime.fromisoformat(utc_str).replace(
                            tzinfo=ZoneInfo(DEFAULT_TZ))
                        corrected = int(dt.timestamp() * 1e9)
                    except ValueError:
                        pass
                with pg_store.get_cursor() as (pg_conn, cur):
                    cur.execute("""
                        INSERT INTO cuda_session (
                            session_id, utc_epoch_ns, system_clock_ns,
                            utc_time, local_time, session_start_ns_corrected
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (session_id) DO UPDATE SET
                          utc_epoch_ns = EXCLUDED.utc_epoch_ns,
                          system_clock_ns = EXCLUDED.system_clock_ns,
                          utc_time = EXCLUDED.utc_time,
                          local_time = EXCLUDED.local_time,
                          session_start_ns_corrected = EXCLUDED.session_start_ns_corrected
                    """, (session_id, utc_ns, sys_ns, utc_str, local_str, corrected))
                    pg_conn.commit()
        except sqlite3.OperationalError:
            pass

        # cuda_context
        try:
            rows = list(conn.execute("SELECT * FROM TARGET_INFO_CUDA_CONTEXT_INFO"))
            ct_rows = [
                (session_id, r["contextId"], r["hwId"], r["vmId"], r["processId"],
                 r["deviceId"], r["parentContextId"],
                 bool(r["isGreenContext"]) if r["isGreenContext"] is not None else None,
                 r["nullStreamId"], r["numMultiprocessors"])
                for r in rows
            ]
            if ct_rows:
                with pg_store.get_cursor() as (pg_conn, cur):
                    cur.execute("DELETE FROM cuda_context WHERE session_id = %s",
                                (session_id,))
                    psycopg2_extras_execute_values(
                        cur,
                        "INSERT INTO cuda_context (session_id, context_id, hw_id, "
                        "vm_id, process_id, device_id, parent_context_id, "
                        "is_green_context, null_stream_id, num_multiprocessors) "
                        "VALUES %s",
                        ct_rows,
                    )
                    pg_conn.commit()
        except sqlite3.OperationalError:
            pass

        # cuda_device
        try:
            rows = list(conn.execute("SELECT * FROM TARGET_INFO_CUDA_DEVICE"))
            dev_rows = [
                (session_id, r["cudaId"], r["gpuId"], r["pid"],
                 r["uuid"], r["numMultiprocessors"]) for r in rows
            ]
            if dev_rows:
                with pg_store.get_cursor() as (pg_conn, cur):
                    cur.execute("DELETE FROM cuda_device WHERE session_id = %s",
                                (session_id,))
                    psycopg2_extras_execute_values(
                        cur,
                        "INSERT INTO cuda_device (session_id, cuda_id, gpu_id, "
                        "pid, uuid, num_multiprocessors) VALUES %s",
                        dev_rows,
                    )
                    pg_conn.commit()
        except sqlite3.OperationalError:
            pass

        # cuda_streams
        try:
            rows = list(conn.execute("SELECT * FROM TARGET_INFO_CUDA_STREAM"))
            stm_rows = [
                (session_id, r["streamId"], r["hwId"], r["vmId"], r["processId"],
                 r["contextId"], r["priority"], r["flag"]) for r in rows
            ]
            if stm_rows:
                with pg_store.get_cursor() as (pg_conn, cur):
                    cur.execute("DELETE FROM cuda_streams WHERE session_id = %s",
                                (session_id,))
                    psycopg2_extras_execute_values(
                        cur,
                        "INSERT INTO cuda_streams (session_id, stream_id, hw_id, "
                        "vm_id, process_id, context_id, priority, flag) VALUES %s",
                        stm_rows,
                    )
                    pg_conn.commit()
        except sqlite3.OperationalError:
            pass

        # system_env
        try:
            rows = list(conn.execute("SELECT * FROM TARGET_INFO_SYSTEM_ENV"))
            env_rows = [
                (session_id, r["name"], r["value"], r["nameEnum"],
                 r["globalVid"], r["devStateName"]) for r in rows
            ]
            if env_rows:
                with pg_store.get_cursor() as (pg_conn, cur):
                    cur.execute("DELETE FROM system_env WHERE session_id = %s",
                                (session_id,))
                    psycopg2_extras_execute_values(
                        cur,
                        "INSERT INTO system_env (session_id, name, value, "
                        "name_enum, global_vid, dev_state_name) VALUES %s",
                        env_rows,
                    )
                    pg_conn.commit()
        except sqlite3.OperationalError:
            pass

        # Enum tables
        enum_rows = []
        enum_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ENUM_%' ORDER BY name"
        )]
        for tbl in enum_tables:
            try:
                rs = list(conn.execute(f"SELECT * FROM {tbl}"))
                for r in rs:
                    d = dict(r)
                    enum_rows.append((
                        session_id, tbl, d.get("id"), d.get("name"), d.get("label"),
                    ))
            except (sqlite3.OperationalError, KeyError):
                pass
        if enum_rows:
            with pg_store.get_cursor() as (pg_conn, cur):
                cur.execute("DELETE FROM nsys_enums WHERE session_id = %s",
                            (session_id,))
                psycopg2_extras_execute_values(
                    cur,
                    "INSERT INTO nsys_enums (session_id, enum_name, entry_id, "
                    "entry_name, entry_label) VALUES %s",
                    enum_rows,
                )
                pg_conn.commit()
    finally:
        conn.close()


def ingest_nsys(session_id: str, session_dir: str, tz_name: str, container: str):
    nsys_dir = os.path.join(session_dir, "nsys")
    if not os.path.isdir(nsys_dir):
        log("  nsys: nsys/ missing, skip")
        return
    sqlite_files = sorted(f for f in os.listdir(nsys_dir) if f.endswith(".sqlite"))
    if not sqlite_files:
        log("  nsys: no .sqlite files, skip")
        return

    for sqlite_file in sqlite_files:
        sqlite_path = os.path.join(nsys_dir, sqlite_file)
        source_tag = os.path.splitext(sqlite_file)[0]
        log(f"  nsys: processing {sqlite_file}")

        meta_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            epoch_offset = _corrected_session_start_ns(meta_conn, tz_name)
            strings = {r[0]: r[1] for r in meta_conn.execute(
                "SELECT id, value FROM StringIds"
            )}
        finally:
            meta_conn.close()

        # Parallel CUPTI table ingest
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [
                pool.submit(_ingest_one_nsys_table, spec, sqlite_path,
                            session_id, epoch_offset, strings, source_tag, container)
                for spec in _NSYS_TABLE_SPECS
            ]
            futures.append(pool.submit(
                _ingest_callchains, sqlite_path, session_id,
                epoch_offset, container, strings,
            ))
            futures.append(pool.submit(
                _ingest_nvtx, sqlite_path, session_id, epoch_offset,
                source_tag, container,
            ))
            for f in as_completed(futures):
                # surface errors immediately
                f.result()

        # Static metadata (small, serial)
        _ingest_nsys_metadata(sqlite_path, session_id)

        log(f"  nsys: {sqlite_file} done")


# ----------------------------------------------------------------------------
# psycopg2 execute_values shim (avoids re-importing inside hot loop)
# ----------------------------------------------------------------------------

from psycopg2.extras import execute_values as psycopg2_extras_execute_values  # noqa: E402


# ----------------------------------------------------------------------------
# Main orchestrator
# ----------------------------------------------------------------------------

def ingest_session(session_dir: str, *,
                   session_id: str | None = None,
                   tz_name: str = DEFAULT_TZ,
                   force: bool = False):
    session_dir = os.path.abspath(session_dir)
    if session_id is None:
        session_id = os.path.basename(session_dir.rstrip("/"))
    log(f"session_id = {session_id}")
    log(f"session_dir = {session_dir}")

    pg_store.init_pool()

    # Existing session → either clear or skip
    if pg_store.session_exists(session_id):
        if force:
            log(f"session {session_id} exists, force=True → deleting")
            pg_store.delete_session(session_id)
        else:
            log(f"session {session_id} already exists in PG. Use --force to re-ingest.")
            return

    # Host
    host_info = gather_host_info()
    pg_store.register_host(**host_info)
    log(f"host: {host_info['host_name']}")

    # Session
    trace_date = session_start_ts(session_dir, tz_name)
    session_start_ns = int(trace_date.timestamp() * 1e9)
    captured_ts_ns = session_start_ns
    components_loaded = None
    lc_path = os.path.join(session_dir, "launch_components.json")
    if os.path.isfile(lc_path):
        try:
            with open(lc_path) as f:
                components_loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    pg_store.register_session(
        session_id,
        host_name=host_info["host_name"],
        start_ts_ns=session_start_ns,
        start_utc=trace_date.astimezone(ZoneInfo("UTC")),
        session_dir=session_dir,
        components_loaded=components_loaded,
    )

    t0 = time.time()

    # Small snapshot / fishlog ingests (sequential — they're tiny)
    ingest_process_tree(session_id, session_dir, captured_ts_ns)
    ingest_node_info(session_id, session_dir, captured_ts_ns)
    ingest_topic_info(session_id, session_dir)
    ingest_topic_hz(session_id, session_dir, captured_ts_ns)
    ingest_component_list(session_id, session_dir)
    ingest_node_list(session_id, session_dir)
    ingest_fish_events(session_id, session_dir, session_start_ns)

    # Big ingests: ros2_trace + nsys in parallel
    errors: list[Exception] = []

    def _ros2_worker():
        try:
            ingest_ros2_trace(session_id, session_dir, trace_date)
        except Exception as e:
            errors.append(e)
            log(f"  ros2_trace FAILED: {e}")
            raise

    def _nsys_worker():
        try:
            ingest_nsys(session_id, session_dir, tz_name, container=session_id)
        except Exception as e:
            errors.append(e)
            log(f"  nsys FAILED: {e}")
            raise

    t_ros2 = threading.Thread(target=_ros2_worker, name="ros2_trace")
    t_nsys = threading.Thread(target=_nsys_worker, name="nsys")
    t_ros2.start(); t_nsys.start()
    t_ros2.join(); t_nsys.join()

    # Update session start/end from observed events
    row = pg_store.fetch_one(
        "SELECT min(ts_ns) AS s, max(ts_ns) AS e "
        "FROM ros2_trace WHERE session_id = %s",
        (session_id,),
    )
    if row and row["s"]:
        pg_store.register_session(
            session_id,
            start_ts_ns=row["s"],
            end_ts_ns=row["e"],
        )

    if errors:
        log(f"DONE WITH ERRORS: {len(errors)}")
        for e in errors:
            log(f"  - {type(e).__name__}: {e}")
    else:
        log(f"DONE in {time.time()-t0:.1f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir")
    ap.add_argument("--session-id", default=None,
                    help="Override session_id (default: basename of session_dir)")
    ap.add_argument("--tz", default=DEFAULT_TZ)
    ap.add_argument("--force", action="store_true",
                    help="Delete existing session in PG before ingesting")
    args = ap.parse_args()

    if not os.path.isdir(args.session_dir):
        sys.exit(f"not a directory: {args.session_dir}")

    ingest_session(args.session_dir, session_id=args.session_id,
                   tz_name=args.tz, force=args.force)


if __name__ == "__main__":
    main()
