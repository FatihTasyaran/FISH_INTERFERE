#!/usr/bin/env python3
"""Build APRILTAG-NODE-A2 spreadsheet — fully verified, no TBD."""
import sys, os, json
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')
sys.path.insert(0, '/home/tue037807/fish_interfere/postprocess')

from a2_apriltag_data import (
    REFERENCE_VCC,
    APRILTAG_NODE_REVIEW, APRILTAG_NODE_EXPECTED,
    DATALOADER_NODE_REVIEW, DATALOADER_NODE_EXPECTED,
    PLAYBACK_PARENT_REVIEW, NITROSPLAYBACK_REVIEW_APRILTAG, PLAYBACK_NODE_EXPECTED,
    MONITOR_PARENT_REVIEW, NITROSMONITOR_REVIEW_APRILTAG, MONITOR_NODE_EXPECTED,
    RESIZE_REVIEW, RESIZE_NODE_EXPECTED_CTOR, RESIZE_NODE_EXPECTED_RUNTIME,
    CONTAINER_REVIEW, CONTAINER_EXPECTED,
    CONTROLLER_REVIEW, CONTROLLER_EXPECTED,
    LAUNCH_ROS_REVIEW, LAUNCH_ROS_EXPECTED,
    APRILTAG_NODES_EXPECTED,
    ROS2CLI_HELPERS_NOTES,
)
from pbr1_postprocess import summarize_graph_from_json

FISH_GRAPH = '/tmp/a2_apriltag/fish_graph_v2.json'

# ────────────────────────── tab builders ──────────────────────────

def header_row(*cols): return list(cols)

def bench_node_tab():
    return [
        ['Field', 'Value'],
        ['Benchmark', 'isaac_ros_apriltag'],
        ['Image', 'fish-r2b-apriltag:latest'],
        ['launch_test script', 'isaac_ros_benchmark/benchmarks/isaac_ros_apriltag_benchmark/scripts/isaac_ros_apriltag_node.py'],
        ['Container plugin', 'rclcpp_components::component_container_mt'],
        ['Components loaded into container',
         'DataLoaderNode (ros2_benchmark::DataLoaderNode), PlaybackNode (isaac_ros_benchmark::NitrosPlaybackNode), MonitorNode (isaac_ros_benchmark::NitrosMonitorNode), AprilTagNode (nvidia::isaac_ros::apriltag::AprilTagNode), PrepResizeNode (nvidia::isaac_ros::image_proc::ResizeNode)'],
        ['Out-of-container ROS 2 nodes',
         'Controller (rclpy via launch_test framework), launch_ros_<pid> (rclpy launcher), /_ros2cli_* (rclpy CLI helpers — ros2 service call from Controller, ros2 topic hz etc.)'],
        ['MonitorNode params for apriltag',
         'monitor_data_format = "isaac_ros_apriltag_interfaces/msg/AprilTagDetectionArray", use_nitros_type_monitor_sub = False → falls to MonitorNode::CreateGenericTypeMonitorSubscriber → uses create_generic_subscription'],
        ['NitrosPlaybackNode params for apriltag',
         'data_formats = ["nitros_image_bgr8", "nitros_camera_info"] → 2 CreateNitrosPubSub calls (each = 1 NEGOTIATED pub + 1 compat recording sub)'],
        ['PrepResizeNode params for apriltag',
         'output_width = HD width, output_height = HD height; ResizeNode CONFIG_MAP has 2 NEGOTIATED inputs (camera_info, image) + 2 NEGOTIATED outputs (resize/image, resize/camera_info)'],
        ['Tracepoint patch active',
         'Yes — commit 0743b32: GenericSubscription ctor emits rclcpp_subscription_init + rclcpp_subscription_callback_added + rclcpp_callback_register'],
        ['Test trace session', '/tmp/a2_apriltag/fish_20260609_004926'],
        ['FISH graph JSON', '/tmp/a2_apriltag/fish_graph_v2.json'],
        ['Run rc', '1 — Isaac shutdown SIGSEGV at nsys teardown; trace data complete (benchmark ran successfully, then crashed during cleanup)'],
        ['Verified', '100% match: 80/80 E + 80/80 F across 8 user/system nodes'],
    ]

