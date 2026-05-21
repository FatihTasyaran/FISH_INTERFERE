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
        # Reset any prior sed modifications so patches re-apply to pristine
        # source — keeps the patch stage idempotent across iterations.
        log "exists, resetting + skipping clone: $(basename "$dst")"
        git -C "$dst" checkout -- . 2>/dev/null || true
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
# vision_opencv: clone but skip cv_bridge subpkg (CMake 4 FindBoost breakage).
# image_geometry IS required from source — image_pipeline's image_proc needs
# its header at build time and there's no humble apt package shipping it.
# apt's ros-humble-cv-bridge satisfies the runtime cv_bridge dep.
clone https://github.com/ros-perception/vision_opencv.git       humble
if [ -d "$R2B_WS_HOME/src/vision_opencv/cv_bridge" ]; then
    touch "$R2B_WS_HOME/src/vision_opencv/cv_bridge/COLCON_IGNORE"
    log "  marked cv_bridge/ COLCON_IGNORE (apt binary used instead)"
fi
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
    # negotiated: NITROS depends on this for topic negotiation, not in apt
    # and not in the public rosdistro. Clone osrf's source — it's a small
    # pure-ROS pkg.
    if [ ! -d "$R2B_WS_HOME/src/negotiated" ]; then
        git -C "$R2B_WS_HOME/src" clone --depth 1 \
            https://github.com/osrf/negotiated.git negotiated || \
            warn "  failed to clone osrf/negotiated"
    fi

    # Patch isaac_ros_gxf — Core target's INTERFACE_LINK_LIBRARIES references
    # magic_enum::magic_enum but the CMakeLists never runs find_package
    # nor ament_export_dependencies for it, so downstream pkgs choke at
    # set_target_properties because the target was never imported.
    GXF_CMAKE="$R2B_WS_HOME/src/isaac_ros_nitros/isaac_ros_gxf/CMakeLists.txt"
    if [ -f "$GXF_CMAKE" ] && ! grep -q "ament_export_dependencies(magic_enum)" "$GXF_CMAKE"; then
        sed -i '/find_package(ament_cmake_auto REQUIRED)/a find_package(magic_enum REQUIRED)' "$GXF_CMAKE"
        sed -i '/^ament_export_targets(export_/a ament_export_dependencies(magic_enum)' "$GXF_CMAKE"
        log "  patched: isaac_ros_gxf find_package(magic_enum) + ament_export_dependencies"
    fi

    # VPI 4 renamed VPIImagePlanePitchLinear::data → ::pBase AND tightened
    # the type to VPIByte* (unsigned char*). Isaac ROS 3.2 source still
    # assigns templated pointer types directly. Two sed passes:
    #   1. .planes[N].data       → .planes[N].pBase
    #   2. .planes[N].pBase = X; → .planes[N].pBase = reinterpret_cast<VPIByte*>(X);
    # The cast is idempotent for VPIByte* values (e.g. image_flip's case)
    # and necessary for templated/non-byte assignments (e.g. tensorops).
    VPI_PATCHED=0
    for src in $(grep -rlE '\.planes\[[^]]*\]\.(data|pBase)\b' \
                  "$R2B_WS_HOME/src/isaac_ros_image_pipeline" \
                  "$R2B_WS_HOME/src/isaac_ros_nitros" \
                  "$R2B_WS_HOME/src/isaac_ros_apriltag" \
                  "$R2B_WS_HOME/src/isaac_ros_benchmark" 2>/dev/null); do
        # Pass 1: data → pBase
        sed -i -E 's/\.planes\[([^]]*)\]\.data\b/.planes[\1].pBase/g' "$src"
        # Pass 2: wrap pBase assignment in reinterpret_cast. -z makes sed
        # treat the whole file as one line so the [^;]+ in the regex can
        # span newlines (NVIDIA often splits the assignment over 2 lines).
        # Negative lookbehind via /reinterpret_cast<VPIByte/! for idempotency.
        sed -i -E -z 's|(\.planes\[[^]]*\]\.pBase[[:space:]]*=[[:space:]]*)(reinterpret_cast<VPIByte[[:space:]]*\*>\([^;]+\));|\1\2;|g; s|(\.planes\[[^]]*\]\.pBase[[:space:]]*=[[:space:]]*)([^;]+);|\1reinterpret_cast<VPIByte *>(\2);|g' "$src"
        VPI_PATCHED=$((VPI_PATCHED + 1))
    done
    [ "$VPI_PATCHED" -gt 0 ] && log "  patched: $VPI_PATCHED file(s) for VPI 4 .data→.pBase + reinterpret_cast<VPIByte*>"

    # VPI 4 dropped VPI_BACKEND_NVENC (Jetson-only). Strip every reference in
    # source files. Most uses are switch cases or return values — deleting
    # the line is safe because compilers tolerate missing case labels and
    # the impl tree has fallback paths.
    NVENC_PATCHED=0
    for src in $(grep -rlE '\bVPI_BACKEND_NVENC\b' \
                  "$R2B_WS_HOME/src/isaac_ros_image_pipeline" \
                  "$R2B_WS_HOME/src/isaac_ros_nitros" \
                  "$R2B_WS_HOME/src/isaac_ros_apriltag" \
                  "$R2B_WS_HOME/src/isaac_ros_benchmark" 2>/dev/null); do
        sed -i '/\bVPI_BACKEND_NVENC\b/d' "$src"
        NVENC_PATCHED=$((NVENC_PATCHED + 1))
    done
    [ "$NVENC_PATCHED" -gt 0 ] && log "  patched: $NVENC_PATCHED file(s) — removed VPI_BACKEND_NVENC refs"
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
rosdep update --rosdistro humble 2>&1 | tail -3 || warn "  rosdep update returned non-zero, continuing"

