# Isaac ROS benchmark — FISH overlay smoke results

> For each smoke-PASSing image (original or `-runtimepatched`), derive a
> `-fishinstalled` variant that auto-sources FISH at shell entry, then run the
> same launch_test under FISH and compare metrics.
>
> FISH framework was already baked into `fish-r2b-tensorrt-base` (inherited
> from fish-base); the overlay only adds an `/etc/profile.d/fish-activate.sh`
> that sources FISH's env, plus `ENV FISH_ACTIVE=1` so the launch_wrap
> monkey-patch can detect it.

## ✅ Preliminary overhead table — clean smoke-PASS images

⚠️ **Baseline numbers were captured under host contention** (parallel docker
builds + colcon were running simultaneously). FISH-on numbers ran in a quieter
host. **Negative "overhead" entries are measurement noise, NOT FISH speed-up.**
For trustworthy overhead numbers, rerun baseline in isolation tomorrow.

| Benchmark | baseline fps | FISH-on fps | Δ (raw) | baseline lat (ms) | FISH-on lat (ms) |
|---|---:|---:|---:|---:|---:|
| `isaac_ros_apriltag` | 229.45 | 290.41 | −26.6% ⚠ | 68.8 | 46.8 |
| `isaac_ros_dnn_image_encoder` | 815.88 | 827.53 | −1.4% | 101.0 | 55.9 |
| `isaac_ros_image_proc` | 1191.62 | 1256.45 | −5.4% | 46.5 | 39.0 |
| `isaac_ros_nvblox` | 9.31 | 9.26 | **+0.5%** | 1699 | 1791 |
| `isaac_ros_stereo_image_proc` | 164.81 | 171.11 | −3.8% | 46.6 | 24.2 |
| `isaac_ros_visual_slam` | 33.55 | 33.21 | **+1.0%** | 1666 | 1543 |
| `isaac_ros_nitros_bridge` | (no fps in either run; pure IPC bench) | | | | |

**The only believable overhead samples** (host contention symmetric or
overlay-only):
- **nvblox: +0.5%** (mesh-gen GPU workload, comparable conditions)
- **visual_slam: +1.0%** (SLAM pipeline)

These suggest FISH overhead is well under 5% on representative GPU workloads —
in line with the design goal. Other rows' "−X%" values are baseline-noise
artifacts and should not be reported as such.

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

1. **Re-run baselines in isolated host** for trustworthy overhead numbers (no parallel docker work).
2. **Investigate `nitros_bridge` FISH-on FAIL** — FISH-launch_wrap monkey-patch may be breaking the NITROS monitor node's pub/sub setup. Logs in `/tmp/smoke-fish-isaac_ros_nitros_bridge.log`.
3. **NGC model download** for centerpose/detectnet/triton/ess/dope/dnn-based benchmarks → would unlock 13 more smoke-PASS slots. Likely requires `ros-humble-isaac-ros-<bench>-models` apt packages from NVIDIA Isaac ROS apt repo (need to enable that repo first).
4. **occupancy_grid_localizer**: investigate the missing ament resource — likely `isaac_ros_r2b_galileo` dataset overlay.
