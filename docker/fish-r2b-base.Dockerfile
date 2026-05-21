# syntax=docker/dockerfile:1.6
# =============================================================================
# fish-r2b-base — fish-base + COMMON Isaac ROS workspace baked in
# =============================================================================
# Sits between fish-base and per-benchmark images. Every benchmark needs:
#   ros2_benchmark, isaac_ros_common, isaac_ros_nitros,
#   isaac_ros_image_pipeline (NITROS variant), vision_opencv (humble),
#   image_pipeline (humble), apriltag_ros, isaac_ros_benchmark
# Cloning + building these inside every per-benchmark image is ~15 minutes
# of duplicated work × 22 benchmarks = ~5.5 hours wasted.
#
# Pre-baking them once here means per-benchmark images only need to clone
# the workload-specific repo + run a colcon build constrained to the new
# packages. Expected per-benchmark build time: ~3-5 minutes instead of 20.
#
# Build:
#   docker build -t fish-r2b-base:latest -f docker/fish-r2b-base.Dockerfile .
# =============================================================================

FROM fish-base:cuda12.6-humble

# Bring in the workspace setup script (full --gpu sweep) and run it. The
# script handles all the patches (resize_qos, NVENC removal), rosdep,
# and the colcon build of the common set.
COPY scripts/setup_isaac_workspace.sh /tmp/setup_isaac_workspace.sh
RUN bash -lc "bash /tmp/setup_isaac_workspace.sh --gpu --datasets /nonexistent" && \
    rm -f /tmp/setup_isaac_workspace.sh

# Persist workspace env for derived images
ENV R2B_WS_HOME=/root/ros_ws \
    ROS2_BENCHMARK_OVERRIDE_ASSETS_ROOT=/root/ros_ws/src/ros2_benchmark/assets

RUN printf '%s\n' \
    '# fish-r2b-base — auto-source the common workspace' \
    'if [ -f /root/ros_ws/install/setup.bash ]; then' \
    '    source /root/ros_ws/install/setup.bash' \
    'fi' \
    > /etc/profile.d/ros2-benchmark-ws.sh && \
    chmod +x /etc/profile.d/ros2-benchmark-ws.sh

WORKDIR /root/ros_ws
