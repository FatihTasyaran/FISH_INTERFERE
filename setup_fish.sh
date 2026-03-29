#!/bin/bash
# =============================================================================
# FISH Setup — One-shot installer for container environments
# =============================================================================
# Runs all FISH installation steps in order:
#   1. Install system dependencies (apt packages)
#   2. Build custom tracepoints (overlay workspace)
#   3. Install FISH framework (ros2 wrapper, daemon, Python tools)
#
# Usage:
#   ./setup_fish.sh              # interactive
#   ./setup_fish.sh --yes        # non-interactive (accept all prompts)
#
# After completion, commit the container to bake FISH into your image.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AUTO_FLAG=""
[ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ] && AUTO_FLAG="--yes"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
MINT='\033[38;2;0;255;170m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}FISH Setup${NC}"
echo -e "Framework for Integrated Scheduling Hierarchy"
echo ""
echo "This will:"
echo "  1. Check and install system dependencies (lttng, tracetools, nsys)"
echo "  2. Build custom tracepoints (action server + rclpy callback chain)"
echo "  3. Install FISH framework (ros2 wrapper, GPU daemon, snapshots)"
echo ""

if [ -z "$AUTO_FLAG" ]; then
    echo -n "Continue? [Y/n] "
    read -r REPLY
    if [[ "$REPLY" =~ ^[Nn]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ─── Step 1: Dependencies ───────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Step 1/3: Dependencies ━━━${NC}"
echo ""

if [ ! -x "$SCRIPT_DIR/install_fish_deps.sh" ]; then
    chmod +x "$SCRIPT_DIR/install_fish_deps.sh"
fi
FISH_SETUP_PARENT=1 "$SCRIPT_DIR/install_fish_deps.sh" $AUTO_FLAG

# Verify critical deps are now present
if ! which lttng &>/dev/null; then
    echo -e "${RED}[ERR]${NC} lttng not found after dependency install. Aborting."
    exit 1
fi

# ─── Step 2: Custom tracepoints ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Step 2/3: Custom Tracepoints ━━━${NC}"
echo ""

if [ ! -x "$SCRIPT_DIR/fish_tracepoints/install_fish_tracepoints" ]; then
    chmod +x "$SCRIPT_DIR/fish_tracepoints/install_fish_tracepoints"
fi
FISH_SETUP_PARENT=1 "$SCRIPT_DIR/fish_tracepoints/install_fish_tracepoints" --all

# ─── Step 3: FISH framework ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Step 3/3: FISH Framework ━━━${NC}"
echo ""

# source runs in this shell — .bashrc gets updated, wrapper gets created
set +u && source "$SCRIPT_DIR/install_fish.sh" && set -u

# Enable FISH by default (user can disable with export FISH_ENABLED=0)
FISH_EN_MARKER="# FISH_ENABLED"
if [ -f "$HOME/.bashrc" ]; then
    sed -i "/$FISH_EN_MARKER/d" "$HOME/.bashrc"
    echo "export FISH_ENABLED=1  $FISH_EN_MARKER" >> "$HOME/.bashrc"
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  FISH setup complete.${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  To activate in this shell:${NC}"
echo -e "${GREEN}    source ~/.bashrc${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  Then commit this container to bake FISH into your image:${NC}"
echo -e "${GREEN}    docker commit <container> my-fish-image:latest${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# ─── perf_event_paranoid ─────────────────────────────────────────────────────
PARANOID=$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo "?")
if [ "$PARANOID" != "?" ] && [ "$PARANOID" -gt 1 ]; then
    echo ""
    echo -e "${MINT}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${MINT}  perf_event_paranoid = $PARANOID (needs to be <= 1 for full tracing)${NC}"
    echo -e "${MINT}${NC}"
    echo -e "${MINT}  Run this on the HOST (not inside the container):${NC}"
    echo -e "${MINT}    sudo sysctl -w kernel.perf_event_paranoid=1${NC}"
    echo -e "${MINT}${NC}"
    echo -e "${MINT}  To make it permanent across reboots:${NC}"
    echo -e "${MINT}    echo 'kernel.perf_event_paranoid=1' | sudo tee -a /etc/sysctl.conf${NC}"
    echo -e "${MINT}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
fi
echo ""
