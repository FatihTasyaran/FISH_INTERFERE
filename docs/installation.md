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

FISH has three core install scripts (`scripts/setup_fish.sh` calls the other
two in order; `fish_tracepoints/install_fish_tracepoints` lives next to the
patches it applies) plus helpers for putting FISH into existing images
(`scripts/container_install.sh`, `scripts/install_all.sh`,
`docker/fish-base.Dockerfile`, `scripts/build_fish_image.sh`) — see
`images.md` for which route each reference workload uses.

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
`babeltrace2` (used by the discards report at session stop) is installed by
`container_install.sh` and the Dockerfiles; add it manually on other paths.

```bash
./scripts/install_fish_deps.sh          # interactive
./scripts/install_fish_deps.sh --yes    # non-interactive
```

### `fish_tracepoints/install_fish_tracepoints`

Builds custom LTTng tracepoints in an overlay workspace and patches rclpy.

**Assumes:** ROS 2 Humble, `ros-humble-tracetools-trace`, `ros-humble-ros2trace`,
`git`, `colcon`, `liblttng-ust-dev` installed.

**Effect:**
- Clones `ros2_tracing` (tracetools 4.1.2), `rclcpp` (16.0.19), `rcl` (5.3.12) at pinned tags
- Patches tracetools (tp_call.h, tracetools.h, tracetools.c, names.py): 22 FISH
  events + the `__fish_active_callback` thread-local
- Patches rcl (publisher.c, client.c), rclcpp (client.hpp, node_timers.cpp,
  callback_group.cpp, executor.cpp incl. the Waitable dispatch wrap,
  executors/*.cpp, node_waitables.cpp, intra_process_manager.cpp,
  publisher_base.cpp, publisher.hpp, generic_subscription.hpp), rclcpp_action
  (server.cpp) and builds them in the overlay workspace `/root/trace_overlay_ws/`
- Creates the `fish_rclpy_trace.py` ctypes bridge and patches rclpy in place
  (subscription.py, service.py, timer.py, node.py, executors.py,
  callback_groups.py, client.py, action/client.py)
- If `/root/ros_ws/src/isaac_ros_nitros` exists: patches and rebuilds
  isaac_ros_nitros (NITROS publish/receive links + GXF topology events)
- Adds `source /root/trace_overlay_ws/install/setup.bash` to `.bashrc`

The full event list, payloads and probe sites are in `tracepoints.md`.

```bash
./install_fish_tracepoints              # all groups (default)
./install_fish_tracepoints --action     # action server dispatch (4 events)
./install_fish_tracepoints --rclpy      # rclpy callback chain (5 events)
./install_fish_tracepoints --client     # service client RTT + atomic timer init (3 events)
./install_fish_tracepoints --link       # callback-link attribution + NITROS topology (6 events)
./install_fish_tracepoints --scheduler  # executor / callback-group introspection (4 events)
./install_fish_tracepoints --all        # all (same as no flags)
```

An image whose overlay was built with an older installer is never patched in
place: build a new tag (e.g. `autoware-dev-trt-a1000-fishwait5`).

### `scripts/install_fish.sh`

Installs the FISH framework: ros2 wrapper, trace session management, GPU
daemon, and snapshot tools.

**Assumes:** ROS 2 Humble installed, `lttng-tools` installed.

**Effect:**
- Creates `/opt/ros/humble/fish/` with wrapper, scripts, Python package,
  `fish_settings.ini` and the `config/*.xml` DDS profiles
- Generates the `ros2` wrapper (RMW guard, Cyclone URI guard, LTTng session,
  `ros2 launch` routed through `fish.launch_wrap` so composable containers
  start under nsys, launch inspection → `launch_components.json`) and the
  `launch_test` wrapper (same, for ros2_benchmark / launch_testing suites)
- Generates `trace_session.sh` (start/inc/dec/stop; per-instance LTTng
  channel with `/dev/shm` guard; nsys drain; `fishlog/discards.txt`)
- Copies the FISH Python package (GPU daemon, launch_wrap, snapshot, settings)
- Reads `config/fish_events.txt` and `config/fish_settings.ini` (relative to
  the repo root)
- Adds PATH and PYTHONPATH to `.bashrc` (skipped with `FISH_SETUP_PARENT=1`,
  the mode the run scripts use to refresh FISH inside a fresh container)

How the wrappers behave at run time is described in `operating_modes.md`.

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
- FISH wrapper, launch_test wrapper, daemon, Python tools, `fish_settings.ini`
  and DDS XML profiles (`/opt/ros/humble/fish/`)
- Updated `.bashrc` (overlay source, PATH, PYTHONPATH, FISH_ENABLED=1)
- All apt dependencies

Alternatively keep the image untouched and refresh FISH per run
(`docker cp` the repo into a fresh container + `FISH_SETUP_PARENT=1 source
scripts/install_fish.sh`) — the route used by `scripts/autoware_runs/*.sh`.

Any container started from this image is ready to trace.

There is also an automated build script:

```bash
./scripts/build_fish_image.sh                   # default base image (aircraft-image:latest)
./scripts/build_fish_image.sh <base_image>      # custom base image
```

## Host requirements

**Container flags.** Every FISH container needs `--privileged` (LTTng, nsys),
`--gpus all` for CUDA workloads and, when `[trace] per_instance = true`,
`--shm-size=2g`: LTTng-UST ring buffers live in `/dev/shm` and Docker's
64 MB default silently yields an empty trace (ISSUE-005 in
`notes/known_issues.txt`). Mount `-v $HOME/fish_traces:/root/fish_traces`
for the sessions.

**DDS (Autoware-class stacks, Cyclone DDS).** On the host:
`sudo sysctl -w net.core.rmem_max=2147483647 net.ipv4.ipfrag_time=3
net.ipv4.ipfrag_high_thresh=134217728` and `sudo ip link set lo multicast on`
(persist the sysctls in `/etc/sysctl.d/`; lo multicast is not persistent).
Set `[ros] rmw_implementation` in `fish_settings.ini` so non-interactive
shells do not fall back to Fast DDS; leave `cyclonedds_uri` empty unless you
know the XML helps (Autoware's does not on a laptop —
`notes/about_docs/autoware_dds_settings.txt`).

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
