#!/usr/bin/env python3
"""per-bench-routine-1 sheet payload builder.

Reads a benchmark's pbr1_summary.json and emits a JSON payload structured for
the google-sheets MCP. The actual MCP calls happen from the calling Claude
session — this script just builds the cell arrays so the session can write
them out tab-by-tab without re-doing the analysis.

Layout (per benchmark = one spreadsheet named "<BENCHNAME>-A1"):
  REFERENCE          — VCC + VCCI summary catalog (shared)
  bench_node         — image/script/dataset/datapoints metadata
  per_node_<name>    — one tab per Node (filled with FISH-graph-derived facts;
                       file/line/exact_code/vcc_code columns left as TBD)
  expected_vertex    — formula prediction (placeholders by node)
  actual_vertex      — from fish_graph.json, ground truth
  perf_overhead      — fishoff vs fishon perf table

Usage:
  python3 pbr1_sheet_builder.py <benchmark_name> > /tmp/pbr1_payload_<name>.json
"""
import sys, os, json, glob

DEST_BASE = '/tmp/per_bench_routine_1'

# ────────────────────────── REFERENCE tab content ──────────────────────────
REFERENCE_ROWS = [
    ['Code', 'Layer / Class', 'Call', 'FISH contribution per call (E/F + aspects)', 'State', 'Notes'],
    ['VCC1', 'rclcpp::Node ctor', 'Node()', '+1 N + 7 E + 7 F + 2 pub_aspect', 'verified', 'Includes /parameter_events sub via TimeSource, 6 boilerplate param services, 1 /rosout pub'],
    ['VCC1.rclpy', 'rclpy.node.Node ctor', 'Node()', '+1 N + 6 E + 6 F + 2 pub_aspect', 'verified', 'No TimeSource auto-attach in rclpy → 1 less /parameter_events sub'],
    ['VCC1.cm', 'rclcpp_components::ComponentManager', 'ComponentManager()', '+1 N + 1 E + 1 F + 1 pub_aspect', 'verified', 'Internal NodeOptions override turns off param services and parameter_events pub'],
    ['VCC2', 'rclcpp::Node', 'create_subscription<T>(topic, qos, cb)', '+1 E + 1 F', 'verified', 'Stock rclcpp Subscription<T> ctor'],
    ['VCC3', 'rclcpp::Node', 'create_publisher<T>(topic, qos)', '+1 pub_aspect (on caller N)', 'verified', 'No E vertex — pub is an aspect on the node, not a callback-binder'],
    ['VCC4', 'rclcpp::Node', 'create_service<T>(name, cb)', '+1 E + 1 F', 'verified', ''],
    ['VCC5', 'rclcpp::Node', 'create_client<T>(name)', '+1 cli_aspect (on caller N)', 'verified', 'No E — symmetric to VCC3 for the request/response API'],
    ['VCC6', 'rclcpp::Node', 'create_wall_timer(period, cb)', '+1 E + 1 F', 'verified', 'Atomic fish_rclcpp_timer_init carries period_ns'],
    ['VCC_GS', 'rclcpp::Node', 'create_generic_subscription(topic, type, qos, cb)', '+1 E + 1 F', 'verified', 'Post-2026-06-08 patch: GenericSubscription ctor now fires rclcpp_subscription_init + rclcpp_subscription_callback_added — see notes/high_impact/generic_subscription_attribution_gap.txt'],
    ['VCCI1', 'NitrosNode (inheritance)', ': public nitros::NitrosNode', 'no extra vertex; triggers VCC1 once', 'verified', ''],
    ['VCCI2', 'NitrosNode::startNitrosNode()', 'create_wall_timer(negotiation_timer, cb)', '+1 E + 1 F', 'verified', ''],
    ['VCCI3', 'NitrosPublisherSubscriberGroup ctor', 'std::make_shared<NPSG>(...)', 'no extra vertex; triggers VCCI4 + VCCI5', 'verified', ''],
    ['VCCI4', 'NPSG::createNitrosSubscribers()', 'per input CONFIG_MAP entry → 1× VCCI6', 'no extra', 'verified', ''],
    ['VCCI5', 'NPSG::createNitrosPublishers()', 'per output CONFIG_MAP entry → 1× VCCI13', 'no extra', 'verified', ''],
    ['VCCI6', 'NitrosSubscriber ctor', 'std::make_shared<NitrosSubscriber>', 'triggers VCCI8 + VCCI9 + VCCI10×N', 'verified', ''],
    ['VCCI8', 'NitrosSubscriber::createCompatibleSubscriber', 'type_manager.createCompatibleSubscriberCallback', '+1 E + 1 F (compat sub on config_.topic_name)', 'verified', 'When peer is non-NITROS-aware, MonitorNode side may also reach this via create_generic_subscription (VCC_GS)'],
    ['VCCI9', 'NitrosSubscriber NEGOTIATED branch', 'std::make_shared<NegotiatedSubscription>', '+2 E + 1 F (sub on <t>/nitros, pub on <t>/nitros/_supported_types)', 'verified', ''],
    ['VCCI10', 'NitrosSubscriber::addSupportedDataFormat (N-1 per format)', 'addSubscriberSupportedFormatCallback', '+1 E + 1 F per non-compat format', 'verified', ''],
    ['VCCI13', 'NitrosPublisher ctor', 'std::make_shared<NitrosPublisher>', 'triggers VCCI14 + VCCI15', 'verified', ''],
    ['VCCI14', 'NitrosPublisher::createCompatiblePublisher', 'type_manager.createCompatiblePublisherCallback', '+1 E (compat pub on config_.topic_name)', 'verified', ''],
    ['VCCI15', 'NitrosPublisher NEGOTIATED branch + start()', 'std::make_shared<NegotiatedPublisher>', '+3 E + 2 F (pub <t>/nitros, graph_change_timer, supported_types sub)', 'verified', ''],
    ['VCCI17', 'NitrosMessageFilterSubscriber<T>.subscribe(...)', 'std::make_shared<ManagedNitrosSubscriber<T>>', 'no extra; triggers VCCI18', 'verified', ''],
    ['VCCI18', 'ManagedNitrosSubscriber ctor', 'wraps NitrosSubscriber (= VCCI6 chain)', 'same as VCCI6', 'verified', ''],
    ['VCC_GHB', 'NitrosNode::postNegotiationCallback (runtime)', 'create_wall_timer(gxf_heartbeat_timer)', '+1 E + 1 F (runtime, after negotiation)', 'verified', 'Not part of static ctor model'],
]

