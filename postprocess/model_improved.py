"""
FISH Model Extraction — Improved
=================================

Entity types: sub, tmr, serv (callback-bearing, 1:1 with function vertices)
Aspects: pub, cli (communication patterns attributed to entities)
Actions: detected post-hoc, component entities tagged with action_name/role
Split callbacks: cli aspect entities get continuation function vertices

See notes/action_resolve_improved.txt and notes/callback_association.txt
for full design rationale.
"""
import os
import time
from itertools import count
from dataclasses import dataclass, field
from typing import Any, Optional

import networkx as nx
from pymongo import MongoClient
from influxdb_client_3 import InfluxDBClient3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MONGO_URI = "mongodb://localhost:27017"
INFLUX_HOST = "http://127.0.0.1:8181"
INFLUX_TOKEN_FILE = os.path.expanduser("~/inf.tok")

SKIP_DEBUG = True
SKIP_RVIZ = True
SKIP_PARAMSERVICE = True
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


# ---------------------------------------------------------------------------
# Dataclasses (preserved from model.py)
# ---------------------------------------------------------------------------

vertex_counter = count(1)

@dataclass
class FishVertex:
    t_v: str        # "EX" | "N" | "E" | "F"
    id_v: int
    A_v: dict = field(default_factory=dict)
    Z_v: list = field(default_factory=list)
    level: int = 0

@dataclass
class FishEdge:
    src: int
    dst: int
    tau: str        # "InterP" | "IntraP"
    msgs: list = field(default_factory=list)

@dataclass
class FishMessage:
    id: str
    name: str
    data_type: str
    src_exec: str
    src_node: str
    src_entity: str
    dst_exec: str
    dst_node: str
    dst_entity: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[FISH model] {msg}", flush=True)


