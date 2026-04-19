"""graph_store.py — persistent, mutable fish graph in MongoDB.

One Mongo database per session. Graph data lives in four collections
inside that database, scope-qualified:

  graph_meta        — one doc per scope (per-container + composed)
  graph_nodes       — one doc per (scope, node_id)
  graph_edges       — one doc per (scope, source, target)
  graph_mutations   — append-only audit log of changes

`scope` values:
  "<role>"           — per-container graph (e.g. "aircraft")
  "__composed__"     — composed multi-container graph from fish_compose
  "__main__"         — default scope for standalone (single-container) runs

Public API (all functions take (db_name, scope) as the first two args):

  save_graph / load_graph / delete_graph / graph_exists / get_metadata
  list_graphs(db_name)   — scopes within one session DB
  list_sessions()        — all session DBs (names starting with fish_)
  update_node / update_edge / add_node / add_edge / remove_node / remove_edge
  to_viz_json / export_json
  remap_graph            — utility for ID offsetting (compose)
  ensure_indexes         — idempotent index setup on a session DB

CLI:
  python3 graph_store.py sessions                                   # list session DBs
  python3 graph_store.py list     <session>                         # scopes in a session
  python3 graph_store.py info     <session> <scope>
  python3 graph_store.py export   <session> <scope> [-o graph.json]
  python3 graph_store.py delete   <session> <scope> --yes
  python3 graph_store.py drop     <session> --yes                   # drop whole DB
  python3 graph_store.py mutations <session> <scope> [-n 20]

See notes/db_structure.txt for the full session-per-DB design.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import networkx as nx
from pymongo import MongoClient, ASCENDING


MONGO_URI = os.environ.get("FISH_MONGO_URI", "mongodb://localhost:27017")

GRAPHS_COLL     = "graph_meta"
NODES_COLL      = "graph_nodes"
EDGES_COLL      = "graph_edges"
MUTATIONS_COLL  = "graph_mutations"

COMPOSED_SCOPE  = "__composed__"
STANDALONE_SCOPE = "__main__"

# Attribute keys promoted from FishVertex.A_v / edge data to top-level
# fields in MongoDB docs. Flat keys keep $set updates and indexable
# queries straightforward.
NODE_ATTR_KEYS = [
    "pid", "full_name", "etype", "ptype", "external",
    "aspects", "gpu_node", "action_name", "action_role",
    "split", "period_ns",
    # Scheduler introspection
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
    """FishVertex-compatible (t_v, id_v, A_v, Z_v, level) — used by
    load_graph so callers can do v.t_v, v.A_v['label'], etc. without
    importing model_improved."""
    t_v: str
    id_v: int
    A_v: dict = field(default_factory=dict)
    Z_v: list = field(default_factory=list)
    level: int = 0


# ---------------------------------------------------------------------------
# Connection & indexes
# ---------------------------------------------------------------------------

def connect(db_name: str, uri: str = MONGO_URI):
    """Return the pymongo Database for a given session."""
    return MongoClient(uri)[db_name]


def _resolve_db(db_name, db):
    """Helper: given either a db handle or a name, yield (name, db)."""
    if db is not None:
        return db.name, db
    if db_name is None:
        raise ValueError("graph_store: either db_name or db must be provided")
    return db_name, connect(db_name)


def ensure_indexes(db_name: str | None = None, *, db=None):
    _, db = _resolve_db(db_name, db)
    db[GRAPHS_COLL].create_index([("scope", ASCENDING)], unique=True)
    db[NODES_COLL].create_index(
        [("scope", ASCENDING), ("node_id", ASCENDING)], unique=True
    )
    db[EDGES_COLL].create_index(
        [("scope", ASCENDING), ("source", ASCENDING), ("target", ASCENDING)],
        unique=True,
    )
    db[MUTATIONS_COLL].create_index(
        [("scope", ASCENDING), ("timestamp", ASCENDING)]
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


def _serialize_node(scope, v):
    t_v = v.t_v
    id_v = v.id_v
    A_v = v.A_v or {}
    Z_v = v.Z_v or []
    lvl = v.level
    doc = {
        "scope": scope,
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


def _serialize_edge(scope, u, v, data):
    doc = {
        "scope": scope,
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


def _log_mutation(db, scope, op, target=None, changes=None,
                  actor=None, note=None):
    db[MUTATIONS_COLL].insert_one({
        "scope": scope,
        "timestamp": _now(),
        "op": op,
        "target": target,
        "changes": changes,
        "actor": actor,
        "note": note,
    })


# ---------------------------------------------------------------------------
# Query / metadata
# ---------------------------------------------------------------------------

def graph_exists(db_name: str | None, scope: str, *, db=None) -> bool:
    _, db = _resolve_db(db_name, db)
    return db[GRAPHS_COLL].find_one(
        {"scope": scope}, {"_id": 1}
    ) is not None


def get_metadata(db_name: str | None, scope: str, *, db=None) -> dict | None:
    _, db = _resolve_db(db_name, db)
    return db[GRAPHS_COLL].find_one({"scope": scope})


def list_graphs(db_name: str | None = None, *, db=None) -> list[dict]:
    """List all scopes (graph_meta docs) inside a session DB."""
    _, db = _resolve_db(db_name, db)
    return list(db[GRAPHS_COLL].find({}, {"_id": 0}).sort("updated_at", -1))


def list_sessions(uri: str = MONGO_URI) -> list[str]:
    """List all MongoDB databases that look like FISH session DBs."""
    client = MongoClient(uri)
    names = client.list_database_names()
    return sorted(n for n in names if n.startswith("fish_"))


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_graph(G: nx.DiGraph, db_name: str, scope: str, *,
               source_trace: str | None = None,
               container_role: str | None = None,
               is_composed: bool = False,
               replace: bool = True,
               actor: str = "model_improved",
               note: str | None = None,
               db=None) -> dict:
    """Persist the full graph under (db_name, scope).

    Overwrites any existing graph for the same scope when replace=True
    (default).
    """
    _, db = _resolve_db(db_name, db)
    ensure_indexes(db=db)

    exists = graph_exists(None, scope, db=db)
    if exists and not replace:
        raise ValueError(
            f"Graph for scope '{scope}' already exists in '{db.name}' "
            f"(pass replace=True to overwrite)."
        )
    if exists:
        db[NODES_COLL].delete_many({"scope": scope})
        db[EDGES_COLL].delete_many({"scope": scope})

    node_docs = []
    for _nid, data in G.nodes(data=True):
        v = data.get("v")
        if v is None:
            continue
        node_docs.append(_serialize_node(scope, v))
    if node_docs:
        db[NODES_COLL].insert_many(node_docs)

    edge_docs = [
        _serialize_edge(scope, u, v, data)
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
        "scope": scope,
        "source_trace": source_trace,
        "container_role": container_role,
        "is_composed": is_composed,
        "stats": stats,
        "updated_at": now,
    }
    if exists:
        db[GRAPHS_COLL].update_one({"scope": scope}, {"$set": meta})
    else:
        meta["created_at"] = now
        db[GRAPHS_COLL].insert_one(dict(meta))

    _log_mutation(db, scope, "save_graph",
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

def load_graph(db_name: str | None, scope: str, *, db=None) -> nx.DiGraph:
    """Rebuild an nx.DiGraph from (db_name, scope)."""
    _, db = _resolve_db(db_name, db)
    if not graph_exists(None, scope, db=db):
        raise KeyError(f"No graph stored for scope '{scope}' in '{db.name}'.")

    G = nx.DiGraph()
    for doc in db[NODES_COLL].find({"scope": scope}):
        vx = _doc_to_vertex(doc)
        G.add_node(vx.id_v, v=vx)
    for doc in db[EDGES_COLL].find({"scope": scope}):
        attrs = {k: doc[k] for k in EDGE_ATTR_KEYS if k in doc}
        G.add_edge(doc["source"], doc["target"], **attrs)
    return G


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def update_node(db_name: str | None, scope: str, node_id, *,
                attrs: dict | None = None,
                label: str | None = None,
                children: list | None = None,
                level: int | None = None,
                actor: str = "manual",
                note: str | None = None,
                db=None) -> int:
    _, db = _resolve_db(db_name, db)
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
        {"scope": scope, "node_id": node_id},
        {"$set": upd},
    )
    if r.matched_count:
        db[GRAPHS_COLL].update_one(
            {"scope": scope}, {"$set": {"updated_at": _now()}},
        )
        _log_mutation(db, scope, "update_node",
                      target={"kind": "node", "id": node_id},
                      changes=upd, actor=actor, note=note)
    return r.matched_count


def update_edge(db_name: str | None, scope: str, source, target, *,
                attrs: dict | None = None,
                actor: str = "manual",
                note: str | None = None,
                db=None) -> int:
    _, db = _resolve_db(db_name, db)
    upd = {"updated_at": _now()}
    if attrs:
        for k, val in attrs.items():
            upd[k] = _sanitize(val)

    r = db[EDGES_COLL].update_one(
        {"scope": scope, "source": source, "target": target},
        {"$set": upd},
    )
    if r.matched_count:
        db[GRAPHS_COLL].update_one(
            {"scope": scope}, {"$set": {"updated_at": _now()}},
        )
        _log_mutation(db, scope, "update_edge",
                      target={"kind": "edge", "id": [source, target]},
                      changes=upd, actor=actor, note=note)
    return r.matched_count


def add_node(db_name: str | None, scope: str, vertex, *,
             actor: str = "manual", note: str | None = None,
             db=None) -> None:
    _, db = _resolve_db(db_name, db)
    doc = _serialize_node(scope, vertex)
    db[NODES_COLL].insert_one(doc)
    db[GRAPHS_COLL].update_one(
        {"scope": scope},
        {"$set": {"updated_at": _now()}, "$inc": {"stats.num_nodes": 1}},
    )
    _log_mutation(db, scope, "add_node",
                  target={"kind": "node", "id": doc["node_id"]},
                  changes=doc, actor=actor, note=note)


def add_edge(db_name: str | None, scope: str, source, target,
             attrs: dict | None = None,
             *, actor: str = "manual", note: str | None = None,
             db=None) -> None:
    _, db = _resolve_db(db_name, db)
    doc = _serialize_edge(scope, source, target, attrs or {})
    db[EDGES_COLL].insert_one(doc)
    db[GRAPHS_COLL].update_one(
        {"scope": scope},
        {"$set": {"updated_at": _now()}, "$inc": {"stats.num_edges": 1}},
    )
    _log_mutation(db, scope, "add_edge",
                  target={"kind": "edge", "id": [source, target]},
                  changes=doc, actor=actor, note=note)


def remove_node(db_name: str | None, scope: str, node_id, *,
                actor: str = "manual", note: str | None = None,
                db=None) -> int:
    _, db = _resolve_db(db_name, db)
    r = db[NODES_COLL].delete_one(
        {"scope": scope, "node_id": node_id}
    )
    er = db[EDGES_COLL].delete_many({
        "scope": scope,
        "$or": [{"source": node_id}, {"target": node_id}],
    })
    if r.deleted_count:
        _log_mutation(db, scope, "remove_node",
                      target={"kind": "node", "id": node_id},
                      changes={"cascaded_edges": er.deleted_count},
                      actor=actor, note=note)
    return r.deleted_count


def remove_edge(db_name: str | None, scope: str, source, target, *,
                actor: str = "manual", note: str | None = None,
                db=None) -> int:
    _, db = _resolve_db(db_name, db)
    r = db[EDGES_COLL].delete_one(
        {"scope": scope, "source": source, "target": target}
    )
    if r.deleted_count:
        _log_mutation(db, scope, "remove_edge",
                      target={"kind": "edge", "id": [source, target]},
                      actor=actor, note=note)
    return r.deleted_count


def delete_graph(db_name: str | None, scope: str, *,
                 actor: str = "manual", note: str | None = None,
                 db=None) -> bool:
    """Delete a single scope's graph (metadata + nodes + edges). Keeps
    mutations log for audit."""
    _, db = _resolve_db(db_name, db)
    if not graph_exists(None, scope, db=db):
        return False
    db[NODES_COLL].delete_many({"scope": scope})
    db[EDGES_COLL].delete_many({"scope": scope})
    db[GRAPHS_COLL].delete_one({"scope": scope})
    _log_mutation(db, scope, "delete_graph", actor=actor, note=note)
    return True


def drop_session(db_name: str, *, uri: str = MONGO_URI):
    """Drop an entire session DB. Irreversible."""
    MongoClient(uri).drop_database(db_name)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def remap_graph(G: nx.DiGraph, offset: int) -> nx.DiGraph:
    """Apply an ID offset to every node; used by fish_compose to avoid
    collisions when merging per-container graphs."""
    mapping = {nid: nid + offset for nid in G.nodes()}
    G2 = nx.relabel_nodes(G, mapping, copy=True)
    for nid, data in G2.nodes(data=True):
        v = data.get("v")
        if v is not None:
            v.id_v = nid
            v.Z_v = [c + offset for c in v.Z_v]
    return G2


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def to_viz_json(db_name: str | None, scope: str, *, db=None) -> dict:
    """Return a dict in the fish_viz.html JSON format."""
    _, db = _resolve_db(db_name, db)
    if not graph_exists(None, scope, db=db):
        raise KeyError(f"No graph stored for scope '{scope}' in '{db.name}'.")

    nodes_out = []
    for doc in db[NODES_COLL].find({"scope": scope}):
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
    for doc in db[EDGES_COLL].find({"scope": scope}):
        e = {"source": doc["source"], "target": doc["target"]}
        for key in EDGE_ATTR_KEYS:
            if key in doc:
                e[key] = doc[key]
        edges_out.append(e)

    return {"nodes": nodes_out, "edges": edges_out}


def export_json(db_name: str | None, scope: str, output_path: str,
                *, db=None) -> None:
    import json
    with open(output_path, "w") as f:
        json.dump(to_viz_json(db_name, scope, db=db), f,
                  indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="FISH graph_store CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sessions", help="List all FISH session databases")

    p = sub.add_parser("list", help="List scopes inside a session")
    p.add_argument("session")

    p = sub.add_parser("info", help="Show graph metadata for one scope")
    p.add_argument("session")
    p.add_argument("scope")

    p = sub.add_parser("export", help="Export a stored graph to JSON")
    p.add_argument("session")
    p.add_argument("scope")
    p.add_argument("-o", "--output", default="fish_graph.json")

    p = sub.add_parser("delete", help="Delete one scope within a session")
    p.add_argument("session")
    p.add_argument("scope")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("drop", help="Drop an entire session database")
    p.add_argument("session")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("mutations", help="Show mutation log for a scope")
    p.add_argument("session")
    p.add_argument("scope")
    p.add_argument("-n", "--limit", type=int, default=20)

    args = ap.parse_args()

    if args.cmd == "sessions":
        sessions = list_sessions()
        if not sessions:
            print("(no fish_* sessions)")
            return
        for s in sessions:
            print(s)
        return

    if args.cmd == "drop":
        if not args.yes:
            print("Use --yes to confirm", file=sys.stderr); sys.exit(1)
        drop_session(args.session)
        print(f"dropped {args.session}")
        return

    db = connect(args.session)

    if args.cmd == "list":
        rows = list_graphs(db=db)
        if not rows:
            print(f"(no graphs stored in '{args.session}')"); return
        for g in rows:
            s = g.get("stats", {}) or {}
            print(f"{g['scope']:24s} "
                  f"nodes={s.get('num_nodes', 0):<5} "
                  f"edges={s.get('num_edges', 0):<5} "
                  f"composed={g.get('is_composed', False)!s:<5} "
                  f"updated={g.get('updated_at')}")
    elif args.cmd == "info":
        meta = get_metadata(None, args.scope, db=db)
        if not meta:
            print(f"No graph for scope '{args.scope}' in '{args.session}'",
                  file=sys.stderr); sys.exit(1)
        meta["_id"] = str(meta["_id"])
        print(json.dumps(meta, indent=2, default=str))
    elif args.cmd == "export":
        export_json(None, args.scope, args.output, db=db)
        print(f"Exported → {args.output}")
    elif args.cmd == "delete":
        if not args.yes:
            print("Use --yes to confirm", file=sys.stderr); sys.exit(1)
        ok = delete_graph(None, args.scope, actor="cli", db=db)
        print("Deleted" if ok else "No such scope")
    elif args.cmd == "mutations":
        cur = (db[MUTATIONS_COLL]
               .find({"scope": args.scope})
               .sort("timestamp", -1)
               .limit(args.limit))
        for m in cur:
            m["_id"] = str(m["_id"])
            print(json.dumps(m, default=str))


if __name__ == "__main__":
    _cli()
