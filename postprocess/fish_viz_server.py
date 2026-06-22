#!/usr/bin/env python3
"""Tiny HTTP server: serves fish_viz_popup.html + per-session fish_graph.json
from PostgreSQL. Designed to be opened as a popup window from the embedded
Grafana panel ("Pop out ↗" button).

Routes:
  GET /                       → fish_viz_popup.html (the D3 graph viewer)
  GET /fish_viz.css           → static (if present)
  GET /fish_graph.json?session_id=X&scope=Y  → graph JSON from PG

Default port 8783 (avoids 8777/8779 used by the older demo servers).

Usage:
  python3 postprocess/fish_viz_server.py [--port 8783] [--host 0.0.0.0]
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import graph_store_pg as gs


def _serve_file(handler, path, ct):
    if not os.path.exists(path):
        handler.send_error(404, f'not found: {path}')
        return
    with open(path, 'rb') as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header('Content-Type', ct)
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.end_headers()
    handler.wfile.write(body)


def _serve_graph(handler, qs):
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    try:
        viz = gs.to_viz_json(sid, scope)
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return
    body = json.dumps(viz).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


def _serve_causal(handler, qs):
    """Empirical comm-edge data: for each publish event (fish_rclcpp_publish_link)
    return (ts_ns, vtid, pub_cb_addr, topic); plus per-topic the set of subscriber
    cb_addrs (derived from graph_nodes E + contains→F). Client uses these to
    compute causal BFS at click time."""
    import psycopg2, psycopg2.extras
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    dsn = os.environ.get('FISH_PG_DSN',
        'host=localhost port=5432 dbname=fish user=fish password=fish')
    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        # Publisher_handle → topic_name
        cur.execute("""
            SELECT payload->>'publisher_handle', payload->>'topic_name'
            FROM ros2_trace
            WHERE session_id=%s AND event='ros2:rcl_publisher_init'
        """, (sid,))
        pub_handle_topic = {h: t for (h, t) in cur.fetchall() if h and t}

        # Publish events (fish_rclcpp_publish_link)
        cur.execute("""
            SELECT ts_ns, vtid,
                   payload->>'callback', payload->>'publisher_handle'
            FROM ros2_trace
            WHERE session_id=%s AND event='ros2:fish_rclcpp_publish_link'
            ORDER BY ts_ns
        """, (sid,))
        pubs = []
        for (ts, vtid, pub_cb_addr, handle) in cur.fetchall():
            topic = pub_handle_topic.get(handle)
            if topic:
                pubs.append([ts, vtid, pub_cb_addr, topic])

        # Subscriber cb_addrs per topic
        cur.execute("""
            SELECT (e.attrs->'aspects'->0->>'topic') AS topic, f.cb_addr
            FROM graph_nodes e
            JOIN graph_edges ge ON ge.session_id=e.session_id AND ge.scope=e.scope
                               AND ge.source=e.node_id AND ge.rel='contains'
            JOIN graph_nodes f ON f.session_id=ge.session_id AND f.scope=ge.scope
                              AND f.node_id=ge.target AND f.type='F'
            WHERE e.session_id=%s AND e.scope=%s AND e.type='E' AND e.etype='sub'
              AND f.cb_addr IS NOT NULL AND f.cb_addr <> 'NA'
        """, (sid, scope))
        subs_by_topic = {}
        for (topic, cb) in cur.fetchall():
            if topic and cb:
                subs_by_topic.setdefault(topic, []).append(cb)
        conn.close()
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return

    # Need same t0_min as /api/gantt to relativize ts
    body = json.dumps({
        'session_id': sid, 'scope': scope,
        'n_pubs': len(pubs),
        'n_topics': len(subs_by_topic),
        'fields_pub': ['ts_ns', 'vtid', 'pub_cb_addr', 'topic'],
        'pubs': pubs,
        'subs_by_topic': subs_by_topic,
    }).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


def _serve_gantt_data(handler, qs):
    """JSON for per-thread gantt: one record per callback execution with
    {vtid, t0, t1, node, entity, etype, symbol, cb_addr}. Resolves cb_addr → E → N
    via the graph edges (callback function → owning entity → owning ROS node)."""
    import psycopg2, psycopg2.extras
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    pid_filter = qs.get('pid', [None])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    dsn = os.environ.get('FISH_PG_DSN',
        'host=localhost port=5432 dbname=fish user=fish password=fish')
    try:
        conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        cur = conn.cursor()
        sql = """
        WITH e_to_n AS (
          SELECT e.session_id, e.scope, e.node_id AS e_id,
                 n.label AS node_name
          FROM graph_nodes e
          JOIN graph_edges ge ON ge.session_id=e.session_id AND ge.scope=e.scope
                             AND ge.target=e.node_id AND ge.rel='contains'
          JOIN graph_nodes n ON n.session_id=ge.session_id AND n.scope=ge.scope
                            AND n.node_id=ge.source AND n.type='N'
          WHERE e.session_id=%s AND e.scope=%s AND e.type='E'
        ),
        f_to_n_graph AS (
          SELECT f.cb_addr,
                 f.label AS cb_symbol,
                 e.etype || ':' || e.label AS entity,
                 e.etype AS etype,
                 en.node_name
          FROM graph_nodes f
          JOIN graph_edges ge ON ge.session_id=f.session_id AND ge.scope=f.scope
                             AND ge.target=f.node_id AND ge.rel='contains'
          JOIN graph_nodes e ON e.session_id=ge.session_id AND e.scope=ge.scope
                            AND e.node_id=ge.source AND e.type='E'
          LEFT JOIN e_to_n en ON en.e_id=e.node_id
          WHERE f.session_id=%s AND f.scope=%s AND f.type='F'
            AND f.cb_addr IS NOT NULL AND f.cb_addr <> 'NA'
        ),
        -- Fallback: callbacks that the model didn't bind to an F (e.g. NITROS
        -- negotiated subs, where _keep_first picks the compat sub and the
        -- negotiated callback never gets an F node). Resolve via the raw
        -- tracepoints: callback → subscription_callback_added → subscription_init
        -- (or service / timer equivalents). Tag the entity with a 'guessed' prefix
        -- in etype so the viz can mark these visually if desired.
        sub_cb_to_topic AS (
          -- callback_added: subscription pointer -> callback pointer
          -- subscription_init: sub_handle -> topic_name (rcl_subscription_init)
          -- The rclcpp_subscription_init has subscription_handle + subscription
          --   (the cpp-level subscription object, used by callback_added).
          SELECT DISTINCT
                 ca.payload->>'callback' AS cb_addr,
                 ri.payload->>'topic_name' AS topic
          FROM ros2_trace ca
          JOIN ros2_trace si
            ON si.session_id = ca.session_id AND si.event='ros2:rclcpp_subscription_init'
           AND si.payload->>'subscription' = ca.payload->>'subscription'
          JOIN ros2_trace ri
            ON ri.session_id = si.session_id AND ri.event='ros2:rcl_subscription_init'
           AND ri.payload->>'subscription_handle' = si.payload->>'subscription_handle'
          WHERE ca.session_id = %s AND ca.event='ros2:rclcpp_subscription_callback_added'
        ),
        cb_symbols AS (
          SELECT payload->>'callback' AS cb_addr, payload->>'symbol' AS symbol
          FROM ros2_trace
          WHERE session_id=%s
            AND event IN ('ros2:rclcpp_callback_register','ros2:fish_rclpy_callback_register')
        ),
        -- Process → node fallback: any procname in ros2_trace, can be 'component_conta'
        pid_to_owning_node AS (
          -- graph_nodes.N rows have pid=NULL (model extractor doesn't bind it),
          -- so derive directly from ros2_trace.rcl_node_init events. Each
          -- (vpid, node_name) pair from node_init gives us the names of ROS
          -- nodes that ran in that process; group into one comma list per pid.
          -- Skip ros2cli / launch_ros chatter that pollutes the names.
          SELECT vpid AS pid,
                 string_agg(DISTINCT payload->>'node_name', ',' ORDER BY payload->>'node_name')
                   AS node_name
          FROM ros2_trace
          WHERE session_id=%s
            AND event = 'ros2:rcl_node_init'
            AND payload->>'node_name' IS NOT NULL
            AND payload->>'node_name' NOT LIKE '\_ros2cli%%'
            AND payload->>'node_name' NOT LIKE 'launch_ros%%'
            AND payload->>'node_name' NOT LIKE 'cnt\_%%'
          GROUP BY vpid
        ),
        f_to_n AS (
          -- 1. Graph-resolved
          SELECT cb_addr, cb_symbol, entity, etype, node_name FROM f_to_n_graph
          UNION ALL
          -- 2. Fallback: not in f_to_n_graph but has sub_cb_added → subscription_init
          SELECT s.cb_addr,
                 COALESCE(sym.symbol, '')                AS cb_symbol,
                 'sub:' || s.topic                       AS entity,
                 'sub'                                    AS etype,
                 NULL                                     AS node_name
          FROM sub_cb_to_topic s
          LEFT JOIN cb_symbols sym ON sym.cb_addr = s.cb_addr
          WHERE s.cb_addr NOT IN (SELECT cb_addr FROM f_to_n_graph)
        ),
        events AS (
          SELECT vpid AS pid, vtid, ts_ns, event,
                 payload->>'callback' AS cb_addr
          FROM ros2_trace
          WHERE session_id=%s
            AND event IN ('ros2:callback_start','ros2:callback_end')
        ),
        paired AS (
          SELECT pid, vtid, cb_addr, ts_ns AS t0, event,
                 lead(ts_ns) OVER (PARTITION BY vtid ORDER BY ts_ns) AS t1,
                 lead(event) OVER (PARTITION BY vtid ORDER BY ts_ns) AS next_event
          FROM events
        )
        SELECT p.pid, p.vtid, p.t0, p.t1, p.cb_addr,
               COALESCE(fn.node_name, pn.node_name, '(unknown)') AS node,
               COALESCE(fn.entity, p.cb_addr)                     AS entity,
               COALESCE(fn.etype, '?')                            AS etype,
               COALESCE(fn.cb_symbol, '')                         AS symbol
        FROM paired p
        LEFT JOIN f_to_n fn ON fn.cb_addr = p.cb_addr
        LEFT JOIN pid_to_owning_node pn ON pn.pid = p.pid
        WHERE p.event='ros2:callback_start' AND p.next_event='ros2:callback_end'
          AND p.t1 IS NOT NULL
        """
        # Param order: e_to_n(sid,scope), f_to_n_graph(sid,scope), sub_cb_to_topic(sid),
        # cb_symbols(sid), pid_to_owning_node(sid), events(sid)
        params = [sid, scope, sid, scope, sid, sid, sid, sid]
        if pid_filter:
            sql += " AND p.pid = %s"
            params.append(int(pid_filter))
        sql += " ORDER BY p.t0"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        # WARN: how many spans were resolved via the raw-tracepoint fallback
        # (i.e. the model didn't bind their cb_addr to an F node)?
        unresolved_via_graph = sum(
            1 for r in rows
            if r["etype"] == "sub" and r["entity"].startswith("sub:") and r["node"] != "(unknown)"
        )
        truly_unresolved = sum(1 for r in rows if r["node"] == "(unknown)")
        # Heuristic: a span resolved 'sub:<topic>' with no symbol attached
        # came through the fallback path. The model fix should reduce this to 0.
        n_fallback = sum(1 for r in rows if r["etype"] == "sub" and not r["symbol"])
        if truly_unresolved or n_fallback:
            sys.stderr.write(
                f"[fish-viz WARN] gantt session={sid} scope={scope}: "
                f"{len(rows)} spans, {n_fallback} resolved via tracepoint fallback "
                f"(model missed), {truly_unresolved} still node=(unknown)\n"
            )
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return

    t0_min = min((r['t0'] for r in rows), default=0)
    t1_max = max((r['t1'] for r in rows), default=0)
    body = json.dumps({
        'session_id': sid, 'scope': scope,
        'pid_filter': pid_filter,
        'n_spans': len(rows),
        't0_min_ns': t0_min, 't1_max_ns': t1_max,
        'spans': [
            # Compact array form to keep payload smaller
            [r['pid'], r['vtid'], r['t0'] - t0_min, r['t1'] - t0_min,
             r['node'], r['entity'], r['etype'], r['symbol'], r['cb_addr']]
            for r in rows
        ],
        'fields': ['pid', 'vtid', 't0_rel_ns', 't1_rel_ns', 'node', 'entity', 'etype', 'symbol', 'cb_addr'],
    }).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


class H(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        sys.stderr.write(f'[fish-viz] {self.address_string()} - {self.log_date_time_string()} - '
                         + (args[0] % args[1:]) + '\n')

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        if path in ('/', '/index.html', '/fish_viz_popup.html'):
            _serve_file(self, os.path.join(HERE, 'fish_viz_popup.html'), 'text/html; charset=utf-8')
        elif path in ('/gantt', '/gantt.html'):
            _serve_file(self, os.path.join(HERE, 'gantt.html'), 'text/html; charset=utf-8')
        elif path == '/fish_graph.json':
            _serve_graph(self, qs)
        elif path == '/api/gantt':
            _serve_gantt_data(self, qs)
        elif path == '/api/causal':
            _serve_causal(self, qs)
        elif path == '/health':
            self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
        else:
            # static file under HERE (for any css/js assets)
            local = os.path.normpath(os.path.join(HERE, path.lstrip('/')))
            if not local.startswith(HERE):
                self.send_error(403); return
            ct = 'text/plain'
            if local.endswith('.css'): ct = 'text/css'
            elif local.endswith('.js'): ct = 'application/javascript'
            elif local.endswith('.json'): ct = 'application/json'
            _serve_file(self, local, ct)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8783)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), H)
    print(f'fish-viz popup server on http://{args.host}:{args.port}/  '
          f'(graph API at /fish_graph.json?session_id=...&scope=...)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == '__main__':
    main()
