"""DETECTNET-NODE-A2 spec."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')

from a2_lib import (container_expected, controller_expected, launch_ros_expected,
                    data_loader_expected, nitros_node, nitros_playback_node,
                    nitros_monitor_node_nitros_sub, managed_nitros_node)
from a2_shared import (CONTAINER_REVIEW, CONTROLLER_REVIEW, LAUNCH_ROS_REVIEW,
                       DATALOADER_NODE_REVIEW, make_nitros_playback_review,
                       make_nitros_monitor_review)
from a2_dnn_image_encoder_data import (
    RESIZE_NODE_DNN_REVIEW, IMAGE_FORMAT_CONVERTER_REVIEW, CROP_REVIEW,
    IMAGE_TO_TENSOR_REVIEW, IMAGE_TENSOR_NORMALIZE_REVIEW,
    INTERLEAVED_TO_PLANAR_REVIEW, RESHAPE_REVIEW,
)

# Simple 1-in/1-out review template for TritonNode + DetectNetDecoderNode
def _simple_nitros_review(name, in_topic, out_topic, file_path):
    return [
        [1, file_path, 'top', f'INPUT_COMPONENT_KEY + OUTPUT_COMPONENT_KEY', '(constants)', '—', '2 NEGOTIATED entries'],
        [2, file_path, 'CONFIG_MAP', 'CONFIG_MAP: 2 NEGOTIATED (input, output)', '(chain)', '—', ''],
        [3, file_path, 'ctor', ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
        [4, file_path, 'startNitrosNode', 'startNitrosNode()', 'VCCI2', '+1 E + 1 F (negotiation_timer)', ''],
        [5, 'nitros chain (1 in × VCCI8+VCCI9)', '—', '—', 'VCCI8 + VCCI9',
         '+2 E + 2 F + 1 pub_aspect', ''],
        [6, 'nitros chain (1 out × VCCI14+VCCI15)', '—', '—', 'VCCI14 + VCCI15',
         '+2 E + 2 F + 2 pub_aspect', ''],
        [7, 'nitros_node.cpp:721 (RUNTIME)', 'gxf_heartbeat_timer', '+1 E + 1 F', 'VCC_GHB', '', ''],
    ]

TRITON_REVIEW = _simple_nitros_review('TritonNode', 'tensor_pub', 'tensor_sub',
    'isaac_ros_dnn_inference/isaac_ros_triton/src/triton_node.cpp')
DETECTNET_DECODER_REVIEW = _simple_nitros_review('DetectNetDecoderNode', 'tensor_sub', 'detections_output',
    'isaac_ros_object_detection/isaac_ros_detectnet/src/detectnet_decoder_node.cpp')

EXPECTED = {
    '/r2b/container':                          container_expected(),
    '/launch_ros_<pid>':                       launch_ros_expected(),
    '/r2b/Controller':                         controller_expected(),
    '/r2b/DataLoaderNode':                     data_loader_expected(),
    '/r2b/PlaybackNode':                       nitros_playback_node(2),
    '/r2b/MonitorNode':                        nitros_monitor_node_nitros_sub(),
    '/r2b/TritonNode':                         nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/DetectNetDecoderNode':               nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    # 7 DNN encoder sub-launch nodes (same as dnn_image_encoder)
    '/r2b/resize_node':                        nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/image_format_converter_node':        nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/crop_node':                          nitros_node(2, 2, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/image_to_tensor':                    managed_nitros_node(1, 1),
    '/r2b/normalize_node':                     managed_nitros_node(1, 1),
    '/r2b/interleaved_to_planar_node':         nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
    '/r2b/reshape_node':                       nitros_node(1, 1, runtime_extra_E=1, runtime_extra_F=1),
}

SPEC = {
    'title': 'DETECTNET-NODE-A2',
    'name': 'isaac_ros_detectnet',
    'image': 'fish-r2b-detectnet:latest',
    'launch_script': 'isaac_ros_benchmark/benchmarks/isaac_ros_object_detection_benchmark/scripts/isaac_ros_detectnet_graph.py',
    'container_name': 'container',
    'components_desc':
        'DataLoaderNode + PlaybackNode (2 NITROS fmt) + MonitorNode (nitros_detection2_d_array → NEGOTIATED NITROS sub) + TritonNode + DetectNetDecoderNode + 7 dnn_image_encoder sub-launch nodes (resize, image_format_converter, crop, image_to_tensor, normalize, interleaved_to_planar, reshape).',
    'extra_fields': {
        'Triton + DetectNet decoder':
            'Both are 1-input/1-output NEGOTIATED NitrosNodes — TritonNode passes through nitros_tensor_list NCHW→NHWC, DetectNetDecoder converts tensor → detection2_d_array',
        'Monitor':
            'monitor_data_format=nitros_detection2_d_array + use_nitros_type_monitor_sub=True → NEG NitrosSub',
    },
    'expected': EXPECTED,
    'per_node_reviews': {
        'node_container':                       CONTAINER_REVIEW,
        'node_launch_ros':                      LAUNCH_ROS_REVIEW,
        'node_Controller':                      CONTROLLER_REVIEW,
        'node_DataLoaderNode':                  DATALOADER_NODE_REVIEW,
        'node_PlaybackNode':                    make_nitros_playback_review(['nitros_image_bgr8', 'nitros_camera_info']),
        'node_MonitorNode':                     make_nitros_monitor_review('nitros_detection2_d_array', use_nitros_type_monitor_sub=True, monitor_topic_remap='detections_output'),
        'node_TritonNode':                      TRITON_REVIEW,
        'node_DetectNetDecoderNode':            DETECTNET_DECODER_REVIEW,
        'node_resize_node':                     RESIZE_NODE_DNN_REVIEW,
        'node_image_format_converter_node':     IMAGE_FORMAT_CONVERTER_REVIEW,
        'node_crop_node':                       CROP_REVIEW,
        'node_image_to_tensor':                 IMAGE_TO_TENSOR_REVIEW,
        'node_normalize_node':                  IMAGE_TENSOR_NORMALIZE_REVIEW,
        'node_interleaved_to_planar_node':      INTERLEAVED_TO_PLANAR_REVIEW,
        'node_reshape_node':                    RESHAPE_REVIEW,
    },
}
