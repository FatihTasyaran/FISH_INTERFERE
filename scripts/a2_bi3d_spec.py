"""BI3D-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_apriltag_data import RESIZE_REVIEW

# Bi3DNode review — 4 NEGOTIATED inputs + 1 NEGOTIATED output (1 NOOP entry ignored)
BI3D_REVIEW = [
    [1, 'isaac_ros_depth_segmentation/isaac_ros_bi3d/src/bi3d_node.cpp', '49-58',
     'INPUT_LEFT_IMAGE + INPUT_RIGHT_IMAGE + INPUT_LEFT_CAM_INFO + INPUT_RIGHT_CAM_INFO + INPUT_DISPARITY (NOOP) + OUTPUT_BI3D',
     '(6 CONFIG_MAP keys; 5 NEGOTIATED + 1 NOOP)', '—', ''],
    [2, 'bi3d_node.cpp', 'CONFIG_MAP', '4 NEGOTIATED inputs + 1 NOOP (disparity values, internal) + 1 NEGOTIATED output (bi3d_node_output)',
     '(chain)', '—', 'NOOP entries do not create vertices'],
    [3, 'bi3d_node.cpp', 'ctor', ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
    [4, 'bi3d_node.cpp', 'startNitrosNode()', 'startNitrosNode()', 'VCCI2', '+1 E + 1 F', ''],
    [5, '4 inputs × VCCI8+VCCI9', 'compat sub + NegSub each',
     'VCCI8 ×4 + VCCI9 ×4', '+8 E + 8 F + 4 pub_aspect', ''],
    [6, '1 output × VCCI14+VCCI15', 'compat pub + NegPub',
     'VCCI14 + VCCI15', '+2 E + 2 F + 2 pub_aspect', ''],
    [7, 'nitros_node.cpp:721 (RUNTIME)', 'gxf_heartbeat_timer', 'VCC_GHB', '+1 E + 1 F', ''],
]

EXPECTED = {
    '/r2b/bi3d_container':         container_expected(),
    '/launch_ros_<pid>':           launch_ros_expected(),
    '/r2b/Controller':             controller_expected(),
    '/r2b/DataLoaderNode':         data_loader_expected(),
    '/r2b/PrepLeftResizeNode':     nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PrepRightResizeNode':    nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PlaybackNode':           nitros_playback_node(4),
    '/r2b/MonitorNode':            nitros_monitor_node_nitros_sub(),
    '/r2b/Bi3DNode':               nitros_node(4, 1, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'BI3D-NODE-A2',
    'name': 'isaac_ros_bi3d',
    'image': 'fish-r2b-bi3d:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_bi3d_benchmark/scripts/isaac_ros_bi3d_node.py',
    'container_name': 'bi3d_container',
    'components_desc':
        'DataLoaderNode + 2× ResizeNode (PrepLeft/RightResize) + Bi3DNode + PlaybackNode (4 NITROS fmt) + MonitorNode (nitros_disparity_image_32FC1 NEG)',
    'extra_fields': {
        'Bi3DNode CONFIG_MAP': '6 keys = 4 NEGOTIATED inputs (left/right image + left/right camera_info) + 1 NOOP (disparity values internal) + 1 NEGOTIATED output (bi3d_node_output)',
        'Monitor path': 'nitros_disparity_image_32FC1 + use_nitros_type_monitor_sub=True → NEG NitrosSub',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':              CONTAINER_REVIEW,
        'node_launch_ros':             LAUNCH_ROS_REVIEW,
        'node_Controller':             CONTROLLER_REVIEW,
        'node_DataLoaderNode':         DATALOADER_NODE_REVIEW,
        'node_PrepLeftResizeNode':     RESIZE_REVIEW,
        'node_PrepRightResizeNode':    RESIZE_REVIEW,
        'node_PlaybackNode':           make_nitros_playback_review(['nitros_image_rgb8','nitros_image_rgb8','nitros_camera_info','nitros_camera_info']),
        'node_MonitorNode':            make_nitros_monitor_review('nitros_disparity_image_32FC1', use_nitros_type_monitor_sub=True, monitor_topic_remap='bi3d_node_output'),
        'node_Bi3DNode':               BI3D_REVIEW,
    },
}
