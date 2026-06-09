#!/usr/bin/env python3
"""APRILTAG-NODE-A2 source-review data.

Per-node, fully verified VCC mapping from source code. Used to build the
expected_vertex + per-node tabs of the A2 spreadsheet.
"""

REFERENCE_VCC = [
    ['Code', 'Layer / Class', 'Call', 'FISH contribution', 'pub_aspect', 'Source / Notes'],
    ['VCC1',     'rclcpp::Node ctor', 'Node(name, options)',
        '+1 N + 7 E + 7 F', '+2 (/rosout, /parameter_events)',
        '/opt/ros/humble/include/rclcpp/rclcpp/node.cpp — TimeSource auto-attach yields 1 /parameter_events sub; 6 boilerplate param services (set/get/list/describe_parameters, get_parameter_types, set_parameters_atomically); /rosout pub_aspect via Logging'],
    ['VCC1.rclpy', 'rclpy.node.Node ctor', 'Node(name)',
        '+1 N + 6 E + 6 F', '+2',
        'No TimeSource auto-attach → 1 less sub; 6 param services'],
    ['VCC1.cm', 'rclcpp_components::ComponentManager', 'ComponentManager()',
        '+1 N + 1 E + 1 F', '+1 (/rosout)',
        'rclcpp_components/src/component_manager.cpp — overrides start_parameter_services=false + start_parameter_event_publisher=false; keeps only TimeSource sub'],
    ['VCC2',  'rclcpp::Node', 'create_subscription<T>(topic, qos, cb)', '+1 E + 1 F', '—',
        'subscription.hpp:235-240 Subscription<T>::Subscription ctor fires rclcpp_subscription_init + rclcpp_subscription_callback_added'],
    ['VCC3',  'rclcpp::Node', 'create_publisher<T>(topic, qos)', '—', '+1 on caller N',
        'publisher.hpp Publisher ctor — pub is an aspect on N, not a callback-binder'],
    ['VCC4',  'rclcpp::Node', 'create_service<T>(name, cb, qos, group)', '+1 E + 1 F', '—',
        'service.hpp Service<T>::Service ctor'],
    ['VCC5',  'rclcpp::Node', 'create_client<T>(name, qos, group)', '—', '+1 cli_aspect on caller N',
        'client.hpp Client ctor — cli is an aspect, no callback-binder'],
    ['VCC6',  'rclcpp::Node', 'create_wall_timer(period, cb, group)', '+1 E + 1 F', '—',
        'create_timer.hpp — atomic fish_rclcpp_timer_init carries period_ns'],
    ['VCC_GS', 'rclcpp::Node', 'create_generic_subscription(topic, type, qos, cb)', '+1 E + 1 F', '—',
        'generic_subscription.hpp — FISH-patched GenericSubscription::GenericSubscription ctor (post-commit 97b0741) fires the same rclcpp_subscription_init + rclcpp_subscription_callback_added tracepoints as Subscription<T>'],
    ['VCC_GP', 'rclcpp::Node', 'create_generic_publisher(topic, type, qos)', '—', '+1 on caller N',
        'generic_publisher.hpp — symmetric to VCC3'],
    ['VCCI1', 'NitrosNode inheritance', ': public nitros::NitrosNode', 'no extra (triggers VCC1 once)', '—', 'nitros_node.cpp — subclass uses VCC1 via base ctor'],
    ['VCCI2', 'NitrosNode::startNitrosNode()', 'create_wall_timer(negotiation_timer, 100ms, cb)', '+1 E + 1 F', '—',
        'nitros_node.cpp:548 — one-shot 100ms timer that drives type negotiation; ALSO iterates nitros_pub_sub_groups vector'],
    ['VCCI3', 'NitrosPublisherSubscriberGroup ctor', 'std::make_shared<NPSG>(...)', 'no extra (triggers VCCI4 + VCCI5)', '—',
        'nitros_publisher_subscriber_group.cpp — container for inputs/outputs'],
    ['VCCI4', 'NPSG::createNitrosSubscribers()', 'loop on INPUT entries → 1× VCCI6 per input', '—', '—',
        'creates one NitrosSubscriber per CONFIG_MAP input'],
    ['VCCI5', 'NPSG::createNitrosPublishers()', 'loop on OUTPUT entries → 1× VCCI13 per output', '—', '—',
        'creates one NitrosPublisher per CONFIG_MAP output'],
    ['VCCI6', 'NitrosSubscriber ctor', 'std::make_shared<NitrosSubscriber>', 'triggers VCCI8 + VCCI9 (for type=NEGOTIATED)', '—',
        'nitros_subscriber.cpp:184-220 ctor + start path'],
    ['VCCI8', 'NitrosSubscriber::createCompatibleSubscriber → type_callback', 'node.create_subscription<MsgT>(topic, ...)', '+1 E + 1 F (compat sub on config.topic_name)', '—',
        'nitros_format_agent.hpp:356 createCompatibleSubscriberCallback; goes through VCC2 (or VCC_GS for typed-as-generic)'],
    ['VCCI9', 'NitrosSubscriber NEGOTIATED branch', 'std::make_shared<NegotiatedSubscription>(...)', '+1 E + 1 F (NegotiatedTopicsInfo sub on <t>/nitros)', '+1 (_supported_types pub)',
        'negotiated_subscription.cpp:71 ctor — line 83 NegotiatedTopicsInfo sub + line 90 _supported_types pub'],
    ['VCCI13', 'NitrosPublisher ctor', 'std::make_shared<NitrosPublisher>', 'triggers VCCI14 + VCCI15 (start)', '—',
        'nitros_publisher.cpp ctor + start'],
    ['VCCI14', 'NitrosPublisher::createCompatiblePublisher → type_callback', 'node.create_publisher<MsgT>(topic, ...)', '—', '+1 (compat pub on config.topic_name)',
        'nitros_format_agent.hpp createCompatiblePublisherCallback; goes through VCC3'],
    ['VCCI15', 'NitrosPublisher NEGOTIATED branch + start()', 'std::make_shared<NegotiatedPublisher>(...).start()', '+1 E + 1 F (graph_change_timer 100ms) + 1 E + 1 F (_supported_types sub on <t>/nitros/_supported_types)', '+1 (NegotiatedTopicsInfo pub on <t>/nitros)',
        'negotiated_publisher.cpp:290 ctor (line 316 NegotiatedTopicsInfo pub + line 324 graph_change_timer 100ms); start() (line 581) creates _supported_types sub'],
    ['VCCI17', 'NitrosMessageFilterSubscriber<T>.subscribe(...)', 'std::make_shared<ManagedNitrosSubscriber<T>>', 'no extra; triggers VCCI18', '—',
        'managed_nitros_message_filters_subscriber.hpp:subscribe() body'],
    ['VCCI18', 'ManagedNitrosSubscriber ctor', 'wraps NitrosSubscriber (=VCCI6 chain)', 'same as VCCI6', '—',
        'managed_nitros_subscriber.hpp ctor'],
    ['VCC_GHB', 'NitrosNode::postNegotiationCallback (runtime)', 'create_wall_timer(gxf_heartbeat_timer)', '+1 E + 1 F (post-negotiation, not ctor-time)', '—',
        'nitros_node.cpp:721 — gxf_heartbeat_timer fires only after type negotiation completes'],
]


