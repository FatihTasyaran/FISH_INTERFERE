# Installation

FISH runs **inside** the container where ROS 2 nodes execute. Copy the
`fish_interfere/` folder into the container, then follow one of the paths below.

## Quick Start (one command)

```bash
docker cp fish_interfere <container>:/root/fish_interfere
docker exec -it <container> bash
cd /root/fish_interfere && ./scripts/setup_fish.sh
source ~/.bashrc
```

Then commit the container and you're done. Every future container started from
this image will trace automatically.

## Installation Scripts

FISH has four install scripts, all under `scripts/`. `scripts/setup_fish.sh`
calls the other three in order, but each can be run independently.

### `scripts/setup_fish.sh`

One-shot installer that runs everything.

**Assumes:** ROS 2 Humble installed, internet access (for apt + git clone).

**Effect:**
- Installs all system dependencies (LTTng, tracetools, colcon, nsys)
- Builds custom tracepoints in an overlay workspace
- Installs FISH ros2 wrapper, GPU daemon, and snapshot tools
- Adds overlay source, PATH, PYTHONPATH, and `FISH_ENABLED=1` to `.bashrc`

```bash
./scripts/setup_fish.sh          # interactive (asks before each install step)
./scripts/setup_fish.sh --yes    # non-interactive (accepts all prompts)
```

### `scripts/install_fish_deps.sh`

Checks and installs container-side dependencies.

**Assumes:** ROS 2 Humble installed.

**Effect:** Installs missing apt packages from the list below. For nsys, adds
the NVIDIA CUDA apt repository and installs `nsight-systems-2025.6.3`. Warns
about `perf_event_paranoid` if it needs to be changed on the host.

```bash
./scripts/install_fish_deps.sh          # interactive
./scripts/install_fish_deps.sh --yes    # non-interactive
```

### `fish_tracepoints/install_fish_tracepoints`

Builds custom LTTng tracepoints in an overlay workspace and patches rclpy.

**Assumes:** ROS 2 Humble, `ros-humble-tracetools-trace`, `ros-humble-ros2trace`,
`git`, `colcon`, `liblttng-ust-dev` installed.

**Effect:**
- Clones `ros2_tracing`, `rclcpp`, `rcl` source at pinned tags
- Patches tracetools C source (tp_call.h, tracetools.h, tracetools.c)
- Builds patched tracetools, rclcpp_action, and rclcpp in overlay workspace at `/root/trace_overlay_ws/`
- Patches rclcpp client.hpp for service client request/response tracing
- Creates `fish_rclpy_trace.py` ctypes bridge in rclpy package directory
- Patches rclpy Python files (subscription.py, service.py, timer.py, node.py, executors.py, action/client.py)
- Updates `tracetools_trace` event list (names.py)
- Adds `source /root/trace_overlay_ws/install/setup.bash` to `.bashrc`

```bash
./install_fish_tracepoints              # all tracepoints (default)
./install_fish_tracepoints --action     # action server only (4 events)
./install_fish_tracepoints --rclpy      # rclpy callback chain only (5 events)
./install_fish_tracepoints --client     # service client only (2 events)
./install_fish_tracepoints --all        # all (same as no flags)
```

### `scripts/install_fish.sh`

Installs the FISH framework: ros2 wrapper, trace session management, GPU
daemon, and snapshot tools.

**Assumes:** ROS 2 Humble installed, `lttng-tools` installed.

**Effect:**
- Creates `/opt/ros/humble/fish/` with wrapper, scripts, and Python package
- Generates `ros2` wrapper that intercepts `ros2 run` and `ros2 launch` for
  automatic tracing when `FISH_ENABLED=1`
- Generates `trace_session.sh` for LTTng session lifecycle management
- Copies FISH Python package (GPU daemon, snapshot, settings)
- Reads `config/fish_events.txt` and `config/fish_settings.ini` (relative to
  the repo root)
- Adds PATH and PYTHONPATH to `.bashrc`

**Must be sourced** (not executed):

```bash
source scripts/install_fish.sh
```

## Baking into a Docker image

After running `scripts/setup_fish.sh` (or the individual scripts), commit the
container to create a reusable image:

```bash
# From the host
docker commit <container> my-fish-image:latest

# If the original image has a specific entrypoint (e.g. tmuxinator):
docker commit --change 'ENTRYPOINT ["tmuxinator","start","-p","/path/to/config.yml.erb"]' \
  <container> my-fish-image:latest
```

The committed image includes:
- Overlay workspace (`/root/trace_overlay_ws/`)
- Patched rclpy files and ctypes bridge
- FISH wrapper, daemon, and Python tools (`/opt/ros/humble/fish/`)
- Updated `.bashrc` (overlay source, PATH, PYTHONPATH, FISH_ENABLED=1)
- All apt dependencies

Any container started from this image is ready to trace.

There is also an automated build script:

```bash
./scripts/build_fish_image.sh                   # default base image (aircraft-image:latest)
./scripts/build_fish_image.sh <base_image>      # custom base image
```

## Host requirements

**perf_event_paranoid** must be `<= 1` on the host for full LTTng tracing:

```bash
# Temporary (until reboot)
sudo sysctl -w kernel.perf_event_paranoid=1

# Permanent
echo 'kernel.perf_event_paranoid=1' | sudo tee -a /etc/sysctl.conf
```

This is a host-level setting and cannot be changed from inside a container.
`scripts/install_fish_deps.sh` and `scripts/setup_fish.sh` will warn if this
needs attention.
