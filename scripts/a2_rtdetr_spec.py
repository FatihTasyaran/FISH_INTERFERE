"""RTDETR-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_generic, managed_nitros_node)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_dnn_image_encoder_data import (
    RESIZE_NODE_DNN_REVIEW, IMAGE_FORMAT_CONVERTER_REVIEW,
    IMAGE_TO_TENSOR_REVIEW, INTERLEAVED_TO_PLANAR_REVIEW, RESHAPE_REVIEW,
)
from a2_misc_data import TENSORRT_REVIEW, simple_nitros_1in1out_review

PAD_REVIEW = simple_nitros_1in1out_review('PadNode',
    'isaac_ros_image_pipeline/isaac_ros_image_proc/src/pad_node.cpp', 'image', 'padded_image')
RTDETR_PREPROCESSOR_REVIEW = simple_nitros_1in1out_review('RtDetrPreprocessorNode',
    'isaac_ros_object_detection/isaac_ros_rtdetr/src/rtdetr_preprocessor_node.cpp', 'encoded_tensor', 'rtdetr_input')
RTDETR_DECODER_REVIEW = simple_nitros_1in1out_review('RtDetrDecoderNode',
    'isaac_ros_object_detection/isaac_ros_rtdetr/src/rtdetr_decoder_node.cpp', 'tensor_sub', 'detections_output')

EXPECTED = {
    '/r2b/container':              container_expected(),
    '/launch_ros_<pid>':           launch_ros_expected(),
    '/r2b/Controller':             controller_expected(),
    '/r2b/DataLoaderNode':         data_loader_expected(),
    '/r2b/PlaybackNode':           nitros_playback_node(2),
    '/r2b/MonitorNode':            nitros_monitor_node_generic('vision_msgs/msg/Detection2DArray'),
    '/r2b/ResizeNode':             nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/PadNode':                nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/ImageFormatConverter':   nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/ImageToTensorNode':      managed_nitros_node(1, 1),
    '/r2b/InterleavedToPlanarNode':nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/ReshapeNode':            nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/RtdetrPreprocessor':     nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/TensorRt':               nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/RtdetrDecoder':          nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'RTDETR-NODE-A2',
    'name': 'isaac_ros_rtdetr',
    'image': 'fish-r2b-rtdetr:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_rtdetr_benchmark/scripts/isaac_ros_rtdetr_graph.py',
    'container_name': 'container',
    'components_desc': 'DataLoader + Playback(2 NITROS) + Monitor (vision_msgs/Detection2DArray → generic) + 9 pipeline nodes (Resize, Pad, ImageFormatConverter, ImageToTensor, InterleavedToPlanar, Reshape, RtdetrPreprocessor, TensorRt, RtdetrDecoder)',
    'extra_fields': {},
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':              CONTAINER_REVIEW,
        'node_launch_ros':             LAUNCH_ROS_REVIEW,
        'node_Controller':             CONTROLLER_REVIEW,
        'node_DataLoaderNode':         DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':           make_nitros_playback_review(['nitros_image_rgb8','nitros_camera_info']),
        'node_MonitorNode':            make_nitros_monitor_review('vision_msgs/msg/Detection2DArray', use_nitros_type_monitor_sub=True, monitor_topic_remap='detections_output'),
        'node_ResizeNode':             RESIZE_NODE_DNN_REVIEW,
        'node_PadNode':                PAD_REVIEW,
        'node_ImageFormatConverter':   IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_ImageToTensorNode':      IMAGE_TO_TENSOR_REVIEW,
        'node_InterleavedToPlanarNode':INTERLEAVED_TO_PLANAR_REVIEW,
        'node_ReshapeNode':            RESHAPE_REVIEW,
        'node_RtdetrPreprocessor':     RTDETR_PREPROCESSOR_REVIEW,
        'node_TensorRt':               TENSORRT_REVIEW,
        'node_RtdetrDecoder':          RTDETR_DECODER_REVIEW,
    },
}
