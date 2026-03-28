"""
graph_utils.py — Graph-theoretic analysis of the FISH DiGraph.

Computes structural metrics on the FISH graph to inform layout decisions,
particularly for the "staircase" (merdiven) visualization approach.

Metrics computed:
  - Degree centrality (in/out/total) at each level
  - Betweenness centrality on the communication subgraph
  - BFS/DFS orderings from source nodes (dataflow direction)
  - Subtree sizes per executor (for staircase step heights)
  - Communication density between executor pairs
"""

import networkx as nx
from collections import defaultdict


# ---------------------------------------------------------------------------
# Subgraph extraction helpers
# ---------------------------------------------------------------------------

def _comm_subgraph(G, level):
    """Extract a communication-only subgraph at a given level (L0, L1, L2).

    Returns a new DiGraph with only the comm edges at the specified level,
    preserving node attributes.
    """
    sub = nx.DiGraph()
    for u, v, data in G.edges(data=True):
        if data.get("rel") == "comm" and data.get("level") == level:
            if u not in sub:
                sub.add_node(u, **G.nodes[u])
            if v not in sub:
                sub.add_node(v, **G.nodes[v])
            sub.add_edge(u, v, **data)
    return sub


def _containment_tree(G):
    """Extract the containment-only subgraph (vertical edges).

    Returns a new DiGraph with only the contains edges.
    """
    tree = nx.DiGraph()
    for u, v, data in G.edges(data=True):
        if data.get("rel") == "contains":
            if u not in tree:
                tree.add_node(u, **G.nodes[u])
            if v not in tree:
                tree.add_node(v, **G.nodes[v])
            tree.add_edge(u, v, **data)
    return tree


# ---------------------------------------------------------------------------
# Vertex-level helpers
# ---------------------------------------------------------------------------

def _vertex_type(G, node_id):
    """Return the vertex type (EX, N, E, F) for a node in the graph."""
    v = G.nodes[node_id].get("v")
    return v.t_v if v else None


def _vertex_label(G, node_id):
    """Return a human-readable label for a node."""
    v = G.nodes[node_id].get("v")
    if v is None:
        return str(node_id)
    if v.t_v == "EX":
        return v.A_v.get("label", str(v.id_v))
    elif v.t_v == "N":
        return v.A_v.get("full_name", v.A_v.get("label", str(v.id_v)))
    elif v.t_v == "E":
        return v.A_v.get("label", str(v.id_v))
    elif v.t_v == "F":
        sym = v.A_v.get("symbol", "")
        if "::" in sym:
            return sym.split("::")[-1][:30]
        return sym[:30] if sym else str(v.id_v)
    return str(v.id_v)


# ---------------------------------------------------------------------------
# 1. Degree analysis
# ---------------------------------------------------------------------------

def compute_degree_metrics(G):
    """Compute communication degree (in/out/total) for each node at L1 level.

    Returns dict: {node_id: {"label": str, "in": int, "out": int, "total": int}}
    """
    comm_l1 = _comm_subgraph(G, "L1")
    results = {}
    # Include all N-level nodes, even those with zero comm edges
    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v is None or v.t_v != "N":
            continue
        in_deg = comm_l1.in_degree(nid) if nid in comm_l1 else 0
        out_deg = comm_l1.out_degree(nid) if nid in comm_l1 else 0
        results[nid] = {
            "label": _vertex_label(G, nid),
            "in": in_deg,
            "out": out_deg,
            "total": in_deg + out_deg,
        }
    return results


def compute_executor_degree(G):
    """Compute communication degree (in/out/total) for each executor at L0 level.

    Returns dict: {ex_id: {"label": str, "in": int, "out": int, "total": int,
                            "inter": int, "intra": int}}
    """
    comm_l0 = _comm_subgraph(G, "L0")
    results = {}
    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v is None or v.t_v != "EX":
            continue
        in_deg = comm_l0.in_degree(nid) if nid in comm_l0 else 0
        out_deg = comm_l0.out_degree(nid) if nid in comm_l0 else 0

        # Count inter vs intra process edges
        inter = 0
        intra = 0
        if nid in comm_l0:
            for _, _, d in comm_l0.out_edges(nid, data=True):
                tau = d.get("tau", "")
                if tau == "InterP":
                    inter += 1
                elif tau == "IntraP":
                    intra += 1
            for _, _, d in comm_l0.in_edges(nid, data=True):
                tau = d.get("tau", "")
                if tau == "InterP":
                    inter += 1
                elif tau == "IntraP":
                    intra += 1

        results[nid] = {
            "label": _vertex_label(G, nid),
            "in": in_deg,
            "out": out_deg,
            "total": in_deg + out_deg,
            "inter": inter,
            "intra": intra,
        }
    return results