# Per-node review for apriltag.
# Each row = (idx, source_file_relpath, line, exact_code, vcc_code, resulting_vertex, notes)

APRILTAG_NODE_REVIEW = [
    [1, 'isaac_ros_apriltag/isaac_ros_apriltag/src/apriltag_node.cpp', '549',
     ': rclcpp::Node("apriltag_node", options)',
     'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect (/rosout, /parameter_events)',
     'TimeSource auto-attach → /parameter_events sub via NodeTimeSource → AsyncParametersClient::on_parameter_event'],
    [2, 'isaac_ros_apriltag/isaac_ros_apriltag/src/apriltag_node.cpp', '558',
     'tf_pub_(create_publisher<tf2_msgs::msg::TFMessage>("tf", rclcpp::QoS(100)))',
     'VCC3',
     '+1 pub_aspect (/tf)',
     'Publisher is an aspect, no E vertex'],
    [3, 'isaac_ros_apriltag/isaac_ros_apriltag/src/apriltag_node.cpp', '559',
     'detections_pub_{create_publisher<AprilTagDetectionArray>("tag_detections", rclcpp::QoS(1))}',
     'VCC3',
     '+1 pub_aspect (/tag_detections → remapped to /apriltag_detections)',
     ''],
    [4, 'isaac_ros_apriltag/isaac_ros_apriltag/src/apriltag_node.cpp', '591',
     'image_sub_.subscribe(this, "image")',
     'VCCI17→18→6→{VCCI8,VCCI9}',
     '+2 E + 2 F + 1 pub_aspect (compat sub /image + NEGOTIATED sub /image/nitros + /image/nitros/_supported_types pub aspect)',
     'NitrosMessageFilterSubscriber<NitrosImageView> for nitros_image_bgr8; NEGOTIATED type'],
    [5, 'isaac_ros_apriltag/isaac_ros_apriltag/src/apriltag_node.cpp', '592',
     'camera_info_sub_.subscribe(this, "camera_info")',
     'VCC2 (via message_filters::Subscriber<CameraInfo>)',
     '+1 E + 1 F (/camera_info sub)',
     'Plain message_filters subscriber — no NITROS adapter'],
]
APRILTAG_NODE_EXPECTED = {'E': 7+2+1, 'F': 7+2+1, 'pub_aspect': 2+1+1+1}
# E = 7 (VCC1) + 2 (VCCI8+VCCI9 from image_sub) + 1 (camera_info sub) = 10
# F = 7 + 2 + 1 = 10
# pub_aspect = 2 (VCC1) + 1 (tf) + 1 (tag_detections) + 1 (image/nitros/_supported_types) = 5