# ───────────────────────── helpers ─────────────────────────
def load_summary(name):
    path = os.path.join(DEST_BASE, name, 'pbr1_summary.json')
    if not os.path.isfile(path): return None
    with open(path) as f:
        return json.load(f)

def perf_table(summary):
    rows = [['Metric', 'FISH off', 'FISH on', 'Δ (on - off)', '% (Δ/off)']]
    overhead = summary.get('overhead') or []
    for row in overhead:
        rows.append([
            row['metric'],
            row['fishoff'],
            row['fishon'],
            row['delta'],
            f"{row['pct']:.2f}%" if row['pct'] is not None else 'n/a',
        ])
    if len(rows) == 1:
        rows.append(['(no perf data — both passes lacked r2b-log JSON)', '', '', '', ''])
    return rows

def actual_vertex_table(summary):
    rows = [['Node full_name', 'E count', 'F count', 'sub topics', 'pub aspect topics', 'srv', 'cli aspect', 'timer periods (ns)']]
    gs = summary.get('graph_summary') or {}
    nodes = gs.get('nodes') or {}
    if not nodes:
        rows.append(['(no graph data — fishon pass produced no fish_graph.json)', '', '', '', '', '', '', ''])
        return rows
    by_type = gs.get('by_type') or {}
    rows.append(['(totals)',
                 by_type.get('E', 0),
                 by_type.get('F', 0),
                 f"{len(nodes)} nodes",
                 '',
                 '',
                 '',
                 ''])
    for name, info in sorted(nodes.items()):
        rows.append([
            name, info.get('E', 0), info.get('F', 0),
            ', '.join(map(str, info.get('sub_topics', []))),
            ', '.join(map(str, info.get('pub_topics', []))),
            ', '.join(map(str, info.get('srv_names', []))),
            ', '.join(map(str, info.get('cli_names', []))),
            ', '.join(map(str, info.get('tmr_periods', []))),
        ])
    return rows

def expected_vertex_table(summary):
    rows = [['Node full_name (from actual)', 'expected E (formula TBD)', 'expected F (formula TBD)', 'VCC composition (TBD per node)', 'delta vs actual']]
    nodes = (summary.get('graph_summary') or {}).get('nodes') or {}
    if not nodes:
        rows.append(['(populate after per-node source review)', '', '', '', ''])
        return rows
    for n, info in sorted(nodes.items()):
        rows.append([n, 'TBD', 'TBD', 'TBD: walk ctor + ancestors, list VCC#/VCCI# codes', f"E_actual={info.get('E',0)}, F_actual={info.get('F',0)}"])
    return rows

