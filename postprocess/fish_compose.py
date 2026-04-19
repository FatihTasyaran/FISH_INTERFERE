#!/usr/bin/env python3
"""
FISH Compose — merge per-container models into a unified multi-container graph.

Reads a compose session (produced by fish_orchestrator.py), ingests each
container's traces (unless cached), extracts per-container models via
model_improved, persists them to graph_store, then merges into a single
FISH graph. The composed graph is also persisted to graph_store and
exported as JSON for fish_viz.html.

Usage:
    python3 fish_compose.py ~/fish_traces/fish_compose_20260413_200800
    python3 fish_compose.py ~/fish_traces/fish_compose_20260413_200800 --skip-ingest
    python3 fish_compose.py ~/fish_traces/fish_compose_20260413_200800 --skip-ingest --from-cache
    python3 fish_compose.py ~/fish_traces/fish_compose_20260413_200800 --force-reextract

Output:
    <compose_session>/fish_graph.json    — unified graph for fish_viz.html
    <compose_session>/compose_report.txt — merge statistics
    MongoDB database named after the compose session (e.g.
    fish_compose_20260419_161633):
        ros2_trace_<role>, snapshot_<role>, ... per-container raw trace
        graph_meta, graph_nodes, graph_edges, graph_mutations scope-
        discriminated (scope = role for per-container, "__composed__"
        for the unified graph).

Architecture:
    L-1: Container (CN)   — one per container (emitted by model_improved)
    L0:  Executor
    L1:  Node
    L2:  Entity
    L3:  Function

    Cross-container edges are created by matching externally-marked entities
    across containers by topic/service name. Unmatched externals remain.
    An aggregated CN→CN edge is added at L-1 for each cross-container pair.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from itertools import count
from pathlib import Path

import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model_improved
import graph_store
from ingest import ingest_session


ID_OFFSET = 100000  # per-container ID offset


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [compose] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Per-container extraction
# ---------------------------------------------------------------------------

def extract_container_model(compose_db: str, role: str) -> nx.DiGraph:
    """Run the full model_improved pipeline for one container.

    `compose_db` is the session DB (shared by all containers).
    `role` is the container role — it selects the role-suffixed
    collections (`ros2_trace_<role>`, etc.) via ScopedMongo and is
    also the CN label.
    """
    model_improved.vertex_counter = count(1)
    mongo = model_improved.connect_mongo(compose_db, role=role)

    executors, nodes = model_improved.identify_executors(mongo)
    entities = model_improved.identify_entities(mongo, nodes)
    functions = model_improved.identify_callbacks(mongo, nodes, entities, executors)
    model_improved.attribute_aspects(mongo, executors, nodes, entities)
    model_improved.attach_callback_groups(mongo, executors, nodes, entities, functions)
    model_improved.detect_oort_threads(
        mongo, executors, nodes, entities, functions,
        session=compose_db, container=role)
    model_improved.detect_actions(entities)
    model_improved.split_callbacks(mongo, entities, functions)

    G = model_improved.create_graph(executors, nodes, entities, functions,
                                    session_name=role)
    G = model_improved.add_horizontal_edges(G, mongo, executors, nodes,
                                            entities, functions)

    log(f"  {role}: {G.number_of_nodes()} vertices, {G.number_of_edges()} edges")
    return G


def get_container_graph(container_dir: str, compose_db: str, role: str, *,
                        skip_ingest: bool, from_cache: bool,
                        force_reextract: bool,
                        tz_name: str = "Europe/Amsterdam") -> nx.DiGraph:
    """Obtain a per-container fish graph, using graph_store as a cache.

    Policy:
      force_reextract → ignore cache, re-ingest (unless skip_ingest) + re-extract + save
      from_cache     → load from graph_store or fail loudly
      default        → if cache exists and skip_ingest, load from graph_store
                       else ingest (unless skip_ingest) + extract + save

    The graph cache key is the `role` (scope) inside the session DB.
    """
    cached = graph_store.graph_exists(compose_db, role)

    if from_cache:
        if not cached:
            raise RuntimeError(
                f"--from-cache was set but graph_store has no entry for "
                f"db='{compose_db}' scope='{role}'. Run without "
                f"--from-cache first."
            )
        log(f"  {role}: loading from graph_store (db={compose_db} scope={role})")
        return graph_store.load_graph(compose_db, role)

    if cached and skip_ingest and not force_reextract:
        log(f"  {role}: cached in graph_store (scope={role}) → loading")
        return graph_store.load_graph(compose_db, role)

    if not skip_ingest:
        log(f"  {role}: ingesting {container_dir}")
        ingest_session(container_dir, role=role, db_name=compose_db,
                       tz_name=tz_name)

    log(f"  {role}: extracting model")
    G = extract_container_model(compose_db, role)

    graph_store.save_graph(
        G, compose_db, role,
        source_trace=container_dir,
        container_role=role,
        is_composed=False,
        actor="fish_compose",
    )
    log(f"  {role}: saved to graph_store (db={compose_db} scope={role})")
    return G


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _find_cn(G: nx.DiGraph):
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v is not None and v.t_v == "CN":
            return nid, v
    return None, None


def match_external_boundaries(per_container_graphs: list) -> tuple[list, set]:
    """Match external E-vertices across containers and emit cross-container
    edges between the *real* entities on each side — not through the ext
    placeholder. The ext placeholder is then dropped from the unified graph.

    per_container_graphs: list of (role, G) after ID remap.

    Returns (cross_edges, remove_ext):
      cross_edges: list of (src_real_id, dst_real_id, attrs_dict)
      remove_ext:  set of ext entity IDs to drop (and any comm edges touching
                   them get filtered out in the merge step).
    """
    # Index ext and real pub/sub aspects by topic.
    ext_pubs, ext_subs = {}, {}   # topic → [(role, ext_id, G)]
    real_pubs, real_subs = {}, {} # topic → [(role, real_id)]

    for role, G in per_container_graphs:
        for nid, data in G.nodes(data=True):
            v = data.get("v")
            if v is None or v.t_v != "E":
                continue
            is_ext = bool(v.A_v.get("external", False))
            for aspect in v.A_v.get("aspects") or []:
                topic = aspect.get("topic")
                if not topic:
                    continue
                a = aspect.get("aspect")
                if a == "pub":
                    if is_ext:
                        ext_pubs.setdefault(topic, []).append((role, nid, G))
                    else:
                        real_pubs.setdefault(topic, []).append((role, nid))
                elif a == "sub":
                    if is_ext:
                        ext_subs.setdefault(topic, []).append((role, nid, G))
                    else:
                        real_subs.setdefault(topic, []).append((role, nid))

    cross_edges, remove_ext, matched_topics = [], set(), set()

    # ext pub in A (placeholder representing "someone outside publishes T")
    # ↔ real pub in some other container B (the actual source).
    # Redirect A's local subscribers (previously fed by the ext) to B's real pub.
    for topic, ext_list in ext_pubs.items():
        real = real_pubs.get(topic, [])
        if not real:
            continue
        for ext_role, ext_id, ext_G in ext_list:
            # A's subscribers that received from this ext via comm out-edges
            local_subs = [v for u, v in ext_G.out_edges(ext_id)
                          if ext_G[u][v].get("rel") == "comm"]
            if not local_subs:
                remove_ext.add(ext_id)
                matched_topics.add(topic)
                continue
            for real_role, real_id in real:
                if real_role == ext_role:
                    continue
                for sub_id in local_subs:
                    cross_edges.append((real_id, sub_id, {
                        "rel": "comm", "level": "L2", "nature": "msg",
                        "topic": topic, "cross_container": True,
                        "src_container": real_role, "dst_container": ext_role,
                    }))
            remove_ext.add(ext_id)
            matched_topics.add(topic)

    # ext sub in A ↔ real sub in B. Redirect A's local publishers (that fed
    # the ext) to B's real subscriber.
    for topic, ext_list in ext_subs.items():
        real = real_subs.get(topic, [])
        if not real:
            continue
        for ext_role, ext_id, ext_G in ext_list:
            local_pubs = [u for u, v in ext_G.in_edges(ext_id)
                          if ext_G[u][v].get("rel") == "comm"]
            if not local_pubs:
                remove_ext.add(ext_id)
                matched_topics.add(topic)
                continue
            for real_role, real_id in real:
                if real_role == ext_role:
                    continue
                for pub_id in local_pubs:
                    cross_edges.append((pub_id, real_id, {
                        "rel": "comm", "level": "L2", "nature": "msg",
                        "topic": topic, "cross_container": True,
                        "src_container": ext_role, "dst_container": real_role,
                    }))
            remove_ext.add(ext_id)
            matched_topics.add(topic)

    log(f"  boundary match: {len(matched_topics)} topics, "
        f"{len(cross_edges)} real→real cross-container L2 edges, "
        f"{len(remove_ext)} ext entities resolved")
    return cross_edges, remove_ext


def merge_container_graphs(container_graphs: list) -> nx.DiGraph:
    """Merge per-container graphs into a unified graph.

    container_graphs: list of (role, G) tuples. Each G already has its own
    CN vertex at L-1 (from model_improved.create_graph). IDs get offset per
    container to avoid collisions.
    """
    if not container_graphs:
        return nx.DiGraph()

    # Phase 1: ID remap
    remapped = []
    for i, (role, G) in enumerate(container_graphs):
        offset = (i + 1) * ID_OFFSET
        G2 = graph_store.remap_graph(G, offset)
        remapped.append((role, G2))
        log(f"  remap: {role} offset={offset} "
            f"({G2.number_of_nodes()} vertices, {G2.number_of_edges()} edges)")

    # Phase 2: per-container CN + membership
    cn_of = {}                       # role → (cn_id, cn_vertex)
    container_of = {}                # vertex_id → cn_id

    for role, G in remapped:
        cn_id, cn_v = _find_cn(G)
        if cn_id is None:
            log(f"  WARN: no CN vertex in {role} — skipping")
            continue
        cn_v.A_v["label"] = role
        cn_v.A_v["role"] = role
        cn_of[role] = (cn_id, cn_v)
        for nid in G.nodes():
            container_of[nid] = cn_id

    # Phase 3: boundary matching across containers (real→real edges)
    cross_edges, remove_ext = match_external_boundaries(remapped)

    # Phase 4: union graph (drop ext placeholders and edges touching them)
    unified = nx.DiGraph()
    for role, G in remapped:
        for nid, data in G.nodes(data=True):
            if nid in remove_ext:
                continue
            unified.add_node(nid, **data)
        for u, v, data in G.edges(data=True):
            if u in remove_ext or v in remove_ext:
                continue
            unified.add_edge(u, v, **data)

    # Phase 5: cross-container L2 edges (real→real)
    for src, dst, attrs in cross_edges:
        if src in unified and dst in unified:
            unified.add_edge(src, dst, **attrs)

    # Phase 6: aggregate CN→CN edges at L-1
    l_minus1 = {}  # (src_cn, dst_cn) → {topics, count}
    for src, dst, attrs in cross_edges:
        sc = container_of.get(src)
        dc = container_of.get(dst)
        if sc is None or dc is None or sc == dc:
            continue
        key = (sc, dc)
        info = l_minus1.setdefault(key, {"topics": set(), "count": 0})
        info["count"] += 1
        t = attrs.get("topic")
        if t:
            info["topics"].add(t)

    for (sc, dc), info in l_minus1.items():
        unified.add_edge(sc, dc,
                         rel="comm", level="L-1",
                         nature="msg", cross_container=True,
                         topics=list(info["topics"]),
                         comm_count=info["count"])

    log(f"  unified: {unified.number_of_nodes()} vertices, "
        f"{unified.number_of_edges()} edges ({len(l_minus1)} CN→CN L-1 edges)")
    return unified


# ---------------------------------------------------------------------------
# Compose driver
# ---------------------------------------------------------------------------

def compose_session(session_dir: str, *, skip_ingest: bool = False,
                    from_cache: bool = False, force_reextract: bool = False,
                    tz_name: str = "Europe/Amsterdam"):
    session_dir = os.path.abspath(session_dir)
    compose_name = os.path.basename(session_dir)
    log(f"=== FISH Compose: {compose_name} ===")

    manifest_path = os.path.join(session_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        log(f"ERROR: manifest.json not found in {session_dir}")
        return

    with open(manifest_path) as f:
        manifest = json.load(f)

    fish_containers = {}
    for alias, cinfo in manifest.get("containers", {}).items():
        if cinfo.get("fish") and cinfo.get("trace_session"):
            role = cinfo.get("role", alias)
            container_dir = os.path.join(session_dir, role)
            if os.path.isdir(container_dir):
                fish_containers[role] = container_dir
            else:
                log(f"  SKIP {role}: directory not found at {container_dir}")

    if not fish_containers:
        log("No FISH containers found. Nothing to compose.")
        return
    log(f"FISH containers: {sorted(fish_containers.keys())}")

    # compose_name IS the session DB name in the new layout.
    compose_db = compose_name

    t0 = time.time()
    container_graphs = []
    for role, cdir in fish_containers.items():
        G = get_container_graph(
            cdir, compose_db, role,
            skip_ingest=skip_ingest,
            from_cache=from_cache,
            force_reextract=force_reextract,
            tz_name=tz_name,
        )
        container_graphs.append((role, G))
    extract_time = time.time() - t0

    t1 = time.time()
    unified = merge_container_graphs(container_graphs)
    merge_time = time.time() - t1

    # Persist composed graph under scope "__composed__" in the session DB
    composed_scope = graph_store.COMPOSED_SCOPE
    graph_store.save_graph(
        unified, compose_db, composed_scope,
        source_trace=session_dir,
        container_role=None,
        is_composed=True,
        actor="fish_compose",
        note=f"Composed from {len(container_graphs)} containers: "
             f"{sorted(fish_containers.keys())}",
    )
    log(f"Saved composed graph to graph_store "
        f"(db={compose_db} scope={composed_scope})")

    # JSON export for viz
    json_path = os.path.join(session_dir, "fish_graph.json")
    graph_store.export_json(compose_db, composed_scope, json_path)
    log(f"Exported → {json_path}")

    # Report
    report_lines = [
        f"FISH Compose Report: {compose_name}",
        f"{'=' * 60}",
        f"Containers: {len(container_graphs)}",
    ]
    for role, G in container_graphs:
        report_lines.append(
            f"  {role}: {G.number_of_nodes()} vertices, "
            f"{G.number_of_edges()} edges")
    report_lines += [
        f"Unified graph: {unified.number_of_nodes()} vertices, "
        f"{unified.number_of_edges()} edges",
        f"Extract: {extract_time:.1f}s  Merge: {merge_time:.1f}s",
        f"Output JSON: {json_path}",
        f"graph_store:  db='{compose_db}' scope='{composed_scope}' "
        f"(plus per-container scopes: {sorted(fish_containers.keys())})",
    ]
    report = "\n".join(report_lines)
    report_path = os.path.join(session_dir, "compose_report.txt")
    with open(report_path, "w") as f:
        f.write(report + "\n")
    print(report)
    log(f"=== Compose complete ({extract_time + merge_time:.1f}s) ===")


def main():
    ap = argparse.ArgumentParser(
        description="FISH Compose — merge multi-container traces into a unified graph")
    ap.add_argument("session_dir", help="Path to compose session directory")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Assume traces already ingested into MongoDB")
    ap.add_argument("--from-cache", action="store_true",
                    help="Load per-container graphs from graph_store "
                         "(implies --skip-ingest; fails if any cache entry missing)")
    ap.add_argument("--force-reextract", action="store_true",
                    help="Ignore graph_store cache; re-run model extraction")
    ap.add_argument("--tz", default="Europe/Amsterdam",
                    help="Timezone for trace timestamps")
    args = ap.parse_args()

    if not os.path.isdir(args.session_dir):
        print(f"Error: {args.session_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    skip_ingest = args.skip_ingest or args.from_cache
    compose_session(
        args.session_dir,
        skip_ingest=skip_ingest,
        from_cache=args.from_cache,
        force_reextract=args.force_reextract,
        tz_name=args.tz,
    )


if __name__ == "__main__":
    main()
