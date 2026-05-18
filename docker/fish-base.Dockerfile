# syntax=docker/dockerfile:1.6
# =============================================================================
# fish-base — minimal FISH framework base image
# =============================================================================
# Target uses:
#   - Foundation image for FISH-on-Isaac-ROS / FISH-on-ros2_benchmark fixture
#     captures (release-readiness test pyramid Tier 2 + Tier 3).
#   - Reusable across the 18+ P0 Isaac ROS workloads in
#     notes/isaac_ros_coverage.txt.
#
# What this image gives you out-of-the-box:
#   - CUDA 12.6 toolkit + nvcc + dev libs (driver libcuda.so still comes
#     from the host at run time via --gpus all).
#   - ROS 2 Humble (`ros-base` profile) + the FISH-required tracetools
#     and tracing-related apt packages.
#   - NVIDIA Nsight Systems 2025.6.3 for GPU profiling.
#   - FISH itself installed at `/opt/ros/humble/fish/` with the custom
#     tracepoint overlay workspace at `/root/trace_overlay_ws/`.
#   - `FISH_ENABLED=1` exported globally; the `ros2` wrapper is on PATH.
#
# What is intentionally NOT in this image (mount at runtime instead):
#   - Per-workload source overlays (ros2_benchmark, isaac_ros_common,
#     apriltag_ros, ...) — these change per fixture and live in a
#     mounted workspace.
#   - r2b dataset rosbags — mount as a read-only volume.
#   - Built artefacts of the per-workload colcon workspace — kept out
#     of the image so the image stays stable.
#
# Build:
#   docker build -t fish-base:cuda12.6-humble -f docker/fish-base.Dockerfile .
#   (Run from repo root so the COPY context is correct.)
#
# Run (typical):
#   docker run --rm -it --gpus all \
#       -v $HOME/r2bdataset2023_v3:/root/r2bdataset2023_v3:ro \
#       -v $HOME/ros_ws:/root/ros_ws \
#       fish-base:cuda12.6-humble
# =============================================================================

FROM nvidia/cuda:12.6.3-devel-ubuntu22.04

ARG ROS_DISTRO=humble
ARG NSYS_PKG=nsight-systems-2025.6.3
ARG DEBIAN_FRONTEND=noninteractive

ENV TZ=Etc/UTC \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    ROS_DISTRO=${ROS_DISTRO} \
    DEBIAN_FRONTEND=${DEBIAN_FRONTEND}

# ─── 1. Base system tools ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg lsb-release software-properties-common \
        git git-lfs patch build-essential cmake \
        python3 python3-pip python3-dev python3-venv \
        locales sudo vim less && \
    locale-gen en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

# ─── 2. ROS 2 Humble apt repository ────────────────────────────────────────
RUN install -d /usr/share/keyrings && \
    curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
        -o /usr/share/keyrings/ros-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu jammy main" \
        > /etc/apt/sources.list.d/ros2.list

