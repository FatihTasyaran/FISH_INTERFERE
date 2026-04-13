#!/bin/bash
# Install FISH into a running Docker container and commit as new image.
#
# Usage:
#   ./container_install.sh <container_name_or_id>
#
# What it does:
#   1. Copies fish_interfere/ into the container
#   2. Runs setup_fish.sh inside (deps + tracepoints + framework)
#   3. Adds FISH source lines to container's .bashrc
#   4. Adds fish_traces volume mount point
#   5. Commits the container as <original_image>-fish:latest
#
# Example:
#   docker run -it --name sim-temp simulation-image bash
#   ./container_install.sh sim-temp
#   # → commits as simulation-image-fish:latest

set -e

CONTAINER="${1:?Usage: $0 <container_name_or_id>}"

# Verify container exists and is running
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" &>/dev/null; then
    echo "Error: container '$CONTAINER' not found or not running"
    exit 1
fi

echo "=== FISH container installer ==="
echo "Container: $CONTAINER"

# Get the original image name (for commit tag)
ORIG_IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")
# Strip :tag if present, strip existing -fish suffix, then append -fish
BASE_IMAGE=$(echo "$ORIG_IMAGE" | sed 's/:.*$//' | sed 's/-fish$//')
NEW_IMAGE="${BASE_IMAGE}-fish:latest"
echo "Original image: $ORIG_IMAGE"
echo "New image:      $NEW_IMAGE"

# 1. Copy fish_interfere into the container
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
echo ""
echo "[1/5] Copying fish_interfere/ into container..."
docker exec "$CONTAINER" rm -rf /root/fish_interfere 2>/dev/null || true
docker cp "$SCRIPT_DIR/." "$CONTAINER":/root/fish_interfere

# 2. Install dependencies + tracepoints + framework
echo ""
echo "[2/5] Running setup_fish.sh..."
docker exec -w /root/fish_interfere "$CONTAINER" bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    # Ensure git is available (needed for some deps)
    apt-get update -qq && apt-get install -y -qq git lttng-tools liblttng-ust-dev babeltrace2 > /dev/null 2>&1 || true
    source setup_fish.sh --yes
'

# 3. Configure .bashrc for FISH
echo ""
echo "[3/5] Configuring .bashrc..."
docker exec "$CONTAINER" bash -c '
    BASHRC="/root/.bashrc"

    # Remove any old FISH block
    sed -i "/# >>> FISH/,/# <<< FISH/d" "$BASHRC"

    # Add FISH block at the END (after all ROS workspace sources)
    cat >> "$BASHRC" << '\''FISHBLOCK'\''

# >>> FISH framework >>>
export FISH_ENABLED=1
if [ -f /opt/ros/humble/fish/trace_overlay_ws/install/local_setup.bash ]; then
    source /opt/ros/humble/fish/trace_overlay_ws/install/local_setup.bash
fi
export PATH="/opt/ros/humble/fish/bin:$PATH"
# <<< FISH framework <<<
FISHBLOCK
    echo "  .bashrc updated"
'

# 4. Create fish_traces directory
echo ""
echo "[4/5] Creating /root/fish_traces..."
docker exec "$CONTAINER" mkdir -p /root/fish_traces

# 5. Commit — restore original entrypoint (container may have been started with --entrypoint sleep)
echo ""
echo "[5/5] Committing as $NEW_IMAGE..."
ORIG_EP=$(docker inspect -f '{{json .Config.Entrypoint}}' "$ORIG_IMAGE" 2>/dev/null || echo '["bash"]')
ORIG_CMD=$(docker inspect -f '{{json .Config.Cmd}}' "$ORIG_IMAGE" 2>/dev/null || echo 'null')
echo "  Restoring entrypoint: $ORIG_EP cmd: $ORIG_CMD"
docker commit \
    --change "ENTRYPOINT $ORIG_EP" \
    --change "CMD ${ORIG_CMD}" \
    "$CONTAINER" "$NEW_IMAGE"

echo ""
echo "=== Done ==="
echo "New image: $NEW_IMAGE"
echo ""
echo "To test:"
echo "  docker run -it --rm --gpus all -v \$HOME/fish_traces:/root/fish_traces $NEW_IMAGE"
echo "  # Inside: which ros2  →  should show /opt/ros/humble/fish/bin/ros2"
