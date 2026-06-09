"""BI3D_FREESPACE-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub, static_transform_publisher,
                    transform_listener_impl)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_apriltag_data import RESIZE_REVIEW
from a2_visual_slam_data import STATIC_TRANSFORM_PUBLISHER_REVIEW, TRANSFORM_LISTENER_REVIEW
from a2_bi3d_spec import BI3D_REVIEW
from a2_misc_data import FREESPACE_REVIEW

EXPECTED = {
    '/r2b/container':                  container_expected(),
    '/launch_ros_<pid>':               launch_ros_expected(),
    '/r2b/Controller':                 controller_expected(),
    '/r2b/DataLoaderNode':             data_loader_expected(),
    '/r2b/PrepLeftResizeNode':         nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PrepRightResizeNode':        nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/Bi3DNode':                   nitros_node(4, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/FreespaceSegmentationNode':  nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/StaticTransformBroadcasterNode': static_transform_publisher(),  # top-level namespace (not /r2b)
    '/r2b/PlaybackNode':               nitros_playback_node(1),  # 1 NITROS format (nitros_disparity_image_32FC1)
    '/r2b/MonitorNode':                nitros_monitor_node_nitros_sub(),
    '/r2b/transform_listener_impl_<hash>': transform_listener_impl(),
}

SPEC = {
    'title': 'BI3D_FREESPACE-NODE-A2',
    'name': 'isaac_ros_bi3d_freespace',
    'image': 'fish-r2b-bi3d_freespace:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_bi3d_freespace_benchmark/scripts/isaac_ros_bi3d_fs_node.py',
    'container_name': 'container',
    'components_desc': 'DataLoader + 2× ResizeNode + Bi3DNode + FreespaceSegmentationNode + StaticTransformBroadcasterNode + Playback(4) + Monitor',
    'extra_fields': {},
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':                       CONTAINER_REVIEW,
        'node_launch_ros':                      LAUNCH_ROS_REVIEW,
        'node_Controller':                      CONTROLLER_REVIEW,
        'node_DataLoaderNode':                  DATALOADER_NODE_REVIEW,
        'node_PrepLeftResizeNode':              RESIZE_REVIEW,
        'node_PrepRightResizeNode':             RESIZE_REVIEW,
        'node_Bi3DNode':                        BI3D_REVIEW,
        'node_FreespaceSegmentationNode':       FREESPACE_REVIEW,
        'node_StaticTransformBroadcasterNode':  STATIC_TRANSFORM_PUBLISHER_REVIEW,
        'node_PlaybackNode':                    make_nitros_playback_review(['nitros_disparity_image_32FC1']),
        'node_MonitorNode':                     make_nitros_monitor_review('nitros_occupancy_grid', use_nitros_type_monitor_sub=True, monitor_topic_remap='freespace_segmentation_output'),
        'node_transform_listener_impl':         TRANSFORM_LISTENER_REVIEW,
    },
}
