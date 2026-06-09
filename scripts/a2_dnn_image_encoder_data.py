"""DNN_IMAGE_ENCODER-NODE-A2 per-node review data."""

# Shared template for "minimal NITROS NEGOTIATED 1-in/1-out node"
def _nitros_1in1out_review(name, in_topic, out_topic, file_path):
    return [
        [1, file_path, 'top',
         f'INPUT_COMPONENT_KEY="..."; OUTPUT_COMPONENT_KEY="..."; INPUT_TOPIC_NAME="{in_topic}"; OUTPUT_TOPIC_NAME="{out_topic}"',
         '(constants)', '—', '2 CONFIG_MAP keys: 1 NEGOTIATED in + 1 NEGOTIATED out'],
        [2, file_path, 'CONFIG_MAP',
         f'CONFIG_MAP: type=NEGOTIATED for both', '(chain)', '—', ''],
        [3, file_path, 'ctor',
         f': nitros::NitrosNode(options, ...)', 'VCCI1 → VCC1',
         '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
        [4, file_path, 'startNitrosNode',
         'startNitrosNode()', 'VCCI2', '+1 E + 1 F (negotiation_timer 100ms)', ''],
        [5, 'nitros_subscriber.cpp + nitros_format_agent.hpp',
         f'compat sub on /{in_topic}', 'VCCI8', '+1 E + 1 F', ''],
        [6, 'negotiated_subscription.cpp ctor',
         f'NegotiatedSubscription on /{in_topic}/nitros', 'VCCI9',
         '+1 E + 1 F + 1 pub_aspect', ''],
        [7, 'nitros_publisher.cpp + nitros_format_agent.hpp',
         f'output compat pub on /{out_topic}', 'VCCI14', '+1 pub_aspect', ''],
        [8, 'negotiated_publisher.cpp:290 ctor + 581 start',
         f'NegotiatedPublisher on /{out_topic}/nitros', 'VCCI15',
         '+1 pub_aspect (TopicsInfo pub) + +1 E + 1 F (graph_change_timer) + +1 E + 1 F (_supported_types sub)', ''],
        [9, 'nitros_node.cpp:721 postNegotiationCallback (RUNTIME)',
         '[runtime] create_wall_timer(gxf_heartbeat_timer)', 'VCC_GHB',
         '+1 E + 1 F (post-negotiation)', ''],
    ]

# Shared template for "ManagedNitros (plain rclcpp::Node) 1-sub/1-pub"
def _managed_review(name, in_topic, out_topic, file_path):
    return [
        [1, file_path, 'ctor',
         f': rclcpp::Node("{name}", options)', 'VCC1',
         '+1 N + 7 E + 7 F + 2 pub_aspect (/rosout, /parameter_events)', ''],
        [2, file_path, 'ctor body',
         f'nitros_*_sub_ = make_shared<ManagedNitrosSubscriber<...>>(this, "{in_topic}", ..., callback, diagnostics, qos)',
         'VCCI17 → VCCI18 → VCCI6 → VCCI8 + VCCI9',
         f'+2 E + 2 F + 1 pub_aspect (compat sub /{in_topic} + NegSub /{in_topic}/nitros)', ''],
        [3, file_path, 'ctor body',
         f'nitros_*_pub_ = make_shared<ManagedNitrosPublisher<...>>(this, "{out_topic}", ..., diagnostics, qos)',
         'VCCI14 + VCCI15',
         f'+2 E + 2 F + 2 pub_aspect (compat pub /{out_topic} + NegPub /{out_topic}/nitros)', ''],
    ]


RESIZE_NODE_DNN_REVIEW = [
    [1, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '43-58',
     'INPUT_CAM_COMPONENT_KEY="sync/camera_info_in"; INPUT_COMPONENT_KEY="sync/image_in"; OUTPUT_COMPONENT_KEY="image_sink/sink"; OUTPUT_CAM_COMPONENT_KEY="camera_info_sink/sink"',
     '(constants)', '—', '4 CONFIG_MAP keys: 2 NEGOTIATED in + 2 NEGOTIATED out'],
    [2, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '82-117',
     'CONFIG_MAP: NEGOTIATED entries (image, camera_info → resize/image, resize/camera_info)',
     '(chain)', '—', ''],
    [3, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '136',
     ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
    [4, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '230',
     'startNitrosNode()', 'VCCI2', '+1 E + 1 F', ''],
    [5, 'nitros chain (2 inputs × VCCI8+VCCI9)', '—', '—', 'VCCI8 + VCCI9 ×2',
     '+4 E + 4 F + 2 pub_aspect', ''],
    [6, 'nitros chain (2 outputs × VCCI14+VCCI15)', '—', '—', 'VCCI14 + VCCI15 ×2',
     '+4 E + 4 F + 4 pub_aspect', ''],
    [7, 'nitros_node.cpp:721 postNegotiationCallback (RUNTIME)', '—', '—', 'VCC_GHB',
     '+1 E + 1 F (gxf_heartbeat_timer)', ''],
]

IMAGE_FORMAT_CONVERTER_REVIEW = _nitros_1in1out_review('image_format_converter',
    'image_raw', 'image',
    'isaac_ros_image_pipeline/isaac_ros_image_proc/src/image_format_converter_node.cpp')

CROP_REVIEW = [
    [1, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/crop_node.cpp', 'top',
     'INPUT_IMAGE_COMPONENT_KEY + INPUT_CAM_COMPONENT_KEY + OUTPUT_IMAGE_COMPONENT_KEY + OUTPUT_CAM_COMPONENT_KEY',
     '(constants)', '—', '4 CONFIG_MAP keys (2 in + 2 out NEGOTIATED) — same template as ResizeNode'],
    [2, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/crop_node.cpp', 'CONFIG_MAP',
     'CONFIG_MAP: 4 NEGOTIATED entries (image, camera_info → cropped image, cropped camera_info)',
     '(chain)', '—', ''],
    [3, 'crop_node.cpp', 'ctor', ': nitros::NitrosNode(...)', 'VCCI1 → VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
    [4, 'crop_node.cpp', 'startNitrosNode', 'startNitrosNode()', 'VCCI2', '+1 E + 1 F', ''],
    [5, 'nitros chain (2 inputs × VCCI8+VCCI9)', '—', '—', 'VCCI8 + VCCI9 ×2',
     '+4 E + 4 F + 2 pub_aspect', ''],
    [6, 'nitros chain (2 outputs × VCCI14+VCCI15)', '—', '—', 'VCCI14 + VCCI15 ×2',
     '+4 E + 4 F + 4 pub_aspect', ''],
    [7, 'nitros_node.cpp:721 (RUNTIME)', '—', '—', 'VCC_GHB', '+1 E + 1 F (gxf_heartbeat_timer)', ''],
]

IMAGE_TO_TENSOR_REVIEW = _managed_review('image_to_tensor_node',
    'image', 'tensor',
    'isaac_ros_dnn_inference/isaac_ros_tensor_proc/src/image_to_tensor_node.cpp')

IMAGE_TENSOR_NORMALIZE_REVIEW = _managed_review('image_tensor_normalize_node',
    'tensor', 'normalized_tensor',
    'isaac_ros_dnn_inference/isaac_ros_tensor_proc/src/image_tensor_normalize_node.cpp')

INTERLEAVED_TO_PLANAR_REVIEW = _nitros_1in1out_review('interleaved_to_planar_node',
    'interleaved_tensor', 'planar_tensor',
    'isaac_ros_dnn_inference/isaac_ros_tensor_proc/src/interleaved_to_planar_node.cpp')

RESHAPE_REVIEW = _nitros_1in1out_review('reshape_node',
    'tensor', 'reshaped_tensor',
    'isaac_ros_dnn_inference/isaac_ros_tensor_proc/src/reshape_node.cpp')
