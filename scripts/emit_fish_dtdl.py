#!/usr/bin/env python3
"""Emit an instance-level FISH DTDL JSON-LD document from a fish_graph.json
+ session directory.

Each vertex in the FISH model graph (and each unique GPU kernel / CUDA stream
observed in the session's InfluxDB data) becomes an instance Interface that
`extends` its corresponding template (see fish_interfere/schemas/) and fills
in concrete Property values. The emitted document is a single JSON-LD array
suitable for ingestion by the on-demand panel builder.

Usage:
  python3 scripts/emit_fish_dtdl.py <session_dir>
      [--out <path>]
      [--graph <fish_graph.json>]
      [--influx-session <name>]       # override session tag (defaults to dir basename)
      [--no-gpu]                      # skip InfluxDB queries entirely

Defaults:
  session_dir: a directory whose basename is the session_id (e.g. fish_20260609_034512)
  --graph:    <session_dir>/fish_graph.json  if present, else --graph required
  --out:      examples/<session_id>.jsonld
  --influx-session: defaults to session_dir basename

Emits instances for: Host, Session, Executor, Node, Entity, Callback (from
fish_graph.json) + GPU_Kernel, CUDA_Stream (from shared `fish` InfluxDB).
"""
import argparse
import json
import os
import re
import socket
import sys

INFLUX_HOST = 'http://localhost:8181'
INFLUX_TOKEN_FILE = os.path.expanduser('~/inf.tok')


def load_influx_token():
    """Read the actual token value out of ~/inf.tok which has 'Token: <value>' format."""
    if not os.path.isfile(INFLUX_TOKEN_FILE):
        return None
    with open(INFLUX_TOKEN_FILE) as f:
        for line in f:
            m = re.match(r'^\s*Token:\s*(\S+)', line)
            if m:
                return m.group(1)
    return None


SCHEMAS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schemas')


def slugify(s):
    """Make a string safe for use inside a dtmi path segment."""
    out = []
    for c in s:
        if c.isalnum():
            out.append(c)
        elif c in '/-_':
            out.append('_')
    slug = ''.join(out).strip('_')
    return slug or 'unnamed'


def _addr_slug(addr):
    """Normalize a hex pointer string."""
    s = str(addr).lower()
    if s.startswith('0x'):
        s = s[2:]
    return s.lstrip('0') or '0'


def load_graph(path):
    with open(path) as f:
        g = json.load(f)
    by_id = {n['id']: n for n in g.get('nodes', [])}
    return g, by_id


def emit_host(session_id):
    hostname = socket.gethostname()
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:host:{slugify(hostname)};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:host;1',
        'displayName': f'Host {hostname}',
        'properties': {
            'hostname': hostname,
        },
        'relationships': {
            'has_session': [f'dtmi:fish:session:{session_id};1'],
        },
    }


