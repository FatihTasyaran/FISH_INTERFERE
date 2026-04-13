#!/usr/bin/env python3
"""
FISH Compose — merge per-container models into a unified multi-container graph.

Reads a compose session (produced by fish_orchestrator.py), ingests each
container's traces, extracts per-container models, then merges them into
a single FISH graph with an L-1 Container layer.

Usage:
    python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260413_200800
    python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260413_200800 --skip-ingest

Output:
    <compose_session>/fish_graph.json   — unified graph for fish_viz.html
    <compose_session>/compose_report.txt — merge statistics

Architecture:
    L-1: Container  (new — one per traced container)
    L0:  Executor    (per-container, ID offset applied)
    L1:  Node
    L2:  Entity
    L3:  Function

    Cross-container edges are created by matching external boundary
    entities across containers (topic/service name matching).
    Unmatched externals remain as ext entities.
"""

import argparse
import json
import os
import sys
import time
from itertools import count
from pathlib import Path

# Add parent to path so we can import from postprocess
sys.path.insert(0, os.path.dirname(__file__))

from model import (
    FishVertex, FishEdge, ret_A_dict, vertex_counter,
    connect_mongo, connect_influx,
    identify_executors, identify_entities, identify_callbacks,
    attribute_aspects, create_graph_add_vertical_relations,
    get_node_infos, add_horizontal_relations,
)
from utils import (
    pretty_print_four_layers, create_detection_summary,
    create_graph_summary, export_fish_json,
)
from ingest import ingest_session


ID_OFFSET = 100000  # per-container ID offset


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [compose] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Per-container model extraction
# ---------------------------------------------------------------------------

def extract_container_model(container_dir, container_role, db_prefix, tz_name="Europe/Amsterdam"):
    """Ingest + extract model for a single container.

    Returns:
        dict with keys: executors, nodes, entities, functions, G, mongo
    """
    db_name = f"{db_prefix}_{container_role}"
    log(f"--- Container: {container_role} → DB: {db_name} ---")

    # Ingest
    log(f"Ingesting {container_role}...")
    ingest_session(container_dir, tz_name=tz_name)

    # The ingest uses basename of container_dir as DB name.
    # We need to rename/use the correct DB. Actually ingest_session uses
    # os.path.basename(session_dir) as the DB name. Since container_dir
    # is like .../fish_compose_XXX/aircraft, the DB name would be "aircraft".
    # We want db_prefix_role, so let's connect with the actual name.
    actual_db_name = os.path.basename(container_dir)
    log(f"Connecting to MongoDB: {actual_db_name}")
    mongo = connect_mongo(actual_db_name)

    # Reset vertex counter for this container (will be offset later)
    import model
    model.vertex_counter = count(1)

    # Extract model
    log(f"Extracting model for {container_role}...")
    executors, nodes = identify_executors(mongo)
    entities = identify_entities(mongo, nodes)
    functions = identify_callbacks(mongo, nodes, entities, executors)
    attribute_aspects(mongo, executors, nodes, entities)

    # Build graph
    G = create_graph_add_vertical_relations(executors, nodes, entities, functions)
    node_infos = get_node_infos(mongo)
    G = add_horizontal_relations(G, node_infos, executors, nodes, entities, functions, mongo=mongo)

    log(f"{container_role}: {len(executors)} EX, {len(nodes)} N, "
        f"{len(entities)} E, {len(functions)} F, {G.number_of_edges()} edges")

    return {
        "executors": executors,
        "nodes": nodes,
        "entities": entities,
        "functions": functions,
        "G": G,
        "mongo": mongo,
        "role": container_role,
    }


# ---------------------------------------------------------------------------
# ID remapping
# ---------------------------------------------------------------------------

def remap_ids(model, offset):
    """Apply ID offset to all vertices in a container model.

    Mutates vertex IDs in-place and returns a mapping old_id → new_id.
    """
    remap = {}

    def _remap_dict(d):
        new_d = {}
        for old_id, v in d.items():
            new_id = old_id + offset
            remap[old_id] = new_id
            v.id_v = new_id
            v.Z_v = [child + offset for child in v.Z_v]
            new_d[new_id] = v
        return new_d

    model["executors"] = _remap_dict(model["executors"])
    model["nodes"] = _remap_dict(model["nodes"])
    model["entities"] = _remap_dict(model["entities"])
    model["functions"] = _remap_dict(model["functions"])

    # Remap graph
    import networkx as nx
    G = model["G"]
    mapping = {old: old + offset for old in G.nodes()}
    model["G"] = nx.relabel_nodes(G, mapping)

    return remap