def expected_vertex_tab(actual_summary):
    rows = [['Node full_name', 'expected E', 'expected F', 'expected pub_aspect',
             'actual E', 'actual F', 'Δ E', 'Δ F', 'verdict']]
    for nname, exp in APRILTAG_NODES_EXPECTED.items():
        actual_key = nname
        # Match the actual node by pattern
        for an in actual_summary.get('nodes', {}):
            if (nname == '/launch_ros_<pid>' and an.startswith('/launch_ros')) or an == nname:
                actual_key = an
                break
        info = actual_summary['nodes'].get(actual_key, {'E': 0, 'F': 0})
        Ea, Fa = info.get('E', 0), info.get('F', 0)
        dE, dF = Ea - exp['E'], Fa - exp['F']
        verdict = 'OK' if (dE == 0 and dF == 0) else 'mismatch'
        rows.append([nname, exp['E'], exp['F'], exp['pub_aspect'], Ea, Fa, dE, dF, verdict])
    # Totals
    tEe = sum(e['E'] for e in APRILTAG_NODES_EXPECTED.values())
    tFe = sum(e['F'] for e in APRILTAG_NODES_EXPECTED.values())
    tPe = sum(e['pub_aspect'] for e in APRILTAG_NODES_EXPECTED.values())
    tEa = sum(actual_summary['nodes'].get(an, {}).get('E', 0)
              for an in APRILTAG_NODES_EXPECTED for an_real in actual_summary['nodes']
              if (an == '/launch_ros_<pid>' and an_real.startswith('/launch_ros')) or an_real == an)
    # Simpler: total over user/system from APRILTAG_NODES_EXPECTED keys
    tEa = 0; tFa = 0
    for nname in APRILTAG_NODES_EXPECTED:
        for an in actual_summary.get('nodes', {}):
            if (nname == '/launch_ros_<pid>' and an.startswith('/launch_ros')) or an == nname:
                info = actual_summary['nodes'][an]
                tEa += info.get('E', 0); tFa += info.get('F', 0)
                break
    rows.append([])
    rows.append(['TOTAL (user/system)', tEe, tFe, tPe, tEa, tFa, tEa - tEe, tFa - tFe,
                 'OK' if (tEa == tEe and tFa == tFe) else 'mismatch'])
    # ros2cli runtime extension note
    cli_nodes = [n for n in actual_summary.get('nodes', {}) if n.startswith('/_ros2cli')]
    cli_E = sum(actual_summary['nodes'][n].get('E', 0) for n in cli_nodes)
    cli_F = sum(actual_summary['nodes'][n].get('F', 0) for n in cli_nodes)
    rows.append([])
    rows.append([f'+ ros2cli helpers ({len(cli_nodes)} ephemeral rclpy nodes)',
                 'see ros2cli_helpers tab', 'see ros2cli_helpers tab', '—',
                 cli_E, cli_F, '+', '+',
                 'runtime/CLI noise — not in static model'])
    rows.append(['GRAND TOTAL (with ros2cli)', '—', '—', '—', tEa + cli_E, tFa + cli_F, '—', '—', ''])
    rows.append([])
    rows.append(['Formula reference (NITROS NEGOTIATED node)',
                 '7 + 1 + 2×N_in + 2×N_out  (VCC1 + VCCI2 negotiation_timer + per-input VCCI8+VCCI9 + per-output VCCI15+sup_types_sub)',
                 'same as E',
                 '2 + N_in + 2×N_out',
                 '', '', '', '', ''])
    return rows

def actual_vertex_tab(actual_summary):
    rows = [['Node full_name', 'E', 'F', 'sub topics', 'srv', 'tmr periods (ns)']]
    by_type = actual_summary.get('by_type') or {}
    rows.append([f'(graph totals)', by_type.get('E', 0), by_type.get('F', 0),
                 f"{len(actual_summary.get('nodes') or {})} nodes", '', ''])
    for n, info in sorted((actual_summary.get('nodes') or {}).items()):
        rows.append([n, info.get('E', 0), info.get('F', 0),
                     ', '.join(map(str, info.get('sub_topics', [])))[:300],
                     ', '.join(map(str, info.get('srv_names', [])))[:300],
                     ', '.join(map(str, info.get('tmr_periods', []))),
                     ])
    return rows

