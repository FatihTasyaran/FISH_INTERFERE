#!/usr/bin/env python3
"""
FISH Post-Processing — Session Data Ingest

Ingests a complete FISH trace session into MongoDB and InfluxDB3.

MongoDB (one database per session):
  - process_tree        : document collection — PID/PPID/LWP/threads
  - node_info           : document collection — per-node pub/sub/service/action
  - topic_info          : document collection — per-topic type + pub/sub counts
  - topic_hz            : document collection — per-topic rate stats
  - component_list      : document collection — container → children
  - node_list           : document collection — node names
  - fish_events         : document collection — daemon kill/resurrect log
  - ros2_trace          : timeseries collection — LTTng events (ns precision)

InfluxDB3 (one database per session):
  - gpu_kernels         : CUPTI kernel executions
  - gpu_memcpy          : CUPTI memory copies
  - gpu_mem_usage       : GPU memory allocation events
  - nvtx_events         : NVTX markers / ranges
  - cuda_runtime        : CUDA runtime API calls

Usage:
  python3 -m postprocess.ingest <session_dir>
  python3 -m postprocess.ingest ~/fish_traces/fish_20260322_212915
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient, InsertOne
from pymongo.errors import CollectionInvalid
from influxdb_client_3 import InfluxDBClient3, WritePrecision

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MONGO_URI = "mongodb://localhost:27017"
INFLUX_HOST = "http://127.0.0.1:8181"
INFLUX_TOKEN_FILE = os.path.expanduser("~/inf.tok")

BATCH_SIZE = 5000


def load_influx_token() -> str:
    with open(INFLUX_TOKEN_FILE) as f:
        for line in f:
            if line.startswith("Token:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError(f"Could not read token from {INFLUX_TOKEN_FILE}")


def log(msg: str):
    print(f"[FISH ingest] {msg}", flush=True)


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def ensure_timeseries(db, name: str, time_field="ts", meta_field="meta",
                      granularity="seconds"):
    try:
        db.create_collection(name, timeseries={
            "timeField": time_field,
            "metaField": meta_field,
            "granularity": granularity,
        })
    except CollectionInvalid:
        pass
    return db[name]


def bulk_insert(coll, docs: list, label: str = ""):
    if not docs:
        return
    ops = [InsertOne(d) for d in docs]
    for i in range(0, len(ops), BATCH_SIZE):
        coll.bulk_write(ops[i:i + BATCH_SIZE], ordered=False)
    log(f"  {label}: {len(docs)} docs")


# ---------------------------------------------------------------------------
# 1. Process tree
# ---------------------------------------------------------------------------

def ingest_process_tree(db, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "ps_tree.txt")
    if not os.path.isfile(path):
        log("  ps_tree.txt not found, skipping")
        return

    with open(path) as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]

    if not lines:
        return

    headers = re.split(r"\s+", lines[0].strip())
    maxsplit = len(headers) - 1

    rows = []
    for ln in lines[1:]:
        parts = re.split(r"\s+", ln.strip(), maxsplit=maxsplit)
        if len(parts) < len(headers):
            continue
        obj = {}
        for h, v in zip(headers, parts):
            if h.upper() not in {"USER", "CMD"}:
                try:
                    obj[h] = int(v)
                except ValueError:
                    obj[h] = v
            else:
                obj[h] = v
        rows.append(obj)

    # Group into processes with threads
    processes = {}
    deferred = []
    for row in rows:
        pid = row["PID"]
        lwp = row["LWP"]
        if pid == lwp:
            processes[pid] = {
                "PID": pid, "PPID": row["PPID"], "LWP": lwp,
                "UID": row.get("UID"), "USER": row.get("USER"),
                "CMD": row["CMD"], "THREADS": [],
            }
        elif pid in processes:
            processes[pid]["THREADS"].append({"LWP": lwp, "CMD": row["CMD"]})
        else:
            deferred.append(row)

    for row in deferred:
        pid = row["PID"]
        if pid in processes:
            processes[pid]["THREADS"].append(
                {"LWP": row["LWP"], "CMD": row["CMD"]})

    bulk_insert(db["process_tree"], list(processes.values()), "process_tree")


# ---------------------------------------------------------------------------
# 2. Node info (parse raw text per node)
# ---------------------------------------------------------------------------

_RE_SECTION = re.compile(r"^\s{2,}([A-Za-z ]+):\s*$")
_RE_ENTRY = re.compile(r"^\s{4,}(\S+?)(?:\s*:\s*(\S.*\S|\S)?)?\s*$")
_SECTION_MAP = {
    "subscribers": "Subscribers",
    "publishers": "Publishers",
    "service servers": "Service_Servers",
    "service clients": "Service_Clients",
    "action servers": "Action_Servers",
    "action clients": "Action_Clients",
}


def _parse_node_info_text(text: str) -> dict:
    """Parse ros2 node info raw text into structured dict."""
    out = {
        "Subscribers": {}, "Publishers": {},
        "Service_Servers": {}, "Service_Clients": {},
        "Action_Servers": {}, "Action_Clients": {},
    }
    current_key = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        m_sec = _RE_SECTION.match(line)
        if m_sec:
            current_key = _SECTION_MAP.get(m_sec.group(1).strip().lower())
            continue
        if current_key:
            m_ent = _RE_ENTRY.match(line)
            if m_ent and m_ent.group(1).startswith("/"):
                out[current_key][m_ent.group(1)] = (
                    m_ent.group(2).strip() if m_ent.group(2) else None
                )
    return out


def ingest_node_info(db, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "node_info_all.json")
    if not os.path.isfile(path):
        log("  node_info_all.json not found, skipping")
        return

    with open(path) as f:
        raw = json.load(f)

    docs = []
    for node_name, text in raw.items():
        parsed = _parse_node_info_text(text)
        parsed["node"] = node_name
        docs.append(parsed)

    bulk_insert(db["node_info"], docs, "node_info")


# ---------------------------------------------------------------------------
# 3. Topic info
# ---------------------------------------------------------------------------

def ingest_topic_info(db, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "topic_info.json")
    if not os.path.isfile(path):
        log("  topic_info.json not found, skipping")
        return

    with open(path) as f:
        raw = json.load(f)

    docs = [{"topic": k, **v} for k, v in raw.items()]
    bulk_insert(db["topic_info"], docs, "topic_info")


# ---------------------------------------------------------------------------
# 4. Topic hz
# ---------------------------------------------------------------------------

def ingest_topic_hz(db, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "topic_hz.json")
    if not os.path.isfile(path):
        log("  topic_hz.json not found, skipping")
        return

    with open(path) as f:
        raw = json.load(f)

    docs = [{"topic": k, **v} for k, v in raw.items()]
    bulk_insert(db["topic_hz"], docs, "topic_hz")


# ---------------------------------------------------------------------------
# 5. Component list
# ---------------------------------------------------------------------------

def ingest_component_list(db, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "component_list.txt")
    if not os.path.isfile(path):
        log("  component_list.txt not found, skipping")
        return

    with open(path) as f:
        content = f.read().strip()

    if content.startswith("#"):
        log("  component_list: no components found, inserting marker")
        db["component_list"].insert_one({"_empty": True, "note": content})
        return

    RE_CONTAINER = re.compile(r"^(?P<container>\S.*\S|\S)$")
    RE_CHILD = re.compile(r"^\s*(?P<index>\d+)\s+(?P<path>/\S+)\s*$")

    docs = []
    current = None
    for line in content.splitlines():
        if not line.strip():
            continue
        m_child = RE_CHILD.match(line)
        if m_child:
            if current is not None:
                current["children"].append({
                    "index": int(m_child.group("index")),
                    "node": m_child.group("path"),
                })
            continue
        m_cont = RE_CONTAINER.match(line)
        if m_cont:
            current = {"container": m_cont.group("container").strip(),
                       "children": []}
            docs.append(current)

    bulk_insert(db["component_list"], docs, "component_list")


# ---------------------------------------------------------------------------
# 6. Node list
# ---------------------------------------------------------------------------

def ingest_node_list(db, session_dir: str):
    path = os.path.join(session_dir, "snapshot", "node_list.txt")
    if not os.path.isfile(path):
        log("  node_list.txt not found, skipping")
        return

    with open(path) as f:
        nodes = [ln.strip() for ln in f if ln.strip()]

    docs = [{"node": n} for n in nodes]
    bulk_insert(db["node_list"], docs, "node_list")


# ---------------------------------------------------------------------------
# 7. FISH event log
# ---------------------------------------------------------------------------

def ingest_fish_events(db, session_dir: str):
    log_dir = os.path.join(session_dir, "fishlog")
    if not os.path.isdir(log_dir):
        log("  fishlog/ not found, skipping")
        return

    docs = []
    for f_name in sorted(os.listdir(log_dir)):
        if not f_name.endswith(".log"):
            continue
        with open(os.path.join(log_dir, f_name)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                doc = {
                    "monotonic_ns": int(parts[0]),
                    "action": parts[1],
                    "killed_pid": int(parts[2]) if parts[2] != "-" else None,
                    "resurrected_pid": int(parts[3]) if parts[3] != "-" else None,
                    "process_name": parts[4],
                }
                if len(parts) > 5:
                    doc["cmd"] = parts[5]
                docs.append(doc)

    bulk_insert(db["fish_events"], docs, "fish_events")


# ---------------------------------------------------------------------------
# 8. ROS 2 LTTng trace → MongoDB timeseries
# ---------------------------------------------------------------------------

# Robust brace-group splitter (handles quoted strings with braces inside)
def _split_brace_groups(text: str) -> list:
    groups = []
    i, n = 0, len(text)
    in_str = False
    escape = False
    depth = 0
    buf = []
    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch == '{':
            if depth == 0:
                buf = []
            else:
                buf.append(ch)
            depth += 1
            i += 1
            continue
        if ch == '}':
            depth -= 1
            if depth <= 0:
                depth = 0
                groups.append(''.join(buf).strip())
                buf = []
            else:
                buf.append(ch)
            i += 1
            continue
        if depth > 0:
            buf.append(ch)
        i += 1
    return groups


_LINE_RE = re.compile(
    r'^\[(?P<ts>\d{2}:\d{2}:\d{2}\.\d{9})\]\s+\((?P<delta>[+0-9.\-]+)\)\s+'
    r'(?P<host>\S+)\s+(?P<event>[^:]+:[^:]+):\s+(?P<rest>.*)$',
    re.DOTALL
)
_KV_RE = re.compile(
    r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|0x[0-9A-Fa-f]+|\d+)',
    re.DOTALL
)


_ARRAY_RE = re.compile(
    r'(\w+)\s*=\s*\[([^\]]*)\]'
)
_ARRAY_ELEM_RE = re.compile(r'\[\d+\]\s*=\s*(\d+)')


def _parse_kv_group(text: str) -> dict:
    out = {}
    # First extract arrays (e.g. gid = [ [0] = 1, [1] = 15, ... ])
    for k, arr_body in _ARRAY_RE.findall(text):
        elems = _ARRAY_ELEM_RE.findall(arr_body)
        if elems:
            out[k] = [int(e) for e in elems]
    # Then extract scalar key=value pairs
    for k, v in _KV_RE.findall(text):
        if k in out:
            continue  # already parsed as array
        if v.startswith('"') and v.endswith('"'):
            val = v[1:-1]
            val = val.replace(r'\"', '"').replace(r'\n', '\n').replace(r'\\', '\\')
            out[k] = val
        elif v.startswith(('0x', '0X')):
            out[k] = v
        else:
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def _parse_trace_line(line: str, trace_date: datetime) -> dict | None:
    m = _LINE_RE.match(line.rstrip('\n'))
    if not m:
        return None

    groups = _split_brace_groups(m.group('rest'))
    cpu = _parse_kv_group(groups[0]) if len(groups) > 0 else {}
    proc = _parse_kv_group(groups[1]) if len(groups) > 1 else {}
    payload = _parse_kv_group(groups[2]) if len(groups) > 2 else {}

    # Timestamp: full nanosecond precision
    # ts → datetime truncated to SECONDS (for MongoDB timeseries bucketing)
    # ts_nanos → full nanosecond remainder within that second (0–999999999)
    hms = m.group('ts')
    hh, mm, ss = int(hms[0:2]), int(hms[3:5]), int(hms[6:8])
    nanos = int(hms[9:])

    ts_sec = trace_date.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    # ts_nanos stores the FULL sub-second part in nanoseconds
    # reconstruct: ts_sec + ts_nanos ns = exact event time
    ts_nanos = nanos

    vpid = int(proc.get("vpid", 0) or 0)
    vtid = int(proc.get("vtid", 0) or 0)
    procname = proc.get("procname", "")

    return {
        "ts": ts_sec,
        "ts_nanos": ts_nanos,
        "event": m.group('event'),
        "cpu_id": int(cpu.get("cpu_id", 0) or 0),
        "vpid": vpid,
        "vtid": vtid,
        "procname": procname,
        "payload": payload,
        "meta": {
            "host": m.group('host'),
            "vpid": vpid,
            "vtid": vtid,
            "procname": procname,
        },
    }


def ingest_ros2_trace(db, session_dir: str, tz_name: str = "Europe/Amsterdam"):
    trace_dir = os.path.join(session_dir, "ros2")

    # Find the actual LTTng trace directory (may be nested)
    ctf_dirs = []
    for root, dirs, files in os.walk(trace_dir):
        if "metadata" in files:
            ctf_dirs.append(root)
    if not ctf_dirs:
        log("  No LTTng CTF trace found in ros2/, skipping")
        return

    # Determine trace date from session directory name
    session_name = os.path.basename(session_dir)
    date_match = re.search(r'(\d{8})_', session_name)
    if date_match:
        ds = date_match.group(1)
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        trace_date = datetime(int(ds[0:4]), int(ds[4:6]), int(ds[6:8]),
                              tzinfo=tz)
    else:
        trace_date = datetime.now(timezone.utc)

    # Setup collection and indexes
    coll = ensure_timeseries(db, "ros2_trace")
    coll.create_index({"event": 1, "ts": 1})
    coll.create_index({"meta.vpid": 1, "ts": 1})
    coll.create_index({"payload.callback": 1})
    coll.create_index({"payload.node_handle": 1})
    coll.create_index({"payload.topic_name": 1, "ts": 1})

    # Stream babeltrace2 output (16M+ lines, cannot hold in memory)
    trace_paths = " ".join(f'"{d}"' for d in ctf_dirs)
    log(f"  Streaming babeltrace2 → MongoDB (batch_size={BATCH_SIZE})...")

    try:
        proc = subprocess.Popen(
            f"babeltrace2 {trace_paths}",
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        log("  babeltrace2 not found, skipping ROS 2 trace ingest")
        return

    buf = []
    inserted = 0
    lines_read = 0
    t_start = time.time()
    last_report = t_start

    for line in proc.stdout:
        lines_read += 1
        doc = _parse_trace_line(line, trace_date)
        if doc:
            buf.append(InsertOne(doc))
            if len(buf) >= BATCH_SIZE:
                coll.bulk_write(buf, ordered=False)
                inserted += len(buf)
                buf.clear()

                now = time.time()
                if now - last_report >= 5.0:
                    elapsed = now - t_start
                    rate = inserted / elapsed if elapsed > 0 else 0
                    log(f"    {inserted:,} docs, {lines_read:,} lines "
                        f"({rate:,.0f} docs/s)")
                    last_report = now

    if buf:
        coll.bulk_write(buf, ordered=False)
        inserted += len(buf)

    proc.wait()
    if proc.returncode != 0:
        stderr = proc.stderr.read()
        log(f"  babeltrace2 exited with code {proc.returncode}: "
            f"{stderr[:200]}")

    elapsed = time.time() - t_start
    rate = inserted / elapsed if elapsed > 0 else 0
    log(f"  ros2_trace: {inserted:,} docs from {lines_read:,} lines "
        f"in {elapsed:.1f}s ({rate:,.0f} docs/s)")


# ---------------------------------------------------------------------------
# 9. nsys SQLite → InfluxDB3
# ---------------------------------------------------------------------------

def ingest_nsys(session_dir: str, session_name: str):
    nsys_dir = os.path.join(session_dir, "nsys")
    if not os.path.isdir(nsys_dir):
        log("  nsys/ not found, skipping")
        return

    sqlite_files = [f for f in os.listdir(nsys_dir) if f.endswith(".sqlite")]
    if not sqlite_files:
        log("  No nsys .sqlite files found, skipping")
        return

    token = load_influx_token()
    db_name = session_name

    client = InfluxDBClient3(
        host=INFLUX_HOST, token=token, database=db_name,
        write_options={"precision": WritePrecision.NS},
    )

    for sqlite_file in sqlite_files:
        sqlite_path = os.path.join(nsys_dir, sqlite_file)
        source_tag = os.path.splitext(sqlite_file)[0]
        log(f"  Processing {sqlite_file}...")

        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row

        # Get session start time for absolute timestamp conversion
        session_start = conn.execute(
            "SELECT utcEpochNs, systemClockNs FROM TARGET_INFO_SESSION_START_TIME"
        ).fetchone()
        if session_start:
            epoch_offset = session_start["utcEpochNs"] - session_start["systemClockNs"]
        else:
            epoch_offset = 0
            log("    WARNING: no session start time, timestamps will be relative")

        # Build string lookup
        strings = {}
        for row in conn.execute("SELECT id, value FROM StringIds"):
            strings[row["id"]] = row["value"]

        def resolve(val):
            """Resolve StringId integer to string, pass through otherwise."""
            if isinstance(val, int) and val in strings:
                return strings[val]
            return val

        # --- GPU Kernels ---
        _ingest_nsys_table(
            client, conn, source_tag, epoch_offset, resolve,
            table="CUPTI_ACTIVITY_KIND_KERNEL",
            measurement="gpu_kernels",
            tag_cols=["deviceId", "contextId", "streamId"],
            field_cols=["correlationId", "registersPerThread",
                        "gridX", "gridY", "gridZ",
                        "blockX", "blockY", "blockZ",
                        "staticSharedMemory", "dynamicSharedMemory"],
            string_cols={"demangledName": "kernel_name",
                         "shortName": "kernel_short_name"},
            has_duration=True,
        )

        # --- Memory copies ---
        _ingest_nsys_table(
            client, conn, source_tag, epoch_offset, resolve,
            table="CUPTI_ACTIVITY_KIND_MEMCPY",
            measurement="gpu_memcpy",
            tag_cols=["deviceId", "contextId", "streamId", "copyKind"],
            field_cols=["correlationId", "bytes"],
            string_cols={},
            has_duration=True,
        )

        # --- CUDA runtime API ---
        _ingest_nsys_table(
            client, conn, source_tag, epoch_offset, resolve,
            table="CUPTI_ACTIVITY_KIND_RUNTIME",
            measurement="cuda_runtime",
            tag_cols=["eventClass"],
            field_cols=["correlationId", "returnValue"],
            string_cols={"nameId": "api_name"},
            has_duration=True,
        )

        # --- NVTX events ---
        _ingest_nvtx(client, conn, source_tag, epoch_offset)

        # --- GPU memory usage ---
        _ingest_nsys_table(
            client, conn, source_tag, epoch_offset, resolve,
            table="CUDA_GPU_MEMORY_USAGE_EVENTS",
            measurement="gpu_mem_usage",
            tag_cols=["deviceId", "contextId", "memKind",
                       "memoryOperationType"],
            field_cols=["bytes", "correlationId"],
            string_cols={},
            has_duration=False,
        )

        conn.close()

    client.close()
    log(f"  nsys ingest complete → InfluxDB database: {db_name}")


def _ingest_nsys_table(client, conn, source_tag, epoch_offset, resolve,
                       table, measurement, tag_cols, field_cols,
                       string_cols, has_duration):
    """Generic nsys table → InfluxDB line protocol."""
    try:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return

    if count == 0:
        return

    log(f"    {table}: {count} rows → {measurement}")
    lp_lines = []

    for row in conn.execute(f"SELECT * FROM {table}"):
        row_dict = dict(row)
        ts_ns = row_dict.get("start", 0)
        if ts_ns is None:
            continue
        abs_ts = ts_ns + epoch_offset

        # Tags
        tags = [f"source={source_tag}"]
        for tc in tag_cols:
            val = row_dict.get(tc)
            if val is not None:
                tags.append(f"{tc}={val}")

        # Fields
        fields = []
        if has_duration:
            end = row_dict.get("end")
            if end is not None and ts_ns is not None:
                fields.append(f"duration_ns={end - ts_ns}i")

        for fc in field_cols:
            val = row_dict.get(fc)
            if val is not None:
                fields.append(f"{fc}={val}i")

        for src_col, dst_name in string_cols.items():
            val = row_dict.get(src_col)
            if val is not None:
                resolved = resolve(val)
                if isinstance(resolved, str):
                    escaped = resolved.replace('"', '\\"')
                    fields.append(f'{dst_name}="{escaped}"')
                else:
                    fields.append(f"{dst_name}={resolved}i")

        if not fields:
            continue

        tag_str = ",".join(tags)
        field_str = ",".join(fields)
        lp_lines.append(f"{measurement},{tag_str} {field_str} {abs_ts}")

        if len(lp_lines) >= BATCH_SIZE:
            client.write(lp_lines)
            lp_lines.clear()

    if lp_lines:
        client.write(lp_lines)

    log(f"    {measurement}: done")


def _ingest_nvtx(client, conn, source_tag, epoch_offset):
    """NVTX events need special handling (text field, optional end)."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM NVTX_EVENTS").fetchone()[0]
    except Exception:
        return

    if count == 0:
        return

    log(f"    NVTX_EVENTS: {count} rows → nvtx_events")
    lp_lines = []

    for row in conn.execute("SELECT * FROM NVTX_EVENTS"):
        row_dict = dict(row)
        ts_ns = row_dict.get("start")
        if ts_ns is None:
            continue
        abs_ts = ts_ns + epoch_offset

        tags = [f"source={source_tag}"]
        domain = row_dict.get("domainId")
        if domain is not None:
            tags.append(f"domainId={domain}")

        fields = []
        end = row_dict.get("end")
        if end is not None:
            fields.append(f"duration_ns={end - ts_ns}i")
        event_type = row_dict.get("eventType")
        if event_type is not None:
            fields.append(f"eventType={event_type}i")

        text = row_dict.get("text") or ""
        if text:
            escaped = text.replace('"', '\\"')
            fields.append(f'text="{escaped}"')

        if not fields:
            continue

        tag_str = ",".join(tags)
        field_str = ",".join(fields)
        lp_lines.append(f"nvtx_events,{tag_str} {field_str} {abs_ts}")

        if len(lp_lines) >= BATCH_SIZE:
            client.write(lp_lines)
            lp_lines.clear()

    if lp_lines:
        client.write(lp_lines)
    log(f"    nvtx_events: done")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def ingest_session(session_dir: str, tz_name: str = "Europe/Amsterdam"):
    session_dir = os.path.abspath(session_dir)
    session_name = os.path.basename(session_dir)

    log(f"Session: {session_name}")
    log(f"Path:    {session_dir}")

    # --- MongoDB ---
    log(f"MongoDB database: {session_name}")
    mongo = MongoClient(MONGO_URI)
    db = mongo[session_name]

    t0 = time.time()

    ingest_process_tree(db, session_dir)
    ingest_node_info(db, session_dir)
    ingest_topic_info(db, session_dir)
    ingest_topic_hz(db, session_dir)
    ingest_component_list(db, session_dir)
    ingest_node_list(db, session_dir)
    ingest_fish_events(db, session_dir)
    ingest_ros2_trace(db, session_dir, tz_name=tz_name)

    mongo.close()

    # --- InfluxDB3 ---
    log(f"InfluxDB database: {session_name}")
    ingest_nsys(session_dir, session_name)

    elapsed = time.time() - t0
    log(f"Done in {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Ingest a FISH trace session into MongoDB + InfluxDB3")
    ap.add_argument("session_dir", help="Path to session directory")
    ap.add_argument("--tz", default="Europe/Amsterdam",
                    help="IANA timezone of the trace clock")
    args = ap.parse_args()

    if not os.path.isdir(args.session_dir):
        print(f"Error: '{args.session_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    ingest_session(args.session_dir, tz_name=args.tz)


if __name__ == "__main__":
    main()