# ---------------------------------------------------------------------------
# 2. Betweenness centrality
# ---------------------------------------------------------------------------

def compute_betweenness(G, level="L1"):
    """Compute betweenness centrality on the communication subgraph.

    Betweenness measures how often a node lies on shortest paths between
    other nodes. High-betweenness nodes are "bridges" in the dataflow.

    Args:
        G: FISH DiGraph
        level: "L0" for executor-level, "L1" for node-level

    Returns dict: {node_id: {"label": str, "betweenness": float}}
    """
    comm = _comm_subgraph(G, level)
    if comm.number_of_nodes() == 0:
        return {}

    bc = nx.betweenness_centrality(comm, normalized=True)
    results = {}
    for nid, val in bc.items():
        results[nid] = {
            "label": _vertex_label(G, nid),
            "betweenness": round(val, 4),
        }
    return results


# ---------------------------------------------------------------------------
# 3. BFS / DFS ordering
# ---------------------------------------------------------------------------

def _find_sources(G, level):
    """Find source nodes (no incoming comm edges) at a given level.

    These are typically sensor/input nodes in the dataflow.
    """
    comm = _comm_subgraph(G, level)
    sources = [n for n in comm.nodes() if comm.in_degree(n) == 0]
    return sources


def _find_sinks(G, level):
    """Find sink nodes (no outgoing comm edges) at a given level.

    These are typically actuator/output nodes in the dataflow.
    """
    comm = _comm_subgraph(G, level)
    sinks = [n for n in comm.nodes() if comm.out_degree(n) == 0]
    return sinks


def compute_bfs_order(G, level="L1"):
    """BFS traversal from source nodes on the communication subgraph.

    Returns a list of (node_id, depth) tuples in BFS order.
    Nodes not reachable from any source get depth = -1.
    """
    comm = _comm_subgraph(G, level)
    sources = _find_sources(G, level)

    visited = {}
    for src in sources:
        for node, depth in nx.bfs_edges(comm, src):
            if node not in visited:
                visited[node] = depth if isinstance(depth, int) else 0
        if src not in visited:
            visited[src] = 0

    # BFS with explicit depth tracking
    visited = {}
    queue = []
    for src in sources:
        if src not in visited:
            visited[src] = 0
            queue.append((src, 0))

    while queue:
        current, depth = queue.pop(0)
        for succ in comm.successors(current):
            if succ not in visited:
                visited[succ] = depth + 1
                queue.append((succ, depth + 1))

    # Add unreachable nodes
    all_nodes = set()
    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v and v.t_v == ("EX" if level == "L0" else "N"):
            all_nodes.add(nid)

    result = []
    for nid in sorted(visited, key=visited.get):
        result.append((nid, visited[nid], _vertex_label(G, nid)))
    for nid in all_nodes - set(visited):
        result.append((nid, -1, _vertex_label(G, nid)))

    return result


def compute_dfs_order(G, level="L1"):
    """DFS traversal from source nodes on the communication subgraph.

    Returns list of (node_id, discovery_time, finish_time, label).
    """
    comm = _comm_subgraph(G, level)
    sources = _find_sources(G, level)

    visited = set()
    order = []
    time = [0]

    def _dfs(node):
        visited.add(node)
        d_time = time[0]
        time[0] += 1
        for succ in comm.successors(node):
            if succ not in visited:
                _dfs(succ)
        f_time = time[0]
        time[0] += 1
        order.append((node, d_time, f_time, _vertex_label(G, node)))

    for src in sources:
        if src not in visited:
            _dfs(src)

    # Nodes not reachable from sources
    all_nodes = set()
    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v and v.t_v == ("EX" if level == "L0" else "N"):
            all_nodes.add(nid)
    for nid in all_nodes - visited:
        order.append((nid, -1, -1, _vertex_label(G, nid)))

    return order