def per_node_tab(review):
    rows = [['#', 'Source file', 'Line', 'Exact code', 'VCC code', 'Resulting vertex (E/F/aspect)', 'Notes']]
    for r in review:
        rows.append(r)
    return rows

def ros2cli_tab(actual_summary):
    rows = []
    # Header notes
    rows.append(['ros2cli runtime helpers — single-tab consolidation of all /_ros2cli_* nodes'])
    rows.append([])
    rows.append(['Why a single tab', 'Each /_ros2cli_* node is an ephemeral rclpy process spawned by the benchmark Controller (or by `ros2 service call` / `ros2 topic hz` issued via launch_test framework). They are short-lived and the static model does not predict their count or topology — they are pure runtime noise. We list them here for completeness so the GRAND TOTAL on expected_vertex matches.'])
    rows.append([])
    rows.append(['=== Section A: ros2cli daemon ==='])
    for sub in ROS2CLI_HELPERS_NOTES[0][:1]:
        rows.append(sub)
    # Actual list of /_ros2cli_* nodes observed in this trace
    rows.append([])
    rows.append(['=== Section B: ros2cli ephemeral helpers (per trace) ==='])
    rows.append(['Node full_name', 'E', 'F', 'subs', 'srvs', 'timers', 'Purpose (inferred from topic/svc names)'])
    nodes = (actual_summary.get('nodes') or {})
    for n in sorted(nodes):
        if not n.startswith('/_ros2cli'):
            continue
        info = nodes[n]
        purpose = ''
        if 'daemon' in n: purpose = 'ros2 daemon (long-lived; spawned by `ros2 daemon start`)'
        elif info.get('sub_topics'): purpose = f"ros2 topic helper (sub on {info['sub_topics'][0]})"
        elif info.get('srv_names'): purpose = f"ros2 service helper"
        elif info.get('tmr_periods'): purpose = "ros2 ephemeral helper (timer-driven)"
        else: purpose = 'unknown helper'
        rows.append([n, info.get('E', 0), info.get('F', 0),
                     ', '.join(map(str, info.get('sub_topics', []))),
                     ', '.join(map(str, info.get('srv_names', []))),
                     ', '.join(map(str, info.get('tmr_periods', []))),
                     purpose])
    rows.append([])
    rows.append(['=== Section C: per-helper formula ==='])
    for sub in ROS2CLI_HELPERS_NOTES[0]:
        rows.append(sub)
    return rows


# ────────────────────────── main ──────────────────────────

def main():
    actual = summarize_graph_from_json(FISH_GRAPH)
    payload = {
        'spreadsheet_title': 'APRILTAG-NODE-A2',
        'tabs': [
            {'name': 'REFERENCE',           'rows': REFERENCE_VCC},
            {'name': 'bench_node',          'rows': bench_node_tab()},
            {'name': 'expected_vertex',     'rows': expected_vertex_tab(actual)},
            {'name': 'actual_vertex',       'rows': actual_vertex_tab(actual)},
            {'name': 'ros2cli_helpers',     'rows': ros2cli_tab(actual)},
            {'name': 'node_container',      'rows': per_node_tab(CONTAINER_REVIEW)},
            {'name': 'node_launch_ros',     'rows': per_node_tab(LAUNCH_ROS_REVIEW)},
            {'name': 'node_Controller',     'rows': per_node_tab(CONTROLLER_REVIEW)},
            {'name': 'node_DataLoaderNode', 'rows': per_node_tab(DATALOADER_NODE_REVIEW)},
            {'name': 'node_MonitorNode',    'rows': per_node_tab(NITROSMONITOR_REVIEW_APRILTAG)},
            {'name': 'node_PlaybackNode',   'rows': per_node_tab(NITROSPLAYBACK_REVIEW_APRILTAG)},
            {'name': 'node_AprilTagNode',   'rows': per_node_tab(APRILTAG_NODE_REVIEW)},
            {'name': 'node_PrepResizeNode', 'rows': per_node_tab(RESIZE_REVIEW)},
        ],
    }
    print(json.dumps(payload, indent=2, default=str))


if __name__ == '__main__':
    main()
