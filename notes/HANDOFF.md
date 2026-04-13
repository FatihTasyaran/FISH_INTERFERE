# FISH Handoff Document

**Last updated:** 2026-04-12 (session ended ~05:30)
**Last commit:** `a5cc45f` on `main` — pushed to `github.com:FatihTasyaran/FISH_INTERFERE.git`
**Working container:** `crazy_hermann` (running, image `autoware-dev-trt-a1000-fish:latest`)

---

## 1. Project Overview

### What FISH Is

FISH (Function Level Inspection of Scheduling Hierarchies) is a profiling framework for ROS 2 systems with GPU workloads. It combines:

- **LTTng-UST tracing** for callback execution timing (callback_start/end, publish, take)
- **NVIDIA Nsight Systems (nsys)** for GPU kernel profiling (CUDA API calls, kernel launches, memory operations)
- **System state snapshots** for process trees, node topology, topic rates

The goal: build a hierarchical model of a ROS 2 application — from executors (L0) down to individual function calls (L3) — with GPU characterization per callback.

### Why It Exists

Fatih's PhD thesis at TU/e. FISH enables:
- Per-callback roofline model (CPU via PMU, GPU via nsys)
- Scheduling analysis for real-time ROS 2 systems
- Cross-boundary communication mapping between containers

### Architecture Decisions

1. **Overlay workspace for tracepoints**: Custom LTTng tracepoints are built in a separate colcon workspace (`trace_overlay_ws`) that overlays the base ROS 2 install. This avoids modifying `/opt/ros/humble` directly. The overlay's `local_setup.bash` (not `setup.bash`) is sourced so it doesn't clobber application workspace chains (e.g., Autoware).

2. **Wrapper-based interception**: FISH installs a `ros2` wrapper at `/opt/ros/humble/fish/bin/ros2` that intercepts `ros2 launch` and `ros2 run`. When `FISH_ENABLED=1`, the wrapper automatically starts LTTng tracing, the GPU daemon, and (for launch) the composable node nsys wrapping.

3. **Two GPU profiling strategies**:
   - **Standalone GPU nodes**: Detected at runtime via `/proc/<pid>/maps` CUDA check. Killed and relaunched under nsys by the daemon (kill+relaunch). Works because standalone nodes have all their args in the cmdline.
   - **Composable node containers**: Wrapped with nsys at launch time via `launch_wrap.py` monkey-patching `ComposableNodeContainer.execute()`. This is TRANSPARENT to the launch system — components load normally into the nsys-wrapped container. **Kill+reload was tried for 8 iterations and abandoned** (see Section 3).

4. **Signal-based orchestration**: The daemon uses a phased pipeline with signal files for inter-process coordination: stabilization → GPU handling → live collectors → replay → auto-stop. Each phase logs monotonic timestamps to fishlog for post-processing correlation.

---

## 2. Current State

### Fully Implemented and Working

- **11 custom LTTng tracepoints**: 4 action server, 5 rclpy callback chain, 2 service client
- **Tracepoint installer** (`install_fish_tracepoints`): Unified script with `--action/--rclpy/--client/--all` flags
- **GPU daemon** (`gpu.py`): Process detection, batch scanning, dual stabilization, standalone kill+relaunch, container skip
- **launch_wrap.py**: Container nsys prefix injection via ComposableNodeContainer.execute() monkey-patch
- **launch_inspect.py**: Static launch analysis, GPU container detection via keyword heuristics, `__gpu_containers__` output
- **Orchestration pipeline**: Stabilization → GPU handler → live collectors → replay → auto-stop (all configurable via `fish_settings.ini`)
- **Snapshot module** (`snapshot.py`): Phase 1 live collection (node info, topic info, topic hz) + Phase 2 shutdown snapshot (ps_tree, component_list)
- **Ingest pipeline** (`postprocess/ingest.py`): Parallel ros2_trace (MongoDB) + nsys (InfluxDB) ingestion
- **Model extraction** (`postprocess/model.py`): Entity/aspect model, runtime aspect attribution, external boundary entities, horizontal relation edges
- **D3 interactive visualization** (`postprocess/fish_viz.html`): Force/tree-stair/radial layouts, node/edge click interaction
- **Autoware integration**: Docker image `autoware-dev-trt-a1000-fish:latest` with FISH + map + rosbag pre-installed
- **Automated test workflow**: `ros2 launch` → stabilization → GPU wrap → replay → auto-stop → nsys reports

### Verified Result

