# FISH Manual

FISH (Framework for Integrated Scheduling Hierarchy) is a profiling framework
for ROS 2 systems. It captures LTTng kernel/userspace traces, NVIDIA GPU
profiling data, and system state snapshots — all from inside the container
where your ROS 2 nodes run.

---

## Quick Start (one command)

```bash
docker cp fish_interfere <container>:/root/fish_interfere
docker exec -it <container> bash
cd /root/fish_interfere && ./setup_fish.sh
source ~/.bashrc
```

Then commit the container and you're done. Every future container started from
this image will trace automatically.

---

## Installation Scripts

FISH has four install scripts. `setup_fish.sh` calls the other three in order,
but each can be run independently.

### `setup_fish.sh`

One-shot installer that runs everything.

**Assumes:** ROS 2 Humble installed, internet access (for apt + git clone).

**Effect:**
- Installs all system dependencies (LTTng, tracetools, colcon, nsys)
- Builds custom tracepoints in an overlay workspace
- Installs FISH ros2 wrapper, GPU daemon, and snapshot tools
- Adds overlay source, PATH, PYTHONPATH, and `FISH_ENABLED=1` to `.bashrc`

```bash
./setup_fish.sh          # interactive (asks before each install step)
./setup_fish.sh --yes    # non-interactive (accepts all prompts)
```

---

### `install_fish_deps.sh`

Checks and installs container-side dependencies.

**Assumes:** ROS 2 Humble installed.

**Effect:** Installs missing apt packages from the list below. For nsys, adds
the NVIDIA CUDA apt repository and installs `nsight-systems-2025.6.3`. Warns
about `perf_event_paranoid` if it needs to be changed on the host.

```bash
./install_fish_deps.sh          # interactive
./install_fish_deps.sh --yes    # non-interactive
```

**Dependencies checked:**

| Package | What for |
|---------|----------|
| `lttng-tools` | LTTng session daemon and CLI |
| `liblttng-ust-dev` | LTTng-UST headers (overlay build) |
| `python3-lttng` | Python LTTng bindings |
| `ros-humble-tracetools` | Base libtracetools.so |
| `ros-humble-tracetools-trace` | Tracetools trace session tools |
| `ros-humble-ros2trace` | `ros2 trace` CLI command |
| `git` | Clone tracetools source for overlay |
| `python3-colcon-common-extensions` | Build overlay workspace |
| `nsight-systems-2025.6.3` | NVIDIA Nsight Systems (GPU profiling, optional) |

---

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
- Patches rclpy Python files (subscription.py, service.py, timer.py, node.py, executors.py)
- Updates `tracetools_trace` event list (names.py)
- Adds `source /root/trace_overlay_ws/install/setup.bash` to `.bashrc` (marker: `# FISH_TRACEPOINTS_OVERLAY`)

```bash
./install_fish_tracepoints              # all tracepoints (default)
./install_fish_tracepoints --action     # action server only (4 events)
./install_fish_tracepoints --rclpy      # rclpy callback chain only (5 events)
./install_fish_tracepoints --client     # service client only (2 events)
./install_fish_tracepoints --all        # all (same as no flags)
```

**Action server tracepoints** (4 events, C++ patches to rclcpp_action):

| Event | Description |
|-------|-------------|
| `ros2:fish_rclcpp_action_server_init` | Links action handle to name + goal/cancel/result service handles |
| `ros2:fish_rclcpp_action_execute_goal` | Goal request dispatch entry |
| `ros2:fish_rclcpp_action_execute_cancel` | Cancel request dispatch entry |
| `ros2:fish_rclcpp_action_execute_result` | Result request dispatch entry |

**rclpy callback chain tracepoints** (5 events, ctypes bridge + Python patches):

| Event | Description |
|-------|-------------|
| `ros2:fish_rclpy_subscription_callback_added` | Links subscription handle to Python callback ID |
| `ros2:fish_rclpy_service_callback_added` | Links service handle to Python callback ID |
| `ros2:fish_rclpy_timer_callback_added` | Links timer handle to Python callback ID |
| `ros2:fish_rclpy_timer_link_node` | Links timer handle to parent node handle |
| `ros2:fish_rclpy_callback_register` | Maps callback ID to Python symbol (module.qualname) |