# rosdep often fails partially because Isaac ROS / camera_ros / NITROS
# packages are not in the public rosdistro index. We accept the partial
# install — anything apt-resolvable gets installed; colcon will tell us
# what's actually missing during the build stage.
set +e
rosdep install -i -r --from-paths src --rosdistro humble -y 2>&1 | tee /tmp/rosdep.log | tail -15
ROSDEP_RC=${PIPESTATUS[0]}
set -e
if [ "$ROSDEP_RC" -ne 0 ]; then
    warn "  rosdep returned exit $ROSDEP_RC (some keys unresolvable — usually Isaac ROS sister packages)"
    warn "  unresolved keys (if any):"
    grep -E "Cannot locate rosdep definition|ERROR:" /tmp/rosdep.log 2>/dev/null | head -10 | sed "s/^/    /"
fi

log "stage F — colcon build"
if [ "$MODE" = "gpu" ]; then
    PACKAGES_UP_TO=(isaac_ros_apriltag isaac_ros_benchmark image_proc apriltag_ros)
else
    PACKAGES_UP_TO=(ros2_benchmark apriltag_ros image_proc)
fi
log "  packages-up-to: ${PACKAGES_UP_TO[*]}"

# Pre-flight: install build deps rosdep couldn't satisfy.
#  - ros-humble-xacro, ros-humble-camera-info-manager: exist in OSRF humble
#    repo but apt index needs refreshing inside this layer.
#  - magic_enum is a header-only C++ library; NVIDIA's apt mirror has it as
#    ros-humble-magic-enum but the apt index didn't pick it up. Build from
#    source — it's a single header + cmake install.
log "  pre-flight: apt update + missing apt deps + TensorRT + magic_enum source install"
apt-get update -qq 2>&1 | tail -3 || true
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ros-humble-xacro \
    ros-humble-camera-info-manager \
    ros-humble-vision-msgs \
    ros-humble-foxglove-msgs \
    ros-humble-camera-calibration-parsers \
    ros-humble-ament-cmake-clang-format 2>&1 | tail -3 || true

# TensorRT lives in a separate derived image, `fish-r2b-tensorrt-base:latest`
# (see docker/fish-r2b-tensorrt-base.Dockerfile), to keep the canonical
# `fish-r2b-base` lean. Per-benchmark Dockerfiles pick the right parent
# (fish-r2b-base for non-DNN, fish-r2b-tensorrt-base for DNN benchmarks)
# via logic in build_benchmark_image.sh.

