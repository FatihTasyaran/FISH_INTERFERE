#!/usr/bin/env python3
"""per-bench-routine-1 postprocess.

For each benchmark dir under /tmp/per_bench_routine_1/<name>/fishon/:
  1. Ingest the LTTng trace via fish_compose.ingest_session.
  2. Build the graph via extract_container_model.
  3. Walk the graph from each EX root → collect summary stats.
  4. Write <name>/pbr1_summary.json with:
     - fishoff_perf  : Isaac ROS BasicPerformanceMetrics from fishoff/r2b-log-*.json
     - fishon_perf   : same from fishon/r2b-log-*.json
     - overhead      : per-metric delta (on - off) and pct
     - graph_summary : per-EX node counts (EX/N/E/F/aspect)
     - per_node      : for each Node (full_name → child E/F counts + sub/pub/cli topics)

Reads the fishon trace dir directly; no DB cleanup beyond that.

Usage:
  python3 pbr1_postprocess.py [<bench-name> ...]
  python3 pbr1_postprocess.py --all            # all dirs in /tmp/per_bench_routine_1
"""
import os, sys, glob, json, traceback
sys.path.insert(0, '/home/tue037807/fish_interfere/postprocess')

import fish_compose
import graph_store

DEST_BASE = '/tmp/per_bench_routine_1'

def find_session(bench_dir):
    cands = sorted(glob.glob(os.path.join(bench_dir, 'fishon', 'fish_*')))
    return cands[-1] if cands else None

def find_perf_json(pass_dir):
    cands = sorted(glob.glob(os.path.join(pass_dir, 'r2b-log-*.json')))
    if not cands:
        return None, None
    with open(cands[-1]) as f:
        data = json.load(f)
    return cands[-1], data

def summarize_graph_from_json(path):
    with open(path) as f:
        d = json.load(f)
    nodes_by_id = {n['id']: n for n in d.get('nodes', [])}
    by_type = {}
    for n in d.get('nodes', []):
        t = n.get('type', '?')
        by_type[t] = by_type.get(t, 0) + 1
    nodes_info = {}
    # For each N → its child E vertices → their child F vertices.
    for n in d.get('nodes', []):
        if n.get('type') != 'N': continue
        full = n.get('full_name') or n.get('label') or f"node_{n.get('id')}"
        entry = nodes_info.setdefault(full, {
            'E': 0, 'F': 0,
            'sub_topics': [], 'pub_topics': [],
            'srv_names': [], 'cli_names': [],
            'tmr_periods': [], 'pub_aspect_topics': [],
            'cli_aspect_names': [],
        })
        for cid in n.get('children', []):
            cv = nodes_by_id.get(cid)
            if cv is None: continue
            ct = cv.get('type')
            if ct != 'E':
                continue
            entry['E'] += 1
            etype = cv.get('etype', '?')
            label = cv.get('label', '?')
            if etype == 'sub':
                entry['sub_topics'].append(label)
            elif etype == 'serv':
                entry['srv_names'].append(label)
            elif etype == 'tmr':
                entry['tmr_periods'].append(str(cv.get('period_ns', label)))
            elif etype == 'cli':
                entry['cli_names'].append(label)
            else:
                entry.setdefault('other_E', []).append(f"etype={etype}, label={label}")
            # Walk this E's F children
            for fid in cv.get('children', []):
                fv = nodes_by_id.get(fid)
                if fv is None or fv.get('type') != 'F': continue
                entry['F'] += 1
                # F may carry pub_aspect / cli_aspect annotations
                for asp in fv.get('aspects', []) or []:
                    a = asp.get('aspect')
                    if a == 'pub':
                        entry['pub_aspect_topics'].append(asp.get('topic') or asp.get('service') or '?')
                    elif a == 'cli':
                        entry['cli_aspect_names'].append(asp.get('service') or asp.get('topic') or '?')
                # Also collect from E's own aspects array (some pubs land here)
            for asp in cv.get('aspects', []) or []:
                a = asp.get('aspect')
                if a == 'pub' and etype not in ('serv', 'cli'):
                    entry['pub_aspect_topics'].append(asp.get('topic') or asp.get('service') or '?')
    # Dedup pub aspects
    for entry in nodes_info.values():
        entry['pub_aspect_topics'] = sorted(set(entry['pub_aspect_topics']))
        entry['cli_aspect_names'] = sorted(set(entry['cli_aspect_names']))
    return {'by_type': by_type, 'nodes': nodes_info}

