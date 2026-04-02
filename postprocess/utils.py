"""
FISH Utilities — pretty printing, helpers
"""
import os
import re
import subprocess


def shorten_symbol(sym):
    """Shorten a C++ callback symbol to its meaningful core."""
    if not sym or sym == "?":
        return sym

    if "ParameterService" in sym:
        m = re.search(r'srv::(\w+?)_(?:Request|Response)', sym)
        op = m.group(1) if m else "op"
        return f"ParameterService::{op}"

    if "TimeSource" in sym:
        if "clock_sub" in sym:
            return "TimeSource::clock_sub"
        if "ParameterEvent" in sym:
            return "TimeSource::param_event"
        return "TimeSource::cb"

    if "message_filters::Subscriber" in sym:
        m = re.search(r'Subscriber<(\w+::msg::\w+)', sym)
        msg = m.group(1) if m else "?"
        return f"msg_filter::Sub<{msg}>"

    m = re.search(r'_Bind<void \((?:(\w+(?:::\w+)*)::)', sym)
    if m:
        cls = m.group(1)
        msg_m = re.search(r'(?:msg|srv)::(\w+?)_?<', sym)
        msg = msg_m.group(1) if msg_m else None
        if sym.rstrip().endswith("()>"):
            return f"{cls}::timer_cb"
        if msg:
            return f"{cls}::cb({msg})"
        return f"{cls}::cb"

    if "TransformListener" in sym:
        return "tf2::TransformListener::cb"

    if len(sym) > 60:
        return "..." + sym[-57:]
    return sym


def nsys_profiled_pids(mongo):
    """Return set of PIDs that are running under nsys profiling.

    FISH kills GPU-intensive nodes and resurrects them wrapped in nsys.
    fish_events records: action='resurrect', resurrected_pid=<nsys PID>.
    The actual node PID is a grandchild of the nsys process, found via
    process_tree: nsys → nsys-launcher → actual_node.

    These nodes get their Function layer from nsys data (gpu_kernels,
    cuda_runtime) instead of rclcpp callback chains.
    """
    profiled = set()
    pt = mongo["process_tree"]

    for doc in mongo["fish_events"].find({"action": "resurrect"}):
        nsys_pid = doc.get("resurrected_pid")
        if nsys_pid is None:
            continue

        # nsys_pid is the nsys profiler process.
        # Chain: nsys (resurrected_pid) → nsys-launcher → actual node
        # Find children of nsys_pid
        for child in pt.find({"PPID": nsys_pid}):
            child_pid = child["PID"]
            child_cmd = child.get("CMD", "")
            if "nsys-launcher" in child_cmd or "nsys" in child_cmd:
                # This is nsys-launcher, look one level deeper
                for grandchild in pt.find({"PPID": child_pid}):
                    gc_cmd = grandchild.get("CMD", "")
                    if "nsys" not in gc_cmd:
                        # This is the actual node process
                        profiled.add(grandchild["PID"])
                        print(f"  nsys-profiled: pid={grandchild['PID']} cmd={gc_cmd[:80]}")
            elif "nsys" not in child_cmd:
                # Direct child that's not nsys tooling
                profiled.add(child_pid)
                print(f"  nsys-profiled: pid={child_pid} cmd={child_cmd[:80]}")

    if profiled:
        print(f"  Total nsys-profiled PIDs: {profiled}")
    return profiled


def confirmed_kills(mongo):
    """Return set of PIDs that were killed during the session, from fish_events.

    fish_events has action='kill' entries with killed_pid.
    These are processes that FISH intentionally killed and resurrected,
    so the original PID is dead and should be skipped.
    """
    killed_pids = set()
    for doc in mongo["fish_events"].find({"action": "kill"}):
        pid = doc.get("killed_pid")
        if pid is not None:
            killed_pids.add(pid)
            print(f"  kill: pid={pid} name={doc.get('process_name', '?')}")
    print(f"  Total killed PIDs: {killed_pids}")
    return killed_pids


def entity_fallback_from_trace(mongo, node_handle):
    """Discover entities for a node from ros2_trace when node_info is missing.

    Uses node_handle (from rcl_node_init) to find:
      - rcl_subscription_init → subscriptions (topic_name, queue_depth)
      - rcl_publisher_init → publishers (topic_name, queue_depth)
      - rcl_service_init → services (service_name)
      - rclcpp_timer_link_node → timers (via timer_handle → rcl_timer_init for period)

    Returns a dict matching node_info structure:
      {"Subscribers": {topic: "?"}, "Publishers": {topic: "?"},
       "Service_Servers": {name: "?"}, "Timers": {handle: period_ns}}
    """
    coll = mongo["ros2_trace"]
    result = {
        "Subscribers": {},
        "Publishers": {},
        "Service_Servers": {},
        "Service_Clients": {},
        "Action_Servers": {},
        "Action_Clients": {},
        "Timers": {},
    }

    # Subscriptions
    for doc in coll.find({"event": "ros2:rcl_subscription_init", "payload.node_handle": node_handle}):
        topic = doc["payload"].get("topic_name", "?")
        result["Subscribers"][topic] = "?"  # msg type not available in trace

    # Publishers
    for doc in coll.find({"event": "ros2:rcl_publisher_init", "payload.node_handle": node_handle}):
        topic = doc["payload"].get("topic_name", "?")
        result["Publishers"][topic] = "?"

    # Services
    for doc in coll.find({"event": "ros2:rcl_service_init", "payload.node_handle": node_handle}):
        name = doc["payload"].get("service_name", "?")
        result["Service_Servers"][name] = "?"

    # Service clients
    for doc in coll.find({"event": "ros2:rcl_client_init", "payload.node_handle": node_handle}):
        name = doc["payload"].get("service_name", "?")
        result["Service_Clients"][name] = "?"

    # Timers — linked via rclcpp_timer_link_node → timer_handle → rcl_timer_init
    for doc in coll.find({"event": "ros2:rclcpp_timer_link_node", "payload.node_handle": node_handle}):
        timer_handle = doc["payload"]["timer_handle"]
        timer_init = coll.find_one({"event": "ros2:rcl_timer_init", "payload.timer_handle": timer_handle})
        period = timer_init["payload"]["period"] if timer_init else 0
        result["Timers"][timer_handle] = period

    print(f"  FALLBACK for node_handle={node_handle}: "
          f"{len(result['Subscribers'])} subs, {len(result['Publishers'])} pubs, "
          f"{len(result['Service_Servers'])} srvs, {len(result['Timers'])} timers")

    return result


def verify_actions(info):
    """Detect ROS 2 actions from flat service/publisher lists.

    Scans Service_Servers and Publishers for the /_action/ pattern,
    groups them by action base name, and verifies all 5 components exist.

    Component entities stay in their original lists (Service_Servers,
    Publishers) — the action is an abstract entity that references them.

    Returns:
      actions: dict of {action_name: {component_name: endpoint_name}}
      info: info dict with Action_Servers added (components NOT removed)
    """
    ACTION_SERVICES = {"send_goal", "cancel_goal", "get_result"}
    ACTION_TOPICS = {"feedback", "status"}

    # Collect all /_action/ endpoints
    action_parts = {}  # base_name → {component: full_name}

    for srv_name in list(info.get("Service_Servers", {}).keys()):
        if "/_action/" in srv_name:
            base, component = srv_name.rsplit("/_action/", 1)
            action_parts.setdefault(base, {})[component] = srv_name

    for pub_name in list(info.get("Publishers", {}).keys()):
        if "/_action/" in pub_name:
            base, component = pub_name.rsplit("/_action/", 1)
            action_parts.setdefault(base, {})[component] = pub_name

    actions = {}
    for base_name, components in action_parts.items():
        found_srvs = set(components.keys()) & ACTION_SERVICES
        found_topics = set(components.keys()) & ACTION_TOPICS

        if found_srvs == ACTION_SERVICES and found_topics == ACTION_TOPICS:
            # Complete action — all 5 components present
            actions[base_name] = components
            # Add as abstract action entity (components stay in their original lists)
            info.setdefault("Action_Servers", {})[base_name] = {
                "components": components
            }
            print(f"  ACTION detected: {base_name} (5/5 components)")
        else:
            missing = (ACTION_SERVICES - found_srvs) | (ACTION_TOPICS - found_topics)
            print(f"  WARNING: incomplete action {base_name}, missing: {missing}")

    return actions, info


