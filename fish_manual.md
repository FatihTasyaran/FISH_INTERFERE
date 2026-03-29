# FISH Manual

FISH (Framework for Integrated Scheduling Hierarchy) is a profiling framework
for ROS 2 systems. It captures LTTng kernel/userspace traces, NVIDIA GPU
profiling data, and system state snapshots.

## Installation

FISH runs **inside** the container where ROS 2 nodes execute. Copy the
`fish_interfere/` folder into the container, then follow one of the paths below.

### Quick install (tracing dependencies already present)

If the container already has `lttng-tools`, `ros-humble-tracetools-trace`, and
build tools (`git`, `colcon`), you only need:

```bash
# 1. Copy FISH into the container
docker cp fish_interfere <container>:/root/fish_interfere

# 2. Inside the container:
cd /root/fish_interfere
source install_fish.sh                              # FISH wrapper + daemon
cd fish_tracepoints && ./install_fish_tracepoints   # custom tracepoints (overlay)
```

### Full install (bare ROS 2 Humble container)

If starting from a container with only ROS 2 Humble:

```bash
# 1. Copy FISH into the container
docker cp fish_interfere <container>:/root/fish_interfere

# 2. Inside the container:
cd /root/fish_interfere
./install_fish_deps.sh          # checks & installs missing apt packages
source install_fish.sh
cd fish_tracepoints && ./install_fish_tracepoints
```

`install_fish_deps.sh` will list missing packages and ask before installing.
Use `--yes` flag to skip the prompt.

### Baking into a Docker image

After installation, commit the container so you don't repeat the setup:

```bash
# From the host:
docker commit <container> my-fish-image:latest

# If the original image uses a specific entrypoint (e.g. tmuxinator):
docker commit --change 'ENTRYPOINT ["tmuxinator","start","-p","/aas/aircraft.yml.erb"]' \
  <container> my-fish-image:latest
```

The committed image includes the overlay workspace, patched `.bashrc`, and all
FISH components. Any container started from this image is ready to trace.

## Custom tracepoints

`install_fish_tracepoints` adds custom LTTng tracepoints via an overlay workspace.

```bash
./install_fish_tracepoints              # install all (default)
./install_fish_tracepoints --action     # action server tracepoints only (4 events)
./install_fish_tracepoints --rclpy      # rclpy callback chain only (5 events)
./install_fish_tracepoints --all        # both (same as no flags)
```

**Action server tracepoints** (C++ patches to rclcpp_action):
- `ros2:rclcpp_action_server_init` — links action handle to name + services
- `ros2:rclcpp_action_execute_goal` — goal request dispatch
- `ros2:rclcpp_action_execute_cancel` — cancel request dispatch
- `ros2:rclcpp_action_execute_result` — result request dispatch

**rclpy callback chain tracepoints** (Python ctypes bridge + rclpy patches):
- `ros2:rclpy_subscription_callback_added` — subscription handle → callback
- `ros2:rclpy_service_callback_added` — service handle → callback
- `ros2:rclpy_timer_callback_added` — timer handle → callback
- `ros2:rclpy_timer_link_node` — timer handle → node handle
- `ros2:rclpy_callback_register` — callback ID → symbol string

The installer also patches `.bashrc` so the overlay is sourced automatically
in new shell sessions.

## Usage

```bash
# Enable tracing
export FISH_ENABLED=1

# Launch nodes as usual — tracing is automatic
ros2 run <pkg> <node>
ros2 launch <pkg> <launch_file>

# Check status
ros2 run fish status

# Stop tracing (takes snapshot, flushes GPU reports, saves trace)
ros2 run fish stop
```

Trace output is saved to `~/fish_traces/<session>/`.

## Dependencies

**Container-side (required):**

| Package | What for |
|---------|----------|
| `lttng-tools` | LTTng session daemon and CLI |
| `liblttng-ust-dev` | LTTng-UST headers (overlay build) |
| `python3-lttng` | Python LTTng bindings |
| `ros-humble-tracetools` | Base libtracetools.so |
| `ros-humble-tracetools-trace` | `ros2 trace` command |
| `git` | Clone tracetools source |
| `python3-colcon-common-extensions` | Build overlay workspace |
| `nsys` (optional) | NVIDIA Nsight Systems for GPU profiling |

Run `./install_fish_deps.sh` to check and install all at once.
