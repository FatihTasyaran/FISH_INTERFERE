# =============================================================================
# fish-r2b-stereo_image_proc-custom.Dockerfile
# =============================================================================
# Drops a multi-executor variant of isaac_ros_disparity_graph.py into the
# existing fish-r2b-stereo_image_proc image so the same workspace can run the
# original benchmark *or* the custom one-container-per-node variant.
#
# Build:
#   docker build -t fish-r2b-stereo_image_proc-custom:latest \
#                -f docker/fish-r2b-stereo_image_proc-custom.Dockerfile .
#
# Run the custom variant (inside the container):
#   launch_test src/isaac_ros_benchmark/benchmarks/isaac_ros_stereo_image_proc_benchmark/scripts/isaac_ros_disparity_graph_custom.py
# =============================================================================
FROM fish-r2b-stereo_image_proc:latest

# Drop the custom script into BOTH the src tree (so colcon/launch_test sees it)
# and the install/share copy (which is what gets resolved at runtime via the
# package's installed scripts/ path).
COPY docker/custom_scripts/isaac_ros_disparity_graph_custom.py \
     /root/ros_ws/src/isaac_ros_benchmark/benchmarks/isaac_ros_stereo_image_proc_benchmark/scripts/isaac_ros_disparity_graph_custom.py
COPY docker/custom_scripts/isaac_ros_disparity_graph_custom.py \
     /root/ros_ws/install/isaac_ros_stereo_image_proc_benchmark/share/isaac_ros_stereo_image_proc_benchmark/scripts/isaac_ros_disparity_graph_custom.py

# Mark the variant so introspection knows this image differs from stock.
LABEL fish.benchmark.variant="one_container_per_node"
LABEL fish.benchmark.base="fish-r2b-stereo_image_proc:latest"
