"""VISUAL_SLAM-NODE-A2 per-node review data."""

VISUAL_SLAM_REVIEW = [
    [1, 'isaac_ros_visual_slam/isaac_ros_visual_slam/src/visual_slam_node.cpp', '~80',
     ': rclcpp::Node("visual_slam_node", options)',
     'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect', 'Plain rclcpp::Node (NOT NitrosNode)'],
    [2, 'visual_slam_node.cpp', '110-111',
     'localize_in_map_callback_group_(create_callback_group(MutuallyExclusive))',
     '(no VCC — cbgroup only)', 'no vertex', ''],
    [3, 'visual_slam_node.cpp', '113-127',
     'image_subs_ = num_cameras_ × ManagedNitrosSubscriber<NitrosImageView>(this, "visual_slam/image_<i>", nitros_image_rgb8_t::supported_type_name, ...)',
     'VCCI17 → 18 → VCCI6 → VCCI8 + VCCI9 (×num_cameras)',
     'For num_cameras=2: +4 E + 4 F + 2 pub_aspect (compat sub /image_<i> + NegSub /image_<i>/nitros each)', ''],
    [4, 'visual_slam_node.cpp', '128-141',
     'camera_info_subs_ = num_cameras_ × create_subscription<CameraInfoType>("visual_slam/camera_info_<i>", ...)',
     'VCC2 ×num_cameras',
     'For num_cameras=2: +2 E + 2 F', ''],
    [5, 'visual_slam_node.cpp', '142-148',
     'imu_sub_ = enable_imu_fusion_ ? create_subscription<ImuType>("visual_slam/imu", ...) : nullptr',
     'VCC2 (conditional)',
     'If enable_imu_fusion_=false (default): +0; if true: +1 E + 1 F',
     'For r2b benchmark default: enable_imu_fusion=false → no IMU sub'],
    [6, 'visual_slam_node.cpp', '149-158',
     'initial_pose_sub_ = create_subscription<PoseWithCovarianceStampedType>("visual_slam/initial_pose", QoS(ServicesQoS()), CallbackInitialPose, {callback_group=localize_in_map})',
     'VCC2',
     '+1 E + 1 F', ''],
    [7, 'visual_slam_node.cpp', '161-233',
     '21 create_publisher<...> calls: visual_slam_status, vo_pose, vo_pose_covariance, tracking_vo_pose, vo_path, slam_path, vis/landmarks_cloud, vis/observations_cloud, vis/loop_closure_cloud, etc.',
     'VCC3 × ~21',
     '~21 pub_aspect (no E)', 'pub is an aspect on N, not a callback-binder'],
    [8, 'visual_slam_node.cpp', '239',
     'create_service<SrvReset>("visual_slam/reset", ...)',
     'VCC4', '+1 E + 1 F', ''],
    [9, 'visual_slam_node.cpp', '244',
     'create_service<SrvGetAllPoses>("visual_slam/get_all_poses", ...)',
     'VCC4', '+1 E + 1 F', ''],
    [10, 'visual_slam_node.cpp', '249',
     'create_service<SrvSetSlamPose>("visual_slam/set_slam_pose", ...)',
     'VCC4', '+1 E + 1 F', ''],
    [11, 'visual_slam_node.cpp', '254-259',
     'create_service<SrvFilePath>("visual_slam/save_map", ...) + create_service<SrvFilePath>("visual_slam/load_map", ...)',
     'VCC4 ×2', '+2 E + 2 F', ''],
    [12, 'visual_slam_node.cpp', '264',
     'create_service<SrvLocalizeInMap>("visual_slam/localize_in_map", ..., callback_group=localize_in_map)',
     'VCC4', '+1 E + 1 F', ''],
]
# Total VisualSlamNode (num_cameras=2, enable_imu_fusion=false):
# E = 7 (VCC1) + 4 (2 image ManagedSub) + 2 (2 camera_info plain sub) + 1 (initial_pose) + 6 (user services) = 20
# F = same = 20
# pub_aspect = 2 (VCC1) + 2 (2 ManagedSub _supported_types) + 21 (create_publisher) = 25
VISUAL_SLAM_NODE_EXPECTED = (20, 20, 25)

TRANSFORM_LISTENER_REVIEW = [
    [1, 'tf2_ros/tf2_ros/src/transform_listener.cpp (tf2_ros package, internal)',
     'rclcpp::Node ctor with NodeOptions(parameter_overrides + start_parameter_services=false)',
     '(VCC1.tf — minimal rclcpp Node)',
     '+1 N + 1 E + 1 F (/parameter_events sub via TimeSource only — no param svc) + 1 pub_aspect (/rosout)',
     'NodeOptions explicitly disables parameter services'],
    [2, 'tf2_ros/transform_listener.cpp', 'sub setup',
     'sub_tf_ = create_subscription<TFMessage>("/tf", ...)',
     'VCC2', '+1 E + 1 F', ''],
    [3, 'tf2_ros/transform_listener.cpp', 'sub setup',
     'sub_tf_static_ = create_subscription<TFMessage>("/tf_static", ...)',
     'VCC2', '+1 E + 1 F', ''],
]

STATIC_TRANSFORM_PUBLISHER_REVIEW = [
    [1, 'tf2_ros/tf2_ros/src/static_transform_broadcaster_node.cpp',
     ': rclcpp::Node("static_transform_publisher", options)', 'VCC1',
     '+1 N + 7 E + 7 F + 2 pub_aspect', ''],
    [2, 'tf2_ros/static_transform_broadcaster_node.cpp', 'pub setup',
     'create_publisher<TFMessage>("/tf_static", QoS().transient_local(), ...)',
     'VCC3', '+1 pub_aspect (no E)', ''],
]
