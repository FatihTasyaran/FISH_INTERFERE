# Usage

After installation and image commit, FISH is active by default
(`FISH_ENABLED=1` is in `.bashrc`).

## Basic commands

```bash
# Tracing is automatic — just launch your nodes
ros2 run <pkg> <node>
ros2 launch <pkg> <launch_file>

# Disable auto-tracing (ros2 works normally, overlay stays active)
export FISH_ENABLED=0

# Re-enable
export FISH_ENABLED=1
```

## FISH subcommands

```bash
ros2 run fish                   # wrapper check
ros2 run fish status            # session + daemon info
ros2 run fish stop              # stop (snapshot + flush + save)
ros2 run fish snapshot          # manual shutdown snapshot
ros2 run fish gpu               # list GPU processes
ros2 run fish gpu relaunch      # manual nsys relaunch
ros2 run fish gpu daemon fg     # run daemon in foreground
```

## Trace output

Each session is saved to `~/fish_traces/<session>/`:

```
~/fish_traces/fish_20260329_182842/
  ros2/         — LTTng binary trace (CTF format)
  nsys/         — Nsight Systems reports (.nsys-rep + .sqlite)
  fishlog/      — FISH event log (kill/resurrect timestamps)
  snapshot/     — System state (process tree, node info, topic info/hz)
```

## Volume mount

To access trace data from the host, mount the trace directory when starting
the container:

```bash
docker run ... -v $HOME/fish_traces:/root/fish_traces ...
```

FISH sets trace output permissions to world-readable on session stop, so the
host user can read the files directly.

## Using `ros2 launch` with FISH

FISH intercepts `ros2 launch` via a shell wrapper installed at
`/opt/ros/humble/fish/bin/ros2`. When `FISH_ENABLED=1`, this wrapper
automatically runs the launch inspection and wrapping pipeline before
delegating to the real `ros2 launch`:

1. **`launch_inspect`** — statically walks the launch description to
   discover all `ComposableNodeContainer` instances and their loaded
   plugins. GPU-hosting containers are identified by keyword matching
   against the plugin class names (e.g. `tensorrt`, `centerpoint`,
   `yolox`). The result is written to
   `<session>/launch_components.json`.

2. **`launch_wrap`** — monkey-patches `ComposableNodeContainer.execute()`
   so that GPU containers are started with an `nsys profile` prefix from
   the very beginning. Non-GPU containers are left untouched.

3. **`ros2 launch`** — the real launch proceeds normally. Components are
   loaded into the (now nsys-wrapped) containers by the launch system
   without any additional intervention.

### PATH ordering requirement

The FISH wrapper must appear **before** the system `ros2` binary in
`$PATH`. ROS 2 workspace setup scripts (`setup.bash`) rebuild `$PATH`
from scratch, which can push the FISH wrapper behind
`/opt/ros/humble/bin/ros2`. When this happens, `ros2 launch` bypasses
FISH entirely — no tracing, no GPU wrapping, no daemon.

To prevent this, FISH paths must be re-exported **after** all workspace
sources. The recommended `.bashrc` ordering is:

```bash
# 1. ROS 2 base
source /opt/ros/humble/setup.bash

# 2. Application workspace (Autoware, AAS, etc.)
source /opt/autoware/setup.bash          # or your workspace

# 3. FISH (must be LAST — re-asserts wrapper priority)
export PATH=/opt/ros/humble/fish/bin:$PATH
export PYTHONPATH=/opt/ros/humble/fish/python:$PYTHONPATH
source /root/trace_overlay_ws/install/local_setup.bash
export FISH_ENABLED=1
```

Note that the tracepoint overlay uses `local_setup.bash` (not
`setup.bash`) so it only adds the overridden libraries
(`libtracetools.so`, `librclcpp_action.so`) to the environment without
resetting the application workspace paths already in
`AMENT_PREFIX_PATH`.

You can verify correct ordering at any time:

```bash
which -a ros2
# Expected:
#   /opt/ros/humble/fish/bin/ros2      ← FISH wrapper (must be first)
#   /opt/ros/humble/bin/ros2           ← real ros2
```

If the wrapper is not first, re-export the FISH paths or source
`.bashrc` again.

### Simulator vs rosbag mode

FISH supports two data source modes, configured in `fish_settings.ini`
under the `[replay]` section:

| Setting | `replay_rosbag = true` | `replay_rosbag = false` |
|---------|------------------------|-------------------------|
| Use case | Offline replay (rosbag) | Live simulator (e.g. AWSIM) |
| Data source | `command` field in settings | External simulator process |
| Auto-stop | After rosbag finishes | Manual (`ros2 run fish stop`) |
| Daemon Phase 4 | Runs rosbag command | Skipped |

For AWSIM or other live simulators, set `replay_rosbag = false` so the
daemon does not attempt to start a rosbag replay after system
stabilization.

### Non-ROS GPU processes

FISH scans `/proc` for CUDA-using processes and attempts to profile
them with nsys. Processes that are not ROS nodes (e.g. AWSIM, Gazebo,
Unity applications) should not be killed and relaunched. These are
excluded via the `SKIP_PATTERNS` list in `gpu.py`. Currently skipped:

- `nsys`, `lttng`, `tmux`, `tmuxinator` (infrastructure)
- `fish.cli`, `fish/python`, `fish_daemon` (FISH itself)
- `AWSIM` (external simulator)

If your setup includes other non-ROS GPU processes, add their binary
name or a unique substring of their command line to `SKIP_PATTERNS`.

## Notes

- `FISH_ENABLED=0` only disables automatic trace session management. The
  overlay workspace remains active (sourced from `.bashrc`). LTTng-UST
  tracepoints have zero overhead when no trace session is running.
- All install scripts are idempotent — safe to run multiple times.
- Custom tracepoint patches use markers (`# FISH_TRACEPOINTS_OVERLAY`,
  `# FISH_ENABLED`) in `.bashrc` so they can be cleanly updated or removed
  without affecting other entries.
