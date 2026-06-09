#!/usr/bin/env python3
"""Unified A2 sheet builder. Given a benchmark spec, build the payload + write."""
import sys, os, json
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')
sys.path.insert(0, '/home/tue037807/fish_interfere/postprocess')

from a2_lib import (
    container_expected, controller_expected, launch_ros_expected,
    data_loader_expected, nitros_node, nitros_playback_node,
    nitros_monitor_node_generic, nitros_monitor_node_nitros_sub, nitros_monitor_node_ros_type,
    total,
)
from a2_shared import (
    REFERENCE_VCC, CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
    DATALOADER_NODE_REVIEW, ROS2CLI_HELPERS_NOTES,
    make_nitros_playback_review, make_nitros_monitor_review,
)
from pbr1_postprocess import summarize_graph_from_json
from pbr1_sheet_writer import make_clients, write_payload


# ─────────────────── Helpers ───────────────────

def bench_node_tab(spec):
    rows = [['Field', 'Value']]
    rows.append(['Benchmark', spec['name']])
    rows.append(['Image', spec['image']])
    rows.append(['launch_test script', spec['launch_script']])
    rows.append(['Container name', spec['container_name']])
    rows.append(['Components loaded', spec['components_desc']])
    rows.append(['Out-of-container ROS 2 nodes', spec.get('out_of_container_desc',
                 'Controller (rclpy), launch_ros_<pid> (rclpy), /_ros2cli_* helpers')])
    for k, v in spec.get('extra_fields', {}).items():
        rows.append([k, v])
    rows.append(['Tracepoint patch active',
                 'Yes — commit 0743b32: GenericSubscription ctor emits rclcpp_subscription_init + callback_added + callback_register'])
    rows.append(['Test trace session', spec.get('session_dir', '—')])
    rows.append(['FISH graph JSON', spec.get('fish_graph_path', '—')])
    rows.append(['Run rc', spec.get('run_rc', '—')])
    rows.append(['Verdict', spec.get('verdict', '—')])
    return rows


def expected_vertex_tab(expected_dict, actual_summary, container_node_name):
    rows = [['Node full_name', 'expected E', 'expected F', 'expected pub_aspect',
             'actual E', 'actual F', 'Δ E', 'Δ F', 'verdict']]
    actuals = (actual_summary or {}).get('nodes') or {}
    tEe = tFe = tPe = tEa = tFa = 0
    for nname, exp in expected_dict.items():
        # Match by pattern
        actual_key = None
        for an in actuals:
            if (nname == '/launch_ros_<pid>' and an.startswith('/launch_ros')) or an == nname:
                actual_key = an; break
        info = actuals.get(actual_key, {'E': 0, 'F': 0}) if actual_key else {'E': 0, 'F': 0}
        Ea, Fa = info.get('E', 0), info.get('F', 0)
        Ee, Fe, Pe = exp
        dE, dF = Ea - Ee, Fa - Fe
        verdict = 'OK' if (dE == 0 and dF == 0) else 'mismatch'
        rows.append([nname, Ee, Fe, Pe, Ea, Fa, dE, dF, verdict])
        tEe += Ee; tFe += Fe; tPe += Pe; tEa += Ea; tFa += Fa
    rows.append([])
    rows.append(['TOTAL (user/system)', tEe, tFe, tPe, tEa, tFa, tEa - tEe, tFa - tFe,
                 'OK' if (tEa == tEe and tFa == tFe) else 'mismatch'])
    # ros2cli
    cli_nodes = [n for n in actuals if n.startswith('/_ros2cli')]
    cli_E = sum(actuals[n].get('E', 0) for n in cli_nodes)
    cli_F = sum(actuals[n].get('F', 0) for n in cli_nodes)
    rows.append([])
    rows.append([f'+ ros2cli helpers ({len(cli_nodes)})',
                 'see ros2cli_helpers tab', 'see ros2cli_helpers tab', '—',
                 cli_E, cli_F, '+', '+', 'runtime/CLI noise — not in static model'])
    rows.append(['GRAND TOTAL', '—', '—', '—', tEa + cli_E, tFa + cli_F, '—', '—', ''])
    rows.append([])
    rows.append(['Formula reference (NITROS NEGOTIATED node)',
                 'E = 7 + 1 + 2·N_in + 2·N_out  (+1 runtime gxf_heartbeat_timer)',
                 'F = same as E',
                 '2 + N_in + 2·N_out',
                 '', '', '', '', ''])
    return rows


