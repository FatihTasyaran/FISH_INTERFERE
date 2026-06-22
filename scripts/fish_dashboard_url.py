#!/usr/bin/env python3
"""Generate a Grafana dashboard URL for a FISH session with explicit time
range (min_event_ts - pad → max_event_ts + pad) and pre-set variables.

The explicit ?from=...&to=... params always override the dashboard's stored
time setting, so this URL is safe to share/bookmark regardless of what
range the dashboard was last viewed at.

Usage:
  python3 scripts/fish_dashboard_url.py <session_id>
                    [--scope __main__]
                    [--dashboard adq49gh]
                    [--pad-seconds 10]
                    [--grafana http://localhost:3000]
                    [--all-sessions]      # print URLs for every session in PG

Examples:
  python3 scripts/fish_dashboard_url.py dtdl_apriltag_20260616_172344
  python3 scripts/fish_dashboard_url.py --all-sessions
"""
import argparse
import os
import sys
import urllib.parse

import psycopg2

PG_DSN = os.environ.get(
    'FISH_PG_DSN',
    'host=localhost port=5432 dbname=fish user=fish password=fish',
)
GRAFANA = os.environ.get('GRAFANA_URL', 'http://localhost:3000')


def session_time_range(cur, session_id: str, pad_seconds: int):
    cur.execute(
        "SELECT min(ts_ns), max(ts_ns) FROM ros2_trace WHERE session_id = %s",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    pad_ns = pad_seconds * 1_000_000_000
    return (row[0] - pad_ns) // 1_000_000, (row[1] + pad_ns) // 1_000_000


def _ms_to_iso(ms: int) -> str:
    """epoch ms → '2026-06-16T17:23:47.998Z' (the format Grafana uses in absolute time URLs)."""
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{ms % 1000:03d}Z'


def build_url(grafana: str, dashboard_uid: str, session_id: str, scope: str,
              from_ms: int, to_ms: int, slug: str = '') -> str:
    # Use ISO 8601 UTC strings to match Grafana's native absolute-range URL format,
    # plus orgId + timezone (Grafana writes these on manual picks too).
    qs = urllib.parse.urlencode({
        'from': _ms_to_iso(from_ms),
        'to':   _ms_to_iso(to_ms),
        'var-session_id': session_id,
        'var-scope': scope,
        'orgId': 1,
        'timezone': 'browser',
    })
    path = f"/d/{dashboard_uid}"
    if slug:
        path += f"/{slug}"
    return f"{grafana.rstrip('/')}{path}?{qs}"


def fetch_slug(grafana: str, dashboard_uid: str, auth: str = 'admin:superslinkyS1') -> str:
    import urllib.request, json, base64
    req = urllib.request.Request(f"{grafana.rstrip('/')}/api/dashboards/uid/{dashboard_uid}")
    req.add_header('Authorization', 'Basic ' + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            d = json.loads(resp.read())
            return d.get('meta', {}).get('slug', '')
    except Exception:
        return ''


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('session_id', nargs='?', help='session_id (omit with --all-sessions)')
    ap.add_argument('--scope', default='__main__')
    ap.add_argument('--dashboard', default='adq49gh')
    ap.add_argument('--pad-seconds', type=int, default=10)
    ap.add_argument('--grafana', default=GRAFANA)
    ap.add_argument('--all-sessions', action='store_true',
                    help='List URLs for every session that has ros2_trace data')
    args = ap.parse_args()

    if not args.session_id and not args.all_sessions:
        ap.error('provide session_id, or use --all-sessions')

    slug = fetch_slug(args.grafana, args.dashboard)
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    if args.all_sessions:
        cur.execute(
            "SELECT DISTINCT session_id FROM ros2_trace ORDER BY session_id"
        )
        sessions = [r[0] for r in cur.fetchall()]
    else:
        sessions = [args.session_id]

    for sid in sessions:
        tr = session_time_range(cur, sid, args.pad_seconds)
        if tr is None:
            sys.stderr.write(f'  {sid}: no ros2_trace data, skip\n')
            continue
        from_ms, to_ms = tr
        url = build_url(args.grafana, args.dashboard, sid, args.scope, from_ms, to_ms, slug)
        if args.all_sessions:
            print(f'{sid:50s}  {url}')
        else:
            print(url)

    conn.close()


if __name__ == '__main__':
    main()