def bench_node_table(name, summary):
    rows = [['Field', 'Value']]
    rows.append(['Benchmark', name])
    rows.append(['Image', f"fish-r2b-{name.replace('isaac_ros_', '')}:latest"])
    rows.append(['fishoff perf JSON', summary.get('fishoff_perf_path') or '—'])
    rows.append(['fishon perf JSON', summary.get('fishon_perf_path') or '—'])
    rows.append(['fishon LTTng session', summary.get('fishon_session') or '—'])
    rows.append(['fish_graph.json', summary.get('fish_graph_path') or '—'])
    if summary.get('error'):
        rows.append(['ERROR', summary['error']])
    rows.append(['', ''])
    rows.append(['Notes', 'Per-node tabs: file:line + exact_code + vcc_code are TBD (manual source review required). actual_vertex tab is FISH-graph-derived ground truth.'])
    rows.append(['Tracepoint patch', 'Built with rclcpp + GenericSubscription tracepoint patch active (commit 97b0741)'])
    return rows

def per_node_table(node_name, info):
    rows = [['#', 'Source file', 'Line', 'Exact code', 'VCC code', 'Resulting vertex (E/F/aspect)', 'Notes']]
    # Pre-fill rows for each declared FISH vertex we observe.
    idx = 1
    for top in info.get('sub_topics', []):
        rows.append([idx, 'TBD', 'TBD', 'TBD: find create_subscription call', 'VCC2 (or VCC_GS / VCCI8 path)', f"E (sub) on {top}", ''])
        idx += 1
    for top in info.get('pub_topics', []):
        rows.append([idx, 'TBD', 'TBD', 'TBD: find create_publisher call', 'VCC3', f"pub_aspect on {top}", ''])
        idx += 1
    for nm in info.get('srv_names', []):
        rows.append([idx, 'TBD', 'TBD', 'TBD', 'VCC4', f"E (srv) on {nm}", ''])
        idx += 1
    for nm in info.get('cli_names', []):
        rows.append([idx, 'TBD', 'TBD', 'TBD', 'VCC5', f"cli_aspect on {nm}", ''])
        idx += 1
    for per in info.get('tmr_periods', []):
        rows.append([idx, 'TBD', 'TBD', 'TBD: find create_wall_timer call', 'VCC6', f"E (tmr) period={per} ns", ''])
        idx += 1
    if idx == 1:
        rows.append([1, '—', '—', '—', '—', '— (no FISH children observed)', ''])
    return rows

def sanitize_tab_name(s):
    # Sheets tab names: max 100 chars, no /\?*[]
    out = ''.join(c if c not in '/\\?*[]:' else '_' for c in s)
    return out[:95]

def main():
    if len(sys.argv) < 2:
        print('usage: pbr1_sheet_builder.py <benchmark_name>', file=sys.stderr); sys.exit(1)
    name = sys.argv[1]
    summary = load_summary(name) or {}
    payload = {
        'spreadsheet_title': f"{name.upper().replace('ISAAC_ROS_', '')}-NODE-A1",
        'tabs': []
    }
    payload['tabs'].append({'name': 'REFERENCE', 'rows': REFERENCE_ROWS})
    payload['tabs'].append({'name': 'bench_node', 'rows': bench_node_table(name, summary)})
    payload['tabs'].append({'name': 'expected_vertex', 'rows': expected_vertex_table(summary)})
    payload['tabs'].append({'name': 'actual_vertex', 'rows': actual_vertex_table(summary)})
    payload['tabs'].append({'name': 'perf_overhead', 'rows': perf_table(summary)})
    # Per-node tabs only for user-relevant nodes (skip ros2cli_* daemon helpers).
    nodes = (summary.get('graph_summary') or {}).get('nodes') or {}
    USER_NODES = [(n, info) for n, info in sorted(nodes.items())
                  if not n.startswith('/_ros2cli')
                  and not n.startswith('/launch_ros_')]
    for nname, info in USER_NODES:
        tab = sanitize_tab_name('node_' + nname.lstrip('/').replace('/', '_'))
        payload['tabs'].append({'name': tab, 'rows': per_node_table(nname, info)})
    print(json.dumps(payload, indent=2, default=str))

if __name__ == '__main__':
    main()
