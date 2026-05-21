# Agent Journal — `isaac_ros_rtdetr`

> One file per benchmark, owned exclusively by the agent assigned to it.
> Append-only. Each iteration gets its own H2 section.
> After the sweep, journals are mined to produce `docs/isaac_ros_known_issues.md`.

## Identity
- **Benchmark:** `isaac_ros_rtdetr`
- **Image tag goal:** `fish-r2b-rtdetr:latest`
- **Workload repos (from inventory):** ['isaac_ros_object_detection', 'isaac_ros_dnn_inference']
- **Smoke-test script:** `isaac_ros_rtdetr_graph`

## Iteration 1 — 2026-05-21 20:28 (manager [Claude] running build directly)
**Hypothesis going in:** Wave 2 fresh build. Workloads: `isaac_ros_object_detection`
+ `isaac_ros_dnn_inference`. Under fish-r2b-tensorrt-base (TensorRT pre-installed)
should be straightforward.

**Action:**
- ran `SKIP_RUN=1 ./scripts/build_benchmark_image.sh isaac_ros_rtdetr` in parallel
  with 5 other wave-2 benchmarks
- patch file state: absent

**Observed:**
- BUILD: ❌ failed at `gxf_isaac_tensor_rt` after 294s (rc=1)
- key log lines:
  ```
  --- stderr: gxf_isaac_tensor_rt
  CMake Error at /usr/local/lib/python3.10/dist-packages/cmake/data/share/cmake-3.27/Modules/FindPackageHandleStandardArgs.cmake:230 (message):
    Could NOT find TENSORRT: Found unsuitable version "..", but required is at
    least "10" (found /usr/include/x86_64-linux-gnu)
  Call Stack (most recent call first):
    FindTENSORRT.cmake:68 (find_package_handle_standard_args)
    CMakeLists.txt:30 (find_package)
  ```

**Diagnosis:** Isaac ROS 3.2's `FindTENSORRT.cmake` parses the version with:
```cmake
string(REGEX MATCH "NV_TENSORRT_MAJOR ([0-9]+)" _ "${_TRT_VERSION_FILE}")
```
But TensorRT 10.x's `NvInferVersion.h` redirects through enterprise macros:
```c
#define TRT_MAJOR_ENTERPRISE 10
#define NV_TENSORRT_MAJOR TRT_MAJOR_ENTERPRISE
```
The regex sees `NV_TENSORRT_MAJOR TRT_MAJOR_ENTERPRISE` — no digit match,
so MAJOR/MINOR/PATCH end up empty, version becomes `..`, FindPackage fails
the `>= 10` check.

Cross-cutting issue (recorded in `docs/SHARED_LEARNINGS.md`): affects every
DNN benchmark whose colcon graph reaches `gxf_isaac_tensor_rt`. Probably all
of: rtdetr, detectnet, dnn_image_encoder, centerpose, dope, segformer, unet,
triton, tensor_rt, foundationpose, segment_anything.

**Fix to apply:** `patch_pre_build` sed that retargets the regex at
`TRT_*_ENTERPRISE` macros (which DO contain digits) instead of the redirected
`NV_TENSORRT_*` macros:

```bash
patch_pre_build() {
    FT=/root/ros_ws/install/isaac_ros_common/share/isaac_ros_common/cmake/modules/FindTENSORRT.cmake
    [ -f "$FT" ] && sed -i '
      s/read_version(NV_TENSORRT_MAJOR/read_version(TRT_MAJOR_ENTERPRISE/;
      s/read_version(NV_TENSORRT_MINOR/read_version(TRT_MINOR_ENTERPRISE/;
      s/read_version(NV_TENSORRT_PATCH/read_version(TRT_PATCH_ENTERPRISE/;
      s/\${NV_TENSORRT_MAJOR}/${TRT_MAJOR_ENTERPRISE}/;
      s/\${NV_TENSORRT_MINOR}/${TRT_MINOR_ENTERPRISE}/;
      s/\${NV_TENSORRT_PATCH}/${TRT_PATCH_ENTERPRISE}/
    ' "$FT"
}
```

**Outcome:** patch not yet applied in this iter; recorded for iter 2.
