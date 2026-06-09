"""DNN_IMAGE_ENCODER-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub, managed_nitros_node)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW,
                       make_nitros_playback_review, make_nitros_monitor_review)
from a2_dnn_image_encoder_data import (
    RESIZE_NODE_DNN_REVIEW, IMAGE_FORMAT_CONVERTER_REVIEW, CROP_REVIEW,
    IMAGE_TO_TENSOR_REVIEW, IMAGE_TENSOR_NORMALIZE_REVIEW,
    INTERLEAVED_TO_PLANAR_REVIEW, RESHAPE_REVIEW,
)

EXPECTED = {
    '/r2b/container':                   container_expected(),
    '/launch_ros_<pid>':                launch_ros_expected(),
    '/r2b/Controller':                  controller_expected(),
    '/r2b/DataLoaderNode':              data_loader_expected(),
    '/r2b/PlaybackNode':                nitros_playback_node(2),
    '/r2b/MonitorNode':                 nitros_monitor_node_nitros_sub(),
    # Sub-launch nodes (added in dnn_image_encoder.launch.py)
    '/r2b/resize_node':                 nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),  # 17
    '/r2b/image_format_converter_node': nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),  # 13
    '/r2b/crop_node':                   nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),  # 17
    '/r2b/image_to_tensor':             managed_nitros_node(1, 1),                                # 11
    '/r2b/normalize_node':              managed_nitros_node(1, 1),                                # 11
    '/r2b/interleaved_to_planar_node':  nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),  # 13
    '/r2b/reshape_node':                nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),  # 13
}

SPEC = {
    'title': 'DNN_IMAGE_ENCODER-NODE-A2',
    'name': 'isaac_ros_dnn_image_encoder',
    'image': 'fish-r2b-dnn_image_encoder:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_dnn_image_encoder_benchmark/scripts/isaac_ros_dnn_image_encoder_node.py',
    'container_name': 'container',
    'components_desc':
        'launch_test: DataLoaderNode + PlaybackNode + MonitorNode + the dnn_image_encoder.launch.py sub-launch which adds 7 nodes (resize_node, image_format_converter_node, crop_node, image_to_tensor, normalize_node, interleaved_to_planar_node, reshape_node) to the same container.',
    'extra_fields': {
        'Sub-launch':
            'isaac_ros_dnn_image_encoder/launch/dnn_image_encoder.launch.py attaches 7 component nodes to the same /r2b/container.',
        'Monitor sub path':
            'nitros_tensor_list_nchw_rgb_f32 + use_nitros_type_monitor_sub=True → CreateNitrosMonitorSubscriber NEGOTIATED',
        'image_to_tensor / normalize_node':
            'Plain rclcpp::Node (NOT NitrosNode) — use ManagedNitrosSubscriber + ManagedNitrosPublisher pair (no negotiation_timer, no gxf_heartbeat).',
        'Other sub-launch nodes':
            'NitrosNode subclasses with CONFIG_MAP (1-in/1-out for image_format_converter, interleaved_to_planar, reshape; 2-in/2-out for resize, crop).',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':          CONTAINER_REVIEW,
        'node_launch_ros':         LAUNCH_ROS_REVIEW,
        'node_Controller':         CONTROLLER_REVIEW,
        'node_DataLoaderNode':     DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':       make_nitros_playback_review(['nitros_image_bgr8', 'nitros_camera_info']),
        'node_MonitorNode':        make_nitros_monitor_review('nitros_tensor_list_nchw_rgb_f32', use_nitros_type_monitor_sub=True, monitor_topic_remap='output'),
        'node_resize_node':        RESIZE_NODE_DNN_REVIEW,
        'node_image_format_converter_node': IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_crop_node':          CROP_REVIEW,
        'node_image_to_tensor':    IMAGE_TO_TENSOR_REVIEW,
        'node_normalize_node':     IMAGE_TENSOR_NORMALIZE_REVIEW,
        'node_interleaved_to_planar_node': INTERLEAVED_TO_PLANAR_REVIEW,
        'node_reshape_node':       RESHAPE_REVIEW,
    },
}
