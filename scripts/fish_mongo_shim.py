#!/usr/bin/env python3
"""FISH MongoDB → HTTP shim for Grafana's Infinity datasource (and others that
speak generic JSON).

Why this exists:
  Grafana's first-party MongoDB datasource is Enterprise-only. We expose the
  same time-series-shaped MongoDB collections (ros2_trace, topic_hz,
  process_tree, fish_events) over plain HTTP+JSON so the free Infinity
  datasource (yesoreyeram-infinity-datasource) can query them.

Endpoints
---------
GET  /health
     Returns {"ok": true, "mongo": "..."} — for liveness.

GET  /sessions
     List all session-named MongoDB databases on the connected instance.

POST /query
     Generic MongoDB find/aggregate translator.

     Body (JSON):
       {
         "session":   "dtdl_apriltag_20260616_172344",   // required — MongoDB db name
         "collection":"ros2_trace",                       // required
         "filter":    {"event": "ros2:callback_start"},   // mongo find filter
         "projection":{"ts": 1, "payload.callback": 1},   // optional
         "sort":      [["ts", 1]],                        // optional
         "limit":     10000,                              // optional, default 10000
         "format":    "table" | "timeseries"              // default "table"
       }

     Response (format=table — Infinity-friendly):
       {"columns": [{"text":"ts","type":"time"},
                    {"text":"payload.callback","type":"string"}],
        "rows":    [[ts_ns_or_iso, "0x...."], ...]}

     Response (format=timeseries — for time-series panels):
       {"target": "ros2_trace.callback_start", "datapoints": [[v, ts_ms], ...]}

POST /aggregate
     Same shape but `pipeline` instead of filter — full MongoDB aggregate.

POST /callback/durations
     Convenience: given (session, cb_addr), reconstruct per-invocation
     duration_ns by pairing ros2:callback_start / ros2:callback_end events.
     Returns timeseries [[duration_ns, ts_ms], ...].

POST /executor/spin
     Convenience: given (session, pid), return fish_executor_* event stream.

Auth
----
Bearer token via `FISH_SHIM_TOKEN` env (optional; if unset, no auth — bind to
localhost only).

Run
---
  python3 scripts/fish_mongo_shim.py [--port 8189] [--host 127.0.0.1]
"""
import argparse
import datetime as _dt
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from pymongo import MongoClient
from bson import ObjectId

MONGO_URI = os.environ.get('FISH_SHIM_MONGO', 'mongodb://localhost:27017')
TOKEN = os.environ.get('FISH_SHIM_TOKEN')  # optional; if set, require Bearer
DEFAULT_LIMIT = 10000

_mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)


