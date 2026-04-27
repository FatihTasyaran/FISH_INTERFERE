"""gpu_task_graph_viz — visualize per-callback DAG + process-level stream graph.

Two modes:

  shape:  pick a callback F vertex + shape signature, render the
          Typed DAG (CPU/SM/CE). Nodes are coloured primarily by PE
          type; GPU nodes (SM/CE) are additionally grouped into
          per-stream clusters so the stream-level structure inside
          ONE invocation shows up visually.

  process: pick an EX vertex (pid) and render the process-level
           stream DAG. Streams are the vertices; edges are the
           heuristic src→dst wait-event counts from
           gpu_task_graph.stream_dag_edges. Vertex labels include the
           deterministic wait-count tallies from stream_wait_counts.

Usage:
  python3 gpu_task_graph_viz.py shape <session> <scope> --f <id> [--sig <hex>] [-o <outdir>]
  python3 gpu_task_graph_viz.py process <session> <scope> --pid <pid> [-o <outdir>]

See notes/paper_important_points.txt P1 / P4, notes/stream_bridge.txt.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_store


PE_COLOR = {
    "CPU": "#6BAED6",   # blue
    "SM":  "#74C476",   # green
    "CE":  "#FB6A4A",   # red
}

# Distinct palette for stream clusters (up to ~10; cycles after)
STREAM_PALETTE = [
    "#FFF5EB", "#F7FCF0", "#F1EEF6", "#FEF0D9", "#F0F9E8",
    "#FEE8C8", "#E0F3DB", "#FDE0DD", "#D4B9DA", "#DADAEB",
]


def _esc(s: str) -> str:
    return str(s).replace('"', '\\"').replace("\n", " ")


def _stream_color(streamId: int) -> str:
    if streamId is None:
        return "#F5F5F5"
    return STREAM_PALETTE[streamId % len(STREAM_PALETTE)]


def _render_svg(dot_path, svg_path):
    try:
        subprocess.check_call(["dot", "-Tsvg", dot_path, "-o", svg_path])
        return True
    except Exception as e:
        print(f"[viz] dot failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Shape view (per callback, single signature)
# ---------------------------------------------------------------------------

def render_shape(session, scope, f_id, sig, outdir):
    G = graph_store.load_graph(session, scope)
    if f_id not in G.nodes:
        raise SystemExit(f"F#{f_id} not in graph")
    v = G.nodes[f_id]["v"]
    dags = v.A_v.get("gpu_dags") or []
    if not dags:
        raise SystemExit(f"F#{f_id} has no gpu_dags")
    if sig:
        matches = [d for d in dags if d["signature"].startswith(sig)]
        if not matches:
            raise SystemExit(f"No shape starting with {sig} on F#{f_id}")
        dag = matches[0]
    else:
        dag = max(dags, key=lambda d: d.get("count", 0))

    nodes = dag["nodes"]
    edges = dag["edges"]

    # Group GPU nodes by stream for cluster rendering
    streams_seen = {}
    for n in nodes:
        if n["pe"] in ("SM", "CE") and "stream" in n:
            streams_seen.setdefault(int(n["stream"]), []).append(n["idx"])

    title = (f"{session}  F#{f_id}\\n"
             f"{_esc(v.A_v.get('label', '')[:90])}\\n"
             f"sig={dag['signature']}  count={dag['count']}  "
             f"streams={dag.get('streams') or sorted(streams_seen)}  "
             f"nodes={len(nodes)} "
             f"(CPU={dag.get('n_cpu', '?')}"
             f" SM={dag.get('n_sm', '?')}"
             f" CE={dag.get('n_ce', '?')})")

    out = [
        "digraph shape {",
        '  rankdir=TB;',
        '  node [shape=box, style="filled,rounded", fontname="Helvetica",'
        ' fontsize=9, margin="0.05,0.02"];',
        '  edge [fontname="Helvetica", fontsize=7];',
        f'  label="{title}";',
        '  labelloc=t; labeljust=l;',
        '  graph [nodesep=0.10, ranksep=0.30];',
    ]

    # CPU nodes + GPU nodes (grouped by stream as subgraph clusters)
    cpu_node_ids = [n["idx"] for n in nodes if n["pe"] == "CPU"]

    # Draw CPU nodes at top (no cluster)
    for n in nodes:
        if n["pe"] == "CPU":
            name = n["name"][:37]
            out.append(f'  n{n["idx"]} [label="CPU\\n{_esc(name)}", '
                       f'fillcolor="{PE_COLOR["CPU"]}"];')

    # Draw GPU nodes inside per-stream clusters
    for sid, node_ids in sorted(streams_seen.items()):
        out.append(f'  subgraph cluster_s{sid} {{')
        out.append(f'    label="stream {sid}"; style="filled,rounded"; '
                   f'fillcolor="{_stream_color(sid)}"; fontname="Helvetica-Bold";')
        for idx in node_ids:
            n = nodes[idx]
            name = n["name"][:37]
            out.append(f'    n{idx} [label="{n["pe"]}\\n{_esc(name)}\\ns{sid}", '
                       f'fillcolor="{PE_COLOR[n["pe"]]}"];')
        out.append('  }')

    # Edges
    for e in edges:
        if len(e) == 3:
            u, v_, kind = e
        else:
            u, v_ = e; kind = ""
        style = ""
        if kind == "cpu_order":
            style = ' [style=dashed, color="#999999"]'
        elif kind == "stream_fifo":
            style = ' [color="#FB6A4A", penwidth=1.2]'
        elif kind == "launch":
            style = ' [color="#333333"]'
        out.append(f'  n{u} -> n{v_}{style};')
    out.append('}')

    base = os.path.join(outdir, f"shape_F{f_id}_{dag['signature']}")
    with open(base + ".dot", "w") as fp:
        fp.write("\n".join(out))
    _render_svg(base + ".dot", base + ".svg")
    print(f"[viz shape] → {base}.svg")
    return base + ".svg"


# ---------------------------------------------------------------------------
# Process view (stream-level DAG overview)
# ---------------------------------------------------------------------------

def render_process(session, scope, pid, outdir):
    G = graph_store.load_graph(session, scope)
    ex_v = None
    for nid, data in G.nodes(data=True):
        v = data["v"]
        if v.t_v == "EX" and v.A_v.get("pid") == pid:
            ex_v = v; break
    if ex_v is None:
        raise SystemExit(f"No EX vertex with pid={pid} in {scope}")
    meta = ex_v.A_v.get("gpu_task_graph")
    if not meta:
        raise SystemExit(f"EX#{ex_v.id_v} has no gpu_task_graph meta. "
                         f"Run gpu_task_graph.py first.")

    wait_counts = meta.get("stream_wait_counts", {})
    edges = meta.get("stream_dag_edges", [])
    sync_types = meta.get("sync_types", {})

    # Which callback F vertices sit under this EX and use which streams?
    cb_stream_links = []
    # walk children
    def _walk(nid, depth=0):
        v = G.nodes[nid]["v"]
        if v.t_v == "F" and v.A_v.get("gpu_streams"):
            cb_stream_links.append((nid, v.A_v.get("label", "")[:50],
                                     v.A_v.get("gpu_invocations", 0),
                                     v.A_v.get("gpu_streams")))
        for c in v.Z_v:
            if c in G.nodes:
                _walk(c, depth + 1)
    _walk(ex_v.id_v)

    # Build dot: nodes = streams, edges = src→dst weighted
    all_streams = set(int(s) for s in wait_counts) | \
                  {int(e["src"]) for e in edges} | \
                  {int(e["dst"]) for e in edges}
    for _, _, _, streams in cb_stream_links:
        all_streams.update(streams or [])

    title = (f"{session}  pid={pid}  {_esc(meta.get('pid_name', '')[:70])}\\n"
             f"callback_bound_time={meta['callback_bound_time_frac']:.1%}, "
             f"stream_bridge={meta['callback_bound_stream_frac']:.1%}, "
             f"oort={meta['oort_frac']:.1%}\\n"
             f"sync_types={sync_types}   "
             f"heuristic unresolved={meta.get('stream_dag_unresolved', 0)}")

    out = [
        "digraph process {",
        '  rankdir=LR;',
        f'  label="{title}";',
        '  labelloc=t; labeljust=l;',
        '  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9];',
        '  graph [nodesep=0.5, ranksep=1.0];',
    ]

    # Stream nodes
    for s in sorted(all_streams):
        wc = wait_counts.get(str(s)) or wait_counts.get(s, 0)
        label = f"stream {s}\\nwaits={wc}"
        out.append(f'  s{s} [label="{label}", fillcolor="{_stream_color(s)}"];')

    # Inter-stream edges (heuristic)
    max_count = max((e["count"] for e in edges), default=1)
    for e in edges:
        src = int(e["src"]); dst = int(e["dst"]); cnt = e["count"]
        pen = max(1.0, 0.5 + 4.0 * (cnt / max_count))
        out.append(f'  s{src} -> s{dst} [label="{cnt}", penwidth={pen:.2f}, '
                   f'color="#333333", fontcolor="#333333"];')

    # Callback nodes (subgraph cluster) — show the callbacks that feed streams
    if cb_stream_links:
        out.append('  subgraph cluster_callbacks {')
        out.append('    label="callbacks"; style="dashed"; fontname="Helvetica-Italic";')
        out.append('    node [shape=note, fillcolor="#FFFBE6", fontsize=8];')
        for fid, lbl, inv, _ in sorted(cb_stream_links, key=lambda x: -x[2])[:10]:
            short = lbl[:40]
            out.append(f'    cb{fid} [label="F#{fid}\\n{_esc(short)}\\ninv={inv}"];')
        out.append('  }')
        # Dashed edges: callback → streams it claims
        for fid, _, _, streams in sorted(cb_stream_links, key=lambda x: -x[2])[:10]:
            for s in (streams or []):
                out.append(f'  cb{fid} -> s{s} [style=dashed, color="#888888", '
                           f'constraint=false];')

    out.append('}')

    base = os.path.join(outdir, f"process_pid{pid}")
    with open(base + ".dot", "w") as fp:
        fp.write("\n".join(out))
    _render_svg(base + ".dot", base + ".svg")
    print(f"[viz process] → {base}.svg")
    return base + ".svg"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    sp = sub.add_parser("shape", help="Render one callback's Typed DAG shape")
    sp.add_argument("session"); sp.add_argument("scope")
    sp.add_argument("--f", type=int, required=True)
    sp.add_argument("--sig", default=None)
    sp.add_argument("-o", "--outdir", default=None)

    pp = sub.add_parser("process", help="Render process-level stream DAG")
    pp.add_argument("session"); pp.add_argument("scope")
    pp.add_argument("--pid", type=int, required=True)
    pp.add_argument("-o", "--outdir", default=None)

    args = ap.parse_args()
    outdir = args.outdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dag_out")
    os.makedirs(outdir, exist_ok=True)

    if args.mode == "shape":
        render_shape(args.session, args.scope, args.f, args.sig, outdir)
    else:
        render_process(args.session, args.scope, args.pid, outdir)


if __name__ == "__main__":
    main()
