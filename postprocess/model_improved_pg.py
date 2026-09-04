"""FISH model extraction — PostgreSQL backend (mirror of model_improved.py).

Reads ros2_trace + supporting tables from PG, produces the same nx.DiGraph
that model_improved.py produces, and exports/persists via graph_store_pg.

Stop condition for the migration: the JSON produced here is STRUCTURALLY
identical to the one produced by the old MongoDB/InfluxDB pipeline for the
same session.

Usage:
  python3 -m postprocess.model_improved_pg --session <session_id> \
      [--scope <scope>] [--out fish_graph.json] [--source-trace <session_dir>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import collections
from dataclasses import dataclass, field
from itertools import count

import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_store
import graph_store_pg


# Filters disabled by default (per project_fish_state notes/task #80)
SKIP_DEBUG = False
SKIP_RVIZ = False
SKIP_PARAMSERVICE = False
INCLUDE_EXTERNAL_ACTIONS = True

_PARAM_TOPICS = {"/parameter_events"}
_PARAM_SRV_SUFFIXES = (
    "/describe_parameters", "/get_parameter_types", "/get_parameters",
    "/list_parameters", "/set_parameters", "/set_parameters_atomically",
)


def _is_param_service(name):
    if name in _PARAM_TOPICS:
        return True
    return any(name.endswith(s) for s in _PARAM_SRV_SUFFIXES)


def log(msg):
    print(f"[FISH model_pg] {msg}", flush=True)


# Warning counter: aggregated per category to keep output manageable.
# Resolved by `_warn_summary()` at the end of identify_callbacks().
_WARN: dict[str, int] = {}


def warn(category: str, detail: str = "", limit_examples: int = 3):
    """Emit a [FISH model_pg WARN] line and bump the per-category counter.

    `category` should be a stable string like 'nitros_duplicate_sub'. The first
    `limit_examples` warnings per category include `detail`; subsequent ones
    only increment the counter and a final summary is printed once at the end.
    """
    count = _WARN.get(category, 0)
    _WARN[category] = count + 1
    if count < limit_examples:
        suffix = f": {detail}" if detail else ""
        print(f"[FISH model_pg WARN] {category}{suffix}", flush=True)


def _warn_summary():
    if not _WARN:
        return
    print("[FISH model_pg WARN] summary:", flush=True)
    for cat, n in sorted(_WARN.items(), key=lambda kv: -kv[1]):
        print(f"[FISH model_pg WARN]   {cat}: {n} occurrence(s)", flush=True)


# ----------------------------------------------------------------------------
# Vertex classes (parity with model_improved.py)
# ----------------------------------------------------------------------------

vertex_counter = count(1)


@dataclass
class FishVertex:
    t_v: str
    id_v: int
    A_v: dict = field(default_factory=dict)
    Z_v: list = field(default_factory=list)
    level: int = 0


# ----------------------------------------------------------------------------
# Helpers — query ros2_trace via SQL with payload JSONB
# ----------------------------------------------------------------------------

def _all_events(session_id: str, event_name: str) -> list[dict]:
    """Return all matching events, oldest-first.

    Each row is a dict with: ts_ns, event, cpu_id, vpid, vtid, procname,
    payload (dict). Inserts a `meta` sub-dict for parity with the old shape
    so code paths using d["meta"]["vpid"] still work.
    """
    rows = pg_store.fetch_all(
        "SELECT ts_ns, event, cpu_id, vpid, vtid, host_name, procname, payload "
        "FROM ros2_trace WHERE session_id = %s AND event = %s "
        "ORDER BY ts_ns, id",
        (session_id, event_name),
    )
    out = []
    for r in rows:
        out.append({
            "ts_ns": r["ts_ns"],
            "event": r["event"],
            "cpu_id": r["cpu_id"],
            "vpid": r["vpid"],
            "vtid": r["vtid"],
            "procname": r["procname"],
            "payload": r["payload"] or {},
            "meta": {
                "host": r.get("host_name"),
                "vpid": r["vpid"],
                "vtid": r["vtid"],
                "procname": r["procname"],
            },
        })
    return out


def _distinct_vpids(session_id: str, event_name: str) -> list[int]:
    """Distinct vpids that fired event_name, ordered by first-occurrence ts."""
    rows = pg_store.fetch_all(
        "SELECT vpid, MIN(ts_ns) AS first_ts FROM ros2_trace "
        "WHERE session_id = %s AND event = %s "
        "GROUP BY vpid ORDER BY first_ts",
        (session_id, event_name),
    )
    return [r["vpid"] for r in rows if r["vpid"] is not None]


def _confirmed_kills(session_id: str) -> set[int]:
    rows = pg_store.fetch_all(
        "SELECT killed_pid FROM fish_events "
        "WHERE session_id = %s AND action = 'kill' AND killed_pid IS NOT NULL",
        (session_id,),
    )
    pids = {r["killed_pid"] for r in rows}
    if pids:
        log(f"  killed PIDs: {sorted(pids)}")
    return pids


def _entity_fallback_from_trace(session_id: str, node_handle: str) -> dict:
    """Mirror of utils.entity_fallback_from_trace using PG."""
    out = {
        "Subscribers": {},
        "Publishers": {},
        "Service_Servers": {},
        "Service_Clients": {},
        "Action_Servers": {},
        "Action_Clients": {},
        "Timers": {},
    }
    nh = node_handle

    # Subscriptions: rcl_subscription_init
    for r in pg_store.fetch_all(
        "SELECT payload FROM ros2_trace "
        "WHERE session_id = %s AND event = 'ros2:rcl_subscription_init' "
        "AND payload->>'node_handle' = %s",
        (session_id, nh),
    ):
        out["Subscribers"][r["payload"].get("topic_name", "?")] = "?"

    for r in pg_store.fetch_all(
        "SELECT payload FROM ros2_trace "
        "WHERE session_id = %s AND event = 'ros2:rcl_publisher_init' "
        "AND payload->>'node_handle' = %s",
        (session_id, nh),
    ):
        out["Publishers"][r["payload"].get("topic_name", "?")] = "?"

    for r in pg_store.fetch_all(
        "SELECT payload FROM ros2_trace "
        "WHERE session_id = %s AND event = 'ros2:rcl_service_init' "
        "AND payload->>'node_handle' = %s",
        (session_id, nh),
    ):
        out["Service_Servers"][r["payload"].get("service_name", "?")] = "?"

    for r in pg_store.fetch_all(
        "SELECT payload FROM ros2_trace "
        "WHERE session_id = %s AND event = 'ros2:rcl_client_init' "
        "AND payload->>'node_handle' = %s",
        (session_id, nh),
    ):
        out["Service_Clients"][r["payload"].get("service_name", "?")] = "?"

    # Timers — atomic events fish_rclcpp_timer_init / fish_rclpy_timer_init
    for ev in ("ros2:fish_rclcpp_timer_init", "ros2:fish_rclpy_timer_init"):
        for r in pg_store.fetch_all(
            "SELECT payload FROM ros2_trace "
            "WHERE session_id = %s AND event = %s "
            "AND payload->>'node_handle' = %s",
            (session_id, ev, nh),
        ):
            p = r["payload"]
            out["Timers"][p["timer_handle"]] = p["period_ns"]

    print(f"  FALLBACK for node_handle={nh}: "
          f"{len(out['Subscribers'])} subs, {len(out['Publishers'])} pubs, "
          f"{len(out['Service_Servers'])} srvs, {len(out['Timers'])} timers")
    return out


# ----------------------------------------------------------------------------
# Layer 0: Executors + Layer 1: Nodes
# ----------------------------------------------------------------------------

def identify_executors(session_id: str):
    """Each distinct vpid that fires ros2:rcl_node_init is an executor."""
    killed_pids = _confirmed_kills(session_id)

    executors: dict[int, FishVertex] = {}
    nodes: dict[int, FishVertex] = {}

    for pid in _distinct_vpids(session_id, "ros2:rcl_node_init"):
        if pid in killed_pids:
            continue

        node_inits = _all_events(session_id, "ros2:rcl_node_init")
        # Filter to this pid (already sorted by ts in _all_events)
        node_inits = [d for d in node_inits if d["vpid"] == pid]
        if not node_inits:
            continue

        if SKIP_DEBUG:
            all_debug = all(d["payload"].get("node_name", "").startswith("_ros2cli")
                            for d in node_inits)
            if all_debug:
                if INCLUDE_EXTERNAL_ACTIONS:
                    has_action = any("send_goal" in d["payload"].get("node_name", "")
                                     for d in node_inits)
                    if not has_action:
                        continue
                else:
                    continue
        if SKIP_RVIZ and any(d["procname"] == "rviz2" for d in node_inits):
            continue

        ex_A = {"label": "temp", "pid": pid, "mode": "NA"}
        ex = FishVertex("EX", next(vertex_counter), ex_A, [], 0)

        for nie in node_inits:
            ns = nie["payload"].get("namespace", "/")
            full_name = ns.rstrip("/") + "/" + nie["payload"]["node_name"]
            n_A = {
                "label": nie["payload"]["node_name"],
                "full_name": full_name,
                "node_handle": nie["payload"]["node_handle"],
                "publishers": {},
            }
            if ex.A_v["label"] == "temp":
                ex.A_v["label"] = f"{nie['procname']} (PID:{pid})"
            n = FishVertex("N", next(vertex_counter), n_A, [], 1)
            ex.Z_v.append(n.id_v)
            nodes[n.id_v] = n

        executors[ex.id_v] = ex

    log(f"Executors: {len(executors)}, Nodes: {len(nodes)}")
    return executors, nodes


# ----------------------------------------------------------------------------
# Layer 2: Entities
# ----------------------------------------------------------------------------

def _node_info_for(session_id: str, node_full_name: str) -> dict:
    """Reassemble the old ros2-cli node_info shape from node_endpoints rows."""
    out = {
        "Subscribers": {}, "Publishers": {},
        "Service_Servers": {}, "Service_Clients": {},
        "Action_Servers": {}, "Action_Clients": {},
    }
    kind_map = {
        "sub": "Subscribers", "pub": "Publishers",
        "srv_server": "Service_Servers", "srv_client": "Service_Clients",
        "act_server": "Action_Servers", "act_client": "Action_Clients",
    }
    for r in pg_store.fetch_all(
        "SELECT endpoint_kind, topic_or_service, message_type "
        "FROM node_endpoints WHERE session_id = %s AND node_full_name = %s",
        (session_id, node_full_name),
    ):
        section = kind_map.get(r["endpoint_kind"])
        if section:
            out[section][r["topic_or_service"]] = r["message_type"]
    return out


def identify_entities(session_id: str, nodes: dict[int, FishVertex]):
    entities: dict[int, FishVertex] = {}

    # Waitable registrations (FISH custom — fish_rclcpp_waitable_init). Indexed
    # by owning node_handle. A Waitable is a first-class entity peer to
    # sub/serv/tmr: it dispatches via Executor::execute_waitable and we wrap
    # that dispatch with callback_start/end using the Waitable's `this` ptr
    # as the cb-id. The same `this` is the entity's cb_addr.
    #
    # IntraProcessManager::add_subscription emits with node_handle=nullptr
    # ("0x0") for intra-process subscription Waitables — those are NOT
    # standalone entities, they're internal dispatch shims behind a parent
    # rclcpp::Subscription that already has its own sub entity. We keep the
    # event in the trace (for completeness + receive/publish attribution)
    # but skip creating a Waitable entity for it, otherwise each intra-proc
    # sub would emit a singleton "waitable@xxxx" node into the FT graph.
    # --- what does each waitable SERVE? (T1, 2026-08-31)
    # A Waitable is a real schedulable entity, but "waitable@addr" hides whose
    # work it dispatches. Associations derivable from the trace:
    #   * NITROS pub-side: fish_nitros_pub_link carries waitable_handle +
    #     publisher_handle + GXF component/entity names; publisher_handle →
    #     topic via rcl_publisher_init.
    #   * rclcpp IPC: fish_rclcpp_ipb_to_subscription (waitable → subscription)
    #     — those waitables are skipped as entities (node_handle=0x0), but if
    #     a future overlay emits the event for entity-worthy waitables the map
    #     is ready.
    pub_handle_topic = {}
    for d in _all_events(session_id, "ros2:rcl_publisher_init"):
        p = d["payload"]
        if p.get("publisher_handle") and p.get("topic_name"):
            pub_handle_topic[p["publisher_handle"]] = p["topic_name"]
    waitable_serves: dict[str, dict] = {}   # waitable ptr → {label_suffix, attrs}
    for d in _all_events(session_id, "ros2:fish_nitros_pub_link"):
        p = d["payload"]
        wp = p.get("waitable_handle")
        if not wp:
            continue
        topic = pub_handle_topic.get(p.get("publisher_handle"))
        gxf = "/".join(x for x in (p.get("component_name"), p.get("entity_name")) if x)
        waitable_serves[wp] = {
            "suffix": f"→pub:{topic}" if topic else (f":{gxf}" if gxf else ""),
            "serves_topic": topic, "serves_role": "pub",
            "gxf_entity": gxf or None,
        }
    for d in _all_events(session_id, "ros2:fish_rclcpp_ipb_to_subscription"):
        p = d["payload"]
        wp = p.get("waitable") or p.get("ipb")
        sub = p.get("subscription_topic") or p.get("topic_name")
        if wp and wp not in waitable_serves:
            waitable_serves[wp] = {
                "suffix": f"→sub:{sub}" if sub else "→sub", "serves_topic": sub,
                "serves_role": "sub", "gxf_entity": None,
            }
    if waitable_serves:
        log(f"  Waitables: {len(waitable_serves)} association(s) "
            f"(NITROS pub_link / ipb_to_subscription) → labels enriched")

    waitables_by_node: dict[str, list[tuple[str, str]]] = {}
    intra_proc_waitables_skipped = 0
    for d in _all_events(session_id, "ros2:fish_rclcpp_waitable_init"):
        p = d["payload"]
        nh = p.get("node_handle")
        wp = p.get("waitable")
        cg = p.get("callback_group")
        if not wp:
            continue
        if not nh or nh == "0x0":
            intra_proc_waitables_skipped += 1
            continue
        waitables_by_node.setdefault(nh, []).append((wp, cg))
    if intra_proc_waitables_skipped:
        log(f"  Waitables: {intra_proc_waitables_skipped} intra-proc "
            f"(IntraProcessManager-registered) — no standalone entity; bound to "
            f"their parent sub entity/F in identify_callbacks via rclcpp_subscription_init")

    # Client registrations — rcl_client_init, indexed by owning node_handle.
    # Clients are the 5th AnyExecutable kind: the executor dispatches
    # execute_client() when a response arrives, and our executor.cpp wrap
    # (install_fish_tracepoints) emits callback_start/end with the rcl client
    # handle as cb-id — the same address recorded here, so no extra
    # registration event is needed.
    # Client handles are REUSED: a transient client is created, destroyed
    # and the next one lands on the same address (Autoware: one address
    # re-initialised 224× for 224 different services). The init timestamp
    # therefore travels with the entity/F so span attribution can pick the
    # latest init preceding the span (gantt) instead of a static cb_addr map.
    clients_by_node: dict[str, list[tuple[str, str, int]]] = {}
    for d in _all_events(session_id, "ros2:rcl_client_init"):
        p = d["payload"]
        ch, nh = p.get("client_handle"), p.get("node_handle")
        srv = p.get("service_name", "?")
        if not ch or not nh:
            continue
        clients_by_node.setdefault(nh, []).append((ch, srv, int(d["ts_ns"])))

    for node_id, node in nodes.items():
        full_name = node.A_v["full_name"]
        node_handle = node.A_v["node_handle"]

        fallback = _entity_fallback_from_trace(session_id, node_handle)
        ni = _node_info_for(session_id, full_name)

        def _union(field_name):
            merged = dict(fallback.get(field_name, {}))
            for k, v in (ni.get(field_name, {}) or {}).items():
                if k not in merged or (v and v != "?"):
                    merged[k] = v
            return merged

        info = {
            "Subscribers":     _union("Subscribers"),
            "Publishers":      _union("Publishers"),
            "Service_Servers": _union("Service_Servers"),
            "Service_Clients": _union("Service_Clients"),
            "Action_Servers":  _union("Action_Servers"),
            "Action_Clients":  _union("Action_Clients"),
            "Timers":          fallback.get("Timers", {}),
        }

        pubs = info["Publishers"]
        if SKIP_PARAMSERVICE:
            pubs = {t: m for t, m in pubs.items() if not _is_param_service(t)}
        node.A_v["publishers"] = pubs

        clients = info["Service_Clients"]
        if SKIP_PARAMSERVICE:
            clients = {s: t for s, t in clients.items() if not _is_param_service(s)}
        node.A_v["service_clients"] = clients

        for topic, msg_type in info["Subscribers"].items():
            if SKIP_PARAMSERVICE and _is_param_service(topic):
                continue
            e_A = {
                "label": topic, "etype": "sub", "cb_addr": "NA",
                "msg_type": msg_type,
                "aspects": [{"aspect": "sub", "topic": topic, "msg_type": msg_type}],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

        for srv_name, srv_type in info["Service_Servers"].items():
            if SKIP_PARAMSERVICE and _is_param_service(srv_name):
                continue
            e_A = {
                "label": srv_name, "etype": "serv", "cb_addr": "NA",
                "srv_type": srv_type,
                "aspects": [
                    {"aspect": "sub", "service": srv_name},
                    {"aspect": "pub", "service": srv_name},
                ],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

        for timer_handle, period_ns in info["Timers"].items():
            e_A = {
                "label": f"timer_{period_ns}ns", "etype": "tmr", "cb_addr": "NA",
                "timer_handle": timer_handle, "period_ns": period_ns,
                "aspects": [],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

        for waitable_ptr, cb_group in waitables_by_node.get(node_handle, []):
            short = waitable_ptr[-6:] if waitable_ptr and waitable_ptr.startswith("0x") else waitable_ptr
            srv = waitable_serves.get(waitable_ptr)
            wl = f"waitable{srv['suffix']}" if srv and srv.get("suffix") else f"waitable@{short}"
            e_A = {
                "label": wl, "etype": "waitable",
                "serves_topic": (srv or {}).get("serves_topic"),
                "serves_role": (srv or {}).get("serves_role"),
                "gxf_entity": (srv or {}).get("gxf_entity"),
                # The Waitable's `this` ptr IS the cb_addr — no resolution
                # needed. Executor::execute_waitable wraps dispatch with
                # callback_start(this), so any publish_link / client_link
                # fired from within will look up cb_to_entity[this].
                "cb_addr": waitable_ptr,
                "waitable_handle": waitable_ptr,
                "callback_group": cb_group,
                # Aspects populated by publish_link / receive_link attribution.
                "aspects": [],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

        for client_handle, srv_name, init_ts in clients_by_node.get(node_handle, []):
            if SKIP_PARAMSERVICE and _is_param_service(srv_name):
                continue
            e_A = {
                "label": srv_name, "etype": "cli",
                # rcl client handle IS the cb_addr: the executor.cpp
                # execute_client wrap emits callback_start/end with it.
                "cb_addr": client_handle,
                "client_handle": client_handle,
                "init_ts_ns": init_ts,
                "aspects": [{"aspect": "pub", "service": srv_name},
                            {"aspect": "sub", "service": srv_name}],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

    log(f"Entities: {len(entities)} "
        f"(sub={sum(1 for e in entities.values() if e.A_v['etype']=='sub')}, "
        f"serv={sum(1 for e in entities.values() if e.A_v['etype']=='serv')}, "
        f"tmr={sum(1 for e in entities.values() if e.A_v['etype']=='tmr')}, "
        f"waitable={sum(1 for e in entities.values() if e.A_v['etype']=='waitable')}, "
        f"cli={sum(1 for e in entities.values() if e.A_v['etype']=='cli')})")
    return entities


# ----------------------------------------------------------------------------
# Layer 3: Callbacks
# ----------------------------------------------------------------------------

def identify_callbacks(session_id: str, nodes, entities, executors):
    t0 = time.time()
    functions: dict[int, FishVertex] = {}
    log(f"identify_callbacks START (bulk) — {len(entities)} entities")

    rcl_sub_init = _all_events(session_id, "ros2:rcl_subscription_init")
    rclcpp_sub_init = _all_events(session_id, "ros2:rclcpp_subscription_init")
    rclcpp_sub_cb_added = _all_events(session_id, "ros2:rclcpp_subscription_callback_added")
    rclcpp_cb_register = _all_events(session_id, "ros2:rclcpp_callback_register")
    rcl_srv_init = _all_events(session_id, "ros2:rcl_service_init")
    rclcpp_srv_cb_added = _all_events(session_id, "ros2:rclcpp_service_callback_added")
    rclcpp_tmr_cb_added = _all_events(session_id, "ros2:rclcpp_timer_callback_added")
    rclpy_sub_cb_added = _all_events(session_id, "ros2:fish_rclpy_subscription_callback_added")
    rclpy_srv_cb_added = _all_events(session_id, "ros2:fish_rclpy_service_callback_added")
    rclpy_tmr_cb_added = _all_events(session_id, "ros2:fish_rclpy_timer_callback_added")
    rclpy_cb_register = _all_events(session_id, "ros2:fish_rclpy_callback_register")
    log(f"  Bulk load: {time.time()-t0:.1f}s")

    sub_init_by_node: dict[str, list[tuple]] = {}
    for d in rcl_sub_init:
        p = d["payload"]
        sub_init_by_node.setdefault(p["node_handle"], []).append(
            (p["subscription_handle"], p["topic_name"]))

    # NitrosNode + negotiated subscriptions register MULTIPLE rclcpp_subscription_init
    # events with the SAME sub_handle (one for the compat / fallback sub, one for the
    # NITROS-negotiated wrapper). The earlier "_keep_first" heuristic matched the
    # MongoDB pipeline's natural-order iteration which happened to surface the
    # correct callback for the apriltag fixture — but it does NOT generalise: in
    # stereo_image_proc_custom the *negotiated* sub registers second and is the
    # one whose callback actually fires.
    #
    # New strategy:
    #   1. Keep ALL (handle → [sub_obj1, sub_obj2, ...]).
    #   2. For each sub_obj, look up its callback via rclcpp_sub_cb_added.
    #   3. Among the candidate callbacks, prefer the one that *fired* in
    #      ros2_trace (has callback_start events). This is the empirical truth.
    #   4. If only one candidate fires → use it (warn if we had to disambiguate).
    #   5. If multiple fire → keep first registered (rare; warn).
    #   6. If none fire → fall back to first (unchanged from old behaviour).
    def _keep_first(docs, key_field, val_field):
        out = {}
        for d in docs:
            k = d["payload"][key_field]
            if k not in out:
                out[k] = d["payload"][val_field]
        return out

    def _group_all(docs, key_field, val_field):
        """Like _keep_first but builds key → [vals in registration order]."""
        out: dict[str, list] = {}
        for d in docs:
            k = d["payload"][key_field]
            out.setdefault(k, []).append(d["payload"][val_field])
        return out

    # Set of cb_addrs that actually have callback_start events — the firing set.
    _firing_cbs: set[str] = set()
    for d in _all_events(session_id, "ros2:callback_start"):
        cb = d["payload"].get("callback")
        if cb:
            _firing_cbs.add(cb)

    rclcpp_sub_objs_by_handle = _group_all(rclcpp_sub_init, "subscription_handle", "subscription")
    rclcpp_sub_cb_all_by_sub = _group_all(rclcpp_sub_cb_added, "subscription", "callback")

    # Intra-process subscriptions (rclcpp Humble): Subscription::post_init_setup
    # creates a SubscriptionIntraProcess Waitable and emits a SECOND
    # rclcpp_subscription_init(rcl_handle, <intra-proc obj>) plus a
    # rclcpp_subscription_callback_added(<intra-proc obj>, &callback-copy).
    # Our IntraProcessManager::add_subscription patch emits
    # fish_rclcpp_waitable_init(<intra-proc obj>, node_handle=nullptr).
    # Joining the two gives every intra-proc Waitable its (node, topic):
    #   waitable ptr → rcl sub handle → topic (rcl_subscription_init) → node.
    # Verified on Autoware fish_20260816_165836: 141/141 intra-proc Waitables
    # matched, and outer-Waitable dispatch count == inner callback count.
    _intra_wait_ptrs: set[str] = set()
    for d in _all_events(session_id, "ros2:fish_rclcpp_waitable_init"):
        p = d["payload"]
        if p.get("waitable") and (not p.get("node_handle") or p.get("node_handle") == "0x0"):
            _intra_wait_ptrs.add(p["waitable"])
    intra_wait_by_handle: dict[str, str] = {}
    for d in rclcpp_sub_init:
        p = d["payload"]
        if p["subscription"] in _intra_wait_ptrs:
            intra_wait_by_handle[p["subscription_handle"]] = p["subscription"]
    if _intra_wait_ptrs:
        _bound_ptrs = set(intra_wait_by_handle.values())
        log(f"  Intra-proc Waitables: {len(_intra_wait_ptrs)} distinct ptrs registered, "
            f"{len(_bound_ptrs)} matched to rclcpp_subscription_init "
            f"({len(intra_wait_by_handle)} handle bindings; "
            f"{len(_intra_wait_ptrs) - len(_bound_ptrs)} unmatched)")

    # Alternate FIRING callbacks per sub_handle that are not the primary F's
    # cb_addr but belong to the same entity (see intra-proc note below).
    # identify_callbacks attaches them to the entity as alt_cb_addrs so
    # attribution (cb_to_entity) and the firing_cb_without_F check see them.
    alt_cbs_by_handle: dict[str, list[str]] = {}

    def _resolve_sub_handle_to_cb(sub_handle: str, topic: str = "") -> str | None:
        """Pick the primary callback for a sub_handle, preferring firing ones.
        Logs warnings on disambiguation."""
        sub_objs = rclcpp_sub_objs_by_handle.get(sub_handle, [])
        if not sub_objs:
            return None
        candidates: list[str] = []
        for so in sub_objs:
            for cb in rclcpp_sub_cb_all_by_sub.get(so, []):
                if cb not in candidates:
                    candidates.append(cb)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        firing = [c for c in candidates if c in _firing_cbs]
        # Intra-process-enabled sub: TWO rclcpp objects share the rcl handle —
        # the plain Subscription (callback runs for DDS / inter-process
        # deliveries) and its SubscriptionIntraProcess Waitable (callback copy
        # runs for same-process deliveries via executor branch 5). Both paths
        # are live at once; which one fires depends on where the publisher
        # lives (Autoware r7: concat's before_sync subs fire the DDS copy
        # because the per-lidar preprocessors are separate containers, while
        # centerpoint's concatenated/pointcloud sub fires the intra-proc copy).
        # Primary F = the intra-proc copy IF it fired (branch-5 dispatch),
        # else the firing DDS copy; every other firing copy is kept as an
        # alternate cb of the same entity. Not a NITROS duplicate — no warn.
        ip_obj = intra_wait_by_handle.get(sub_handle)
        if ip_obj:
            ip_cbs = [c for c in rclcpp_sub_cb_all_by_sub.get(ip_obj, []) if c in candidates]
            fired_ip = [c for c in ip_cbs if c in _firing_cbs]
            primary = (fired_ip or firing or ip_cbs or candidates)[0]
            alts = [c for c in firing if c != primary]
            if alts:
                alt_cbs_by_handle[sub_handle] = alts
            return primary
        # Multiple callbacks registered for this sub_handle — typical NITROS case
        if len(firing) == 1:
            warn(
                "nitros_duplicate_sub",
                f"sub_handle={sub_handle} topic={topic!r} had {len(candidates)} "
                f"candidate cb_addrs, picked firing {firing[0]}",
            )
            return firing[0]
        if firing:
            warn(
                "multi_firing_sub",
                f"sub_handle={sub_handle} topic={topic!r} had {len(firing)} "
                f"firing cb_addrs, keeping first {firing[0]}",
            )
            alt_cbs_by_handle[sub_handle] = firing[1:]
            return firing[0]
        warn(
            "no_firing_cb_for_sub",
            f"sub_handle={sub_handle} topic={topic!r} none of {len(candidates)} "
            f"candidate cb_addrs fired; keeping first {candidates[0]}",
        )
        return candidates[0]

    # Legacy keep-first dict kept around for any non-sub callsites that still
    # consult it directly (e.g. external boundary edges).
    rclcpp_sub_by_handle = _keep_first(rclcpp_sub_init, "subscription_handle", "subscription")
    rclcpp_sub_cb_by_sub = _keep_first(rclcpp_sub_cb_added, "subscription", "callback")

    symbol_by_cb: dict[str, str] = {}
    for d in rclcpp_cb_register:
        symbol_by_cb[d["payload"]["callback"]] = d["payload"]["symbol"]
    for d in rclpy_cb_register:
        symbol_by_cb[d["payload"]["callback"]] = d["payload"]["symbol"]

    srv_init_by_node: dict[str, list[tuple]] = {}
    for d in rcl_srv_init:
        p = d["payload"]
        srv_init_by_node.setdefault(p["node_handle"], []).append(
            (p["service_handle"], p["service_name"]))

    rclcpp_srv_cb_by_handle = _keep_first(rclcpp_srv_cb_added, "service_handle", "callback")
    rclcpp_tmr_cb_by_handle = _keep_first(rclcpp_tmr_cb_added, "timer_handle", "callback")
    rclpy_sub_cb_by_handle = _keep_first(rclpy_sub_cb_added, "subscription_handle", "callback")
    rclpy_srv_cb_by_handle = _keep_first(rclpy_srv_cb_added, "service_handle", "callback")
    rclpy_tmr_cb_by_handle = _keep_first(rclpy_tmr_cb_added, "timer_handle", "callback")

    def _resolve_cb(cb_ptr):
        sym = symbol_by_cb.get(cb_ptr)
        if sym:
            return {"callback": cb_ptr, "symbol": sym}
        return None

    # GPU pid set (oort detection works off cuda_runtime in PG)
    gpu_pids = _gpu_pids_from_pg(session_id)
    node_to_pid = {}
    for ex in executors.values():
        for n_id in ex.Z_v:
            node_to_pid[n_id] = ex.A_v["pid"]

    for node_id, node in nodes.items():
        is_gpu = node_to_pid.get(node_id) in gpu_pids
        node_handle = node.A_v["node_handle"]

        sub_chain = {}
        for sub_handle, topic in sub_init_by_node.get(node_handle, []):
            # Firing-aware resolution (handles NITROS duplicate sub_handles)
            cb_ptr = _resolve_sub_handle_to_cb(sub_handle, topic=topic)
            if cb_ptr:
                info = _resolve_cb(cb_ptr)
                if info:
                    ip = intra_wait_by_handle.get(sub_handle)
                    alts = alt_cbs_by_handle.get(sub_handle)
                    if ip or alts:
                        info = dict(info)
                        if ip:
                            info["intra_proc_waitable"] = ip
                            # primary cb is the intra-proc copy only if it fired
                            info["intra_proc_primary"] = cb_ptr in rclcpp_sub_cb_all_by_sub.get(ip, [])
                        if alts:
                            info["alt_cb_addrs"] = alts
                    sub_chain[topic] = info
                    continue
            cb_ptr = rclpy_sub_cb_by_handle.get(sub_handle)
            if cb_ptr:
                info = _resolve_cb(cb_ptr)
                if info:
                    sub_chain[topic] = info
                else:
                    warn("sub_no_register",
                         f"sub_handle={sub_handle} topic={topic!r} cb_ptr={cb_ptr} has no callback_register")
            elif sub_handle not in rclcpp_sub_objs_by_handle and sub_handle not in rclpy_sub_cb_by_handle:
                warn("sub_handle_unresolved",
                     f"sub_handle={sub_handle} topic={topic!r} (node={node.A_v.get('full_name')}) no cb_added event")

        srv_chain = {}
        for srv_handle, srv_name in srv_init_by_node.get(node_handle, []):
            cb_ptr = rclcpp_srv_cb_by_handle.get(srv_handle)
            if cb_ptr:
                info = _resolve_cb(cb_ptr)
                if info:
                    srv_chain[srv_name] = info
                    continue
            cb_ptr = rclpy_srv_cb_by_handle.get(srv_handle)
            if cb_ptr:
                info = _resolve_cb(cb_ptr)
                if info:
                    srv_chain[srv_name] = info

        for e_id in list(node.Z_v):
            entity = entities.get(e_id)
            if entity is None or entity.t_v != "E":
                continue
            etype = entity.A_v["etype"]
            cb_info = None
            if etype == "sub":
                cb_info = sub_chain.get(entity.A_v["label"])
            elif etype == "serv":
                cb_info = srv_chain.get(entity.A_v["label"])
            elif etype == "tmr":
                th = entity.A_v.get("timer_handle")
                if th:
                    cb_ptr = rclcpp_tmr_cb_by_handle.get(th)
                    if cb_ptr:
                        cb_info = _resolve_cb(cb_ptr)
                    if not cb_info:
                        cb_ptr = rclpy_tmr_cb_by_handle.get(th)
                        if cb_ptr:
                            cb_info = _resolve_cb(cb_ptr)
            elif etype == "waitable":
                # The Waitable's `this` ptr is the cb_addr (set in
                # identify_entities). It is also the value passed to
                # TRACEPOINT(callback_start) by our executor.cpp wrap. No
                # rclcpp_callback_register fires for Waitable subclasses, so
                # the symbol is not available — fall back to the entity label
                # (e.g. "waitable@abcdef").
                wp = entity.A_v.get("waitable_handle")
                if wp:
                    sym = symbol_by_cb.get(wp) or entity.A_v["label"]
                    cb_info = {"callback": wp, "symbol": sym}
            elif etype == "cli":
                # The rcl client handle is the cb_addr (executor.cpp
                # execute_client wrap). No callback_register fires for the
                # response path — label from the service name.
                ch = entity.A_v.get("client_handle")
                if ch:
                    cb_info = {"callback": ch,
                               "symbol": f"client:{entity.A_v['label']}",
                               "init_ts_ns": entity.A_v.get("init_ts_ns")}

            if cb_info:
                entity.A_v["cb_addr"] = cb_info["callback"]
                f_A = {"label": cb_info["symbol"], "ptype": "cpu",
                       "cb_addr": cb_info["callback"]}
                if cb_info.get("init_ts_ns") is not None:
                    # handle-reuse disambiguation for gantt (see clients_by_node)
                    f_A["init_ts_ns"] = cb_info["init_ts_ns"]
                if is_gpu:
                    f_A["gpu_node"] = True
                ip = cb_info.get("intra_proc_waitable")
                alts = cb_info.get("alt_cb_addrs")
                if ip:
                    # Intra-process-CAPABLE sub: it has a SubscriptionIntraProcess
                    # Waitable (ipc_waitable) and therefore TWO live delivery
                    # paths — executor branch 5 (same-process publisher → the
                    # Waitable's callback copy; outer callback_start carries the
                    # Waitable `this`, inner one the cb) and DDS (other-process
                    # publisher → plain Subscription callback). `delivery` says
                    # which path(s) fired in this session for the primary/alt cbs.
                    ipc_primary = bool(cb_info.get("intra_proc_primary"))
                    entity.A_v["ipc_waitable"] = ip
                    f_A["ipc_capable"] = True
                    f_A["ipc_waitable"] = ip
                    f_A["delivery"] = ("both" if alts else ("ipc" if ipc_primary else "dds"))
                if alts:
                    # Other FIRING callbacks of the same sub (e.g. the DDS copy
                    # when the intra-proc copy is primary, or vice versa).
                    entity.A_v["alt_cb_addrs"] = alts
                    f_A["alt_cb_addrs"] = alts
                f = FishVertex("F", next(vertex_counter), f_A, [], 3)
                entity.Z_v.append(f.id_v)
                functions[f.id_v] = f

    n_cap = sum(1 for f in functions.values() if f.A_v.get("ipc_capable"))
    dl = collections.Counter(f.A_v.get("delivery") for f in functions.values() if f.A_v.get("ipc_capable"))
    if n_cap:
        log(f"  Delivery: {n_cap} ipc-capable sub F node(s) — "
            f"{dl.get('ipc',0)} ipc / {dl.get('dds',0)} dds / {dl.get('both',0)} both")

    # Sanity check: any cb_addr that fired but didn't produce an F is a
    # silent attribution miss — warn at the end so it's visible.
    bound_cb_addrs = {f.A_v.get("cb_addr") for f in functions.values()}
    bound_cb_addrs |= {f.A_v.get("ipc_waitable") for f in functions.values()}
    for f in functions.values():
        bound_cb_addrs |= set(f.A_v.get("alt_cb_addrs") or [])
    missed = _firing_cbs - bound_cb_addrs - {None}
    if missed:
        warn("firing_cb_without_F",
             f"{len(missed)} cb_addr(s) fired but produced no F node — e.g. {next(iter(missed))}")

    log(f"identify_callbacks DONE in {time.time()-t0:.1f}s — {len(functions)} functions")
    _warn_summary()
    return functions


def _gpu_pids_from_pg(session_id: str) -> set[int]:
    """Find PIDs that issued CUDA work in this session.

    gpu_kernels.global_pid is the nsys-encoded id; OS pid = (global_pid >> 24)
    & 0xFFFFFF on our toolchain (verified against the apriltag fresh session:
    raw 281506400436224 → pid 1873 == component_container_mt). Fall back to
    cuda_runtime.global_tid >> 24 if gpu_kernels yields nothing.
    """
    rows = pg_store.fetch_all(
        "SELECT DISTINCT global_pid FROM gpu_kernels "
        "WHERE session_id = %s AND global_pid IS NOT NULL",
        (session_id,),
    )
    pids = {(int(r["global_pid"]) >> 24) & 0xFFFFFF for r in rows}
    if not pids:
        rows = pg_store.fetch_all(
            "SELECT DISTINCT global_tid FROM cuda_runtime "
            "WHERE session_id = %s AND global_tid IS NOT NULL",
            (session_id,),
        )
        pids = {(int(r["global_tid"]) >> 24) & 0xFFFFFF for r in rows}
    return pids


# ----------------------------------------------------------------------------
# Aspect attribution
# ----------------------------------------------------------------------------

def attribute_aspects(session_id, executors, nodes, entities):
    t0 = time.time()
    cb_to_entity = {}
    node_to_entities: dict = {}   # node_id → [e_id, ...]
    for n_id, node in nodes.items():
        for e_id in node.Z_v:
            if e_id in entities:
                node_to_entities.setdefault(n_id, []).append(e_id)
    e_to_node = {e_id: n_id for n_id, ents in node_to_entities.items() for e_id in ents}
    # Build pub_handle → owning Node lookup (from rcl_publisher_init)
    nh_to_node = {n.A_v["node_handle"]: n_id for n_id, n in nodes.items()}
    pub_handle_to_node = {}
    for d in _all_events(session_id, "ros2:rcl_publisher_init"):
        nh = d["payload"].get("node_handle")
        ph = d["payload"].get("publisher_handle")
        if nh in nh_to_node and ph:
            pub_handle_to_node[ph] = nh_to_node[nh]
    for e_id, e in entities.items():
        cb = e.A_v.get("cb_addr")
        if cb and cb != "NA":
            cb_to_entity[cb] = (e_id, e)
        # Outer branch-5 dispatch of an intra-process sub carries the
        # SubscriptionIntraProcess Waitable ptr as `callback`; map it to the
        # same entity so links emitted between the outer and inner
        # callback_start (e.g. from Waitable::execute bookkeeping) attribute
        # to the right place instead of being dropped as "unknown cb".
        ip = e.A_v.get("ipc_waitable")
        if ip:
            cb_to_entity.setdefault(ip, (e_id, e))
        for alt in (e.A_v.get("alt_cb_addrs") or []):
            cb_to_entity.setdefault(alt, (e_id, e))

    publish_links = _all_events(session_id, "ros2:fish_rclcpp_publish_link")
    client_links = _all_events(session_id, "ros2:fish_rclcpp_client_link")
    receive_links = _all_events(session_id, "ros2:fish_rclcpp_receive_link")

    if publish_links or client_links:
        log(f"attribute_aspects: tracepoint method "
            f"({len(publish_links)} pub links, {len(client_links)} cli links, "
            f"{len(receive_links)} recv links)")

        pub_handle_to_topic = {
            d["payload"]["publisher_handle"]: d["payload"]["topic_name"]
            for d in _all_events(session_id, "ros2:rcl_publisher_init")
        }
        cli_handle_to_service = {
            d["payload"]["client_handle"]: d["payload"]["service_name"]
            for d in _all_events(session_id, "ros2:rcl_client_init")
        }
        sub_handle_to_topic = {}
        sub_handle_to_node: dict[str, int] = {}
        for d in _all_events(session_id, "ros2:rcl_subscription_init"):
            p = d["payload"]
            sh = p.get("subscription_handle")
            if not sh:
                continue
            sub_handle_to_topic[sh] = p.get("topic_name")
            nh = p.get("node_handle")
            if nh in nh_to_node:
                sub_handle_to_node[sh] = nh_to_node[nh]
        seen_pub, seen_cli, seen_recv = set(), set(), set()
        total_pub = total_cli = total_recv = 0
        total_pub_unattributed = 0

        # Process receive_link first — sub_handle uniquely identifies the
        # sub entity (via owning node + topic). __fish_active_callback may
        # be NULL when the executor is between callbacks (DDS path through
        # execute_subscription); in that case fall back to the sub_handle
        # → (node, topic) → entity lookup. This is exact, not a heuristic.
        for d in receive_links:
            p = d["payload"]
            topic = sub_handle_to_topic.get(p["subscription_handle"])
            if not topic:
                continue
            if SKIP_PARAMSERVICE and _is_param_service(topic):
                continue
            pair = cb_to_entity.get(p["callback"])
            if not pair:
                # sub_handle → owning node → sub entity in that node with
                # matching topic. 1-to-1; not a heuristic.
                n_id = sub_handle_to_node.get(p["subscription_handle"])
                if n_id is None:
                    continue
                target = None
                for e_id in node_to_entities.get(n_id, []):
                    ent = entities[e_id]
                    if ent.A_v.get("etype") != "sub":
                        continue
                    for a in ent.A_v.get("aspects", []):
                        if a.get("aspect") == "sub" and a.get("topic") == topic:
                            target = (e_id, ent, a)
                            break
                    if target:
                        break
                if not target:
                    continue
                e_id, entity, aspect = target
                if (e_id, topic) in seen_recv:
                    continue
                seen_recv.add((e_id, topic))
                aspect["received"] = True
                total_recv += 1
                continue
            e_id, entity = pair
            if (e_id, topic) in seen_recv:
                continue
            seen_recv.add((e_id, topic))
            for a in entity.A_v.get("aspects", []):
                if a.get("aspect") == "sub" and a.get("topic") == topic:
                    a["received"] = True
                    a.setdefault("recv_cb_addrs", []).append(p["callback"])
                    total_recv += 1
                    break
            else:
                entity.A_v["aspects"].append({
                    "aspect": "sub", "topic": topic, "received": True,
                    "recv_cb_addrs": [p["callback"]],
                })
                total_recv += 1

        # publish_link: cb_to_entity look-up only. With Waitable as a
        # first-class entity (and Executor::execute_waitable wrapped with
        # callback_start using the Waitable's `this`), every legitimate
        # publish_link should resolve to a registered entity (sub/tmr/serv/
        # waitable). If it does not, log + drop — no heuristic fallback.
        for d in publish_links:
            p = d["payload"]
            topic = pub_handle_to_topic.get(p["publisher_handle"])
            if not topic:
                continue
            if SKIP_PARAMSERVICE and _is_param_service(topic):
                continue
            pair = cb_to_entity.get(p["callback"])
            if not pair:
                total_pub_unattributed += 1
                continue
            e_id, entity = pair
            if (e_id, topic) not in seen_pub:
                seen_pub.add((e_id, topic))
                entity.A_v["aspects"].append({"aspect": "pub", "topic": topic})
                total_pub += 1
        for d in client_links:
            p = d["payload"]
            service = cli_handle_to_service.get(p["client_handle"])
            if not service:
                continue
            if SKIP_PARAMSERVICE and _is_param_service(service):
                continue
            pair = cb_to_entity.get(p["callback"])
            if not pair:
                continue
            e_id, entity = pair
            if (e_id, service) in seen_cli:
                continue
            seen_cli.add((e_id, service))
            entity.A_v["aspects"].append({"aspect": "cli", "service": service})
            total_cli += 1
        log(f"  Tracepoint attribution: {total_pub} pub "
            f"({total_pub_unattributed} dropped — unknown cb), "
            f"{total_cli} cli, {total_recv} recv")
    else:
        log("attribute_aspects: fallback (runtime callback window scan)")
        _attribute_via_runtime(session_id, executors, nodes, entities, cb_to_entity)
    log(f"attribute_aspects DONE in {time.time()-t0:.1f}s")


def _attribute_via_runtime(session_id, executors, nodes, entities, cb_to_entity):
    node_to_pid = {}
    for ex in executors.values():
        for n_id in ex.Z_v:
            node_to_pid[n_id] = ex.A_v["pid"]
    pids_needing = set()
    for node_id, node in nodes.items():
        if node.A_v.get("publishers") or node.A_v.get("service_clients"):
            pid = node_to_pid.get(node_id)
            if pid:
                pids_needing.add(pid)

    pub_ht_by_pid: dict[int, dict] = {}
    for d in _all_events(session_id, "ros2:rcl_publisher_init"):
        pid = d["vpid"]
        if pid in pids_needing:
            pub_ht_by_pid.setdefault(pid, {})[
                d["payload"]["publisher_handle"]] = d["payload"]["topic_name"]
    cli_ht_by_pid: dict[int, dict] = {}
    for d in _all_events(session_id, "ros2:rcl_client_init"):
        pid = d["vpid"]
        if pid in pids_needing:
            cli_ht_by_pid.setdefault(pid, {})[
                d["payload"]["client_handle"]] = d["payload"]["service_name"]

    events_by_pid: dict[int, list] = {}
    for evt_name, evt_type, hkey in [
        ("ros2:callback_start", "cb_start", "callback"),
        ("ros2:callback_end", "cb_end", "callback"),
        ("ros2:rcl_publish", "publish", "publisher_handle"),
        ("ros2:fish_rclcpp_client_request_sent", "cli_req", "client_handle"),
    ]:
        for d in _all_events(session_id, evt_name):
            pid = d["vpid"]
            if pid not in pids_needing:
                continue
            events_by_pid.setdefault(pid, []).append(
                (d["ts_ns"], evt_type, d["vtid"], d["payload"][hkey]))
    for pid in events_by_pid:
        events_by_pid[pid].sort()

    total_pub = total_cli = 0
    for node_id, node in nodes.items():
        pid = node_to_pid.get(node_id)
        if not pid:
            continue
        pub_ht = pub_ht_by_pid.get(pid, {})
        cli_ht = cli_ht_by_pid.get(pid, {})
        active_cbs = {}
        entity_pubs: dict[str, set] = {}
        entity_clis: dict[str, set] = {}
        for ns, evt_type, vtid, handle in events_by_pid.get(pid, []):
            if evt_type == "cb_start":
                active_cbs[vtid] = handle
            elif evt_type == "cb_end":
                active_cbs.pop(vtid, None)
            elif evt_type == "publish":
                cb = active_cbs.get(vtid)
                if cb is None and active_cbs:
                    cb = next(iter(active_cbs.values()))
                if cb:
                    topic = pub_ht.get(handle)
                    if topic and not (SKIP_PARAMSERVICE and _is_param_service(topic)):
                        entity_pubs.setdefault(cb, set()).add(topic)
            elif evt_type == "cli_req":
                cb = active_cbs.get(vtid)
                if cb is None and active_cbs:
                    cb = next(iter(active_cbs.values()))
                if cb:
                    service = cli_ht.get(handle)
                    if service and not (SKIP_PARAMSERVICE and _is_param_service(service)):
                        entity_clis.setdefault(cb, set()).add(service)
        for cb, topics in entity_pubs.items():
            pair = cb_to_entity.get(cb)
            if not pair:
                continue
            e_id, entity = pair
            existing = {a.get("topic") for a in entity.A_v["aspects"]
                        if a.get("aspect") == "pub" and a.get("topic")}
            for topic in topics:
                if topic not in existing:
                    entity.A_v["aspects"].append({"aspect": "pub", "topic": topic})
                    total_pub += 1
        for cb, services in entity_clis.items():
            pair = cb_to_entity.get(cb)
            if not pair:
                continue
            e_id, entity = pair
            existing = {a.get("service") for a in entity.A_v["aspects"]
                        if a.get("aspect") == "cli" and a.get("service")}
            for service in services:
                if service not in existing:
                    entity.A_v["aspects"].append({"aspect": "cli", "service": service})
                    total_cli += 1
    log(f"  Runtime attribution: {total_pub} pub, {total_cli} cli")


# ----------------------------------------------------------------------------
# Scheduler introspection (callback groups + executor type)
# ----------------------------------------------------------------------------

def attach_callback_groups(session_id, executors, nodes, entities, functions):
    t0 = time.time()
    exec_init_docs = _all_events(session_id, "ros2:fish_rclcpp_executor_init")
    cg_init_docs = _all_events(session_id, "ros2:fish_rclcpp_callback_group_init")
    cg_add_docs = _all_events(session_id, "ros2:fish_rclcpp_cbgroup_add")
    ex_add_docs = _all_events(session_id, "ros2:fish_rclcpp_executor_add_cbgroup")
    if not (exec_init_docs or cg_init_docs or cg_add_docs or ex_add_docs):
        log("attach_callback_groups: no scheduler events, skipping")
        return
    log(f"attach_callback_groups: "
        f"{len(exec_init_docs)} exec_init, {len(cg_init_docs)} cg_init, "
        f"{len(cg_add_docs)} cg_add, {len(ex_add_docs)} ex_add")

    cg_info = {}
    for d in cg_init_docs:
        p = d["payload"]
        cg_info[p["group_addr"]] = {
            "type": p["group_type"],
            "automatic_add": bool(p["automatic_add"]),
            "pid": d["vpid"],
        }
    entity_to_group = {}
    for d in cg_add_docs:
        p = d["payload"]
        entity_to_group[p["entity_addr"]] = {
            "group_addr": p["group_addr"],
            "kind": p["entity_kind"],
        }
    executors_per_pid = {}
    for d in exec_init_docs:
        p = d["payload"]
        executors_per_pid.setdefault(d["vpid"], []).append({
            "executor_addr": p["executor_addr"],
            "type": p["executor_type"],
            "num_threads": p["num_threads"],
        })
    exec_to_groups = {}
    for d in ex_add_docs:
        p = d["payload"]
        exec_to_groups.setdefault(p["executor_addr"], set()).add(p["group_addr"])

    # Map sub/srv handles back to (topic_or_name, node_handle, kind)
    handle_to_topic_node = {}
    for d in _all_events(session_id, "ros2:rcl_subscription_init"):
        p = d["payload"]
        handle_to_topic_node[p["subscription_handle"]] = (p["topic_name"], p["node_handle"], "sub")
    for d in _all_events(session_id, "ros2:rcl_service_init"):
        p = d["payload"]
        handle_to_topic_node[p["service_handle"]] = (p["service_name"], p["node_handle"], "serv")

    for ex_id, ex in executors.items():
        pid = ex.A_v.get("pid")
        if pid is None:
            continue
        execs = executors_per_pid.get(pid, [])
        if not execs:
            continue
        execs_ranked = sorted(
            execs, key=lambda e: len(exec_to_groups.get(e["executor_addr"], [])),
            reverse=True,
        )
        primary = execs_ranked[0]
        ex.A_v["executor_addr"] = primary["executor_addr"]
        ex.A_v["executor_type"] = primary["type"]
        ex.A_v["num_threads"] = primary["num_threads"]
        seen = set()
        cb_groups_list = []
        for e in execs:
            for g_addr in exec_to_groups.get(e["executor_addr"], []):
                if g_addr in seen:
                    continue
                seen.add(g_addr)
                ci = cg_info.get(g_addr, {})
                cb_groups_list.append({
                    "id": g_addr, "type": ci.get("type", "Unknown"),
                    "automatic_add": ci.get("automatic_add"),
                })
        if cb_groups_list:
            ex.A_v["cb_groups"] = cb_groups_list
        if len(execs) > 1:
            ex.A_v["extra_executors"] = [
                {"addr": e["executor_addr"], "type": e["type"],
                 "num_threads": e["num_threads"]} for e in execs_ranked[1:]
            ]

    cg_on_e = 0
    for e_id, e in entities.items():
        etype = e.A_v.get("etype")
        rcl_handle = None
        if etype == "tmr":
            rcl_handle = e.A_v.get("timer_handle")
        elif etype == "waitable":
            # cbgroup_add fires with entity_kind="wait" and entity_ptr = waitable_ptr,
            # which is what we stored in A_v["waitable_handle"].
            rcl_handle = e.A_v.get("waitable_handle")
        elif etype in ("sub", "serv"):
            label = e.A_v.get("label")
            parent_nh = None
            for nid, n in nodes.items():
                if e_id in n.Z_v:
                    parent_nh = n.A_v.get("node_handle")
                    break
            if parent_nh is None:
                continue
            for h, (name, nh, kind) in handle_to_topic_node.items():
                if nh == parent_nh and name == label and kind == etype:
                    rcl_handle = h
                    break

        if rcl_handle is None:
            continue
        mapping = entity_to_group.get(rcl_handle)
        if not mapping:
            continue
        ci = cg_info.get(mapping["group_addr"])
        if not ci:
            continue
        cg_attr = {
            "id": mapping["group_addr"], "type": ci["type"],
            "automatic_add": ci["automatic_add"],
        }
        e.A_v["callback_group"] = cg_attr
        cg_on_e += 1
        for f_id in e.Z_v:
            f = functions.get(f_id)
            if f:
                f.A_v["callback_group"] = cg_attr

    log(f"attach_callback_groups DONE in {time.time()-t0:.1f}s — "
        f"{cg_on_e}/{len(entities)} entities got CG info")


# ----------------------------------------------------------------------------
# Out-of-ROS threads
# ----------------------------------------------------------------------------

def detect_oort_threads(session_id, executors, nodes, entities, functions):
    gpu_pids = _gpu_pids_from_pg(session_id)
    if not gpu_pids:
        log("detect_oort_threads: no GPU processes, skipping")
        return

    cb_tids_by_pid: dict[int, set] = {}
    rows = pg_store.fetch_all(
        "SELECT DISTINCT vpid, vtid FROM ros2_trace "
        "WHERE session_id = %s AND event = 'ros2:callback_start'",
        (session_id,),
    )
    for r in rows:
        cb_tids_by_pid.setdefault(r["vpid"], set()).add(r["vtid"])

    rows = pg_store.fetch_all(
        "SELECT DISTINCT tid FROM cuda_runtime WHERE session_id = %s "
        "AND tid IS NOT NULL",
        (session_id,),
    )
    cuda_tids = {int(r["tid"]) for r in rows}
    if not cuda_tids:
        log("detect_oort_threads: no CUDA tids")
        return

    n_by_pid = {}
    for ex in executors.values():
        pid = ex.A_v.get("pid")
        if pid is None:
            continue
        for n_id in ex.Z_v:
            n = nodes.get(n_id)
            if n is not None:
                n_by_pid.setdefault(pid, n)

    created = 0
    for pid in gpu_pids:
        n_vertex = n_by_pid.get(pid)
        if n_vertex is None:
            continue
        cb_tids = cb_tids_by_pid.get(pid, set())
        for tid in sorted(cuda_tids):
            if tid in cb_tids:
                continue
            label = f"tid_{tid}"
            oore_A = {"label": f"{label}_io", "etype": "oore", "cb_addr": "NA",
                      "thread_tid": tid, "aspects": []}
            oore = FishVertex("E", next(vertex_counter), oore_A, [], 2)
            oort_A = {"label": f"{label}_gpu_loop", "ptype": "oort",
                      "gpu_node": True, "thread_tid": tid, "cb_addr": "NA"}
            oort = FishVertex("F", next(vertex_counter), oort_A, [], 3)
            oore.Z_v.append(oort.id_v)
            n_vertex.Z_v.append(oore.id_v)
            entities[oore.id_v] = oore
            functions[oort.id_v] = oort
            created += 1
    log(f"detect_oort_threads: {created} oort thread(s)")


# ----------------------------------------------------------------------------
# Action detection + split callbacks (mirror of model_improved)
# ----------------------------------------------------------------------------

def detect_joins(nodes, entities, functions):
    """Multi-input "join" nodes, measured from publish attribution.

    A ROS node that combines several inputs inside its own memory (Autoware
    concatenate_data collectors, message_filters sync policies, sensor
    fusion buffers) publishes the merged result from WHICHEVER input
    callback completes the set — or from a timeout timer. The in-memory
    hand-off is invisible to ROS-level tracing, but its *signature* is not:
    the SAME output topic carries a pub aspect on ≥2 different subscription
    callbacks of the SAME node (they were each, at some point, the last one
    in). That is the detection rule — no heuristic about the app internals.

    Members of the join = the completing sub F's + the node's other sub F's
    whose entity carries the same message type as a completing input (the
    only structural assumption; e.g. Autoware's right lidar never completed
    a set in the traced session but subscribes the same PointCloud2 stream)
    + the node's timers that also published the topic (timeout path).

    Annotation only — NO data edge is invented for members that did not
    publish. F attrs: join_group=<output topic>, join_role=
    completer|member|timeout. Node attrs: joins={topic: [f_ids]}.
    add_horizontal_edges then ties members with nature="join" L3 edges so
    the FT (task) grouping keeps a join's inputs together; those edges are
    membership, not message hops (chain-latency tools must not count them).
    """
    n_joins = 0
    for n_id, node in nodes.items():
        # entity → (etype, topics it publishes, sub topic, msg_type, f_ids)
        ents = []
        for e_id in node.Z_v:
            e = entities.get(e_id)
            if e is None or e.t_v != "E":
                continue
            etype = e.A_v.get("etype")
            pubs = {a.get("topic") for a in e.A_v.get("aspects", []) if a.get("aspect") == "pub" and a.get("topic")}
            sub_topic = None; msg_type = None
            for a in e.A_v.get("aspects", []):
                if a.get("aspect") == "sub" and a.get("topic"):
                    sub_topic = a["topic"]; msg_type = a.get("msg_type"); break
            f_ids = [f for f in e.Z_v if f in functions]
            ents.append((e_id, etype, pubs, sub_topic, msg_type, f_ids))
        # candidate join outputs: topics published by ≥2 distinct SUB entities
        topic_pub_subs = {}
        for e_id, etype, pubs, sub_topic, msg_type, f_ids in ents:
            if etype == "sub":
                for t in pubs:
                    topic_pub_subs.setdefault(t, []).append((e_id, msg_type))
        for topic, subs in topic_pub_subs.items():
            if len(subs) < 2:
                continue
            # only DATA outputs qualify: /diagnostics, */debug/*, processing_time
            # etc. are published from many callbacks without any join semantics
            if topic_class_for_join(topic) != "data":
                continue
            completer_types = {mt for _, mt in subs if mt}
            completer_e = {e_id for e_id, _ in subs}
            members = {}
            for e_id, etype, pubs, sub_topic, msg_type, f_ids in ents:
                if etype == "sub":
                    if e_id in completer_e:
                        members[e_id] = "completer"
                    elif msg_type and msg_type in completer_types and sub_topic and topic_class_for_join(sub_topic) == "data":
                        members[e_id] = "member"
                elif etype == "tmr" and topic in pubs:
                    members[e_id] = "timeout"
            f_list = []
            for e_id, role in members.items():
                for f_id in entities[e_id].Z_v:
                    f = functions.get(f_id)
                    if f is None:
                        continue
                    # an F can take part in several joins (concat completes
                    # the merged cloud AND the per-lidar synced clouds):
                    # joins = {output_topic: role}; join_group = first one
                    # (display / grouping key), join_role = its role there.
                    f.A_v.setdefault("joins", {})[topic] = role
                    f.A_v.setdefault("join_group", topic)
                    f.A_v.setdefault("join_role", role)
                    f_list.append(f_id)
                entities[e_id].A_v.setdefault("joins", {})[topic] = role
                entities[e_id].A_v.setdefault("join_group", topic)
                entities[e_id].A_v.setdefault("join_role", role)
            node.A_v.setdefault("joins", {})[topic] = f_list
            n_joins += 1
            log(f"  join: {node.A_v.get('full_name')} → {topic}: "
                f"{sum(1 for r in members.values() if r=='completer')} completer(s), "
                f"{sum(1 for r in members.values() if r=='member')} member(s), "
                f"{sum(1 for r in members.values() if r=='timeout')} timeout timer(s)")
    log(f"detect_joins: {n_joins} join output(s) found")


def detect_state_links(session_id, nodes, entities, functions):
    """Sample-and-hold ("state") links inside a node — Layer A of the task model
    (notes/design_task_model_from_ft.txt).

    ROS 2 nodes of the timer-driven / latest-value kind (Case 1b in
    notes/join_semantics_from_traces.txt) receive inputs in cheap subscription
    callbacks that only DEPOSIT the message into node memory, and do the work
    in another callback (a timer, or a sub-callback of a different input)
    that READS the latest deposited values and publishes. No ROS layer is
    crossed by that read, so there is no traced edge — the FT graph breaks
    exactly there (which is right: the trigger chain ends), but the DATA
    dependency, and hence the data age across the boundary, is lost.

    Detection (structural, DECLARED assumption, marked inferred=True):
      in one node, S = data-subscription F's that fired at least once and
      publish NOTHING (deposit-only) and are not join members;
      P = F's of the same node that publish at least one DATA topic
      (timers, sub-completers, services);
      → for every s∈S, p∈P: state link s → p  (nature='state').
    The link means "p reads the latest value deposited by s"; it is NOT a
    trigger/precedence edge (FT grouping ignores it) and it carries a data
    age (measured by measure_flows: p's start − s's last start).
    Verification of the assumption is the job of the optional uprobe
    instrument (notes/plan_uprobe_join_verification.txt).
    """
    # firing set (deposit-only subs that never fired carry no state)
    fired = set()
    for r in pg_store.fetch_all(
        "SELECT DISTINCT payload->>'callback' AS cb FROM ros2_trace "
        "WHERE session_id=%s AND event='ros2:callback_start'", (session_id,)):
        if r["cb"]:
            fired.add(r["cb"])
    n_links = 0; n_nodes = 0
    for n_id, node in nodes.items():
        S, P = [], []
        for e_id in node.Z_v:
            e = entities.get(e_id)
            if e is None or e.t_v != "E":
                continue
            asp = e.A_v.get("aspects", [])
            pubs = [a for a in asp if a.get("aspect") == "pub" and a.get("topic")
                    and topic_class_for_join(a["topic"]) == "data"]
            sub_t = next((a.get("topic") for a in asp if a.get("aspect") == "sub" and a.get("topic")), None)
            fs = [f for f in e.Z_v if f in functions]
            if not fs:
                continue
            if pubs:
                P.extend(fs)
            elif (e.A_v.get("etype") == "sub" and sub_t and topic_class_for_join(sub_t) == "data"
                  and not e.A_v.get("join_group")):
                for f in fs:
                    fa = functions[f].A_v
                    cbs = [fa.get("cb_addr")] + list(fa.get("alt_cb_addrs") or []) + [fa.get("ipc_waitable")]
                    if any(c in fired for c in cbs if c):
                        S.append((f, sub_t))
        if not S or not P:
            continue
        n_nodes += 1
        for f_s, t in S:
            functions[f_s].A_v.setdefault("state_consumers", [])
            for f_p in P:
                functions[f_p].A_v.setdefault("state_inputs", []).append(t)
                functions[f_s].A_v["state_consumers"].append(f_p)
                node.A_v.setdefault("state_links", []).append([f_s, f_p, t])
                n_links += 1
    log(f"detect_state_links: {n_links} inferred state link(s) in {n_nodes} node(s)")


def _callback_windows(session_id, functions):
    """Nesting-aware callback windows per (vpid, vtid) from callback_start/end.
    Returns (wins, wstarts, owner, starts_by_f):
      wins[(vpid,vtid)] = sorted [(t0, t1, f_id)]
      owner(key, ts)   → f_id of the innermost recent window containing ts (or None)
      starts_by_f[f]   = sorted start times of F's instances
    A callback address maps to an F through cb_addr / alt_cb_addrs / ipc_waitable."""
    import bisect
    from collections import defaultdict
    cb2f = {}
    for f_id, f in functions.items():
        for c in [f.A_v.get("cb_addr")] + list(f.A_v.get("alt_cb_addrs") or []) + [f.A_v.get("ipc_waitable")]:
            if c and c != "NA":
                cb2f.setdefault(c, f_id)
    wins = defaultdict(list); stack = defaultdict(list)
    for r in pg_store.fetch_all(
        "SELECT vpid, vtid, ts_ns, event, payload->>'callback' AS cb FROM ros2_trace "
        "WHERE session_id=%s AND event IN ('ros2:callback_start','ros2:callback_end') "
        "ORDER BY vpid, vtid, ts_ns", (session_id,)):
        key = (r["vpid"], r["vtid"]); ts = int(r["ts_ns"])
        if r["event"].endswith("start"):
            stack[key].append((ts, r["cb"]))
        elif stack[key]:
            t0, cb0 = stack[key].pop()
            f = cb2f.get(cb0)
            if f is not None:
                wins[key].append((t0, ts, f))
    for k in wins: wins[k].sort()
    wstarts = {k: [w[0] for w in ws] for k, ws in wins.items()}
    def owner(key, ts):
        ws = wins.get(key)
        if not ws: return None
        i = bisect.bisect_right(wstarts[key], ts) - 1
        for j in range(i, max(-1, i - 8), -1):
            t0, t1, f = ws[j]
            if t0 <= ts <= t1: return f
        return None
    starts_by_f = defaultdict(list)
    for key, ws in wins.items():
        for t0, t1, f in ws: starts_by_f[f].append(t0)
    for f in starts_by_f: starts_by_f[f].sort()
    return wins, wstarts, owner, starts_by_f


def _sub_f_by_node_topic(nodes, entities, functions):
    """(node_handle, topic) → [sub F ids]. rcl_subscription_init carries
    node_handle + topic + rmw_subscription_handle, so an rmw_take can be
    attributed to the subscription's F even when that F's callback never
    fires (polled subscriptions)."""
    m = {}
    for n_id, node in nodes.items():
        nh = node.A_v.get("node_handle")
        for e_id in node.Z_v:
            e = entities.get(e_id)
            if e is None or e.t_v != "E" or e.A_v.get("etype") != "sub":
                continue
            fs = [f for f in e.Z_v if f in functions]
            if fs:
                m.setdefault((nh, e.A_v.get("label")), []).extend(fs)
    return m


def detect_polled_subs(session_id, nodes, entities, functions):
    """Polled subscriptions (Autoware InterProcessPollingSubscriber & co.).

    The subscription exists (rcl_subscription_init, a registered callback
    that NEVER fires) and the message is pulled with take() from INSIDE
    another callback — typically the node's timer. The ROS layer emits
    ros2:rmw_take for that pull, and the pull lies inside the reader's
    callback_start/end window. That is direct evidence (not an inference)
    of a sample-and-hold read: reader = enclosing callback, data age =
    take_ts − DDS source_timestamp (measure_flows, flow_method='polled_take').

    Marks: sub F.polled=True, F.poll_readers={reader_f: n}, E.polled=True;
    node.state_links gets [sub_f, reader_f, topic] plus node.polled_links
    [sub_f, reader_f, topic, n] so the L3 state edge is tagged
    polled=True / inferred=False. mark_phases keeps polled sub F's in the
    data phase (their own callback never fires by design).
    Only taken=1 counts; a take inside the sub's OWN callback (normal
    dispatch after wait) is not polling."""
    from collections import Counter
    wins, wstarts, owner, _ = _callback_windows(session_id, functions)
    if not wins:
        return
    subf = _sub_f_by_node_topic(nodes, entities, functions)
    rh2 = {}
    for r in pg_store.fetch_all(
        "SELECT vpid, payload->>'rmw_subscription_handle' AS rh, payload->>'topic_name' AS t, "
        "payload->>'node_handle' AS nh FROM ros2_trace WHERE session_id=%s AND event='ros2:rcl_subscription_init'",
        (session_id,)):
        rh2[(r["vpid"], r["rh"])] = (r["nh"], r["t"])
    polls = Counter()            # (sub_f, reader_f, topic) → n
    n_take_in = 0
    for r in pg_store.fetch_all(
        "SELECT vpid, vtid, ts_ns, payload->>'rmw_subscription_handle' AS rh FROM ros2_trace "
        "WHERE session_id=%s AND event='ros2:rmw_take' AND payload->>'taken' IN ('1','true')", (session_id,)):
        nt = rh2.get((r["vpid"], r["rh"]))
        if not nt: continue
        fs = subf.get(nt)
        if not fs: continue
        rd = owner((r["vpid"], r["vtid"]), int(r["ts_ns"]))
        if rd is None or rd in fs: continue
        n_take_in += 1
        for f in fs:
            polls[(f, rd, nt[1])] += 1
    if not polls:
        log("detect_polled_subs: no polled subscriptions (no rmw_take inside foreign callback windows)")
        return
    f2node = {}
    for n_id, node in nodes.items():
        for e_id in node.Z_v:
            e = entities.get(e_id)
            if e is None: continue
            for f in e.Z_v: f2node[f] = n_id
    n_sub = set(); n_links = 0
    for (f_s, f_p, topic), n in polls.items():
        if topic_class_for_join(topic) != "data":
            continue
        fa = functions[f_s].A_v
        fa["polled"] = True
        fa.setdefault("poll_readers", {})[f_p] = n
        n_sub.add(f_s)
        node = nodes.get(f2node.get(f_s))
        if node is None: continue
        existing = {(a, b) for a, b, _t in (node.A_v.get("state_links") or [])}
        if (f_s, f_p) not in existing:
            node.A_v.setdefault("state_links", []).append([f_s, f_p, topic])
        node.A_v.setdefault("polled_links", []).append([f_s, f_p, topic, n])
        functions[f_p].A_v.setdefault("state_inputs", []).append(topic)
        functions[f_s].A_v.setdefault("state_consumers", []).append(f_p)
        n_links += 1
    for n_id, node in nodes.items():
        for e_id in node.Z_v:
            e = entities.get(e_id)
            if e is not None and e.t_v == "E" and any(f in n_sub for f in e.Z_v):
                e.A_v["polled"] = True
    log(f"detect_polled_subs: {len(n_sub)} polled sub F(s), {n_links} polled state link(s) "
        f"({n_take_in} rmw_take inside foreign callback windows)")


def measure_flows(G, session_id, functions, nodes=None, entities=None):
    """Per-instance flow measurement on the finished graph (needs a session
    traced with [trace] per_instance = true: ros2:rcl_publish + ros2:rmw_take;
    degrades to intra-process pairing + state ages when rmw_take is absent).

    For every L3 comm edge:
      nature=msg   pub_F → sub_F   hop latency = take (or ipc dispatch) − publish,
                                    per instance, paired by DDS source_timestamp
                                    (inter-process) or by time (intra-process)
      nature=state s → p           data age = start(p instance) − last start(s)
    Writes n / p50 / p90 / max (ns) into the edge attrs:
      flow_n, hop_ns_p50, hop_ns_p90, hop_ns_max          (msg edges)
      flow_n, age_ns_p50, age_ns_p90, age_ns_max          (state edges)
      flow_method: 'rmw_take' | 'ipc_time' | 'state'
    """
    import bisect
    from collections import defaultdict
    def q(a, p): a = sorted(a); return a[int(p * (len(a) - 1))]

    wins, wstarts, owner, starts_by_f = _callback_windows(session_id, functions)
    subf = _sub_f_by_node_topic(nodes, entities, functions) if nodes is not None else {}

    # publishes: rcl_publish → (topic, pub_F, ts)
    pub_topic = {}
    for r in pg_store.fetch_all(
        "SELECT vpid, payload->>'publisher_handle' AS h, payload->>'topic_name' AS t FROM ros2_trace "
        "WHERE session_id=%s AND event='ros2:rcl_publisher_init'", (session_id,)):
        pub_topic[(r["vpid"], r["h"])] = r["t"]
    pubs = defaultdict(list)   # (topic, pub_F) → [ts]
    n_pub = 0
    for r in pg_store.fetch_all(
        "SELECT vpid, vtid, ts_ns, payload->>'publisher_handle' AS h FROM ros2_trace "
        "WHERE session_id=%s AND event='ros2:rcl_publish'", (session_id,)):
        t = pub_topic.get((r["vpid"], r["h"]))
        if not t: continue
        f = owner((r["vpid"], r["vtid"]), int(r["ts_ns"]))
        if f is None: continue
        pubs[(t, f)].append(int(r["ts_ns"])); n_pub += 1
    for k in pubs: pubs[k].sort()
    if n_pub == 0:
        log("measure_flows: no ros2:rcl_publish events (per_instance off) — hop latencies skipped; state ages only")

    # takes: rmw_take → (topic, sub_F, take_ts, source_ts)
    rmw2topic = {}; rmw2nt = {}
    for r in pg_store.fetch_all(
        "SELECT vpid, payload->>'rmw_subscription_handle' AS rh, payload->>'topic_name' AS t, "
        "payload->>'node_handle' AS nh FROM ros2_trace "
        "WHERE session_id=%s AND event='ros2:rcl_subscription_init'", (session_id,)):
        rmw2topic[(r["vpid"], r["rh"])] = r["t"]; rmw2nt[(r["vpid"], r["rh"])] = (r["nh"], r["t"])
    takes = defaultdict(list)  # (topic, sub_F) → [(take_ts, src_ts)]   dispatched takes (hop latency)
    polled = defaultdict(list) # (topic, sub_F, reader_F) → [(take_ts, src_ts)]   polled reads (data age)
    n_take = n_poll = 0
    for r in pg_store.fetch_all(
        "SELECT vpid, vtid, ts_ns, payload->>'rmw_subscription_handle' AS rh, "
        "(payload->>'source_timestamp')::bigint AS st FROM ros2_trace "
        "WHERE session_id=%s AND event='ros2:rmw_take' AND payload->>'taken' IN ('1','true')", (session_id,)):
        t = rmw2topic.get((r["vpid"], r["rh"]))
        if not t: continue
        key = (r["vpid"], r["vtid"]); ts = int(r["ts_ns"])
        st = int(r["st"]) if r["st"] is not None else None
        ws = wins.get(key)
        if not ws: continue
        # (a) take INSIDE a callback window that is not the sub's own callback →
        #     polled read: consumer = enclosing callback (see detect_polled_subs)
        rd = owner(key, ts)
        sfs = subf.get(rmw2nt.get((r["vpid"], r["rh"]))) or []
        if rd is not None and sfs and rd not in sfs:
            for sf in sfs:
                polled[(t, sf, rd)].append((ts, st))
                takes[(t, sf)].append((ts, st))    # hop latency pub → sub F stays measurable
            n_poll += 1
            continue
        # (b) dispatched take: the message is consumed by the NEXT callback on this thread
        i = bisect.bisect_left(wstarts[key], ts)
        if i < len(ws) and ws[i][0] - ts <= 5_000_000:
            takes[(t, ws[i][2])].append((ts, st)); n_take += 1
    for k in takes: takes[k].sort()
    for k in polled: polled[k].sort()

    n_msg = n_state = 0; n_pairs = 0
    for u, v, d in G.edges(data=True):
        if d.get("rel") != "comm" or d.get("level") != "L3":
            continue
        nat = d.get("nature")
        if nat == "state":
            if d.get("polled"):
                # measured read: age = take − DDS source_timestamp (publish time)
                pr = polled.get((d.get("topic"), u, v), [])
                ages = [ts - st for ts, st in pr if st is not None and ts >= st]
                if ages:
                    d.update(flow_n=len(ages), age_ns_p50=q(ages, .5), age_ns_p90=q(ages, .9), age_ns_max=max(ages), flow_method="polled_take")
                    n_state += 1
                continue
            s_starts = starts_by_f.get(u, []); p_starts = starts_by_f.get(v, [])
            ages = []
            for tp in p_starts:
                i = bisect.bisect_left(s_starts, tp)
                if i > 0: ages.append(tp - s_starts[i - 1])
            if ages:
                d.update(flow_n=len(ages), age_ns_p50=q(ages, .5), age_ns_p90=q(ages, .9), age_ns_max=max(ages), flow_method="state")
                n_state += 1
            continue
        if nat not in ("msg", None):
            continue
        topic = d.get("topic")
        if not topic: continue
        pts = pubs.get((topic, u))
        if not pts: continue
        lat = []
        tk = takes.get((topic, v))
        if tk:
            # inter-process: pair each take with the publish whose ts is closest to
            # the DDS source_timestamp (same host → same clock domain)
            for take_ts, src in tk:
                ref = src if src else take_ts
                i = bisect.bisect_left(pts, ref)
                cand = [pts[j] for j in (i - 1, i) if 0 <= j < len(pts)]
                if not cand: continue
                pt = min(cand, key=lambda x: abs(x - ref))
                if abs(pt - ref) <= 50_000_000 and take_ts >= pt:
                    lat.append(take_ts - pt)
            method = "rmw_take"
        else:
            # intra-process (or per_instance without rmw_take): each start of the
            # consumer F pairs with the latest publish before it (≤ 50 ms)
            for tv in starts_by_f.get(v, []):
                i = bisect.bisect_right(pts, tv) - 1
                if i >= 0 and tv - pts[i] <= 50_000_000:
                    lat.append(tv - pts[i])
            method = "ipc_time"
        if lat:
            d.update(flow_n=len(lat), hop_ns_p50=q(lat, .5), hop_ns_p90=q(lat, .9), hop_ns_max=max(lat), flow_method=method)
            n_msg += 1; n_pairs += len(lat)
    log(f"measure_flows: {n_msg} msg edge(s) with hop latency ({n_pairs} pairs; publishes {n_pub}, takes {n_take}, polled takes {n_poll}), "
        f"{n_state} state edge(s) with data age")


def topic_class_for_join(topic: str) -> str:
    """Infra topics never count as join members (mirrors fish_viz_server)."""
    try:
        from postprocess.fish_viz_server import topic_class
        return topic_class(topic)
    except Exception:
        return "infra" if topic in ("/clock", "/parameter_events", "/tf", "/tf_static", "/rosout") else "data"


def detect_actions(entities):
    ACTION_SRV_ROLES = {
        "send_goal": "goal", "cancel_goal": "cancel", "get_result": "result",
    }
    ACTION_PUB_ROLES = {"feedback", "status"}
    cnt = 0
    for e_id, entity in entities.items():
        label = entity.A_v.get("label", "")
        if "/_action/" not in label:
            continue
        base, _, component = label.rpartition("/_action/")
        if not base or not component:
            continue
        if component in ACTION_SRV_ROLES:
            entity.A_v["action_name"] = base
            entity.A_v["action_role"] = ACTION_SRV_ROLES[component]
            cnt += 1
        for aspect in entity.A_v.get("aspects", []):
            topic = aspect.get("topic", "")
            if "/_action/" in topic:
                _, _, pub_comp = topic.rpartition("/_action/")
                if pub_comp in ACTION_PUB_ROLES:
                    aspect["action_name"] = base
                    aspect["action_role"] = pub_comp
        if component in ACTION_PUB_ROLES:
            entity.A_v["action_name"] = base
            entity.A_v["action_role"] = component
            cnt += 1
    if cnt:
        log(f"Actions detected: {cnt} component entities tagged")


def mark_phases(session_id, executors, entities, functions):
    """Tag every F vertex with phase ∈ {data, init, zero_exec, unknown}.

    Rationale: ROS/NITROS bringup fires many one-shot callbacks
    (TimeSource attach, ComponentManager parameter broadcast, Negotiated
    handshake, supported-types exchange, …) before the runtime data
    pipeline starts ticking. They form a big WCC of "boot noise" that
    obscures the real pipeline. We define:

      data_phase_start_ns[executor] :=
          min(first_fire) over the executor's non-one-shot timer cbs
          (n_fires ≥ 2; one-shot timers are typically NITROS
          startNitrosNode triggers, themselves part of init).

      session_data_start_ns :=
          min(data_phase_start_ns[ex]) across all executors that have
          any periodic timer.  Used as a fallback for executors that
          themselves carry no periodic timer (mirrors the gantt's
          session-level hyperperiod anchor).

      F.phase :=
          "data"      — ANY fire of this F's cb is ≥ data_phase_start
                       (its executor's, or the session fallback).
          "init"      — all fires precede that boundary.
          "zero_exec" — cb was registered but NEVER fired during the
                       trace (no callback_start observed). Typical:
                       /<node>/get_parameters, /<node>/set_parameters,
                       and other admin services that exist on every
                       ROS node but no one ever calls.
          "unknown"   — cb fired, but the session has no periodic
                       timer anywhere, so no anchor to compare to.
                       Rare; only matters for sessions without any
                       periodic timer at all.

    External (ptype="ext") boundary F vertices have no cb and are
    tagged "data" — they are connection terminals, never noise.
    """
    t0 = time.time()
    cb_starts = _all_events(session_id, "ros2:callback_start")
    if not cb_starts:
        log("mark_phases: no callback_start events, skipping")
        return

    # Per-cb: first + last start ts, fire count.
    cb_first_start: dict[str, int] = {}
    cb_last_start: dict[str, int] = {}
    cb_fire_count: dict[str, int] = {}
    for d in cb_starts:
        cb = d["payload"].get("callback")
        if not cb:
            continue
        ts = int(d["ts_ns"])
        cb_fire_count[cb] = cb_fire_count.get(cb, 0) + 1
        if cb not in cb_first_start or ts < cb_first_start[cb]:
            cb_first_start[cb] = ts
        if cb not in cb_last_start or ts > cb_last_start[cb]:
            cb_last_start[cb] = ts

    # entity_id → executor_id (via callback_group).
    cg_to_executor: dict[str, int] = {}
    for ex_id, ex in executors.items():
        for cg in ex.A_v.get("cb_groups", []):
            cg_id = cg.get("id")
            if cg_id:
                cg_to_executor[cg_id] = ex_id
    entity_to_executor: dict[int, int] = {}
    for e_id, e in entities.items():
        cg = e.A_v.get("callback_group")
        if cg and cg.get("id") in cg_to_executor:
            entity_to_executor[e_id] = cg_to_executor[cg["id"]]
    cb_to_executor: dict[str, int] = {}
    for e_id, e in entities.items():
        cb = e.A_v.get("cb_addr")
        if cb and cb != "NA":
            ex_id = entity_to_executor.get(e_id)
            if ex_id is not None:
                cb_to_executor[cb] = ex_id

    # Per-executor data_phase_start: min first_start of all periodic
    # (n_fires ≥ 2) timer cbs belonging to this executor's entities.
    exec_data_start: dict[int, int] = {}
    for e_id, e in entities.items():
        if e.A_v.get("etype") != "tmr":
            continue
        cb = e.A_v.get("cb_addr")
        if not cb or cb == "NA":
            continue
        if cb_fire_count.get(cb, 0) < 2:
            continue  # one-shot — part of init
        first = cb_first_start.get(cb)
        if first is None:
            continue
        ex_id = entity_to_executor.get(e_id)
        if ex_id is None:
            continue
        cur = exec_data_start.get(ex_id)
        if cur is None or first < cur:
            exec_data_start[ex_id] = first

    # Session-level fallback: earliest data_phase_start across all
    # executors that have one. Used for cbs on executors without a
    # periodic timer of their own — mirrors the gantt's session
    # hyperperiod anchor.
    session_data_start: int | None = (
        min(exec_data_start.values()) if exec_data_start else None
    )

    n_init = n_data = n_zero = n_unknown = 0
    n_fallback = 0
    for f_id, f in functions.items():
        # External (boundary) F vertices have ptype="ext" and no cb_addr.
        if f.A_v.get("ptype") == "ext":
            f.A_v["phase"] = "data"
            n_data += 1
            continue
        cb = f.A_v.get("cb_addr")
        if f.A_v.get("polled") and (not cb or cb == "NA" or cb not in cb_last_start):
            # polled subscription: its callback never fires by design, the data
            # is pulled by another callback (detect_polled_subs) → live.
            f.A_v["phase"] = "data"
            n_data += 1
            continue
        if not cb or cb == "NA" or cb not in cb_last_start:
            # cb registered but never fired.
            f.A_v["phase"] = "zero_exec"
            n_zero += 1
            continue
        ex_id = cb_to_executor.get(cb)
        data_start = exec_data_start.get(ex_id) if ex_id is not None else None
        if data_start is None:
            data_start = session_data_start
            if data_start is not None:
                n_fallback += 1
        if data_start is None:
            # cb did fire, but no periodic timer anywhere — no anchor.
            f.A_v["phase"] = "unknown"
            n_unknown += 1
            continue
        # "any fire ≥ data_start" ⇔ "last fire ≥ data_start"
        if cb_last_start[cb] >= data_start:
            f.A_v["phase"] = "data"
            n_data += 1
        else:
            f.A_v["phase"] = "init"
            n_init += 1
    log(f"mark_phases DONE in {time.time()-t0:.1f}s — "
        f"{n_data} data, {n_init} init, {n_zero} zero_exec, {n_unknown} unknown "
        f"({len(exec_data_start)}/{len(executors)} executors had a "
        f"periodic timer; {n_fallback} F's used session-level fallback)")


def split_callbacks(session_id, entities, functions):
    has_req = pg_store.fetch_one(
        "SELECT 1 FROM ros2_trace WHERE session_id = %s "
        "AND event = 'ros2:fish_rclcpp_client_request_sent' LIMIT 1",
        (session_id,),
    )
    if not has_req:
        log("split_callbacks: no fish_rclcpp_client_request_sent, skipping")
        return
    split_count = 0
    for e_id, entity in entities.items():
        cli_aspects = [a for a in entity.A_v.get("aspects", [])
                       if a.get("aspect") == "cli"]
        if not cli_aspects or not entity.Z_v:
            continue
        for f_id in list(entity.Z_v):
            func = functions.get(f_id)
            if not func:
                continue
            orig_label = func.A_v["label"]
            func.A_v["label"] = f"{orig_label}::part1"
            func.A_v["ptype"] = "cpu"
            func.A_v["split"] = "request"
            cont_A = {"label": f"{orig_label}::continuation",
                      "ptype": "cpu",
                      "cb_addr": func.A_v.get("cb_addr"),
                      "split": "response",
                      # Inherit phase from the original F (continuation is just
                      # the response half of the same logical callback).
                      "phase": func.A_v.get("phase", "unknown")}
            cont = FishVertex("F", next(vertex_counter), cont_A, [], 3)
            entity.Z_v.append(cont.id_v)
            functions[cont.id_v] = cont
            split_count += 1
    if split_count:
        log(f"split_callbacks: {split_count} callbacks split")


# ----------------------------------------------------------------------------
# Graph construction (verbatim from model_improved, modulo source-of-truth)
# ----------------------------------------------------------------------------

def create_graph(executors, nodes, entities, functions, session_name=""):
    G = nx.DiGraph()
    cn_id = next(vertex_counter)
    cn_A = {"label": session_name or "container"}
    cn = FishVertex("CN", cn_id, cn_A, list(executors.keys()), -1)
    G.add_node(cn.id_v, v=cn)
    for ex in executors.values():
        G.add_node(ex.id_v, v=ex)
    for n in nodes.values():
        G.add_node(n.id_v, v=n)
    for e in entities.values():
        G.add_node(e.id_v, v=e)
    for f in functions.values():
        G.add_node(f.id_v, v=f)
    for ex_id in cn.Z_v:
        G.add_edge(cn.id_v, ex_id, rel="contains", level="L-1_L0")
    for ex in executors.values():
        for n_id in ex.Z_v:
            G.add_edge(ex.id_v, n_id, rel="contains", level="L0_L1")
    for n in nodes.values():
        for e_id in n.Z_v:
            if e_id in entities:
                G.add_edge(n.id_v, e_id, rel="contains", level="L1_L2")
    for e in entities.values():
        for f_id in e.Z_v:
            if f_id in functions:
                G.add_edge(e.id_v, f_id, rel="contains", level="L2_L3")
    log(f"Graph vertical: {G.number_of_nodes()} vertices, {G.number_of_edges()} edges")
    return G


def add_horizontal_edges(G, session_id, executors, nodes, entities, functions):
    topic_meta = {}
    for r in pg_store.fetch_all(
        "SELECT topic, type, publisher_count, subscription_count "
        "FROM topic_info WHERE session_id = %s",
        (session_id,),
    ):
        topic_meta[r["topic"]] = {
            "msg_type": r["type"] or "",
            "pub_count": r["publisher_count"] or 0,
            "sub_count": r["subscription_count"] or 0,
        }
    for r in pg_store.fetch_all(
        "SELECT topic, average_rate FROM topic_hz WHERE session_id = %s",
        (session_id,),
    ):
        topic_meta.setdefault(r["topic"], {})["avg_rate_hz"] = r["average_rate"] or 0

    sub_index: dict[str, list[tuple]] = {}
    srv_index: dict[str, list[tuple]] = {}
    pub_index: dict[str, list[tuple]] = {}
    cli_index: dict[str, list[tuple]] = {}
    entity_to_node = {}
    for n in nodes.values():
        for e_id in n.Z_v:
            e = entities.get(e_id)
            if not e or e.t_v != "E":
                continue
            entity_to_node[e_id] = n.id_v
            for aspect in e.A_v.get("aspects", []):
                if aspect["aspect"] == "sub" and aspect.get("topic"):
                    sub_index.setdefault(aspect["topic"], []).append((e_id, n.id_v))
                elif aspect["aspect"] == "sub" and aspect.get("service"):
                    srv_index.setdefault(aspect["service"], []).append((e_id, n.id_v))
                elif aspect["aspect"] == "pub" and aspect.get("topic"):
                    pub_index.setdefault(aspect["topic"], []).append((e_id, n.id_v))
                elif aspect["aspect"] == "cli":
                    cli_index.setdefault(aspect["service"], []).append((e_id, n.id_v))

    l2_count = 0
    for topic, sub_list in sub_index.items():
        pub_entities = pub_index.get(topic, [])
        if pub_entities:
            meta = topic_meta.get(topic, {})
            for pub_e, pub_n in pub_entities:
                for sub_e, sub_n in sub_list:
                    # Do NOT skip same-node pub→sub: intra-process self-feedback
                    # (e.g., NegotiatedPublisher in-node compat path) is a valid
                    # graph edge. Tag it so consumers can filter.
                    intra_node = (pub_n == sub_n)
                    attrs = {"rel": "comm", "level": "L2", "nature": "msg", "topic": topic}
                    if intra_node:
                        attrs["intra_node"] = True
                    if meta.get("msg_type"):
                        attrs["msg_type"] = meta["msg_type"]
                    if meta.get("avg_rate_hz"):
                        attrs["avg_rate_hz"] = meta["avg_rate_hz"]
                    G.add_edge(pub_e, sub_e, **attrs)
                    l2_count += 1
    for service, srv_list in srv_index.items():
        cli_entities = cli_index.get(service, [])
        if cli_entities:
            for cli_e, cli_n in cli_entities:
                for srv_e, srv_n in srv_list:
                    intra_node = (cli_n == srv_n)
                    extra = {"intra_node": True} if intra_node else {}
                    G.add_edge(cli_e, srv_e, rel="comm", level="L2",
                               nature="dep", service=service, direction="request", **extra)
                    G.add_edge(srv_e, cli_e, rel="comm", level="L2",
                               nature="dep", service=service, direction="response", **extra)
                    l2_count += 2
    log(f"  L2 edges: {l2_count}")

    ext_count = 0
    all_pub_topics = set(pub_index.keys())
    all_sub_topics = set(sub_index.keys())
    for topic, sub_list in sub_index.items():
        if topic in all_pub_topics:
            continue
        ext_e_A = {"label": f"ext:{topic}", "etype": "sub", "external": True,
                   "aspects": [{"aspect": "pub", "topic": topic}]}
        ext_e = FishVertex("E", next(vertex_counter), ext_e_A, [], 2)
        entities[ext_e.id_v] = ext_e
        G.add_node(ext_e.id_v, v=ext_e)
        ext_f_A = {"label": f"ext:{topic}", "ptype": "ext", "phase": "data"}
        ext_f = FishVertex("F", next(vertex_counter), ext_f_A, [], 3)
        functions[ext_f.id_v] = ext_f
        ext_e.Z_v.append(ext_f.id_v)
        G.add_node(ext_f.id_v, v=ext_f)
        G.add_edge(ext_e.id_v, ext_f.id_v, rel="contains", level="V")
        for sub_e, _ in sub_list:
            meta = topic_meta.get(topic, {})
            attrs = {"rel": "comm", "level": "L2", "nature": "msg",
                     "topic": topic, "external": True}
            if meta.get("msg_type"):
                attrs["msg_type"] = meta["msg_type"]
            G.add_edge(ext_e.id_v, sub_e, **attrs)
            l2_count += 1
            ext_count += 1
    for topic, pub_list in pub_index.items():
        if topic in all_sub_topics:
            continue
        ext_e_A = {"label": f"ext:{topic}", "etype": "sub", "external": True,
                   "aspects": [{"aspect": "sub", "topic": topic}]}
        ext_e = FishVertex("E", next(vertex_counter), ext_e_A, [], 2)
        entities[ext_e.id_v] = ext_e
        G.add_node(ext_e.id_v, v=ext_e)
        ext_f_A = {"label": f"ext:{topic}", "ptype": "ext", "phase": "data"}
        ext_f = FishVertex("F", next(vertex_counter), ext_f_A, [], 3)
        functions[ext_f.id_v] = ext_f
        ext_e.Z_v.append(ext_f.id_v)
        G.add_node(ext_f.id_v, v=ext_f)
        G.add_edge(ext_e.id_v, ext_f.id_v, rel="contains", level="V")
        for pub_e, _ in pub_list:
            G.add_edge(pub_e, ext_e.id_v, rel="comm", level="L2",
                       nature="msg", topic=topic, external=True)
            l2_count += 1
            ext_count += 1
    if ext_count:
        log(f"  External boundary: {ext_count} edges")

    # L3 propagation — direction-aware, no longer cartesian.
    #
    # Background: every entity owns ONE main F (the original callback),
    # plus optionally a "continuation" F (created by split_callbacks for
    # entities with `cli` aspect — handles the async response). The old
    # logic took src × dst cartesian, which (1) sent topic publishes to
    # response continuations and (2) sent response edges to request F's.
    # Now we route:
    #   topic msg edge: main F → main F (skip continuations both sides)
    #   service request edge: cli main → srv main
    #   service response edge: srv main → cli continuation (only)
    #
    # Filters use the F's `split` attr set by split_callbacks:
    #   None or "request" → main / request F
    #   "response"        → continuation F
    def _funcs_main(entity_id):
        return [f for f in entities[entity_id].Z_v
                if f in functions
                and functions[f].A_v.get("split") != "response"]

    def _funcs_continuation(entity_id):
        return [f for f in entities[entity_id].Z_v
                if f in functions
                and functions[f].A_v.get("split") == "response"]

    l3_count = 0
    for u, v, data in list(G.edges(data=True)):
        if data.get("level") != "L2" or data.get("rel") != "comm":
            continue
        if u not in entities or v not in entities:
            continue
        nature = data.get("nature")
        direction = data.get("direction")
        if nature == "dep" and direction == "response":
            # Service response: srv main → cli continuation only
            src_funcs = _funcs_main(u)
            dst_funcs = _funcs_continuation(v)
            if not dst_funcs:
                # Fallback if no continuation was split out (no client_link
                # event captured): treat the cli's main F as the receiver too.
                dst_funcs = _funcs_main(v)
        else:
            # Topic msg, service request: main → main
            src_funcs = _funcs_main(u)
            dst_funcs = _funcs_main(v)
        for sf in src_funcs:
            for df in dst_funcs:
                G.add_edge(sf, df, rel="comm", level="L3",
                           **{k: vv for k, vv in data.items() if k not in ("rel", "level")})
                l3_count += 1
    log(f"  L3 propagated: {l3_count}")

    # Join membership ties (see detect_joins): undirected in meaning, stored
    # as one L3 edge per member pair (member → completer/first f), nature=
    # "join", NOT a message hop. Keeps a join's inputs in one FT.
    join_count = 0
    for n_id, node in nodes.items():
        for topic, f_list in (node.A_v.get("joins") or {}).items():
            fl = [f for f in f_list if f in functions]
            if len(fl) < 2:
                continue
            # tie every member to the first completer (or first member)
            anchor_f = next((f for f in fl if functions[f].A_v.get("join_role") == "completer"), fl[0])
            for f in fl:
                if f == anchor_f:
                    continue
                G.add_edge(f, anchor_f, rel="comm", level="L3", nature="join",
                           topic=topic, join_group=topic, intra_node=True)
                join_count += 1
    if join_count:
        log(f"  Join ties: {join_count} L3 edges (nature=join)")

    # State links (detect_state_links / detect_polled_subs): sample-and-hold reads
    # inside a node — NOT precedence; FT grouping ignores nature='state'.
    state_count = 0; polled_count = 0
    for n_id, node in nodes.items():
        polled_set = {(a, b, t): n for a, b, t, n in (node.A_v.get("polled_links") or [])}
        for f_s, f_p, topic in (node.A_v.get("state_links") or []):
            if f_s in functions and f_p in functions:
                n_poll = polled_set.get((f_s, f_p, topic))
                if n_poll:
                    # observed: rmw_take inside the reader's callback window
                    G.add_edge(f_s, f_p, rel="comm", level="L3", nature="state",
                               topic=topic, inferred=False, polled=True, n_polls=n_poll, intra_node=True)
                    polled_count += 1
                else:
                    G.add_edge(f_s, f_p, rel="comm", level="L3", nature="state",
                               topic=topic, inferred=True, intra_node=True)
                state_count += 1
    if state_count:
        log(f"  State links: {state_count} L3 edges (nature=state; {polled_count} polled/observed, rest inferred)")

    # ──────────────────────────────────────────────────────────────────
    # NITROS GXF intra-node edges (Option 1: static topology)
    #
    # Each NITROS Codelet chain ("GraphIOGroup") emits:
    #   fish_nitros_sub_link  per ingress (NitrosSubscriber → GXF Receiver)
    #   fish_nitros_pub_link  per egress  (NitrosPublisher  → GXF Transmitter)
    # All events sharing the same (node_handle, group_addr) belong to one
    # I/O group. Within a group, every ingress feeds every egress (because
    # the GXF Codelet chain links them internally). We therefore generate
    # cross-product L2+L3 edges: ingress sub entity → egress waitable entity.
    #
    # This closes the rclcpp pub/sub model's blind spot for NITROS data flow
    # (input received via NitrosSubscriber → GXF MessageRelay → Codelet.tick
    # → MessageRelay → NitrosPublisherWaitable.execute → output published).
    # No msg_id correlation; the static topology is sufficient for graph
    # extraction.
    # ──────────────────────────────────────────────────────────────────
    nitros_sub_links = _all_events(session_id, "ros2:fish_nitros_sub_link")
    nitros_pub_links = _all_events(session_id, "ros2:fish_nitros_pub_link")
    if nitros_sub_links or nitros_pub_links:
        log(f"  NITROS topology: {len(nitros_sub_links)} sub_link, "
            f"{len(nitros_pub_links)} pub_link events")

        # Locally rebuild lookup maps (kept minimal — these match the maps in
        # attribute_aspects but live in a different scope here).
        _nh_to_node = {n.A_v["node_handle"]: n.id_v for n in nodes.values()}
        _node_to_entities: dict = {}
        for n_id, n in nodes.items():
            for e_id in n.Z_v:
                if e_id in entities:
                    _node_to_entities.setdefault(n_id, []).append(e_id)

        # sub_handle → owning sub entity id
        sub_handle_to_entity_id: dict[str, int] = {}
        for d in _all_events(session_id, "ros2:rcl_subscription_init"):
            p = d["payload"]
            sh = p.get("subscription_handle")
            nh = p.get("node_handle")
            topic = p.get("topic_name")
            if not (sh and nh and topic):
                continue
            n_id = _nh_to_node.get(nh)
            if n_id is None:
                continue
            for e_id in _node_to_entities.get(n_id, []):
                ent = entities[e_id]
                if ent.A_v.get("etype") == "sub" and ent.A_v.get("label") == topic:
                    sub_handle_to_entity_id[sh] = e_id
                    break

        # waitable_handle → owning waitable entity id (1:1, since cb_addr ==
        # waitable_handle for waitable entities — see identify_entities).
        waitable_handle_to_entity_id: dict[str, int] = {}
        for e_id, ent in entities.items():
            if ent.A_v.get("etype") == "waitable":
                wh = ent.A_v.get("waitable_handle")
                if wh:
                    waitable_handle_to_entity_id[wh] = e_id

        # group_addr → {ingress: [(sub_handle, entity_id)],
        #               egress:  [(pub_handle, waitable_handle, entity_id)]}
        groups: dict[tuple, dict] = {}
        for d in nitros_sub_links:
            p = d["payload"]
            key = (p.get("node_handle"), p.get("group_addr"))
            ent_id = sub_handle_to_entity_id.get(p.get("subscription_handle"))
            if ent_id is None:
                continue
            groups.setdefault(key, {"ingress": [], "egress": []})["ingress"].append(
                (p["subscription_handle"], ent_id))
        for d in nitros_pub_links:
            p = d["payload"]
            key = (p.get("node_handle"), p.get("group_addr"))
            ent_id = waitable_handle_to_entity_id.get(p.get("waitable_handle"))
            if ent_id is None:
                continue
            groups.setdefault(key, {"ingress": [], "egress": []})["egress"].append(
                (p["publisher_handle"], p["waitable_handle"], ent_id))

        nitros_l3_count = 0
        nitros_groups_with_edges = 0
        for (nh, ga), g in groups.items():
            if not g["ingress"] or not g["egress"]:
                continue
            nitros_groups_with_edges += 1
            for (_sh, src_eid) in g["ingress"]:
                for (_ph, _wh, dst_eid) in g["egress"]:
                    # E-level edge (L2-equivalent, nature="gxf_internal")
                    G.add_edge(src_eid, dst_eid,
                               rel="comm", level="L2",
                               nature="gxf_internal", intra_node=True,
                               nitros_group=str(ga))
                    # F-level propagation: main F → main F
                    for sf in _funcs_main(src_eid):
                        for df in _funcs_main(dst_eid):
                            G.add_edge(sf, df,
                                       rel="comm", level="L3",
                                       nature="gxf_internal",
                                       intra_node=True,
                                       nitros_group=str(ga))
                            nitros_l3_count += 1
        log(f"  NITROS intra-node: {nitros_groups_with_edges} groups, "
            f"{nitros_l3_count} L3 edges added")

    # L1 aggregation
    node_pairs = {}
    for u, v, data in G.edges(data=True):
        if data.get("level") != "L2" or data.get("rel") != "comm":
            continue
        src_n = entity_to_node.get(u, u if u in nodes else None)
        dst_n = entity_to_node.get(v, v if v in nodes else None)
        if src_n and dst_n and src_n != dst_n:
            node_pairs.setdefault((src_n, dst_n), []).append(data)
    l1_count = 0
    for (sn, dn), edges in node_pairs.items():
        topics = [e.get("topic", e.get("service", "?")) for e in edges]
        G.add_edge(sn, dn, rel="comm", level="L1",
                   comm_count=len(edges), topics=topics)
        l1_count += 1
    log(f"  L1 aggregated: {l1_count}")

    # L0 aggregation
    node_to_exec = {}
    for ex in executors.values():
        for n_id in ex.Z_v:
            node_to_exec[n_id] = ex.id_v
    exec_pairs = {}
    for u, v, data in G.edges(data=True):
        if data.get("level") != "L1":
            continue
        se = node_to_exec.get(u)
        de = node_to_exec.get(v)
        if se and de and se != de:
            exec_pairs.setdefault((se, de), []).append(data)
    l0_count = 0
    for (se, de), edges in exec_pairs.items():
        total = sum(e.get("comm_count", 1) for e in edges)
        G.add_edge(se, de, rel="comm", level="L0",
                   tau="InterP", comm_count=total)
        l0_count += 1
    log(f"  L0 aggregated: {l0_count}")
    log(f"  Total horizontal: {l2_count + l3_count + l1_count + l0_count}")
    return G


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------

def extract(session_id: str, *, scope: str = graph_store_pg.STANDALONE_SCOPE,
            session_name: str | None = None,
            no_split: bool = False,
            save: bool = True,
            out_path: str | None = None,
            source_trace: str | None = None) -> nx.DiGraph:
    """Run the full extraction pipeline on a PG-resident session.

    Returns the nx.DiGraph; also (by default) persists to PG via
    graph_store_pg.save_graph and exports JSON if out_path is set.
    """
    global vertex_counter
    vertex_counter = count(1)

    executors, nodes = identify_executors(session_id)
    entities = identify_entities(session_id, nodes)
    functions = identify_callbacks(session_id, nodes, entities, executors)
    attribute_aspects(session_id, executors, nodes, entities)
    detect_joins(nodes, entities, functions)
    detect_state_links(session_id, nodes, entities, functions)
    detect_polled_subs(session_id, nodes, entities, functions)
    attach_callback_groups(session_id, executors, nodes, entities, functions)
    mark_phases(session_id, executors, entities, functions)
    detect_oort_threads(session_id, executors, nodes, entities, functions)
    detect_actions(entities)
    if not no_split:
        split_callbacks(session_id, entities, functions)

    cn_label = session_name or session_id
    G = create_graph(executors, nodes, entities, functions, session_name=cn_label)
    G = add_horizontal_edges(G, session_id, executors, nodes, entities, functions)
    measure_flows(G, session_id, functions, nodes, entities)

    if save:
        meta = graph_store_pg.save_graph(
            G, session_id, scope,
            source_trace=source_trace,
            container_role=None,
            is_composed=False,
            actor="model_improved_pg",
        )
        log(f"Saved to graph_store_pg: ({session_id}, {scope}) → "
            f"{meta['stats']['num_nodes']} nodes, {meta['stats']['num_edges']} edges")

    if out_path:
        graph_store_pg.export_json(session_id, scope, out_path)
        log(f"JSON: {out_path}")

    return G


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="session_id (PG)")
    ap.add_argument("--scope", default=graph_store_pg.STANDALONE_SCOPE)
    ap.add_argument("--out", default=None, help="Output fish_graph.json path")
    ap.add_argument("--no-split", action="store_true")
    ap.add_argument("--no-save", action="store_true",
                    help="Skip persisting to graph_store_pg")
    ap.add_argument("--source-trace", default=None)
    ap.add_argument("--session-name", default=None,
                    help="CN label (defaults to session_id)")
    args = ap.parse_args()

    pg_store.init_pool()
    extract(
        args.session, scope=args.scope,
        session_name=args.session_name,
        no_split=args.no_split, save=not args.no_save,
        out_path=args.out, source_trace=args.source_trace,
    )


if __name__ == "__main__":
    main()
