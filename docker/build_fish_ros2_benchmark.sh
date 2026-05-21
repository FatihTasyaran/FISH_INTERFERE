#!/bin/bash
# Build the fish-ros2-benchmark image (fish-base + ros2_benchmark workspace).
# Run from the repo root.
#
# Usage:
#   ./docker/build_fish_ros2_benchmark.sh           # cpu mode → tag :cpu
#   SETUP_MODE=gpu ./docker/build_fish_ros2_benchmark.sh   # gpu mode → tag :gpu

set -euo pipefail
cd "$(dirname "$0")/.."

SETUP_MODE="${SETUP_MODE:-cpu}"
FISH_BASE_TAG="${FISH_BASE_TAG:-cuda12.6-humble}"
IMAGE="fish-ros2-benchmark:${SETUP_MODE}"

# Ensure fish-base exists; if not, build it first.
if ! docker image inspect "fish-base:${FISH_BASE_TAG}" >/dev/null 2>&1; then
    echo "[build] fish-base:${FISH_BASE_TAG} not found, building it first..."
    ./docker/build_fish_base.sh
fi

echo "[build] $IMAGE  (setup mode: $SETUP_MODE, base: fish-base:${FISH_BASE_TAG})"
docker build \
    --build-arg "FISH_BASE_TAG=${FISH_BASE_TAG}" \
    --build-arg "SETUP_MODE=${SETUP_MODE}" \
    -t "$IMAGE" \
    -f docker/fish-ros2-benchmark.Dockerfile \
    .

echo ""
echo "[build] done."
echo "Image:  $IMAGE"
echo "Size:   $(docker images --format '{{.Size}}' "$IMAGE")"
echo ""
echo "Run:"
echo "  docker run -dit --name r2b \\"
echo "      --runtime=nvidia --gpus all --privileged --net host \\"
echo "      -v \$HOME/r2bdataset2023_v3:/root/r2bdataset2023_v3:ro \\"
echo "      -v \$HOME/fish_traces:/root/fish_traces \\"
echo "      $IMAGE"
echo "  docker exec -it r2b bash"
echo ""
echo "Inside:"
if [ "$SETUP_MODE" = "gpu" ]; then
    echo "  launch_test \$R2B_WS_HOME/src/isaac_ros_benchmark/benchmarks/isaac_ros_apriltag_benchmark/scripts/isaac_ros_apriltag_node.py"
else
    echo "  launch_test \$R2B_WS_HOME/src/ros2_benchmark/scripts/apriltag_ros_apriltag_node.py"
fi
