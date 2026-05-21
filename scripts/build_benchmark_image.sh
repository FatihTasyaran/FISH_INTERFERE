#!/bin/bash
# =============================================================================
# build_benchmark_image.sh — build + smoke-test ONE Isaac ROS benchmark image
# =============================================================================
# Usage:
#   ./scripts/build_benchmark_image.sh <benchmark_name>
#   ./scripts/build_benchmark_image.sh isaac_ros_apriltag
#
# What it does:
#   1. Looks up the benchmark in tests/benchmark_inventory.json
#   2. Picks a representative launch script (or the one named via $SCRIPT_NAME)
#   3. Generates a tiny Dockerfile that:
#        FROM fish-base + clones the workload repo at release-3.2
#        + runs scripts/setup_isaac_workspace.sh with the right repo set
#        + does colcon build constrained to the workload package(s)
#   4. Builds the image as fish-r2b-<benchmark>:cpu or :gpu
#   5. Runs launch_test of the chosen script inside the image
#      with a hard timeout — captures exit code + last 40 lines
#   6. Writes the result row into tests/benchmark_status.json
#
# Env vars (optional):
#   BENCHMARK_TIMEOUT       launch_test timeout in seconds (default 300)
#   SCRIPT_NAME             specific script name to run (default first)
#   SKIP_RUN                set to 1 to skip the smoke-test step (build only)
# =============================================================================

set -euo pipefail

# ─── Args & setup ──────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    echo "Usage: $0 <benchmark_name>"
    echo "Example: $0 isaac_ros_apriltag"
    exit 1
fi
BENCHMARK="$1"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INVENTORY="$REPO_ROOT/tests/benchmark_inventory.json"
STATUS_JSON="$REPO_ROOT/tests/benchmark_status.json"
BENCHMARK_TIMEOUT="${BENCHMARK_TIMEOUT:-300}"
SKIP_RUN="${SKIP_RUN:-0}"

if [ ! -f "$INVENTORY" ]; then
    echo "[build] benchmark inventory not found: $INVENTORY" >&2
    exit 1
fi

