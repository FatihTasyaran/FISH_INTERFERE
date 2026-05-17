#!/bin/bash
# =============================================================================
# FISH Auto Mission — Full automated trace session
# =============================================================================
# Starts simulation, waits for initialization, runs mission, stops everything.
# Produces a complete FISH trace session with known action sequence.
#
# Usage:
#   ./fish_auto_mission.sh
#
# Output:
#   ~/fish_traces/fish_<timestamp>/   — trace data
#   /tmp/fish_mission_<timestamp>.log — mission manifest
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SIM_DIR="${SIM_DIR:-$HOME/aerial-autonomy-stack/scripts}"
INSTANCE="${INSTANCE:-0}"
DRONE_ID="${DRONE_ID:-1}"
CONTAINER="aircraft-container-inst${INSTANCE}_${DRONE_ID}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

log() { echo -e "${GREEN}[AUTO]${NC} $*"; }

# ─── Check sim_run.sh exists ────────────────────────────────────────────────
if [ ! -f "$SIM_DIR/sim_run.sh" ]; then
    echo "sim_run.sh not found at $SIM_DIR/sim_run.sh"
    echo "Set SIM_DIR to the correct path."
    exit 1
fi

# ─── Step 1: Start simulation ───────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Step 1/3: Starting simulation ━━━${NC}"
echo ""

log "Launching sim_run.sh in background..."

# sim_run.sh blocks on "read -n 1 -s" — run it in background with a pipe
# so we can send a keystroke later to shut it down
SIM_FIFO="/tmp/fish_sim_fifo_$$"
mkfifo "$SIM_FIFO"

(
    cd "$SIM_DIR"
    AUTOPILOT=px4 NUM_QUADS=1 NUM_VTOLS=0 WORLD=swiss_town \
        HEADLESS=false RTF=3.0 \
        bash sim_run.sh < "$SIM_FIFO"
) &
SIM_PID=$!

# Keep the fifo open for writing (so sim_run.sh's read doesn't get EOF)
exec 3>"$SIM_FIFO"

log "sim_run.sh started (PID $SIM_PID)"
log "Waiting for containers to come up..."

# Wait for aircraft container to be running
for i in $(seq 1 60); do
    if docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
        log "  $CONTAINER is running"
        break
    fi
    if [ "$i" -eq 60 ]; then
        log "Timeout waiting for container. Aborting."
        echo "q" >&3
        exec 3>&-
        rm -f "$SIM_FIFO"
        exit 1
    fi
    sleep 2
done

# ─── Step 2: Run mission ────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Step 2/3: Running mission ━━━${NC}"
echo ""

"$SCRIPT_DIR/fish_mission.sh" "$CONTAINER" || true

# ─── Step 3: Stop simulation ────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Step 3/3: Stopping simulation ━━━${NC}"
echo ""

log "Sending stop signal to sim_run.sh..."

# Send a keystroke to sim_run.sh's read
echo "q" >&3
exec 3>&-

# Wait for sim_run.sh to finish cleanup
log "Waiting for cleanup..."
wait $SIM_PID 2>/dev/null || true

rm -f "$SIM_FIFO"

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Auto mission complete.${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  Trace data: ~/fish_traces/ (latest session)${NC}"
echo -e "${GREEN}  Manifest:   /tmp/fish_mission_*.log (latest)${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