# ─── 3. ROS 2 Humble + FISH-required apt packages ──────────────────────────
# Combined into one apt-get to reduce layer count and minimise the final
# image size. Packages chosen from:
#   - scripts/install_fish_deps.sh (FISH's own dependency check)
#   - docs/dependencies.md
#   - the rosdep run we already verified against ros2_benchmark
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-${ROS_DISTRO}-ros-base \
        ros-${ROS_DISTRO}-cv-bridge \
        ros-${ROS_DISTRO}-image-transport \
        ros-${ROS_DISTRO}-image-transport-plugins \
        ros-${ROS_DISTRO}-apriltag \
        ros-${ROS_DISTRO}-apriltag-msgs \
        ros-${ROS_DISTRO}-ros-testing \
        ros-${ROS_DISTRO}-tracetools \
        ros-${ROS_DISTRO}-tracetools-trace \
        ros-${ROS_DISTRO}-tracetools-launch \
        ros-${ROS_DISTRO}-ros2trace \
        lttng-tools \
        liblttng-ust-dev \
        python3-lttng \
        python3-colcon-common-extensions \
        python3-rosdep \
        python3-pytest \
        python3-pytest-mock \
        babeltrace2 && \
    rm -rf /var/lib/apt/lists/* && \
    rosdep init && \
    rosdep update --rosdistro ${ROS_DISTRO}

# ─── 4. NVIDIA Nsight Systems (nsys) ───────────────────────────────────────
# Pulls the CUDA apt repo just for nsys (the rest of CUDA is already in
# the base image). Pinned version matches scripts/install_fish_deps.sh.
RUN curl -sSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb \
        -o /tmp/cuda-keyring.deb && \
    dpkg -i /tmp/cuda-keyring.deb && rm /tmp/cuda-keyring.deb && \
    apt-get update && \
    apt-get install -y --no-install-recommends ${NSYS_PKG} && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf "$(find /opt/nvidia/nsight-systems -name nsys -type f 2>/dev/null | head -1)" \
        /usr/local/bin/nsys

# ─── 5. Copy FISH source (only what install_fish needs) ────────────────────
# We don't COPY the whole repo on purpose — `postprocess/` is host-side
# analysis, `notes/` is dev-only, and committing those into the image
# would bloat it. The runtime-relevant subset is below.
COPY scripts            /root/fish_interfere/scripts
COPY config             /root/fish_interfere/config
COPY fish_tracepoints   /root/fish_interfere/fish_tracepoints
COPY python             /root/fish_interfere/python
COPY fish_orchestrator.py /root/fish_interfere/fish_orchestrator.py

# ─── 6. Build FISH tracepoint overlay + install FISH framework ─────────────
# Skip scripts/install_fish_deps.sh (we've done that in step 3). Run the
# tracepoint builder and source install_fish.sh directly. The .bashrc
# additions install_fish.sh makes are harmless inside the image; we
# also set the same vars via ENV below so non-interactive shells see
# them too.
WORKDIR /root/fish_interfere
RUN chmod +x scripts/*.sh fish_tracepoints/install_fish_tracepoints && \
    bash fish_tracepoints/install_fish_tracepoints --all && \
    bash -c "set -e; \
             source /opt/ros/humble/setup.bash; \
             FISH_SETUP_PARENT=1 source scripts/install_fish.sh"

# ─── 7. Final environment ──────────────────────────────────────────────────
ENV PATH=/usr/local/cuda/bin:/opt/ros/humble/fish/bin:/opt/ros/humble/bin:${PATH} \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-} \
    PYTHONPATH=/opt/ros/humble/fish/python:/opt/ros/humble/lib/python3.10/site-packages \
    AMENT_PREFIX_PATH=/root/trace_overlay_ws/install:/opt/ros/humble \
    CMAKE_PREFIX_PATH=/root/trace_overlay_ws/install:/opt/ros/humble \
    FISH_ENABLED=1

# Auto-source ROS + overlay for ALL shells:
#  - /etc/profile.d/*.sh is sourced by login shells (bash -l, bash -lc)
#  - /root/.bashrc additions cover interactive non-login shells
# Having both means `docker run ... bash -lc 'ros2 ...'` Just Works.
RUN printf '%s\n' \
    '# FISH base image — auto-source ROS + tracepoint overlay' \
    'if [ -f /opt/ros/humble/setup.bash ]; then' \
    '    source /opt/ros/humble/setup.bash' \
    'fi' \
    'if [ -f /root/trace_overlay_ws/install/local_setup.bash ]; then' \
    '    source /root/trace_overlay_ws/install/local_setup.bash' \
    'fi' \
    'export FISH_ENABLED=1' \
    > /etc/profile.d/ros-fish.sh && \
    chmod +x /etc/profile.d/ros-fish.sh && \
    printf '%s\n' \
    'source /etc/profile.d/ros-fish.sh' \
    >> /root/.bashrc

WORKDIR /root
CMD ["bash"]