# ─── Lookup ────────────────────────────────────────────────────────────────
ENTRY=$(python3 -c "
import json, sys
with open('$INVENTORY') as f:
    inv = json.load(f)
for b in inv['benchmarks']:
    if b['name'] == '$BENCHMARK':
        print(json.dumps(b))
        sys.exit(0)
sys.exit(2)
")
if [ -z "$ENTRY" ]; then
    echo "[build] benchmark '$BENCHMARK' not in inventory" >&2
    exit 2
fi

CATEGORY=$(python3 -c "import json;print(json.loads('''$ENTRY''')['category'])")
# workload_repos is an array; fall back to legacy single workload_repo if present.
WORKLOAD_REPOS=$(python3 -c "
import json
b = json.loads('''$ENTRY''')
repos = b.get('workload_repos')
if repos is None and 'workload_repo' in b: repos = [b['workload_repo']]
print(' '.join(repos or []))
")
SCRIPT_NAME="${SCRIPT_NAME:-$(python3 -c "import json;b=json.loads('''$ENTRY''');print(b['scripts'][0] if b['scripts'] else '')")}"

log()  { echo -e "\033[1;36m[build:$BENCHMARK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[build:$BENCHMARK]\033[0m $*" >&2; }
fail() { echo -e "\033[1;31m[build:$BENCHMARK]\033[0m $*" >&2; }

log "category=$CATEGORY  workload_repos=[$WORKLOAD_REPOS]  script=$SCRIPT_NAME"

# Skip hardware-bound entries — don't even try
if [ "$CATEGORY" = "hardware_skip" ]; then
    REASON=$(python3 -c "import json;b=json.loads('''$ENTRY''');print(b.get('reason','hardware skip'))")
    warn "skipping — $REASON"
    python3 "$REPO_ROOT/scripts/_status_record.py" "$BENCHMARK" skipped "$REASON" "" ""
    exit 0
fi

IMAGE_TAG="fish-r2b-$(echo "$BENCHMARK" | sed 's/^isaac_ros_//'):latest"

# ─── Generate Dockerfile + workload setup script ──────────────────────────
DOCKERFILE="$REPO_ROOT/docker/_generated_${BENCHMARK}.Dockerfile"
WORKLOAD_SCRIPT="$REPO_ROOT/docker/_generated_${BENCHMARK}_setup.sh"
log "generating $DOCKERFILE + $(basename "$WORKLOAD_SCRIPT")"

# Workload setup script — run inside the docker layer. No fragile escape juggling.
# Per-benchmark patch hook: if docker/_benchmark_patches/<bench>.sh exists,
# it gets COPYed into /tmp/patch.sh and sourced AFTER the workspace is set up
# and BEFORE the final colcon build. That lets per-benchmark agents add
# extra clones / sed patches / apt deps / cmake flags without ever touching
# this shared script. The patch file should define one or more of:
#   PATCH_EXTRA_REPOS=(...)        extra git URLs (release-3.2 branch)
#   PATCH_EXTRA_APT="pkg1 pkg2"    extra apt-get install -y
#   PATCH_EXTRA_CMAKE_ARGS="-DFOO=BAR -DBAZ=QUX"
#   PATCH_EXTRA_COLCON_TARGETS="pkg_x pkg_y"
# A function `patch_pre_build` can be defined for arbitrary commands.
PATCH_FILE="$REPO_ROOT/docker/_benchmark_patches/${BENCHMARK}.sh"
HAS_PATCH=0
[ -f "$PATCH_FILE" ] && HAS_PATCH=1

{
    echo "#!/bin/bash"
    echo "# Auto-generated workload setup for $BENCHMARK"
    echo "set -e"
    echo ""
    echo "# fish-r2b-base already has the common Isaac ROS workspace built,"
    echo "# so we skip setup_isaac_workspace.sh and only add the workload repo(s)."
    echo ""
    echo "# Initialise patch-supplied variables (empty by default)"
    echo "PATCH_EXTRA_REPOS=()"
    echo "PATCH_EXTRA_APT=\"\""
    echo "PATCH_EXTRA_CMAKE_ARGS=\"\""
    echo "PATCH_EXTRA_COLCON_TARGETS=\"\""
    echo "patch_pre_build() { :; }"
    echo ""
    if [ "$HAS_PATCH" = "1" ]; then
        echo "echo '[workload] sourcing per-benchmark patch'"
        echo "source /tmp/patch.sh"
    fi
    echo ""
    echo "cd /root/ros_ws/src"
    for repo in $WORKLOAD_REPOS; do
        echo "git clone --depth 1 --branch release-3.2 https://github.com/NVIDIA-ISAAC-ROS/${repo}.git || true"
    done
    echo 'for repo in "${PATCH_EXTRA_REPOS[@]}"; do git clone --depth 1 --branch release-3.2 "$repo" || true; done'
    echo ""
    echo 'if [ -n "$PATCH_EXTRA_APT" ]; then apt-get update && apt-get install -y --no-install-recommends $PATCH_EXTRA_APT || true; fi'
    echo ""
    echo "patch_pre_build"
    echo ""
    echo "cd /root/ros_ws"
    echo "source /opt/ros/humble/setup.bash"
    echo "rosdep install -i -r --from-paths src --rosdistro humble -y 2>&1 | tail -5 || true"
    echo ""
    echo "# Final colcon build — fish-r2b-base already supplies cmake 3.27 + base deps."
    echo "# (Isaac ROS uses \$<INSTALL_PREFIX> in target props, banned by CMake 4.x.)"
    echo "colcon build \\"
    echo "    --packages-up-to ${BENCHMARK}_benchmark isaac_ros_benchmark \$PATCH_EXTRA_COLCON_TARGETS \\"
    echo "    --cmake-args \$PATCH_EXTRA_CMAKE_ARGS"
} > "$WORKLOAD_SCRIPT"
chmod +x "$WORKLOAD_SCRIPT"

# Dockerfile stays static — just COPY and RUN the generated script.
# FROM fish-r2b-tensorrt-base (= fish-r2b-base + TensorRT pre-installed).
# Common Isaac ROS workspace + TensorRT dev libs are all baked in. Per-benchmark
# build only clones the workload repo + colcon builds the new package(s).
# Non-DNN benchmarks waste ~7 GB of unused TensorRT; this is fine because
# disk isn't the bottleneck — agent context budget is, and baking TensorRT
# once saves ~5 GB download per DNN benchmark + ~10 min wall time.
cat > "$DOCKERFILE" <<EOF
# Auto-generated by scripts/build_benchmark_image.sh — do not edit by hand
# Target benchmark: $BENCHMARK   ($WORKLOAD_REPOS)
FROM fish-r2b-tensorrt-base:latest

COPY $(realpath --relative-to="$REPO_ROOT" "$WORKLOAD_SCRIPT") /tmp/workload_setup.sh
$([ "$HAS_PATCH" = "1" ] && echo "COPY $(realpath --relative-to="$REPO_ROOT" "$PATCH_FILE") /tmp/patch.sh")
RUN chmod +x /tmp/workload_setup.sh && \\
    bash -lc /tmp/workload_setup.sh && \\
    rm -f /tmp/workload_setup.sh /tmp/patch.sh

WORKDIR /root/ros_ws
ENV R2B_WS_HOME=/root/ros_ws
ENV ROS2_BENCHMARK_OVERRIDE_ASSETS_ROOT=/root/ros_ws/src/ros2_benchmark/assets
RUN printf '%s\n' \\
    '# benchmark image — auto-source workspace' \\
    'if [ -f /root/ros_ws/install/setup.bash ]; then source /root/ros_ws/install/setup.bash; fi' \\
    > /etc/profile.d/ros2-benchmark-ws.sh
EOF

# ─── Build ─────────────────────────────────────────────────────────────────
log "docker build → $IMAGE_TAG"
# Use a stable path so external monitors can tail it.
BUILD_LOG="/tmp/fish-r2b-build-${BENCHMARK}.log"
: > "$BUILD_LOG"
trap 'rm -f "$DOCKERFILE" "$WORKLOAD_SCRIPT"' EXIT

BUILD_START=$(date +%s)
if docker build --progress=plain -t "$IMAGE_TAG" -f "$DOCKERFILE" "$REPO_ROOT" > "$BUILD_LOG" 2>&1; then
    BUILD_END=$(date +%s)
    BUILD_DUR=$((BUILD_END - BUILD_START))
    BUILD_RC=0
    log "BUILD OK (${BUILD_DUR}s)"
else
    BUILD_RC=$?
    BUILD_END=$(date +%s)
    BUILD_DUR=$((BUILD_END - BUILD_START))
    fail "BUILD FAILED (rc=$BUILD_RC, ${BUILD_DUR}s)"
    tail -40 "$BUILD_LOG" >&2
    BUILD_TAIL=$(tail -10 "$BUILD_LOG" | sed 's/"/\\"/g')
    python3 "$REPO_ROOT/scripts/_status_record.py" "$BENCHMARK" build_failed "${BUILD_DUR}s rc=$BUILD_RC" "$IMAGE_TAG" "$BUILD_TAIL"
    exit $BUILD_RC
fi

# ─── Smoke test (run launch_test inside the new image) ─────────────────────
if [ "$SKIP_RUN" = "1" ] || [ -z "$SCRIPT_NAME" ]; then
    log "skipping run test (SKIP_RUN=$SKIP_RUN  SCRIPT_NAME=$SCRIPT_NAME)"
    python3 "$REPO_ROOT/scripts/_status_record.py" "$BENCHMARK" build_only "${BUILD_DUR}s" "$IMAGE_TAG" ""
    exit 0
fi

log "smoke test: launch_test $SCRIPT_NAME (timeout ${BENCHMARK_TIMEOUT}s)"
RUN_LOG=$(mktemp)
SCRIPT_PATH="/root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCHMARK}_benchmark/scripts/${SCRIPT_NAME}.py"

# Bind-mount r2b dataset(s) from host into the standard location ros2_benchmark
# expects. Both 2023_v3 and 2024_v1 contain sub-bags merged into one virtual
# r2b_dataset/ dir via tmpfs overlay below. If a benchmark needs a bag this
# script doesn't cover, it'll surface as a launch_test failure rather than a
# silent skip.
DATASET_MOUNTS=()
[ -d /home/tue037807/r2bdataset2023_v3 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2023_v3:/host/r2b_2023:ro )
[ -d /home/tue037807/r2bdataset2024_v1 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2024_v1:/host/r2b_2024:ro )

RUN_START=$(date +%s)
if timeout "$BENCHMARK_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
        "$IMAGE_TAG" \
        bash -lc "
            DEST=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST
            for src in /host/r2b_2023 /host/r2b_2024; do
                [ -d \$src ] || continue
                for bag in \$src/*; do
                    [ -d \$bag ] || continue
                    ln -sf \$bag \$DEST/\$(basename \$bag) 2>/dev/null
                done
            done
            launch_test $SCRIPT_PATH
        " > "$RUN_LOG" 2>&1; then
    RUN_END=$(date +%s)
    RUN_DUR=$((RUN_END - RUN_START))
    RUN_RC=0
    log "RUN OK (${RUN_DUR}s)"
    RUN_TAIL=$(tail -10 "$RUN_LOG" | sed 's/"/\\"/g')
    python3 "$REPO_ROOT/scripts/_status_record.py" "$BENCHMARK" passed "${BUILD_DUR}s+${RUN_DUR}s" "$IMAGE_TAG" "$RUN_TAIL"
else
    RUN_RC=$?
    RUN_END=$(date +%s)
    RUN_DUR=$((RUN_END - RUN_START))
    fail "RUN FAILED (rc=$RUN_RC, ${RUN_DUR}s)"
    tail -40 "$RUN_LOG" >&2
    RUN_TAIL=$(tail -10 "$RUN_LOG" | sed 's/"/\\"/g')
    python3 "$REPO_ROOT/scripts/_status_record.py" "$BENCHMARK" run_failed "${BUILD_DUR}s+${RUN_DUR}s rc=$RUN_RC" "$IMAGE_TAG" "$RUN_TAIL"
fi

rm -f "$RUN_LOG"
log "done"