Session `fish_20260412_032638` (in container `crazy_hermann`):
- **14,538 CUDA kernel events** from `/pointcloud_container`
- **48,909 CUDA runtime calls**
- **3,587 memcpy operations**
- Pipeline fully intact during profiling (pointcloud visible in RViz, vehicle moving)

### Partially Implemented

1. **Standalone GPU node nsys reports are empty**: `shape_estimation_node` and `detection_by_tracker_node` are killed and relaunched under nsys by the daemon, but their nsys reports contain 0 CUDA events. Investigation needed — likely they don't receive input data during the replay window, or nsys injection fails for some reason on relaunched processes. The kill+relaunch mechanism itself works.

2. **Traffic light container nsys wrapping**: `camera6/camera7 traffic_light_node_container` get wrapped and start loading TensorRT engines, but the ONNX→TRT compile takes ~60-120s. If replay is short (0.6x, ~50s), the compilation isn't done before session ends. On subsequent runs with cached engines, this should work. **Needs verification with cached TRT engines.**

3. **launch_inspect namespace resolution**: Static analysis can't resolve `PushRosNamespace` context from enclosing `GroupAction`. Workaround: fuzzy short-name matching at reload time. This affects the `__gpu_containers__` list accuracy — some containers might be under a different key than their runtime name.

### Explicitly Decided NOT To Do

1. **Kill+reload for containers**: Abandoned after 8 iterations. The ROS 2 launch system does NOT retry `LoadComposableNodes` after container death. Manual reload via `ros2 component load` is brittle: wrong CLI flags, missing YAML expansion, TRT compile timeouts, shell escaping, race conditions with launch system. See `gpu_profiling_journey.txt` for the full history.

2. **nsys attach to running processes**: nsys doesn't support CUDA API tracing via attach — it requires LD_PRELOAD injection at process start time. So we must either start under nsys or kill+relaunch.

3. **Full nsys flags for containers**: `--stats=true`, `--sample=process-tree`, `--cudabacktrace` cause nsys to SIGSEGV (exit 139) when wrapping multi-threaded `component_container_mt`. Minimal flags only: `--trace=cuda,nvtx --export=sqlite --force-overwrite=true`.

---

## 3. Critical Implementation Details

### Non-Obvious Design Choices

1. **Execute-time patching, not init-time**: `launch_wrap.py` patches `ComposableNodeContainer.execute(context)` because `LaunchConfiguration` substitutions can only be resolved when a `LaunchContext` is available. At `__init__` time, `name=LaunchConfiguration("container_name")` is just an opaque object.

2. **Executable.__prefix private attribute path**: The prefix that ExecuteProcess prepends to the command lives at `self._ExecuteLocal__process_description._Executable__prefix` (Python name mangling through 3 levels of inheritance: ExecuteProcess → ExecuteLocal stores `__process_description` → Executable stores `__prefix`). Getting this wrong means nsys is never prepended.

3. **Foreign nsys signaling**: launch_wrap'd nsys processes are children of the launch system process, NOT of the FISH daemon. At auto-stop, the daemon must scan `/proc` for ALL nsys processes whose `--output=` path contains the session directory, then SIGINT them. Without this, nsys processes get SIGTERM from launch teardown and die without writing reports.

4. **Dual stabilization**: The daemon polls BOTH `ros2 component list` (are components loaded?) AND "process started with pid" count from launch stdout (are all processes spawned?). Both must be stable for N consecutive polls before GPU handling begins. This prevents killing containers before components load.

5. **Overlay uses local_setup.bash**: The FISH tracepoint overlay uses `local_setup.bash` (not `setup.bash`) in `.bashrc` so it PREPENDS to the existing `AMENT_PREFIX_PATH` without resetting the workspace chain. Without this, Autoware's packages become invisible.

### Gotchas and Fixed Bugs

| Bug | Symptom | Root Cause | Fix |
|-----|---------|------------|-----|
| `ros2 component list <name>` parser returned 0 | Live components always empty | Output format has NO header when called with specific name (unlike no-args version) | Detect both formats |
| nsys SIGSEGV exit 139 | Containers die immediately after wrapping | `--stats=true` + `--sample=process-tree` + multi-threaded container | Use minimal nsys flags for containers |
| nsys filename collision | Multiple nsys instances SIGSEGV | Same timestamp + same container name hint → same output path | Monotonic counter suffix (`_001`, `_002`) |
| `'LaunchConfiguration' object is not iterable` | launch_wrap crash at execute time | `_Node__node_name` is sometimes a single Substitution, not a list | `_resolve_one()` handles str, single sub, and lists |
| `--params-file` not found | `ros2 component load` fails | Flag doesn't exist in Humble. Only `--parameter name:=value` individual params | YAML expansion + corrected flags |
| Container killed at t=6s, components load at t=30s | Empty container after nsys relaunch | No stabilization phase | Dual stabilization with configurable intervals |
| `install_fish.sh` overwrites updated `gpu.py` | Code changes lost on reinstall | Script copies from `$SCRIPT_DIR/python/fish/` which has old files | Always copy `fish_interfere/` recursively, then run install |
| `docker commit` preserves `sleep infinity` as CMD | Container won't give bash prompt | Committed from a container started with `sleep infinity` entrypoint | `docker build` with `CMD ["bash"]` Dockerfile |

