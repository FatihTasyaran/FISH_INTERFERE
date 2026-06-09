"""Shared per-node reviews used across all A2 benchmark sheets.

Container, launch_ros, Controller, DataLoaderNode are IDENTICAL across all
ros2_benchmark-based Isaac ROS benchmarks. NitrosPlaybackNode varies by
data_formats (2 for image-based, 1+ for tensor pipelines). NitrosMonitorNode
varies by params (5 distinct paths).
"""

from a2_apriltag_data import (
    REFERENCE_VCC,                # shared catalog
    CONTAINER_REVIEW,              # ComponentManager — same source/lines
    CONTROLLER_REVIEW,             # rclpy Controller — same source/lines
    LAUNCH_ROS_REVIEW,             # rclpy launch_ros agent
    DATALOADER_NODE_REVIEW,        # ros2_benchmark::DataLoaderNode — same source/lines
    PLAYBACK_PARENT_REVIEW,        # ros2_benchmark::PlaybackNode base class
    MONITOR_PARENT_REVIEW,         # ros2_benchmark::MonitorNode base class
    ROS2CLI_HELPERS_NOTES,
)

def make_nitros_playback_review(data_formats):
    """Per-bench NitrosPlaybackNode review. data_formats is a list of NITROS supported_type_names."""
    rows = list(PLAYBACK_PARENT_REVIEW)
    rows.append([6, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '46',
                 ': ros2_benchmark::PlaybackNode("NitrosPlaybackNode", options) — delegating ctor (no parent generic pub/sub)',
                 '(parent body invoked above)', '—', ''])
    rows.append([7, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '56-65',
                 'nitros_type_manager_ + registerSupportedType<...>() × 10',
                 '(no vertex — type registration only)', '—', ''])
    rows.append([8, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '69-76',
                 f'for each data_format → CreateNitrosPubSub(data_format, index) [{len(data_formats)}× for this bench: {", ".join(data_formats)}]',
                 '—', '—',
                 'All formats NITROS-registered → CreateNitrosPubSub (not CreateGenericPubSub)'])
    rows.append([9, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '92-130',
                 '[per format] NitrosPublisher NEGOTIATED on "inputN" + nitros_pub->start()',
                 'VCCI13 + VCCI14 + VCCI15',
                 '+1 pub_aspect (compat pub "inputN") + +2 E + 2 F (NegPub: graph_change_timer + _supported_types sub) + 1 pub_aspect (NegotiatedTopicsInfo pub)',
                 ''])
    rows.append([10, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_playback_node.cpp', '143-160',
                 '[per format] type_manager.createCompatibleSubscriberCallback(..., "buffer/inputN", ...)',
                 'VCCI8',
                 '+1 E + 1 F (recording sub on /buffer/inputN)', ''])
    return rows

def make_nitros_monitor_review(monitor_data_format, use_nitros_type_monitor_sub, monitor_topic_remap='output'):
    """Per-bench NitrosMonitorNode review. Returns review rows + which sub path taken."""
    rows = list(MONITOR_PARENT_REVIEW)
    rows.append([5, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '36',
                 ': ros2_benchmark::MonitorNode("NitrosMonitorNode", options) — delegating ctor',
                 '(parent body invoked above)', '—', ''])
    rows.append([6, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '47-58',
                 'nitros_type_manager_ + registerSupportedType<...>() × 10',
                 '(no vertex)', '—', ''])

    # Decide path: hasFormat(monitor_data_format) + use_nitros_type_monitor_sub
    nitros_supported = {
        'nitros_image_bgr8', 'nitros_image_rgb8', 'nitros_image_mono8',
        'nitros_image_mono16', 'nitros_image_rgb16', 'nitros_image_bgr16',
        'nitros_image_bgra8', 'nitros_image_rgba8', 'nitros_image_nv12', 'nitros_image_nv24',
        'nitros_camera_info', 'nitros_disparity_image_32FC1',
        'nitros_detection2_d_array', 'nitros_detection3_d_array',
        'nitros_tensor_list_nchw', 'nitros_tensor_list_nhwc',
        'nitros_tensor_list_nchw_rgb_f32', 'nitros_point_cloud',
        'nitros_compressed_image', 'nitros_occupancy_grid',
        'nitros_pose_cov_stamped', 'nitros_flat_scan',
    }
    is_nitros = monitor_data_format in nitros_supported

    if not is_nitros:
        rows.append([7, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '60-68',
                     'CreateMonitorSubscriber() → hasFormat() false → falls to parent CreateGenericTypeMonitorSubscriber',
                     '—', '—',
                     f'monitor_data_format="{monitor_data_format}" is a ROS msg type, NOT a NITROS supported_type'])
        rows.append([8, 'ros2_benchmark/ros2_benchmark/src/monitor_node.cpp', '75',
                     f'monitor_sub_ = this->create_generic_subscription("{monitor_topic_remap}", monitor_data_format_, kQoS, monitor_subscriber_callback)',
                     'VCC_GS',
                     f'+1 E + 1 F (/{monitor_topic_remap})',
                     'POST-PATCH (97b0741 + 0743b32): GenericSubscription ctor emits the rclcpp tracepoints + callback_register'])
    elif use_nitros_type_monitor_sub:
        rows.append([7, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '60-78',
                     'CreateMonitorSubscriber() → hasFormat() true + use_nitros_type_monitor_sub=true → CreateNitrosMonitorSubscriber',
                     '—', '—',
                     f'monitor_data_format="{monitor_data_format}" is NITROS-supported'])
        rows.append([8, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '128-175',
                     f'CreateNitrosMonitorSubscriber: NitrosSubscriber NEGOTIATED config (compat_data_format={monitor_data_format}, topic_name="{monitor_topic_remap}"), nitros_sub_->start()',
                     'VCCI6 → VCCI8 + VCCI9', '—', ''])
        rows.append([9, 'nitros_subscriber.cpp:223 + nitros_format_agent.hpp:367',
                     f'compat sub on /{monitor_topic_remap} ({monitor_data_format} type)',
                     'VCCI8', '+1 E + 1 F', ''])
        rows.append([10, 'negotiated_subscription.cpp:71-105 ctor',
                     f'NegotiatedSubscription on /{monitor_topic_remap}/nitros',
                     'VCCI9', '+1 E + 1 F + 1 pub_aspect', ''])
    else:
        rows.append([7, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '60-100',
                     'CreateMonitorSubscriber() → hasFormat() true + use_nitros_type_monitor_sub=false → CREATE_ROS_TYPE_MONITOR_HELPER(<T>) → CreateROSTypeMonitorSubscriber<T>()',
                     '—', '—',
                     f'Maps NITROS type "{monitor_data_format}" to its underlying ROS type via getROSTypeName()'])
        rows.append([8, 'isaac_ros_benchmark/isaac_ros_benchmark/src/nitros_monitor_node.cpp', '116',
                     f'monitor_sub_ = create_subscription<ROSMessageType>("{monitor_topic_remap}", ros2_benchmark::kQoS, monitor_subscriber_callback, sub_options)',
                     'VCC2',
                     f'+1 E + 1 F (/{monitor_topic_remap})', ''])
    return rows
