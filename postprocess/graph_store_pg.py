"""Persistent FISH model graph in PostgreSQL — mirrors graph_store.py.

The MongoDB-based graph_store stored each session's graph in four
scope-qualified collections (graph_meta, graph_nodes, graph_edges,
graph_mutations). The PG version uses the same four normalized tables
(see scripts/init_fish_pg.sql), with a (session_id, scope) composite
key in place of "session DB + scope".

Public API:
  save_graph(G, session_id, scope, *, ...)
  load_graph(session_id, scope) -> nx.DiGraph
  graph_exists(session_id, scope)
  to_viz_json(session_id, scope) -> dict
  export_json(session_id, scope, out_path)
  delete_graph(session_id, scope)
  list_graphs(session_id)
  remap_graph(G, offset)
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import networkx as nx
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_store


STANDALONE_SCOPE = "__main__"
COMPOSED_SCOPE = "__composed__"

# Typed columns we promote out of A_v to the row level.
NODE_TYPED_KEYS = ("pid", "full_name", "etype", "ptype", "cb_addr", "external")
# Everything else lives in attrs (JSONB).
NODE_EXTRA_KEYS = (
    "aspects", "gpu_node", "action_name", "action_role",
    "split", "period_ns",
    "executor_addr", "executor_type", "num_threads", "cb_groups",
    "extra_executors", "callback_group",
    "thread_tid",
    "node_handle", "publishers", "service_clients",
    "msg_type", "srv_type", "timer_handle",
    "gpu_dags", "gpu_invocations", "gpu_streams",
    "gpu_task_graph",
)
EDGE_TYPED_KEYS = ("rel", "level")
EDGE_EXTRA_KEYS = (
    "nature", "topic", "service", "msg_type", "avg_rate_hz", "external",
    "direction", "topics", "comm_count", "cross_container",
    "src_container", "dst_container", "tau",
)


@dataclass
class Vertex:
    t_v: str
    id_v: int
    A_v: dict = field(default_factory=dict)
    Z_v: list = field(default_factory=list)
    level: int = 0


def _sanitize(val):
    if isinstance(val, set):
        return list(val)
    return val


def _serialize_node(v):
    A_v = v.A_v or {}
    typed = {k: _sanitize(A_v.get(k)) for k in NODE_TYPED_KEYS}
    extras = {k: _sanitize(A_v.get(k)) for k in NODE_EXTRA_KEYS
              if A_v.get(k) is not None}
    # Allow truly-unknown A_v keys to leak into attrs too
    for k, val in A_v.items():
        if k in NODE_TYPED_KEYS or k in NODE_EXTRA_KEYS or k == "label":
            continue
        extras[k] = _sanitize(val)
    return {
        "node_id": v.id_v,
        "type": v.t_v,
        "label": A_v.get("label", ""),
        "level": v.level,
        "children": list(v.Z_v),
        "attrs": extras,
        **typed,
    }


def _serialize_edge(u, w, data):
    typed = {k: data.get(k) for k in EDGE_TYPED_KEYS}
    extras = {k: _sanitize(data.get(k)) for k in EDGE_EXTRA_KEYS
              if data.get(k) is not None}
    return {"source": u, "target": w, **typed, "attrs": extras}


def _doc_to_vertex(row):
    A_v = {"label": row.get("label", "")}
    for k in NODE_TYPED_KEYS:
        v = row.get(k)
        if v is not None:
            A_v[k] = v
    if row.get("attrs"):
        for k, v in row["attrs"].items():
            A_v[k] = v
    return Vertex(
        t_v=row["type"],
        id_v=row["node_id"],
        A_v=A_v,
        Z_v=list(row.get("children", []) or []),
        level=row.get("level", 0),
    )


# ---------------------------------------------------------------------------
# Query / existence
# ---------------------------------------------------------------------------

def graph_exists(session_id: str, scope: str) -> bool:
    row = pg_store.fetch_one(
        "SELECT 1 FROM graph_meta WHERE session_id = %s AND scope = %s",
        (session_id, scope),
    )
    return row is not None


def list_graphs(session_id: str) -> list[dict]:
    return pg_store.fetch_all(
        "SELECT * FROM graph_meta WHERE session_id = %s ORDER BY updated_at DESC",
        (session_id,),
    )


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_graph(G: nx.DiGraph, session_id: str, scope: str = STANDALONE_SCOPE, *,
               source_trace: str | None = None,
               container_role: str | None = None,
               is_composed: bool = False,
               actor: str = "model_improved_pg",
               note: str | None = None,
               replace: bool = True) -> dict:
    """Persist (or replace) the graph at (session_id, scope)."""
    exists = graph_exists(session_id, scope)
    if exists and not replace:
        raise ValueError(
            f"Graph for ({session_id}, {scope}) already exists; pass replace=True"
        )

    nodes = []
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v is None:
            continue
        nodes.append(_serialize_node(v))
    edges = [_serialize_edge(u, w, data) for u, w, data in G.edges(data=True)]

    with pg_store.get_cursor() as (conn, cur):
        if exists:
            cur.execute("DELETE FROM graph_nodes WHERE session_id = %s AND scope = %s",
                        (session_id, scope))
            cur.execute("DELETE FROM graph_edges WHERE session_id = %s AND scope = %s",
                        (session_id, scope))

        if nodes:
            node_rows = [
                (session_id, scope, n["node_id"], n["type"], n["label"],
                 n["level"], n["children"], json.dumps(n["attrs"], default=str),
                 n.get("pid"), n.get("full_name"), n.get("etype"),
                 n.get("ptype"), n.get("cb_addr"), n.get("external"))
                for n in nodes
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO graph_nodes
                   (session_id, scope, node_id, type, label, level, children,
                    attrs, pid, full_name, etype, ptype, cb_addr, external)
                   VALUES %s""",
                node_rows,
            )

        if edges:
            edge_rows = [
                (session_id, scope, e["source"], e["target"], e["rel"], e["level"],
                 json.dumps(e["attrs"], default=str))
                for e in edges
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO graph_edges
                   (session_id, scope, source, target, rel, level, attrs)
                   VALUES %s
                   ON CONFLICT DO NOTHING""",
                edge_rows,
            )

        stats = {
            "num_nodes": len(nodes),
            "num_edges": len(edges),
            "num_externals": sum(1 for n in nodes if n.get("external")),
        }
        cur.execute("""
            INSERT INTO graph_meta (session_id, scope, is_composed,
                                    source_trace, container_role, stats)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id, scope) DO UPDATE SET
              is_composed = EXCLUDED.is_composed,
              source_trace = EXCLUDED.source_trace,
              container_role = EXCLUDED.container_role,
              stats = EXCLUDED.stats,
              updated_at = now()
        """, (session_id, scope, is_composed, source_trace, container_role,
              json.dumps(stats)))

        cur.execute("""
            INSERT INTO graph_mutations (session_id, scope, op, actor, note,
                                          changes)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (session_id, scope, "save_graph", actor, note,
              json.dumps({"stats": stats, "replaced": exists}, default=str)))
        conn.commit()

    return {"session_id": session_id, "scope": scope, "stats": stats}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_graph(session_id: str, scope: str = STANDALONE_SCOPE) -> nx.DiGraph:
    if not graph_exists(session_id, scope):
        raise KeyError(f"No graph at ({session_id}, {scope})")
    G = nx.DiGraph()
    for row in pg_store.fetch_all(
        "SELECT * FROM graph_nodes WHERE session_id = %s AND scope = %s "
        "ORDER BY node_id",
        (session_id, scope),
    ):
        vx = _doc_to_vertex(row)
        G.add_node(vx.id_v, v=vx)
    for row in pg_store.fetch_all(
        "SELECT * FROM graph_edges WHERE session_id = %s AND scope = %s",
        (session_id, scope),
    ):
        attrs = {}
        for k in EDGE_TYPED_KEYS:
            if row.get(k) is not None:
                attrs[k] = row[k]
        if row.get("attrs"):
            for k, v in row["attrs"].items():
                attrs[k] = v
        G.add_edge(row["source"], row["target"], **attrs)
    return G


