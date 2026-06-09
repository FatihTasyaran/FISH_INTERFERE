"""DOPE-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node_generic,
                    nitros_monitor_node_nitros_sub)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_misc_data import TENSORRT_REVIEW, DOPE_DECODER_REVIEW
from a2_lib import managed_nitros_node
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
    '/r2b/PlaybackNode':                nitros_playback_node_generic(2),
    '/r2b/MonitorNode':                 nitros_monitor_node_nitros_sub(),
    '/r2b/resize_node':                 nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/image_format_converter_node': nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/crop_node':                   nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/image_to_tensor':             managed_nitros_node(1, 1),
    '/r2b/normalize_node':              managed_nitros_node(1, 1),
    '/r2b/interleaved_to_planar_node':  nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/reshape_node':                nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/TensorRTNode':                nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/DopeDecoder':                 nitros_node(1, 2, runtime_extra_E=1, runtime_extra_F=1),  # 1 in 2 outs (poses + objects)
}

SPEC = {
    'title': 'DOPE-NODE-A2',
    'name': 'isaac_ros_dope',
    'image': 'fish-r2b-dope:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_dope_benchmark/scripts/isaac_ros_dope_graph.py',
    'container_name': 'container',
    'components_desc': 'DataLoader + Playback(2 ROS msg → generic) + Monitor + TensorRTNode + DopeDecoderNode',
    'extra_fields': {},
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':                       CONTAINER_REVIEW,
        'node_launch_ros':                      LAUNCH_ROS_REVIEW,
        'node_Controller':                      CONTROLLER_REVIEW,
        'node_DataLoaderNode':                  DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':                    make_nitros_playback_review(['sensor_msgs/msg/Image','sensor_msgs/msg/CameraInfo']),
        'node_MonitorNode':                     make_nitros_monitor_review('nitros_detection3_d_array', use_nitros_type_monitor_sub=True, monitor_topic_remap='poses'),
        'node_resize_node':                     RESIZE_NODE_DNN_REVIEW,
        'node_image_format_converter_node':     IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_crop_node':                       CROP_REVIEW,
        'node_image_to_tensor':                 IMAGE_TO_TENSOR_REVIEW,
        'node_normalize_node':                  IMAGE_TENSOR_NORMALIZE_REVIEW,
        'node_interleaved_to_planar_node':      INTERLEAVED_TO_PLANAR_REVIEW,
        'node_reshape_node':                    RESHAPE_REVIEW,
        'node_TensorRTNode':                    TENSORRT_REVIEW,
        'node_DopeDecoder':                     DOPE_DECODER_REVIEW,
    },
}
