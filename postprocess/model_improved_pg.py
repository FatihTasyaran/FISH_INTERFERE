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

    log(f"Entities: {len(entities)} "
        f"(sub={sum(1 for e in entities.values() if e.A_v['etype']=='sub')}, "
        f"serv={sum(1 for e in entities.values() if e.A_v['etype']=='serv')}, "
        f"tmr={sum(1 for e in entities.values() if e.A_v['etype']=='tmr')})")
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
    # NITROS-negotiated wrapper). The "active" callback in the trace — the one that
    # actually publishes — is the FIRST one registered. The original MongoDB pipeline
    # got this for free because find()'s natural-order iteration happened to expose
    # it last, so dict-comp's last-wins kept it. PG returns rows strictly by (ts_ns,
    # id) — so we explicitly keep-first for these handle→callback chains to match.
    def _keep_first(docs, key_field, val_field):
        out = {}
        for d in docs:
            k = d["payload"][key_field]
            if k not in out:
                out[k] = d["payload"][val_field]
        return out

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
            sub_obj = rclcpp_sub_by_handle.get(sub_handle)
            if sub_obj:
                cb_ptr = rclcpp_sub_cb_by_sub.get(sub_obj)
                if cb_ptr:
                    info = _resolve_cb(cb_ptr)
                    if info:
                        sub_chain[topic] = info
                        continue
            cb_ptr = rclpy_sub_cb_by_handle.get(sub_handle)
            if cb_ptr:
                info = _resolve_cb(cb_ptr)
                if info:
                    sub_chain[topic] = info

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

            if cb_info:
                entity.A_v["cb_addr"] = cb_info["callback"]
                f_A = {"label": cb_info["symbol"], "ptype": "cpu",
                       "cb_addr": cb_info["callback"]}
                if is_gpu:
                    f_A["gpu_node"] = True
                f = FishVertex("F", next(vertex_counter), f_A, [], 3)
                entity.Z_v.append(f.id_v)
                functions[f.id_v] = f

    log(f"identify_callbacks DONE in {time.time()-t0:.1f}s — {len(functions)} functions")
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
    for e_id, e in entities.items():
        cb = e.A_v.get("cb_addr")
        if cb and cb != "NA":
            cb_to_entity[cb] = (e_id, e)

    publish_links = _all_events(session_id, "ros2:fish_rclcpp_publish_link")
    client_links = _all_events(session_id, "ros2:fish_rclcpp_client_link")

    if publish_links or client_links:
        log(f"attribute_aspects: tracepoint method "
            f"({len(publish_links)} pub links, {len(client_links)} cli links)")

        pub_handle_to_topic = {
            d["payload"]["publisher_handle"]: d["payload"]["topic_name"]
            for d in _all_events(session_id, "ros2:rcl_publisher_init")
        }
        cli_handle_to_service = {
            d["payload"]["client_handle"]: d["payload"]["service_name"]
            for d in _all_events(session_id, "ros2:rcl_client_init")
        }
        seen_pub, seen_cli = set(), set()
        total_pub = total_cli = 0
        for d in publish_links:
            p = d["payload"]
            topic = pub_handle_to_topic.get(p["publisher_handle"])
            if not topic:
                continue
            if SKIP_PARAMSERVICE and _is_param_service(topic):
                continue
            pair = cb_to_entity.get(p["callback"])
            if not pair:
                continue
            e_id, entity = pair
            if (e_id, topic) in seen_pub:
                continue
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
        log(f"  Tracepoint attribution: {total_pub} pub, {total_cli} cli")
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
                      "split": "response"}
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
                    if pub_n == sub_n:
                        continue
                    attrs = {"rel": "comm", "level": "L2", "nature": "msg", "topic": topic}
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
                    if cli_n == srv_n:
                        continue
                    G.add_edge(cli_e, srv_e, rel="comm", level="L2",
                               nature="dep", service=service, direction="request")
                    G.add_edge(srv_e, cli_e, rel="comm", level="L2",
                               nature="dep", service=service, direction="response")
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
        ext_f_A = {"label": f"ext:{topic}", "ptype": "ext"}
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
        ext_f_A = {"label": f"ext:{topic}", "ptype": "ext"}
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

    # L3 propagation
    l3_count = 0
    for u, v, data in list(G.edges(data=True)):
        if data.get("level") != "L2" or data.get("rel") != "comm":
            continue
        if u not in entities or v not in entities:
            continue
        src_funcs = [f for f in entities[u].Z_v if f in functions]
        dst_funcs = [f for f in entities[v].Z_v if f in functions]
        for sf in src_funcs:
            for df in dst_funcs:
                G.add_edge(sf, df, rel="comm", level="L3",
                           **{k: vv for k, vv in data.items() if k not in ("rel", "level")})
                l3_count += 1
    log(f"  L3 propagated: {l3_count}")

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
    attach_callback_groups(session_id, executors, nodes, entities, functions)
    detect_oort_threads(session_id, executors, nodes, entities, functions)
    detect_actions(entities)
    if not no_split:
        split_callbacks(session_id, entities, functions)

    cn_label = session_name or session_id
    G = create_graph(executors, nodes, entities, functions, session_name=cn_label)
    G = add_horizontal_edges(G, session_id, executors, nodes, entities, functions)

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
