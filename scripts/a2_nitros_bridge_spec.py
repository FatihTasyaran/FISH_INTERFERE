"""NITROS_BRIDGE-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_apriltag_data import RESIZE_REVIEW

EXPECTED = {
    '/r2b/container':              container_expected(),
    '/launch_ros_<pid>':           launch_ros_expected(),
    '/r2b/Controller':             controller_expected(),
    '/r2b/DataLoaderNode':         data_loader_expected(),
    '/r2b/PlaybackNode':           nitros_playback_node(1),
    '/r2b/MonitorNode':            nitros_monitor_node_nitros_sub(),
    '/r2b/PrepResizeNode':         nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'NITROS_BRIDGE-NODE-A2',
    'name': 'isaac_ros_nitros_bridge',
    'image': 'fish-r2b-nitros_bridge:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_nitros_bridge_benchmark/scripts/isaac_ros_nitros_bridge_reference.py',
    'container_name': 'container',
    'components_desc': 'DataLoader + PrepResizeNode + Playback(1 NITROS) + Monitor (nitros_image_bgr8 NEG). Reference (no actual bridge node — just measures throughput through ros2 type-adapted NITROS image pipeline)',
    'extra_fields': {},
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':         CONTAINER_REVIEW,
        'node_launch_ros':        LAUNCH_ROS_REVIEW,
        'node_Controller':        CONTROLLER_REVIEW,
        'node_DataLoaderNode':    DATALOADER_NODE_REVIEW,
        'node_PrepResizeNode':    RESIZE_REVIEW,
        'node_PlaybackNode':      make_nitros_playback_review(['nitros_image_bgr8']),
        'node_MonitorNode':       make_nitros_monitor_review('nitros_image_bgr8', use_nitros_type_monitor_sub=True, monitor_topic_remap='ros2_output_image'),
    },
}
