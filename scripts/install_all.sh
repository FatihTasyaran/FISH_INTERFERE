#!/bin/bash
# Install/update FISH in all AAS containers in parallel.
#
# Usage:
#   ./install_all.sh           # Fresh install: base images → *-fish:latest
#   ./install_all.sh --cont    # Update: *-fish:latest → *-fish:latest (keeps deps)
#
# What happens:
#   1. Starts temporary containers from each image (in parallel)
#   2. Runs container_install.sh on each (in parallel)
#   3. Removes temporary containers
#
# Images:
#   Fresh:  simulation-image:latest  → simulation-image-fish:latest
#           ground-image:latest      → ground-image-fish:latest
#           aircraft-image:latest    → aircraft-image-fish:latest
#
#   --cont: simulation-image-fish:latest → simulation-image-fish:latest (overwrite)
#           ground-image-fish:latest     → ground-image-fish:latest
#           aircraft-image-fish:latest   → aircraft-image-fish:latest

set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONT_FLAG=false

if [[ "$1" == "--cont" ]]; then
    CONT_FLAG=true
    echo "=== FISH update mode (--cont): installing on existing -fish images ==="
else
    echo "=== FISH fresh install: base images → -fish images ==="
fi

# Define images
if $CONT_FLAG; then
    IMAGES=("simulation-image-fish:latest" "ground-image-fish:latest" "aircraft-image-fish:latest")
else
    IMAGES=("simulation-image:latest" "ground-image:latest" "aircraft-image:latest")
fi

TEMP_NAMES=("fish-install-sim" "fish-install-gnd" "fish-install-air")
LABELS=("simulation" "ground" "aircraft")

# Cleanup on exit
cleanup() {
    echo ""
    echo "Cleaning up temporary containers..."
    for name in "${TEMP_NAMES[@]}"; do
        docker rm -f "$name" 2>/dev/null || true
    done
}
trap cleanup EXIT

# 1. Start all temporary containers in parallel
echo ""
echo "[1/3] Starting temporary containers..."
PIDS=()
for i in "${!IMAGES[@]}"; do
    img="${IMAGES[$i]}"
    name="${TEMP_NAMES[$i]}"
    label="${LABELS[$i]}"

    # Check image exists
    if ! docker image inspect "$img" &>/dev/null; then
        echo "  SKIP $label: image '$img' not found"
        continue
    fi

    echo "  Starting $name from $img..."
    docker run -d --name "$name" --gpus all --entrypoint sleep "$img" infinity &>/dev/null &
    PIDS+=($!)
done

# Wait for all docker run commands
for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
done

# Give containers a moment to fully start
sleep 2

# Verify all running
for i in "${!TEMP_NAMES[@]}"; do
    name="${TEMP_NAMES[$i]}"
    label="${LABELS[$i]}"
    if docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q true; then
        echo "  $label: running"
    else
        echo "  $label: FAILED to start"
    fi
done

# 2. Run container_install.sh on each in parallel
echo ""
echo "[2/3] Installing FISH in parallel..."

install_one() {
    local name="$1"
    local label="$2"
    local logfile="/tmp/fish_install_${label}.log"

    if ! docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q true; then
        echo "  [$label] SKIP — container not running"
        return 1
    fi

    echo "  [$label] Installing... (log: $logfile)"
    "$SCRIPT_DIR/container_install.sh" "$name" > "$logfile" 2>&1
    local rc=$?
    if [ $rc -eq 0 ]; then
        echo "  [$label] DONE"
    else
        echo "  [$label] FAILED (see $logfile)"
    fi
    return $rc
}

# Launch all installs in background
BG_PIDS=()
for i in "${!TEMP_NAMES[@]}"; do
    name="${TEMP_NAMES[$i]}"
    label="${LABELS[$i]}"
    install_one "$name" "$label" &
    BG_PIDS+=($!)
done

# Wait for all
FAILED=0
for i in "${!BG_PIDS[@]}"; do
    if ! wait "${BG_PIDS[$i]}"; then
        FAILED=$((FAILED + 1))
    fi
done

# 3. Summary
echo ""
echo "[3/3] Results:"
for label in "${LABELS[@]}"; do
    img="${label}-image-fish:latest"
    if docker image inspect "$img" &>/dev/null; then
        size=$(docker image inspect -f '{{.Size}}' "$img" | awk '{printf "%.1f GB", $1/1024/1024/1024}')
        echo "  $img  ($size)"
    else
        echo "  $img  NOT FOUND"
    fi
done

if [ $FAILED -gt 0 ]; then
    echo ""
    echo "WARNING: $FAILED installation(s) failed. Check /tmp/fish_install_*.log"
    exit 1
fi

echo ""
echo "=== All done ==="
