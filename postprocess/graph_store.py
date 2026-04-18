"""graph_store.py — persistent, mutable fish graph in MongoDB.

Collections (stored in one meta DB, separate from per-session trace DBs):
  fish_graphs    — one doc per session_name (metadata + stats)
  fish_nodes     — one doc per (session_name, node_id)
  fish_edges     — one doc per (session_name, source, target)
  fish_mutations — append-only log of changes

Public API:
  save_graph / load_graph / delete_graph / graph_exists / get_metadata / list_graphs
  update_node / update_edge / add_node / add_edge / remove_node / remove_edge
  to_viz_json / export_json
  ensure_indexes / connect

CLI:
  python graph_store.py list
  python graph_store.py info    <session>
  python graph_store.py export  <session> [-o fish_graph.json]
  python graph_store.py delete  <session> --yes
  python graph_store.py mutations <session> [-n 20]
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import networkx as nx
from pymongo import MongoClient, ASCENDING

MONGO_URI = os.environ.get("FISH_MONGO_URI", "mongodb://localhost:27017")
META_DB = os.environ.get("FISH_META_DB", "fish_meta")

GRAPHS_COLL = "fish_graphs"
NODES_COLL = "fish_nodes"
EDGES_COLL = "fish_edges"
MUTATIONS_COLL = "fish_mutations"

# Attribute keys promoted from FishVertex.A_v / edge data dict to top-level
# fields in MongoDB docs. Keeping them flat makes $set updates and queries
# straightforward.
NODE_ATTR_KEYS = [
    "pid", "full_name", "etype", "ptype", "external",
    "aspects", "gpu_node", "action_name", "action_role",
    "split", "period_ns",
    # Scheduler introspection (from fish_executor_init / fish_callback_group_init / ...)
    "executor_addr", "executor_type", "num_threads", "cb_groups",
    "extra_executors",
    "callback_group",
]
EDGE_ATTR_KEYS = [
    "rel", "level", "nature", "topic", "service",
    "msg_type", "avg_rate_hz", "external", "direction",
    "topics", "comm_count", "cross_container",
    "src_container", "dst_container", "tau",
]


@dataclass
class Vertex:
    """FishVertex-compatible (t_v, id_v, A_v, Z_v, level) — used by load_graph
    so callers can do v.t_v, v.A_v['label'], etc. without importing model."""
    t_v: str
    id_v: int
    A_v: dict = field(default_factory=dict)
    Z_v: list = field(default_factory=list)
    level: int = 0


# ---------------------------------------------------------------------------
# Connection & indexes
# ---------------------------------------------------------------------------

def connect(uri: str = MONGO_URI, db: str = META_DB):
    return MongoClient(uri)[db]


def ensure_indexes(db=None):
    if db is None:
        db = connect()
    db[GRAPHS_COLL].create_index([("session_name", ASCENDING)], unique=True)
    db[NODES_COLL].create_index(
        [("session_name", ASCENDING), ("node_id", ASCENDING)], unique=True
    )
    db[EDGES_COLL].create_index(
        [("session_name", ASCENDING),
         ("source", ASCENDING), ("target", ASCENDING)],
        unique=True,
    )
    db[MUTATIONS_COLL].create_index(
        [("session_name", ASCENDING), ("timestamp", ASCENDING)]
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _sanitize(val):
    if isinstance(val, set):
        return list(val)
    return val


def _serialize_node(session_name, v):
    """FishVertex (or plain object with .t_v / .A_v / ...) → node doc."""
    t_v = v.t_v
    id_v = v.id_v
    A_v = v.A_v or {}
    Z_v = v.Z_v or []
    lvl = v.level
    doc = {
        "session_name": session_name,
        "node_id": id_v,
        "type": t_v,
        "label": A_v.get("label", ""),
        "level": lvl,
        "children": list(Z_v),
        "updated_at": _now(),
    }
    for key in NODE_ATTR_KEYS:
        val = A_v.get(key)
        if val is not None:
            doc[key] = _sanitize(val)
    return doc


def _serialize_edge(session_name, u, v, data):
    doc = {
        "session_name": session_name,
        "source": u,
        "target": v,
        "updated_at": _now(),
    }
    for key in EDGE_ATTR_KEYS:
        val = data.get(key)
        if val is not None:
            doc[key] = _sanitize(val)
    return doc


def _doc_to_vertex(doc):
    A_v = {"label": doc.get("label", "")}
    for key in NODE_ATTR_KEYS:
        if key in doc:
            A_v[key] = doc[key]
    return Vertex(
        t_v=doc["type"],
        id_v=doc["node_id"],
        A_v=A_v,
        Z_v=list(doc.get("children", [])),
        level=doc.get("level", 0),
    )


def _log_mutation(db, session_name, op, target=None, changes=None,
                  actor=None, note=None):
    db[MUTATIONS_COLL].insert_one({
        "session_name": session_name,
        "timestamp": _now(),
        "op": op,
        "target": target,
        "changes": changes,
        "actor": actor,
        "note": note,
    })


# ---------------------------------------------------------------------------
# Metadata / query
# ---------------------------------------------------------------------------

def graph_exists(session_name, db=None):
    if db is None:
        db = connect()
    return db[GRAPHS_COLL].find_one(
        {"session_name": session_name}, {"_id": 1}
    ) is not None


def get_metadata(session_name, db=None):
    if db is None:
        db = connect()
    return db[GRAPHS_COLL].find_one({"session_name": session_name})


def list_graphs(db=None):
    if db is None:
        db = connect()
    return list(db[GRAPHS_COLL].find({}, {"_id": 0}).sort("updated_at", -1))


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_graph(G: nx.DiGraph, session_name: str, *,
               source_trace: str | None = None,
               container_role: str | None = None,
               is_composed: bool = False,
               replace: bool = True,
               actor: str = "model_improved",
               note: str | None = None,
               db=None) -> dict:
    """Persist the full graph under session_name. Overwrites by default."""
    if db is None:
        db = connect()
    ensure_indexes(db)

    exists = graph_exists(session_name, db)
    if exists and not replace:
        raise ValueError(
            f"Graph for session '{session_name}' already exists "
            f"(pass replace=True to overwrite)."
        )
    if exists:
        db[NODES_COLL].delete_many({"session_name": session_name})
        db[EDGES_COLL].delete_many({"session_name": session_name})

    node_docs = []
    for _nid, data in G.nodes(data=True):
        v = data.get("v")
        if v is None:
            continue
        node_docs.append(_serialize_node(session_name, v))
    if node_docs:
        db[NODES_COLL].insert_many(node_docs)

    edge_docs = [
        _serialize_edge(session_name, u, v, data)
        for u, v, data in G.edges(data=True)
    ]
    if edge_docs:
        db[EDGES_COLL].insert_many(edge_docs)

    stats = {
        "num_nodes": len(node_docs),
        "num_edges": len(edge_docs),
        "num_externals": sum(1 for d in node_docs if d.get("external")),
    }

    now = _now()
    meta = {
        "session_name": session_name,
        "source_trace": source_trace,
        "container_role": container_role,
        "is_composed": is_composed,
        "stats": stats,
        "updated_at": now,
    }
    if exists:
        db[GRAPHS_COLL].update_one(
            {"session_name": session_name}, {"$set": meta}
        )
    else:
        meta["created_at"] = now
        db[GRAPHS_COLL].insert_one(dict(meta))

    _log_mutation(db, session_name, "save_graph",
                  changes={"stats": stats,
                           "source_trace": source_trace,
                           "container_role": container_role,
                           "is_composed": is_composed,
                           "replaced": exists},
                  actor=actor, note=note)
    return meta


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_graph(session_name: str, db=None) -> nx.DiGraph:
    """Rebuild an nx.DiGraph with `v=<Vertex>` on each node and edge attrs
    matching model_improved.py's conventions."""
    if db is None:
        db = connect()
    if not graph_exists(session_name, db):
        raise KeyError(f"No graph stored for session '{session_name}'.")

    G = nx.DiGraph()
    for doc in db[NODES_COLL].find({"session_name": session_name}):
        vx = _doc_to_vertex(doc)
        G.add_node(vx.id_v, v=vx)
    for doc in db[EDGES_COLL].find({"session_name": session_name}):
        attrs = {k: doc[k] for k in EDGE_ATTR_KEYS if k in doc}
        G.add_edge(doc["source"], doc["target"], **attrs)
    return G


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def update_node(session_name, node_id, *,
                attrs: dict | None = None,
                label: str | None = None,
                children: list | None = None,
                level: int | None = None,
                actor: str = "manual",
                note: str | None = None,
                db=None) -> int:
    """Update a node. `attrs` entries go to top-level fields."""
    if db is None:
        db = connect()
    upd = {"updated_at": _now()}
    if label is not None:
        upd["label"] = label
    if children is not None:
        upd["children"] = list(children)
    if level is not None:
        upd["level"] = level
    if attrs:
        for k, val in attrs.items():
            upd[k] = _sanitize(val)

    r = db[NODES_COLL].update_one(
        {"session_name": session_name, "node_id": node_id},
        {"$set": upd},
    )
    if r.matched_count:
        db[GRAPHS_COLL].update_one(
            {"session_name": session_name},
            {"$set": {"updated_at": _now()}},
        )
        _log_mutation(db, session_name, "update_node",
                      target={"kind": "node", "id": node_id},
                      changes=upd, actor=actor, note=note)
    return r.matched_count