### Workarounds

1. **`_resolve_one()` tolerance**: Instead of using launch's `perform_substitutions()` (which requires a proper substitution list), `_resolve_one()` handles mixed types: None, plain str, single Substitution, or list of mixed str+Substitution. This is fragile but necessary because launch_ros stores attributes inconsistently.

2. **GPU keyword heuristic**: `launch_inspect.py` uses a keyword list (`cuda`, `tensorrt`, `centerpoint`, `bevfusion`, `yolox`, etc.) to identify GPU containers. This is necessarily incomplete — a new GPU plugin with an unusual name won't be detected. The fallback is to wrap ALL containers (when `__gpu_containers__` list is empty).

3. **Short-name fuzzy matching**: The static inspector can't resolve full runtime namespaces (from `PushRosNamespace` in `GroupAction`). So container lookup and component matching use the last path segment only. This works because container names are unique within Autoware's architecture.

---

## 4. File Map

### Core Python Package (`python/fish/`)

| File | Lines | Purpose |
|------|-------|---------|
| `launch_wrap.py` | 437 | **Container nsys wrapping** — monkey-patches ComposableNodeContainer.execute() to inject nsys prefix |
| `launch_inspect.py` | 569 | **Static launch analysis** — extracts composable node specs, detects GPU containers |
| `gpu.py` | 1703 | **GPU daemon** — process detection, stabilization, kill/relaunch, orchestration, replay |
| `snapshot.py` | 564 | **System state capture** — live collection (background) + shutdown snapshot |
| `settings.py` | 67 | **Configuration** — reads fish_settings.ini with defaults |
| `cli.py` | ~50 | **CLI dispatcher** — routes `ros2 run fish <cmd>` to appropriate module |
| `__init__.py` | 0 | Package marker |

### Shell Scripts (top level)

| File | Purpose |
|------|---------|
| `install_fish.sh` | Generates ros2 wrapper + trace_session.sh, copies Python files to `/opt/ros/humble/fish/` |
| `setup_fish.sh` | One-shot installer: deps → tracepoints → framework |
| `install_fish_deps.sh` | Dependency checker (lttng, nsys, tracetools, ros2trace) |
| `fish_settings.ini` | All configurable parameters (daemon intervals, nsys flags, replay command) |

### Tracepoint Installer (`fish_tracepoints/`)

| File | Purpose |
|------|---------|
| `install.sh` | C++ tracepoint patches (tracetools, rclcpp_action server, rclcpp client) |
| `install_rclpy.sh` | Python tracepoint patches (rclpy subscription, service, timer, executor, client, action client) |

### Post-Processing (`postprocess/`)

| File | Purpose |
|------|---------|
| `ingest.py` | Parallel ingestion: ros2_trace → MongoDB, nsys → InfluxDB |
| `model.py` | Entity/aspect model extraction, horizontal relations, export |
| `utils.py` | Visualization exports (DOT, radial, staircase, matrix, JSON) |
| `fish_viz.html` | D3 interactive visualization |

### Documentation (`*.txt`, `*.md`)

| File | Purpose |
|------|---------|
| `orchestration.txt` | Signal-based pipeline architecture |
| `component_handling.txt` | Container detection/reload decision flow |
| `gpu_profiling_journey.txt` | **READ THIS FIRST** — full narrative of container GPU profiling attempts |
| `immediate_work.txt` | Numbered work items with priorities (some FIXED) |
| `comm_dep_models.txt` | 3 communication patterns (pub/sub, service, action) with ASCII art |
| `design_decisions.txt` | DD-001 through DD-005 |
| `known_issues.txt` | ISSUE-001: rclpy timer inflation |

### Key Relationships

