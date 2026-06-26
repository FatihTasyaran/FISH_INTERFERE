"""Render each non-singleton WCC of FISH's F-graph as a Graphviz DAG,
in the same style as gpu_dag_viz.py.

Node label = entity type + entity name + parent node + callback_addr +
exec-time stats (min/max/avg/std in µs). GPU-work submitters get a red
border (penwidth=3, color=#d62728). Non-GPU nodes have a thin grey border.

Edge color encodes nature:
    data (observed L3)        — solid black
    gxf_internal (intra-node) — solid green
    other                     — solid grey

Usage:
    python3 wcc_dag_viz.py --session fish_20260626_155010 \\
        --graph-json /path/to/fish_graph.json \\
        -o <outdir>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict

import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_store


ETYPE_COLOR = {
    "sub":      "#bfe1f5",
    "serv":     "#f5d6bf",
    "tmr":      "#f5bfbf",
    "waitable": "#fff2a3",
    "ext":      "#dddddd",
    "?":        "#ffffff",
}

GPU_BORDER = "#d62728"  # red for GPU work submitters
CPU_BORDER = "#888888"  # grey for the rest

EDGE_COLOR = {
    "gxf_internal": "#2ca02c",
    "default":      "#333333",
}


def _esc(s: str) -> str:
    return s.replace('"', '\\"').replace("\n", " ")


def _short_name(lbl: str) -> str:
    """Compact long bind-closure labels for readability."""
    if not lbl:
        return "?"
    if lbl.startswith("ext:"):
        return lbl[4:][:60]
    if "waitable@" in lbl:
        return lbl
    m = re.search(r"std::_Bind<void \(\*?([A-Za-z_][\w:]+)::([\w_]+)", lbl)
    if m:
        return f"{m.group(1).split('::')[-1]}::{m.group(2)}"
    if "rclcpp::TimeSource" in lbl:
        return "TimeSource::attachNode"
    if "rclcpp_components::ComponentManager" in lbl:
        return "ComponentManager::*"
    if "negotiated::NegotiatedSubscription" in lbl:
        return "NegotiatedSubscription::*"
    if "negotiated::NegotiatedPublisher" in lbl:
        return "NegotiatedPublisher::*"
    if "ros2component.api" in lbl:
        return "ros2cli::list_nodes"
    if "ROSTopicHz" in lbl:
        return "ros2topic.hz"
    return lbl[:60]


def fetch_cb_stats(session_id: str) -> dict[str, dict]:
    """Per-callback execution time stats from ros2_trace.

    Pairs callback_start with callback_end on the same (vpid, vtid, cb_addr),
    in time order. Returns: cb_addr → {count, min_ns, max_ns, avg_ns, std_ns}.
    """
    rows = pg_store.fetch_all(
        """
        SELECT vpid, vtid, ts_ns, event, payload->>'callback' AS cb
        FROM ros2_trace
        WHERE session_id = %s
          AND event IN ('ros2:callback_start', 'ros2:callback_end')
          AND payload->>'callback' IS NOT NULL
        ORDER BY vpid, vtid, ts_ns
        """,
        (session_id,),
    )
    durations: dict[str, list[int]] = defaultdict(list)
    open_starts: dict[tuple, int] = {}  # (vpid, vtid, cb) → start_ts
    for r in rows:
        key = (r["vpid"], r["vtid"], r["cb"])
        ts = int(r["ts_ns"])
        if r["event"] == "ros2:callback_start":
            open_starts[key] = ts
        else:  # callback_end
            start_ts = open_starts.pop(key, None)
            if start_ts is not None:
                durations[r["cb"]].append(ts - start_ts)
    stats: dict[str, dict] = {}
    for cb, ds in durations.items():
        if not ds:
            continue
        avg = sum(ds) / len(ds)
        std = statistics.pstdev(ds) if len(ds) > 1 else 0.0
        stats[cb] = {
            "count": len(ds),
            "min_ns": min(ds),
            "max_ns": max(ds),
            "avg_ns": avg,
            "std_ns": std,
        }
    return stats


def _fmt_ns(ns: float) -> str:
    if ns < 1_000:
        return f"{ns:.0f}ns"
    if ns < 1_000_000:
        return f"{ns/1_000:.1f}µs"
    if ns < 1_000_000_000:
        return f"{ns/1_000_000:.2f}ms"
    return f"{ns/1e9:.2f}s"


def build_wccs(graph_json: dict, *, allowed_phases: set[str]):
    F_all = {n["id"]: n for n in graph_json["nodes"] if n["type"] == "F"}
    E = {n["id"]: n for n in graph_json["nodes"] if n["type"] == "E"}
    N = {n["id"]: n for n in graph_json["nodes"] if n["type"] == "N"}

    E_to_N, F_to_E = {}, {}
    for e in graph_json["edges"]:
        if e.get("rel") == "contains":
            if e["source"] in N and e["target"] in E:
                E_to_N[e["target"]] = e["source"]
            if e["source"] in E and e["target"] in F_all:
                F_to_E[e["target"]] = e["source"]

    # Filter F's by allowed phases (default: data only).
    F = {fid: f for fid, f in F_all.items()
         if f.get("phase", "unknown") in allowed_phases}

    G = nx.DiGraph()
    for fid in F:
        G.add_node(fid)
    edge_attrs = {}
    for e in graph_json["edges"]:
        if e.get("level") == "L3" and e["source"] in F and e["target"] in F:
            G.add_edge(e["source"], e["target"])
            edge_attrs[(e["source"], e["target"])] = {
                "topic": e.get("topic") or e.get("service", ""),
                "nature": e.get("nature", ""),
            }

    wccs = sorted(nx.weakly_connected_components(G), key=len, reverse=True)
    return F, E, N, F_to_E, E_to_N, G, edge_attrs, wccs


def f_descriptor(fid: int, F, E, N, F_to_E, E_to_N):
    """Return dict with display info for one F vertex."""
    f = F[fid]
    eid = F_to_E.get(fid)
    ent = E.get(eid) if eid else None
    nid = E_to_N.get(eid) if eid else None
    node = N.get(nid) if nid else None

    etype = ent.get("etype") if ent else "ext"
    cb_addr = ent.get("cb_addr") if ent else None
    if cb_addr == "NA":
        cb_addr = None

    if ent and etype == "sub":
        ent_label = ent.get("label", "?")
    elif ent and etype == "tmr":
        period_ms = (ent.get("period_ns") or 0) // 1_000_000
        ent_label = f"timer({period_ms}ms)"
    elif ent and etype == "serv":
        ent_label = ent.get("label", "?")
    elif ent and etype == "waitable":
        pub_topic = None
        for a in ent.get("aspects", []):
            if a.get("aspect") == "pub":
                pub_topic = a.get("topic")
                break
        ent_label = f"waitable → {pub_topic}" if pub_topic else (ent.get("label") or "waitable")
    else:
        ent_label = _short_name(f.get("label"))

    return {
        "fid": fid,
        "etype": etype,
        "ent_label": ent_label,
        "f_label": _short_name(f.get("label")),
        "cb_addr": cb_addr,
        "node_name": node.get("full_name") if node else (node.get("label") if node else "external"),
        "gpu_node": bool(f.get("gpu_node")) or bool((ent and ent.get("gpu_node"))),
    }


def build_node_label(desc: dict, stats: dict | None) -> str:
    lines = []
    # Header: entity type + ent_label
    lines.append(f"[{desc['etype']}] {desc['ent_label']}")
    # Parent node
    lines.append(f"@ {desc['node_name']}")
    # Symbol (function name)
    f_lbl = desc["f_label"]
    if f_lbl and f_lbl != desc["ent_label"]:
        lines.append(f"{f_lbl}")
    # cb_addr
    if desc["cb_addr"]:
        lines.append(f"cb={desc['cb_addr']}")
    # Exec stats
    if stats:
        lines.append(
            f"n={stats['count']}  "
            f"min={_fmt_ns(stats['min_ns'])}  "
            f"max={_fmt_ns(stats['max_ns'])}"
        )
        lines.append(
            f"avg={_fmt_ns(stats['avg_ns'])}  "
            f"std={_fmt_ns(stats['std_ns'])}"
        )
    else:
        lines.append("(no exec times)")
    return _esc("\n".join(lines))


def render_one_wcc(wcc_idx: int, wcc: set[int], G_full: nx.DiGraph,
                   edge_attrs: dict, F, E, N, F_to_E, E_to_N,
                   cb_stats: dict[str, dict], session_id: str) -> str:
    """Render one WCC as DOT source string."""
    sg = G_full.subgraph(wcc).copy()

    out = []
    out.append("digraph wcc {")
    out.append("  rankdir=LR;")
    out.append("  newrank=true;")
    out.append("  graph [fontname=\"Helvetica\", fontsize=11, nodesep=0.20, ranksep=0.45, "
               "bgcolor=\"white\", labelloc=t, labeljust=l];")
    out.append("  node [shape=box, style=\"filled,rounded\", fontname=\"Helvetica\", "
               "fontsize=9, margin=\"0.10,0.05\"];")
    out.append("  edge [fontname=\"Helvetica\", fontsize=8];")
    out.append(f"  label=\"FISH WCC#{wcc_idx} — {len(wcc)} F vertices, "
               f"{sg.number_of_edges()} edges  (session {session_id})\";")

    # Group nodes by parent node_name → cluster subgraphs
    by_node = defaultdict(list)
    descs = {}
    for fid in wcc:
        d = f_descriptor(fid, F, E, N, F_to_E, E_to_N)
        descs[fid] = d
        by_node[d["node_name"]].append(fid)

    for ci, (node_name, fids) in enumerate(sorted(by_node.items())):
        safe = re.sub(r"\W+", "_", node_name) + f"_{ci}"
        out.append(f"  subgraph cluster_{safe} {{")
        out.append(f"    label=\"{_esc(node_name)}\"; style=\"rounded\"; "
                   f"bgcolor=\"#fafafa\"; color=\"#888888\"; fontsize=10;")
        for fid in fids:
            d = descs[fid]
            fill = ETYPE_COLOR.get(d["etype"], "#ffffff")
            border = GPU_BORDER if d["gpu_node"] else CPU_BORDER
            penwidth = "3" if d["gpu_node"] else "1"
            stats = cb_stats.get(d["cb_addr"]) if d["cb_addr"] else None
            label = build_node_label(d, stats)
            out.append(
                f"    n{fid} [label=\"{label}\", fillcolor=\"{fill}\", "
                f"color=\"{border}\", penwidth={penwidth}];"
            )
        out.append("  }")

    for u, v in sg.edges():
        a = edge_attrs.get((u, v), {})
        nature = a.get("nature", "")
        topic = a.get("topic", "")
        if nature == "gxf_internal":
            color, lbl = EDGE_COLOR["gxf_internal"], "GXF"
            style = "solid"
            penwidth = "1.6"
        else:
            color = EDGE_COLOR["default"]
            lbl = topic
            style = "solid"
            penwidth = "1.0"
        out.append(
            f"  n{u} -> n{v} [label=\"{_esc(lbl)}\", color=\"{color}\", "
            f"style={style}, penwidth={penwidth}];"
        )
    out.append("}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True, help="session_id (PG)")
    ap.add_argument("--graph-json", required=True,
                    help="Path to fish_graph.json exported by model_improved_pg")
    ap.add_argument("-o", "--out", default="wcc_dag_out",
                    help="Output directory (default: wcc_dag_out)")
    ap.add_argument("--min-size", type=int, default=2,
                    help="Skip WCCs with fewer than N F vertices (default: 2)")
    ap.add_argument("--include-init", action="store_true",
                    help="Include F vertices whose phase is 'init' (handshake / one-shot boot work). "
                         "Default: drop them — only callbacks that fired after their executor's first "
                         "non-one-shot timer tick are included.")
    ap.add_argument("--include-unknown", action="store_true",
                    help="Include F vertices whose phase is 'unknown' (executor had no periodic timer, "
                         "OR cb never fired). Default: drop them.")
    args = ap.parse_args()

    allowed = {"data"}
    if args.include_init:
        allowed.add("init")
    if args.include_unknown:
        allowed.add("unknown")

    with open(args.graph_json) as f:
        gj = json.load(f)

    print(f"[wcc_dag_viz] Fetching callback exec-time stats from PG…")
    cb_stats = fetch_cb_stats(args.session)
    print(f"[wcc_dag_viz]   {len(cb_stats)} distinct callbacks with timing")

    F, E, N, F_to_E, E_to_N, G, edge_attrs, wccs = build_wccs(gj, allowed_phases=allowed)
    print(f"[wcc_dag_viz] phase filter: keeping {sorted(allowed)}; "
          f"{len(F)} F vertices, {G.number_of_edges()} L3 edges after filter")
    print(f"[wcc_dag_viz] {len(wccs)} WCCs total; "
          f"rendering {sum(1 for w in wccs if len(w) >= args.min_size)} with "
          f"≥{args.min_size} F vertices")

    os.makedirs(args.out, exist_ok=True)
    rendered = 0
    for i, wcc in enumerate(wccs):
        if len(wcc) < args.min_size:
            continue
        dot_src = render_one_wcc(i, wcc, G, edge_attrs, F, E, N, F_to_E, E_to_N,
                                 cb_stats, args.session)
        dot_path = os.path.join(args.out, f"wcc_{i:02d}.dot")
        svg_path = os.path.join(args.out, f"wcc_{i:02d}.svg")
        png_path = os.path.join(args.out, f"wcc_{i:02d}.png")
        with open(dot_path, "w") as f:
            f.write(dot_src)
        try:
            subprocess.check_call(["dot", "-Tsvg", dot_path, "-o", svg_path])
            subprocess.check_call(["dot", "-Tpng", "-Gdpi=130", dot_path, "-o", png_path])
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[wcc_dag_viz]   WCC#{i}: dot render failed: {e}")
            continue
        rendered += 1
        print(f"[wcc_dag_viz]   WCC#{i:2d}: {len(wcc):>3d} F, "
              f"{G.subgraph(wcc).number_of_edges():>3d} edges  → {os.path.basename(svg_path)}")
    print(f"[wcc_dag_viz] DONE: {rendered} files in {args.out}/")


if __name__ == "__main__":
    main()
