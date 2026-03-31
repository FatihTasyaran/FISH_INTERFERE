#!/bin/bash
# =============================================================================
# Build FISH Image — Installs FISH into a base image and commits
# =============================================================================
# Starts a temporary container from the base image, copies fish_interfere,
# runs setup_fish.sh, and commits as aircraft-fish-image:latest.
#
# Usage:
#   ./build_fish_image.sh                          # default base image
#   ./build_fish_image.sh <base_image>             # custom base image
#   ./build_fish_image.sh aircraft-image:v2        # specific tag
# =============================================================================
set -euo pipefail

BASE_IMAGE="${1:-aircraft-image:latest}"
FISH_IMAGE="aircraft-fish-image:latest"
CONTAINER_NAME="fish-build-$$"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
warn() { echo -e "${YELLOW}[BUILD]${NC} $*"; }
err()  { echo -e "${RED}[BUILD]${NC} $*" >&2; }

cleanup() {
    if docker inspect "$CONTAINER_NAME" &>/dev/null; then
        log "Cleaning up container $CONTAINER_NAME..."
        docker rm -f "$CONTAINER_NAME" &>/dev/null || true
    fi
}
trap cleanup EXIT

# ─── Step 1: Start temporary container ───────────────────────────────────────
log "Base image: $BASE_IMAGE"
log "Target:     $FISH_IMAGE"
log "Container:  $CONTAINER_NAME"
echo ""

log "[1/4] Starting temporary container..."
docker run -d \
    --privileged \
    --runtime=nvidia --gpus all \
    --device /dev/dri --device /dev/kfd \
    -e DISPLAY=$DISPLAY \
    -e XAUTHORITY=/root/.Xauthority \
    -v $HOME/.Xauthority:/root/.Xauthority:rw \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=all \
    --cap-add=SYS_ADMIN \
    --security-opt seccomp=unconfined \
    -v /etc/vulkan/icd.d:/etc/vulkan/icd.d:rw \
    -v /etc/glvnd/egl-vendor.d:/etc/glvnd/egl-vendor.d:rw \
    -v /etc/OpenCL/vendors:/etc/OpenCL/vendors:rw \
    --net host \
    --entrypoint /bin/bash \
    --name "$CONTAINER_NAME" \
    "$BASE_IMAGE" \
    -c "tail -f /dev/null"

log "  Container started"

# ─── Step 2: Copy fish_interfere ─────────────────────────────────────────────
log "[2/4] Copying fish_interfere into container..."
docker cp "$SCRIPT_DIR" "$CONTAINER_NAME:/root/fish_interfere"
log "  Copied"

# ─── Step 3: Run setup_fish.sh inside container ─────────────────────────────
log "[3/4] Running setup_fish.sh..."
echo ""

docker exec "$CONTAINER_NAME" bash -c \
    "cd /root/fish_interfere && chmod +x setup_fish.sh && ./setup_fish.sh --yes"

echo ""
log "  Setup complete"

# ─── Step 4: Commit as fish image ───────────────────────────────────────────
log "[4/4] Committing as $FISH_IMAGE..."

docker commit \
    --change='ENTRYPOINT ["tmuxinator","start","-p","/aas/aircraft.yml.erb"]' \
    "$CONTAINER_NAME" "$FISH_IMAGE"

log "  Committed: $FISH_IMAGE"

# Cleanup (trap will remove container)
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  $FISH_IMAGE built successfully.${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  Test with:${NC}"
echo -e "${GREEN}    cd ~/aerial-autonomy-stack/scripts${NC}"
echo -e "${GREEN}    AUTOPILOT=px4 NUM_QUADS=1 NUM_VTOLS=0 WORLD=swiss_town \\${NC}"
echo -e "${GREEN}      HEADLESS=false RTF=3.0 ./sim_run.sh${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