DATALOADER_NODE_REVIEW = [
    [1, 'ros2_benchmark/ros2_benchmark/src/data_loader_node.cpp', '30',
     ': rclcpp::Node("DataLoader", options)',
     'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect',
     'TimeSource + 6 param svc'],
    [2, 'ros2_benchmark/ros2_benchmark/src/data_loader_node.cpp', '31',
     'service_callback_group_{create_callback_group(Reentrant)}',
     '(no VCC code — callback group only, no entity)',
     'no new vertex (fish_cbgroup_init aspect only)',
     ''],
    [3, 'ros2_benchmark/ros2_benchmark/src/data_loader_node.cpp', '33',
     'create_service<SetData>("set_data", &SetDataServiceCallback, ...)',
     'VCC4',
     '+1 E + 1 F (/set_data)',
     ''],
    [4, 'ros2_benchmark/ros2_benchmark/src/data_loader_node.cpp', '43',
     'create_service<StartLoading>("start_loading", &StartLoadingServiceCallback, ...)',
     'VCC4',
     '+1 E + 1 F (/start_loading)',
     ''],
    [5, 'ros2_benchmark/ros2_benchmark/src/data_loader_node.cpp', '53',
     'create_service<StopLoading>("stop_loading", &StopLoadingServiceCallback, ...)',
     'VCC4',
     '+1 E + 1 F (/stop_loading)',
     ''],
    [6, 'ros2_benchmark/ros2_benchmark/src/data_loader_node.cpp', '59',
     'create_service<GetTopicMessageTimestamps>("get_topic_message_timestamps", ...)',
     'VCC4',
     '+1 E + 1 F (/get_topic_message_timestamps)',
     ''],
]
DATALOADER_NODE_EXPECTED = {'E': 7+4, 'F': 7+4, 'pub_aspect': 2}
# E = 7 (VCC1) + 4 user services = 11; F = 11