def pretty_print_executors(executors, nodes):
    """Print executor→node tree with all FishVertex attributes."""
    print("=" * 70)
    print("FISH Graph — Executor → Node Tree")
    print("=" * 70)

    for ex_id, ex in executors.items():
        print(f"\n[EX] id={ex.id_v}  level={ex.level}")
        print(f"  A_v:")
        for k, v in ex.A_v.items():
            print(f"    {k}: {v}")
        print(f"  Z_v: {ex.Z_v}")

        for child_id in ex.Z_v:
            n = nodes.get(child_id)
            if n is None:
                print(f"  └── [N] id={child_id}  (NOT FOUND)")
                continue
            is_last = (child_id == ex.Z_v[-1])
            prefix = "└──" if is_last else "├──"
            print(f"  {prefix} [N] id={n.id_v}  level={n.level}")
            print(f"  {'    ' if is_last else '│   '}A_v:")
            for k, v in n.A_v.items():
                print(f"  {'    ' if is_last else '│   '}  {k}: {v}")
            if n.Z_v:
                print(f"  {'    ' if is_last else '│   '}Z_v: {n.Z_v}")

    print("\n" + "=" * 70)
    print(f"Total: {len(executors)} executors, {len(nodes)} nodes")
    print("=" * 70)


def pretty_print_three_layers(executors, nodes, entities):
    """Print EX → N → E tree with attributes."""
    print("=" * 70)
    print("FISH Graph — Executor → Node → Entity Tree")
    print("=" * 70)

    total_e = 0
    for ex_id, ex in executors.items():
        print(f"\n[EX] id={ex.id_v}  label={ex.A_v.get('label','')}  pid={ex.A_v.get('pid','')}  mode={ex.A_v.get('mode','')}")

        for i, n_id in enumerate(ex.Z_v):
            n = nodes.get(n_id)
            if n is None:
                print(f"  └── [N] id={n_id}  (NOT FOUND)")
                continue
            n_last = (i == len(ex.Z_v) - 1)
            n_prefix = "└──" if n_last else "├──"
            n_cont = "    " if n_last else "│   "

            pubs = n.A_v.get('publishers', {})
            pub_str = f"  ({len(pubs)} pubs)" if pubs else ""
            print(f"  {n_prefix} [N] id={n.id_v}  label={n.A_v.get('label','')}{pub_str}")
            print(f"  {n_cont}full_name: {n.A_v.get('full_name','')}")

            # Entity children
            entity_ids = [eid for eid in n.Z_v if eid in entities]
            for j, e_id in enumerate(entity_ids):
                e = entities[e_id]
                e_last = (j == len(entity_ids) - 1)
                e_prefix = "└──" if e_last else "├──"
                e_cont = "    " if e_last else "│   "

                etype = e.A_v.get('etype', '?')
                label = e.A_v.get('label', '?')

                # Type-specific extra info
                extra = ""
                if etype == "sub":
                    extra = f"  msg={e.A_v.get('msg_type', '?')}"
                elif etype == "serv":
                    extra = f"  srv={e.A_v.get('srv_type', '?')}"
                elif etype == "tmr":
                    period = e.A_v.get('period_ns', 0)
                    if period >= 1_000_000_000:
                        extra = f"  period={period / 1e9:.1f}s"
                    elif period >= 1_000_000:
                        extra = f"  period={period / 1e6:.1f}ms"
                    else:
                        extra = f"  period={period}ns"
                if e.A_v.get("action_name"):
                    a_short = e.A_v['action_name'].rsplit('/',1)[-1]
                    extra += f"  action:{a_short}:{e.A_v.get('action_role','?')}"

                print(f"  {n_cont}{e_prefix} [{etype.upper()}] id={e.id_v}  {label}{extra}")

                total_e += 1

    print("\n" + "=" * 70)
    print(f"Total: {len(executors)} executors, {len(nodes)} nodes, {total_e} entities")
    print("=" * 70)


def pretty_print_four_layers(executors, nodes, entities, functions):
    """Print EX → N → E → F tree with callback info."""
    print("=" * 70)
    print("FISH Graph — Executor → Node → Entity → Function Tree")
    print("=" * 70)

    total_e = 0
    total_f = 0
    for ex_id, ex in executors.items():
        print(f"\n[EX] id={ex.id_v}  label={ex.A_v.get('label','')}  pid={ex.A_v.get('pid','')}  mode={ex.A_v.get('mode','')}")

        for i, n_id in enumerate(ex.Z_v):
            n = nodes.get(n_id)
            if n is None:
                continue
            n_last = (i == len(ex.Z_v) - 1)
            n_pre = "└── " if n_last else "├── "
            n_cont = "    " if n_last else "│   "

            pubs = n.A_v.get('publishers', {})
            pub_str = f"  ({len(pubs)} pubs)" if pubs else ""
            print(f"  {n_pre}[N] id={n.id_v}  {n.A_v.get('full_name', n.A_v.get('label',''))}{pub_str}")

            # Entity children
            entity_ids = [eid for eid in n.Z_v if eid in entities]
            for j, e_id in enumerate(entity_ids):
                e = entities[e_id]
                e_last = (j == len(entity_ids) - 1)
                e_pre = "└── " if e_last else "├── "
                e_cont = "    " if e_last else "│   "

                etype = e.A_v.get('etype', '?')
                label = e.A_v.get('label', '?')

                # Type-specific extra info
                extra = ""
                if etype == "tmr":
                    period = e.A_v.get('period_ns', 0)
                    if period >= 1_000_000_000:
                        extra = f"  period={period / 1e9:.1f}s"
                    elif period >= 1_000_000:
                        extra = f"  period={period / 1e6:.1f}ms"
                    else:
                        extra = f"  period={period}ns"
                if e.A_v.get("action_name"):
                    a_short = e.A_v['action_name'].rsplit('/',1)[-1]
                    extra += f"  action:{a_short}:{e.A_v.get('action_role','?')}"

                cb_addr = e.A_v.get('cb_addr', 'NA')
                cb_str = f"  cb={cb_addr}" if cb_addr != "NA" else ""
                print(f"  {n_cont}{e_pre}[{etype.upper()}] id={e.id_v}  {label}{extra}{cb_str}")

                total_e += 1

                # Function children
                func_ids = [fid for fid in e.Z_v if fid in functions]
                for k, f_id in enumerate(func_ids):
                    f = functions[f_id]
                    f_last = (k == len(func_ids) - 1)
                    f_pre = "└── " if f_last else "├── "

                    sym_short = shorten_symbol(f.A_v.get('label', '?'))
                    ptype = f.A_v.get('ptype', '?')

                    print(f"  {n_cont}{e_cont}{f_pre}[F] id={f.id_v}  {sym_short}  ptype={ptype}")
                    total_f += 1

    print("\n" + "=" * 70)
    print(f"Total: {len(executors)} executors, {len(nodes)} nodes, {total_e} entities, {total_f} functions")
    print("=" * 70)


def create_detection_summary(executors, nodes, entities, functions, mongo):
    """Print a summary of Function layer detection: matched, GPU-pending, action, unexpected."""
    gpu_pids = nsys_profiled_pids(mongo)
    node_to_pid = {}
    for ex_id, ex in executors.items():
        for n_id in ex.Z_v:
            node_to_pid[n_id] = ex.A_v['pid']

    gpu_node_ids = {n_id for n_id, pid in node_to_pid.items() if pid in gpu_pids}
    gpu_entity_ids = set()
    for n_id in gpu_node_ids:
        gpu_entity_ids.update(nodes[n_id].Z_v)

    print(f"\n--- Function Layer (Layer 3) ---")
    print(f"Total Function vertices: {len(functions)}")
    matched = sum(1 for e in entities.values() if e.A_v.get("cb_addr", "NA") != "NA")
    print(f"Entities with callbacks: {matched}/{len(entities)}")

    no_cb_gpu = [e for eid, e in entities.items() if eid in gpu_entity_ids and e.A_v.get("cb_addr", "NA") == "NA"]
    no_cb_action = [e for e in entities.values() if e.A_v.get("cb_addr", "NA") == "NA"
                    and e.A_v.get("action_name")]
    no_cb_other = [e for eid, e in entities.items() if e.A_v.get("cb_addr", "NA") == "NA"
                   and not e.A_v.get("action_name")
                   and eid not in gpu_entity_ids]

    if no_cb_gpu:
        print(f"  nsys-profiled (GPU function layer pending): {len(no_cb_gpu)} entities")
    if no_cb_action:
        print(f"  action components (internal dispatch): {len(no_cb_action)} entities")
    if no_cb_other:
        print(f"  UNEXPECTED missing callbacks ({len(no_cb_other)}):")
        for e in no_cb_other:
            print(f"    [{e.A_v['etype']}] {e.A_v['label']}")