# ---------------------------------------------------------------------------
# 4. Subtree analysis (for staircase step sizing)
# ---------------------------------------------------------------------------

def compute_subtree_sizes(G):
    """Compute subtree size for each executor.

    Returns dict: {ex_id: {"label": str, "nodes": int, "entities": int,
                            "functions": int, "total": int,
                            "depth": int, "max_width": int}}
    """
    tree = _containment_tree(G)
    results = {}

    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v is None or v.t_v != "EX":
            continue

        # Count descendants by type
        n_count = 0
        e_count = 0
        f_count = 0

        descendants = nx.descendants(tree, nid) if nid in tree else set()
        for d in descendants:
            dv = G.nodes[d].get("v")
            if dv is None:
                continue
            if dv.t_v == "N":
                n_count += 1
            elif dv.t_v == "E":
                e_count += 1
            elif dv.t_v == "F":
                f_count += 1

        # Width per layer (for layout sizing)
        widths = {"N": n_count, "E": e_count, "F": f_count}
        max_width = max(widths.values()) if widths else 0

        # Depth: always 3 if functions exist, 2 if entities, 1 if nodes
        depth = 0
        if f_count > 0:
            depth = 3
        elif e_count > 0:
            depth = 2
        elif n_count > 0:
            depth = 1

        results[nid] = {
            "label": _vertex_label(G, nid),
            "nodes": n_count,
            "entities": e_count,
            "functions": f_count,
            "total": 1 + n_count + e_count + f_count,  # including the EX itself
            "depth": depth,
            "max_width": max_width,
        }

    return results


# ---------------------------------------------------------------------------
# 5. Communication density matrix (executor-to-executor)
# ---------------------------------------------------------------------------

def compute_comm_density(G):
    """Compute communication density between executor pairs.

    Returns:
        matrix: dict of {(src_ex_id, dst_ex_id): {"topics": [...], "count": int}}
        ex_labels: dict of {ex_id: label}
    """
    # Build node → executor mapping
    node_to_ex = {}
    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v is None or v.t_v != "EX":
            continue
        for child in v.Z_v:
            node_to_ex[child] = nid

    matrix = {}
    ex_labels = {}

    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v and v.t_v == "EX":
            ex_labels[nid] = _vertex_label(G, nid)

    for u, v, data in G.edges(data=True):
        if data.get("rel") != "comm" or data.get("level") != "L1":
            continue
        src_ex = node_to_ex.get(u)
        dst_ex = node_to_ex.get(v)
        if src_ex is None or dst_ex is None:
            continue
        key = (src_ex, dst_ex)
        if key not in matrix:
            matrix[key] = {"topics": [], "count": 0}
        topics = data.get("topics", [])
        matrix[key]["topics"].extend(topics)
        matrix[key]["count"] += data.get("comm_count", 1)

    return matrix, ex_labels


# ---------------------------------------------------------------------------
# 6. Dataflow classification
# ---------------------------------------------------------------------------

def classify_dataflow_role(G):
    """Classify each node's dataflow role: source, sink, relay, or isolated.

    - source: publishes but doesn't subscribe (to comm topics)
    - sink: subscribes but doesn't publish
    - relay: both publishes and subscribes
    - isolated: no comm edges

    Returns dict: {node_id: {"label": str, "role": str,
                              "pub_topics": [...], "sub_topics": [...]}}
    """
    comm = _comm_subgraph(G, "L1")
    results = {}

    for nid, ndata in G.nodes(data=True):
        v = ndata.get("v")
        if v is None or v.t_v != "N":
            continue

        out_topics = []
        in_topics = []
        if nid in comm:
            for _, _, d in comm.out_edges(nid, data=True):
                out_topics.extend(d.get("topics", []))
            for _, _, d in comm.in_edges(nid, data=True):
                in_topics.extend(d.get("topics", []))

        has_out = len(out_topics) > 0
        has_in = len(in_topics) > 0

        if has_out and has_in:
            role = "relay"
        elif has_out:
            role = "source"
        elif has_in:
            role = "sink"
        else:
            role = "isolated"

        results[nid] = {
            "label": _vertex_label(G, nid),
            "role": role,
            "pub_topics": sorted(set(out_topics)),
            "sub_topics": sorted(set(in_topics)),
        }

    return results


