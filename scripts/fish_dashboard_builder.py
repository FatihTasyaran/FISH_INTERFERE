#!/usr/bin/env python3
"""FISH on-demand dashboard builder.

Given a DTDL JSON-LD instance document (output of emit_fish_dtdl.py) and a
target root @id, generate a Grafana dashboard automatically by:
  1. Walking the instance graph (focus / subtree / level view).
  2. Looking up each instance's Telemetry entries from its `extends` template
     (schemas/<type>.jsonld).
  3. Resolving `{placeholder}` tokens in fish:tagSelector against each
     instance's Properties.
  4. Building Grafana panel JSON for each resolved Telemetry (using the
     viz hint from fish:recommendedViz).
  5. Laying panels out in a grid.
  6. Optionally uploading the dashboard to Grafana via REST API.

Discovery commands:
  --list-instances [--type callback|entity|...]    just dump @ids + properties

Build commands:
  --root <@id> --view focus|subtree|level [--depth N] [--max-panels N]
  --upload     POST the dashboard to Grafana (default: print JSON to stdout)

Defaults (override via env or CLI):
  GRAFANA_URL          http://localhost:3000
  GRAFANA_AUTH         admin:admin                (HTTP Basic for /api/datasources, /api/dashboards/db)
  FISH_DS_INFLUX_NAME  FISH-InfluxDB              (Grafana datasource name for InfluxDB v3)
  FISH_DS_MONGO_NAME   FISH-MongoDB-Shim          (Grafana datasource name for the Infinity → shim)
  FISH_SHIM_URL        http://localhost:8189      (used as Infinity datasource base URL)
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error

SCHEMAS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schemas')


# ─── Template loader ────────────────────────────────────────────────────────

def load_templates():
    """Read schemas/*.jsonld into a dict: dtmi → Interface JSON."""
    out = {}
    for fname in os.listdir(SCHEMAS_DIR):
        if not fname.endswith('.jsonld'):
            continue
        if fname == 'fish_context.jsonld':
            continue
        path = os.path.join(SCHEMAS_DIR, fname)
        with open(path) as f:
            d = json.load(f)
        tid = d.get('@id')
        if tid:
            out[tid] = d
    return out


def telemetry_entries(template):
    return [c for c in template.get('contents', [])
            if isinstance(c.get('@type'), list) and 'Telemetry' in c['@type']
            or c.get('@type') == 'Telemetry']


# ─── Instance graph helpers ─────────────────────────────────────────────────

def load_instance_doc(path):
    with open(path) as f:
        return json.load(f)


def index_instances(doc):
    return {i['@id']: i for i in doc.get('instances', [])}


def chase_session_id(inst):
    return (inst.get('properties') or {}).get('session_id')


def collect_focus(root_id, by_id):
    if root_id not in by_id:
        raise SystemExit(f"root @id not found in instance doc: {root_id}")
    return [by_id[root_id]]


def collect_subtree(root_id, by_id, depth=None):
    if root_id not in by_id:
        raise SystemExit(f"root @id not found in instance doc: {root_id}")
    seen, frontier, out = {root_id}, [(by_id[root_id], 0)], []
    while frontier:
        inst, d = frontier.pop(0)
        out.append(inst)
        if depth is not None and d >= depth:
            continue
        for _name, targets in (inst.get('relationships') or {}).items():
            for t in targets:
                if t in by_id and t not in seen:
                    seen.add(t)
                    frontier.append((by_id[t], d + 1))
    return out


def collect_level(root_id, by_id):
    root = by_id.get(root_id)
    if not root:
        raise SystemExit(f"root @id not found: {root_id}")
    target_template = root.get('extends')
    return [i for i in by_id.values() if i.get('extends') == target_template]


# ─── Resolver ───────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_.]*)\}')


def resolve_placeholders(value, properties):
    """Replace {key} occurrences in `value` with properties[key]. Returns None
    if any placeholder doesn't have a matching property.
    """
    if not isinstance(value, str):
        return value
    missing = []
    def _sub(m):
        k = m.group(1)
        if k in properties:
            return str(properties[k])
        missing.append(k)
        return m.group(0)
    out = _PLACEHOLDER_RE.sub(_sub, value)
    return None if missing else out


def resolve_tag_selector(tag_selector, properties):
    """Apply property expansion to each value in a tagSelector dict."""
    resolved = {}
    for k, v in (tag_selector or {}).items():
        rv = resolve_placeholders(v, properties)
        if rv is None:
            return None  # missing property — caller should skip this telemetry
        resolved[k] = rv
    return resolved


# ─── Query builders (per store) ─────────────────────────────────────────────

def build_influxdb_sql(measurement, field_name, tags, dbname):
    """SELECT $field FROM $measurement WHERE $tags. Time filter added by
    Grafana via $__timeFilter macro.
    """
    where = ' AND '.join([f'"{k}" = \'{v}\'' for k, v in tags.items()])
    if where:
        where += ' AND '
    return f'SELECT time, "{field_name}" FROM "{measurement}" WHERE {where}$__timeFilter(time) ORDER BY time'


def build_shim_body(tele_name, properties, tag_selector, collection, field_name, dbname, projection_hint=None):
    """Most telemetries from MongoDB-backed templates can be served by /query
    in 'timeseries' mode. Two special cases get convenience endpoints.
    """
    session = properties.get('session_id', dbname.replace('{session_id}', '') or '')
    # Special endpoints
    if tele_name == 'callback_duration_ns':
        cb = properties.get('cb_addr', '')
        return {'endpoint': '/callback/durations',
                'body': {'session': session, 'cb_addr': cb}}
    if tele_name == 'spin_events':
        pid = properties.get('pid', '')
        return {'endpoint': '/executor/spin',
                'body': {'session': session, 'pid': pid}}
    # Generic /query
    body = {
        'session': session,
        'collection': collection,
        'filter': dict(tag_selector or {}),
        'projection': {'ts': 1, field_name: 1} if field_name else {'ts': 1},
        'format': 'timeseries',
        'value_field': field_name,
        'limit': 50000,
    }
    return {'endpoint': '/query', 'body': body}


# ─── Panel factory ──────────────────────────────────────────────────────────

def grafana_panel_skel(panel_id, title, gx, gy, gw, gh):
    return {
        'id': panel_id,
        'gridPos': {'x': gx, 'y': gy, 'w': gw, 'h': gh},
        'title': title,
        'datasource': None,           # filled per target
        'targets': [],
        'fieldConfig': {'defaults': {}, 'overrides': []},
        'options': {},
    }


def influx_target(uid, refid, sql):
    return {
        'datasource': {'type': 'influxdb', 'uid': uid},
        'refId': refid,
        'rawQuery': True,
        'query': sql,
        'resultFormat': 'time_series',
    }


def infinity_target(uid, refid, base_url, endpoint, body):
    return {
        'datasource': {'type': 'yesoreyeram-infinity-datasource', 'uid': uid},
        'refId': refid,
        'type': 'json',
        'source': 'url',
        'url': f'{base_url.rstrip("/")}{endpoint}',
        'url_options': {
            'method': 'POST',
            'body_content_type': 'application/json',
            'body_type': 'raw',
            'data': json.dumps(body),
        },
        'parser': 'backend',
        'format': 'timeseries',
        'root_selector': 'datapoints',
        'columns': [
            {'selector': '0', 'text': 'value', 'type': 'number'},
            {'selector': '1', 'text': 'time',  'type': 'timestamp'},
        ],
    }


VIZ_TO_PANEL_TYPE = {
    'timeseries': 'timeseries',
    'histogram':  'histogram',
    'stat':       'stat',
    'timeline':   'state-timeline',
    'heatmap':    'heatmap',
    'bargauge':   'bargauge',
}


def make_panel(panel_id, title, gx, gy, gw, gh, viz, target, unit=None):
    p = grafana_panel_skel(panel_id, title, gx, gy, gw, gh)
    p['type'] = VIZ_TO_PANEL_TYPE.get(viz, 'timeseries')
    p['datasource'] = target['datasource']
    p['targets'] = [target]
    if unit:
        p['fieldConfig']['defaults']['unit'] = {
            'nanosecond': 'ns', 'microsecond': 'µs', 'millisecond': 'ms',
            'percent': 'percent', 'byte': 'bytes', 'kibibyte': 'kbytes',
            'mebibyte': 'mbytes', 'hertz': 'hertz', 'count': 'short',
        }.get(unit, unit)
    return p


# ─── Grafana API helpers ────────────────────────────────────────────────────

def grafana_request(method, url, auth, body=None):
    req = urllib.request.Request(url, method=method)
    if auth:
        token = base64.b64encode(auth.encode()).decode()
        req.add_header('Authorization', f'Basic {token}')
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Grafana API {method} {url} → {e.code}: {e.read().decode()[:300]}')


def discover_datasources(grafana_url, auth):
    """Return {name: uid} for all configured datasources."""
    rows = grafana_request('GET', f'{grafana_url}/api/datasources', auth)
    return {d['name']: d['uid'] for d in rows}


# ─── Driver ─────────────────────────────────────────────────────────────────

def build_dashboard(doc, root_id, view, depth, max_panels, ds_uids, shim_url):
    by_id = index_instances(doc)
    templates = load_templates()

    if view == 'focus':
        instances = collect_focus(root_id, by_id)
    elif view == 'subtree':
        instances = collect_subtree(root_id, by_id, depth=depth)
    elif view == 'level':
        instances = collect_level(root_id, by_id)
    else:
        raise SystemExit(f"unknown view: {view}")

    sys.stderr.write(f"[builder] view={view} root={root_id} → {len(instances)} instances\n")

    panels = []
    pid = 1
    col = 0
    row = 0
    GW, GH = 12, 8
    DASH_W = 24
    skipped = []
    refid_idx = 0

    for inst in instances:
        tmpl_id = inst.get('extends')
        tmpl = templates.get(tmpl_id)
        if tmpl is None:
            skipped.append((inst['@id'], 'no template'))
            continue
        for tele in telemetry_entries(tmpl):
            if len(panels) >= max_panels:
                break
            tele_name = tele.get('name', '?')
            viz = tele.get('fish:recommendedViz', 'timeseries')
            store = tele.get('fish:store', 'influxdb')
            unit = tele.get('fish:unit')
            tag_sel = resolve_tag_selector(tele.get('fish:tagSelector'), inst.get('properties') or {})
            if tag_sel is None:
                skipped.append((inst['@id'] + ':' + tele_name, 'unresolved placeholder'))
                continue

            refid = chr(ord('A') + (refid_idx % 26)); refid_idx += 1

            if store == 'influxdb':
                uid = ds_uids.get('influxdb')
                if not uid:
                    skipped.append((tele_name, 'no influxdb datasource'))
                    continue
                sql = build_influxdb_sql(
                    tele.get('fish:measurement', ''),
                    tele.get('fish:fieldName', 'value'),
                    tag_sel,
                    tele.get('fish:dbName', 'fish'),
                )
                target = influx_target(uid, refid, sql)
            elif store == 'mongodb':
                uid = ds_uids.get('mongodb_shim')
                if not uid:
                    skipped.append((tele_name, 'no shim datasource'))
                    continue
                qb = build_shim_body(
                    tele_name,
                    inst.get('properties') or {},
                    tag_sel,
                    tele.get('fish:collection', ''),
                    tele.get('fish:fieldName', ''),
                    tele.get('fish:dbName', '{session_id}'),
                )
                target = infinity_target(uid, refid, shim_url, qb['endpoint'], qb['body'])
            else:
                skipped.append((tele_name, f'unknown store: {store}'))
                continue

            title = f"{tele_name}  [{inst.get('displayName', inst['@id'])[:50]}]"
            panels.append(make_panel(pid, title, col, row, GW, GH, viz, target, unit))
            pid += 1
            col += GW
            if col >= DASH_W:
                col = 0
                row += GH
        if len(panels) >= max_panels:
            break

    title = f"FISH {view} — {by_id[root_id].get('displayName', root_id)[:80]}"
    uid_seed = f'fish-{view}-{root_id.replace(":", "-").replace(";", "-")[:30]}'
    dashboard = {
        'dashboard': {
            'uid': re.sub(r'[^a-zA-Z0-9_-]', '', uid_seed)[:40],
            'title': title,
            'tags': ['fish', 'auto-generated', f'fish:view:{view}'],
            'schemaVersion': 39,
            'time': {'from': 'now-1h', 'to': 'now'},
            'panels': panels,
        },
        'overwrite': True,
    }
    return dashboard, skipped


def list_instances(doc, by_type=None):
    type_short = {}
    for i in doc.get('instances', []):
        ext = i.get('extends', '?').replace('dtmi:fish:', '').replace(';1', '')
        if by_type and ext != by_type:
            continue
        props = i.get('properties') or {}
        key_field = (props.get('full_name') or props.get('cb_addr')
                     or props.get('topic_or_name') or props.get('kernel_name')
                     or props.get('session_id') or props.get('hostname') or '')
        print(f"  {ext:<12s}  {i['@id']:<70s}  {key_field}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('jsonld', help='Path to DTDL instance JSON-LD (from emit_fish_dtdl.py)')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--list-instances', action='store_true', help='List @ids + types and exit')
    g.add_argument('--root', help='Root instance @id for the dashboard')
    ap.add_argument('--type', help='Filter --list-instances to one template type (e.g. callback)')
    ap.add_argument('--view', default='focus', choices=['focus', 'subtree', 'level'])
    ap.add_argument('--depth', type=int, default=None, help='Max relationship hops for subtree view')
    ap.add_argument('--max-panels', type=int, default=40)
    ap.add_argument('--upload', action='store_true', help='POST dashboard to Grafana')
    ap.add_argument('--out', help='Write dashboard JSON to this path (default: stdout)')
    args = ap.parse_args()

    doc = load_instance_doc(args.jsonld)

    if args.list_instances:
        list_instances(doc, by_type=args.type)
        return

    grafana_url = os.environ.get('GRAFANA_URL', 'http://localhost:3000')
    grafana_auth = os.environ.get('GRAFANA_AUTH', 'admin:admin')
    ds_influx = os.environ.get('FISH_DS_INFLUX_NAME', 'FISH-InfluxDB')
    ds_mongo = os.environ.get('FISH_DS_MONGO_NAME', 'FISH-MongoDB-Shim')
    shim_url = os.environ.get('FISH_SHIM_URL', 'http://localhost:8189')

    ds_uids = {}
    try:
        names = discover_datasources(grafana_url, grafana_auth)
        ds_uids['influxdb']     = names.get(ds_influx)
        ds_uids['mongodb_shim'] = names.get(ds_mongo)
        sys.stderr.write(f"[builder] datasources: influxdb={ds_uids['influxdb']!r}  mongo_shim={ds_uids['mongodb_shim']!r}\n")
    except Exception as e:
        sys.stderr.write(f"[builder] WARNING: could not discover Grafana datasources ({e}). Targets will reference uids by name.\n")
        ds_uids['influxdb']     = ds_influx
        ds_uids['mongodb_shim'] = ds_mongo

    dashboard, skipped = build_dashboard(doc, args.root, args.view, args.depth, args.max_panels, ds_uids, shim_url)

    sys.stderr.write(f"[builder] generated {len(dashboard['dashboard']['panels'])} panels  ({len(skipped)} skipped)\n")
    for inst_id, reason in skipped[:10]:
        sys.stderr.write(f"   skip: {inst_id} — {reason}\n")
    if len(skipped) > 10:
        sys.stderr.write(f"   …+{len(skipped) - 10} more\n")

    out_dest = sys.stdout
    if args.out:
        out_dest = open(args.out, 'w')
    json.dump(dashboard, out_dest, indent=2)
    if args.out:
        out_dest.close()
        sys.stderr.write(f"[builder] wrote {args.out}\n")
    else:
        sys.stdout.write('\n')

    if args.upload:
        url = f'{grafana_url}/api/dashboards/db'
        resp = grafana_request('POST', url, grafana_auth, dashboard)
        dash_url = f"{grafana_url}{resp.get('url', '/')}"
        sys.stderr.write(f"[builder] uploaded: {dash_url}\n")


if __name__ == '__main__':
    main()
