# Agent Journal — `isaac_ros_centerpose`

> One file per benchmark, owned exclusively by the agent assigned to it.
> Append-only. Each iteration gets its own H2 section.
> After the sweep, journals are mined to produce `docs/isaac_ros_known_issues.md`.

## Identity
- **Benchmark:** `isaac_ros_centerpose`
- **Image tag goal:** `fish-r2b-centerpose:latest`
- **Workload repos (from inventory):** ['isaac_ros_pose_estimation', 'isaac_ros_dnn_inference']
- **Smoke-test script:** `isaac_ros_centerpose_graph`

## Iteration 1 — 2026-05-21 HH:MM
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
- **Final image:** `fish-r2b-centerpose:latest`
- **Total wall time:** Xs build + Ys smoke
- **Notable findings:** _anything future-you / future-agents / documentation should know_
- **Recommended PATCH_EXTRA_*:** final contents of the patch file (if any)