def update_edge(session_name, source, target, *,
                attrs: dict | None = None,
                actor: str = "manual",
                note: str | None = None,
                db=None) -> int:
    if db is None:
        db = connect()
    upd = {"updated_at": _now()}
    if attrs:
        for k, val in attrs.items():
            upd[k] = _sanitize(val)

    r = db[EDGES_COLL].update_one(
        {"session_name": session_name, "source": source, "target": target},
        {"$set": upd},
    )
    if r.matched_count:
        db[GRAPHS_COLL].update_one(
            {"session_name": session_name},
            {"$set": {"updated_at": _now()}},
        )
        _log_mutation(db, session_name, "update_edge",
                      target={"kind": "edge", "id": [source, target]},
                      changes=upd, actor=actor, note=note)
    return r.matched_count


def add_node(session_name, vertex, *,
             actor: str = "manual", note: str | None = None,
             db=None) -> None:
    if db is None:
        db = connect()
    doc = _serialize_node(session_name, vertex)
    db[NODES_COLL].insert_one(doc)
    db[GRAPHS_COLL].update_one(
        {"session_name": session_name},
        {"$set": {"updated_at": _now()},
         "$inc": {"stats.num_nodes": 1}},
    )
    _log_mutation(db, session_name, "add_node",
                  target={"kind": "node", "id": doc["node_id"]},
                  changes=doc, actor=actor, note=note)


