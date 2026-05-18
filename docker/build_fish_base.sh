#!/bin/bash
# Build the FISH base image.
# Run from the repo root (the Dockerfile uses scripts/, config/, etc.
# from there).
#
# Usage:
#   ./docker/build_fish_base.sh                # default tag
#   IMAGE_TAG=v0.1 ./docker/build_fish_base.sh # custom tag

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="${IMAGE_TAG:-cuda12.6-humble}"
IMAGE="fish-base:${IMAGE_TAG}"

echo "[build_fish_base] building $IMAGE from $(pwd)"
docker build \
    -t "$IMAGE" \
    -f docker/fish-base.Dockerfile \
    .

echo ""
echo "[build_fish_base] done."
echo "Image:  $IMAGE"
echo "Size:   $(docker images --format '{{.Size}}' "$IMAGE")"
echo ""
echo "Smoke-test inside the new image:"
echo "  docker run --rm --gpus all $IMAGE bash -lc \\"
echo "    'nvcc --version; ros2 --help | head -3; which ros2; echo FISH_ENABLED=\$FISH_ENABLED'"
