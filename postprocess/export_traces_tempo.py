#!/usr/bin/env python3
"""Export FISH session ros2_trace callback events to Tempo via OTLP/HTTP/JSON.

Each (callback_start, callback_end) pair on a single thread becomes one span.
- service.name = process name + pid (so each ROS node is a row in Tempo)
- span name    = parent E-node label (e.g. '/r2b/apriltag_detections', 'timer_500ms')
                 falling back to the cb_addr if no parent found
- trace_id     = derived from (session_id, pid) so all callbacks of a process
                 land in one trace, viewable as a gantt chart per process

POST raw OTLP/JSON to <tempo>/v1/traces (no opentelemetry-sdk needed).

Time handling: Tempo's block index uses INGEST wall-clock time, not span ts.
A search for a span window 16h in the past finds nothing because the block
index says "this block was written at NOW". Two modes:
  --shift-to-now      (default) → shift span timestamps so the first span lands
                                  AT export wall-clock time. Original session ts
                                  preserved in span attributes (fish.original_ts_ns).
                                  Dashboard URL must point at the *shifted* window
                                  — print it at end.
  --keep-original-ts          → use raw session ts. Tempo blocks then 'startTime'
                                  in the future relative to spans; search needs a
                                  panel timeFrom='24h' override and works only if
                                  ingest happened recently.

Usage:
  python3 postprocess/export_traces_tempo.py <session_id>
                          [--scope __main__]
                          [--tempo http://localhost:4318]
                          [--batch 5000]
                          [--shift-to-now | --keep-original-ts]
                          [--dry-run]
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

import psycopg2
import psycopg2.extras

PG_DSN = os.environ.get(
    'FISH_PG_DSN',
    'host=localhost port=5432 dbname=fish user=fish password=fish',
)


def _id_from(s: str, n: int) -> str:
    """Deterministic hex id of length n derived from string s (for span/trace ids)."""
    h = hashlib.sha256(s.encode()).hexdigest()
    return h[:n]


def trace_id_for(session_id: str, pid: int) -> str:
    """32 hex chars (128 bits) — OTLP trace_id format."""
    return _id_from(f'fish|{session_id}|pid={pid}', 32)


def span_id_for(session_id: str, vtid: int, ts_ns_start: int) -> str:
    """16 hex chars (64 bits) — OTLP span_id format."""
    return _id_from(f'fish|{session_id}|vtid={vtid}|ts={ts_ns_start}', 16)


def fetch_callback_spans(cur, session_id: str, scope: str):
    """Return list of dicts: one per (start,end) pair.
    Each row has: pid, vtid, cb_addr, label, etype, node_name, ts_start_ns, ts_end_ns.
    """
    sql = """
    WITH f_to_e AS (
      SELECT f.cb_addr,
             e.etype || ':' || e.label AS span_name,
             e.label                   AS e_label,
             f.label                   AS cb_symbol
      FROM graph_nodes f
      JOIN graph_edges ge ON ge.session_id = f.session_id AND ge.scope = f.scope
                         AND ge.target = f.node_id AND ge.rel = 'contains'
      JOIN graph_nodes e ON e.session_id = ge.session_id AND e.scope = ge.scope
                        AND e.node_id = ge.source
      WHERE f.session_id = %s AND f.scope = %s
        AND f.type = 'F' AND f.cb_addr IS NOT NULL AND f.cb_addr <> 'NA'
    ),
    pid_to_node AS (
      SELECT pid, string_agg(DISTINCT label, ',' ORDER BY label) AS node_name
      FROM graph_nodes
      WHERE session_id = %s AND scope = %s AND type = 'N'
      GROUP BY pid
    ),
    events AS (
      SELECT vpid AS pid, vtid, ts_ns, event, payload->>'callback' AS cb_addr,
             procname
      FROM ros2_trace
      WHERE session_id = %s
        AND event IN ('ros2:callback_start','ros2:callback_end')
      ORDER BY ts_ns
    ),
    paired AS (
      SELECT pid, vtid, cb_addr, ts_ns AS ts_start_ns, procname,
             lead(ts_ns) OVER (PARTITION BY vtid ORDER BY ts_ns) AS ts_end_ns,
             lead(event)  OVER (PARTITION BY vtid ORDER BY ts_ns) AS next_event,
             event
      FROM events
    )
    SELECT p.pid, p.vtid, p.cb_addr, p.ts_start_ns, p.ts_end_ns,
           COALESCE(fe.span_name, p.cb_addr) AS span_name,
           COALESCE(fe.e_label, p.cb_addr)   AS e_label,
           COALESCE(fe.cb_symbol, '')        AS cb_symbol,
           COALESCE(pn.node_name, p.procname || ':' || p.pid, 'pid:' || p.pid) AS node_name
    FROM paired p
    LEFT JOIN f_to_e fe ON fe.cb_addr = p.cb_addr
    LEFT JOIN pid_to_node pn ON pn.pid = p.pid
    WHERE p.event = 'ros2:callback_start'
      AND p.next_event = 'ros2:callback_end'
      AND p.ts_end_ns IS NOT NULL
    ORDER BY p.ts_start_ns
    """
    cur.execute(sql, (session_id, scope, session_id, scope, session_id))
    return cur.fetchall()


def make_otlp_payload(session_id: str, scope: str, rows, ts_offset_ns: int = 0):
    """Build OTLP/JSON payload. Groups spans by (pid, node_name) — one ResourceSpans
    per process — and emits each (start,end) pair as a span. If ts_offset_ns is
    non-zero, span timestamps are shifted by that amount (used to bring historical
    sessions into the present so Tempo's block-index window covers them).
    """
    by_proc = {}
    for r in rows:
        key = (r['pid'], r['node_name'])
        by_proc.setdefault(key, []).append(r)

    resource_spans = []
    for (pid, node_name), rs in by_proc.items():
        tid = trace_id_for(session_id, pid)
        spans = []
        for r in rs:
            shifted_start = r['ts_start_ns'] + ts_offset_ns
            shifted_end   = r['ts_end_ns']   + ts_offset_ns
            spans.append({
                'traceId': tid,
                'spanId': span_id_for(session_id, r['vtid'], r['ts_start_ns']),
                'name': r['span_name'],
                'kind': 1,
                'startTimeUnixNano': str(shifted_start),
                'endTimeUnixNano':   str(shifted_end),
                'attributes': [
                    {'key': 'fish.session_id',      'value': {'stringValue': session_id}},
                    {'key': 'fish.scope',           'value': {'stringValue': scope}},
                    {'key': 'fish.vtid',            'value': {'intValue': str(r['vtid'])}},
                    {'key': 'fish.pid',             'value': {'intValue': str(pid)}},
                    {'key': 'fish.cb_addr',         'value': {'stringValue': r['cb_addr']}},
                    {'key': 'fish.entity',          'value': {'stringValue': r['e_label']}},
                    {'key': 'fish.callback_symbol', 'value': {'stringValue': r.get('cb_symbol') or ''}},
                    {'key': 'fish.original_ts_ns',  'value': {'intValue': str(r['ts_start_ns'])}},
                    {'key': 'fish.ts_offset_ns',    'value': {'intValue': str(ts_offset_ns)}},
                ],
                'status': {'code': 1},
            })
        resource_spans.append({
            'resource': {
                'attributes': [
                    {'key': 'service.name',  'value': {'stringValue': node_name}},
                    {'key': 'fish.pid',      'value': {'intValue': str(pid)}},
                    {'key': 'fish.session_id','value': {'stringValue': session_id}},
                ]
            },
            'scopeSpans': [{
                'scope': {'name': 'fish.ros2_trace'},
                'spans': spans,
            }]
        })
    return {'resourceSpans': resource_spans}


def chunk_payload(payload, batch_size):
    """Re-emit smaller OTLP payloads of at most `batch_size` spans each, keeping
    resource/scope structure intact per chunk."""
    out = []
    cur = {'resourceSpans': []}
    count = 0
    for rs in payload['resourceSpans']:
        for scope in rs['scopeSpans']:
            spans = scope['spans']
            for i in range(0, len(spans), batch_size):
                sub = {
                    'resource': rs['resource'],
                    'scopeSpans': [{'scope': scope['scope'], 'spans': spans[i:i+batch_size]}],
                }
                yield {'resourceSpans': [sub]}


def _patch_meta_after_flush(tempo_data_dir: str, min_ts_ns: int, max_ts_ns: int, session_id: str):
    """Wait for Tempo to flush a block, then rewrite meta.json startTime/endTime so
    the block-index filter accepts session-time search windows. Idempotent — patches
    the newest block under tempo_data_dir/blocks/single-tenant.

    Tempo bug: when the live-store flushes a block, it records startTime as the
    wall-clock time at flush, regardless of the actual minimum span timestamp
    inside. That breaks TraceQL search whenever spans are not "live".
    """
    import datetime as _dt
    import glob, time as _t
    blocks_dir = os.path.join(tempo_data_dir, 'blocks', 'single-tenant')
    deadline = _t.time() + 60
    found = None
    while _t.time() < deadline:
        metas = sorted(glob.glob(os.path.join(blocks_dir, '*', 'meta.json')),
                       key=lambda p: os.path.getmtime(p), reverse=True)
        for m in metas:
            try:
                d = json.load(open(m))
            except Exception:
                continue
            if d.get('endTime', '').startswith('2026-06-19') or d.get('totalObjects', 0) < 1:
                # block freshly written but not ours, OR ours pending — keep looking
                pass
            if d.get('totalObjects', 0) >= 1:
                # newest non-empty block; assume it's ours
                found = m
                break
        if found:
            break
        print(f'  waiting for block flush... ({len(metas)} blocks)')
        _t.sleep(3)

    if not found:
        print('  patch-meta: no block found after 60s; skip', file=sys.stderr)
        return

    iso_start = _dt.datetime.fromtimestamp(min_ts_ns/1e9, tz=_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    iso_end   = _dt.datetime.fromtimestamp(max_ts_ns/1e9, tz=_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    d = json.load(open(found))
    old = (d.get('startTime'), d.get('endTime'))
    d['startTime'] = iso_start
    d['endTime'] = iso_end
    with open(found, 'w') as f:
        json.dump(d, f, indent=2)
    print(f'  patched {os.path.basename(os.path.dirname(found))}/meta.json: '
          f'startTime {old[0]} → {iso_start}, endTime {old[1]} → {iso_end}')
    # Tempo needs to re-read the index; nudge via docker restart externally if container
    import subprocess
    try:
        subprocess.run(['docker', 'restart', 'tempo'], check=True, capture_output=True, timeout=30)
        print('  tempo restarted to reload index')
    except Exception as e:
        print(f'  WARN: could not restart tempo container: {e}', file=sys.stderr)


def post(tempo_url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        tempo_url.rstrip('/') + '/v1/traces',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('session_id')
    ap.add_argument('--scope', default='__main__')
    ap.add_argument('--tempo', default=os.environ.get('TEMPO_URL', 'http://localhost:4318'))
    ap.add_argument('--tempo-data-dir', default='/home/tue037807/fish_interfere/tempo_data',
                    help='path to Tempo data dir on host (for --patch-meta)')
    ap.add_argument('--batch', type=int, default=5000)
    ap.add_argument('--dry-run', action='store_true', help='Build payload but do not POST')
    shift = ap.add_mutually_exclusive_group()
    shift.add_argument('--shift-to-now', action='store_true', default=False,
                       help='shift span timestamps so first span lands at NOW; '
                            'lets Tempo block-index window cover the trace data')
    shift.add_argument('--keep-original-ts', action='store_true', default=True,
                       help='(default) emit with raw session ts; combine with --patch-meta '
                            'so Tempo search finds the block at session-time URLs')
    ap.add_argument('--patch-meta', action='store_true', default=True,
                    help='(default) after ingest, wait for block flush + rewrite meta.json '
                         'startTime/endTime to actual span time range. Required for TraceQL search '
                         'when span ts differs from ingest wall-time.')
    ap.add_argument('--no-patch-meta', dest='patch_meta', action='store_false')
    args = ap.parse_args()

    conn = psycopg2.connect(PG_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()
    t0 = time.time()
    rows = fetch_callback_spans(cur, args.session_id, args.scope)
    print(f'PG fetch: {len(rows)} callback pairs in {time.time()-t0:.2f}s')
    conn.close()
    if not rows:
        sys.exit('no callback pairs for that session')

    ts_offset_ns = 0
    if args.shift_to_now and not args.keep_original_ts:
        min_ts = min(r['ts_start_ns'] for r in rows)
        max_ts = max(r['ts_end_ns'] for r in rows)
        # Shift so first span starts ~30s in the past (gives Tempo block-index headroom)
        target_first = int(time.time() * 1e9) - 30 * 1_000_000_000
        ts_offset_ns = target_first - min_ts
        print(f'Shifting timestamps: offset={ts_offset_ns} ns '
              f'({ts_offset_ns/1e9/3600:+.2f} h)')
        new_first = (min_ts + ts_offset_ns) / 1e9
        new_last = (max_ts + ts_offset_ns) / 1e9
        print(f'  shifted window: {new_first:.0f} → {new_last:.0f} '
              f'(epoch s, last {(new_last-new_first):.1f}s)')

    payload = make_otlp_payload(args.session_id, args.scope, rows, ts_offset_ns=ts_offset_ns)
    n_proc = len(payload['resourceSpans'])
    print(f'Built OTLP: {n_proc} processes / {len(rows)} spans')

    if args.dry_run:
        print(json.dumps(payload['resourceSpans'][0], indent=2)[:1500])
        return

    n_sent = 0
    t0 = time.time()
    for chunk in chunk_payload(payload, args.batch):
        status, body = post(args.tempo, chunk)
        sent = sum(len(s['spans']) for r in chunk['resourceSpans'] for s in r['scopeSpans'])
        n_sent += sent
        if status >= 300:
            print(f'  POST {status}: {body[:200]}', file=sys.stderr)
        print(f'  {n_sent:6d}/{len(rows)} spans posted ({status})')
    print(f'Done: {n_sent} spans in {time.time()-t0:.2f}s')

    if args.patch_meta and not args.shift_to_now:
        # Tempo's vParquet4 writer records startTime = block-creation wall-time and
        # endTime = max span ts, which breaks block-index filtering when span ts is
        # historical. Rewrite to the actual [min span ts, max span ts] range.
        min_ts = min(r['ts_start_ns'] for r in rows)
        max_ts = max(r['ts_end_ns'] for r in rows)
        _patch_meta_after_flush(args.tempo_data_dir, min_ts, max_ts, args.session_id)

    # print example trace_ids so user can query
    seen = set()
    for rs in payload['resourceSpans']:
        for s in rs['scopeSpans'][0]['spans']:
            if s['traceId'] not in seen:
                svc = next(a['value']['stringValue'] for a in rs['resource']['attributes']
                           if a['key'] == 'service.name')
                print(f'  trace {s["traceId"]}  →  {svc}')
                seen.add(s['traceId'])
    print(f'(one trace per process; {len(seen)} processes total)')


if __name__ == '__main__':
    main()