def create_graph_summary(G):
    """Print a summary of the FISH DiGraph: vertex/edge counts by level and type."""
    print(f"\n--- FISH Graph Summary ---")
    print(f"Vertices: {G.number_of_nodes()}")
    print(f"Edges:    {G.number_of_edges()}")
    v_edges = sum(1 for _, _, d in G.edges(data=True) if d.get("rel") == "contains")
    h_edges = sum(1 for _, _, d in G.edges(data=True) if d.get("rel") == "comm")
    print(f"  Vertical (contains): {v_edges}")
    print(f"  Horizontal (comm):   {h_edges}")
    for level in ["L0", "L1", "L2", "L3"]:
        cnt = sum(1 for _, _, d in G.edges(data=True) if d.get("level") == level and d.get("rel") == "comm")
        if cnt:
            print(f"    {level}: {cnt}")


def _dot_escape(s):
    """Escape a string for use in DOT labels."""
    return str(s).replace('"', '\\"').replace('<', '\\<').replace('>', '\\>')


def _short_topic(topic):
    """Shorten a topic/service name for graph labels."""
    # Remove common prefixes
    if topic.startswith("/Drone1/"):
        topic = topic[8:]
    # Remove parameter service boilerplate
    for suffix in ["/describe_parameters", "/get_parameter_types",
                   "/get_parameters", "/list_parameters",
                   "/set_parameters_atomically", "/set_parameters"]:
        if topic.endswith(suffix):
            return topic.split("/")[-1][:12]
    # For action components, shorten
    if "/_action/" in topic:
        base, comp = topic.rsplit("/_action/", 1)
        return base.split("/")[-1][:10] + "/" + comp[:6]
    parts = topic.strip("/").split("/")
    return parts[-1][:20] if parts else topic[:20]


def _is_boilerplate(v):
    """Check if a vertex is ROS 2 boilerplate (ParameterService, TimeSource, /parameter_events)."""
    if v.t_v == "E":
        label = v.A_v.get("label", "")
        if "parameter" in label.lower():
            return True
    elif v.t_v == "F":
        label = v.A_v.get("label", "")
        if "ParameterService" in label or "TimeSource::param_event" in label:
            return True
    return False


def export_fish_overview(G, out_path, fmt="pdf"):
    """Export L0+L1 overview graph: executors and nodes with comm edges.

    Shows the high-level system topology: which processes talk to which,
    and which nodes are involved. Entity and function layers are hidden.
    """
    lines = [
        'digraph FISH_Overview {',
        '  rankdir=LR;',
        '  splines=true;',
        '  nodesep=0.8;',
        '  ranksep=1.5;',
        '  bgcolor=white;',
        '  label="FISH Graph — L0/L1 Overview";',
        '  labelloc=t;',
        '  fontsize=14;',
        '',
    ]

    # Collect executors and their nodes
    executors = {}
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v and v.t_v == "EX":
            executors[nid] = v

    nodes = {}
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v and v.t_v == "N":
            nodes[nid] = v

    # Cluster per executor
    for ex_id, ex_v in executors.items():
        ex_label = ex_v.A_v.get("label", "?")
        pid = ex_v.A_v.get("pid", "?")
        lines.append(f'  subgraph cluster_{ex_id} {{')
        lines.append(f'    label="EX: {_dot_escape(ex_label)}\\npid={pid}";')
        lines.append(f'    style="rounded,filled"; fillcolor="#D6EAF8";')
        lines.append(f'    color="#2C3E50"; fontsize=11;')

        for n_id in ex_v.Z_v:
            if n_id not in nodes:
                continue
            nv = nodes[n_id]
            name = nv.A_v.get("label", "?")
            pubs = len(nv.A_v.get("publishers", {}))
            ents = len([eid for eid in nv.Z_v if G.nodes.get(eid, {}).get("v", None)
                        and G.nodes[eid]["v"].t_v == "E"])
            label = f"{_dot_escape(name)}\\n({ents}E, {pubs}P)"
            lines.append(f'    {n_id} [label="{label}", shape=circle, style=filled, '
                         f'fillcolor="#90EE90", fontsize=9, width=1.0];')

        lines.append(f'  }}')
        lines.append('')

    # L1 horizontal edges only
    for u, v, data in G.edges(data=True):
        if data.get("rel") != "comm" or data.get("level") != "L1":
            continue
        count = data.get("comm_count", 1)
        topics = data.get("topics", [])
        topic_str = "\\n".join(_short_topic(t) for t in topics[:3])
        if len(topics) > 3:
            topic_str += f"\\n+{len(topics)-3} more"

        natures = data.get("natures", set())
        if "dep" in natures:
            edge_style = 'style=solid'
        else:
            edge_style = 'style=dashed'

        lines.append(f'  {u} -> {v} [label="{topic_str}", {edge_style}, '
                     f'fontsize=7, arrowsize=0.8];')

    lines.append('}')

    dot_path = out_path + ".dot"
    with open(dot_path, "w") as f:
        f.write("\n".join(lines))

    out_file = out_path + "." + fmt
    result = subprocess.run(
        ["dot", f"-T{fmt}", dot_path, "-o", out_file],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"[FISH viz] dot error: {result.stderr[:200]}")
    else:
        print(f"[FISH viz] Overview: {out_file}")
    return out_file