def actual_vertex_tab(actual_summary):
    rows = [['Node full_name', 'E', 'F', 'sub topics', 'srv', 'tmr periods (ns)']]
    by_type = (actual_summary or {}).get('by_type') or {}
    nodes = (actual_summary or {}).get('nodes') or {}
    rows.append([f'(graph totals)', by_type.get('E', 0), by_type.get('F', 0),
                 f'{len(nodes)} nodes', '', ''])
    for n, info in sorted(nodes.items()):
        rows.append([n, info.get('E', 0), info.get('F', 0),
                     ', '.join(map(str, info.get('sub_topics', [])))[:300],
                     ', '.join(map(str, info.get('srv_names', [])))[:300],
                     ', '.join(map(str, info.get('tmr_periods', [])))])
    return rows


def ros2cli_tab(actual_summary):
    rows = [['ros2cli runtime helpers — single-tab consolidation']]
    rows.append([])
    rows.append(['Why a single tab',
                 'Each /_ros2cli_* node is an ephemeral rclpy process spawned by ros2 CLI tools (service call, topic hz/echo) invoked by the benchmark Controller. Short-lived, runtime noise — not in the static expected model.'])
    rows.append([])
    rows.append(['=== Observed helpers in this trace ==='])
    rows.append(['Node full_name', 'E', 'F', 'subs', 'srvs', 'timers', 'Purpose (inferred)'])
    nodes = (actual_summary or {}).get('nodes') or {}
    for n in sorted(nodes):
        if not n.startswith('/_ros2cli'): continue
        info = nodes[n]
        if 'daemon' in n: purpose = 'ros2 daemon (long-lived; spawned by `ros2 daemon start`)'
        elif info.get('sub_topics'): purpose = f"topic helper (sub on {info['sub_topics'][0]})"
        elif info.get('srv_names'): purpose = 'service helper'
        elif info.get('tmr_periods'): purpose = 'timer-driven helper'
        else: purpose = 'unknown'
        rows.append([n, info.get('E', 0), info.get('F', 0),
                     ', '.join(map(str, info.get('sub_topics', []))),
                     ', '.join(map(str, info.get('srv_names', []))),
                     ', '.join(map(str, info.get('tmr_periods', []))), purpose])
    rows.append([])
    rows.append(['=== Per-helper baseline ==='])
    for sub in ROS2CLI_HELPERS_NOTES[0]:
        rows.append(sub)
    return rows


def per_node_tab(review):
    rows = [['#', 'Source file', 'Line', 'Exact code', 'VCC code', 'Resulting vertex (E/F/aspect)', 'Notes']]
    for r in review:
        rows.append(r)
    return rows


def build_payload(spec, actual_summary):
    """spec keys: name, image, launch_script, container_name, components_desc, fish_graph_path, session_dir, run_rc, verdict, expected (dict), per_node_reviews (dict), extra_fields"""
    payload = {
        'spreadsheet_title': spec['title'],
        'tabs': [
            {'name': 'REFERENCE',       'rows': REFERENCE_VCC},
            {'name': 'bench_node',      'rows': bench_node_tab(spec)},
            {'name': 'expected_vertex', 'rows': expected_vertex_tab(spec['expected'], actual_summary, spec['container_name'])},
            {'name': 'actual_vertex',   'rows': actual_vertex_tab(actual_summary)},
            {'name': 'ros2cli_helpers', 'rows': ros2cli_tab(actual_summary)},
        ],
    }
    for tab_name, review in spec['per_node_reviews'].items():
        payload['tabs'].append({'name': tab_name, 'rows': per_node_tab(review)})
    return payload


def write_bench(spec, fish_graph_path):
    """Build + write the A2 spreadsheet for one benchmark."""
    actual = summarize_graph_from_json(fish_graph_path)
    spec['fish_graph_path'] = fish_graph_path
    payload = build_payload(spec, actual)
    sheets, drive = make_clients()
    sid = write_payload(sheets, drive, payload)
    return sid


if __name__ == '__main__':
    print('a2_build.py — import and call write_bench(spec, fish_graph_path) per benchmark.')