# ---------------------------------------------------------------------------
# 7. Staircase ordering (combines metrics for layout)
# ---------------------------------------------------------------------------

def compute_staircase_order(G):
    """Compute the optimal executor ordering for staircase layout.

    Strategy:
      1. BFS depth from sources → dataflow direction (sources first)
      2. Tiebreak by communication degree (busier executors higher)
      3. Within each executor, order nodes by their comm degree

    Returns:
        ex_order: [(ex_id, {"label", "rank", "bfs_depth", "degree",
                             "subtree_size", "node_order": [(nid, label, degree)]})]
    """
    # BFS at L0 (executor level)
    bfs_l0 = compute_bfs_order(G, level="L0")
    bfs_depth_l0 = {nid: depth for nid, depth, _ in bfs_l0}

    # Degree at L0
    ex_degree = compute_executor_degree(G)

    # Subtree sizes
    subtrees = compute_subtree_sizes(G)

    # Node degrees at L1
    node_degree = compute_degree_metrics(G)

    # BFS at L1
    bfs_l1 = compute_bfs_order(G, level="L1")
    bfs_depth_l1 = {nid: depth for nid, depth, _ in bfs_l1}

    # Dataflow roles
    roles = classify_dataflow_role(G)

    # Build executor ordering
    ex_list = []
    for ex_id in subtrees:
        depth = bfs_depth_l0.get(ex_id, 999)
        degree = ex_degree.get(ex_id, {}).get("total", 0)
        label = subtrees[ex_id]["label"]
        ex_list.append((ex_id, depth, degree, label))

    # Sort: BFS depth ascending, then degree descending (busiest first among same depth)
    ex_list.sort(key=lambda x: (x[1] if x[1] >= 0 else 999, -x[2]))

    # Build node ordering within each executor
    tree = _containment_tree(G)
    result = []
    for rank, (ex_id, bfs_d, deg, label) in enumerate(ex_list):
        # Get nodes under this executor
        child_nodes = []
        if ex_id in tree:
            for child in tree.successors(ex_id):
                cv = G.nodes[child].get("v")
                if cv and cv.t_v == "N":
                    nd = node_degree.get(child, {})
                    role = roles.get(child, {}).get("role", "isolated")
                    child_nodes.append((
                        child,
                        _vertex_label(G, child),
                        nd.get("total", 0),
                        bfs_depth_l1.get(child, -1),
                        role,
                    ))

        # Sort nodes: sources first, then relays, then sinks, then isolated
        role_order = {"source": 0, "relay": 1, "sink": 2, "isolated": 3}
        child_nodes.sort(key=lambda x: (role_order.get(x[4], 9), -x[2]))

        result.append((ex_id, {
            "label": label,
            "rank": rank,
            "bfs_depth": bfs_d,
            "degree": deg,
            "subtree": subtrees.get(ex_id, {}),
            "node_order": child_nodes,
        }))

    return result


# ---------------------------------------------------------------------------
# Pretty-print all metrics
# ---------------------------------------------------------------------------

