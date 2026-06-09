"""IMAGE_PROC-NODE-A2 spec — used by a2_build.write_bench."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW,
                       make_nitros_playback_review, make_nitros_monitor_review)
from a2_image_proc_data import RECTIFY_REVIEW

# Container name: 'rectify_container'
# 4 components: DataLoaderNode, RectifyNode, PlaybackNode (NitrosPlaybackNode 2 formats), MonitorNode (NitrosMonitorNode nitros_neg sub)

EXPECTED = {
    '/r2b/rectify_container':   container_expected(),
    '/launch_ros_<pid>':        launch_ros_expected(),
    '/r2b/Controller':          controller_expected(),
    '/r2b/DataLoaderNode':      data_loader_expected(),
    '/r2b/PlaybackNode':        nitros_playback_node(2),  # 2 NITROS formats: nitros_image_bgr8 + nitros_camera_info
    '/r2b/MonitorNode':         nitros_monitor_node_nitros_sub(),  # use_nitros_type_monitor_sub=True + monitor_data_format=nitros_image_bgr8
    '/r2b/RectifyNode':         nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),  # 2 NEGOTIATED in + 2 NEGOTIATED out + gxf_heartbeat
}

SPEC = {
    'title': 'IMAGE_PROC-NODE-A2',
    'name': 'isaac_ros_image_proc',
    'image': 'fish-r2b-image_proc:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_image_proc_benchmark/scripts/isaac_ros_rectify_node.py',
    'container_name': 'rectify_container',
    'components_desc': 'DataLoaderNode (ros2_benchmark::DataLoaderNode), RectifyNode (nvidia::isaac_ros::image_proc::RectifyNode), PlaybackNode (isaac_ros_benchmark::NitrosPlaybackNode with data_formats=[nitros_image_bgr8, nitros_camera_info]), MonitorNode (isaac_ros_benchmark::NitrosMonitorNode with monitor_data_format=nitros_image_bgr8 + use_nitros_type_monitor_sub=True)',
    'extra_fields': {
        'MonitorNode params for image_proc':
            'monitor_data_format = "nitros_image_bgr8" (NITROS-registered) + use_nitros_type_monitor_sub = True → CreateNitrosMonitorSubscriber (NitrosSubscriber NEGOTIATED on /image_rect)',
        'NitrosPlaybackNode params for image_proc':
            'data_formats = ["nitros_image_bgr8", "nitros_camera_info"]',
        'RectifyNode CONFIG_MAP':
            '4 NEGOTIATED entries (2 inputs: camera_info, image; 2 outputs: image_rect, camera_info_rect)',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':       CONTAINER_REVIEW,
        'node_launch_ros':      LAUNCH_ROS_REVIEW,
        'node_Controller':      CONTROLLER_REVIEW,
        'node_DataLoaderNode':  DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':    make_nitros_playback_review(['nitros_image_bgr8', 'nitros_camera_info']),
        'node_MonitorNode':     make_nitros_monitor_review('nitros_image_bgr8', use_nitros_type_monitor_sub=True, monitor_topic_remap='image_rect'),
        'node_RectifyNode':     RECTIFY_REVIEW,
    },
    'session_dir': None,    # filled in after run
    'fish_graph_path': None,
    'run_rc': None,
    'verdict': None,
}