def overhead_table(off, on):
    if not off or not on: return None
    keys = sorted(set(off) | set(on))
    out = []
    for k in keys:
        ov, nv = off.get(k), on.get(k)
        if not isinstance(ov, (int, float)) or not isinstance(nv, (int, float)):
            continue
        delta = nv - ov
        pct = (delta / ov * 100.0) if ov else None
        out.append({'metric': k, 'fishoff': ov, 'fishon': nv, 'delta': delta, 'pct': pct})
    return out

def process_one(name):
    bench_dir = os.path.join(DEST_BASE, name)
    out_path = os.path.join(bench_dir, 'pbr1_summary.json')
    summary = {'name': name}
    try:
        off_path, off_data = find_perf_json(os.path.join(bench_dir, 'fishoff'))
        on_path, on_data = find_perf_json(os.path.join(bench_dir, 'fishon'))
        summary['fishoff_perf_path'] = off_path
        summary['fishon_perf_path'] = on_path
        summary['fishoff_perf'] = off_data
        summary['fishon_perf'] = on_data
        summary['overhead'] = overhead_table(off_data, on_data)

        # Prefer the LAST session dir, but if it has no ust/ data, fall back
        # to an earlier one. This handles cases where a transient nsys SIGSEGV
        # killed the run mid-way and left an empty session.
        candidates = sorted(glob.glob(os.path.join(bench_dir, 'fishon', 'fish_*')))
        sess = None
        for c in reversed(candidates):
            sess_name = os.path.basename(c)
            ust_dir = os.path.join(c, 'ros2', sess_name, 'ust')
            if os.path.isdir(ust_dir) and any(os.scandir(ust_dir)):
                sess = c
                break
        summary['fishon_session'] = sess
        if sess:
            sess_name = os.path.basename(sess)
            db_name = f"pbr1_{name}_{sess_name[len('fish_'):]}"
            print(f"[{name}] ingest {sess} → {db_name}", flush=True)
            fish_compose.ingest_session(sess, role=None, db_name=db_name)
            print(f"[{name}] extract_container_model {db_name}", flush=True)
            G = fish_compose.extract_container_model(db_name, None)
            # Session dir is root-owned (docker bind mount). Write graph JSON
            # under the host-owned benchmark dir instead.
            fish_graph_path = os.path.join(bench_dir, 'fish_graph.json')
            graph_store.save_graph(G, db_name, '__main__')
            graph_store.export_json(db_name, '__main__', fish_graph_path)
            summary['fish_graph_path'] = fish_graph_path
            summary['graph_summary'] = summarize_graph_from_json(fish_graph_path)
        else:
            summary['fish_graph_error'] = 'no fishon session dir with non-empty ust/ found'
    except Exception as e:
        summary['error'] = f'{type(e).__name__}: {e}'
        summary['error_trace'] = traceback.format_exc()
        print(f"[{name}] ERROR: {e}", flush=True)
    with open(out_path, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"[{name}] wrote {out_path}", flush=True)
    return summary

def main():
    args = sys.argv[1:]
    if not args or args == ['--all']:
        names = sorted(d for d in os.listdir(DEST_BASE)
                       if d.startswith('isaac_ros_') and os.path.isdir(os.path.join(DEST_BASE, d)))
    else:
        names = args
    for n in names:
        process_one(n)
    print(f"DONE processed {len(names)} benchmarks", flush=True)

if __name__ == '__main__':
    main()
