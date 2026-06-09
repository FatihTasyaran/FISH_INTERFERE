"""CENTERPOSE-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node_generic,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_misc_data import TRITON_REVIEW, CENTERPOSE_DECODER_REVIEW

EXPECTED = {
    '/r2b/centerpose_container':  container_expected(),
    '/launch_ros_<pid>':          launch_ros_expected(),
    '/r2b/Controller':            controller_expected(),
    '/r2b/DataLoaderNode':        data_loader_expected(),
    '/r2b/PlaybackNode':          nitros_playback_node_generic(2),  # ROS msg → generic
    '/r2b/MonitorNode':           nitros_monitor_node_nitros_sub(),
    '/r2b/TritonNode':            nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/CenterPoseDecoderNode': nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'CENTERPOSE-NODE-A2',
    'name': 'isaac_ros_centerpose',
    'image': 'fish-r2b-centerpose:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_centerpose_benchmark/scripts/isaac_ros_centerpose_graph.py',
    'container_name': 'centerpose_container',
    'components_desc': 'DataLoader + Playback(2 ROS msg → generic) + Monitor(nitros_detection3_d_array NEG) + TritonNode + CenterPoseDecoderNode',
    'extra_fields': {},
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':              CONTAINER_REVIEW,
        'node_launch_ros':             LAUNCH_ROS_REVIEW,
        'node_Controller':             CONTROLLER_REVIEW,
        'node_DataLoaderNode':         DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':           make_nitros_playback_review(['sensor_msgs/msg/Image','sensor_msgs/msg/CameraInfo']),
        'node_MonitorNode':            make_nitros_monitor_review('nitros_detection3_d_array', use_nitros_type_monitor_sub=True, monitor_topic_remap='centerpose_decoder/centerpose_output'),
        'node_TritonNode':             TRITON_REVIEW,
        'node_CenterPoseDecoderNode':  CENTERPOSE_DECODER_REVIEW,
    },
}