# ---------------------------------------------------------------------------
# External boundary matching
# ---------------------------------------------------------------------------

def match_external_boundaries(container_models):
    """Match external entities across containers and create cross-container edges.

    For each ext entity in container A with topic T:
      - Search all other containers for a real (non-ext) entity with matching topic
      - If found: record a cross-container edge, mark ext for removal
      - If not found: keep ext entity

    Returns:
        cross_edges: list of (src_id, dst_id, edge_attrs) for cross-container comm
        remove_ext: set of ext entity IDs to remove
    """
    # Build indexes: topic → [(container_role, entity_id, entity, is_pub)]
    pub_index = {}  # topic → [(role, entity_id, entity)]
    sub_index = {}  # topic → [(role, entity_id, entity)]
    ext_pubs = {}   # topic → [(role, entity_id, entity)]
    ext_subs = {}   # topic → [(role, entity_id, entity)]

    for role, m in container_models.items():
        for e_id, e in m["entities"].items():
            if e.t_v != "E":
                continue
            is_ext = e.A_v.get("external", False)
            for aspect in e.A_v.get("aspects", []):
                topic = aspect.get("topic")
                if not topic:
                    continue
                if aspect["aspect"] == "pub":
                    target = ext_pubs if is_ext else pub_index
                    target.setdefault(topic, []).append((role, e_id, e))
                elif aspect["aspect"] == "sub":
                    target = ext_subs if is_ext else sub_index
                    target.setdefault(topic, []).append((role, e_id, e))

    cross_edges = []
    remove_ext = set()
    matched_topics = set()

    # Match: ext publisher in A ↔ real subscriber in B (different containers)
    # This means A has a subscriber whose data comes from outside → ext pub was created
    # If B has a real publisher for that topic → cross-container edge B_pub → A_sub
    for topic, ext_list in ext_pubs.items():
        real_pubs = pub_index.get(topic, [])
        if not real_pubs:
            continue
        for ext_role, ext_id, ext_e in ext_list:
            for real_role, real_id, real_e in real_pubs:
                if ext_role == real_role:
                    continue  # same container, skip
                # Found cross-container match!
                # The ext entity in ext_role was a placeholder for real_role's publisher
                # Find the subscriber entity that the ext was connected to
                cross_edges.append((real_id, ext_id, {
                    "rel": "comm", "level": "L2", "nature": "msg",
                    "topic": topic, "cross_container": True,
                    "src_container": real_role, "dst_container": ext_role,
                }))
                remove_ext.add(ext_id)
                matched_topics.add(topic)

    # Match: ext subscriber in A ↔ real publisher in B
    for topic, ext_list in ext_subs.items():
        real_subs = sub_index.get(topic, [])
        if not real_subs:
            continue
        for ext_role, ext_id, ext_e in ext_list:
            for real_role, real_id, real_e in real_subs:
                if ext_role == real_role:
                    continue
                cross_edges.append((ext_id, real_id, {
                    "rel": "comm", "level": "L2", "nature": "msg",
                    "topic": topic, "cross_container": True,
                    "src_container": ext_role, "dst_container": real_role,
                }))
                remove_ext.add(ext_id)
                matched_topics.add(topic)

    log(f"External matching: {len(matched_topics)} topics matched, "
        f"{len(cross_edges)} cross-container edges, "
        f"{len(remove_ext)} ext entities to remove")

    return cross_edges, remove_ext


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_models(container_models, compose_name):
    """Merge per-container models into a unified graph with L-1 layer.

    1. Create Container vertices (L-1)
    2. Remap vertex IDs (per-container offset)
    3. Match external boundaries across containers
    4. Build unified graph
    """
    import networkx as nx

    log("=== Merging models ===")

    # Phase 1: Remap IDs
    all_remaps = {}
    for i, (role, m) in enumerate(container_models.items()):
        offset = i * ID_OFFSET
        remap = remap_ids(m, offset)
        all_remaps[role] = remap
        log(f"  {role}: offset={offset}, {len(remap)} vertices remapped")

    # Phase 2: Create Container vertices (L-1)
    container_counter = count(-(len(container_models)))  # negative IDs for L-1
    container_vertices = {}
    for role, m in container_models.items():
        c_id = next(container_counter)
        c_A = {"label": role, "role": role}
        c_v = FishVertex("CN", c_id, c_A, [], -1)  # CN = Container Node, level -1
        # Z_v = executor IDs from this container
        c_v.Z_v = list(m["executors"].keys())
        container_vertices[c_id] = c_v
        log(f"  Container [{c_id}] {role}: {len(c_v.Z_v)} executors")

    # Phase 3: Match external boundaries
    cross_edges, remove_ext = match_external_boundaries(container_models)

    # Phase 4: Build unified graph
    unified_G = nx.DiGraph()

    # Add container vertices
    for c_id, c_v in container_vertices.items():
        unified_G.add_node(c_id, v=c_v)

    # Add container → executor containment edges
    for c_id, c_v in container_vertices.items():
        for ex_id in c_v.Z_v:
            unified_G.add_edge(c_id, ex_id, rel="contains", level="L-1_L0")

    # Merge per-container graphs
    for role, m in container_models.items():
        G = m["G"]
        for node_id, data in G.nodes(data=True):
            if node_id in remove_ext:
                continue  # skip matched ext entities
            unified_G.add_node(node_id, **data)
        for u, v, data in G.edges(data=True):
            if u in remove_ext or v in remove_ext:
                continue
            unified_G.add_edge(u, v, **data)

    # Add cross-container edges
    for src, dst, attrs in cross_edges:
        # src/dst might be ext entities being removed — find the actual
        # subscriber/publisher they were connected to
        unified_G.add_edge(src, dst, **attrs)

    # Aggregate cross-container edges to L-1 level
    # For each cross-container L2 edge, find which containers src/dst belong to
    container_of = {}  # vertex_id → container_id
    for c_id, c_v in container_vertices.items():
        for ex_id in c_v.Z_v:
            container_of[ex_id] = c_id
            # Also map all descendants
            for role, m in container_models.items():
                if c_v.A_v["role"] == role:
                    for n_id in m["nodes"]:
                        container_of[n_id] = c_id
                    for e_id in m["entities"]:
                        container_of[e_id] = c_id
                    for f_id in m["functions"]:
                        container_of[f_id] = c_id

    # L-1 edges: aggregate cross-container comm
    l_minus1_pairs = {}  # (src_container, dst_container) → {topics, count}
    for src, dst, attrs in cross_edges:
        src_c = container_of.get(src)
        dst_c = container_of.get(dst)
        if src_c is not None and dst_c is not None and src_c != dst_c:
            key = (src_c, dst_c)
            if key not in l_minus1_pairs:
                l_minus1_pairs[key] = {"topics": set(), "count": 0}
            l_minus1_pairs[key]["count"] += 1
            topic = attrs.get("topic")
            if topic:
                l_minus1_pairs[key]["topics"].add(topic)

    for (src_c, dst_c), info in l_minus1_pairs.items():
        unified_G.add_edge(src_c, dst_c,
                           rel="comm", level="L-1",
                           nature="msg", cross_container=True,
                           topics=list(info["topics"]),
                           comm_count=info["count"])

    v_count = unified_G.number_of_nodes()
    e_count = unified_G.number_of_edges()
    log(f"Unified graph: {v_count} vertices, {e_count} edges")
    log(f"  Containers: {len(container_vertices)}")
    log(f"  Cross-container edges: {len(cross_edges)} L2 + {len(l_minus1_pairs)} L-1")

    return unified_G, container_vertices


