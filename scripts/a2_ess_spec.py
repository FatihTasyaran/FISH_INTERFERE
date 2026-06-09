"""ESS-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)

# ESSDisparityNode review — same template as DisparityNode (4 NEGOTIATED in + 1 NEGOTIATED out)
ESS_REVIEW = [
    [1, 'isaac_ros_dnn_stereo_depth/isaac_ros_ess/src/ess_disparity_node.cpp', 'top',
     'CONFIG_MAP: 4 NEGOTIATED inputs (left/right image + left/right camera_info) + 1 NEGOTIATED output (disparity)',
     '(chain)', '—', 'Same 4-in/1-out template as Bi3DNode / stereo DisparityNode'],
    [2, 'ess_disparity_node.cpp', 'ctor', ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
    [3, 'ess_disparity_node.cpp', 'startNitrosNode()', 'startNitrosNode()', 'VCCI2', '+1 E + 1 F', ''],
    [4, '4 inputs', 'compat sub + NegSub × 4', 'VCCI8 + VCCI9 ×4', '+8 E + 8 F + 4 pub_aspect', ''],
    [5, '1 output', 'compat pub + NegPub', 'VCCI14 + VCCI15', '+2 E + 2 F + 2 pub_aspect', ''],
    [6, 'nitros_node.cpp:721 (RUNTIME)', 'gxf_heartbeat_timer', 'VCC_GHB', '+1 E + 1 F', ''],
]

EXPECTED = {
    '/r2b/ess_disparity_container': container_expected(),
    '/launch_ros_<pid>':            launch_ros_expected(),
    '/r2b/Controller':              controller_expected(),
    '/r2b/DataLoaderNode':          data_loader_expected(),
    '/r2b/PlaybackNode':            nitros_playback_node(4),
    '/r2b/MonitorNode':             nitros_monitor_node_nitros_sub(),
    '/r2b/ESSDisparityNode':        nitros_node(4, 2, runtime_extra_E=1, runtime_extra_F=1),  # 4 inputs + 2 outputs (disparity + passthrough camera_info)
}

SPEC = {
    'title': 'ESS-NODE-A2',
    'name': 'isaac_ros_ess',
    'image': 'fish-r2b-ess:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_ess_benchmark/scripts/isaac_ros_ess_node.py',
    'container_name': 'ess_disparity_container',
    'components_desc': 'DataLoaderNode + PlaybackNode (4 NITROS fmt) + MonitorNode (disparity NEG) + ESSDisparityNode (4-in 1-out NEG)',
    'extra_fields': {
        'ESS pipeline': '4 NITROS NEGOTIATED inputs (left + right image, left + right camera_info) → ESS disparity output',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':           CONTAINER_REVIEW,
        'node_launch_ros':          LAUNCH_ROS_REVIEW,
        'node_Controller':          CONTROLLER_REVIEW,
        'node_DataLoaderNode':      DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':        make_nitros_playback_review(['nitros_image_rgb8','nitros_camera_info','nitros_image_rgb8','nitros_camera_info']),
        'node_MonitorNode':         make_nitros_monitor_review('nitros_disparity_image_32FC1', use_nitros_type_monitor_sub=True, monitor_topic_remap='disparity'),
        'node_ESSDisparityNode':    ESS_REVIEW,
    },
}