def export_fish_detail(G, ex_id, out_path, fmt="pdf", filter_boilerplate=True):
    """Export a detail graph for a single executor: N→E→F subtree with L2 edges.

    Shows the internal structure of one executor with all its entities
    and functions, plus incoming/outgoing L2 comm edges.
    """
    ex_v = G.nodes[ex_id]["v"]
    ex_label = ex_v.A_v.get("label", "?")
    pid = ex_v.A_v.get("pid", "?")

    skip_ids = set()
    if filter_boilerplate:
        for nid, data in G.nodes(data=True):
            v = data.get("v")
            if v and _is_boilerplate(v):
                skip_ids.add(nid)
                if v.t_v == "E":
                    for fid in v.Z_v:
                        skip_ids.add(fid)

    # Collect all IDs in this executor's subtree
    subtree_ids = {ex_id}
    for n_id in ex_v.Z_v:
        nv_data = G.nodes.get(n_id, {})
        nv = nv_data.get("v")
        if not nv:
            continue
        subtree_ids.add(n_id)
        for e_id in nv.Z_v:
            ev_data = G.nodes.get(e_id, {})
            ev = ev_data.get("v")
            if not ev or ev.t_v != "E":
                continue
            if e_id in skip_ids:
                continue
            subtree_ids.add(e_id)
            for f_id in ev.Z_v:
                if f_id not in skip_ids:
                    subtree_ids.add(f_id)

    STYLE = {
        "EX": 'shape=circle, style=filled, fillcolor="#ADD8E6", fontsize=11, width=1.2',
        "N":  'shape=circle, style=filled, fillcolor="#90EE90", fontsize=9, width=1.0',
        "E":  'shape=circle, style=filled, fillcolor="#FFD700", fontsize=8, width=0.7',
        "F":  'shape=circle, style=filled, fillcolor="white", fontsize=7, width=0.5',
    }

    lines = [
        'digraph FISH_Detail {',
        '  rankdir=TB;',
        '  splines=true;',
        '  nodesep=0.6;',
        '  ranksep=1.0;',
        '  bgcolor=white;',
        f'  label="FISH Detail — {_dot_escape(ex_label)} (pid={pid})";',
        '  labelloc=t;',
        '  fontsize=14;',
        '',
    ]

    # External nodes referenced by L2 edges (source nodes from other executors)
    external_ids = set()
    for u, v, data in G.edges(data=True):
        if data.get("level") == "L2" and data.get("rel") == "comm":
            if v in subtree_ids and u not in subtree_ids:
                external_ids.add(u)
            elif u in subtree_ids and v not in subtree_ids:
                external_ids.add(v)

    # Add internal nodes
    for nid in subtree_ids:
        v = G.nodes[nid].get("v")
        if not v:
            continue
        t_v = v.t_v
        style = STYLE.get(t_v, 'shape=circle')

        if t_v == "EX":
            label = f"EX\\n{_dot_escape(v.A_v.get('label', '?'))}\\npid={pid}"
        elif t_v == "N":
            label = f"N\\n{_dot_escape(v.A_v.get('label', '?'))}"
        elif t_v == "E":
            etype = v.A_v.get('etype', '?')
            short = _short_topic(v.A_v.get('label', '?'))
            label = f"{etype.upper()}\\n{_dot_escape(short)}"
        elif t_v == "F":
            sym = shorten_symbol(v.A_v.get('label', '?'))
            ptype = v.A_v.get('ptype', '?')
            if len(sym) > 22:
                sym = sym[:20] + ".."
            label = f"{_dot_escape(sym)}\\n({ptype})"
        else:
            label = str(nid)

        lines.append(f'  {nid} [label="{label}", {style}];')

    # Add external reference nodes (gray, smaller)
    for nid in external_ids:
        v = G.nodes.get(nid, {}).get("v")
        if not v:
            continue
        name = v.A_v.get("label", v.A_v.get("full_name", str(nid)))
        label = f"ext\\n{_dot_escape(name)}"
        lines.append(f'  {nid} [label="{label}", shape=circle, style="filled,dashed", '
                     f'fillcolor="#D5D8DC", fontsize=8, width=0.8];')

    lines.append('')

    # Edges: vertical (within subtree) + L2 horizontal (touching subtree)
    for u, v, data in G.edges(data=True):
        rel = data.get("rel", "?")

        if rel == "contains" and u in subtree_ids and v in subtree_ids:
            lines.append(f'  {u} -> {v} [color="#E74C3C", style=dashed, arrowsize=0.7];')

        elif rel == "comm" and data.get("level") in ("L2", "L3"):
            if u not in subtree_ids and u not in external_ids:
                continue
            if v not in subtree_ids and v not in external_ids:
                continue

            nature = data.get("nature", "msg")
            topic = data.get("topic", data.get("service", ""))
            short = _short_topic(topic) if topic else ""

            if nature == "dep":
                edge_style = 'color="black", style=solid, arrowsize=0.8'
            else:
                edge_style = 'color="black", style=dashed, arrowsize=0.8'
            if short:
                edge_style += f', label="{_dot_escape(short)}", fontsize=7'

            lines.append(f'  {u} -> {v} [{edge_style}];')

    lines.append('}')

    dot_path = out_path + ".dot"
    with open(dot_path, "w") as f:
        f.write("\n".join(lines))

    out_file = out_path + "." + fmt
    result = subprocess.run(
        ["dot", f"-T{fmt}", dot_path, "-o", out_file],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"[FISH viz] dot error: {result.stderr[:200]}")
    else:
        print(f"[FISH viz] Detail: {out_file}")
    return out_file


def export_fish_dot(G, out_path, fmt="pdf", filter_boilerplate=True):
    """Export FISH DiGraph to DOT format and render with graphviz.

    Visual encoding (matching FISH paper slide style):
      EX: lightblue circle        N: green circle
      E:  gold/orange circle      F: small white circle
      Vertical edges:  red dashed arrows (containment)
      Horizontal msg:  black dashed arrows (topic communication)
      Horizontal dep:  black solid arrows (service dependency)

    Executor clusters group each EX with its N→E→F subtree.
    filter_boilerplate: if True, skip ParameterService entities/functions.
    """
    STYLE = {
        "EX": 'shape=circle, style=filled, fillcolor="#ADD8E6", '
              'fontsize=11, width=1.2, fixedsize=true',
        "N":  'shape=circle, style=filled, fillcolor="#90EE90", '
              'fontsize=9, width=1.0, fixedsize=true',
        "E":  'shape=circle, style=filled, fillcolor="#FFD700", '
              'fontsize=8, width=0.7, fixedsize=true',
        "F":  'shape=circle, style=filled, fillcolor="white", '
              'fontsize=7, width=0.5, fixedsize=true',
    }

    # Determine which node IDs to skip
    skip_ids = set()
    if filter_boilerplate:
        for nid, data in G.nodes(data=True):
            v = data.get("v")
            if v and _is_boilerplate(v):
                skip_ids.add(nid)
                # Also skip function children of skipped entities
                if v.t_v == "E":
                    for fid in v.Z_v:
                        skip_ids.add(fid)

    def _node_label(v):
        t_v = v.t_v
        if t_v == "EX":
            return f"EX\\n{_dot_escape(v.A_v.get('label', '?'))}\\npid={v.A_v.get('pid', '?')}"
        elif t_v == "N":
            return f"N\\n{_dot_escape(v.A_v.get('label', '?'))}"
        elif t_v == "E":
            is_ext = v.A_v.get('external', False)
            etype = v.A_v.get('etype', '?')
            label_raw = v.A_v.get('label', '?')
            if is_ext:
                short = _short_topic(label_raw.replace('ext:', ''))
                return f"EXT\\n{_dot_escape(short)}"
            short = _short_topic(label_raw)
            return f"{etype.upper()}\\n{_dot_escape(short)}"
        elif t_v == "F":
            sym = shorten_symbol(v.A_v.get('label', '?'))
            ptype = v.A_v.get('ptype', '?')
            if ptype == "ext":
                short = sym.replace('ext:', '')
                short = _short_topic(short) if '/' in short else short
                if len(short) > 22:
                    short = short[:20] + ".."
                return f"EXT\\n{_dot_escape(short)}"
            if len(sym) > 22:
                sym = sym[:20] + ".."
            return f"{_dot_escape(sym)}\\n({ptype})"
        return str(v.id_v)

    def _node_style(v):
        """Return DOT style string for external vs normal vertices."""
        is_ext = v.A_v.get('external', False) or v.A_v.get('ptype') == 'ext'
        if is_ext:
            if v.t_v == "E":
                return 'shape=circle, style="filled,dashed", fillcolor="#D5D8DC", fontsize=8, width=0.7'
            elif v.t_v == "F":
                return 'shape=circle, style="filled,dashed", fillcolor="#E8E8E8", fontsize=7, width=0.5'
        return None  # use default

    # Build executor → subtree map for clustering
    # EX → [N ids] → [E ids] → [F ids]
    ex_nodes = {}  # ex_id → {n_ids, e_ids, f_ids}
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v and v.t_v == "EX":
            subtree = {"n": [], "e": [], "f": []}
            for n_id in v.Z_v:
                n_data = G.nodes.get(n_id, {})
                nv = n_data.get("v")
                if not nv:
                    continue
                subtree["n"].append(n_id)
                for e_id in nv.Z_v:
                    e_data = G.nodes.get(e_id, {})
                    ev = e_data.get("v")
                    if not ev or ev.t_v != "E":
                        continue
                    if e_id in skip_ids:
                        continue
                    subtree["e"].append(e_id)
                    for f_id in ev.Z_v:
                        if f_id in skip_ids:
                            continue
                        subtree["f"].append(f_id)
            ex_nodes[nid] = subtree

    lines = [
        'digraph FISH {',
        '  rankdir=TB;',
        '  splines=true;',
        '  nodesep=0.5;',
        '  ranksep=1.2;',
        '  bgcolor=white;',
        '  compound=true;',
        '',
    ]

    # Create clusters per executor
    for ex_id, subtree in ex_nodes.items():
        ex_v = G.nodes[ex_id]["v"]
        ex_label = ex_v.A_v.get("label", "?")

        lines.append(f'  subgraph cluster_{ex_id} {{')
        lines.append(f'    label="{_dot_escape(ex_label)} (pid={ex_v.A_v.get("pid", "?")})";')
        lines.append(f'    style=rounded;')
        lines.append(f'    color="#2C3E50";')
        lines.append(f'    fontsize=10;')
        lines.append(f'')

        # EX node
        lines.append(f'    {ex_id} [label="{_node_label(ex_v)}", {STYLE["EX"]}];')

        # N nodes
        for n_id in subtree["n"]:
            nv = G.nodes[n_id]["v"]
            lines.append(f'    {n_id} [label="{_node_label(nv)}", {STYLE["N"]}];')

        # E nodes
        for e_id in subtree["e"]:
            ev = G.nodes[e_id]["v"]
            style = _node_style(ev) or STYLE["E"]
            lines.append(f'    {e_id} [label="{_node_label(ev)}", {style}];')

        # F nodes
        for f_id in subtree["f"]:
            fv = G.nodes[f_id]["v"]
            style = _node_style(fv) or STYLE["F"]
            lines.append(f'    {f_id} [label="{_node_label(fv)}", {style}];')

        lines.append(f'  }}')
        lines.append('')

    # External entities (not in any executor subtree)
    clustered_ids = set()
    for subtree in ex_nodes.values():
        clustered_ids.add(subtree.get("ex_id", 0))
        clustered_ids.update(subtree["n"])
        clustered_ids.update(subtree["e"])
        clustered_ids.update(subtree["f"])
    for ex_id in ex_nodes:
        clustered_ids.add(ex_id)

    for nid, data in G.nodes(data=True):
        if nid in clustered_ids or nid in skip_ids:
            continue
        v = data.get("v")
        if not v:
            continue
        if v.A_v.get("external") or v.A_v.get("ptype") == "ext":
            style = _node_style(v)
            if not style:
                style = 'shape=circle, style="filled,dashed", fillcolor="#D5D8DC", fontsize=7, width=0.5'
            lines.append(f'  {nid} [label="{_node_label(v)}", {style}];')

    lines.append('')

    # Edges
    for u, v, data in G.edges(data=True):
        if u in skip_ids or v in skip_ids:
            continue

        rel = data.get("rel", "?")

        if rel == "contains":
            lines.append(f'  {u} -> {v} [color="#E74C3C", style=dashed, arrowsize=0.7];')

        elif rel == "comm":
            nature = data.get("nature", "msg")
            level = data.get("level", "")

            if nature == "dep":
                edge_style = 'color="black", style=solid, arrowsize=0.8'
            else:
                edge_style = 'color="black", style=dashed, arrowsize=0.8'

            if level == "L3":
                topic = data.get("topic", data.get("service", ""))
                if topic:
                    short = _short_topic(topic)
                    edge_style += f', label="{_dot_escape(short)}", fontsize=6'
                edge_style += ', penwidth=0.8'
            elif level == "L2":
                topic = data.get("topic", data.get("service", ""))
                if topic:
                    short = _short_topic(topic)
                    edge_style += f', label="{_dot_escape(short)}", fontsize=7'
            elif level == "L1":
                count = data.get("comm_count", 0)
                if count > 1:
                    edge_style += f', label="{count}", fontsize=8, fontcolor="blue"'
            elif level == "L0":
                count = data.get("comm_count", 0)
                tau = data.get("tau", "?")
                edge_style += f', label="{count} ({tau})", fontsize=8, fontcolor="blue", penwidth=2'

            lines.append(f'  {u} -> {v} [{edge_style}];')

    lines.append('}')

    dot_path = out_path + ".dot"
    with open(dot_path, "w") as f:
        f.write("\n".join(lines))

    out_file = out_path + "." + fmt
    result = subprocess.run(
        ["dot", f"-T{fmt}", dot_path, "-o", out_file],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"[FISH viz] dot error: {result.stderr[:200]}")
    else:
        print(f"[FISH viz] Exported: {out_file}")
        skipped = len(skip_ids)
        if skipped:
            print(f"[FISH viz] Filtered {skipped} boilerplate vertices")

    return out_file


