"""NVBLOX-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node_generic,
                    nitros_monitor_node_generic, transform_listener_impl)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_visual_slam_data import (VISUAL_SLAM_REVIEW, VISUAL_SLAM_NODE_EXPECTED,
                                 TRANSFORM_LISTENER_REVIEW)
from a2_nvblox_data import NVBLOX_NODE_REVIEW, NVBLOX_NODE_EXPECTED, IMAGE_FORMAT_CONVERTER_REVIEW

EXPECTED = {
    '/r2b/container':                          container_expected(),
    '/launch_ros_<pid>':                       launch_ros_expected(),
    '/r2b/Controller':                         controller_expected(),
    '/r2b/DataLoaderNode':                     data_loader_expected(),
    '/r2b/PlaybackNode':                       nitros_playback_node_generic(5),
    '/r2b/MonitorNode0':                       nitros_monitor_node_generic('nvblox_msgs/msg/Mesh'),
    '/r2b/MonitorNode1':                       nitros_monitor_node_generic('nvblox_msgs/msg/DistanceMapSlice'),
    '/r2b/NvbloxNode':                         NVBLOX_NODE_EXPECTED,
    '/r2b/VisualSlamNode':                     VISUAL_SLAM_NODE_EXPECTED,
    '/r2b/PrepLeftImageFormatConverterNode':   nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PrepRightImageFormatConverterNode':  nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/transform_listener_impl_<hash1>':    transform_listener_impl(),
    '/r2b/transform_listener_impl_<hash2>':    transform_listener_impl(),
}

SPEC = {
    'title': 'NVBLOX-NODE-A2',
    'name': 'isaac_ros_nvblox',
    'image': 'fish-r2b-nvblox:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_nvblox_benchmark/scripts/isaac_ros_nvblox_node.py',
    'container_name': 'container',
    'components_desc':
        'DataLoaderNode, PlaybackNode (NitrosPlaybackNode with 5 ROS-msg data_formats → generic path), MonitorNode0 (nvblox_msgs/msg/Mesh → generic), MonitorNode1 (nvblox_msgs/msg/DistanceMapSlice → generic), NvbloxNode (plain rclcpp Node — 2 ManagedNitrosMessageFilter image subs + 2 plain camera_info subs + 1 pointcloud sub + 2 transform/pose subs + 6 user services + 1 wall_timer), VisualSlamNode (plain rclcpp Node — same as visual_slam bench, num_cameras=2 + enable_imu_fusion=false), 2× ImageFormatConverter (NITROS 1-in/1-out). Plus 2 tf2_ros::TransformListener helpers.',
    'extra_fields': {
        'PlaybackNode path':
            'data_formats = [sensor_msgs/Image, sensor_msgs/CameraInfo, sensor_msgs/Image, sensor_msgs/CameraInfo, tf2_msgs/TFMessage] — all ROS msgs → CreateGenericPubSub',
        'MonitorNode paths':
            'Both monitor_data_formats are ROS msg names (nvblox_msgs/Mesh, nvblox_msgs/DistanceMapSlice) → hasFormat()=false → generic path',
        'NvbloxNode params (defaults for r2b bench)':
            'use_color=true, use_depth=true, use_lidar=true → 2 ManagedNitrosMessageFilter (depth+left rgb) + 2 plain message_filters camera_info + 1 pointcloud sub + 2 transform/pose subs',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':                       CONTAINER_REVIEW,
        'node_launch_ros':                      LAUNCH_ROS_REVIEW,
        'node_Controller':                      CONTROLLER_REVIEW,
        'node_DataLoaderNode':                  DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':                    make_nitros_playback_review(['sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo', 'sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo', 'tf2_msgs/msg/TFMessage']),
        'node_MonitorNode0':                    make_nitros_monitor_review('nvblox_msgs/msg/Mesh', use_nitros_type_monitor_sub=True, monitor_topic_remap='mesh'),
        'node_MonitorNode1':                    make_nitros_monitor_review('nvblox_msgs/msg/DistanceMapSlice', use_nitros_type_monitor_sub=True, monitor_topic_remap='map_slice'),
        'node_NvbloxNode':                      NVBLOX_NODE_REVIEW,
        'node_VisualSlamNode':                  VISUAL_SLAM_REVIEW,
        'node_PrepLeftImageFormatConverter':    IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_PrepRightImageFormatConverter':   IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_transform_listener_impl':         TRANSFORM_LISTENER_REVIEW,
    },
}
