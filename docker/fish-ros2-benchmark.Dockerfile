# syntax=docker/dockerfile:1.6
# =============================================================================
# fish-ros2-benchmark — fish-base + a built ros2_benchmark workspace
# =============================================================================
# Stacks the ros2_benchmark / Isaac ROS workspace on top of fish-base so a
# single `docker run` lands in a container with EVERYTHING pre-built:
#   - CUDA toolkit + nvcc + VPI 4 + CMake 4 + nsys (from fish-base)
#   - ROS 2 Humble + FISH framework (from fish-base)
#   - ros2_benchmark / isaac_ros_common / apriltag_ros workspace, source-
#     cloned at release-3.2, patches applied, colcon-built (this layer)
#
# Build modes (selected via ARG SETUP_MODE):
#
#   cpu   (default)   Christian Rauch apriltag_ros + image_proc only.
#                     Sufficient for the CPU AprilTag benchmark.
#
#   gpu               + isaac_ros_apriltag, isaac_ros_benchmark,
#                     isaac_ros_nitros, isaac_ros_image_pipeline.
#                     Required for the NITROS-accelerated GPU AprilTag
#                     benchmark.
#
# Build:
#   docker build -t fish-ros2-benchmark:cpu -f docker/fish-ros2-benchmark.Dockerfile .
#   docker build -t fish-ros2-benchmark:gpu \
#       --build-arg SETUP_MODE=gpu \
#       -f docker/fish-ros2-benchmark.Dockerfile .
#
# Run:
#   docker run -dit --name r2b \
#       --runtime=nvidia --gpus all --privileged --net host \
#       -v $HOME/r2bdataset2023_v3:/root/r2bdataset2023_v3:ro \
#       -v $HOME/fish_traces:/root/fish_traces \
#       fish-ros2-benchmark:cpu
#   docker exec -it r2b bash
#
# Inside:
#   source $R2B_WS_HOME/install/setup.bash
#   launch_test $R2B_WS_HOME/src/ros2_benchmark/scripts/apriltag_ros_apriltag_node.py
# =============================================================================

ARG FISH_BASE_TAG=cuda12.6-humble
FROM fish-base:${FISH_BASE_TAG}

ARG SETUP_MODE=cpu

# Network access during build is required (git clone). On hermetic CI, skip
# this layer and build the workspace at run time via docker exec instead.
COPY scripts/setup_isaac_workspace.sh /tmp/setup_isaac_workspace.sh

# Reuse the bash login env so /etc/profile.d/ros-fish.sh sources ROS + the
# trace overlay. --datasets is intentionally pointed at a non-existent path
# so the script's symlink stage is a no-op at build time — datasets are
# always mounted at run time, never baked into the image.
RUN bash -lc "bash /tmp/setup_isaac_workspace.sh --${SETUP_MODE} --datasets /nonexistent" && \
    rm -f /tmp/setup_isaac_workspace.sh

ENV R2B_WS_HOME=/root/ros_ws \
    ROS2_BENCHMARK_OVERRIDE_ASSETS_ROOT=/root/ros_ws/src/ros2_benchmark/assets

# Auto-source the built workspace for new shells. /etc/profile.d/ros-fish.sh
# already sources ROS + the FISH trace overlay; this extends that.
RUN printf '%s\n' \
    '# fish-ros2-benchmark — auto-source the workspace' \
    'if [ -f /root/ros_ws/install/setup.bash ]; then' \
    '    source /root/ros_ws/install/setup.bash' \
    'fi' \
    > /etc/profile.d/ros2-benchmark.sh && \
    chmod +x /etc/profile.d/ros2-benchmark.sh

WORKDIR /root/ros_ws
