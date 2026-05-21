# Agent Journal — `isaac_ros_bi3d`

> One file per benchmark, owned exclusively by the agent assigned to it.
> Append-only. Each iteration gets its own H2 section.
> After the sweep, journals are mined to produce `docs/isaac_ros_known_issues.md`.

## Identity
- **Benchmark:** `isaac_ros_bi3d`
- **Image tag goal:** `fish-r2b-bi3d:latest`
- **Workload repos (from inventory):** ['isaac_ros_depth_segmentation']
- **Smoke-test script:** `isaac_ros_bi3d_node`

## Iteration 1 — 2026-05-21
**Hypothesis going in:** Stale `build_failed` was pre-fish-r2b-base (cv_bridge/Boost).
Expected a clean rebuild against fish-r2b-base to succeed, since `isaac_ros_depth_segmentation`
is small and the workspace prereqs are pre-built.

**Action:**
- ran `SKIP_RUN=1 ./scripts/build_benchmark_image.sh isaac_ros_bi3d`
- patch file state: did not exist

**Observed:**
- BUILD: failed at colcon `gxf_isaac_sgm` (Image Pipeline transitive dep)
- 1 package failed, 3 aborted (`gxf_isaac_bi3d`, `gxf_isaac_utils`, `isaac_ros_nitros`)
- key log lines:
  ```
  /root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm/
    gxf/extensions/sgm/sgm_disparity.cpp:172:35:
    error: ‘VPI_BACKEND_XAVIER’ is not a member of ‘nvidia::isaac::vpi’;
           did you mean ‘VPI_BACKEND_ORIN’?
  /root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm/
    gxf/gems/vpi/constants.cpp:33:18: error: ‘VPI_BACKEND_XAVIER’ was not declared
  ```

**Diagnosis:** Same as the existing finding from `isaac_ros_stereo_image_proc`:
VPI 4 dropped `VPI_BACKEND_XAVIER`. The bi3d colcon target graph reaches
`gxf_isaac_sgm`, which isn't pre-built in fish-r2b-base (only the higher-level
`isaac_ros_image_proc` is). The follow-on `unordered_map(<init-list>)` error in
constants.cpp is just the cascading template substitution failure caused by
the missing enum identifier.

**Fix applied:** Copied the `isaac_ros_stereo_image_proc.sh` pattern verbatim
into `docker/_benchmark_patches/isaac_ros_bi3d.sh`. Replaces
`VPI_BACKEND_XAVIER` with `VPI_BACKEND_ORIN` in the two affected sources
inside the sgm package. The runtime branch is x86-dead-code (Jetson dispatch),
so symbol identity doesn't matter — only that it links.
```bash
patch_pre_build() {
  local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
  if [ -d "$sgm_dir" ]; then
    sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
      "$sgm_dir/gxf/gems/vpi/constants.cpp" \
      "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" || true
  fi
}
```

**Outcome:** sgm fixed; new failure surfaced in `gxf_isaac_bi3d`:
```
CMake Error at .../isaac_ros_common/cmake/modules/FindTENSORRT.cmake:35
  Could not find TENSORRT_INCLUDE_DIR using the following files:
  NvInferVersion.h
```
TensorRT is not in fish-r2b-base. Confirmed `tensorrt-dev` is available via
apt (and pulls libnvinfer-dev / libnvinfer-plugin-dev / libnvonnxparsers-dev,
which is exactly what FindTENSORRT.cmake requires). Moving to iter 2.

## Iteration 2 — 2026-05-21
**Hypothesis:** Adding `PATCH_EXTRA_APT="tensorrt-dev"` makes
NvInferVersion.h + nvinfer/nvinfer_plugin/nvonnxparser libs discoverable
under default CMake search paths. No further patches expected, but
gxf_isaac_bi3d compile may surface VPI-4/CMake-4 issues familiar from the
base-build SHARED_LEARNINGS notes.

**Action:** ran the build again with the updated patch file (sgm sed +
tensorrt-dev apt install).

**Observed:** Builds in iter 2 + a parallel iter 2-bis kept hitting `rc=137`
within ~30s — the agent inferred OOM but `dmesg` post-mortem (after the
manager [Claude] took over) showed NO OOM events, plenty of free RAM.
The actual cause was buildkit voluntary cancellation when two simultaneous
docker builds raced on apt-update + shared layer extraction from the
~16 GB fish-r2b-base. Each retry the agent fired re-collided.

**Fix applied:** None additional — the patch itself (VPI_BACKEND_ORIN +
tensorrt-dev) was correct. The agent was killed by its own context budget
before it could finish; the manager (Claude) then:
1. Added `tensorrt-dev` permanently to a new derived image
   `fish-r2b-tensorrt-base:latest` (extends fish-r2b-base, +7 GB TensorRT),
   so future DNN builds skip the 5 GB download.
2. Switched `build_benchmark_image.sh` to `FROM fish-r2b-tensorrt-base`.
3. Re-ran bi3d **serially** (no parallel siblings) → success.

## Iteration 3 (serial retry by manager) — 2026-05-21
**Action:** `SKIP_RUN=1 ./scripts/build_benchmark_image.sh isaac_ros_bi3d`
with build_benchmark_image.sh now FROMing fish-r2b-tensorrt-base, no parallel
siblings.

**Observed:** Build completed cleanly. Status JSON: `build_only  (452s)`.
`fish-r2b-bi3d:latest` produced.

## Final Status
- **Result:** ✅ **PASSED** (build_only)
- **Final image:** `fish-r2b-bi3d:latest`
- **Total wall time:** ~7.5 min build (serial), smoke skipped (SKIP_RUN=1)
- **Notable findings:**
  * VPI_BACKEND_XAVIER → VPI_BACKEND_ORIN sed is required for any benchmark
    whose colcon graph reaches `gxf_isaac_sgm` (sibling pkgs: stereo_image_proc,
    bi3d, bi3d_freespace; possibly nvblox).
  * tensorrt-dev should be in the base image, not a per-benchmark patch.
    Now baked into `fish-r2b-tensorrt-base`.
  * Parallel docker builds against fish-r2b-tensorrt-base initially collided
    (rc=137 within 30s) — NOT memory, NOT real OOM. Likely buildkit shared
    layer extraction race on initial apt-update inside the workload setup.
    Serial works fine.
- **Patch retained:** `docker/_benchmark_patches/isaac_ros_bi3d.sh` keeps the
  VPI_BACKEND_XAVIER sed + tensorrt-dev apt list (no-op vs base now, but
  idempotent and self-documenting).