PLAYBACK_PARENT_REVIEW = [
    [1, 'ros2_benchmark/ros2_benchmark/src/playback_node.cpp', '42',
     ': rclcpp::Node(node_name, options)',
     'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect',
     ''],
    [2, 'ros2_benchmark/ros2_benchmark/src/playback_node.cpp', '43',
     'service_callback_group_{create_callback_group(MutuallyExclusive)}',
     '(no VCC — cbgroup only)',
     'no new vertex',
     ''],
    [3, 'ros2_benchmark/ros2_benchmark/src/playback_node.cpp', '45',
     'create_service<StartRecording>("start_recording", ...)',
     'VCC4',
     '+1 E + 1 F',
     ''],
    [4, 'ros2_benchmark/ros2_benchmark/src/playback_node.cpp', '54',
     'create_service<StopRecording>("stop_recording", ...)',
     'VCC4',
     '+1 E + 1 F',
     ''],
    [5, 'ros2_benchmark/ros2_benchmark/src/playback_node.cpp', '63',
     'create_service<PlayMessages>("play_messages", ...)',
     'VCC4',
     '+1 E + 1 F',
     ''],
]

NITROSPLAYBACK_REVIEW_APRILTAG = PLAYBACK_PARENT_REVIEW + [
    [6, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '46',
     ': ros2_benchmark::PlaybackNode("NitrosPlaybackNode", options) → uses delegating ctor (no CreateGenericPubSub from parent public ctor)',
     '(parent body invoked above)',
     '—',
     'Uses the (name, options) base ctor, NOT the public one which calls CreateGenericPubSub'],
    [7, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '56-65',
     'nitros_type_manager_ = std::make_shared<NitrosTypeManager>(this); registerSupportedType<...>() × 10',
     '(no vertex — type registration only)',
     '—',
     'Registers NitrosImage, NitrosCameraInfo, NitrosDetection2DArray, etc.'],
    # For apriltag: data_formats = [nitros_image_bgr8, nitros_camera_info] = 2 formats
    [8, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '69-76',
     'for each data_format → CreateNitrosPubSub(data_format, index) [2× for apriltag: nitros_image_bgr8 + nitros_camera_info]',
     '—',
     '—',
     'Both formats are NITROS-registered → CreateNitrosPubSub (not CreateGenericPubSub)'],
    [9, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '92-130',
     '[per format] Builds NitrosPublisher NEGOTIATED on "inputN" then calls nitros_pub->start()',
     'VCCI13 + VCCI14 + VCCI15',
     '+1 pub_aspect (compat pub "inputN") + +2 E + 2 F (NegPub: graph_change_timer + _supported_types sub) + 1 pub_aspect (NegotiatedTopicsInfo pub "inputN/nitros")',
     'NegotiatedPublisher ctor + start(): graph_change_timer @100ms + /inputN/nitros/_supported_types sub'],
    [10, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '143-160',
     '[per format] nitros_type_manager_.getFormatCallbacks(data_format).createCompatibleSubscriberCallback(this, sub, "buffer/inputN", ...)',
     'VCCI8',
     '+1 E + 1 F (recording sub on /buffer/inputN)',
     'Compat-style subscriber for recording, dispatched through type-manager → node.create_subscription<MsgT>()'],
]
# For apriltag: 2 data formats (nitros_image_bgr8 + nitros_camera_info)
# Per format: +1 pub_aspect (compat) + +2 E + 2 F (NegPub) + 1 pub_aspect (NegPubTopicsInfo) + 1 E + 1 F (recording sub) = 3 E + 3 F + 2 pub_aspect
# 2 formats: 6 E + 6 F + 4 pub_aspect
PLAYBACK_NODE_EXPECTED = {'E': 7+3+6, 'F': 7+3+6, 'pub_aspect': 2+4}
# E = 7 (VCC1) + 3 (user services) + 6 (2 formats × 3) = 16; F = 16


