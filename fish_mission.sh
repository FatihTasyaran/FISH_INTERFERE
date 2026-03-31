#!/bin/bash
# =============================================================================
# FISH Mission Script — Automated action sequence for trace capture
# =============================================================================
# Runs a predefined mission (takeoff → orbit → land) inside the aircraft
# container via docker exec. Designed to run on the HOST while sim_run.sh
# is active.
#
# Prerequisites:
#   - sim_run.sh is running (containers are up)
#   - FISH is installed and enabled in the aircraft container
#
# Usage:
#   ./fish_mission.sh                              # default container name
#   ./fish_mission.sh <container_name>             # custom container name
#   INSTANCE=1 ./fish_mission.sh                   # for multi-instance
#
# Sim command:
#   AUTOPILOT=px4 NUM_QUADS=1 NUM_VTOLS=0 WORLD=swiss_town \
#     HEADLESS=false RTF=3.0 ./sim_run.sh
# =============================================================================
set -euo pipefail

INSTANCE="${INSTANCE:-0}"
DRONE_ID="${DRONE_ID:-1}"
CONTAINER="${1:-aircraft-container-inst${INSTANCE}_${DRONE_ID}}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[MISSION]${NC} $*"; }
warn() { echo -e "${YELLOW}[MISSION]${NC} $*"; }
err()  { echo -e "${RED}[MISSION]${NC} $*" >&2; }

MANIFEST="/tmp/fish_mission_$(date +%Y%m%d_%H%M%S).log"

record() {
    local ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    echo "$ts  $*" | tee -a "$MANIFEST"
}

# Source chain for ros2 commands inside the container
SOURCE="source /opt/ros/humble/setup.bash && \
source /aas/github_ws/install/setup.bash && \
source /aas/aircraft_ws/install/setup.bash && \
source /root/trace_overlay_ws/install/setup.bash"

send_action() {
    local action_name=$1
    local action_type=$2
    local params=$3
    local timeout=${4:-120}

    record "SEND  $action_name  $params"
    log "Sending: $action_name"

    local output
    output=$(docker exec "$CONTAINER" bash -c \
        "$SOURCE && timeout $timeout ros2 action send_goal \
        /Drone${DRONE_ID}/${action_name} \
        autopilot_interface_msgs/action/${action_type} \
        '${params}' --feedback" 2>&1) || true

    if echo "$output" | grep -q "SUCCEEDED"; then
        record "OK    $action_name  SUCCEEDED"
        log "  $action_name → SUCCEEDED"
        return 0
    elif echo "$output" | grep -q "ABORTED"; then
        record "FAIL  $action_name  ABORTED"
        warn "  $action_name → ABORTED"
        return 1
    elif echo "$output" | grep -q "rejected"; then
        record "FAIL  $action_name  REJECTED"
        warn "  $action_name → REJECTED"
        return 1
    else
        record "FAIL  $action_name  UNKNOWN: $(echo "$output" | tail -1)"
        warn "  $action_name → $(echo "$output" | tail -1)"
        return 1
    fi
}

# ─── Pre-flight ──────────────────────────────────────────────────────────────
log "Container: $CONTAINER"
log "Drone ID:  $DRONE_ID"
log "Manifest:  $MANIFEST"
log ""

# Check container is running
if ! docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
    err "Container $CONTAINER is not running."
    err "Start sim_run.sh first."
    exit 1
fi

# Wait for nodes to be ready
log "Waiting for nodes to initialize..."
for i in $(seq 1 30); do
    NODES=$(docker exec "$CONTAINER" bash -c \
        "$SOURCE && ros2 node list 2>/dev/null" 2>/dev/null | grep -c "Drone" || true)
    if [ "$NODES" -ge 2 ]; then
        log "  $NODES drone nodes found."
        break
    fi
    if [ "$i" -eq 30 ]; then
        err "Timeout waiting for nodes. Only $NODES found."
        exit 1
    fi
    sleep 2
done

# Wait for action servers to register AND PX4 to be ready (GPS fix, EKF init)
log "Waiting for action servers and PX4 to be ready..."
for i in $(seq 1 60); do
    ACTIONS=$(docker exec "$CONTAINER" bash -c \
        "$SOURCE && ros2 action list 2>/dev/null" 2>/dev/null | grep -c "action" || true)
    if [ "$ACTIONS" -ge 4 ]; then
        log "  $ACTIONS action servers found."
        break
    fi
    if [ "$i" -eq 60 ]; then
        warn "Timeout waiting for action servers. Only $ACTIONS found. Continuing anyway."
        break
    fi
    sleep 2
done

# Extra settle time for PX4 SITL (GPS fix, EKF convergence)
log "Settling (30s for PX4 SITL init)..."
sleep 30
log "Ready."
record "START  mission"
log ""

# ─── Mission sequence ───────────────────────────────────────────────────────
log "=== Mission Start ==="
log ""

# 1. Takeoff
if send_action "takeoff_action" "Takeoff" "{takeoff_altitude: 40.0}"; then
    sleep 10

    # 2. Orbit
    send_action "orbit_action" "Orbit" "{east: 200.0, north: 0.0, altitude: 80.0, radius: 100.0}" || true
    sleep 10

    # 3. Land
    send_action "land_action" "Land" "{landing_altitude: 10.0}" || true
    sleep 10
else
    warn "Takeoff failed, skipping remaining actions"
fi

record "END    mission"

log ""
log "=== Mission Complete ==="
log ""
log "Manifest: $MANIFEST"
log ""

# Show manifest
echo ""
echo "─── Mission Manifest ───"
cat "$MANIFEST"
echo "────────────────────────"
