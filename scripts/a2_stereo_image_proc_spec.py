"""STEREO_IMAGE_PROC-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW,
                       make_nitros_playback_review, make_nitros_monitor_review)
from a2_apriltag_data import RESIZE_REVIEW  # PrepLeft/RightResize reuse
from a2_stereo_image_proc_data import DISPARITY_REVIEW

EXPECTED = {
    '/r2b/disparity_container':  container_expected(),
    '/launch_ros_<pid>':         launch_ros_expected(),
    '/r2b/Controller':           controller_expected(),
    '/r2b/DataLoaderNode':       data_loader_expected(),
    '/r2b/PrepLeftResizeNode':   nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PrepRightResizeNode':  nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/DisparityNode':        nitros_node(4, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PlaybackNode':         nitros_playback_node(4),   # 4 formats: 2 image + 2 camera_info
    '/r2b/MonitorNode':          nitros_monitor_node_nitros_sub(),  # nitros_disparity_image_32FC1 + use_nitros_type_monitor_sub=True
}

SPEC = {
    'title': 'STEREO_IMAGE_PROC-NODE-A2',
    'name': 'isaac_ros_stereo_image_proc',
    'image': 'fish-r2b-stereo_image_proc:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_stereo_image_proc_benchmark/scripts/isaac_ros_disparity_node.py',
    'container_name': 'disparity_container',
    'components_desc': 'DataLoaderNode, PrepLeftResizeNode (image_proc::ResizeNode), PrepRightResizeNode (image_proc::ResizeNode), DisparityNode (stereo_image_proc::DisparityNode — 4 NEGOTIATED inputs, 1 NEGOTIATED output), PlaybackNode (NitrosPlaybackNode with 4 data_formats: 2 image_bgr8 + 2 camera_info), MonitorNode (NitrosMonitorNode monitor_data_format=nitros_disparity_image_32FC1 + use_nitros_type_monitor_sub=True)',
    'extra_fields': {
        'MonitorNode path': 'CreateNitrosMonitorSubscriber → NitrosSubscriber NEGOTIATED on /disparity (compat) + /disparity/nitros (NEG)',
        'PlaybackNode data_formats': '4: nitros_image_bgr8, nitros_image_bgr8, nitros_camera_info, nitros_camera_info',
        'DisparityNode CONFIG_MAP': '5 NEGOTIATED (4 in + 1 out): /left/image_rect, /right/image_rect, /left/camera_info, /right/camera_info, /disparity',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':         CONTAINER_REVIEW,
        'node_launch_ros':        LAUNCH_ROS_REVIEW,
        'node_Controller':        CONTROLLER_REVIEW,
        'node_DataLoaderNode':    DATALOADER_NODE_REVIEW,
        'node_PrepLeftResizeNode':  RESIZE_REVIEW,
        'node_PrepRightResizeNode': RESIZE_REVIEW,
        'node_DisparityNode':     DISPARITY_REVIEW,
        'node_PlaybackNode':      make_nitros_playback_review(['nitros_image_bgr8', 'nitros_image_bgr8', 'nitros_camera_info', 'nitros_camera_info']),
        'node_MonitorNode':       make_nitros_monitor_review('nitros_disparity_image_32FC1', use_nitros_type_monitor_sub=True, monitor_topic_remap='disparity'),
    },
}
