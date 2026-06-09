"""Shared per-node reviews for nodes that follow the standard 1-in/1-out NITROS template."""

def simple_nitros_1in1out_review(name, file_hint, in_topic='input', out_topic='output'):
    """For a NITROS NEGOTIATED node with 1 input + 1 output."""
    return [
        [1, file_hint, 'top',
         'INPUT_COMPONENT_KEY + OUTPUT_COMPONENT_KEY (2 NEGOTIATED CONFIG_MAP entries)',
         '(constants)', '—', ''],
        [2, file_hint, 'CONFIG_MAP', '2 NEGOTIATED entries (1 in + 1 out)', '(chain)', '—', ''],
        [3, file_hint, 'ctor', ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
        [4, file_hint, 'startNitrosNode', 'startNitrosNode()', 'VCCI2', '+1 E + 1 F (negotiation_timer)', ''],
        [5, 'nitros chain — 1 input', 'compat sub + NegSub on /' + in_topic, 'VCCI8 + VCCI9', '+2 E + 2 F + 1 pub_aspect', ''],
        [6, 'nitros chain — 1 output', 'compat pub + NegPub on /' + out_topic, 'VCCI14 + VCCI15', '+2 E + 2 F + 2 pub_aspect', ''],
        [7, 'nitros_node.cpp:721 (RUNTIME)', 'gxf_heartbeat_timer', 'VCC_GHB', '+1 E + 1 F', ''],
    ]

def simple_nitros_NxM_review(name, file_hint, n_in, n_out):
    """Generic N-in/M-out NITROS NEGOTIATED review (compressed)."""
    return [
        [1, file_hint, 'top', f'CONFIG_MAP: {n_in} NEGOTIATED inputs + {n_out} NEGOTIATED outputs', '(constants)', '—', ''],
        [2, file_hint, 'ctor', ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
        [3, file_hint, 'startNitrosNode', 'startNitrosNode()', 'VCCI2', '+1 E + 1 F', ''],
        [4, f'{n_in} inputs', f'compat sub + NegSub ×{n_in}', f'VCCI8 + VCCI9 ×{n_in}',
         f'+{2*n_in} E + {2*n_in} F + {n_in} pub_aspect', ''],
        [5, f'{n_out} outputs', f'compat pub + NegPub ×{n_out}', f'VCCI14 + VCCI15 ×{n_out}',
         f'+{2*n_out} E + {2*n_out} F + {2*n_out} pub_aspect', ''],
        [6, 'nitros_node.cpp:721 (RUNTIME)', 'gxf_heartbeat_timer', 'VCC_GHB', '+1 E + 1 F', ''],
    ]

# Specific reviews
TENSORRT_REVIEW         = simple_nitros_1in1out_review('TensorRTNode',         'isaac_ros_dnn_inference/isaac_ros_tensor_rt/src/tensor_rt_node.cpp', 'tensor_pub', 'tensor_sub')
TRITON_REVIEW           = simple_nitros_1in1out_review('TritonNode',           'isaac_ros_dnn_inference/isaac_ros_triton/src/triton_node.cpp',       'tensor_pub', 'tensor_sub')
UNET_DECODER_REVIEW     = simple_nitros_1in1out_review('UNetDecoderNode',      'isaac_ros_image_segmentation/isaac_ros_unet/src/unet_decoder_node.cpp', 'tensor_sub', 'unet/raw_segmentation_mask')
CENTERPOSE_DECODER_REVIEW = simple_nitros_1in1out_review('CenterPoseDecoderNode', 'isaac_ros_pose_estimation/isaac_ros_centerpose/src/centerpose_decoder_node.cpp', 'tensor_sub', 'centerpose_decoder/centerpose_output')
DOPE_DECODER_REVIEW     = simple_nitros_1in1out_review('DopeDecoderNode',      'isaac_ros_pose_estimation/isaac_ros_dope/src/dope_decoder_node.cpp', 'belief_map_array', 'poses')
DETECTNET_DECODER_REVIEW = simple_nitros_1in1out_review('DetectNetDecoderNode', 'isaac_ros_object_detection/isaac_ros_detectnet/src/detectnet_decoder_node.cpp', 'tensor_sub', 'detections_output')

# Freespace segmentation — read inside container if available; assume 1-in/1-out NITROS
FREESPACE_REVIEW        = simple_nitros_1in1out_review('FreespaceSegmentationNode', 'isaac_ros_depth_segmentation/isaac_ros_bi3d_freespace/src/freespace_segmentation_node.cpp', 'bi3d_node_output', 'freespace_planar')
