# Shared Learnings — Isaac ROS 3.2 + VPI 4 + CMake 4

> The manager appends cross-cutting findings here between waves. Workers in
> later waves get this file pre-loaded into context so they can shortcut
> issues that earlier agents already solved.

## Pre-flight context (baked into fish-r2b-tensorrt-base)

`build_benchmark_image.sh` now FROMs `fish-r2b-tensorrt-base:latest`, an image
that extends `fish-r2b-base` with TensorRT pre-installed. This avoids the
~5 GB / ~10 min TensorRT download per DNN benchmark. Patches like
`PATCH_EXTRA_APT="tensorrt-dev"` are now redundant — leave them in patch
files; they're idempotent (apt sees TensorRT already installed, no-op).

The common Isaac ROS workspace is already built into `fish-r2b-base:latest`
(layered under tensorrt-base).
Packages built and ready for downstream use:
- `ros2_benchmark`, `apriltag_ros`, `image_proc`, `image_geometry`
- `isaac_ros_common`, `isaac_ros_gxf`, `isaac_ros_apriltag`,
  `isaac_ros_benchmark`, `isaac_ros_image_proc`
- All `gxf_isaac_*` extensions, all `isaac_ros_nitros_*` types
- `negotiated`, `negotiated_interfaces`

Already installed in the image:
- CMake 3.27.9 (in `/usr/local/bin/cmake`)
- magic_enum (system-wide, including subdir-less symlinks)
- CV-CUDA 0.10.1-beta (lib + dev, cuda12-x86_64)
- VPI 4 (apt — fish-base inherited)
- All explicit apt deps (ros-humble-xacro, vision-msgs, foxglove-msgs, etc.)

## Known patterns from base build

These patterns were necessary to make Isaac ROS 3.2 release-3.2 work with
VPI 4 + CMake 4 + CUDA 12.6. Per-benchmark workloads may exhibit similar
patterns:

### VPI 4 API changes
- `VPIImagePlanePitchLinear::data` → `VPIImagePlanePitchLinear::pBase`
- `pBase` is `VPIByte*` (unsigned char*) — assignments need
  `reinterpret_cast<VPIByte *>(expr)`. Multi-line assignments (sed -z).
- `VPI_BACKEND_NVENC` removed (Jetson-only). Strip references.

### CMake 4 issues
- `$<INSTALL_PREFIX>` outside install(EXPORT) is a hard error → use cmake 3.27
- CMake 4 removed FindBoost — covered by being on 3.27

### Missing exports from NVIDIA pkgs
- `isaac_ros_gxf` references `magic_enum::magic_enum` in INTERFACE_LINK_LIBRARIES
  but never runs `find_package(magic_enum)` or `ament_export_dependencies`.
  Downstream targets fail at set_target_properties.

### Missing build deps
- `negotiated` (osrf) — not in any apt repo or rosdistro
- `magic_enum` — not in apt repos enabled in base
- `nvcv/Tensor.hpp` — needs CV-CUDA install (GitHub releases)

## Success criteria (per user)

**A benchmark "works" if launch_test completes the benchmark protocol without
a crash and produces metric output.** Performance numbers (fps, latency) are
NOT the goal — host is on PREEMPT kernel (not PREEMPT_RT), background load
is uncontrolled (parallel agents during this sweep), and we lack the
NVIDIA-spec'd thermal/clock-locked test rig. We just need a workload FISH can
trace + profile. Speed comparisons against NVIDIA's official numbers belong
in a separate measurement campaign with a cleaner host env.

So:
- `passed` (build + run) = ✅
- `build_only` (image built, smoke deferred or SKIP_RUN) = ✅
- `run_failed` where the run produced metric output before crashing during
  shutdown = ✅ (e.g. apriltag's exit -11 segfault AFTER benchmark numbers)
- `build_failed` or run crash before any metric output = ❌

## Per-wave findings

### Wave 2 cross-cutting issues — 2026-05-21

**1. TensorRT 10.x version detection breaks FindTENSORRT.cmake**

Isaac ROS 3.2's `FindTENSORRT.cmake` (installed in fish-r2b-base via
isaac_ros_common) parses version with `string(REGEX MATCH "NV_TENSORRT_MAJOR ([0-9]+)" ...)`.
TensorRT 10.x header redirects through enterprise macros:
```c
#define TRT_MAJOR_ENTERPRISE 10
#define NV_TENSORRT_MAJOR TRT_MAJOR_ENTERPRISE
```
The regex sees `NV_TENSORRT_MAJOR TRT_MAJOR_ENTERPRISE` (not a digit) → version=".."
→ `Could NOT find TENSORRT: required ">= 10"`.

**Fix (in patch_pre_build for any DNN benchmark):**
```bash
FT=/root/ros_ws/install/isaac_ros_common/share/isaac_ros_common/cmake/modules/FindTENSORRT.cmake
[ -f "$FT" ] && sed -i '
  s/read_version(NV_TENSORRT_MAJOR/read_version(TRT_MAJOR_ENTERPRISE/;
  s/read_version(NV_TENSORRT_MINOR/read_version(TRT_MINOR_ENTERPRISE/;
  s/read_version(NV_TENSORRT_PATCH/read_version(TRT_PATCH_ENTERPRISE/;
  s/\${NV_TENSORRT_MAJOR}/${TRT_MAJOR_ENTERPRISE}/;
  s/\${NV_TENSORRT_MINOR}/${TRT_MINOR_ENTERPRISE}/;
  s/\${NV_TENSORRT_PATCH}/${TRT_PATCH_ENTERPRISE}/
' "$FT"
```

Affects: `rtdetr`, likely `detectnet`, `dnn_image_encoder`, `centerpose`, `dope`,
`segformer`, `unet`, `triton`, `tensor_rt`, `foundationpose`, `segment_anything`.

**2. `ISAAC_ROS_WS is not set` (ess, possibly others with `*_models_install` pkgs)**

Some Isaac ROS pkgs (`isaac_ros_ess_models_install`) have a CMake step that
calls `install_isaac_ros_asset` which downloads model weights at build time.
That step requires the env var `ISAAC_ROS_WS` and a network connection.

For build-only sweep this is unnecessary — model weights are a runtime
concern. Workaround: COLCON_IGNORE the *_models_install packages.

**Fix (in patch_pre_build):**
```bash
find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \;
```



## Wave 1 findings — 2026-05-21

### Universal: r2b_dataset bind-mount — RESOLVED
- `build_benchmark_image.sh` now bind-mounts both
  `/home/tue037807/r2bdataset2023_v3` and `/home/tue037807/r2bdataset2024_v1`
  into the smoke-test container and symlinks each sub-bag into
  `/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset/`.
- **Workers should NOT use SKIP_RUN=1 anymore.** Run the full build+smoke flow:
  ```
  ./scripts/build_benchmark_image.sh <BENCH>
  ```
  Pass status indicates both build OK and launch_test ran without crashing.
- A benchmark that needs a bag not in either dataset will surface as a
  `run_failed` rather than silent skip — the worker should diagnose and
  either patch the launch script via `patch_pre_build()` or accept the
  failure with a journal note.

### Base image is sufficient for canary benchmarks with zero patches
- `isaac_ros_apriltag` built cleanly with no patch file in 332s. The base
  image's pre-built apriltag/image_pipeline/isaac_ros_common stack covers it.
- Expectation: `isaac_ros_image_proc` and `isaac_ros_stereo_image_proc` may
  cache-hit entirely because their workload_repos list is empty in inventory.