def emit_session(session_id, session_dir):
    sess = {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:session:{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:session;1',
        'displayName': f'Session {session_id}',
        'properties': {
            'session_id': session_id,
            'session_dir': session_dir,
        },
        'relationships': {
            'has_executor': [],
            'runs_container': [],
        },
    }
    return sess


def emit_executor(ex_node, session_id):
    pid = ex_node.get('pid', 0)
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:executor:pid_{pid}__sess_{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:executor;1',
        'displayName': ex_node.get('label', f'EX pid={pid}'),
        'properties': {
            'pid': pid,
            'session_id': session_id,
        },
        'relationships': {
            'has_node': [],
        },
        '_graph_id': ex_node['id'],
    }


def emit_node(n_node, session_id):
    full = n_node.get('full_name') or n_node.get('label', f"node_{n_node['id']}")
    slug = slugify(full)
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:node:{slug}__sess_{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:node;1',
        'displayName': full,
        'properties': {
            'full_name': full,
            'session_id': session_id,
        },
        'relationships': {
            'has_entity': [],
        },
        '_graph_id': n_node['id'],
    }


def emit_entity(e_node, parent_node_full_name, session_id):
    label = e_node.get('label', f"entity_{e_node['id']}")
    etype = e_node.get('etype', 'unknown')
    slug = slugify(f"{parent_node_full_name}__{label}__{etype}")
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:entity:{slug}__sess_{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:entity;1',
        'displayName': f'{etype}: {label}',
        'properties': {
            'topic_or_name': label,
            'entity_kind': etype,
            'session_id': session_id,
        },
        'relationships': {
            'has_callback': [],
        },
        '_graph_id': e_node['id'],
    }


def emit_callback(f_node, session_id):
    cb_addr = f_node.get('cb_addr') or f"id_{f_node['id']}"
    sym = f_node.get('label', '')
    ptype = f_node.get('ptype', 'cpu')
    slug = _addr_slug(cb_addr)
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:callback:{slug}__sess_{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:callback;1',
        'displayName': sym[:80] + ('…' if len(sym) > 80 else ''),
        'properties': {
            'cb_addr': cb_addr,
            'symbol_demangled': sym,
            'language': 'python' if 'rclpy' in sym.lower() else 'cpp',
            'session_id': session_id,
        },
        'relationships': {
            'launches': [],
        },
        '_graph_id': f_node['id'],
        '_ptype': ptype,
    }


def emit_gpu_kernel(row, session_id):
    name = row.get('kernel_name') or row.get('kernel_short_name') or 'unknown_kernel'
    slug = slugify(name)[:80]  # cap pathlen
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:gpu_kernel:{slug}__sess_{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:gpu_kernel;1',
        'displayName': name[:80] + ('…' if len(name) > 80 else ''),
        'properties': {
            'kernel_name': name,
            'kernel_signature': row.get('kernel_name', name),
            'grid_dim': f"({row.get('gridX',0)},{row.get('gridY',0)},{row.get('gridZ',0)})",
            'block_dim': f"({row.get('blockX',0)},{row.get('blockY',0)},{row.get('blockZ',0)})",
            'registers_per_thread': int(row.get('registersPerThread', 0) or 0),
            'static_smem_bytes': int(row.get('staticSharedMemory', 0) or 0),
            'dynamic_smem_bytes': int(row.get('dynamicSharedMemory', 0) or 0),
            'session_id': session_id,
        },
        'relationships': {
            'on_stream': [],
        },
        '_streamIds': set(),
    }


def emit_cuda_stream(stream_id, owner_pid, session_id):
    s = str(stream_id)
    slug = slugify(s)
    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        '@id': f'dtmi:fish:cuda_stream:{slug}__sess_{session_id};1',
        '@type': 'Interface',
        'extends': 'dtmi:fish:cuda_stream;1',
        'displayName': f'CUstream {s}',
        'properties': {
            'stream_handle': s,
            'owner_pid': int(owner_pid) if owner_pid is not None else 0,
            'session_id': session_id,
        },
        'relationships': {},
    }


def query_influx(token, db, sql):
    """Run an InfluxDB3 SQL query via the CLI. Returns parsed rows (list of dicts).

    Uses the influxdb3 CLI in JSON output mode so we don't need a Python client
    on PATH. Robust to empty results / connection refused (returns []).
    """
    import subprocess
    cmd = [os.path.expanduser('~/.influxdb/influxdb3'), 'query',
           '--host', INFLUX_HOST, '--token', token, '--database', db,
           '--format', 'json', sql]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"  influxdb query failed: {e}\n")
        return []
    if res.returncode != 0:
        sys.stderr.write(f"  influxdb query rc={res.returncode}: {res.stderr.strip()[:300]}\n")
        return []
    out = res.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        sys.stderr.write(f"  influxdb output not JSON: {out[:200]}\n")
        return []