MONITOR_PARENT_REVIEW = [
    [1, 'ros2_benchmark/ros2_benchmark/src/monitor_node.cpp', '26',
     ': rclcpp::Node(node_name, options)',
     'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect',
     ''],
    [2, 'ros2_benchmark/ros2_benchmark/src/monitor_node.cpp', '30',
     'service_callback_group_{create_callback_group(MutuallyExclusive)}',
     '(no VCC)',
     'no new vertex',
     ''],
    [3, 'ros2_benchmark/ros2_benchmark/src/monitor_node.cpp', '32',
     'create_service<StartMonitoring>("monitor_nodeX_start_monitoring", ...)',
     'VCC4',
     '+1 E + 1 F',
     'monitor_index_ defaults to 0 → topic "/monitor_node0_start_monitoring"'],
    [4, 'ros2_benchmark/ros2_benchmark/src/monitor_node.cpp', '42',
     'create_service<StopMonitoring>("monitor_nodeX_stop_monitoring", ...)',
     'VCC4',
     '+1 E + 1 F',
     ''],
]

NITROSMONITOR_REVIEW_APRILTAG = MONITOR_PARENT_REVIEW + [
    [5, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '36',
     ': ros2_benchmark::MonitorNode("NitrosMonitorNode", options) → uses delegating ctor (skips parent CreateGenericTypeMonitorSubscriber)',
     '(parent body invoked above)',
     '—',
     'Uses (name, options) ctor, NOT public one'],
    [6, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '47-58',
     'nitros_type_manager_ + registerSupportedType<...>() × 10',
     '(no vertex)',
     '—',
     ''],
    [7, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '60',
     'CreateMonitorSubscriber()',
     '(branches based on params)',
     '—',
     'For apriltag: monitor_data_format = "isaac_ros_apriltag_interfaces/msg/AprilTagDetectionArray" (a ROS msg name, NOT a NITROS supported_type), use_nitros_type_monitor_sub = False. Then hasFormat() returns false → falls to parent CreateGenericTypeMonitorSubscriber.'],
    [8, 'ros2_benchmark/ros2_benchmark/src/monitor_node.cpp', '75',
     'monitor_sub_ = this->create_generic_subscription("output", monitor_data_format_, kQoS, monitor_subscriber_callback)',
     'VCC_GS',
     '+1 E + 1 F (/output → remapped to /apriltag_detections)',
     'AFTER FISH patch 97b0741: GenericSubscription ctor fires rclcpp_subscription_init + rclcpp_subscription_callback_added. Without patch, rcl_subscription_init fires but rclcpp_* do not — that was the original A0 ΔF=-1 bug.'],
]
MONITOR_NODE_EXPECTED = {'E': 7+2+1, 'F': 7+2+1, 'pub_aspect': 2}
# E = 7 (VCC1) + 2 (user services) + 1 (generic sub) = 10; F = 10