def add_edge(session_name, source, target, attrs: dict | None = None,
             *, actor: str = "manual", note: str | None = None, db=None) -> None:
    if db is None:
        db = connect()
    doc = _serialize_edge(session_name, source, target, attrs or {})
    db[EDGES_COLL].insert_one(doc)
    db[GRAPHS_COLL].update_one(
        {"session_name": session_name},
        {"$set": {"updated_at": _now()},
         "$inc": {"stats.num_edges": 1}},
    )
    _log_mutation(db, session_name, "add_edge",
                  target={"kind": "edge", "id": [source, target]},
                  changes=doc, actor=actor, note=note)


def remove_node(session_name, node_id, *,
                actor: str = "manual", note: str | None = None,
                db=None) -> int:
    if db is None:
        db = connect()
    r = db[NODES_COLL].delete_one(
        {"session_name": session_name, "node_id": node_id}
    )
    # cascade edges
    er = db[EDGES_COLL].delete_many({
        "session_name": session_name,
        "$or": [{"source": node_id}, {"target": node_id}],
    })
    if r.deleted_count:
        _log_mutation(db, session_name, "remove_node",
                      target={"kind": "node", "id": node_id},
                      changes={"cascaded_edges": er.deleted_count},
                      actor=actor, note=note)
    return r.deleted_count


def remove_edge(session_name, source, target, *,
                actor: str = "manual", note: str | None = None,
                db=None) -> int:
    if db is None:
        db = connect()
    r = db[EDGES_COLL].delete_one(
        {"session_name": session_name, "source": source, "target": target}
    )
    if r.deleted_count:
        _log_mutation(db, session_name, "remove_edge",
                      target={"kind": "edge", "id": [source, target]},
                      actor=actor, note=note)
    return r.deleted_count