```
install_fish.sh
  ├── generates /opt/ros/humble/fish/bin/ros2 (wrapper)
  │     ├── on "ros2 launch": calls launch_inspect → launch_wrap → real ros2 launch
  │     └── on "ros2 run": calls trace_session.sh → real ros2 run
  ├── generates /opt/ros/humble/fish/scripts/trace_session.sh
  │     ├── start: LTTng session + daemon (gpu.py)
  │     └── stop: snapshot + daemon stop + LTTng destroy
  └── copies python/fish/*.py → /opt/ros/humble/fish/python/fish/

gpu.py daemon_loop()
  ├── Phase 1: _wait_for_stable() — reads /tmp/fish_launch_output.log
  ├── Phase 2: scan_ros2_processes_with_cuda_check()
  │     ├── container? → skip (launch_wrap handled it)
  │     └── standalone GPU? → kill + relaunch_with_nsys()
  ├── Phase 3: LiveCollector.start()
  ├── Phase 4: replay (from fish_settings.ini [replay] command)
  └── Phase 5: graceful_stop_nsys() — signals BOTH daemon + foreign nsys

launch_wrap.py
  ├── reads launch_components.json (from launch_inspect.py)
  ├── patches ComposableNodeContainer.execute()
  └── calls ros2cli.cli.main() for actual launch
```

---

## 5. Conventions & Patterns

### Naming

- **fishlog events**: `<monotonic_ns> <action> <killed_pid> <resurrected_pid> <process_name> <cmdline>` — fixed format for post-processing
- **Signal files**: `/tmp/fish_<signal_name>` — e.g., `fish_system_stable`, `fish_gpu_handler_complete`
- **nsys output**: `nsys_<container_safe_name>_<YYYYMMDD_HHMMSS>_<counter>.sqlite`
- **Session dirs**: `~/fish_traces/fish_<YYYYMMDD_HHMMSS>/` with subdirs `ros2/`, `nsys/`, `fishlog/`, `snapshot/`

### Code Style

- Turkish comments in txt docs, English in code
- `_private_function()` prefix for module-internal helpers
- `UPPER_CASE` for constants and process name sets
- Type hints on public functions, informal on private ones
- `print(f"[FISH] ...")` for user-visible daemon output
- `logger.log_daemon(f"event_name key=value")` for machine-parseable fishlog

### Patterns

- **Monkey-patching**: Both `launch_inspect.py` and `launch_wrap.py` use `__init__` or `execute()` hooks on launch_ros classes. Always install patches BEFORE importing the classes that will be patched.
- **Graceful degradation**: launch_inspect/launch_wrap failures are non-fatal — wrapper continues with reduced functionality (no GPU container wrapping, fallback to daemon-only).
- **fish_settings.ini driven**: All timing parameters, nsys flags, replay commands configurable. Code reads via `settings.getfloat("section", "key")`.
- **Docker workflow**: `fish_interfere/` is copied RECURSIVELY into container, then `install_fish.sh` is sourced. This guarantees latest code. Never rely on bind mounts for iterating (inode issues with editor safe-writes).

---

## 6. Next Steps

### Immediate (Where We Stopped)

The last successful run was session `fish_20260412_032638` in container `crazy_hermann`. The `launch_wrap` approach works: 14K+ CUDA kernel events captured from pointcloud_container with pipeline intact.

**Next task: Verify and commit the latest state.**

1. The docker image `autoware-dev-trt-a1000-fish:latest` should be re-committed from `crazy_hermann` to bake in the latest code.
2. Run a clean FISH vs no-FISH comparison now that launch_wrap works — verify pipeline behavior is identical.
3. Investigate why standalone GPU nodes (`shape_estimation`, `detection_by_tracker`) have empty nsys reports after kill+relaunch.

### Ordered Follow-Up

4. **FISH output colorization**: User requested colored terminal output for stabilization polls, signals, handler events. Not yet implemented.

5. **Traffic light TRT engine caching**: Mount `/root/autoware_data` as a persistent volume so TRT engines are cached between runs. First run compiles (~120s), subsequent runs load from cache (~1s).

6. **Ingest the Autoware trace**: Run `ingest.py` on session `fish_20260412_032638` to get data into MongoDB + InfluxDB. Then run `model.py` to extract the entity/aspect model and generate the D3 visualization.

7. **Per-callback roofline model** (item 9 in immediate_work.txt): Use nsys CUDA kernel data + LTTng callback boundaries to compute per-callback GPU utilization. Combine with PMU counters for CPU roofline.

8. **Multi-container trace composition** (item 10): Compose traces from multiple containers into unified model. External boundary entities already implemented — need cross-container edge matching.

9. **Service client continuation function** (item 8): Split callback model for service clients (request phase → continuation phase).

10. **Jazzy port**: Port to ROS 2 Jazzy for newer Autoware versions.

