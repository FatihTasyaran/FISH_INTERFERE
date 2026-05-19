#!/bin/bash
# =============================================================================
# setup_isaac_workspace.sh — set up the ros2_benchmark + Isaac ROS workspace
#                            inside a fish-base container
# =============================================================================
# Idempotent: safe to re-run. Clones the source repos if missing, applies the
# known compatibility patches (Humble vs release-3.2), builds the workspace
# with colcon. Two passes:
#
#   --cpu   (default)   Clones the minimal CPU AprilTag chain:
#                       ros2_benchmark, isaac_ros_common, apriltag_ros,
#                       vision_opencv, image_pipeline. Builds:
#                           ros2_benchmark, apriltag_ros, image_proc.
#
#   --gpu                Also clones isaac_ros_benchmark, isaac_ros_apriltag,
#                       isaac_ros_nitros, isaac_ros_image_pipeline. Builds:
#                           isaac_ros_apriltag, isaac_ros_benchmark.
#
#   --datasets <dir>     If given, sym-links the r2b dataset directories
#                       under the workspace's assets/datasets/r2b_dataset/
#                       so ros2_benchmark scripts can find the bags.
#                       Default: /root/r2bdataset2023_v3 if present.
#
# Usage (inside a fish-base container):
#   bash scripts/setup_isaac_workspace.sh
#   bash scripts/setup_isaac_workspace.sh --gpu
#   bash scripts/setup_isaac_workspace.sh --gpu --datasets /root/r2bdataset2023_v3
#
# After the script finishes, source the workspace:
#   source $R2B_WS_HOME/install/setup.bash
# =============================================================================

set -euo pipefail

# ─── Argument parsing ──────────────────────────────────────────────────────
MODE="cpu"
DATASETS="/root/r2bdataset2023_v3"

while [ $# -gt 0 ]; do
    case "$1" in
        --cpu)         MODE="cpu" ;;
        --gpu)         MODE="gpu" ;;
        --datasets)    shift; DATASETS="$1" ;;
        -h|--help)
            sed -n '2,/^# ====/p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *)             echo "[setup] unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

# ─── Sanity checks ─────────────────────────────────────────────────────────
if ! command -v colcon >/dev/null 2>&1; then
    echo "[setup] colcon not on PATH — are you inside fish-base? sourcing /opt/ros/humble"
    source /opt/ros/humble/setup.bash
fi
if ! command -v nvcc >/dev/null 2>&1; then
    echo "[setup] nvcc not on PATH — adding /usr/local/cuda/bin"
    export PATH=/usr/local/cuda/bin:${PATH}
fi

export R2B_WS_HOME="${R2B_WS_HOME:-/root/ros_ws}"
export ROS2_BENCHMARK_OVERRIDE_ASSETS_ROOT="$R2B_WS_HOME/src/ros2_benchmark/assets"

log()  { echo -e "\033[1;36m[setup]\033[0m $*"; }
warn() { echo -e "\033[1;33m[setup]\033[0m $*" >&2; }

# ─── Clone helper ──────────────────────────────────────────────────────────
clone() {
    local url=$1
    local branch=$2
    local dst="$R2B_WS_HOME/src/$(basename "$url" .git)"
    if [ -d "$dst" ]; then
        log "exists, skipping clone: $(basename "$dst")"
    else
        log "clone $url @ $branch"
        git -C "$R2B_WS_HOME/src" clone --depth 1 --branch "$branch" "$url"
    fi
}

# ─── 1. Workspace skeleton ─────────────────────────────────────────────────
log "WS=$R2B_WS_HOME  mode=$MODE  datasets=$DATASETS"
mkdir -p "$R2B_WS_HOME/src"

# ─── 2. CPU baseline repos ────────────────────────────────────────────────
log "stage A — CPU baseline repos"
clone https://github.com/NVIDIA-ISAAC-ROS/ros2_benchmark.git    release-3.2
clone https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_common.git  release-3.2
clone https://github.com/christianrauch/apriltag_ros.git        master    || \
    clone https://github.com/christianrauch/apriltag_ros.git    main
clone https://github.com/ros-perception/vision_opencv.git       humble
clone https://github.com/ros-perception/image_pipeline.git      humble

# ─── 3. Patches ────────────────────────────────────────────────────────────
log "stage B — apply known patches"