def jsonable(obj):
    """Recursively coerce ObjectId / datetime / bytes / non-JSON types."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [jsonable(x) for x in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def doc_ts_ns(doc):
    """Combine MongoDB doc's 'ts' (datetime, second precision) with 'ts_nanos'
    sub-second remainder to produce a full epoch-ns timestamp.
    """
    ts = doc.get('ts')
    sub_ns = int(doc.get('ts_nanos') or 0)
    if isinstance(ts, _dt.datetime):
        sec = int(ts.replace(tzinfo=_dt.timezone.utc).timestamp() if ts.tzinfo is None
                  else ts.timestamp())
        # ts often stored truncated to the second; ts_nanos has the remainder
        return sec * 1_000_000_000 + sub_ns
    if isinstance(ts, (int, float)):
        # Heuristic: >1e15 already ns
        v = int(ts)
        if v > 1e15:
            return v
        if v > 1e12:
            return v * 1_000
        return v * 1_000_000_000 + sub_ns
    return None


def flatten_for_table(doc, projection_keys):
    """Pull projection keys (dotted paths) out of a possibly-nested doc.

    Returns a row in the same order as projection_keys.
    """
    out = []
    for path in projection_keys:
        cur = doc
        for part in path.split('.'):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        out.append(jsonable(cur))
    return out


def to_ms_epoch(v):
    """Best-effort coerce a ts value to epoch ms for Grafana datapoints."""
    if isinstance(v, _dt.datetime):
        return int(v.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000
                   if v.tzinfo is None else v.timestamp() * 1000)
    if isinstance(v, (int, float)):
        # Heuristic: > 1e15 means nanoseconds, > 1e12 means microseconds
        if v > 1e15:
            return int(v / 1_000_000)  # ns → ms
        if v > 1e12:
            return int(v / 1_000)      # us → ms
        return int(v)
    return None


# ─── route implementations ──────────────────────────────────────────────────

def _list_dbs():
    return sorted(
        n for n in _mongo.list_database_names()
        if n not in ('admin', 'config', 'local')
    )


def _query(body):
    session = body.get('session')
    coll_name = body.get('collection')
    if not session or not coll_name:
        return {'error': "missing required fields: 'session' and 'collection'"}, 400
    filt = body.get('filter') or {}
    proj = body.get('projection') or None
    sort = body.get('sort')
    limit = int(body.get('limit') or DEFAULT_LIMIT)
    fmt = body.get('format', 'table')

    coll = _mongo[session][coll_name]
    cur = coll.find(filt, proj)
    if sort:
        cur = cur.sort(sort)
    cur = cur.limit(limit)
    docs = list(cur)

    if fmt == 'timeseries':
        # Expect projection like {"ts": 1, "<field>": 1}; first non-ts key = value
        field = body.get('value_field')
        if not field:
            non_ts = [k for k in (proj or {}) if k != 'ts' and k != '_id']
            field = non_ts[0] if non_ts else None
        target = body.get('target', f"{coll_name}.{field or 'ts'}")
        datapoints = []
        for d in docs:
            ts_ms = to_ms_epoch(d.get('ts'))
            if ts_ms is None:
                continue
            val = jsonable(d.get(field)) if field else 1
            try:
                val = float(val) if val is not None else None
            except (TypeError, ValueError):
                val = None
            if val is None:
                continue
            datapoints.append([val, ts_ms])
        return {'target': target, 'datapoints': datapoints}, 200

    # table format
    if proj:
        keys = [k for k in proj if k != '_id']
    elif docs:
        keys = sorted(docs[0].keys() - {'_id'})
    else:
        keys = []
    columns = [{'text': k, 'type': 'time' if k == 'ts' else 'string'} for k in keys]
    rows = [flatten_for_table(d, keys) for d in docs]
    return {'columns': columns, 'rows': rows, 'count': len(rows)}, 200


def _aggregate(body):
    session = body.get('session')
    coll_name = body.get('collection')
    pipeline = body.get('pipeline') or []
    if not session or not coll_name:
        return {'error': "missing 'session' or 'collection'"}, 400
    coll = _mongo[session][coll_name]
    docs = list(coll.aggregate(pipeline, allowDiskUse=True))
    return {'rows': [jsonable(d) for d in docs], 'count': len(docs)}, 200


def _callback_durations(body):
    """Pair ros2:callback_start / ros2:callback_end for a single cb_addr.

    MongoDB stores ts as second-precision datetime + ts_nanos field for the
    sub-second nanosecond remainder. We reconstruct full ns timestamps via
    doc_ts_ns() before computing durations.
    """
    session = body.get('session')
    cb_addr = body.get('cb_addr')
    if not session or not cb_addr:
        return {'error': "missing 'session' or 'cb_addr'"}, 400
    coll = _mongo[session]['ros2_trace']
    filt = {
        'event': {'$in': ['ros2:callback_start', 'ros2:callback_end']},
        'payload.callback': cb_addr,
    }
    docs = list(coll.find(
        filt, {'event': 1, 'ts': 1, 'ts_nanos': 1},
    ).sort([('ts', 1), ('ts_nanos', 1)]).limit(100000))
    datapoints = []
    pending_start_ns = None
    for d in docs:
        ev = d.get('event')
        ts_ns = doc_ts_ns(d)
        if ts_ns is None:
            continue
        if ev == 'ros2:callback_start':
            pending_start_ns = ts_ns
        elif ev == 'ros2:callback_end' and pending_start_ns is not None:
            duration_ns = ts_ns - pending_start_ns
            datapoints.append([duration_ns, pending_start_ns // 1_000_000])
            pending_start_ns = None
    return {'target': f'callback_duration_ns@{cb_addr}',
            'datapoints': datapoints,
            'count': len(datapoints)}, 200


def _executor_spin(body):
    session = body.get('session')
    pid = body.get('pid')
    if not session or pid is None:
        return {'error': "missing 'session' or 'pid'"}, 400
    coll = _mongo[session]['ros2_trace']
    docs = list(coll.find(
        {'event': {'$regex': '^ros2:fish_executor'}, 'meta.vpid': int(pid)},
        {'event': 1, 'ts': 1, 'payload': 1},
    ).sort([('ts', 1)]).limit(50000))
    return {'rows': [jsonable(d) for d in docs], 'count': len(docs)}, 200


ROUTES = {
    ('GET', '/health'):              lambda _b: ({'ok': True, 'mongo': MONGO_URI}, 200),
    ('GET', '/sessions'):            lambda _b: ({'sessions': _list_dbs()}, 200),
    ('POST', '/query'):              lambda b: _query(b),
    ('POST', '/aggregate'):          lambda b: _aggregate(b),
    ('POST', '/callback/durations'): lambda b: _callback_durations(b),
    ('POST', '/executor/spin'):      lambda b: _executor_spin(b),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    def _send(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        if TOKEN is None:
            return True
        hdr = self.headers.get('Authorization', '')
        return hdr == f'Bearer {TOKEN}'

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()

    def _dispatch(self, method):
        if not self._check_auth():
            return self._send(401, {'error': 'unauthorized'})
        path = urlparse(self.path).path
        handler = ROUTES.get((method, path))
        if handler is None:
            return self._send(404, {'error': f'no route {method} {path}'})
        body = {}
        if method == 'POST':
            length = int(self.headers.get('Content-Length') or 0)
            if length:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError as e:
                    return self._send(400, {'error': f'bad JSON: {e}'})
        try:
            payload, status = handler(body)
        except Exception as e:
            return self._send(500, {'error': f'{type(e).__name__}: {e}'})
        return self._send(status, payload)

    def do_GET(self):  return self._dispatch('GET')
    def do_POST(self): return self._dispatch('POST')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8189)
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"fish_mongo_shim listening on http://{args.host}:{args.port}  "
                     f"(mongo={MONGO_URI}  token={'set' if TOKEN else 'OFF'})\n")
    srv.serve_forever()


if __name__ == '__main__':
    main()