---

## 7. Open Questions / Known Issues

### Unresolved Questions

1. **Why are standalone GPU nsys reports empty?** The daemon successfully kills `shape_estimation_node` (PID X) and relaunches it under nsys (PID Y). The relaunched process runs for the entire replay duration. But the nsys report has 0 CUDA events. Possibilities:
   - Upstream data doesn't flow to the relaunched node (DDS discovery delay?)
   - nsys injection fails when the process wasn't started from scratch (env mismatch?)
   - The node doesn't actually use CUDA during the sample rosbag's replay window
   
2. **launch_wrap prefix: is Executable.__prefix really the right hook?** The prefix injection works (we see `nsys-1: process started with pid [431]` in launch output). But we haven't verified that nsys is actually INJECTING its CUDA hooks into the child process. It's possible that ExecuteProcess handles the prefix differently than we think (e.g., shell prefix vs exec prefix).

3. **Should containers use the same nsys flags as standalone?** Currently containers use minimal flags (`--trace=cuda,nvtx --export=sqlite --force-overwrite=true`) because the full flags crash nsys on multi-threaded containers. Is there a subset of the full flags that works? Or is this an nsys bug that might be fixed in newer versions?

### Known Limitations

1. **OOM risk**: Autoware + FISH + 5 nsys processes + LTTng → ~28GB on a 30GB system. The system crashed once during development. Mitigations: reduce LTTng buffer sizes, limit nsys trace buffers, or use a machine with more RAM.

2. **GPU keyword heuristic is incomplete**: `launch_inspect.py` uses a hardcoded keyword list for GPU detection. New GPU plugins not matching any keyword won't be wrapped. Fallback: set `__gpu_containers__` to empty → all containers get wrapped (safe but wasteful).

3. **Namespace resolution**: Static launch analysis can't resolve `PushRosNamespace` context. Container names in `launch_components.json` may differ from runtime names. The fuzzy short-name matching workaround handles most cases but isn't guaranteed.

4. **LTTng event discards**: Some runs show `Warning: N events were discarded`. Need to increase `--subbuf-size-ust` parameter in trace_session.sh.

5. **Traffic light TRT compile**: First run after fresh container always compiles TRT engines from ONNX (~60-120s per model). Session should be long enough for this, or engines should be pre-cached in the image.

6. **`ros2 component load` reload code is preserved but not used**: The kill+reload code path (`_handle_container_gpu_process`, `_reload_from_matched_pairs`, etc.) is still in `gpu.py` but dead code. The daemon now skips all containers. This code could be removed or kept as fallback.

---

## Appendix: Docker Images

| Image | Size | Contents |
|-------|------|----------|
| `autoware-dev-trt-a1000:latest` | 28.8GB | Base Autoware + TensorRT (no FISH) |
| `autoware-dev-trt-a1000-fish:latest` | 31.1GB | Above + FISH + nsys + map + rosbag + .bashrc configured |

**To start a FISH test:**
```bash
docker run --privileged -it --runtime=nvidia --gpus all --device /dev/dri --device /dev/kfd -e DISPLAY=$DISPLAY -e XAUTHORITY=/root/.Xauthority -v $HOME/.Xauthority:/root/.Xauthority:rw -v /tmp/.X11-unix:/tmp/.X11-unix -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all --cap-add=SYS_ADMIN --security-opt seccomp=unconfined -v /etc/vulkan/icd.d:/etc/vulkan/icd.d:rw -v /etc/glvnd/egl-vendor.d:/etc/glvnd/egl-vendor.d:rw -v /etc/OpenCL/vendors:/etc/OpenCL/vendors:rw -v $HOME/fish_traces:/root/fish_traces --net host autoware-dev-trt-a1000-fish:latest
```

Inside:
```bash
ros2 launch autoware_launch logging_simulator.launch.xml map_path:=$HOME/autoware_map/sample-map-rosbag vehicle_model:=sample_vehicle sensor_model:=sample_sensor_kit
```

FISH handles everything automatically: trace → stabilization → nsys wrapping → replay → auto-stop.

---

## Appendix: User (Fatih) Preferences

- Speaks Turkish in conversation, code comments in English
- Wants concrete, actionable next steps — not vague summaries
- Dislikes unnecessary caution/fallbacks: "hayir bizde fallback yok kardes"
- Prefers understanding the problem deeply before implementing: asks "why" before "how"
- Values general solutions over application-specific hacks
- Working hours often extend past midnight
- Uses nvtop and RViz to visually verify GPU activity and pipeline integrity