# (a) image_pipeline resize QoS — required by ros2_benchmark's PrepResizeNode
PATCH_FILE="$R2B_WS_HOME/src/image_pipeline/resize_qos_profile.patch"
if [ ! -f "$PATCH_FILE" ] && [ -d "$R2B_WS_HOME/src/image_pipeline" ]; then
    pushd "$R2B_WS_HOME/src/image_pipeline" > /dev/null
    git config user.email "fish@local" || true
    git config user.name  "fish-setup"  || true
    wget -q https://raw.githubusercontent.com/NVIDIA-ISAAC-ROS/ros2_benchmark/main/resources/patch/resize_qos_profile.patch
    if git apply --check resize_qos_profile.patch 2>/dev/null; then
        git apply resize_qos_profile.patch
        log "  applied: resize_qos_profile.patch"
    else
        warn "  resize_qos_profile.patch already applied or not applicable; skipping"
    fi
    popd > /dev/null
fi

# (b) isaac_ros_common NVENC removal — VPI 4 dropped this enum (Jetson-only).
VPI_UTIL="$R2B_WS_HOME/src/isaac_ros_common/isaac_ros_common/src/vpi_utilities.cpp"
if [ -f "$VPI_UTIL" ] && grep -q VPI_BACKEND_NVENC "$VPI_UTIL"; then
    sed -i '/VPI_BACKEND_NVENC/d' "$VPI_UTIL"
    log "  patched: removed VPI_BACKEND_NVENC reference from $(basename "$VPI_UTIL")"
fi

# ─── 4. (optional) GPU repos ───────────────────────────────────────────────
if [ "$MODE" = "gpu" ]; then
    log "stage C — Isaac ROS GPU repos"
    clone https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_benchmark.git       release-3.2
    clone https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_apriltag.git        release-3.2
    clone https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_nitros.git          release-3.2
    clone https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_image_pipeline.git  release-3.2
fi

# ─── 5. Dataset symlinks ───────────────────────────────────────────────────
if [ -d "$DATASETS" ]; then
    log "stage D — link datasets from $DATASETS"
    DEST="$R2B_WS_HOME/src/ros2_benchmark/assets/datasets/r2b_dataset"
    mkdir -p "$DEST"
    for d in "$DATASETS"/*; do
        [ -d "$d" ] || continue
        name=$(basename "$d")
        if [ ! -e "$DEST/$name" ]; then
            ln -sf "$d" "$DEST/$name"
            log "  linked: $name"
        fi
    done
else
    warn "stage D — dataset dir $DATASETS not found, skipping symlinks"
fi

# ─── 6. rosdep + colcon build ──────────────────────────────────────────────
log "stage E — rosdep install"
cd "$R2B_WS_HOME"
rosdep update --rosdistro humble 2>&1 | tail -3
rosdep install -i -r --from-paths src --rosdistro humble -y 2>&1 | tail -5

log "stage F — colcon build"
if [ "$MODE" = "gpu" ]; then
    PACKAGES_UP_TO=(isaac_ros_apriltag isaac_ros_benchmark image_proc apriltag_ros)
else
    PACKAGES_UP_TO=(ros2_benchmark apriltag_ros image_proc)
fi
log "  packages-up-to: ${PACKAGES_UP_TO[*]}"
colcon build --packages-up-to "${PACKAGES_UP_TO[@]}" 2>&1 | tail -25

# ─── 7. Done ───────────────────────────────────────────────────────────────
log "DONE — to use the workspace in this shell, run:"
echo "    export R2B_WS_HOME=$R2B_WS_HOME"
echo "    export ROS2_BENCHMARK_OVERRIDE_ASSETS_ROOT=$ROS2_BENCHMARK_OVERRIDE_ASSETS_ROOT"
echo "    source $R2B_WS_HOME/install/setup.bash"
echo ""
echo "Then run a benchmark, e.g.:"
if [ "$MODE" = "gpu" ]; then
    echo "    launch_test $R2B_WS_HOME/src/isaac_ros_benchmark/benchmarks/isaac_ros_apriltag_benchmark/scripts/isaac_ros_apriltag_node.py"
else
    echo "    launch_test $R2B_WS_HOME/src/ros2_benchmark/scripts/apriltag_ros_apriltag_node.py"
fi