def print_graph_metrics(G):
    """Compute and print all graph metrics for the FISH DiGraph."""

    print("\n" + "=" * 70)
    print("FISH Graph Metrics")
    print("=" * 70)

    # --- Degree metrics (node level) ---
    print("\n--- Node Degree (L1 communication) ---")
    deg = compute_degree_metrics(G)
    sorted_deg = sorted(deg.items(), key=lambda x: -x[1]["total"])
    print(f"  {'Node':<35s} {'In':>4s} {'Out':>4s} {'Total':>5s}")
    print(f"  {'-'*35} {'-'*4} {'-'*4} {'-'*5}")
    for nid, d in sorted_deg:
        print(f"  {d['label']:<35s} {d['in']:>4d} {d['out']:>4d} {d['total']:>5d}")

    # --- Degree metrics (executor level) ---
    print("\n--- Executor Degree (L0 communication) ---")
    ex_deg = compute_executor_degree(G)
    sorted_ex = sorted(ex_deg.items(), key=lambda x: -x[1]["total"])
    print(f"  {'Executor':<25s} {'In':>4s} {'Out':>4s} {'Tot':>4s} {'IntP':>5s} {'IntaP':>5s}")
    print(f"  {'-'*25} {'-'*4} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")
    for nid, d in sorted_ex:
        print(f"  {d['label']:<25s} {d['in']:>4d} {d['out']:>4d} {d['total']:>4d} "
              f"{d['inter']:>5d} {d['intra']:>5d}")

    # --- Betweenness centrality ---
    print("\n--- Betweenness Centrality (L1) ---")
    bc = compute_betweenness(G, "L1")
    sorted_bc = sorted(bc.items(), key=lambda x: -x[1]["betweenness"])
    for nid, d in sorted_bc:
        bar = "#" * int(d["betweenness"] * 50)
        print(f"  {d['label']:<35s}  {d['betweenness']:.4f}  {bar}")

    # --- Dataflow roles ---
    print("\n--- Dataflow Roles (L1) ---")
    roles = classify_dataflow_role(G)
    for role_type in ["source", "relay", "sink", "isolated"]:
        nodes_in_role = [(nid, d) for nid, d in roles.items() if d["role"] == role_type]
        if nodes_in_role:
            print(f"  {role_type.upper()}:")
            for nid, d in nodes_in_role:
                pub_str = ", ".join(d["pub_topics"][:3]) or "-"
                sub_str = ", ".join(d["sub_topics"][:3]) or "-"
                print(f"    {d['label']:<35s}  pub: {pub_str}")
                if d["sub_topics"]:
                    print(f"    {'':35s}  sub: {sub_str}")

    # --- BFS order ---
    print("\n--- BFS Order from Sources (L1) ---")
    bfs = compute_bfs_order(G, level="L1")
    for nid, depth, label in bfs:
        indent = "  " * max(0, depth)
        d_str = f"depth={depth}" if depth >= 0 else "unreachable"
        print(f"  {indent}{label:<30s}  ({d_str})")

    # --- Subtree sizes ---
    print("\n--- Executor Subtree Sizes ---")
    st = compute_subtree_sizes(G)
    sorted_st = sorted(st.items(), key=lambda x: -x[1]["total"])
    print(f"  {'Executor':<25s} {'N':>3s} {'E':>4s} {'F':>4s} {'Tot':>5s} {'MaxW':>5s}")
    print(f"  {'-'*25} {'-'*3} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")
    for nid, d in sorted_st:
        print(f"  {d['label']:<25s} {d['nodes']:>3d} {d['entities']:>4d} "
              f"{d['functions']:>4d} {d['total']:>5d} {d['max_width']:>5d}")

    # --- Communication density ---
    print("\n--- Executor Communication Density ---")
    matrix, labels = compute_comm_density(G)
    if matrix:
        for (src, dst), info in sorted(matrix.items(), key=lambda x: -x[1]["count"]):
            src_l = labels.get(src, str(src))
            dst_l = labels.get(dst, str(dst))
            topics_short = [t.split("/")[-1] for t in info["topics"][:5]]
            print(f"  {src_l:<20s} → {dst_l:<20s}  "
                  f"count={info['count']:>2d}  topics: {', '.join(topics_short)}")
    else:
        print("  (no executor-level comm edges)")

    # --- Staircase ordering ---
    print("\n--- Staircase Layout Order ---")
    staircase = compute_staircase_order(G)
    for ex_id, info in staircase:
        st_info = info["subtree"]
        print(f"\n  Step {info['rank']}: {info['label']:<25s}  "
              f"bfs_depth={info['bfs_depth']}  degree={info['degree']}  "
              f"subtree_size={st_info.get('total', '?')}")
        for nid, nlabel, ndeg, nbfs, nrole in info["node_order"]:
            print(f"    [{nrole:>8s}]  {nlabel:<35s}  deg={ndeg}  bfs={nbfs}")

    print("\n" + "=" * 70)
    return {
        "degree": deg,
        "executor_degree": ex_deg,
        "betweenness": bc,
        "roles": roles,
        "bfs": bfs,
        "subtrees": st,
        "comm_density": (matrix, labels),
        "staircase": staircase,
    }