Existing `ros2:callback_start` and `ros2:callback_end` events are reused for
rclpy runtime callback boundaries (injected into rclpy executors.py).

**Service client tracepoints** (2 events, C++ patches to rclcpp client.hpp):

| Event | Description |
|-------|-------------|
| `ros2:fish_rclcpp_client_request_sent` | Client sends request (client_handle + sequence_number) |
| `ros2:fish_rclcpp_client_response_received` | Client receives response (client_handle + sequence_number + response_type) |

`response_type`: 0 = future-only, 1 = callback, 2 = callback with request.
When a callback is present (type 1 or 2), existing `ros2:callback_start` and
`ros2:callback_end` events are fired around the callback invocation.
Round-trip latency = `response_received.ts - request_sent.ts`.

---

### `install_fish.sh`

Installs the FISH framework: ros2 wrapper, trace session management, GPU
daemon, and snapshot tools.

**Assumes:** ROS 2 Humble installed, `lttng-tools` installed.

**Effect:**
- Creates `/opt/ros/humble/fish/` with wrapper, scripts, and Python package
- Generates `ros2` wrapper at `/opt/ros/humble/fish/bin/ros2` that intercepts
  `ros2 run` and `ros2 launch` for automatic tracing when `FISH_ENABLED=1`
- Generates `trace_session.sh` for LTTng session lifecycle management
- Copies FISH Python package (GPU daemon, snapshot, settings)
- Adds PATH and PYTHONPATH to `.bashrc`

**Must be sourced** (not executed):

```bash
source install_fish.sh
```

---

## Usage

After installation and image commit, FISH is active by default
(`FISH_ENABLED=1` is in `.bashrc`).

```bash
# Tracing is automatic — just launch your nodes
ros2 run <pkg> <node>
ros2 launch <pkg> <launch_file>

# Disable auto-tracing (ros2 works normally, overlay stays active)
export FISH_ENABLED=0

# Re-enable
export FISH_ENABLED=1

# FISH subcommands
ros2 run fish                   # wrapper check
ros2 run fish status            # session + daemon info
ros2 run fish stop              # stop (snapshot + flush + save)
ros2 run fish snapshot          # manual shutdown snapshot
ros2 run fish gpu               # list GPU processes
ros2 run fish gpu relaunch      # manual nsys relaunch
ros2 run fish gpu daemon fg     # run daemon in foreground
```

### Trace output

Each session is saved to `~/fish_traces/<session>/`:

```
~/fish_traces/fish_20260329_182842/
  ros2/         — LTTng binary trace (CTF format)
  nsys/         — Nsight Systems reports (.nsys-rep + .sqlite)
  fishlog/      — FISH event log (kill/resurrect timestamps)
  snapshot/     — System state (process tree, node info, topic info/hz)
```

### Volume mount

To access trace data from the host, mount the trace directory when starting
the container:

```bash
docker run ... -v $HOME/fish_traces:/root/fish_traces ...
```

FISH sets trace output permissions to world-readable on session stop, so the
host user can read the files directly.

---

## Baking into a Docker image

After running `setup_fish.sh` (or the individual scripts), commit the
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

Any container started from this image is ready to trace — no additional setup.

---

## Host requirements

**perf_event_paranoid** must be `<= 1` on the host for full LTTng tracing:

```bash
# Temporary (until reboot)
sudo sysctl -w kernel.perf_event_paranoid=1

# Permanent
echo 'kernel.perf_event_paranoid=1' | sudo tee -a /etc/sysctl.conf
```

This is a host-level setting — it cannot be changed from inside a container.
`install_fish_deps.sh` and `setup_fish.sh` will warn if this needs attention.

---

## Notes

- `FISH_ENABLED=0` only disables automatic trace session management. The
  overlay workspace remains active (sourced from `.bashrc`). LTTng-UST
  tracepoints have zero overhead when no trace session is running.
- All install scripts are idempotent — safe to run multiple times.
- Custom tracepoint patches use markers (`# FISH_TRACEPOINTS_OVERLAY`,
  `# FISH_ENABLED`) in `.bashrc` so they can be cleanly updated or removed
  without affecting other entries.
