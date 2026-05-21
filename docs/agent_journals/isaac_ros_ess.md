# Agent Journal — `isaac_ros_ess`

> One file per benchmark, owned exclusively by the agent assigned to it.
> Append-only. Each iteration gets its own H2 section.
> After the sweep, journals are mined to produce `docs/isaac_ros_known_issues.md`.

## Identity
- **Benchmark:** `isaac_ros_ess`
- **Image tag goal:** `fish-r2b-ess:latest`
- **Workload repos (from inventory):** ['isaac_ros_dnn_stereo_depth']
- **Smoke-test script:** `isaac_ros_ess_node`

## Iteration 1 — 2026-05-21 20:28 (manager [Claude] running build directly)
**Hypothesis going in:** Wave 2 fresh build. Workload `isaac_ros_dnn_stereo_depth`
under fish-r2b-tensorrt-base should "just work" — TensorRT pre-installed,
common pkgs pre-built.

**Action:**
- ran `SKIP_RUN=1 ./scripts/build_benchmark_image.sh isaac_ros_ess` in parallel
  with 5 other wave-2 benchmarks
- patch file state: absent (no patch)

**Observed:**
- BUILD: ❌ failed at `isaac_ros_ess_models_install` after 63s (rc=1)
- key log lines:
  ```
  --- stderr: isaac_ros_ess_models_install
  CMake Error at /root/ros_ws/install/isaac_ros_common/share/isaac_ros_common/cmake/isaac_ros_common-extras-assets.cmake:34 (message):
    ERROR: ISAAC_ROS_WS is not set.
  Call Stack (most recent call first):
    CMakeLists.txt:24 (install_isaac_ros_asset)
  Failed   <<< isaac_ros_ess_models_install [2.76s, exited with code 1]
  ```

**Diagnosis:** `isaac_ros_ess_models_install` is a CMake "package" whose
sole purpose is to call `install_isaac_ros_asset` (NVIDIA's helper for
downloading pretrained DNN weights from NGC). The helper requires
`ISAAC_ROS_WS` env var pointing at the workspace root AND network access.
This pkg is part of the `isaac_ros_dnn_stereo_depth` repo (cloned as a
workload dep for ess).

For a **build-only sweep**, model weights are irrelevant (you can't smoke
them without a running ROS node anyway). The simplest fix is to mark the
models_install pkg as `COLCON_IGNORE` so colcon skips it. The ess benchmark
node itself does NOT depend on the install target at build time.

**Fix to apply:** `patch_pre_build` that touches a COLCON_IGNORE marker file
inside every `*_models_install` subdir under `/root/ros_ws/src/`. Per
SHARED_LEARNINGS, this is a cross-cutting pattern (likely affects detectnet,
centerpose, segformer, unet, ... — anywhere NVIDIA ships a `_models_install`
companion pkg).

```bash
patch_pre_build() {
    find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \;
}
```

**Outcome:** patch not yet applied in this iter; recorded for iter 2.