# CV-CUDA — needed by isaac_ros_image_proc's pad_node etc. Not in any apt
# repo we have; fetch x86_64 cuda12 debs from GitHub releases.
if [ ! -f /usr/include/nvcv/Tensor.hpp ]; then
    log "    downloading CV-CUDA 0.10.1 debs (lib + dev)"
    cd /tmp
    for pkg in cvcuda-lib-0.10.1_beta-cuda12-x86_64-linux.deb cvcuda-dev-0.10.1_beta-cuda12-x86_64-linux.deb; do
        curl -fsSLO "https://github.com/CVCUDA/CV-CUDA/releases/download/v0.10.1-beta/$pkg" || \
            warn "  cvcuda fetch failed: $pkg"
    done
    DEBIAN_FRONTEND=noninteractive apt-get install -y ./cvcuda-*.deb 2>&1 | tail -3 || true
    rm -f /tmp/cvcuda-*.deb
    cd "$R2B_WS_HOME"
fi

if ! cmake --find-package -DNAME=magic_enum -DCOMPILER_ID=GNU -DLANGUAGE=CXX -DMODE=EXIST 2>/dev/null; then
    log "    magic_enum not found — installing from source"
    rm -rf /tmp/magic_enum_src
    git clone --depth 1 https://github.com/Neargye/magic_enum.git /tmp/magic_enum_src 2>&1 | tail -2
    cmake -S /tmp/magic_enum_src -B /tmp/magic_enum_build \
        -DMAGIC_ENUM_OPT_BUILD_TESTS=OFF \
        -DMAGIC_ENUM_OPT_BUILD_EXAMPLES=OFF \
        -DMAGIC_ENUM_OPT_INSTALL=ON 2>&1 | tail -3
    cmake --install /tmp/magic_enum_build 2>&1 | tail -3
    rm -rf /tmp/magic_enum_src /tmp/magic_enum_build
fi

# isaac_ros_gxf's expected_macro.hpp does `#include "magic_enum.hpp"` without
# the magic_enum/ subdir prefix. Neargye's install lays out headers under
# /usr/local/include/magic_enum/. Symlink each header to the parent dir so
# both `#include "magic_enum.hpp"` and `#include <magic_enum/magic_enum.hpp>`
# resolve correctly.
if [ -d /usr/local/include/magic_enum ]; then
    for hpp in /usr/local/include/magic_enum/*.hpp; do
        [ -f "$hpp" ] || continue
        ln -sf "$hpp" "/usr/local/include/$(basename "$hpp")"
    done
    log "    symlinked magic_enum/*.hpp → /usr/local/include/ (subdir-less include)"
fi

# CMake version handling for Isaac ROS sources:
#  - CMake 4.x bans `$<INSTALL_PREFIX>` outside install(EXPORT) — isaac_ros_gxf
#    uses it in INTERFACE_LINK_LIBRARIES of every imported library target.
#    No policy switch in 4.x re-enables this.
#  - CMake 4.x removed FindBoost — many legacy ROS pkgs still use it.
# Downgrade to CMake 3.27 via pip (puts /usr/local/bin/cmake — shadows the
# apt-installed 4.3 because /usr/local/bin precedes /usr/bin in PATH).
# 3.27 has CUDA::nvtx3 (added in 3.26) so fish-base's other needs are still met.
if cmake --version | head -1 | grep -qE "version (4|[5-9])\."; then
    log "  downgrading cmake → 3.27.9 via pip (shadows apt's 4.x)"
    python3 -m pip install --quiet --no-cache-dir cmake==3.27.9
    hash -r
fi
log "  cmake at: $(command -v cmake)  $(cmake --version | head -1)"

# CMake 3.27 still wants CMP0167 = OLD for legacy FindBoost code paths.
# --event-handlers console_cohesion+ keeps each package's output together
# (vs interleaved like console_direct+), so per-package errors stay readable.
# console_direct+ ALSO needed for stderr (otherwise stays in log/ inside
# the container which dies on failure).
# Full output goes to docker build log — DO NOT tail. We need every line.
colcon build \
    --packages-up-to "${PACKAGES_UP_TO[@]}" \
    --event-handlers console_cohesion+ console_direct- \
    --cmake-args \
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.27" \
        "-DCMAKE_POLICY_DEFAULT_CMP0167=OLD" 2>&1

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
