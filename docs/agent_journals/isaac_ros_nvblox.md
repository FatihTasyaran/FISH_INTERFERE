# Agent Journal — `isaac_ros_nvblox`

> One file per benchmark, owned exclusively by the agent assigned to it.
> Append-only. Each iteration gets its own H2 section.
> After the sweep, journals are mined to produce `docs/isaac_ros_known_issues.md`.

## Identity
- **Benchmark:** `isaac_ros_nvblox`
- **Image tag goal:** `fish-r2b-nvblox:latest`
- **Workload repos (from inventory):** ['isaac_ros_nvblox', 'isaac_ros_visual_slam']
- **Smoke-test script:** `isaac_ros_nvblox_node`

## Summary — 16 iteration arc (Claude as manager+worker)

**Final result: ⏭️ DEFERRED.** Patch file in `docker/_benchmark_patches/isaac_ros_nvblox.sh`
captures every fix attempted; build_only image not produced on this host.

### Patch layers (each iter added one)
1. `libboost-thread-dev libboost-system-dev` — VPI_BACKEND_XAVIER sed for transitive gxf_isaac_sgm
2. `ros-humble-rviz-default-plugins` — for nvblox_rviz_plugin's `find_package(rviz_default_plugins)`
3. COLCON_IGNORE on `nvblox_rviz_plugin/` — visualization, not needed for benchmark
4. `git submodule update --init --recursive` inside cloned repo — `nvblox_core` was missing
5. `libgflags-dev libgoogle-glog-dev` — Google flags/logging used by nvblox_core
6. `libbenchmark-dev libsqlite3-dev` — Google Benchmark, sqlite for nvblox_core thirdparty

### Architectural blocker hit at iter 7+: stdgpu CMake 3.27 strict check
nvblox_core bundles `stdgpu` (3rd-party GPU container lib) via `add_subdirectory`.
stdgpu defines `FILE_SET HEADERS` on its target. nvblox's
`install(TARGETS ... stdgpu EXPORT nvbloxTargets ...)` does NOT re-export those file
sets. CMake 3.27 added a strict check:
> `install TARGETS target stdgpu is exported but not all of its interface file sets are installed`

### Attempted fixes (none worked end-to-end)
- **iter 8**: add `FILE_SET HEADERS` to install command — sed inserted but stdgpu's FILE_SET name didn't match
- **iter 9**: remove `stdgpu` from install TARGETS — downstream `<stdgpu/cstddef.h>` include path broke
- **iter 10**: restore stdgpu + delete `EXPORT nvbloxTargets` line via sed — caused `install(EXPORT)` later to fail with "unknown export"
- **iter 11**: sed range `/install(EXPORT.*Targets/,/^)/d` — orphaned `${PROJECT_NAME}Targets` token, parse error at line 272
- **iter 12-14**: awk multi-line block disarm for `install(EXPORT)` AND `ament_export_targets()` — same `install(EXPORT) given unknown export nvbloxTargets` error
- **iter 15**: multi-line `ament_export_targets(` block stripped — same error
- **iter 16**: python-regex DOTALL disable of both install(TARGETS stdgpu) and install(EXPORT Targets) blocks AND ament_export_targets() — same error

### Hypotheses for follow-up
1. **CMake 3.26 downgrade** for this benchmark: the strict check is 3.27+. Pin cmake==3.26.x via pip in patch_pre_build.
2. **Patch stdgpu's CMakeLists.txt** inside the FetchContent_Declare'd directory — block the `FILE_SET HEADERS` declaration on stdgpu's target.
3. **Drop `add_subdirectory(stdgpu)`** entirely from nvblox_core, link stdgpu as a system lib.
4. **Use NVIDIA's release-3.3 dev branch** if it's released — they may have fixed this upstream.

### Architectural note
NOT a Jetson-vs-x86 hardware issue — nvblox builds on Jetson too. CMake 3.27 + Isaac ROS 3.2 release packaging tension.

---

## Iteration 1 — 2026-05-21 (per-iter detail below, template form)
**Hypothesis going in:** _what you expected to happen_

**Action:**
- ran `./scripts/build_benchmark_image.sh <bench>`
- patch file state: empty / contains X / Y

**Observed:**
- BUILD: ✅ ok / ❌ failed at colcon step Z
- key log lines:
  ```
  <copy 5-10 relevant lines from /tmp/fish-r2b-build-<bench>.log>
  ```

**Diagnosis:** _root cause analysis — be specific about the missing dep / cmake target / header / etc._

**Fix applied:** _what you wrote to docker/_benchmark_patches/<bench>.sh and why_
```bash
# excerpt of patch added
PATCH_EXTRA_APT="ros-humble-foo"
```

**Outcome:** _did the next build pass? did the error change? move to iter 2 if not done_

## Iteration 2 — ...
(repeat)

## Final Status
- **Result:** ✅ PASSED / ❌ FAILED (gave up after N iterations)
- **Final image:** `fish-r2b-nvblox:latest`
- **Total wall time:** Xs build + Ys smoke
- **Notable findings:** _anything future-you / future-agents / documentation should know_
- **Recommended PATCH_EXTRA_*:** final contents of the patch file (if any)
