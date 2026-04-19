"""Standalone GPU DAG visualizer — export one DAG (per oort F vertex,
per signature) from graph_store as a Graphviz dot file + rendered SVG,
and print a compact text summary.

Not wired into fish_viz.html. Use this when you want to eyeball the
shape of the GPU DAG FISH extracted, separate from the main model
graph.

Usage:
    python3 gpu_dag_viz.py <session> <scope> [--f <id>] [--sig <hex>]
                          [--top]  [-o <outdir>]
    python3 gpu_dag_viz.py fish_compose_20260419_161633 aircraft --top

    --top           pick the most-repeated DAG on the (sole or
                    --f-specified) oort F vertex (default if no --sig)
    --sig <hex>     pick a specific DAG by signature
    --f <id>        restrict to a specific F vertex; if only one oort
                    in the scope, optional
    -o <outdir>     output directory (default: postprocess/dag_out/)

Outputs:
    <outdir>/<signature>.dot
    <outdir>/<signature>.svg   (rendered by the system `dot` tool)
    <outdir>/<signature>.txt   (compact text summary)

Coloring: CPU = blue, SM = green, CE = red (matches PDF Fig 4.9).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_store


PE_COLOR = {
    "CPU": "#6BAED6",   # blue
    "SM":  "#74C476",   # green
    "CE":  "#FB6A4A",   # red
}


def load_dag(session, scope, *, f_id=None, signature=None, pick_top=True):
    """Return (f_vertex, chosen_dag_dict) from graph_store.

    f_id: None → take unique oort F in scope, or error.
    signature: None → pick by pick_top (most-repeated) if True else error.
    """
    G = graph_store.load_graph(session, scope)
    oort_fs = [(nid, d["v"]) for nid, d in G.nodes(data=True)
               if d["v"].A_v.get("ptype") == "oort"]
    if f_id is not None:
        oort_fs = [(nid, v) for nid, v in oort_fs if nid == f_id]
    if not oort_fs:
        raise SystemExit(f"No matching oort F in (db={session}, scope={scope}, f_id={f_id})")
    if len(oort_fs) > 1 and f_id is None:
        raise SystemExit(
            f"Multiple oort F vertices in this scope, pick with --f <id>. "
            f"Found: {[nid for nid, _ in oort_fs]}")
    f_id_final, v = oort_fs[0]

    dags = v.A_v.get("gpu_dags") or []
    if not dags:
        raise SystemExit(f"F#{f_id_final} has no gpu_dags attached. Run gpu_dag.py first.")

    if signature:
        matching = [d for d in dags if d.get("signature", "").startswith(signature)]
        if not matching:
            raise SystemExit(f"No DAG with signature prefix '{signature}'. Available: "
                             f"{[d['signature'] for d in dags]}")
        chosen = matching[0]
    elif pick_top:
        chosen = max(dags, key=lambda d: d.get("count", 0))
    else:
        raise SystemExit("Pass --sig or --top")

    return f_id_final, v, chosen


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------

def summarize(v, dag) -> str:
    nodes = dag["nodes"]
    edges = dag["edges"]

    counts = Counter(n["pe"] for n in nodes)
    total_dur_by_pe = Counter()
    for n in nodes:
        total_dur_by_pe[n["pe"]] += int(n.get("c_max_ns", 0))
    per_name = Counter((n["pe"], n["name"]) for n in nodes)

    lines = []
    lines.append(f"F vertex          : #{v.id_v}  {v.A_v.get('label')}")
    lines.append(f"signature         : {dag['signature']}")
    lines.append(f"invocations (this shape): {dag['count']:,}")
    lines.append(f"nodes             : {len(nodes)}  (CPU={counts['CPU']} SM={counts['SM']} CE={counts['CE']})")
    lines.append(f"edges             : {len(edges)}")
    lines.append(f"WCET sum per PE   : CPU={total_dur_by_pe['CPU']:,}ns  "
                 f"SM={total_dur_by_pe['SM']:,}ns  CE={total_dur_by_pe['CE']:,}ns")
    lines.append(f"WCET total        : {sum(total_dur_by_pe.values()):,}ns")
    lines.append("")
    lines.append("Top repeated ops (pe, name, count):")
    for (pe, name), n in per_name.most_common(20):
        lines.append(f"  {pe:3s}  {n:>4}×  {name[:80]}")
    # Stream distribution for GPU ops
    streams = Counter(n.get("stream") for n in nodes if n["pe"] in ("SM", "CE"))
    if streams:
        lines.append("")
        lines.append(f"GPU stream distribution: {dict(streams)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dot + SVG
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace('"', '\\"').replace("\n", " ")


def to_dot(dag, *, title="GPU DAG") -> str:
    nodes = dag["nodes"]
    edges = dag["edges"]

    out = [
        "digraph gpudag {",
        '  rankdir=TB;',
        '  node [shape=box, style="filled,rounded", fontname="Helvetica",'
        ' fontsize=9, margin="0.05,0.02"];',
        '  edge [fontname="Helvetica", fontsize=7];',
        f'  label="{_esc(title)}";',
        '  labelloc=t;',
        '  labeljust=l;',
        '  graph [nodesep=0.10, ranksep=0.25];',
    ]
    for n in nodes:
        pe = n["pe"]
        color = PE_COLOR.get(pe, "#EEEEEE")
        name = n["name"]
        if len(name) > 40:
            name = name[:37] + "…"
        label = f"{pe}\\n{name}"
        extra = []
        if n.get("c_max_ns"):
            extra.append(f"{n['c_max_ns']/1e3:.1f}µs")
        if n.get("bytes"):
            extra.append(f"{n['bytes']}B")
        if n.get("stream"):
            extra.append(f"s{n['stream']}")
        if extra:
            label += "\\n" + " ".join(extra)
        out.append(f'  n{n["idx"]} [label="{_esc(label)}", fillcolor="{color}"];')
    for e in edges:
        if len(e) == 3:
            u, v, kind = e
        else:
            u, v = e; kind = ""
        style = ""
        if kind == "cpu_order":
            style = ' [style=dashed, color="#999999"]'
        elif kind == "stream_fifo":
            style = ' [color="#FB6A4A", penwidth=1.2]'
        elif kind == "launch":
            style = ' [color="#333333"]'
        out.append(f"  n{u} -> n{v}{style};")
    out.append("}")
    return "\n".join(out)


def render_svg(dot_path: str, svg_path: str):
    try:
        subprocess.check_call(["dot", "-Tsvg", dot_path, "-o", svg_path])
        return True
    except subprocess.CalledProcessError as e:
        print(f"[viz] WARN: dot rendering failed: {e}")
        return False
    except FileNotFoundError:
        print(f"[viz] WARN: graphviz (dot) not installed")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", help="graph_store db_name")
    ap.add_argument("scope", help="role (aircraft, ground, …) or __composed__")
    ap.add_argument("--f", type=int, default=None,
                    help="F vertex id (if multiple oort in scope)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--top", action="store_true",
                     help="pick the most-repeated DAG (default)")
    grp.add_argument("--sig", default=None,
                     help="pick by signature (or prefix)")
    ap.add_argument("-o", "--outdir", default=None,
                    help="output directory (default: postprocess/dag_out/)")
    args = ap.parse_args()

    outdir = args.outdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dag_out")
    os.makedirs(outdir, exist_ok=True)

    f_id, v, dag = load_dag(
        args.session, args.scope,
        f_id=args.f, signature=args.sig,
        pick_top=(not args.sig)
    )

    sig = dag["signature"]
    base = os.path.join(outdir, f"{sig}")
    dot_path = base + ".dot"
    svg_path = base + ".svg"
    txt_path = base + ".txt"

    summary = summarize(v, dag)
    with open(txt_path, "w") as fp:
        fp.write(summary + "\n")
    print(summary)
    print()
    print(f"[viz] text summary → {txt_path}")

    title = (f"{args.session}  scope={args.scope}  F#{f_id} "
             f"{v.A_v.get('label')}  sig={sig}  "
             f"({dag['count']:,} invocations)")
    dot_src = to_dot(dag, title=title)
    with open(dot_path, "w") as fp:
        fp.write(dot_src)
    print(f"[viz] dot file     → {dot_path}  ({len(dot_src):,} bytes)")

    if render_svg(dot_path, svg_path):
        print(f"[viz] SVG          → {svg_path}")
        print(f"[viz] open in browser: file://{os.path.abspath(svg_path)}")
    else:
        print("[viz] render manually: dot -Tsvg {dot_path} -o {svg_path}")


if __name__ == "__main__":
    main()