# ---------------------------------------------------------------------------
# Export (fish_graph.json — same format as the MongoDB-backed export)
# ---------------------------------------------------------------------------

def to_viz_json(session_id: str, scope: str = STANDALONE_SCOPE) -> dict:
    nodes_out = []
    for row in pg_store.fetch_all(
        "SELECT * FROM graph_nodes WHERE session_id = %s AND scope = %s "
        "ORDER BY node_id",
        (session_id, scope),
    ):
        n = {
            "id": row["node_id"],
            "type": row["type"],
            "label": row.get("label", ""),
            "level": row.get("level", 0),
            "children": list(row.get("children", []) or []),
        }
        for k in NODE_TYPED_KEYS:
            v = row.get(k)
            if v is not None:
                n[k] = v
        if row.get("attrs"):
            for k, v in row["attrs"].items():
                n[k] = v
        nodes_out.append(n)

    edges_out = []
    for row in pg_store.fetch_all(
        "SELECT * FROM graph_edges WHERE session_id = %s AND scope = %s",
        (session_id, scope),
    ):
        e = {"source": row["source"], "target": row["target"]}
        for k in EDGE_TYPED_KEYS:
            if row.get(k) is not None:
                e[k] = row[k]
        if row.get("attrs"):
            for k, v in row["attrs"].items():
                e[k] = v
        edges_out.append(e)

    return {"nodes": nodes_out, "edges": edges_out}


def export_json(session_id: str, scope: str, out_path: str):
    payload = to_viz_json(session_id, scope)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return out_path


def delete_graph(session_id: str, scope: str = STANDALONE_SCOPE):
    with pg_store.get_cursor() as (conn, cur):
        cur.execute("DELETE FROM graph_nodes WHERE session_id = %s AND scope = %s",
                    (session_id, scope))
        cur.execute("DELETE FROM graph_edges WHERE session_id = %s AND scope = %s",
                    (session_id, scope))
        cur.execute("DELETE FROM graph_meta WHERE session_id = %s AND scope = %s",
                    (session_id, scope))
        conn.commit()


# ---------------------------------------------------------------------------
# Utility — ID remapping (used by multi-container compose, parity with mongo
# graph_store.remap_graph)
# ---------------------------------------------------------------------------

def remap_graph(G: nx.DiGraph, offset: int) -> nx.DiGraph:
    mapping = {nid: nid + offset for nid in G.nodes()}
    G2 = nx.relabel_nodes(G, mapping, copy=True)
    for nid, data in G2.nodes(data=True):
        v = data.get("v")
        if v is not None:
            v.id_v = nid
            v.Z_v = [c + offset for c in v.Z_v]
    return G2