def collect_gpu_instances(token, influx_session, session_id):
    """Return (kernel_instances, stream_instances).

    kernel_instances:  one per unique kernel_name in session
    stream_instances:  one per unique (streamId, globalPid) in session
    Wires gpu_kernel.on_stream → cuda_stream via streamId.
    """
    if not token:
        sys.stderr.write("  no influx token — skipping GPU layer\n")
        return [], []

    # Unique kernels with representative metadata (first occurrence).
    # NB: InfluxDB 3 lowercases unquoted identifiers — column names with capitals
    # MUST be double-quoted in SQL.
    kernel_rows = query_influx(token, 'fish', f"""
        SELECT kernel_name, kernel_short_name,
               MIN("gridX") AS "gridX", MIN("gridY") AS "gridY", MIN("gridZ") AS "gridZ",
               MIN("blockX") AS "blockX", MIN("blockY") AS "blockY", MIN("blockZ") AS "blockZ",
               MIN("registersPerThread") AS "registersPerThread",
               MIN("staticSharedMemory") AS "staticSharedMemory",
               MIN("dynamicSharedMemory") AS "dynamicSharedMemory"
        FROM gpu_kernels
        WHERE session = '{influx_session}'
        GROUP BY kernel_name, kernel_short_name
    """)
    sys.stderr.write(f"  fetched {len(kernel_rows)} unique kernels for session '{influx_session}'\n")

    kernels_by_name = {}
    for row in kernel_rows:
        kinst = emit_gpu_kernel(row, session_id)
        kernels_by_name[row.get('kernel_name', '')] = kinst

    # Kernel ↔ stream mapping
    kstream_rows = query_influx(token, 'fish', f"""
        SELECT DISTINCT kernel_name, "streamId"
        FROM gpu_kernels
        WHERE session = '{influx_session}'
    """)
    stream_owner = {}  # streamId → pid (best effort)
    streams_by_id = {}
    for row in kstream_rows:
        sid = row.get('streamId')
        kname = row.get('kernel_name', '')
        if sid is None:
            continue
        if sid not in streams_by_id:
            streams_by_id[sid] = emit_cuda_stream(sid, stream_owner.get(sid), session_id)
        kinst = kernels_by_name.get(kname)
        if kinst is not None:
            kinst['_streamIds'].add(sid)

    # Stream → pid mapping
    pid_rows = query_influx(token, 'fish', f"""
        SELECT DISTINCT "streamId", "globalPid"
        FROM gpu_kernels
        WHERE session = '{influx_session}'
    """)
    for row in pid_rows:
        sid = row.get('streamId')
        pid = row.get('globalPid')
        if sid in streams_by_id and pid is not None:
            # nsys CUPTI globalPid layout (verified empirically against a2_apriltag run):
            #   bits 40+ : host_id, bits 24..39 : OS pid, bits 0..23 : padding
            # So the actual OS pid is (globalPid >> 24) & 0xFFFFFF.
            try:
                os_pid = (int(pid) >> 24) & 0xFFFFFF
            except (TypeError, ValueError):
                os_pid = pid
            streams_by_id[sid]['properties']['owner_pid'] = os_pid
            streams_by_id[sid]['properties']['global_pid_raw'] = int(pid) if isinstance(pid, (int, str)) else pid

    # Materialize kernel→stream relationship from accumulated _streamIds
    for kinst in kernels_by_name.values():
        kinst['relationships']['on_stream'] = [
            streams_by_id[sid]['@id'] for sid in sorted(kinst.pop('_streamIds')) if sid in streams_by_id
        ]

    return list(kernels_by_name.values()), list(streams_by_id.values())