def export_fish_radial(G, out_path, fmt="png", filter_boilerplate=True):
    """Export FISH graph as a radial tree with communication chords.

    Layout: twopi engine — executor at center, nodes on inner ring,
    entities on outer ring. Communication edges as curved arcs.
    Boilerplate entities are filtered by default.
    """
    STYLE = {
        "EX": 'shape=circle, style=filled, fillcolor="#ADD8E6", '
              'fontsize=10, width=1.0',
        "N":  'shape=circle, style=filled, fillcolor="#90EE90", '
              'fontsize=8, width=0.8',
        "E":  'shape=circle, style=filled, fillcolor="#FFD700", '
              'fontsize=7, width=0.55',
        "F":  'shape=point, width=0.15, fillcolor="#CCCCCC", style=filled',
    }

    skip_ids = set()
    if filter_boilerplate:
        for nid, data in G.nodes(data=True):
            v = data.get("v")
            if v and _is_boilerplate(v):
                skip_ids.add(nid)
                if v.t_v == "E":
                    for fid in v.Z_v:
                        skip_ids.add(fid)

    # Create a virtual root that connects to all executors
    lines = [
        'digraph FISH_Radial {',
        '  layout=twopi;',
        '  overlap=false;',
        '  splines=curved;',
        '  ranksep=2.5;',
        '  bgcolor=white;',
        '  label="FISH Graph — Radial Layout";',
        '  labelloc=t;',
        '  fontsize=14;',
        '',
        '  root_center [label="FISH", shape=doublecircle, style=filled, '
        'fillcolor="#2C3E50", fontcolor=white, fontsize=12, width=0.8];',
        '',
    ]

    # Add nodes
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if not v or nid in skip_ids:
            continue
        t_v = v.t_v
        style = STYLE.get(t_v, 'shape=circle')

        is_ext = v.A_v.get('external', False) or v.A_v.get('ptype') == 'ext'

        if t_v == "EX":
            label = f"{_dot_escape(v.A_v.get('label', '?'))}\\npid={v.A_v.get('pid', '?')}"
        elif t_v == "N":
            label = _dot_escape(v.A_v.get('label', '?'))
        elif t_v == "E":
            if is_ext:
                short = _short_topic(v.A_v.get('label', '?').replace('ext:', ''))
                label = f"EXT:{_dot_escape(short)}"
                style = 'shape=circle, style="filled,dashed", fillcolor="#D5D8DC", fontsize=8, width=0.7'
            else:
                etype = v.A_v.get('etype', '?')
                short = _short_topic(v.A_v.get('label', '?'))
                label = f"{etype.upper()}:{_dot_escape(short)}"
        elif t_v == "F":
            if is_ext:
                short = _short_topic(v.A_v.get('label', '?').replace('ext:', ''))
                label = f"EXT:{_dot_escape(short)}" if short else "EXT"
                style = 'shape=point, width=0.15, fillcolor="#999999", style=filled'
            else:
                label = ""
        else:
            continue

        lines.append(f'  {nid} [label="{label}", {style}];')

    lines.append('')

    # Root → all executors (invisible edges to set center)
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v and v.t_v == "EX":
            lines.append(f'  root_center -> {nid} [style=invis];')
    lines.append('')

    # Vertical edges (containment) — thin, light red
    for u, v, data in G.edges(data=True):
        if u in skip_ids or v in skip_ids:
            continue
        if data.get("rel") == "contains":
            lines.append(f'  {u} -> {v} [color="#E8A0A0", style=solid, '
                         f'arrowsize=0.4, penwidth=0.5];')

    lines.append('')

    # Horizontal edges (communication) — bold, colored by nature
    for u, v, data in G.edges(data=True):
        if u in skip_ids or v in skip_ids:
            continue
        if data.get("rel") != "comm":
            continue
        level = data.get("level", "")
        # Show L1 and L3 edges in radial (L2 noisy, L0 redundant)
        if level not in ("L1", "L3"):
            continue

        nature = data.get("nature", "msg")
        natures = data.get("natures", set())
        count = data.get("comm_count", 1)

        if "dep" in natures or nature == "dep":
            edge_style = 'color="#2C3E50", style=solid'
        else:
            edge_style = 'color="#1A5276", style=dashed'

        pw = min(1.0 + count * 0.3, 4.0)
        topics = data.get("topics", [])
        short_topics = [_short_topic(t) for t in topics[:2]]
        label = "\\n".join(short_topics)
        if len(topics) > 2:
            label += f"\\n+{len(topics)-2}"

        lines.append(f'  {u} -> {v} [{edge_style}, penwidth={pw:.1f}, '
                     f'label="{label}", fontsize=6, arrowsize=0.6];')

    lines.append('}')

    dot_path = out_path + ".dot"
    with open(dot_path, "w") as f:
        f.write("\n".join(lines))

    out_file = out_path + "." + fmt
    result = subprocess.run(
        ["twopi", f"-T{fmt}", dot_path, "-o", out_file],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        # Fallback to dot if twopi not available
        result = subprocess.run(
            ["dot", f"-T{fmt}", dot_path, "-o", out_file],
            capture_output=True, text=True, timeout=60
        )
    if result.returncode != 0:
        print(f"[FISH viz] radial error: {result.stderr[:200]}")
    else:
        print(f"[FISH viz] Radial: {out_file}")
    return out_file


def export_fish_matrix(G, executors, nodes, entities, out_path, fmt="png",
                       filter_boilerplate=True):
    """Export node-level adjacency matrix as a heatmap.

    Rows/columns are nodes, cells show communication topic count.
    Color intensity = number of topics between two nodes.
    Diagonal shows entity count per node.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np

    # Collect nodes in order, grouped by executor
    node_order = []
    node_labels = []
    ex_boundaries = []  # (start_idx, end_idx, ex_label) for grid lines

    for ex_id, ex in executors.items():
        ex_label = ex.A_v.get("label", "?")
        start = len(node_order)
        for n_id in ex.Z_v:
            n = nodes.get(n_id)
            if not n:
                continue
            node_order.append(n_id)
            name = n.A_v.get("label", "?")
            # Count non-boilerplate entities
            if filter_boilerplate:
                e_count = sum(1 for eid in n.Z_v
                              if eid in entities and not _is_boilerplate(entities[eid]))
            else:
                e_count = sum(1 for eid in n.Z_v if eid in entities)
            node_labels.append(f"{name}\n({e_count}E)")
        end = len(node_order)
        if end > start:
            ex_boundaries.append((start, end, ex_label))

    n = len(node_order)
    if n == 0:
        print("[FISH viz] No nodes for matrix")
        return None

    # Build adjacency matrix from L1 edges
    nid_to_idx = {nid: i for i, nid in enumerate(node_order)}
    matrix = np.zeros((n, n), dtype=float)
    topic_matrix = [[[] for _ in range(n)] for _ in range(n)]

    for u, v, data in G.edges(data=True):
        if data.get("rel") != "comm" or data.get("level") != "L1":
            continue
        i = nid_to_idx.get(u)
        j = nid_to_idx.get(v)
        if i is not None and j is not None:
            count = data.get("comm_count", 1)
            matrix[i][j] = count
            topics = data.get("topics", [])
            topic_matrix[i][j] = topics

    # Diagonal: entity count (self-info)
    for idx, nid in enumerate(node_order):
        node = nodes.get(nid)
        if node:
            if filter_boilerplate:
                e_count = sum(1 for eid in node.Z_v
                              if eid in entities and not _is_boilerplate(entities[eid]))
            else:
                e_count = sum(1 for eid in node.Z_v if eid in entities)
            matrix[idx][idx] = e_count

    # Plot
    fig, ax = plt.subplots(figsize=(max(8, n * 1.2), max(6, n * 1.0)))

    # Custom colormap: white(0) → light blue → dark blue
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("fish",
        ["#FFFFFF", "#D6EAF8", "#5DADE2", "#1A5276"])

    max_val = max(matrix.max(), 1)
    im = ax.imshow(matrix, cmap=cmap, vmin=0, vmax=max_val,
                   aspect='equal', interpolation='nearest')

    # Cell annotations
    for i in range(n):
        for j in range(n):
            val = matrix[i][j]
            if val > 0:
                if i == j:
                    # Diagonal: entity count
                    text = f"{int(val)}E"
                    color = "#2C3E50"
                else:
                    # Off-diagonal: topic count + short names
                    topics = topic_matrix[i][j]
                    shorts = [_short_topic(t) for t in topics[:2]]
                    text = "\n".join(shorts)
                    if len(topics) > 2:
                        text += f"\n+{len(topics)-2}"
                    color = "white" if val > max_val * 0.6 else "#2C3E50"
                ax.text(j, i, text, ha='center', va='center',
                        fontsize=6, color=color, fontweight='bold')

    # Axis labels
    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels(node_labels, rotation=45, ha='right', fontsize=8)
    ax.set_yticklabels(node_labels, fontsize=8)

    # Executor boundary lines
    for start, end, ex_label in ex_boundaries:
        if start > 0:
            ax.axhline(y=start - 0.5, color='#2C3E50', linewidth=1.5)
            ax.axvline(x=start - 0.5, color='#2C3E50', linewidth=1.5)
        # Label on the side
        mid = (start + end - 1) / 2
        ax.text(-0.8, mid, ex_label, ha='right', va='center',
                fontsize=8, fontweight='bold', color='#2C3E50',
                rotation=0)

    # Outer boundary for last group
    if ex_boundaries:
        last_end = ex_boundaries[-1][1]
        ax.axhline(y=last_end - 0.5, color='#2C3E50', linewidth=1.5)
        ax.axvline(x=last_end - 0.5, color='#2C3E50', linewidth=1.5)

    ax.set_xlabel("Destination Node (subscriber)", fontsize=10)
    ax.set_ylabel("Source Node (publisher)", fontsize=10)
    ax.set_title("FISH Communication Matrix — Node Level (L1)\n"
                 "Diagonal = entity count, Off-diagonal = topic count",
                 fontsize=12)

    # Colorbar
    cbar = plt.colorbar(im, ax=ax, shrink=0.6)
    cbar.set_label("Count", fontsize=9)

    # Arrow annotation
    ax.annotate("", xy=(n - 0.3, -0.8), xytext=(-0.3, -0.8),
                arrowprops=dict(arrowstyle="->", color="black", lw=1.5))
    ax.text(n / 2, -1.2, "data flow direction →", ha='center',
            fontsize=8, style='italic')

    plt.tight_layout()

    out_file = out_path + "." + fmt
    plt.savefig(out_file, dpi=200, bbox_inches='tight')
    plt.close()


def export_fish_staircase(G, out_path, fmt="png"):
    """Export staircase (merdiven) layout visualization.

    Each executor is a 'step' on a staircase arranged diagonally.
    Step order is determined by graph metrics (BFS depth, comm degree).
    Step height reflects subtree size. Horizontal edges route through
    the empty space above lower steps.

    Shows EX, N, E layers. F layer omitted for readability.
    """
    from graph_utils import compute_staircase_order
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
    import numpy as np

    staircase = compute_staircase_order(G)

    # --- Layout parameters ---
    e_spacing = 1.2       # horizontal space per entity
    layer_h = 2.5         # vertical space between layers
    e_row_h = 1.2         # vertical space between entity rows (when wrapping)
    step_gap_x = 3.0      # horizontal gap between steps
    step_descent = 3.5    # vertical descent per step
    node_gap = 1.5        # gap between node groups within a step
    max_e_per_row = 8     # wrap entities into rows if more than this
    r_ex = 0.7            # radius for EX circles
    r_n = 0.55            # radius for N circles
    r_e = 0.35            # radius for E circles

    # Colors
    c_ex = "#AED6F1"
    c_n = "#A9DFBF"
    c_e = "#F9E79F"
    c_step_bg = ["#F8F9F9", "#EAECEE", "#F8F9F9", "#EAECEE", "#F8F9F9"]
    c_edge_v = "#E74C3C"
    c_edge_h = "#2C3E50"

    # --- Build subtree structure per step ---
    steps = []
    for ex_id, info in staircase:
        node_order = info["node_order"]
        subtree = []
        for nid, nlabel, ndeg, nbfs, nrole in node_order:
            nv = G.nodes[nid]["v"]
            elist = []
            for eid in nv.Z_v:
                ev_data = G.nodes.get(eid, {})
                ev = ev_data.get("v")
                if ev and ev.t_v == "E":
                    elist.append(eid)
            subtree.append((nid, nlabel, nrole, elist))
        total_e = sum(len(el) for _, _, _, el in subtree)
        n_count = len(subtree)

        # Width: each node group uses min(entity_count, max_e_per_row) columns
        node_cols = []
        node_rows = []
        for _, _, _, elist in subtree:
            cols = min(len(elist), max_e_per_row) if elist else 1
            rows = max(1, -(-len(elist) // max_e_per_row))  # ceil division
            node_cols.append(cols)
            node_rows.append(rows)

        step_w = max(sum(c * e_spacing for c in node_cols) + (n_count - 1) * node_gap, 4.0)
        max_e_rows = max(node_rows) if node_rows else 1
        step_h = 2 * layer_h + max_e_rows * e_row_h + 1.5

        steps.append({
            "ex_id": ex_id,
            "label": info["label"],
            "info": info,
            "subtree": subtree,
            "total_e": total_e,
            "node_cols": node_cols,
            "node_rows": node_rows,
            "step_w": step_w,
            "step_h": step_h,
        })

    # --- Compute positions ---
    positions = {}   # vertex_id -> (x, y)
    step_boxes = []  # (x, y_top, w, h, label, bg_color)

    x_cursor = 0.0
    y_cursor = 0.0

    for si, step in enumerate(steps):
        ex_id = step["ex_id"]
        step_w = step["step_w"]
        step_h = step["step_h"]
        subtree = step["subtree"]
        node_cols = step["node_cols"]

        # Step bounding box
        box_x = x_cursor - 1.0
        box_y_top = y_cursor + 1.5
        box_w = step_w + 2.0
        box_h = step_h + 1.0
        bg = c_step_bg[si % len(c_step_bg)]
        step_boxes.append((box_x, box_y_top, box_w, box_h, step["label"], bg))

        # EX position: top center
        positions[ex_id] = (x_cursor + step_w / 2, y_cursor)

        # Node positions
        node_widths = [c * e_spacing for c in node_cols]
        total_nw = sum(node_widths) + max(0, len(subtree) - 1) * node_gap
        nx_start = x_cursor + (step_w - total_nw) / 2

        nx_cursor = nx_start
        for ni, (nid, nlabel, nrole, elist) in enumerate(subtree):
            nw = node_widths[ni]
            node_cx = nx_cursor + nw / 2
            positions[nid] = (node_cx, y_cursor - layer_h)

            # Entity positions: wrap into grid rows
            e_count = len(elist)
            cols = node_cols[ni]
            for ei, eid in enumerate(elist):
                col = ei % cols
                row = ei // cols
                if cols == 1:
                    e_x = node_cx
                else:
                    e_x = nx_cursor + (col + 0.5) * nw / cols
                e_y = y_cursor - 2 * layer_h - row * e_row_h
                positions[eid] = (e_x, e_y)

            nx_cursor += nw + node_gap

        x_cursor += step_w + step_gap_x
        y_cursor -= step_descent

    # --- Collect horizontal edges (L1 level, node-to-node) ---
    h_edges = []
    for u, v, data in G.edges(data=True):
        if data.get("rel") == "comm" and data.get("level") == "L1":
            if u in positions and v in positions:
                topics = data.get("topics", [])
                h_edges.append((u, v, topics))

    # --- Determine figure size ---
    all_x = [p[0] for p in positions.values()]
    all_y = [p[1] for p in positions.values()]
    x_range = max(all_x) - min(all_x) + 8
    y_range = max(all_y) - min(all_y) + 10
    fig_w = max(16, x_range * 0.8)
    fig_h = max(10, y_range * 0.8)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # --- Draw step backgrounds ---
    for box_x, box_y_top, box_w, box_h, label, bg in step_boxes:
        rect = FancyBboxPatch(
            (box_x, box_y_top - box_h), box_w, box_h,
            boxstyle="round,pad=0.3",
            facecolor=bg, edgecolor="#BDC3C7", linewidth=1.5,
            alpha=0.6, zorder=0
        )
        ax.add_patch(rect)
        ax.text(box_x + box_w / 2, box_y_top - 0.3,
                label, ha="center", va="top",
                fontsize=9, fontweight="bold", color="#566573",
                zorder=1)

    # --- Draw vertical edges (containment) ---
    for u, v, data in G.edges(data=True):
        if data.get("rel") != "contains":
            continue
        if u not in positions or v not in positions:
            continue
        uv = G.nodes[u]["v"]
        vv = G.nodes[v]["v"]
        # Only draw EX->N and N->E (skip E->F)
        if (uv.t_v == "EX" and vv.t_v == "N") or (uv.t_v == "N" and vv.t_v == "E"):
            x0, y0 = positions[u]
            x1, y1 = positions[v]
            ax.plot([x0, x1], [y0, y1],
                    color=c_edge_v, linewidth=0.8, linestyle="--",
                    alpha=0.5, zorder=2)

    # --- Draw horizontal edges routed through gap space ---
    # Edges route through the empty triangular space above lower steps.
    # For each edge, the routing channel sits just above the higher endpoint.
    edge_colors = ["#2C3E50", "#8E44AD", "#C0392B", "#2980B9", "#27AE60"]

    if h_edges:
        route_y_base = max(all_y) + 2.0
        route_spacing = 1.0

        for ei, (src, dst, topics) in enumerate(h_edges):
            x0, y0 = positions[src]
            x1, y1 = positions[dst]
            ec = edge_colors[ei % len(edge_colors)]

            # Intra-executor edge (same step, different nodes)
            if abs(x0 - x1) < 0.01 and abs(y0 - y1) < 0.01:
                # True self-loop (shouldn't happen at L1), skip
                continue

            # Check if src and dst are in the same step (intra-executor comm)
            same_step = False
            for step in steps:
                step_nids = {nid for nid, _, _, _ in step["subtree"]}
                if src in step_nids and dst in step_nids:
                    same_step = True
                    break

            if same_step:
                # Intra-executor: draw a curved arc between nodes
                ax.annotate("",
                    xy=(x1, y1 + r_n), xytext=(x0, y0 + r_n),
                    arrowprops=dict(
                        arrowstyle="-|>", color=ec,
                        connectionstyle="arc3,rad=-0.3",
                        linewidth=1.8, alpha=0.8
                    ), zorder=5)
                mid_x = (x0 + x1) / 2
                mid_y = max(y0, y1) + 1.2
                topic_str = ", ".join(_short_topic(t) for t in topics[:2])
                ax.text(mid_x, mid_y, topic_str, ha="center", va="bottom",
                        fontsize=8, fontweight="bold", color=ec,
                        fontstyle="italic", zorder=6)
            else:
                # Inter-executor: Manhattan routing through gap
                route_y = route_y_base + ei * route_spacing

                path_x = [x0, x0, x1, x1]
                path_y = [y0 + r_n, route_y, route_y, y1 + r_n]
                ax.plot(path_x, path_y,
                        color=ec, linewidth=1.8, alpha=0.7,
                        solid_capstyle="round", zorder=4)

                # Arrow at destination
                ax.annotate("",
                    xy=(x1, y1 + r_n), xytext=(x1, y1 + r_n + 0.3),
                    arrowprops=dict(arrowstyle="-|>", color=ec, lw=1.8),
                    zorder=5)

                # Topic label on the horizontal segment
                mid_x = (x0 + x1) / 2
                topic_str = ", ".join(_short_topic(t) for t in topics[:2])
                if len(topics) > 2:
                    topic_str += f" +{len(topics)-2}"
                ax.text(mid_x, route_y + 0.3, topic_str,
                        ha="center", va="bottom", fontsize=8,
                        fontweight="bold", color=ec, fontstyle="italic",
                        zorder=6)

    # --- Draw vertices ---
    for vid, (x, y) in positions.items():
        vdata = G.nodes.get(vid, {})
        v = vdata.get("v")
        if v is None:
            continue

        if v.t_v == "EX":
            circle = plt.Circle((x, y), r_ex, facecolor=c_ex,
                                edgecolor="#2980B9", linewidth=2, zorder=10)
            ax.add_patch(circle)
            label = v.A_v.get("label", "?")
            pid = v.A_v.get("pid", "?")
            ax.text(x, y + 0.05, f"{label}", ha="center", va="center",
                    fontsize=7, fontweight="bold", zorder=11)
            ax.text(x, y - 0.25, f"pid={pid}", ha="center", va="center",
                    fontsize=5, color="#566573", zorder=11)

        elif v.t_v == "N":
            circle = plt.Circle((x, y), r_n, facecolor=c_n,
                                edgecolor="#27AE60", linewidth=1.5, zorder=10)
            ax.add_patch(circle)
            name = v.A_v.get("label", "?")
            if len(name) > 18:
                name = name[:16] + ".."
            ax.text(x, y, name, ha="center", va="center",
                    fontsize=6, fontweight="bold", zorder=11)

        elif v.t_v == "E":
            circle = plt.Circle((x, y), r_e, facecolor=c_e,
                                edgecolor="#F39C12", linewidth=1, zorder=10)
            ax.add_patch(circle)
            etype = v.A_v.get("etype", "?")
            label = _short_topic(v.A_v.get("label", "?"))
            if len(label) > 12:
                label = label[:10] + ".."
            ax.text(x, y + 0.08, etype.upper(), ha="center", va="center",
                    fontsize=5, fontweight="bold", zorder=11)
            ax.text(x, y - 0.12, label, ha="center", va="center",
                    fontsize=4, color="#7B7D7D", zorder=11)

    # --- Title and legend ---
    ax.set_title("FISH Graph — Staircase Layout (Merdiven)\n"
                 "Step order: BFS depth → comm degree | Edge routing through gaps",
                 fontsize=13, fontweight="bold", pad=20)
    ax.set_aspect("equal")
    ax.axis("off")

    # Legend
    legend_items = [
        mpatches.Patch(facecolor=c_ex, edgecolor="#2980B9", label="Executor (L0)"),
        mpatches.Patch(facecolor=c_n, edgecolor="#27AE60", label="Node (L1)"),
        mpatches.Patch(facecolor=c_e, edgecolor="#F39C12", label="Entity (L2)"),
        plt.Line2D([0], [0], color=c_edge_v, linestyle="--", linewidth=1, label="Containment"),
        plt.Line2D([0], [0], color=c_edge_h, linewidth=1.5, label="Communication"),
    ]
    ax.legend(handles=legend_items, loc="lower right", fontsize=8,
              framealpha=0.8, edgecolor="#BDC3C7")

    # Margins
    margin = 3
    route_top = max(all_y) + 2.0 + len(h_edges) * 1.0 + 3
    ax.set_xlim(min(all_x) - margin, max(all_x) + margin)
    ax.set_ylim(min(all_y) - margin, route_top)

    plt.tight_layout()
    out_file = f"{out_path}.{fmt}"
    plt.savefig(out_file, dpi=200, bbox_inches="tight")
    plt.close()
    print(f"[FISH viz] Staircase: {out_file}")
    return out_file
    print(f"[FISH viz] Matrix: {out_file}")
    return out_file


def export_fish_l3_chains(G, out_path, fmt="png"):
    """Export L3 function-level chain graph — only functions and their edges.

    Shows the callback-to-callback dataflow chains. Each node is a function
    vertex (callback), each edge is a propagated L3 communication or
    dependency edge. External functions shown in gray dashed.
    """
    import subprocess

    lines = [
        'digraph FISH_L3 {',
        '  rankdir=LR;',
        '  splines=true;',
        '  nodesep=0.6;',
        '  ranksep=1.0;',
        '  bgcolor=white;',
        '  label="FISH L3 — Function Chain Graph";',
        '  labelloc=t;',
        '  fontsize=14;',
        '',
    ]

    # Collect all function vertices involved in L3 edges
    l3_nodes = set()
    l3_edges = []
    for u, v, data in G.edges(data=True):
        if data.get("level") == "L3" and data.get("rel") == "comm":
            l3_nodes.add(u)
            l3_nodes.add(v)
            l3_edges.append((u, v, data))

    if not l3_nodes:
        print("[FISH viz] L3 chains: no L3 edges, skipping")
        return None

    # Also find parent entity for labeling
    entity_of_func = {}  # f_id → entity label
    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if v and v.t_v == "E":
            for f_id in v.Z_v:
                entity_of_func[f_id] = v.A_v.get("label", "?")

    # Add function nodes
    for nid in l3_nodes:
        data = G.nodes.get(nid, {})
        v = data.get("v")
        if not v:
            continue

        is_ext = v.A_v.get("external", False) or v.A_v.get("ptype") == "ext"
        sym = v.A_v.get("label", str(nid))
        ptype = v.A_v.get("ptype", "?")
        entity_label = entity_of_func.get(nid, "")

        # Shorten symbol
        short_sym = shorten_symbol(sym) if not is_ext else sym.replace("ext:", "")
        if "/" in short_sym:
            short_sym = _short_topic(short_sym)
        if len(short_sym) > 25:
            short_sym = short_sym[:23] + ".."

        # Shorten entity label
        short_entity = _short_topic(entity_label) if "/" in entity_label else entity_label
        if short_entity.startswith("ext:"):
            short_entity = short_entity.replace("ext:", "")

        label = f"{_dot_escape(short_sym)}\\n[{_dot_escape(short_entity)}]"

        if is_ext:
            style = ('shape=box, style="rounded,filled,dashed", fillcolor="#D5D8DC", '
                     'fontsize=8, width=1.5, height=0.5')
        else:
            style = ('shape=box, style="rounded,filled", fillcolor="#FFFFCC", '
                     'fontsize=8, width=1.5, height=0.5')

        lines.append(f'  {nid} [label="{label}", {style}];')

    lines.append('')

    # Add L3 edges
    for u, v, data in l3_edges:
        nature = data.get("nature", "msg")
        topic = data.get("topic", data.get("service", ""))
        short = _short_topic(topic) if topic else ""
        ext = data.get("external", False)

        if nature == "dep":
            edge_style = 'color="#E74C3C", style=solid, arrowsize=0.8'
        elif ext:
            edge_style = 'color="#7F8C8D", style=dashed, arrowsize=0.8'
        else:
            edge_style = 'color="#2C3E50", style=solid, arrowsize=0.8'

        if short:
            edge_style += f', label="{_dot_escape(short)}", fontsize=7'

        lines.append(f'  {u} -> {v} [{edge_style}];')

    lines.append('}')

    dot_path = out_path + ".dot"
    with open(dot_path, "w") as f:
        f.write("\n".join(lines))

    out_file = out_path + "." + fmt
    result = subprocess.run(
        ["dot", f"-T{fmt}", dot_path, "-o", out_file],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[FISH viz] dot error: {result.stderr[:200]}")

    print(f"[FISH viz] L3 chains: {out_file}")
    return out_file


def export_fish_json(G, out_path="fish_graph.json"):
    """Export FISH graph as JSON for D3 interactive visualization.

    Each node carries: id, type, label, level, children, and type-specific fields.
    Each edge carries: source, target, rel, level, nature, and comm-specific fields.
    """
    import json

    graph_json = {"nodes": [], "edges": []}

    for nid, data in G.nodes(data=True):
        v = data.get("v")
        if not v:
            continue
        node = {
            "id": v.id_v,
            "type": v.t_v,
            "label": v.A_v.get("label", str(v.id_v)),
            "level": v.level,
            "children": list(v.Z_v) if v.Z_v else [],
        }
        if v.t_v == "EX":
            node["pid"] = v.A_v.get("pid")
        elif v.t_v == "N":
            node["full_name"] = v.A_v.get("full_name", "")
        elif v.t_v == "E":
            node["etype"] = v.A_v.get("etype")
            node["external"] = v.A_v.get("external", False)
            node["aspects"] = v.A_v.get("aspects", [])
        elif v.t_v == "F":
            node["ptype"] = v.A_v.get("ptype")
            node["external"] = v.A_v.get("ptype") == "ext"
            node["cb_addr"] = v.A_v.get("cb_addr", "")
        graph_json["nodes"].append(node)

    for u, v, data in G.edges(data=True):
        edge = {
            "source": u,
            "target": v,
            "rel": data.get("rel"),
            "level": data.get("level", ""),
            "nature": data.get("nature", ""),
        }
        for k in ["topic", "service", "msg_type", "avg_rate_hz", "direction", "tau"]:
            if data.get(k):
                edge[k] = data[k]
        if data.get("external"):
            edge["external"] = True
        if data.get("comm_count"):
            edge["comm_count"] = data["comm_count"]
        graph_json["edges"].append(edge)

    with open(out_path, "w") as f:
        json.dump(graph_json, f, indent=2)

    print(f"[FISH viz] JSON: {len(graph_json['nodes'])} nodes, "
          f"{len(graph_json['edges'])} edges → {out_path}")
    return out_path