def load_influx_token():
    with open(INFLUX_TOKEN_FILE) as f:
        for line in f:
            if line.startswith("Token:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError(f"Could not read token from {INFLUX_TOKEN_FILE}")


def connect_mongo(db_name, role=None):
    """Connect to a session's MongoDB database, optionally role-scoped.

    Returns a ScopedMongo so existing callers like `mongo["ros2_trace"]`
    transparently resolve to `ros2_trace_<role>` when a role is set.
    """
    from db_scope import ScopedMongo
    client = MongoClient(MONGO_URI)
    db = client[db_name]
    tag = f"{db_name}" + (f"  (role={role})" if role else "")
    log(f"MongoDB connected → {tag}")
    return ScopedMongo(db, role=role)


def connect_influx(db_name=None, role=None):
    """Connect to the shared `fish` InfluxDB3 database.

    All FISH nsys/GPU data lives in a single InfluxDB database; session
    and container are represented as tags on each point. Parameters are
    accepted (and ignored) for API symmetry with connect_mongo — callers
    that need to filter by session/container do so in the SELECT.
    """
    token = load_influx_token()
    client = InfluxDBClient3(host=INFLUX_HOST, token=token, database="fish")
    log(f"InfluxDB3 connected → fish"
        + (f"  (session={db_name}, container={role})" if db_name or role else ""))
    return client


# ---------------------------------------------------------------------------
# Layer 0: Executors
# ---------------------------------------------------------------------------

def identify_executors(mongo):
    """Identify executor processes from rcl_node_init events.

    Each unique vpid with at least one rcl_node_init is an executor.
    Killed PIDs (from FISH kill/relaunch) are excluded.
    """
    from utils import confirmed_kills

    coll = mongo["ros2_trace"]
    executor_pids = coll.distinct("vpid", {"event": "ros2:rcl_node_init"})
    killed_pids = confirmed_kills(mongo)

    executors = {}
    nodes = {}

    for pid in executor_pids:
        if pid in killed_pids:
            continue

        node_inits = list(coll.find({"event": "ros2:rcl_node_init", "vpid": pid}))

        # Skip ros2cli tooling
        if SKIP_DEBUG:
            all_debug = all(n['payload']['node_name'].startswith("_ros2cli")
                           for n in node_inits)
            if all_debug:
                if INCLUDE_EXTERNAL_ACTIONS:
                    has_action = any("send_goal" in n['payload']['node_name']
                                    for n in node_inits)
                    if not has_action:
                        continue
                else:
                    continue

        # Skip rviz
        if SKIP_RVIZ and any(n['procname'] == "rviz2" for n in node_inits):
            continue

        # Create executor vertex
        ex_A = {"label": "temp", "pid": pid, "mode": "NA"}
        ex = FishVertex("EX", next(vertex_counter), ex_A, [], 0)

        for nie in node_inits:
            ns = nie['payload'].get('namespace', '/')
            full_name = ns.rstrip('/') + '/' + nie['payload']['node_name']

            n_A = {
                "label": nie['payload']['node_name'],
                "full_name": full_name,
                "node_handle": nie['payload']['node_handle'],
                "publishers": {},
            }
            if ex.A_v['label'] == "temp":
                ex.A_v['label'] = f"{nie['procname']} (PID:{pid})"

            n = FishVertex("N", next(vertex_counter), n_A, [], 1)
            ex.Z_v.append(n.id_v)
            nodes[n.id_v] = n

        executors[ex.id_v] = ex

    log(f"Executors: {len(executors)}, Nodes: {len(nodes)}")
    return executors, nodes


# ---------------------------------------------------------------------------
# Layer 2: Entities (sub, tmr, serv ONLY)
# ---------------------------------------------------------------------------

def identify_entities(mongo, nodes):
    """Create entities from init events. ONLY sub, tmr, serv.

    Publishers and clients are NOT entity types — they become aspects
    via attribute_aspects().
    """
    from utils import entity_fallback_from_trace, verify_actions

    entities = {}

    for node_id, node in nodes.items():
        full_name = node.A_v['full_name']
        node_handle = node.A_v['node_handle']

        # Get entity info (node_info first, trace fallback for timers)
        fallback = entity_fallback_from_trace(mongo, node_handle)
        info = mongo["node_info"].find_one({"node": full_name})
        if info is None:
            info = fallback
        info["Timers"] = fallback["Timers"]

        # Store publisher info on node (for later aspect attribution)
        pubs = info.get("Publishers", {})
        if SKIP_PARAMSERVICE:
            pubs = {t: m for t, m in pubs.items() if not _is_param_service(t)}
        node.A_v["publishers"] = pubs

        # Store client info on node (for later aspect attribution)
        clients = info.get("Service_Clients", {})
        if SKIP_PARAMSERVICE:
            clients = {s: t for s, t in clients.items() if not _is_param_service(s)}
        node.A_v["service_clients"] = clients

        # --- Subscriptions → sub entities ---
        for topic, msg_type in info.get("Subscribers", {}).items():
            if SKIP_PARAMSERVICE and _is_param_service(topic):
                continue
            e_A = {
                "label": topic,
                "etype": "sub",
                "cb_addr": "NA",
                "msg_type": msg_type,
                "aspects": [{"aspect": "sub", "topic": topic, "msg_type": msg_type}],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

        # --- Service servers → serv entities ---
        for srv_name, srv_type in info.get("Service_Servers", {}).items():
            if SKIP_PARAMSERVICE and _is_param_service(srv_name):
                continue
            e_A = {
                "label": srv_name,
                "etype": "serv",
                "cb_addr": "NA",
                "srv_type": srv_type,
                "aspects": [
                    {"aspect": "sub", "service": srv_name},
                    {"aspect": "pub", "service": srv_name},
                ],
            }
            e = FishVertex("E", next(vertex_counter), e_A, [], 2)
            node.Z_v.append(e.id_v)
            entities[e.id_v] = e

        # --- Timers → tmr entities ---
        for timer_handle, period_ns in info.get("Timers", {}).items():
            e_A = {
                "label": f"timer_{period_ns}ns",
                "etype": "tmr",
                "cb_addr": "NA",
                "timer_handle": timer_handle,
                "period_ns": period_ns,
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


# ---------------------------------------------------------------------------
# Layer 3: Callbacks (bulk load)
# ---------------------------------------------------------------------------

def identify_callbacks(mongo, nodes, entities, executors):
    """Resolve callback chains for each entity. Bulk load approach.

    Loads all init events once (~13K docs), builds in-memory indexes,
    resolves chains via O(1) dict lookups.
    """
    from utils import nsys_profiled_pids

    t0 = time.time()
    functions = {}
    coll = mongo["ros2_trace"]
    log(f"identify_callbacks START (bulk) — {len(entities)} entities")

    # --- Bulk load all init events ---
    def _bulk(event_name):
        docs = list(coll.find({"event": event_name}))
        return docs

    rcl_sub_init = _bulk("ros2:rcl_subscription_init")
    rclcpp_sub_init = _bulk("ros2:rclcpp_subscription_init")
    rclcpp_sub_cb_added = _bulk("ros2:rclcpp_subscription_callback_added")
    rclcpp_cb_register = _bulk("ros2:rclcpp_callback_register")
    rcl_srv_init = _bulk("ros2:rcl_service_init")
    rclcpp_srv_cb_added = _bulk("ros2:rclcpp_service_callback_added")
    rclcpp_tmr_cb_added = _bulk("ros2:rclcpp_timer_callback_added")
    rclpy_sub_cb_added = _bulk("ros2:rclpy_subscription_callback_added")
    rclpy_srv_cb_added = _bulk("ros2:rclpy_service_callback_added")
    rclpy_tmr_cb_added = _bulk("ros2:rclpy_timer_callback_added")
    rclpy_cb_register = _bulk("ros2:rclpy_callback_register")

    log(f"  Bulk load: {time.time()-t0:.1f}s")

    # --- Build in-memory indexes ---
    # node_handle → [(sub_handle, topic)]
    sub_init_by_node = {}
    for d in rcl_sub_init:
        p = d["payload"]
        sub_init_by_node.setdefault(p["node_handle"], []).append(
            (p["subscription_handle"], p["topic_name"]))

    # subscription_handle → subscription object
    rclcpp_sub_by_handle = {d["payload"]["subscription_handle"]: d["payload"]["subscription"]
                            for d in rclcpp_sub_init}

    # subscription object → callback
    rclcpp_sub_cb_by_sub = {d["payload"]["subscription"]: d["payload"]["callback"]
                            for d in rclcpp_sub_cb_added}

    # callback → symbol (rclcpp + rclpy)
    symbol_by_cb = {}
    for d in rclcpp_cb_register:
        symbol_by_cb[d["payload"]["callback"]] = d["payload"]["symbol"]
    for d in rclpy_cb_register:
        symbol_by_cb[d["payload"]["callback"]] = d["payload"]["symbol"]

    # node_handle → [(srv_handle, srv_name)]
    srv_init_by_node = {}
    for d in rcl_srv_init:
        p = d["payload"]
        srv_init_by_node.setdefault(p["node_handle"], []).append(
            (p["service_handle"], p["service_name"]))

    # service_handle → callback (rclcpp)
    rclcpp_srv_cb_by_handle = {d["payload"]["service_handle"]: d["payload"]["callback"]
                               for d in rclcpp_srv_cb_added}

    # timer_handle → callback (rclcpp)
    rclcpp_tmr_cb_by_handle = {d["payload"]["timer_handle"]: d["payload"]["callback"]
                               for d in rclcpp_tmr_cb_added}

    # rclpy fallbacks
    rclpy_sub_cb_by_handle = {d["payload"]["subscription_handle"]: d["payload"]["callback"]
                              for d in rclpy_sub_cb_added}
    rclpy_srv_cb_by_handle = {d["payload"]["service_handle"]: d["payload"]["callback"]
                              for d in rclpy_srv_cb_added}
    rclpy_tmr_cb_by_handle = {d["payload"]["timer_handle"]: d["payload"]["callback"]
                              for d in rclpy_tmr_cb_added}

    # --- Resolve per-node ---
    gpu_pids = nsys_profiled_pids(mongo)
    node_to_pid = {}
    for ex in executors.values():
        for n_id in ex.Z_v:
            node_to_pid[n_id] = ex.A_v['pid']

    def _resolve_cb(cb_ptr):
        sym = symbol_by_cb.get(cb_ptr)
        if sym:
            return {"callback": cb_ptr, "symbol": sym}
        return None

    for node_id, node in nodes.items():
        is_gpu = node_to_pid.get(node_id) in gpu_pids
        node_handle = node.A_v['node_handle']

        # Build subscription chain: topic → {callback, symbol}
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
            # rclpy fallback
            cb_ptr = rclpy_sub_cb_by_handle.get(sub_handle)
            if cb_ptr:
                info = _resolve_cb(cb_ptr)
                if info:
                    sub_chain[topic] = info

        # Build service chain: srv_name → {callback, symbol}
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

        # Match entities to callbacks
        for e_id in node.Z_v:
            entity = entities.get(e_id)
            if not entity or entity.t_v != "E":
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
                f_A = {
                    "label": cb_info["symbol"],
                    "ptype": "cpu",
                    "cb_addr": cb_info["callback"],
                }
                if is_gpu:
                    f_A["gpu_node"] = True
                f = FishVertex("F", next(vertex_counter), f_A, [], 3)
                entity.Z_v.append(f.id_v)
                functions[f.id_v] = f

    log(f"identify_callbacks DONE in {time.time()-t0:.1f}s — {len(functions)} functions")
    return functions


# ---------------------------------------------------------------------------
# Scheduler introspection: executor type + callback groups
# ---------------------------------------------------------------------------

def attach_callback_groups(mongo, executors, nodes, entities, functions):
    """Attach executor_type / num_threads / cb_groups on EX vertices, and
    callback_group info on E (and mirrored onto F).

    Reads four new FISH tracepoints:
      fish_executor_init         — (pid → executor type + num_threads)
      fish_callback_group_init   — (group_addr → type + automatic_add)
      fish_cbgroup_add           — (entity_rcl_handle → group_addr)
      fish_executor_add_cbgroup  — (executor_addr → group_addrs)
    """
    coll = mongo["ros2_trace"]
    t0 = time.time()

    # --- Bulk load new tracepoint events --------------------------------
    exec_init_docs = list(coll.find({"event": "ros2:fish_executor_init"}))
    cg_init_docs   = list(coll.find({"event": "ros2:fish_callback_group_init"}))
    cg_add_docs    = list(coll.find({"event": "ros2:fish_cbgroup_add"}))
    ex_add_docs    = list(coll.find({"event": "ros2:fish_executor_add_cbgroup"}))

    if not (exec_init_docs or cg_init_docs or cg_add_docs or ex_add_docs):
        log("attach_callback_groups: no scheduler events in trace, skipping")
        return

    log(f"attach_callback_groups: "
        f"{len(exec_init_docs)} exec_init, {len(cg_init_docs)} cg_init, "
        f"{len(cg_add_docs)} cg_add, {len(ex_add_docs)} ex_add")

    # --- fish_callback_group_init: group_addr → CG metadata -------------
    cg_info = {}
    for d in cg_init_docs:
        p = d["payload"]
        cg_info[p["group_addr"]] = {
            "type": p["group_type"],
            "automatic_add": bool(p["automatic_add"]),
            "pid": d["vpid"],
        }

    # --- fish_cbgroup_add: entity_rcl_handle → group_addr ---------------
    entity_to_group = {}
    for d in cg_add_docs:
        p = d["payload"]
        entity_to_group[p["entity_addr"]] = {
            "group_addr": p["group_addr"],
            "kind": p["entity_kind"],
        }

    # --- fish_executor_init: pid → [executor info] ----------------------
    executors_per_pid = {}
    for d in exec_init_docs:
        p = d["payload"]
        pid = d["vpid"]
        executors_per_pid.setdefault(pid, []).append({
            "executor_addr": p["executor_addr"],
            "type": p["executor_type"],
            "num_threads": p["num_threads"],
        })

    # --- fish_executor_add_cbgroup: executor_addr → set(group_addrs) ----
    exec_to_groups = {}
    for d in ex_add_docs:
        p = d["payload"]
        exec_to_groups.setdefault(p["executor_addr"], set()).add(p["group_addr"])

    # --- Handle → (entity_kind, node_handle, name) for sub/serv/cli ----
    # This bridge lets us match entity_addr (rcl handle) in fish_cbgroup_add
    # to the right E vertex even if we don't already store the handle on E.
    handle_to_topic_node = {}   # rcl handle → (topic_or_name, node_handle, kind)
    for d in coll.find({"event": "ros2:rcl_subscription_init"}):
        p = d["payload"]
        handle_to_topic_node[p["subscription_handle"]] = (
            p["topic_name"], p["node_handle"], "sub")
    for d in coll.find({"event": "ros2:rcl_service_init"}):
        p = d["payload"]
        handle_to_topic_node[p["service_handle"]] = (
            p["service_name"], p["node_handle"], "serv")

    # timer_handle is already stored on tmr E vertices (A_v['timer_handle'])

    # --- Attach to EX vertices -----------------------------------------
    for ex_id, ex in executors.items():
        pid = ex.A_v.get("pid")
        if pid is None:
            continue
        execs = executors_per_pid.get(pid, [])
        if not execs:
            continue
        # Primary = executor with most cb_groups in this process (usually only 1)
        execs_ranked = sorted(
            execs,
            key=lambda e: len(exec_to_groups.get(e["executor_addr"], [])),
            reverse=True,
        )
        primary = execs_ranked[0]
        # All addresses come from the trace as hex strings already
        ex.A_v["executor_addr"] = primary["executor_addr"]
        ex.A_v["executor_type"] = primary["type"]
        ex.A_v["num_threads"] = primary["num_threads"]

        # Aggregate CG info for all executors in process (usually same set)
        seen = set()
        cb_groups_list = []
        for e in execs:
            for g_addr in exec_to_groups.get(e["executor_addr"], []):
                if g_addr in seen:
                    continue
                seen.add(g_addr)
                ci = cg_info.get(g_addr, {})
                cb_groups_list.append({
                    "id": g_addr,
                    "type": ci.get("type", "Unknown"),
                    "automatic_add": ci.get("automatic_add"),
                })
        if cb_groups_list:
            ex.A_v["cb_groups"] = cb_groups_list

        if len(execs) > 1:
            ex.A_v["extra_executors"] = [
                {"addr": e["executor_addr"],
                 "type": e["type"],
                 "num_threads": e["num_threads"]}
                for e in execs_ranked[1:]
            ]

    # --- Attach to E / F vertices --------------------------------------
    # node_handle → node_id lookup
    node_by_handle = {n.A_v["node_handle"]: nid for nid, n in nodes.items()}

    cg_on_e = 0
    for e_id, e in entities.items():
        etype = e.A_v.get("etype")
        rcl_handle = None
        if etype == "tmr":
            rcl_handle = e.A_v.get("timer_handle")
        elif etype in ("sub", "serv"):
            # Find the rcl_handle via (node_handle, name) lookup
            # We need to walk handle_to_topic_node and match.
            label = e.A_v.get("label")
            # Parent node is the E's parent
            parent_nh = None
            # Find parent node by searching which node has e_id in Z_v
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
            "id": mapping["group_addr"],
            "type": ci["type"],
            "automatic_add": ci["automatic_add"],
        }
        e.A_v["callback_group"] = cg_attr
        cg_on_e += 1

        # Mirror onto children F vertices
        for f_id in e.Z_v:
            f = functions.get(f_id)
            if f:
                f.A_v["callback_group"] = cg_attr

    log(f"attach_callback_groups DONE in {time.time()-t0:.1f}s — "
        f"{cg_on_e}/{len(entities)} entities got CG info")


# ---------------------------------------------------------------------------
# Out-of-ROS threads (oort)
# ---------------------------------------------------------------------------

def detect_oort_threads(mongo, executors, nodes, entities, functions, *,
                        session=None, container=None):
    """Detect non-executor threads that do GPU work and anchor them in
    the graph as (oore entity → oort function) vertices.

    Pattern (see notes/EXPECTED_OBSERVED.txt §8):
      Some ROS 2 processes run their heavy compute in a plain Python/C++
      thread that is NOT dispatched by any rclpy/rclcpp executor (yolo_py
      is the canonical example — main thread runs an infinite inference
      loop using ONNX Runtime + CUDA). The rclpy/rclcpp executor in the
      same process handles only housekeeping callbacks (e.g. /clock).

      Such threads fall outside the F layer because no callback_register
      / callback_start fires for them. To keep FISH's hierarchy intact
      (every layer N's children live at N+1), we introduce:

        etype "oore" (E layer) — placeholder entity, one per oort thread
        ptype "oort" (F layer) — the thread itself, GPU-capable, gpu_node=True

      `gpu_dags` attribute (later) attaches the Typed DAG(s) to the oort F.

    Detection:
      For each GPU-classified process (nsys_profiled_pids), query
      InfluxDB cuda_runtime for distinct `tid` doing CUDA API calls in
      this (session, container). Any such tid that does NOT appear as
      a `callback_start.vtid` in ros2_trace is an oort thread.
    """
    from utils import nsys_profiled_pids

    gpu_pids = nsys_profiled_pids(mongo)
    if not gpu_pids:
        log("detect_oort_threads: no GPU processes, skipping")
        return

    coll = mongo["ros2_trace"]

    # Per-pid set of vtids that fire callback_start (executor threads)
    cb_tids_by_pid = {}
    for d in coll.find({"event": "ros2:callback_start"}, {"vpid": 1, "vtid": 1}):
        cb_tids_by_pid.setdefault(d["vpid"], set()).add(d["vtid"])

    # CUDA-active tids from InfluxDB (pyarrow.Table response → to_pylist)
    try:
        if session and container:
            influx = connect_influx(session, role=container)
        else:
            influx = connect_influx()
        q = (f"SELECT DISTINCT tid FROM cuda_runtime "
             f"WHERE session='{session or ''}' AND container='{container or ''}'")
        table = influx.query(q)
        cuda_tids = {int(row["tid"]) for row in table.to_pylist()
                     if row.get("tid") is not None}
        influx.close()
    except Exception as e:
        log(f"detect_oort_threads: InfluxDB query failed ({e}), skipping")
        return

    if not cuda_tids:
        log("detect_oort_threads: no CUDA tids in InfluxDB for this session")
        return

    # Map pid → N vertex (first node under the EX for that pid)
    n_by_pid = {}
    for ex in executors.values():
        pid = ex.A_v.get("pid")
        if pid is None:
            continue
        for n_id in ex.Z_v:
            n = nodes.get(n_id)
            if n is not None:
                n_by_pid.setdefault(pid, n)  # first-wins

    created = 0
    for pid in gpu_pids:
        n_vertex = n_by_pid.get(pid)
        if n_vertex is None:
            continue
        cb_tids = cb_tids_by_pid.get(pid, set())
        # Heuristic: for single-GPU-process sessions, all CUDA tids belong
        # to this pid. For multi-GPU-process scenarios we'd cross-check via
        # globalPid; deferred until that case actually appears.
        for tid in sorted(cuda_tids):
            if tid in cb_tids:
                continue  # executor thread, not oort
            label = f"tid_{tid}"
            oore_A = {
                "label": f"{label}_io",
                "etype": "oore",
                "cb_addr": "NA",
                "thread_tid": tid,
                "aspects": [],
            }
            oore = FishVertex("E", next(vertex_counter), oore_A, [], 2)

            oort_A = {
                "label": f"{label}_gpu_loop",
                "ptype": "oort",
                "gpu_node": True,
                "thread_tid": tid,
                "cb_addr": "NA",
            }
            oort = FishVertex("F", next(vertex_counter), oort_A, [], 3)

            oore.Z_v.append(oort.id_v)
            n_vertex.Z_v.append(oore.id_v)
            entities[oore.id_v] = oore
            functions[oort.id_v] = oort
            created += 1

    log(f"detect_oort_threads: {created} oort thread(s) detected "
        f"(+ {created} oore placeholder entity(ies), {created*2} new vertices)")


# ---------------------------------------------------------------------------
# Aspect Attribution
# ---------------------------------------------------------------------------

def attribute_aspects(mongo, executors, nodes, entities):
    """Attribute pub and cli aspects to entities.

    Strategy:
      1. Try fish_publish_link / fish_client_link tracepoints (deterministic)
      2. Fall back to runtime callback window scan (probabilistic)
    """
    coll = mongo["ros2_trace"]
    t0 = time.time()

    # Build callback → entity mapping
    cb_to_entity = {}
    for e_id, e in entities.items():
        cb = e.A_v.get("cb_addr")
        if cb and cb != "NA":
            cb_to_entity[cb] = (e_id, e)

    # --- Try fish_publish_link first ---
    publish_links = list(coll.find({"event": "ros2:fish_publish_link"}))
    client_links = list(coll.find({"event": "ros2:fish_client_link"}))

    if publish_links or client_links:
        log(f"attribute_aspects: using tracepoint method "
            f"({len(publish_links)} pub links, {len(client_links)} cli links)")
        _attribute_via_tracepoints(
            publish_links, client_links, cb_to_entity, mongo, nodes, entities)
    else:
        log(f"attribute_aspects: fish_publish_link not found, using runtime fallback")
        _attribute_via_runtime(mongo, executors, nodes, entities, cb_to_entity)

    log(f"attribute_aspects DONE in {time.time()-t0:.1f}s")


def _attribute_via_tracepoints(publish_links, client_links, cb_to_entity,
                                mongo, nodes, entities):
    """Deterministic aspect attribution from fish_publish_link/fish_client_link."""
    coll = mongo["ros2_trace"]

    # Build publisher_handle → topic from rcl_publisher_init
    pub_handle_to_topic = {}
    for d in coll.find({"event": "ros2:rcl_publisher_init"}):
        p = d["payload"]
        pub_handle_to_topic[p["publisher_handle"]] = p["topic_name"]

    # Build client_handle → service from rcl_client_init
    cli_handle_to_service = {}
    for d in coll.find({"event": "ros2:rcl_client_init"}):
        p = d["payload"]
        cli_handle_to_service[p["client_handle"]] = p["service_name"]

    total_pub = 0
    total_cli = 0

    # Pub aspects from fish_publish_link
    seen_pub = set()  # (entity_id, topic) dedup
    for d in publish_links:
        p = d["payload"]
        pub_handle = p["publisher_handle"]
        callback = p["callback"]
        topic = pub_handle_to_topic.get(pub_handle)
        if not topic:
            continue
        if SKIP_PARAMSERVICE and _is_param_service(topic):
            continue
        pair = cb_to_entity.get(callback)
        if not pair:
            continue
        e_id, entity = pair
        key = (e_id, topic)
        if key in seen_pub:
            continue
        seen_pub.add(key)
        entity.A_v["aspects"].append({"aspect": "pub", "topic": topic})
        total_pub += 1

    # Cli aspects from fish_client_link
    seen_cli = set()
    for d in client_links:
        p = d["payload"]
        cli_handle = p["client_handle"]
        callback = p["callback"]
        service = cli_handle_to_service.get(cli_handle)
        if not service:
            continue
        if SKIP_PARAMSERVICE and _is_param_service(service):
            continue
        pair = cb_to_entity.get(callback)
        if not pair:
            continue
        e_id, entity = pair
        key = (e_id, service)
        if key in seen_cli:
            continue
        seen_cli.add(key)
        entity.A_v["aspects"].append({"aspect": "cli", "service": service})
        total_cli += 1

    log(f"  Tracepoint attribution: {total_pub} pub, {total_cli} cli")


def _attribute_via_runtime(mongo, executors, nodes, entities, cb_to_entity):
    """Fallback: runtime callback window scan (from model.py bulk load)."""
    coll = mongo["ros2_trace"]
    t0 = time.time()

    node_to_pid = {}
    for ex in executors.values():
        for n_id in ex.Z_v:
            node_to_pid[n_id] = ex.A_v['pid']

    # Collect PIDs needing work
    pids_needing = set()
    for node_id, node in nodes.items():
        if node.A_v.get("publishers") or node.A_v.get("service_clients"):
            pid = node_to_pid.get(node_id)
            if pid:
                pids_needing.add(pid)

    # Bulk load publisher/client init
    pub_ht_by_pid = {}
    for d in coll.find({"event": "ros2:rcl_publisher_init"}):
        pid = d["vpid"]
        if pid in pids_needing:
            pub_ht_by_pid.setdefault(pid, {})[
                d["payload"]["publisher_handle"]] = d["payload"]["topic_name"]

    cli_ht_by_pid = {}
    for d in coll.find({"event": "ros2:rcl_client_init"}):
        pid = d["vpid"]
        if pid in pids_needing:
            cli_ht_by_pid.setdefault(pid, {})[
                d["payload"]["client_handle"]] = d["payload"]["service_name"]

    # Bulk load runtime events
    def _full_ns(doc):
        return int(doc["ts"].timestamp() * 1e9) + doc["ts_nanos"]

    events_by_pid = {}
    for evt_name, evt_type, hkey in [
        ("ros2:callback_start", "cb_start", "callback"),
        ("ros2:callback_end", "cb_end", "callback"),
        ("ros2:rcl_publish", "publish", "publisher_handle"),
        ("ros2:rclcpp_client_request_sent", "cli_req", "client_handle"),
    ]:
        for d in coll.find({"event": evt_name}):
            pid = d["vpid"]
            if pid not in pids_needing:
                continue
            events_by_pid.setdefault(pid, []).append(
                (_full_ns(d), evt_type, d["vtid"], d["payload"][hkey]))

    for pid in events_by_pid:
        events_by_pid[pid].sort()

    log(f"  Runtime bulk load: {time.time()-t0:.1f}s")

    # Scan per node
    total_pub = 0
    total_cli = 0

    for node_id, node in nodes.items():
        pid = node_to_pid.get(node_id)
        if not pid:
            continue

        pub_ht = pub_ht_by_pid.get(pid, {})
        cli_ht = cli_ht_by_pid.get(pid, {})
        active_cbs = {}
        entity_pubs = {}
        entity_clis = {}

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

        # Transfer to entities
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

    # Free memory
    import gc
    del events_by_pid, pub_ht_by_pid, cli_ht_by_pid
    gc.collect()

    log(f"  Runtime attribution: {total_pub} pub, {total_cli} cli")


# ---------------------------------------------------------------------------
# Action Detection (post-hoc tagging)
# ---------------------------------------------------------------------------

def detect_actions(entities):
    """Scan entities for /_action/ pattern and tag with action_name/role.

    No abstract action entity is created. Component entities get properties:
      action_name: base name (e.g., "/Drone1/takeoff_action")
      action_role: component role (e.g., "goal", "cancel", "result",
                   "feedback", "status", "expiry")
    """
    ACTION_SRV_ROLES = {
        "send_goal": "goal",
        "cancel_goal": "cancel",
        "get_result": "result",
    }
    ACTION_PUB_ROLES = {"feedback", "status"}

    action_count = 0

    for e_id, entity in entities.items():
        label = entity.A_v.get("label", "")
        if "/_action/" not in label:
            continue

        base, _, component = label.rpartition("/_action/")
        if not base or not component:
            continue

        # Service components
        if component in ACTION_SRV_ROLES:
            entity.A_v["action_name"] = base
            entity.A_v["action_role"] = ACTION_SRV_ROLES[component]
            action_count += 1

        # Check pub aspects for feedback/status
        for aspect in entity.A_v.get("aspects", []):
            topic = aspect.get("topic", "")
            if "/_action/" in topic:
                _, _, pub_comp = topic.rpartition("/_action/")
                if pub_comp in ACTION_PUB_ROLES:
                    aspect["action_name"] = base
                    aspect["action_role"] = pub_comp

        # Sub entities for feedback/status (client side)
        if component in ACTION_PUB_ROLES:
            entity.A_v["action_name"] = base
            entity.A_v["action_role"] = component
            action_count += 1

    if action_count:
        log(f"Actions detected: {action_count} component entities tagged")


# ---------------------------------------------------------------------------
# Split Callbacks (cli aspect → continuation function)
# ---------------------------------------------------------------------------

def split_callbacks(mongo, entities, functions):
    """For entities with cli aspects, split callback into part1 + continuation.

    Uses rclcpp_client_request_sent and rclcpp_client_response_received
    tracepoints to determine the split point.

    Creates:
      [F] entity_cb_part1 (before request)
      [F] entity_cb_continuation (after response)
    """
    coll = mongo["ros2_trace"]

    # Check if split tracepoints exist
    has_request_sent = coll.count_documents(
        {"event": "ros2:rclcpp_client_request_sent"}, limit=1)
    if not has_request_sent:
        log("split_callbacks: no rclcpp_client_request_sent events, skipping")
        return

    split_count = 0

    for e_id, entity in entities.items():
        cli_aspects = [a for a in entity.A_v.get("aspects", [])
                       if a.get("aspect") == "cli"]
        if not cli_aspects:
            continue
        if not entity.Z_v:
            continue

        # This entity has cli aspect + function → split
        for f_id in list(entity.Z_v):
            func = functions.get(f_id)
            if not func:
                continue

            # Rename original to _part1
            orig_label = func.A_v["label"]
            func.A_v["label"] = f"{orig_label}::part1"
            func.A_v["ptype"] = "cpu"
            func.A_v["split"] = "request"

            # Create continuation function
            cont_A = {
                "label": f"{orig_label}::continuation",
                "ptype": "cpu",
                "cb_addr": func.A_v.get("cb_addr"),
                "split": "response",
            }
            cont = FishVertex("F", next(vertex_counter), cont_A, [], 3)
            entity.Z_v.append(cont.id_v)
            functions[cont.id_v] = cont
            split_count += 1

    if split_count:
        log(f"split_callbacks: {split_count} callbacks split (part1 + continuation)")


# ---------------------------------------------------------------------------
# Graph Construction
# ---------------------------------------------------------------------------

def create_graph(executors, nodes, entities, functions, session_name=""):
    """Build directed graph with vertical containment edges."""
    G = nx.DiGraph()

    # L-1: Container node wrapping all executors
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

    # CN → EX
    for ex_id in cn.Z_v:
        G.add_edge(cn.id_v, ex_id, rel="contains", level="L-1_L0")
    # EX → N
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


def add_horizontal_edges(G, mongo, executors, nodes, entities, functions):
    """Add communication edges from entity aspects.

    pub aspect → sub entity (nature: msg)
    cli aspect → serv entity (nature: dep, with split callback precedence)
    """
    # Topic metadata
    topic_meta = {}
    for d in mongo["topic_info"].find():
        t = d.get("topic")
        if t:
            topic_meta[t] = {
                "msg_type": d.get("type", ""),
                "pub_count": d.get("publisher_count", 0),
                "sub_count": d.get("subscription_count", 0),
            }
    for d in mongo["topic_hz"].find():
        t = d.get("topic")
        if t:
            topic_meta.setdefault(t, {})["avg_rate_hz"] = d.get("average_rate", 0)

    # --- Build indexes ---
    # sub entities by topic
    sub_index = {}      # topic → [(entity_id, node_id)]
    # serv entities by service name
    srv_index = {}      # service → [(entity_id, node_id)]
    # pub aspects by topic
    pub_index = {}      # topic → [(entity_id, node_id)]
    # cli aspects by service
    cli_index = {}      # service → [(entity_id, node_id)]

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

    # --- L2 entity edges ---
    l2_count = 0

    # Topic edges: pub → sub
    for topic, sub_list in sub_index.items():
        pub_entities = pub_index.get(topic, [])
        if pub_entities:
            meta = topic_meta.get(topic, {})
            for pub_e, pub_n in pub_entities:
                for sub_e, sub_n in sub_list:
                    if pub_n == sub_n:
                        continue
                    attrs = {"rel": "comm", "level": "L2", "nature": "msg",
                             "topic": topic}
                    if meta.get("msg_type"):
                        attrs["msg_type"] = meta["msg_type"]
                    if meta.get("avg_rate_hz"):
                        attrs["avg_rate_hz"] = meta["avg_rate_hz"]
                    G.add_edge(pub_e, sub_e, **attrs)
                    l2_count += 1

    # Service edges: cli → serv (dep)
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

    # --- External boundary entities ---
    ext_count = 0
    all_pub_topics = set(pub_index.keys())
    all_sub_topics = set(sub_index.keys())

    # Topics with subscribers but no publisher
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

    # Topics with publishers but no subscriber
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

    # --- L3 propagation ---
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
                           **{k: v for k, v in data.items() if k not in ("rel", "level")})
                l3_count += 1
    log(f"  L3 propagated: {l3_count}")

    # --- L1 aggregation ---
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

    # --- L0 aggregation ---
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

    # --- L-1 aggregation (container level, used by fish_compose) ---
    # Single-container: no cross-container edges, but the structure is there
    # fish_compose.py handles multi-container L-1 edges

    total = l2_count + l3_count + l1_count + l0_count
    log(f"  Total horizontal: {total}")
    return G


# ---------------------------------------------------------------------------
# JSON Export
# ---------------------------------------------------------------------------

def export_json(G, output_path="fish_graph.json"):
    """Export graph as JSON for fish_viz.html."""
    nodes_out = []
    edges_out = []

    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if not v:
            continue
        n = {"id": v.id_v, "type": v.t_v, "label": v.A_v.get("label", ""),
             "level": v.level, "children": v.Z_v}
        for key in ["pid", "full_name", "etype", "ptype", "external",
                     "aspects", "gpu_node", "action_name", "action_role",
                     "split", "period_ns"]:
            val = v.A_v.get(key)
            if val is not None:
                n[key] = val
        nodes_out.append(n)

    for u, v, data in G.edges(data=True):
        edge = {"source": u, "target": v}
        for key in ["rel", "level", "nature", "topic", "service",
                     "msg_type", "avg_rate_hz", "external", "direction",
                     "topics", "comm_count", "cross_container",
                     "src_container", "dst_container", "tau"]:
            val = data.get(key)
            if val is not None:
                if isinstance(val, set):
                    val = list(val)
                edge[key] = val
        edges_out.append(edge)

    import json
    with open(output_path, "w") as f:
        json.dump({"nodes": nodes_out, "edges": edges_out}, f, indent=2, default=str)

    log(f"JSON: {len(nodes_out)} nodes, {len(edges_out)} edges → {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="FISH model extraction (improved)")
    ap.add_argument("--session", required=True,
                    help="MongoDB database name for this session")
    ap.add_argument("--role", default=None,
                    help="Container role (aircraft/simulation/ground/...) "
                         "for multi-container sessions; unset for standalone")
    ap.add_argument("--scope", default=None,
                    help="Graph scope (default: --role value, or '__main__' "
                         "for standalone)")
    ap.add_argument("--no-split", action="store_true", help="Disable callback splitting")
    ap.add_argument("--no-db", action="store_true",
                    help="Skip persisting the graph into graph_store")
    ap.add_argument("--source-trace", default=None,
                    help="Trace directory path, stored as metadata")
    args = ap.parse_args()

    mongo = connect_mongo(args.session, role=args.role)
    influx = connect_influx(args.session, role=args.role)

    # Phase 1: Vertex discovery
    executors, nodes = identify_executors(mongo)
    entities = identify_entities(mongo, nodes)
    functions = identify_callbacks(mongo, nodes, entities, executors)
    attribute_aspects(mongo, executors, nodes, entities)
    attach_callback_groups(mongo, executors, nodes, entities, functions)
    detect_oort_threads(mongo, executors, nodes, entities, functions,
                        session=args.session,
                        container=args.role or args.session)
    detect_actions(entities)

    if not args.no_split:
        split_callbacks(mongo, entities, functions)

    # Phase 2: Graph construction
    # CN label uses the role if set, else the session name.
    cn_label = args.role or args.session
    G = create_graph(executors, nodes, entities, functions, session_name=cn_label)
    G = add_horizontal_edges(G, mongo, executors, nodes, entities, functions)

    # Export
    export_json(G)

    if not args.no_db:
        from graph_store import save_graph, STANDALONE_SCOPE
        scope = args.scope or args.role or STANDALONE_SCOPE
        meta = save_graph(
            G, args.session, scope,
            source_trace=args.source_trace,
            container_role=args.role,
            is_composed=False,
            actor="model_improved",
        )
        log(f"Saved to graph_store: db='{args.session}' scope='{scope}' "
            f"{meta['stats']['num_nodes']} nodes, "
            f"{meta['stats']['num_edges']} edges")

    log("Done.")
