#!/usr/bin/env python3
"""Push examples/grafana_gapit/{panel.html,panel.css,panel.js} into a Grafana
dashboard's gapit-htmlgraphics-panel via REST API. Replaces the tedious
UI copy/paste workflow.

Usage:
  python3 scripts/push_gapit_panel.py <dashboard_uid> [--panel-id N]

Auth via env: GRAFANA_URL (default http://localhost:3000)
              GRAFANA_AUTH (default admin:superslinkyS1)
"""
import argparse, json, os, sys, urllib.request, base64

GRAFANA_URL = os.environ.get('GRAFANA_URL', 'http://localhost:3000')
GRAFANA_AUTH = os.environ.get('GRAFANA_AUTH', 'admin:superslinkyS1')
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(method, path, body=None):
    req = urllib.request.Request(GRAFANA_URL + path, method=method)
    req.add_header('Authorization', 'Basic ' + base64.b64encode(GRAFANA_AUTH.encode()).decode())
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(body).encode()
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('dashboard_uid')
    ap.add_argument('--panel-id', type=int, default=None,
                    help='Specific panel ID; otherwise first gapit panel found')
    ap.add_argument('--bundle-dir', default=os.path.join(HERE, 'examples', 'grafana_gapit'),
                    help='Dir holding panel.html / panel.css / panel.js')
    args = ap.parse_args()

    html = open(os.path.join(args.bundle_dir, 'panel.html')).read()
    css = open(os.path.join(args.bundle_dir, 'panel.css')).read()
    js  = open(os.path.join(args.bundle_dir, 'panel.js')).read()

    payload = request('GET', f'/api/dashboards/uid/{args.dashboard_uid}')
    dash = payload['dashboard']
    panels = dash.get('panels', [])

    target = None
    if args.panel_id is not None:
        target = next((p for p in panels if p.get('id') == args.panel_id), None)
    else:
        target = next((p for p in panels if p.get('type') == 'gapit-htmlgraphics-panel'), None)
    if target is None:
        sys.exit('No gapit-htmlgraphics-panel found in dashboard ' + args.dashboard_uid)

    opts = target.setdefault('options', {})
    opts['html'] = html
    opts['rootCSS'] = css
    opts['onRender'] = js
    print(f'  panel #{target.get("id")} "{target.get("title")}" updated:'
          f' html={len(html)}B  css={len(css)}B  js={len(js)}B')

    body = {'dashboard': dash, 'overwrite': True, 'message': 'push_gapit_panel.py: '
            f'sync html/css/js for panel {target.get("id")}'}
    resp = request('POST', '/api/dashboards/db', body)
    print(f'  saved: version {resp.get("version")} → {GRAFANA_URL}{resp.get("url", "")}')


if __name__ == '__main__':
    main()