# ---------------------------------------------------------------------------
# JSON export (extended for L-1)
# ---------------------------------------------------------------------------

def export_compose_json(G, container_vertices, output_path):
    """Export unified graph as JSON for fish_viz.html.

    Extends the standard export with L-1 container nodes.
    """
    nodes = []
    edges = []

    for node_id, data in G.nodes(data=True):
        v = data.get("v")
        if v:
            n = {
                "id": v.id_v,
                "type": v.t_v,
                "label": v.A_v.get("label", ""),
                "level": v.level,
                "children": v.Z_v,
            }
            # Copy relevant attributes
            for key in ["pid", "full_name", "etype", "ptype", "external",
                        "role", "aspects", "gpu_node"]:
                val = v.A_v.get(key)
                if val is not None:
                    n[key] = val
            nodes.append(n)
        elif node_id in container_vertices:
            cv = container_vertices[node_id]
            nodes.append({
                "id": cv.id_v,
                "type": "CN",
                "label": cv.A_v["label"],
                "level": -1,
                "children": cv.Z_v,
                "role": cv.A_v.get("role", ""),
            })

    for u, v, data in G.edges(data=True):
        edge = {"source": u, "target": v}
        for key in ["rel", "level", "nature", "topic", "service",
                     "msg_type", "avg_rate_hz", "external", "cross_container",
                     "src_container", "dst_container", "topics", "comm_count"]:
            val = data.get(key)
            if val is not None:
                if isinstance(val, set):
                    val = list(val)
                edge[key] = val
        edges.append(edge)

    result = {"nodes": nodes, "edges": edges}
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, default=str)

    log(f"JSON: {len(nodes)} nodes, {len(edges)} edges → {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def compose_session(session_dir, skip_ingest=False, tz_name="Europe/Amsterdam"):
    session_dir = os.path.abspath(session_dir)
    compose_name = os.path.basename(session_dir)

    log(f"=== FISH Compose: {compose_name} ===")

    # Read manifest
    manifest_path = os.path.join(session_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        log(f"ERROR: manifest.json not found in {session_dir}")
        return

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Find FISH-enabled containers
    fish_containers = {}
    for alias, cinfo in manifest.get("containers", {}).items():
        if cinfo.get("fish") and cinfo.get("trace_session"):
            role = cinfo.get("role", alias)
            container_dir = os.path.join(session_dir, role)
            if os.path.isdir(container_dir):
                fish_containers[role] = container_dir
            else:
                log(f"  SKIP {role}: directory not found")

    log(f"FISH containers: {list(fish_containers.keys())}")

    if not fish_containers:
        log("No FISH containers found. Nothing to compose.")
        return

    # Extract per-container models
    t0 = time.time()
    container_models = {}

    for role, container_dir in fish_containers.items():
        if skip_ingest:
            # Assume already ingested — just connect and extract
            db_name = role
            log(f"--- Container: {role} (skip ingest, DB: {db_name}) ---")
            mongo = connect_mongo(db_name)

            import model
            model.vertex_counter = count(1)

            executors, nodes = identify_executors(mongo)
            entities = identify_entities(mongo, nodes)
            functions = identify_callbacks(mongo, nodes, entities, executors)
            attribute_aspects(mongo, executors, nodes, entities)
            G = create_graph_add_vertical_relations(executors, nodes, entities, functions)
            node_infos = get_node_infos(mongo)
            G = add_horizontal_relations(G, node_infos, executors, nodes, entities, functions, mongo=mongo)

            container_models[role] = {
                "executors": executors, "nodes": nodes,
                "entities": entities, "functions": functions,
                "G": G, "mongo": mongo, "role": role,
            }
            log(f"{role}: {len(executors)} EX, {len(nodes)} N, "
                f"{len(entities)} E, {len(functions)} F")
        else:
            container_models[role] = extract_container_model(
                container_dir, role, compose_name, tz_name)

    extract_time = time.time() - t0
    log(f"Extraction done in {extract_time:.1f}s")

    # Merge
    t1 = time.time()
    unified_G, container_vertices = merge_models(container_models, compose_name)
    merge_time = time.time() - t1

    # Export
    json_path = os.path.join(session_dir, "fish_graph.json")
    export_compose_json(unified_G, container_vertices, json_path)

    # Report
    report_lines = [
        f"FISH Compose Report: {compose_name}",
        f"{'='*60}",
        f"Containers: {len(fish_containers)}",
    ]
    for role, m in container_models.items():
        report_lines.append(
            f"  {role}: {len(m['executors'])} EX, {len(m['nodes'])} N, "
            f"{len(m['entities'])} E, {len(m['functions'])} F")
    report_lines.extend([
        f"Unified graph: {unified_G.number_of_nodes()} vertices, "
        f"{unified_G.number_of_edges()} edges",
        f"Extraction: {extract_time:.1f}s, Merge: {merge_time:.1f}s",
        f"Output: {json_path}",
    ])
    report = "\n".join(report_lines)

    report_path = os.path.join(session_dir, "compose_report.txt")
    with open(report_path, "w") as f:
        f.write(report + "\n")

    print(report)
    log(f"=== Compose complete ({extract_time + merge_time:.1f}s) ===")


def main():
    ap = argparse.ArgumentParser(
        description="FISH Compose — merge multi-container traces into unified graph")
    ap.add_argument("session_dir", help="Path to compose session directory")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Skip ingest (assume already ingested)")
    ap.add_argument("--tz", default="Europe/Amsterdam",
                    help="Timezone for trace timestamps")
    args = ap.parse_args()

    if not os.path.isdir(args.session_dir):
        print(f"Error: {args.session_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    compose_session(args.session_dir, skip_ingest=args.skip_ingest, tz_name=args.tz)


if __name__ == "__main__":
    main()
