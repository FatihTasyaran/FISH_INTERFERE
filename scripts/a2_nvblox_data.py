"""NVBLOX-NODE-A2 per-node review data."""

NVBLOX_NODE_REVIEW = [
    [1, 'isaac_ros_nvblox/nvblox_ros/src/lib/nvblox_node.cpp', '~100',
     ': rclcpp::Node("nvblox_node", options)', 'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect', 'Plain rclcpp::Node (NOT NitrosNode)'],
    [2, 'nvblox_node.cpp', '~250-265',
     '2× ManagedNitrosMessageFilters Subscriber<NitrosView> (depth and left rgb) + 2× message_filters::Subscriber<CameraInfo> (depth_camera_info, left_camera_info) via image_exact_sync',
     'VCCI17 (×2) → 2× (VCCI8+VCCI9)  +  2× VCC2',
     '+4 E + 4 F + 2 pub_aspect (NITROS msg filters) + 2 E + 2 F (plain camera_info subs)',
     'For r2b benchmark: 2 NITROS image inputs (depth + left rgb) + 2 camera_info inputs paired via exact-time sync. use_color=true, use_depth=true default.'],
    [3, 'nvblox_node.cpp', '345-348',
     'if (use_lidar) pointcloud_sub_ = create_subscription<PointCloud2>("pointcloud", input_qos, ...)',
     'VCC2 (conditional)',
     'For r2b nvblox bench: use_lidar=true → +1 E + 1 F (/pointcloud sub)',
     ''],
    [4, 'nvblox_node.cpp', '351',
     'transform_sub_ = create_subscription<TransformStamped>("transform", kQueueSize, ...)',
     'VCC2', '+1 E + 1 F (/transform sub)', ''],
    [5, 'nvblox_node.cpp', '354',
     'pose_sub_ = create_subscription<PoseStamped>("pose", 10, ...)',
     'VCC2', '+1 E + 1 F (/pose sub)', ''],
    [6, 'nvblox_node.cpp', '363-409',
     '~16 create_publisher<...> calls (static_esdf_pointcloud, static_map_slice, static_occupancy_grid, dynamic_occupancy_grid, combined_occupancy_grid, esdf_slice_bounds, workspace_bounds, shapes_to_clear, pessimistic_*, dynamic_color_frame_overlay, dynamic_points, dynamic_depth_frame_overlay, dynamic_esdf_pointcloud, dynamic_map_slice, combined_esdf_pointcloud, combined_map_slice)',
     'VCC3 × ~16',
     '~16 pub_aspect',
     'pubs are aspects, not E vertices'],
    [7, 'nvblox_node.cpp', '417-441',
     '6 create_service: save_ply, save_map, load_map, save_rates, save_timings, send_esdf_and_gradient',
     'VCC4 ×6', '+6 E + 6 F', ''],
    [8, 'nvblox_node.cpp', '450',
     'wall_timer create_wall_timer(period, &NvbloxNode::tick)',
     'VCC6', '+1 E + 1 F (10ms timer)', ''],
]
# Total NvbloxNode (use_color=true, use_depth=true, use_lidar=true defaults):
# E = 7 (VCC1) + 4 (2 ManagedSub) + 2 (2 camera_info) + 1 (pointcloud) + 1 (transform) + 1 (pose) + 6 (user services) + 1 (timer) = 23
# F = same = 23
NVBLOX_NODE_EXPECTED = (23, 23, 24)


# ImageFormatConverterNode review (used by PrepLeftImageFormatConverter + PrepRightImageFormatConverter)
IMAGE_FORMAT_CONVERTER_REVIEW = [
    [1, 'isaac_ros_image_pipeline/isaac_ros_image_proc/src/image_format_converter_node.cpp', 'top',
     'INPUT_COMPONENT_KEY + OUTPUT_COMPONENT_KEY',
     '(constants)', '—', '2 CONFIG_MAP keys: 1 NEGOTIATED in + 1 NEGOTIATED out (image + camera_info-less since no camera_info path)'],
    [2, 'image_format_converter_node.cpp', 'CONFIG_MAP',
     'CONFIG_MAP: 2 NEGOTIATED (image_raw → image)', '(chain)', '—', ''],
    [3, 'image_format_converter_node.cpp', 'ctor',
     ': nitros::NitrosNode(...)', 'VCCI1 → VCC1', '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
    [4, 'image_format_converter_node.cpp', 'startNitrosNode',
     'startNitrosNode()', 'VCCI2', '+1 E + 1 F (negotiation_timer)', ''],
    [5, 'nitros chain (1 input × VCCI8+VCCI9)', 'compat sub + NegSub',
     '+2 E + 2 F + 1 pub_aspect', 'VCCI8 + VCCI9', '', ''],
    [6, 'nitros chain (1 output × VCCI14+VCCI15)', 'compat pub + NegPub',
     '+2 E + 2 F + 2 pub_aspect', 'VCCI14 + VCCI15', '', ''],
    [7, 'nitros_node.cpp:721 (RUNTIME)', 'gxf_heartbeat_timer',
     '+1 E + 1 F', 'VCC_GHB', '', ''],
]
