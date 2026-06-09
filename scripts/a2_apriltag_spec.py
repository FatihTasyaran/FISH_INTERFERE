"""APRILTAG-NODE-A2 spec (unified format, compatible with a2_build.write_bench)."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_generic)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_apriltag_data import (
    APRILTAG_NODE_REVIEW, NITROSMONITOR_REVIEW_APRILTAG,
    NITROSPLAYBACK_REVIEW_APRILTAG, RESIZE_REVIEW,
)

EXPECTED = {
    '/r2b/container':       container_expected(),
    '/launch_ros_<pid>':    launch_ros_expected(),
    '/r2b/Controller':      controller_expected(),
    '/r2b/DataLoaderNode':  data_loader_expected(),
    '/r2b/MonitorNode':     nitros_monitor_node_generic('isaac_ros_apriltag_interfaces/msg/AprilTagDetectionArray'),
    '/r2b/PlaybackNode':    nitros_playback_node(2),
    '/r2b/AprilTagNode':    (10, 10, 5),  # plain rclcpp::Node with NitrosMessageFilter image_sub + plain msg_filter camera_info_sub
    '/r2b/PrepResizeNode':  nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'APRILTAG-NODE-A2',
    'name': 'isaac_ros_apriltag',
    'image': 'fish-r2b-apriltag:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_apriltag_benchmark/scripts/isaac_ros_apriltag_node.py',
    'container_name': 'container',
    'components_desc': 'DataLoaderNode + PrepResizeNode (ResizeNode 2-in/2-out NEG) + PlaybackNode (NitrosPlayback 2 NITROS fmt) + MonitorNode (use_nitros_type_monitor_sub=False + AprilTagDetectionArray → falls to generic path → create_generic_subscription) + AprilTagNode (plain rclcpp::Node with NitrosMessageFilter image_sub + plain msg_filter camera_info_sub).',
    'extra_fields': {
        'AprilTagNode subscriber setup':
            'image_sub_ = NitrosMessageFilterSubscriber<NitrosImageView> (VCCI17 → 18 → 6 → VCCI8 + VCCI9): 2 E + 2 F + 1 pub_aspect; camera_info_sub_ = plain message_filters::Subscriber<CameraInfo>: 1 E + 1 F.',
        'MonitorNode path':
            'monitor_data_format="isaac_ros_apriltag_interfaces/msg/AprilTagDetectionArray" + use_nitros_type_monitor_sub=False. nitros_type_manager_.hasFormat() returns false → CreateGenericTypeMonitorSubscriber → create_generic_subscription. Patched in commit 0743b32 to fire rclcpp_subscription_init + callback_added + callback_register.',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':       CONTAINER_REVIEW,
        'node_launch_ros':      LAUNCH_ROS_REVIEW,
        'node_Controller':      CONTROLLER_REVIEW,
        'node_DataLoaderNode':  DATALOADER_NODE_REVIEW,
        'node_MonitorNode':     NITROSMONITOR_REVIEW_APRILTAG,
        'node_PlaybackNode':    NITROSPLAYBACK_REVIEW_APRILTAG,
        'node_AprilTagNode':    APRILTAG_NODE_REVIEW,
        'node_PrepResizeNode':  RESIZE_REVIEW,
    },
}
