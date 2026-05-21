# Agent Journal — `isaac_ros_apriltag`

## Identity
- **Benchmark:** `isaac_ros_apriltag`
- **Image tag goal:** `fish-r2b-apriltag:latest`
- **Workload repos (from inventory):** ['isaac_ros_apriltag']
- **Smoke-test script:** `isaac_ros_apriltag_node`

## Iteration 1 — 2026-05-21 17:26 UTC
**Hypothesis going in:** apriltag is canary #1 — fish-r2b-base already contains
`isaac_ros_apriltag` package in its workspace, so cloning workload + colcon
build should be a near no-op. No patch file written.

**Action:**
- ran `./scripts/build_benchmark_image.sh isaac_ros_apriltag`
- patch file state: absent (no patch applied)

**Observed:**
- BUILD: OK in 332s
- RUN: FAILED in 20s (rc=1) — `RuntimeError: Bag path
  /root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset/r2b_storage does
  not exist.`

**Diagnosis:** Build chain is correct. Runtime failure is purely missing
benchmark dataset assets — they live on host at
`/home/tue037807/r2bdataset2023_v3/r2b_storage` but the build_benchmark_image.sh
script does not bind-mount or COPY them in. This is an environmental/input
issue orthogonal to per-benchmark patching.

**Fix applied:** None (issue is universal across benchmarks and not in the
worker's territory — build_benchmark_image.sh is hands-off). Re-ran with
`SKIP_RUN=1` to confirm build_only success.

## Iteration 2 — 2026-05-21 17:27 UTC
**Action:** `SKIP_RUN=1 ./scripts/build_benchmark_image.sh isaac_ros_apriltag`

**Observed:** BUILD OK (cache hit, 0s). Status recorded as `build_only`.

## Final Status
- **Result:** PASSED (build_only — counts as success per worker spec)
- **Final image:** `fish-r2b-apriltag:latest`
- **Total wall time:** 332s build, 0s subsequent cache hit
- **Notable findings:** r2b_dataset assets are needed for the launch_test smoke
  step but are not part of the build sweep mandate. Universal across all
  benchmarks — escalated to SHARED_LEARNINGS for future work.
- **Recommended PATCH_EXTRA_*:** none — clean build, no patch needed.
