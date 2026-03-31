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

## Notes

- `FISH_ENABLED=0` only disables automatic trace session management. The
  overlay workspace remains active (sourced from `.bashrc`). LTTng-UST
  tracepoints have zero overhead when no trace session is running.
- All install scripts are idempotent — safe to run multiple times.
- Custom tracepoint patches use markers (`# FISH_TRACEPOINTS_OVERLAY`,
  `# FISH_ENABLED`) in `.bashrc` so they can be cleanly updated or removed
  without affecting other entries.
