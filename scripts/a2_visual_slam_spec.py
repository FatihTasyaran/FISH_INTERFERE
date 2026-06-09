"""VISUAL_SLAM-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_playback_node_generic,
                    nitros_monitor_node_generic, transform_listener_impl,
                    static_transform_publisher)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_visual_slam_data import (VISUAL_SLAM_REVIEW, VISUAL_SLAM_NODE_EXPECTED,
                                 TRANSFORM_LISTENER_REVIEW, STATIC_TRANSFORM_PUBLISHER_REVIEW)

EXPECTED = {
    '/r2b/vslam_container':       container_expected(),                # 4/4/1
    '/launch_ros_<pid>':          launch_ros_expected(),               # 6/6/2
    '/r2b/Controller':            controller_expected(),               # 6/6/2
    '/r2b/DataLoaderNode':        data_loader_expected(),              # 11/11/2
    '/r2b/PlaybackNode':          nitros_playback_node_generic(5),     # 15/15/7 (5 ROS types not in NITROS reg)
    '/r2b/MonitorNode':           nitros_monitor_node_generic('nav_msgs/msg/Odometry'),  # 10/10/2
    '/r2b/VisualSlamNode':        VISUAL_SLAM_NODE_EXPECTED,           # 20/20/25
    '/r2b/transform_listener_impl_<hash>': transform_listener_impl(),  # 3/3/1
    '/static_transform_publisher': static_transform_publisher(),       # 7/7/3
}

SPEC = {
    'title': 'VISUAL_SLAM-NODE-A2',
    'name': 'isaac_ros_visual_slam',
    'image': 'fish-r2b-visual_slam:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_visual_slam_benchmark/scripts/isaac_ros_visual_slam_node.py',
    'container_name': 'vslam_container',
    'components_desc':
        'DataLoaderNode, PlaybackNode (NitrosPlaybackNode with data_formats = [sensor_msgs/Image, sensor_msgs/CameraInfo, sensor_msgs/Image, sensor_msgs/CameraInfo, sensor_msgs/Imu] — ALL ROS msg types, NOT NITROS-registered → CreateGenericPubSub path), MonitorNode (nav_msgs/msg/Odometry → generic path), VisualSlamNode (plain rclcpp::Node with num_cameras=2 default + enable_imu_fusion=false default). Plus static_transform_publisher (separate process) + 1 tf2_ros::TransformListener internal node /r2b/transform_listener_impl_<hash>.',
    'extra_fields': {
        'PlaybackNode path':
            'data_formats are all ROS msg type names, not NITROS supported_type_names → hasFormat()=false → CreateGenericPubSub (parent\'s method) for each. Per format: GenericPublisher (pub_aspect) + GenericSubscription (1 E + 1 F via VCC_GS).',
        'MonitorNode path':
            'monitor_data_format="nav_msgs/msg/Odometry" → hasFormat()=false → CreateGenericTypeMonitorSubscriber → create_generic_subscription (1 E + 1 F).',
        'VisualSlamNode default params':
            'num_cameras=2 (stereo) + enable_imu_fusion=false → 2 image ManagedSub + 2 camera_info plain sub + 0 IMU sub + 1 initial_pose sub.',
        'static_transform_publisher + transform_listener':
            'visual_slam config attaches a tf2_ros::StaticTransformBroadcaster as a separate launch process; cuVSLAM internally builds a tf2_ros::TransformListener which spawns the transform_listener_impl_<hash> Node.',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':                CONTAINER_REVIEW,
        'node_launch_ros':               LAUNCH_ROS_REVIEW,
        'node_Controller':               CONTROLLER_REVIEW,
        'node_DataLoaderNode':           DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':             make_nitros_playback_review(['sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo', 'sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo', 'sensor_msgs/msg/Imu']),
        'node_MonitorNode':              make_nitros_monitor_review('nav_msgs/msg/Odometry', use_nitros_type_monitor_sub=True, monitor_topic_remap='output'),
        'node_VisualSlamNode':           VISUAL_SLAM_REVIEW,
        'node_transform_listener_impl':  TRANSFORM_LISTENER_REVIEW,
        'node_static_transform_publisher': STATIC_TRANSFORM_PUBLISHER_REVIEW,
    },
}