RESIZE_REVIEW = [
    [1, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '46-55',
     'INPUT_CAM_COMPONENT_KEY="sync/camera_info_in"; INPUT_COMPONENT_KEY="sync/image_in"; OUTPUT_COMPONENT_KEY="image_sink/sink"; OUTPUT_CAM_COMPONENT_KEY="camera_info_sink/sink"',
     '(no VCC — constants only)',
     '—',
     '4 CONFIG_MAP keys total: 2 inputs (camera_info, image) + 2 outputs (resized image, resized camera_info)'],
    [2, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '82-117',
     'CONFIG_MAP: 4 entries all type=NEGOTIATED — INPUT_CAM_*=camera_info, INPUT_*=image, OUTPUT_*=resize/image, OUTPUT_CAM_*=resize/camera_info',
     '(CONFIG_MAP describes the chain that VCCI4+VCCI5 will instantiate)',
     '—',
     'topic_names remapped at launch: image→data_loader/image, camera_info→data_loader/camera_info, resize/image→buffer/image, resize/camera_info→buffer/camera_info'],
    [3, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '136-145',
     ': nitros::NitrosNode(options, APP_YAML_FILENAME, CONFIG_MAP, PRESET_EXTENSION_SPEC_NAMES, EXTENSION_SPEC_FILENAMES, GENERATOR_RULE_FILENAMES, EXTENSIONS, PACKAGE_NAME)',
     'VCCI1 → VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect (rclcpp::Node base via NitrosNode)',
     'NitrosNode is rclcpp::Node subclass'],
    [4, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/resize_node.cpp', '230',
     'startNitrosNode()',
     'VCCI2',
     '+1 E + 1 F (negotiation_timer 100ms)',
     'Single negotiation_timer for the whole pub/sub group'],
    [5, 'nitros_node.cpp', 'startNitrosNode',
     'iterates nitros_pub_sub_groups_; per group: createNitrosSubscribers + createNitrosPublishers',
     'VCCI4 + VCCI5',
     '—',
     'Per CONFIG_MAP entry: 1 NitrosSubscriber (VCCI6) per input + 1 NitrosPublisher (VCCI13) per output'],
    # 2 inputs (INPUT_CAM + INPUT) → 2× (VCCI6 → VCCI8+VCCI9):
    [6, 'nitros_subscriber.cpp:223 + nitros_format_agent.hpp:367-372', 'input #0 (camera_info, INPUT_CAM_COMPONENT_KEY)',
     'createCompatibleSubscriberCallback → node.create_subscription<sensor_msgs::msg::CameraInfo>(/data_loader/camera_info, ...)',
     'VCCI8',
     '+1 E + 1 F (compat sub on /data_loader/camera_info)',
     ''],
    [7, 'negotiated_subscription.cpp:83+90', 'input #0 NegotiatedSubscription for camera_info NEGOTIATED branch',
     'make_shared<NegotiatedSubscription>("/data_loader/camera_info/nitros")',
     'VCCI9',
     '+1 E + 1 F (NegotiatedTopicsInfo sub) + 1 pub_aspect (_supported_types pub)',
     ''],
    [8, 'nitros_subscriber.cpp:223 + nitros_format_agent.hpp:367-372', 'input #1 (image, INPUT_COMPONENT_KEY)',
     'createCompatibleSubscriberCallback → node.create_subscription<NitrosImage>(/data_loader/image, ...) [or compat type]',
     'VCCI8',
     '+1 E + 1 F (compat sub on /data_loader/image)',
     ''],
    [9, 'negotiated_subscription.cpp:83+90', 'input #1 NegotiatedSubscription for image NEGOTIATED branch',
     'make_shared<NegotiatedSubscription>("/data_loader/image/nitros")',
     'VCCI9',
     '+1 E + 1 F + 1 pub_aspect',
     ''],
    # 2 outputs (OUTPUT_CAM + OUTPUT) → 2× (VCCI13 → VCCI14+VCCI15):
    [10, 'nitros_publisher.cpp + nitros_format_agent.hpp', 'output #0 (resize/image → buffer/image)',
     'createCompatiblePublisherCallback → node.create_publisher<NitrosImage>(/buffer/image, ...)',
     'VCCI14',
     '+1 pub_aspect (compat pub on /buffer/image)',
     ''],
    [11, 'negotiated_publisher.cpp:290 ctor + 581 start', 'output #0 NegotiatedPublisher /buffer/image/nitros',
     'NegotiatedPublisher(/buffer/image/nitros) ctor + start()',
     'VCCI15',
     '+1 pub_aspect (NegotiatedTopicsInfo pub) + +1 E + 1 F (graph_change_timer 100ms) + +1 E + 1 F (_supported_types sub)',
     'ctor:316 NegotiatedTopicsInfo pub; ctor:324 graph_change_timer (100ms); start:585 _supported_types sub'],
    [12, 'nitros_publisher.cpp + nitros_format_agent.hpp', 'output #1 (resize/camera_info → buffer/camera_info)',
     'createCompatiblePublisherCallback → node.create_publisher<NitrosCameraInfo>(/buffer/camera_info, ...)',
     'VCCI14',
     '+1 pub_aspect (compat pub on /buffer/camera_info)',
     ''],
    [13, 'negotiated_publisher.cpp:290 ctor + 581 start', 'output #1 NegotiatedPublisher /buffer/camera_info/nitros',
     'NegotiatedPublisher(/buffer/camera_info/nitros) ctor + start()',
     'VCCI15',
     '+1 pub_aspect + +1 E + 1 F + +1 E + 1 F',
     ''],
    # Runtime extension:
    [14, 'nitros_node.cpp:721 postNegotiationCallback (RUNTIME, not ctor)',
     '[runtime] create_wall_timer(gxf_heartbeat_timer)',
     'VCC_GHB',
     '+1 E + 1 F (runtime, after type negotiation completes)',
     'Not part of static ctor model; classed as runtime delta'],
]
# Static (ctor-time): VCC1 (7+7) + VCCI2 (1+1) + 2 inputs × (2+2) + 2 outputs × (2+2) = 7+1+4+4 = 16 E, 16 F
# pub_aspect: 2 (VCC1) + 2 (VCCI9 ×2 inputs) + 2 (VCCI14 ×2 outputs) + 2 (VCCI15 ×2 outputs) = 8
RESIZE_NODE_EXPECTED_CTOR = {'E': 16, 'F': 16, 'pub_aspect': 8}
RESIZE_NODE_EXPECTED_RUNTIME = {'E': +1, 'F': +1, 'pub_aspect': 0}  # gxf_heartbeat_timer


