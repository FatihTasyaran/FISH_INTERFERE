#!/bin/bash
# =============================================================================
# FISH Dependency Installer
# =============================================================================
# Checks and installs all container-side dependencies required by FISH.
# Assumes ROS 2 Humble is already installed.
#
# Usage:
#   ./install_fish_deps.sh          # interactive (asks before installing)
#   ./install_fish_deps.sh --yes    # non-interactive (install all missing)
# =============================================================================
set -euo pipefail

AUTO_YES=false
[ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ] && AUTO_YES=true

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
MINT='\033[38;2;0;255;170m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $*"; }
miss() { echo -e "  ${YELLOW}[--]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*" >&2; }

ask_yn() {
    if $AUTO_YES; then
        return 0
    fi
    local prompt=$1
    echo -n "$prompt [y/N] "
    read -r REPLY
    [[ "$REPLY" =~ ^[Yy]$ ]]
}

# ─── Check ROS 2 Humble ─────────────────────────────────────────────────────
if [ ! -f /opt/ros/humble/setup.bash ]; then
    err "ROS 2 Humble not found at /opt/ros/humble."
    err "FISH requires a working ROS 2 Humble installation."
    exit 1
fi
echo -e "${GREEN}[FISH]${NC} ROS 2 Humble found"
echo ""

# ─── Define dependencies ────────────────────────────────────────────────────
# Each entry: "check_command|apt_package|description"
DEPS=(
    # Tracing (LTTng)
    "which lttng|lttng-tools|LTTng session daemon and CLI"
    "dpkg -s liblttng-ust-dev|liblttng-ust-dev|LTTng-UST development headers (tracetools build)"
    "dpkg -s python3-lttng|python3-lttng|Python LTTng bindings (ros2 trace)"
    # ROS tracing packages
    "dpkg -s ros-humble-tracetools|ros-humble-tracetools|ROS 2 tracetools (libtracetools.so)"
    "dpkg -s ros-humble-tracetools-trace|ros-humble-tracetools-trace|tracetools trace session tools"
    "dpkg -s ros-humble-ros2trace|ros-humble-ros2trace|ros2 trace CLI command"
    # Build tools (for overlay workspace)
    "which git|git|Git (clone tracetools source)"
    "which colcon|python3-colcon-common-extensions|colcon build system"
)

# ─── Check each dependency ───────────────────────────────────────────────────
echo "Checking dependencies..."
echo ""

MISSING_APT=()
MISSING_NAMES=()

for entry in "${DEPS[@]}"; do
    IFS='|' read -r check pkg desc <<< "$entry"
    if eval "$check" &>/dev/null; then
        ok "$desc"
    else
        miss "$desc  -->  $pkg"
        MISSING_APT+=("$pkg")
        MISSING_NAMES+=("$desc")
    fi
done

# nsys check (separate — needs CUDA apt repo)
NSYS_MISSING=false
if which nsys &>/dev/null; then
    ok "NVIDIA Nsight Systems (GPU profiling)"
else
    miss "NVIDIA Nsight Systems (GPU profiling)"
    NSYS_MISSING=true
fi

echo ""

# ─── Install missing apt packages ───────────────────────────────────────────
if [ ${#MISSING_APT[@]} -gt 0 ]; then
    echo -e "${YELLOW}Missing ${#MISSING_APT[@]} package(s):${NC}"
    for i in "${!MISSING_APT[@]}"; do
        echo "  - ${MISSING_APT[$i]}  (${MISSING_NAMES[$i]})"
    done
    echo ""

    if ask_yn "Install missing packages?"; then
        echo ""
        apt-get update -qq
        apt-get install -y --no-install-recommends "${MISSING_APT[@]}"
        echo ""
        echo -e "${GREEN}[FISH]${NC} apt packages installed."
    else
        echo ""
        echo "Skipped. Install manually:"
        echo "  apt install ${MISSING_APT[*]}"
    fi
    echo ""
else
    echo -e "${GREEN}All apt dependencies satisfied.${NC}"
    echo ""
fi

# ─── Install nsys ────────────────────────────────────────────────────────────
NSYS_PKG="nsight-systems-2025.6.3"
CUDA_KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb"
CUDA_REPO="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64"

if $NSYS_MISSING; then
    echo -e "${YELLOW}NVIDIA Nsight Systems is not installed.${NC}"
    echo "FISH uses nsys for GPU kernel profiling (optional but recommended)."
    echo ""
    echo "The following steps will be performed:"
    echo "  1. Download and install CUDA apt keyring from:"
    echo "     $CUDA_KEYRING_URL"
    echo "  2. apt update"
    echo "  3. apt install $NSYS_PKG"
    echo ""
    echo "Manual: https://docs.nvidia.com/nsight-systems/InstallationGuide/index.html"
    echo ""

    if ask_yn "Install $NSYS_PKG?"; then
        echo ""

        # Add CUDA apt repo if not present
        if [ ! -f /usr/share/keyrings/cuda-archive-keyring.gpg ] && \
           ! ls /etc/apt/sources.list.d/cuda* &>/dev/null; then
            echo "Adding CUDA apt repository..."
            local_deb="/tmp/cuda-keyring.deb"
            wget -q "$CUDA_KEYRING_URL" -O "$local_deb"
            dpkg -i "$local_deb"
            rm -f "$local_deb"
        else
            echo "CUDA apt repository already configured."
        fi

        apt-get update -qq
        apt-get install -y --no-install-recommends "$NSYS_PKG"
        echo ""

        # Ensure nsys is in PATH
        NSYS_BIN=$(find /opt/nvidia/nsight-systems -name "nsys" -type f 2>/dev/null | head -1)
        if [ -n "$NSYS_BIN" ] && ! which nsys &>/dev/null; then
            ln -sf "$NSYS_BIN" /usr/local/bin/nsys
            echo "Symlinked: $NSYS_BIN -> /usr/local/bin/nsys"
        fi

        echo -e "${GREEN}[FISH]${NC} nsys installed: $(nsys --version 2>&1 | head -1)"
    else
        echo ""
        echo "Skipped. GPU profiling will not be available."
    fi
    echo ""
fi

# ─── Summary (skip when called from setup_fish.sh) ──────────────────────────
if [ "${FISH_SETUP_PARENT:-}" != "1" ]; then
    echo -e "${GREEN}[FISH]${NC} Dependency check complete. Next steps:"
    echo "  1. source install_fish.sh"
    echo "  2. cd fish_tracepoints && ./install_fish_tracepoints"
    echo ""

    PARANOID=$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo "?")
    if [ "$PARANOID" != "?" ] && [ "$PARANOID" -gt 1 ]; then
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
fi