def delete_graph(session_name, *, actor: str = "manual",
                 note: str | None = None, db=None) -> bool:
    if db is None:
        db = connect()
    if not graph_exists(session_name, db):
        return False
    db[NODES_COLL].delete_many({"session_name": session_name})
    db[EDGES_COLL].delete_many({"session_name": session_name})
    db[GRAPHS_COLL].delete_one({"session_name": session_name})
    _log_mutation(db, session_name, "delete_graph",
                  actor=actor, note=note)
    return True


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def remap_graph(G: nx.DiGraph, offset: int) -> nx.DiGraph:
    """Apply an ID offset to every node in-place-ish (relabels a copy).

    Both the networkx node IDs and each vertex's own .id_v / .Z_v get shifted,
    keeping Vertex objects consistent with graph keys. Used by fish_compose
    to avoid ID collisions when merging per-container graphs.
    """
    mapping = {nid: nid + offset for nid in G.nodes()}
    G2 = nx.relabel_nodes(G, mapping, copy=True)
    for nid, data in G2.nodes(data=True):
        v = data.get("v")
        if v is not None:
            v.id_v = nid
            v.Z_v = [c + offset for c in v.Z_v]
    return G2


def to_viz_json(session_name, db=None) -> dict:
    """Return a dict in the fish_viz.html JSON format."""
    if db is None:
        db = connect()
    if not graph_exists(session_name, db):
        raise KeyError(f"No graph stored for session '{session_name}'.")

    nodes_out = []
    for doc in db[NODES_COLL].find({"session_name": session_name}):
        n = {
            "id": doc["node_id"],
            "type": doc["type"],
            "label": doc.get("label", ""),
            "level": doc.get("level", 0),
            "children": list(doc.get("children", [])),
        }
        for key in NODE_ATTR_KEYS:
            if key in doc:
                n[key] = doc[key]
        nodes_out.append(n)

    edges_out = []
    for doc in db[EDGES_COLL].find({"session_name": session_name}):
        e = {"source": doc["source"], "target": doc["target"]}
        for key in EDGE_ATTR_KEYS:
            if key in doc:
                e[key] = doc[key]
        edges_out.append(e)

    return {"nodes": nodes_out, "edges": edges_out}


def export_json(session_name, output_path, db=None) -> None:
    import json
    with open(output_path, "w") as f:
        json.dump(to_viz_json(session_name, db), f, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="FISH graph_store CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List stored graphs")

    p = sub.add_parser("info", help="Show graph metadata")
    p.add_argument("session")

    p = sub.add_parser("export", help="Export a stored graph to JSON")
    p.add_argument("session")
    p.add_argument("-o", "--output", default="fish_graph.json")

    p = sub.add_parser("delete", help="Delete a stored graph")
    p.add_argument("session")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("mutations", help="Show mutation log")
    p.add_argument("session")
    p.add_argument("-n", "--limit", type=int, default=20)

    args = ap.parse_args()
    db = connect()

    if args.cmd == "list":
        rows = list_graphs(db)
        if not rows:
            print("(no graphs stored)")
            return
        for g in rows:
            s = g.get("stats", {}) or {}
            print(f"{g['session_name']:40s} "
                  f"nodes={s.get('num_nodes', 0):<5} "
                  f"edges={s.get('num_edges', 0):<5} "
                  f"composed={g.get('is_composed', False)!s:<5} "
                  f"updated={g.get('updated_at')}")
    elif args.cmd == "info":
        meta = get_metadata(args.session, db)
        if not meta:
            print(f"No graph for session '{args.session}'", file=sys.stderr)
            sys.exit(1)
        meta["_id"] = str(meta["_id"])
        print(json.dumps(meta, indent=2, default=str))
    elif args.cmd == "export":
        export_json(args.session, args.output, db)
        print(f"Exported → {args.output}")
    elif args.cmd == "delete":
        if not args.yes:
            print("Use --yes to confirm deletion", file=sys.stderr)
            sys.exit(1)
        ok = delete_graph(args.session, actor="cli", db=db)
        print("Deleted" if ok else "No such graph")
    elif args.cmd == "mutations":
        cur = (db[MUTATIONS_COLL]
               .find({"session_name": args.session})
               .sort("timestamp", -1)
               .limit(args.limit))
        for m in cur:
            m["_id"] = str(m["_id"])
            print(json.dumps(m, default=str))


if __name__ == "__main__":
    _cli()