CONTAINER_REVIEW = [
    [1, 'rclcpp_components/src/component_manager.cpp', '37-50',
     'ComponentManager()',
     'VCC1.cm',
     '+1 N + 1 E + 1 F + 1 pub_aspect (/rosout)',
     'NodeOptions override: start_parameter_services=false, start_parameter_event_publisher=false → no 6 param svc, no /parameter_events pub; KEEPS TimeSource sub on /parameter_events'],
    [2, 'rclcpp_components/src/component_manager.cpp', 'service registration',
     'create_service<LoadNode>("_container/load_node", ...) + UnloadNode + ListNodes',
     'VCC4 ×3',
     '+3 E + 3 F (/_container/load_node, /_container/unload_node, /_container/list_nodes)',
     ''],
]
CONTAINER_EXPECTED = {'E': 1+3, 'F': 1+3, 'pub_aspect': 1}


CONTROLLER_REVIEW = [
    [1, 'ros2_benchmark/ros2_benchmark/ros2_benchmark/ros2_benchmark_test.py', '144',
     'self.node = rclpy.create_node("Controller", namespace=self.generate_namespace())',
     'VCC1.rclpy',
     '+1 N + 6 E + 6 F + 2 pub_aspect',
     '6 param services (rclpy does NOT auto-attach TimeSource → no /parameter_events sub)'],
    [2, 'ros2_benchmark/ros2_benchmark/ros2_benchmark/ros2_benchmark_test.py', '536-680',
     'ServiceClient.create_service_client_blocking(...) — for set_data, start_loading, stop_loading, start_recording, stop_recording, play_messages, get_topic_message_timestamps, start_monitoring, stop_monitoring',
     'VCC5 (×N)',
     '+N cli_aspect (no E, no F)',
     'rclpy service clients are aspects, not callback-binders'],
]
CONTROLLER_EXPECTED = {'E': 6, 'F': 6, 'pub_aspect': 2}