def build_instance_doc(graph, by_id, session_id, session_dir,
                       influx_session=None, fetch_gpu=True):
    instances = []
    host = emit_host(session_id)
    instances.append(host)
    session = emit_session(session_id, session_dir)
    instances.append(session)

    # Index by graph id so we can wire relationships
    by_graph_id = {}

    # EX layer
    for n in graph.get('nodes', []):
        if n.get('type') != 'EX':
            continue
        ex = emit_executor(n, session_id)
        instances.append(ex)
        by_graph_id[n['id']] = ex
        session['relationships']['has_executor'].append(ex['@id'])

    # N layer
    for n in graph.get('nodes', []):
        if n.get('type') != 'N':
            continue
        node = emit_node(n, session_id)
        instances.append(node)
        by_graph_id[n['id']] = node

    # Wire EX → N via children
    for n in graph.get('nodes', []):
        if n.get('type') != 'EX':
            continue
        for cid in n.get('children', []):
            child = by_id.get(cid)
            if child and child.get('type') == 'N':
                ex_inst = by_graph_id[n['id']]
                n_inst = by_graph_id[cid]
                ex_inst['relationships']['has_node'].append(n_inst['@id'])

    # E layer
    for n in graph.get('nodes', []):
        if n.get('type') != 'E':
            continue
        # Find parent N (the N whose children include this E.id)
        parent_full = None
        for candidate in graph.get('nodes', []):
            if candidate.get('type') == 'N' and n['id'] in candidate.get('children', []):
                parent_full = candidate.get('full_name') or candidate.get('label', '')
                break
        e_inst = emit_entity(n, parent_full or 'unknown', session_id)
        instances.append(e_inst)
        by_graph_id[n['id']] = e_inst
        # Wire N → E
        if parent_full is not None:
            for candidate in graph.get('nodes', []):
                if candidate.get('type') == 'N' and n['id'] in candidate.get('children', []):
                    node_inst = by_graph_id.get(candidate['id'])
                    if node_inst is not None:
                        node_inst['relationships']['has_entity'].append(e_inst['@id'])
                    break

    # F layer
    for n in graph.get('nodes', []):
        if n.get('type') != 'F':
            continue
        cb_inst = emit_callback(n, session_id)
        instances.append(cb_inst)
        by_graph_id[n['id']] = cb_inst
        # Wire E → F
        for candidate in graph.get('nodes', []):
            if candidate.get('type') == 'E' and n['id'] in candidate.get('children', []):
                e_inst = by_graph_id.get(candidate['id'])
                if e_inst is not None:
                    e_inst['relationships']['has_callback'].append(cb_inst['@id'])
                break

    # GPU layer (queries InfluxDB shared `fish` db)
    if fetch_gpu:
        token = load_influx_token()
        kernels, streams = collect_gpu_instances(
            token, influx_session or session_id, session_id)
        # Wire executor → owns_stream via owner_pid
        ex_by_pid = {ex['properties'].get('pid'): ex for ex in instances if 'executor;1' in ex.get('extends','')}
        for sinst in streams:
            owner_pid = sinst['properties'].get('owner_pid')
            ex = ex_by_pid.get(owner_pid)
            if ex is not None:
                ex['relationships'].setdefault('owns_stream', []).append(sinst['@id'])
        instances.extend(streams)
        instances.extend(kernels)

    return {
        '@context': ['dtmi:dtdl:context;3', 'dtmi:fish:context;1'],
        'session_id': session_id,
        'influx_session': influx_session or session_id,
        'emitted_from': session_dir,
        'instance_count': len(instances),
        'instances': instances,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('session_dir', help='Session directory whose basename is the session_id')
    ap.add_argument('--graph', help='Path to fish_graph.json (default: <session_dir>/fish_graph.json)')
    ap.add_argument('--out', help='Output JSON-LD path (default: examples/<session_id>.jsonld)')
    ap.add_argument('--influx-session', help='InfluxDB session tag value (default: session_dir basename)')
    ap.add_argument('--no-gpu', action='store_true', help='Skip GPU/InfluxDB queries')
    args = ap.parse_args()

    session_dir = os.path.abspath(args.session_dir)
    session_id = os.path.basename(session_dir.rstrip('/'))
    graph_path = args.graph or os.path.join(session_dir, 'fish_graph.json')
    if not os.path.isfile(graph_path):
        sys.exit(f"fish_graph.json not found: {graph_path}")

    graph, by_id = load_graph(graph_path)
    doc = build_instance_doc(graph, by_id, session_id, session_dir,
                              influx_session=args.influx_session,
                              fetch_gpu=not args.no_gpu)

    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'examples', f'{session_id}.jsonld')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(doc, f, indent=2)

    by_type = {}
    for inst in doc['instances']:
        ext = inst.get('extends', '?')
        by_type[ext] = by_type.get(ext, 0) + 1
    print(f"wrote {out}")
    print(f"  {doc['instance_count']} instances")
    for t, c in sorted(by_type.items()):
        print(f"    {c:4d} × {t}")


if __name__ == '__main__':
    main()
