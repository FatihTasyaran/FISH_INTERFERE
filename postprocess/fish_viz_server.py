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


def _serve_taskset(handler, qs):
    """Hyperperiod + task table via extract_taskset.py as a subprocess.
    Returns the JSON it writes to stdout."""
    import subprocess
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    try:
        out = subprocess.run(
            ['python3', os.path.join(HERE, 'extract_taskset.py'),
             sid, '--scope', scope],
            check=True, capture_output=True, timeout=60,
        )
        body = out.stdout
    except subprocess.CalledProcessError as e:
        handler.send_error(500, f'extract_taskset failed: {e.stderr.decode()[:500]}')
        return
    except Exception as e:
        handler.send_error(500, f'extract_taskset error: {e}')
        return

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


def _window_stats(fires_sorted, anchor_ns, h_ns, end_ns):
    """Bucket sorted fire timestamps into H-sized windows on the anchor grid,
    but restrict to windows that fall between the cb's first fire and last
    fire (inclusive). Leading/trailing zero windows (before the cb has
    started or after it has stopped firing) are EXCLUDED. Middle gap
    windows where the cb temporarily went silent ARE counted as 0 —
    that's real signal."""
    if not h_ns or h_ns <= 0 or anchor_ns is None or end_ns is None:
        return None
    if not fires_sorted:
        return {'mean': 0, 'std': 0, 'min': 0, 'max': 0, 'n_windows': 0}
    valid = [ts for ts in fires_sorted if ts >= anchor_ns and ts < end_ns]
    if not valid:
        return {'mean': 0, 'std': 0, 'min': 0, 'max': 0, 'n_windows': 0}
    first_idx = (valid[0]  - anchor_ns) // h_ns
    last_idx  = (valid[-1] - anchor_ns) // h_ns
    n_windows = last_idx - first_idx + 1
    counts = [0] * n_windows
    for ts in valid:
        counts[((ts - anchor_ns) // h_ns) - first_idx] += 1
    mean = sum(counts) / n_windows
    var = sum((c - mean) ** 2 for c in counts) / n_windows
    return {
        'mean': round(mean, 3),
        'std': round(var ** 0.5, 3),
        'min': min(counts),
        'max': max(counts),
        'n_windows': n_windows,
    }


def _serve_cb_stats(handler, qs):
    """Per-callback execution stats per H_tmr (executor) and H_session window.
    Returns one row per cb_addr with mean/std/min/max fires/window."""
    import subprocess, psycopg2
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    dsn = os.environ.get('FISH_PG_DSN',
        'host=localhost port=5432 dbname=fish user=fish password=fish')
    try:
        out = subprocess.run(
            ['python3', os.path.join(HERE, 'extract_taskset.py'),
             sid, '--scope', scope],
            check=True, capture_output=True, timeout=120,
        )
        taskset = json.loads(out.stdout)
    except subprocess.CalledProcessError as e:
        handler.send_error(500, f'extract_taskset failed: {e.stderr.decode()[:500]}')
        return
    except Exception as e:
        handler.send_error(500, f'extract_taskset error: {e}')
        return

    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        cur.execute("SELECT start_ts_ns, end_ts_ns FROM sessions WHERE session_id=%s", (sid,))
        row = cur.fetchone()
        if not row:
            handler.send_error(404, f'session not found: {sid}')
            conn.close(); return
        session_start_ns, session_end_ns = row
        cur.execute("""
            SELECT payload->>'callback' AS cb_addr, vpid AS pid, ts_ns
            FROM ros2_trace
            WHERE session_id=%s AND event='ros2:callback_start'
              AND payload->>'callback' IS NOT NULL
            ORDER BY ts_ns
        """, (sid,))
        fires_by_cb = {}
        for cb, pid, ts in cur.fetchall():
            fires_by_cb.setdefault(cb, []).append(ts)
        conn.close()
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return

    # Anchor per-pid + session, computed from FIRST firing of any tmr-nominal cb
    tmr_cbs_by_pid = {}
    tmr_cbs_all = set()
    for pid_str, ex in taskset.get('executors', {}).items():
        for t in (ex.get('tasks') or []):
            if t.get('period_source') == 'tmr-nominal' and t.get('cb_addr'):
                tmr_cbs_by_pid.setdefault(pid_str, set()).add(t['cb_addr'])
                tmr_cbs_all.add(t['cb_addr'])
    anchor_by_pid = {}
    anchor_session = None
    for pid_str, tmr_set in tmr_cbs_by_pid.items():
        for cb in tmr_set:
            for ts in fires_by_cb.get(cb, ()):
                cur = anchor_by_pid.get(pid_str)
                if cur is None or ts < cur:
                    anchor_by_pid[pid_str] = ts
                if anchor_session is None or ts < anchor_session:
                    anchor_session = ts

    H_session = taskset.get('session_hyperperiod_tmr_only_ns')
    rows = []
    for pid_str, ex in taskset.get('executors', {}).items():
        H_tmr = ex.get('hyperperiod_tmr_only_ns')
        anchor_ex = anchor_by_pid.get(pid_str)
        ex_label = ex.get('label')
        for t in (ex.get('tasks') or []):
            cb = t.get('cb_addr')
            fires = fires_by_cb.get(cb, [])
            ex_stats = _window_stats(fires, anchor_ex, H_tmr, session_end_ns) \
                if (H_tmr and anchor_ex) else None
            sess_stats = _window_stats(fires, anchor_session, H_session, session_end_ns) \
                if (H_session and anchor_session) else None
            ipw = t.get('ipc_waitable')
            rows.append({
                'cb_addr': cb,
                'symbol': t.get('symbol', '') or '',
                'ex_label': ex_label,
                'pid': pid_str,
                'node': t.get('node', '') or '',
                'entity': t.get('entity', '') or '',
                'period_source': t.get('period_source'),
                'period_ns': t.get('period_ns'),
                'n_total': len(fires),
                # intra-process sub: outer branch-5 Waitable dispatch count
                # (should equal n_total; a gap = dispatches whose inner cb
                # did not run, e.g. empty intra-proc buffer take).
                'ipc_capable': bool(t.get('ipc_capable')),
                'delivery': t.get('delivery'),
                'n_dispatch_ipc': len(fires_by_cb.get(ipw, [])) if ipw else None,
                'join_group': t.get('join_group'),
                'join_role': t.get('join_role'),
                'h_tmr_ns': H_tmr,
                'ex_stats': ex_stats,
                'sess_stats': sess_stats,
            })

    body = json.dumps({
        'session_id': sid,
        'scope': scope,
        'session_duration_ns': session_end_ns - session_start_ns,
        'h_session_tmr_only_ns': H_session,
        'rows': rows,
    }).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


def _serve_task_graphs(handler, qs):
    """Decompose the F-subgraph into weakly-connected components on comm edges.
    Each WCC is a task graph (DAG). Control-plane Fs (parameter_events, rosout,
    _supported_types) are stripped by default (?strip_control=0 to keep them).
    Returns one summary record per WCC."""
    import psycopg2
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    strip_control = (qs.get('strip_control', ['1'])[0] != '0')
    CONTROL_PATTERNS = ('/parameter_events', '/rosout', '/_supported_types')
    dsn = os.environ.get('FISH_PG_DSN',
        'host=localhost port=5432 dbname=fish user=fish password=fish')

    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        # F + E + N + EX in one shot
        cur.execute("""
          WITH e_to_n AS (
            SELECT e.session_id, e.scope, e.node_id AS e_id, n.label AS node_name
            FROM graph_nodes e
            JOIN graph_edges ge ON ge.session_id=e.session_id AND ge.scope=e.scope
                               AND ge.target=e.node_id AND ge.rel='contains'
            JOIN graph_nodes n ON n.session_id=ge.session_id AND n.scope=ge.scope
                              AND n.node_id=ge.source AND n.type='N'
            WHERE e.session_id=%s AND e.scope=%s AND e.type='E'
          ),
          n_to_ex AS (
            SELECT n.session_id, n.scope, n.node_id AS n_id,
                   ex.label AS ex_label, ex.pid AS ex_pid
            FROM graph_nodes n
            JOIN graph_edges ge ON ge.session_id=n.session_id AND ge.scope=n.scope
                               AND ge.target=n.node_id AND ge.rel='contains'
            JOIN graph_nodes ex ON ex.session_id=ge.session_id AND ex.scope=ge.scope
                                AND ex.node_id=ge.source AND ex.type='EX'
            WHERE n.session_id=%s AND n.scope=%s AND n.type='N'
          )
          SELECT f.cb_addr, f.label AS symbol,
                 e.etype, e.label AS entity_label,
                 COALESCE(en.node_name, '') AS node_name,
                 COALESCE(nx.ex_label, '')  AS ex_label,
                 nx.ex_pid
          FROM graph_nodes f
          JOIN graph_edges ge_fe ON ge_fe.session_id=f.session_id AND ge_fe.scope=f.scope
                                AND ge_fe.target=f.node_id AND ge_fe.rel='contains'
          JOIN graph_nodes e ON e.session_id=ge_fe.session_id AND e.scope=ge_fe.scope
                            AND e.node_id=ge_fe.source AND e.type='E'
          LEFT JOIN e_to_n  en ON en.e_id = e.node_id
          LEFT JOIN graph_edges ge_ne ON ge_ne.session_id=e.session_id AND ge_ne.scope=e.scope
                                     AND ge_ne.target=e.node_id AND ge_ne.rel='contains'
          LEFT JOIN graph_nodes n ON n.session_id=ge_ne.session_id AND n.scope=ge_ne.scope
                                  AND n.node_id=ge_ne.source AND n.type='N'
          LEFT JOIN n_to_ex nx ON nx.n_id = n.node_id
          WHERE f.session_id=%s AND f.scope=%s AND f.type='F'
            AND f.cb_addr IS NOT NULL AND f.cb_addr <> 'NA'
        """, (sid, scope, sid, scope, sid, scope))
        f_meta = {}
        for cb, sym, etype, ent, node, ex_label, ex_pid in cur.fetchall():
            f_meta[cb] = {
                'symbol': sym or '',
                'etype': etype,
                'entity': f"{etype}:{ent}" if (etype and ent) else '',
                'node': node or '',
                'ex_label': ex_label or '',
                'ex_pid': ex_pid,
            }

        # Comm edges between F nodes
        cur.execute("""
          WITH f_nodes AS (
            SELECT node_id, cb_addr FROM graph_nodes
            WHERE session_id=%s AND scope=%s AND type='F'
              AND cb_addr IS NOT NULL AND cb_addr <> 'NA'
          )
          SELECT s.cb_addr, t.cb_addr, ge.attrs->>'topic'
          FROM graph_edges ge
          JOIN f_nodes s ON s.node_id=ge.source
          JOIN f_nodes t ON t.node_id=ge.target
          WHERE ge.session_id=%s AND ge.scope=%s AND ge.rel='comm'
        """, (sid, scope, sid, scope))
        edges = [{'src': r[0], 'dst': r[1], 'topic': r[2]} for r in cur.fetchall()]

        # Fire counts per cb
        cur.execute("""
          SELECT payload->>'callback', COUNT(*)
          FROM ros2_trace
          WHERE session_id=%s AND event='ros2:callback_start'
            AND payload->>'callback' IS NOT NULL
          GROUP BY 1
        """, (sid,))
        fire_count = {r[0]: r[1] for r in cur.fetchall()}
        conn.close()
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return

    # Optionally drop control-plane Fs
    if strip_control:
        drop = {cb for cb, m in f_meta.items()
                if any(pat in m.get('entity', '') for pat in CONTROL_PATTERNS)}
        f_meta = {cb: m for cb, m in f_meta.items() if cb not in drop}
        edges = [e for e in edges if e['src'] in f_meta and e['dst'] in f_meta]

    # Adjacency: undirected for WCC, directed for root/sink
    und = {cb: set() for cb in f_meta}
    inc = {cb: set() for cb in f_meta}
    out = {cb: set() for cb in f_meta}
    for e in edges:
        if e['src'] in f_meta and e['dst'] in f_meta:
            und[e['src']].add(e['dst'])
            und[e['dst']].add(e['src'])
            inc[e['dst']].add(e['src'])
            out[e['src']].add(e['dst'])

    # BFS for WCCs
    visited = set()
    wccs = []
    for cb in sorted(f_meta.keys()):
        if cb in visited:
            continue
        comp = []
        queue = [cb]
        visited.add(cb)
        while queue:
            c = queue.pop()
            comp.append(c)
            for nb in und[c]:
                if nb not in visited:
                    visited.add(nb); queue.append(nb)
        wccs.append(set(comp))
    wccs.sort(key=lambda c: -len(c))

    out_wccs = []
    for i, comp in enumerate(wccs):
        roots  = sorted(cb for cb in comp if not inc[cb])
        sinks  = sorted(cb for cb in comp if not out[cb])
        timer_roots = [cb for cb in roots if f_meta[cb].get('etype') == 'tmr']
        wcc_edges = [e for e in edges if e['src'] in comp and e['dst'] in comp]
        # Longest path (DAG depth) — simple topo via BFS layer
        # Compute depth from any root via Kahn-style relaxation
        depth = {cb: 0 for cb in comp}
        order_left = {cb: len(inc[cb] & comp) for cb in comp}
        ready = [cb for cb in comp if order_left[cb] == 0]
        while ready:
            c = ready.pop()
            for nb in out[c]:
                if nb in comp:
                    if depth[nb] < depth[c] + 1:
                        depth[nb] = depth[c] + 1
                    order_left[nb] -= 1
                    if order_left[nb] == 0:
                        ready.append(nb)
        max_depth = max(depth.values()) if depth else 0
        out_wccs.append({
            'idx': i,
            'n_cbs': len(comp),
            'n_edges': len(wcc_edges),
            'n_roots': len(roots),
            'n_timer_roots': len(timer_roots),
            'n_sinks': len(sinks),
            'max_depth': max_depth,
            'total_fires': sum(fire_count.get(cb, 0) for cb in comp),
            'distinct_executors': sorted({f_meta[cb].get('ex_pid')
                                          for cb in comp if f_meta[cb].get('ex_pid')}),
            'roots': [{
                'cb_addr': cb, 'symbol': f_meta[cb].get('symbol', '')[:120],
                'etype': f_meta[cb].get('etype'),
                'entity': f_meta[cb].get('entity'),
                'node': f_meta[cb].get('node'),
                'ex_label': f_meta[cb].get('ex_label'),
                'ex_pid': f_meta[cb].get('ex_pid'),
                'n_fires': fire_count.get(cb, 0),
            } for cb in roots],
            'sinks': [{
                'cb_addr': cb, 'symbol': f_meta[cb].get('symbol', '')[:80],
                'etype': f_meta[cb].get('etype'),
                'entity': f_meta[cb].get('entity'),
                'node': f_meta[cb].get('node'),
            } for cb in sinks],
            'nodes': [{
                'cb_addr': cb, 'symbol': f_meta[cb].get('symbol', '')[:120],
                'etype': f_meta[cb].get('etype'),
                'entity': f_meta[cb].get('entity'),
                'node': f_meta[cb].get('node'),
                'ex_label': f_meta[cb].get('ex_label'),
                'ex_pid': f_meta[cb].get('ex_pid'),
                'n_fires': fire_count.get(cb, 0),
                'depth': depth[cb],
                'is_root': cb in inc and not inc[cb],
                'is_sink': cb in out and not out[cb],
            } for cb in sorted(comp)],
            'edges': wcc_edges,
        })

    body = json.dumps({
        'session_id': sid, 'scope': scope,
        'strip_control': strip_control,
        'n_f_nodes': len(f_meta),
        'n_edges': len(edges),
        'n_wccs': len(wccs),
        'wccs': out_wccs,
    }).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# Topic classes for FT (Fish Task) grouping
# ---------------------------------------------------------------------------
# An FT is a weakly-connected component of the F-graph over *data-carrying*
# comm edges. ROS 2 infrastructure topics (/diagnostics, /tf, /tf_static,
# /parameter_events, /clock, /rosout, ...) are published/consumed by almost
# every node and would glue unrelated pipelines into one giant FT — measured
# on Autoware fish_20260816_165836: FT#0 = 299 F / 491 edges of which 160
# /diagnostics + 96 /tf + 17 /tf_static; without those three the same set
# splits into 112 components (localization 46, lidar perception 37, control
# 27, IMU odometry 15, ...). Infra edges are still returned (edge_class=
# 'infra') and drawn faded; ?infra=include restores the old grouping.
# Override/extend the list via FISH_INFRA_TOPICS (comma-separated globs).
import fnmatch as _fnmatch
_INFRA_TOPIC_GLOBS = [
    # ROS 2 plumbing
    '/tf', '/tf_static',
    '/parameter_events', '/clock', '/rosout', '/service_log',
    '/*/describe_parameters', '/*/get_parameters', '/*/get_parameter_types',
    '/*/list_parameters', '/*/set_parameters', '/*/set_parameters_atomically',
    # diagnostics / statistics (rqt_graph "Debug" quiet list: /clock /rosout
    # /statistics /diag_agg /time — see rqt_graph/dotcode.py QUIET_NAMES)
    '/diagnostics', '/diagnostics_agg', '/diagnostics_toplevel_state',
    '/statistics', '/time', '/diag_agg',
    # Autoware debug / timing side channels
    '*/debug/*', '*/processing_time_ms', '*/processing_time_detail_ms',
    '*/cyclic_time_ms', '*/pipeline_latency_ms',
    # rqt_graph "hidden" convention: last name segment starting with '_'
    # (e.g. /foo/_bar) — internal / hidden topics.
    '*/_*',
]
_env_globs = os.environ.get('FISH_INFRA_TOPICS')
if _env_globs:
    _INFRA_TOPIC_GLOBS = [g.strip() for g in _env_globs.split(',') if g.strip()]


def topic_class(topic: str) -> str:
    """'infra' for ROS 2 plumbing / diagnostics topics, else 'data'."""
    if not topic:
        return 'data'
    for g in _INFRA_TOPIC_GLOBS:
        if _fnmatch.fnmatchcase(topic, g):
            return 'infra'
    return 'data'


def _glob_list(spec):
    """rqt_graph-style include/exclude spec: comma-separated globs, '-' prefix
    excludes. '' or '/' means include everything. Returns (includes, excludes)."""
    inc, exc = [], []
    for tok in (spec or '').split(','):
        tok = tok.strip()
        if not tok or tok == '/':
            continue
        if tok.startswith('-'):
            exc.append(tok[1:])
        else:
            inc.append(tok)
    return inc, exc


def _glob_match(name, inc, exc):
    if any(_fnmatch.fnmatchcase(name or '', g) for g in exc):
        return False
    if not inc:
        return True
    return any(_fnmatch.fnmatchcase(name or '', g) for g in inc)


def _fetch_wcc_payload(sid, scope, allowed, infra_mode='exclude',
                       node_filter='', topic_filter='', ext_mode='show'):
    """Shared backbone for /api/wcc and /api/wcc-svg.

    Filters (rqt_graph-inspired):
      infra_mode   'exclude' (default) | 'include' — do infra topics glue FTs?
      node_filter  comma-separated globs on the owning ROS node name; '-' prefix
                   excludes (rqt_graph "Namespaces" box). Applied to F nodes.
      topic_filter same, on edge topic (rqt_graph "Topics" box). Applied to edges.
      ext_mode     'show' (default) | 'hide' — drop external boundary pseudo-F
                   nodes (ptype='ext'): source-ext = topics published by nothing
                   we traced (bag, other hosts), sink-ext = topics with no traced
                   subscriber (rqt_graph "Dead sinks"). Their edges become
                   boundary edges recorded per FT as n_ext_in / n_ext_out.

    Returns: (fnodes, edges, out_wccs, n_wccs) on success, raises on PG error.
    """
    node_inc, node_exc = _glob_list(node_filter)
    topic_inc, topic_exc = _glob_list(topic_filter)
    import psycopg2
    dsn = os.environ.get('FISH_PG_DSN',
        'host=localhost port=5432 dbname=fish user=fish password=fish')
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    # F vertices with parent entity + node + executor + attrs (for phase, gpu_node)
    cur.execute("""
      WITH e_to_n AS (
        SELECT e.session_id, e.scope, e.node_id AS e_id, n.label AS node_name,
               COALESCE(n.full_name, n.label) AS node_full
        FROM graph_nodes e
        JOIN graph_edges ge ON ge.session_id=e.session_id AND ge.scope=e.scope
                           AND ge.target=e.node_id AND ge.rel='contains'
        JOIN graph_nodes n  ON n.session_id=ge.session_id AND n.scope=ge.scope
                           AND n.node_id=ge.source AND n.type='N'
        WHERE e.session_id=%s AND e.scope=%s AND e.type='E'
      )
      SELECT f.node_id, f.cb_addr, f.label AS symbol, f.attrs, f.ptype,
             e.etype, e.label AS entity_label,
             COALESCE(en.node_name, '') AS node_name,
             COALESCE(en.node_full, '') AS node_full
      FROM graph_nodes f
      JOIN graph_edges ge_fe ON ge_fe.session_id=f.session_id AND ge_fe.scope=f.scope
                            AND ge_fe.target=f.node_id AND ge_fe.rel='contains'
      JOIN graph_nodes e ON e.session_id=ge_fe.session_id AND e.scope=ge_fe.scope
                        AND e.node_id=ge_fe.source AND e.type='E'
      LEFT JOIN e_to_n en ON en.e_id = e.node_id
      WHERE f.session_id=%s AND f.scope=%s AND f.type='F'
    """, (sid, scope, sid, scope))
    fnodes = {}
    for f_id, cb, sym, attrs, ptype_col, etype, ent_label, node_name, node_full in cur.fetchall():
        attrs = attrs or {}
        # graph_store_pg lifts ptype into a column; older rows may still
        # carry it in attrs.
        ptype = ptype_col or attrs.get('ptype') or 'cpu'
        phase = attrs.get('phase', 'unknown')
        is_ext = (ptype == 'ext')
        if is_ext:
            phase = 'data'
        if phase not in allowed:
            continue
        if ext_mode == 'hide' and is_ext:
            continue
        # node filter on the FULL ROS node name (e.g. /sensing/lidar/concatenate_data).
        # ext pseudo-nodes have no owning node → keep them unless an explicit
        # exclude glob matches their 'ext:<topic>' label.
        if node_inc or node_exc:
            if is_ext:
                if node_exc and any(_fnmatch.fnmatchcase(sym or '', g) for g in node_exc):
                    continue
            elif not _glob_match(node_full or node_name or '', node_inc, node_exc):
                continue
        fnodes[f_id] = {
            'id': f_id,
            'cb_addr': cb or '',
            'symbol': sym or '',
            'etype': etype or 'ext',
            'ent_label': ent_label or '',
            'node': node_name or '',
            'node_full': node_full or '',
            'phase': phase,
            'gpu_node': bool(attrs.get('gpu_node')),
            'ptype': ptype,
        }

    cur.execute("""
      SELECT source, target, attrs
      FROM graph_edges
      WHERE session_id=%s AND scope=%s AND rel='comm' AND level = 'L3'
    """, (sid, scope))
    edges = []
    n_edges_topic_filtered = 0
    for s, t, a in cur.fetchall():
        if s in fnodes and t in fnodes:
            a = a or {}
            topic = a.get('topic') or a.get('service', '')
            if (topic_inc or topic_exc) and not _glob_match(topic, topic_inc, topic_exc):
                n_edges_topic_filtered += 1
                continue
            edges.append({
                'src': s, 'dst': t,
                'topic': topic,
                'nature': a.get('nature', ''),
                'intra_node': bool(a.get('intra_node')),
                'edge_class': 'data' if a.get('nature') == 'join' else topic_class(topic),
            })

    cur.execute("""
      SELECT vpid, vtid, ts_ns, event, payload->>'callback' AS cb
      FROM ros2_trace
      WHERE session_id=%s
        AND event IN ('ros2:callback_start', 'ros2:callback_end')
        AND payload->>'callback' IS NOT NULL
      ORDER BY vpid, vtid, ts_ns
    """, (sid,))
    durations = {}
    open_starts = {}
    for vpid, vtid, ts, ev, cb in cur.fetchall():
        key = (vpid, vtid, cb)
        ts = int(ts)
        if ev == 'ros2:callback_start':
            open_starts[key] = ts
        else:
            start_ts = open_starts.pop(key, None)
            if start_ts is not None:
                durations.setdefault(cb, []).append(ts - start_ts)
    cb_stats = {}
    for cb, ds in durations.items():
        if not ds: continue
        n = len(ds)
        mean = sum(ds) / n
        if n > 1:
            var = sum((x - mean) ** 2 for x in ds) / n
            std = var ** 0.5
        else:
            std = 0.0
        cb_stats[cb] = {
            'count': n, 'min_ns': min(ds), 'max_ns': max(ds),
            'avg_ns': mean, 'std_ns': std,
        }
    conn.close()

    for n in fnodes.values():
        n['stats'] = cb_stats.get(n['cb_addr'])

    # FT grouping edges: data-only by default (see _INFRA_TOPIC_GLOBS).
    group_edges = edges if infra_mode == 'include' \
        else [e for e in edges if e['edge_class'] != 'infra']
    und = {fid: set() for fid in fnodes}
    for e in group_edges:
        und[e['src']].add(e['dst'])
        und[e['dst']].add(e['src'])
    visited, wccs = set(), []
    for start in fnodes:
        if start in visited: continue
        comp, queue = [], [start]
        visited.add(start)
        while queue:
            c = queue.pop()
            comp.append(c)
            for nb in und[c]:
                if nb not in visited:
                    visited.add(nb); queue.append(nb)
        wccs.append(set(comp))
    wccs.sort(key=lambda c: -len(c))

    out_wccs = []
    for i, comp in enumerate(wccs):
        if len(comp) < 2:
            continue
        wcc_nodes = [fnodes[f_id] for f_id in sorted(comp)]
        wcc_edges = [e for e in edges if e['src'] in comp and e['dst'] in comp]
        n_infra = sum(1 for e in wcc_edges if e['edge_class'] == 'infra')
        n_ext_src = sum(1 for n in wcc_nodes if n['ptype'] == 'ext' and any(e['src'] == n['id'] for e in wcc_edges))
        n_ext_snk = sum(1 for n in wcc_nodes if n['ptype'] == 'ext' and any(e['dst'] == n['id'] for e in wcc_edges))
        out_wccs.append({
            'idx': i,
            'n_cbs': len(comp),
            'n_real_cbs': sum(1 for n in wcc_nodes if n['ptype'] != 'ext'),
            'n_edges': len(wcc_edges),
            'n_data_edges': len(wcc_edges) - n_infra,
            'n_infra_edges': n_infra,
            'n_ext_sources': n_ext_src,
            'n_ext_sinks': n_ext_snk,
            'nodes': wcc_nodes,
            'edges': wcc_edges,
        })
    return fnodes, edges, out_wccs, len(wccs)


def _namespace_views(fnodes, edges, min_f=2):
    """One pseudo-FT per TOP-LEVEL ROS namespace (/sensing, /perception, …):
    every F whose owning node lives under that namespace + the edges among
    them (data AND infra, the renderer fades infra). Unlike FTs these are
    NOT connectivity classes — they are the "what does this subsystem look
    like" view the user asked for (the FT list continues with them). ext
    pseudo-nodes have no namespace and are attached to a namespace view only
    if they connect to one of its F's (as boundary sources/sinks)."""
    by_ns = {}
    for fid, n in fnodes.items():
        full = n.get('node_full') or ''
        if n.get('ptype') == 'ext' or not full.startswith('/'):
            continue
        parts = full.strip('/').split('/')
        ns = '/' + parts[0] if len(parts) > 1 else '/'   # top-level namespace ('/' for root nodes)
        by_ns.setdefault(ns, set()).add(fid)
    views = []
    for ns, fids in sorted(by_ns.items()):
        # pull in ext boundary nodes touching this namespace
        touching = set(fids)
        for e in edges:
            if e['src'] in fids and fnodes[e['dst']].get('ptype') == 'ext': touching.add(e['dst'])
            if e['dst'] in fids and fnodes[e['src']].get('ptype') == 'ext': touching.add(e['src'])
        if len(fids) < min_f:
            continue
        v_nodes = [fnodes[f] for f in sorted(touching)]
        v_edges = [e for e in edges if e['src'] in touching and e['dst'] in touching]
        n_infra = sum(1 for e in v_edges if e['edge_class'] == 'infra')
        views.append({
            'idx': f'ns:{ns}', 'kind': 'namespace', 'namespace': ns,
            'n_cbs': len(v_nodes), 'n_real_cbs': len(fids),
            'n_edges': len(v_edges), 'n_data_edges': len(v_edges) - n_infra, 'n_infra_edges': n_infra,
            'n_ext_sources': sum(1 for n in v_nodes if n['ptype'] == 'ext' and any(e['src'] == n['id'] for e in v_edges)),
            'n_ext_sinks':   sum(1 for n in v_nodes if n['ptype'] == 'ext' and any(e['dst'] == n['id'] for e in v_edges)),
            'nodes': v_nodes, 'edges': v_edges,
        })
    return views


def _serve_wcc(handler, qs):
    """Phase-filtered F-subgraph WCCs with full per-node + per-edge metadata.

    Returns one record per WCC with embedded nodes + edges. Each node
    carries: cb_addr, etype, ent_label, node (full ROS node name), full
    symbol, phase, gpu_node flag, exec_stats (count, min/max/avg/std in
    ns). Each edge carries: src, dst, topic, nature (gxf_internal vs
    others).

    Default filter: phase == "data". Override with ?include_init=1 or
    ?include_unknown=1.
    """
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    include_init      = (qs.get('include_init',      ['0'])[0] != '0')
    include_zero_exec = (qs.get('include_zero_exec', ['0'])[0] != '0')
    include_unknown   = (qs.get('include_unknown',   ['0'])[0] != '0')
    allowed = {'data'}
    if include_init:      allowed.add('init')
    if include_zero_exec: allowed.add('zero_exec')
    if include_unknown:   allowed.add('unknown')
    infra_mode = (qs.get('infra', ['exclude'])[0] or 'exclude').lower()
    if infra_mode not in ('exclude', 'include'):
        infra_mode = 'exclude'
    node_filter  = (qs.get('node',  [''])[0] or '')
    topic_filter = (qs.get('topic', [''])[0] or '')
    ext_mode = (qs.get('ext', ['show'])[0] or 'show').lower()
    if ext_mode not in ('show', 'hide'):
        ext_mode = 'show'

    try:
        fnodes, edges, out_wccs, n_wccs = _fetch_wcc_payload(
            sid, scope, allowed, infra_mode, node_filter, topic_filter, ext_mode)
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return

    n_infra = sum(1 for e in edges if e['edge_class'] == 'infra')
    ns_views = _namespace_views(fnodes, edges)
    body = json.dumps({
        'session_id': sid, 'scope': scope,
        'allowed_phases': sorted(allowed),
        'infra_mode': infra_mode,
        'infra_topic_globs': _INFRA_TOPIC_GLOBS,
        'namespaces': [{k: v for k, v in nv.items() if k not in ('nodes', 'edges')} for nv in ns_views],
        'node_filter': node_filter, 'topic_filter': topic_filter, 'ext_mode': ext_mode,
        'n_f_nodes': len(fnodes),
        'n_edges': len(edges),
        'n_data_edges': len(edges) - n_infra,
        'n_infra_edges': n_infra,
        'n_wccs': n_wccs,
        'wccs': out_wccs,
        'namespace_views': ns_views,
    }).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


_ETYPE_FILL = {
    'sub':      '#bfe1f5',
    'serv':     '#f5d6bf',
    'tmr':      '#f5bfbf',
    'waitable': '#fff2a3',
    'ext':      '#dddddd',
}


def _dot_escape(s: str) -> str:
    if s is None:
        return ''
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _wcc_to_dot(wcc: dict) -> str:
    """Render one WCC's nodes+edges into Graphviz DOT source.

    Each F vertex becomes a 2-line labelled box (line 1: '[etype] ent_label',
    line 2: cb_addr). Boxes are clustered by their owning ROS node so the
    DAG layout reflects per-node grouping. Edges colored: green for
    GXF intra-node (nature=gxf_internal), grey otherwise. The full
    metadata payload lives on the client side; here we only emit the
    minimal text. Each node carries id="n<fid>" so client JS can attach
    hover handlers via getElementById.
    """
    from collections import defaultdict
    nodes = wcc['nodes']
    edges = wcc['edges']

    by_node = defaultdict(list)
    for n in nodes:
        by_node[n.get('node') or '__external__'].append(n)

    out = []
    out.append('digraph wcc {')
    out.append('  rankdir=TB;')
    out.append('  newrank=true;')  # critical for clustered graphs — without this,
                                   # dot raises "init_rank" on big multi-cluster DAGs
    out.append('  graph [fontname="Helvetica" fontsize=11 nodesep=0.30 ranksep=0.55 '
               'bgcolor="white" splines=spline compound=true];')
    out.append('  node  [shape=box style="filled,rounded" fontname="Helvetica" '
               'fontsize=10 margin="0.12,0.06" width=2 height=0.55 fixedsize=false];')
    out.append('  edge  [fontname="Helvetica" fontsize=8 color="#444"];')

    # Clusters per ROS node
    import re
    for nm, items in sorted(by_node.items()):
        safe = re.sub(r'\W+', '_', nm)
        out.append(f'  subgraph cluster_{safe} {{')
        out.append(f'    label="{_dot_escape(nm)}"; style="rounded"; '
                   f'bgcolor="#fafafa"; color="#888"; fontsize=10;')
        for n in items:
            etype = n.get('etype', 'ext')
            fill = _ETYPE_FILL.get(etype, '#ffffff')
            border = '#d62728' if n.get('gpu_node') else '#888'
            penw = '3' if n.get('gpu_node') else '1'
            ent_label = n.get('ent_label') or n.get('symbol') or '?'
            cb = n.get('cb_addr') or '–'
            # Two-line label via DOT escape "\n"
            line1 = f"[{etype}] {ent_label}" if etype != 'ext' else ent_label
            label = f"{_dot_escape(line1)}\\n{_dot_escape(cb)}"
            out.append(
                f'    n{n["id"]} [id="n{n["id"]}" label="{label}" '
                f'fillcolor="{fill}" color="{border}" penwidth={penw}];'
            )
        out.append('  }')

    for e in edges:
        topic = e.get('topic') or ''
        style = 'solid'
        if e.get('nature') == 'gxf_internal':
            color = '#2ca02c'; pw = '2'
            label = 'GXF'
        elif e.get('nature') == 'join':
            # join membership tie (detect_joins): not a message hop
            color = '#1f77b4'; pw = '1.5'; style = 'dotted'
            label = 'join'
        elif e.get('edge_class') == 'infra':
            color = '#bbbbbb'; pw = '0.8'; style = 'dashed'
            label = topic
        else:
            color = '#444'; pw = '1.2'
            label = topic
        out.append(
            f'  n{e["src"]} -> n{e["dst"]} '
            f'[label="{_dot_escape(label)}" color="{color}" fontcolor="{color}" '
            f'penwidth={pw} style={style}];'
        )
    out.append('}')
    return '\n'.join(out)


def _render_dot_to_svg(dot_source: str) -> str:
    """Pipe DOT source through `dot -Tsvg` and return the SVG string."""
    import subprocess
    p = subprocess.run(['dot', '-Tsvg'],
                       input=dot_source.encode(),
                       capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f'dot failed: {p.stderr.decode()[:400]}')
    return p.stdout.decode()


def _serve_wcc_svg(handler, qs):
    """Same payload shape as /api/wcc but each WCC also carries a server-
    rendered Graphviz SVG. Use `dot` for the layout (rankdir=TB, layered
    DAG) and embed the SVG inline on the client side. Each <g class="node">
    in the SVG gets id="n<fid>"; the client maps that id to the full
    metadata (cb_addr / phase / stats / etc.) and attaches hover handlers.

    Each WCC's nodes are clustered by their owning ROS node so the layout
    surfaces per-node grouping the same way our gpu_dag skeletons do.
    """
    sid = (qs.get('session_id') or qs.get('session') or [''])[0]
    scope = (qs.get('scope') or ['__main__'])[0]
    if not sid:
        handler.send_error(400, 'session_id required')
        return
    include_init      = (qs.get('include_init',      ['0'])[0] != '0')
    include_zero_exec = (qs.get('include_zero_exec', ['0'])[0] != '0')
    include_unknown   = (qs.get('include_unknown',   ['0'])[0] != '0')
    allowed = {'data'}
    if include_init:      allowed.add('init')
    if include_zero_exec: allowed.add('zero_exec')
    if include_unknown:   allowed.add('unknown')
    infra_mode = (qs.get('infra', ['exclude'])[0] or 'exclude').lower()
    if infra_mode not in ('exclude', 'include'):
        infra_mode = 'exclude'
    node_filter  = (qs.get('node',  [''])[0] or '')
    topic_filter = (qs.get('topic', [''])[0] or '')
    ext_mode = (qs.get('ext', ['show'])[0] or 'show').lower()
    if ext_mode not in ('show', 'hide'):
        ext_mode = 'show'

    try:
        fnodes, edges, out_wccs, n_wccs = _fetch_wcc_payload(
            sid, scope, allowed, infra_mode, node_filter, topic_filter, ext_mode)
    except Exception as e:
        handler.send_error(500, f'PG error: {e}')
        return

    # Render SVG per WCC, then one per top-level namespace (appended).
    rendered = []
    for w in out_wccs + _namespace_views(fnodes, edges):
        try:
            dot_src = _wcc_to_dot(w)
            svg = _render_dot_to_svg(dot_src)
        except Exception as e:
            svg = f"<!-- render failed: {e} -->"
        # nodes_meta: id → full metadata so client can hover/lookup
        meta = {str(n['id']): n for n in w['nodes']}
        rendered.append({
            'idx': w['idx'],
            'kind': w.get('kind', 'ft'),
            'namespace': w.get('namespace'),
            'n_cbs': w['n_cbs'],
            'n_edges': w['n_edges'],
            'n_data_edges': w.get('n_data_edges', w['n_edges']),
            'n_infra_edges': w.get('n_infra_edges', 0),
            'n_real_cbs': w.get('n_real_cbs', w['n_cbs']),
            'n_ext_sources': w.get('n_ext_sources', 0),
            'n_ext_sinks': w.get('n_ext_sinks', 0),
            'svg': svg,
            'nodes_meta': meta,
        })

    n_infra = sum(1 for e in edges if e.get('edge_class') == 'infra')
    body = json.dumps({
        'session_id': sid, 'scope': scope,
        'allowed_phases': sorted(allowed),
        'infra_mode': infra_mode,
        'node_filter': node_filter, 'topic_filter': topic_filter, 'ext_mode': ext_mode,
        'n_f_nodes': len(fnodes),
        'n_edges': len(edges),
        'n_data_edges': len(edges) - n_infra,
        'n_infra_edges': n_infra,
        'n_wccs': n_wccs,
        'wccs': rendered,
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

        # GPU-submitter detection: a cb_addr is marked as a GPU submitter if at
        # least one of its execution windows on the right vtid coincided with
        # cuda_runtime calls. Coarse-grained (per cb_addr, not per execution)
        # — keeps the response small and the rendering legible.
        gpu_submitter_cbs: list[str] = []
        try:
            cur.execute("""
                SELECT DISTINCT cb_addr FROM (
                  WITH events AS (
                    SELECT vpid AS pid, vtid, ts_ns, event,
                           payload->>'callback' AS cb_addr
                    FROM ros2_trace
                    WHERE session_id=%s
                      AND event IN ('ros2:callback_start','ros2:callback_end')
                  ),
                  paired AS (
                    SELECT vtid, cb_addr, ts_ns AS t0,
                           lead(ts_ns) OVER (PARTITION BY vtid ORDER BY ts_ns) AS t1,
                           lead(event) OVER (PARTITION BY vtid ORDER BY ts_ns) AS next_event,
                           event
                    FROM events
                  )
                  SELECT p.cb_addr, p.vtid, p.t0, p.t1
                  FROM paired p
                  WHERE p.event='ros2:callback_start' AND p.next_event='ros2:callback_end'
                ) p
                WHERE EXISTS (
                  SELECT 1 FROM cuda_runtime cr
                  WHERE cr.session_id = %s
                    AND cr.tid = p.vtid
                    AND cr.ts_ns >= p.t0 AND cr.ts_ns <= p.t1
                  LIMIT 1
                )
            """, (sid, sid))
            gpu_submitter_cbs = [r['cb_addr'] for r in cur.fetchall()]
        except Exception as e:
            sys.stderr.write(f"[fish-viz WARN] gpu_submitter detection failed: {e}\n")
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
        'gpu_submitter_cbs': gpu_submitter_cbs,
        'spans': [
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
        elif path in ('/gantt', '/gantt-tid', '/gantt.html', '/gantt-tid.html'):
            _serve_file(self, os.path.join(HERE, 'gantt.html'), 'text/html; charset=utf-8')
        elif path in ('/gantt-cb', '/gantt-cb.html'):
            _serve_file(self, os.path.join(HERE, 'gantt_cb.html'), 'text/html; charset=utf-8')
        elif path in ('/cb-stats', '/cb-stats.html'):
            _serve_file(self, os.path.join(HERE, 'cb_stats.html'), 'text/html; charset=utf-8')
        elif path in ('/wcc', '/wcc.html', '/wcc-view', '/wcc_view.html',
                      '/ft', '/ft.html', '/ft-view', '/ft_view.html'):
            # FT = Fish Task — user-facing renaming of WCC view. Both URLs
            # serve the same wcc_view.html (the page itself shows "FT" labels).
            _serve_file(self, os.path.join(HERE, 'wcc_view.html'), 'text/html; charset=utf-8')
        elif path == '/fish_graph.json':
            _serve_graph(self, qs)
        elif path == '/api/gantt':
            _serve_gantt_data(self, qs)
        elif path == '/api/causal':
            _serve_causal(self, qs)
        elif path == '/api/taskset':
            _serve_taskset(self, qs)
        elif path == '/api/cb-stats':
            _serve_cb_stats(self, qs)
        elif path == '/api/task-graphs':
            _serve_task_graphs(self, qs)
        elif path in ('/api/wcc', '/api/ft'):
            _serve_wcc(self, qs)
        elif path in ('/api/wcc-svg', '/api/ft-svg'):
            _serve_wcc_svg(self, qs)
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