LAUNCH_ROS_REVIEW = [
    [1, '(launch_ros internal rclpy node — not user code)', 'N/A',
     'rclpy Node("launch_ros_<pid>")',
     'VCC1.rclpy',
     '+1 N + 6 E + 6 F + 2 pub_aspect',
     'Auto-spawned by launch_ros for its lifecycle; same as Controller'],
]
LAUNCH_ROS_EXPECTED = {'E': 6, 'F': 6, 'pub_aspect': 2}


# Aggregate expected vs actual for apriltag (from previous validated run + this new run)
APRILTAG_NODES_EXPECTED = {
    '/r2b/container':       CONTAINER_EXPECTED,
    '/launch_ros_<pid>':    LAUNCH_ROS_EXPECTED,
    '/r2b/Controller':      CONTROLLER_EXPECTED,
    '/r2b/DataLoaderNode':  DATALOADER_NODE_EXPECTED,
    '/r2b/MonitorNode':     MONITOR_NODE_EXPECTED,
    '/r2b/PlaybackNode':    PLAYBACK_NODE_EXPECTED,
    '/r2b/AprilTagNode':    APRILTAG_NODE_EXPECTED,
    '/r2b/PrepResizeNode':  {'E': RESIZE_NODE_EXPECTED_CTOR['E'] + RESIZE_NODE_EXPECTED_RUNTIME['E'],
                              'F': RESIZE_NODE_EXPECTED_CTOR['F'] + RESIZE_NODE_EXPECTED_RUNTIME['F'],
                              'pub_aspect': RESIZE_NODE_EXPECTED_CTOR['pub_aspect']},
}


# ros2cli helper bookkeeping — single tab in A2 with sub-tables explaining what each does.
ROS2CLI_HELPERS_NOTES = [
    # Each is an rclpy short-lived node that spawns when ros2 launch / benchmark framework issues service calls.
    # All match VCC1.rclpy = 6 E + 6 F + 2 pub_aspect, with optional 1 wall-timer / 1 sub for specific tools.
    [
        ['Section', 'Description', 'Per-helper baseline E', 'Per-helper baseline F', 'Notes'],
        ['ros2cli daemon', 'Single long-lived rclpy daemon spawned by `ros2 daemon start`. Pre-exists in any session that uses ros2 CLI tools.', 'VCC1.rclpy baseline + 1 timer (heartbeat) = 1 E + 1 F (observed in trace as minimal because the daemon process appears with only the daemon-specific timer + minimal services).', '1', 'Observed in apriltag: /_ros2cli_daemon_0_<hash> with E=1, F=1.'],
        ['ros2cli ephemeral nodes', 'Short-lived rclpy nodes spawned by `ros2 service call`, `ros2 topic echo`, etc. Each typically has 2 E + 2 F (1 service-call helper, 1 timer; in some cases 1 sub if `topic echo` was used).', '2', '2', 'Observed in apriltag run: 9 such nodes (/_ros2cli_1301, _1498, _1525, _1605, _1808, _1911, _1934, _1957, _1980). Most spawned by the Controller making service calls to set_data, start_loading, play_messages, start_monitoring, etc.'],
    ],
]


if __name__ == '__main__':
    # Quick sanity print
    for nname, exp in APRILTAG_NODES_EXPECTED.items():
        print(f'{nname:30s}  E={exp["E"]:3d}  F={exp["F"]:3d}  pub_aspect={exp["pub_aspect"]:3d}')
    print(f'TOTAL (user/system nodes):',
          f"E={sum(e['E'] for e in APRILTAG_NODES_EXPECTED.values())}",
          f"F={sum(e['F'] for e in APRILTAG_NODES_EXPECTED.values())}",
          f"pub_aspect={sum(e['pub_aspect'] for e in APRILTAG_NODES_EXPECTED.values())}")
