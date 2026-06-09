"""TENSOR_RT-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub, managed_nitros_node)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_dnn_image_encoder_data import (
    RESIZE_NODE_DNN_REVIEW, IMAGE_FORMAT_CONVERTER_REVIEW,
    IMAGE_TO_TENSOR_REVIEW, IMAGE_TENSOR_NORMALIZE_REVIEW, RESHAPE_REVIEW,
)
from a2_misc_data import TENSORRT_REVIEW

EXPECTED = {
    '/r2b/tensor_rt_container':  container_expected(),
    '/launch_ros_<pid>':         launch_ros_expected(),
    '/r2b/Controller':           controller_expected(),
    '/r2b/DataLoaderNode':       data_loader_expected(),
    '/r2b/PlaybackNode':         nitros_playback_node(1),
    '/r2b/MonitorNode':          nitros_monitor_node_nitros_sub(),
    '/r2b/ResizeNode':           nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/ImageFormatConverter': nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/ImageToTensorNode':    managed_nitros_node(1, 1),
    '/r2b/NormalizeNode':        managed_nitros_node(1, 1),
    '/r2b/ReshapeNode':          nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/TensorRTNode':         nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'TENSOR_RT-NODE-A2',
    'name': 'isaac_ros_tensor_rt',
    'image': 'fish-r2b-tensor_rt:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_tensor_rt_benchmark/scripts/isaac_ros_tensor_rt_ps_node.py',
    'container_name': 'tensor_rt_container',
    'components_desc': 'DataLoader + Playback(1 nitros_tensor_list) + Monitor + 5 image preprocessing + TensorRTNode',
    'extra_fields': {},
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':                       CONTAINER_REVIEW,
        'node_launch_ros':                      LAUNCH_ROS_REVIEW,
        'node_Controller':                      CONTROLLER_REVIEW,
        'node_DataLoaderNode':                  DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':                    make_nitros_playback_review(['nitros_tensor_list_nchw_rgb_f32']),
        'node_MonitorNode':                     make_nitros_monitor_review('nitros_tensor_list_nhwc_rgb_f32', use_nitros_type_monitor_sub=True, monitor_topic_remap='tensor_sub'),
        'node_ResizeNode':                      RESIZE_NODE_DNN_REVIEW,
        'node_ImageFormatConverter':            IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_ImageToTensorNode':               IMAGE_TO_TENSOR_REVIEW,
        'node_NormalizeNode':                   IMAGE_TENSOR_NORMALIZE_REVIEW,
        'node_ReshapeNode':                     RESHAPE_REVIEW,
        'node_TensorRTNode':                    TENSORRT_REVIEW,
    },
}
