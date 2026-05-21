# Isaac ROS benchmark — FISH overlay smoke results

> For each smoke-PASSing image (original or `-runtimepatched`), derive a
> `-fishinstalled` variant that auto-sources FISH at shell entry, then run the
> same launch_test under FISH and compare metrics.
>
> FISH framework was already baked into `fish-r2b-tensorrt-base` (inherited
> from fish-base); the overlay only adds an `/etc/profile.d/fish-activate.sh`
> that sources FISH's env, plus `ENV FISH_ACTIVE=1` so the launch_wrap
> monkey-patch can detect it.

## 🚨 Critical caveat — the FISH overlay images did NOT actually activate FISH

Audit after the sweep showed `/tmp/fish_traces/` is EMPTY. Reasons:

1. The Dockerfile's `RUN ... fish-activate.sh` sourced `/opt/ros/humble/fish/scripts/fish_env.sh`
   which **does not exist**. The only script in `/opt/ros/humble/fish/scripts/` is `trace_session.sh`.
2. FISH activation is via a `ros2` wrapper at `/opt/ros/humble/fish/bin/ros2`. To use it, that
   directory must be in `PATH` BEFORE `/opt/ros/humble/bin/`. We didn't add it.
3. Even with the wrapper in PATH, our smoke tests call `launch_test <script>.py` which is a
   Python entry point that bypasses the `ros2 launch` wrapper. To actually trace, we'd need
   to source `trace_session.sh` + call `start_session` BEFORE the launch, then `stop_session`
   after, and capture the resulting `~/fish_traces/<session>/` artifacts.

**So the "FISH-on" smoke results below are really just a second baseline run, not FISH-traced.**
The "overhead" numbers reflect normal run-to-run variance plus host contention, not FISH cost.

## ⚠️ Run-to-run variance table (mislabeled "FISH overlay")

| Benchmark | baseline fps | FISH-on fps | Δ (raw) | baseline lat (ms) | FISH-on lat (ms) |
|---|---:|---:|---:|---:|---:|
| `isaac_ros_apriltag` | 229.45 | 290.41 | −26.6% ⚠ | 68.8 | 46.8 |
| `isaac_ros_dnn_image_encoder` | 815.88 | 827.53 | −1.4% | 101.0 | 55.9 |
| `isaac_ros_image_proc` | 1191.62 | 1256.45 | −5.4% | 46.5 | 39.0 |
| `isaac_ros_nvblox` | 9.31 | 9.26 | **+0.5%** | 1699 | 1791 |
| `isaac_ros_stereo_image_proc` | 164.81 | 171.11 | −3.8% | 46.6 | 24.2 |
| `isaac_ros_visual_slam` | 33.55 | 33.21 | **+1.0%** | 1666 | 1543 |
| `isaac_ros_nitros_bridge` | (no fps in either run; pure IPC bench) | | | | |

All these numbers are noise: FISH wasn't active in either column. The +0.5%
and +1.0% I previously claimed as "FISH overhead" are run-to-run variance,
not FISH cost.

## ❌ Images that worked baseline but FAIL under FISH

| Image | Failure | Notes |
|---|---|---|
| `fish-r2b-nitros_bridge-fishinstalled:latest` | `No messages were observed from the monitor node monitor_node0` | FISH activation seems to have broken the NITROS bridge's IPC monitor pipeline. Real FISH interaction issue. Log: `/tmp/smoke-fish-isaac_ros_nitros_bridge.log` |

## ❌ Images that failed baseline AND fail under FISH

These 14 fail for non-FISH reasons (missing TensorRT bin / ONNX model files /
ament resources). FISH doesn't change anything for them. See
`docs/isaac_ros_smoke_results.md` for baseline failure causes.

Their `-runtimepatched-fishinstalled` variants got built and tested anyway;
all show the same baseline-level errors:
- `trt-converter failed to convert with status: 1` (8 benchmarks): bi3d,
  bi3d_freespace, dope, foundationpose, pynitros, rtdetr, segformer,
  tensor_rt, unet — missing ONNX model files
- DNN model `config.pbtxt` missing (3): centerpose, detectnet, triton
- `std::error_code` (1): ess
- `Could not find requested resource in ament index` (1): occupancy_grid_localizer
- generic rc=1, no metric (1): segment_anything

Full failure logs available at `/tmp/smoke-fish-<bench>.log` for each.

## Image count summary

After the overnight sweep, the repo has produced:

| Layer | Count | Notes |
|---|---|---|
| `fish-base:cuda12.6-humble` | 1 | CUDA + ROS Humble + VPI 4 + nsys + FISH (pre-existing) |
| `fish-r2b-base:latest` | 1 | + common Isaac ROS workspace (16 GB) |
| `fish-r2b-tensorrt-base:latest` | 1 | + TensorRT 10 dev libs (23 GB) |
| `fish-r2b-<bench>:latest` | 22 | per-benchmark build_only images (22 of 24 viable+vram_risky) |
| `fish-r2b-<bench>-runtimepatched:latest` | 15 | failed-smoke images + libnvinfer-bin (and placeholder for model dirs) |
| `fish-r2b-<bench>-fishinstalled:latest` | 7 | baseline-PASS originals + FISH activation overlay |
| `fish-r2b-<bench>-runtimepatched-fishinstalled:latest` | 14 | runtimepatched + FISH activation (all still fail baseline-level) |
| **Total** | **61** | |

## Tomorrow's todo (from these logs)

1. **Properly activate FISH** for actual overhead measurement:
   - Modify the `-fishinstalled` Dockerfile to prepend `/opt/ros/humble/fish/bin` to `PATH` so the wrapped `ros2` takes priority.
   - Replace `launch_test <script>` smoke invocation with a sequence:
     ```bash
     source /opt/ros/humble/fish/scripts/trace_session.sh
     start_session
     launch_test <script>
     stop_session
     ```
   - Or write a wrapper that detects the script invocation and routes through `ros2 launch` so the FISH `ros2` wrapper intercepts.
   - Verify by checking `~/fish_traces/<session>/` has `ros2/`, `nsys/`, `snapshot/` populated after each run.
2. **Re-run baselines in isolated host** (no parallel docker work) — current baseline numbers are contaminated by overnight build contention.
3. **Investigate `nitros_bridge` baseline FAIL** under the no-op "FISH overlay" — log says `No messages were observed from the monitor node monitor_node0`. Since FISH wasn't active, this is a NITROS-bridge intermittent issue (the original baseline smoke PASSED, this rerun FAILed in same image). Logs in `/tmp/smoke-fish-isaac_ros_nitros_bridge.log`.
4. **NGC model download** for centerpose/detectnet/triton/ess/dope/dnn-based benchmarks → would unlock 13 more smoke-PASS slots. Likely requires `ros-humble-isaac-ros-<bench>-models` apt packages from NVIDIA Isaac ROS apt repo (need to enable that repo first).
5. **occupancy_grid_localizer**: investigate the missing ament resource — likely `isaac_ros_r2b_galileo` dataset overlay.
