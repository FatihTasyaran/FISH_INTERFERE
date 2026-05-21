# Manager Agent Log

> Append-only status feed from the manager agent overseeing the 24-worker fleet.
> Each entry is a wave summary or significant decision. Read by the human supervisor.

## Manager bootstrap — 2026-05-21 17:25 UTC
**Critical environment constraint discovered:** the `Agent` (subagent) tool is
NOT available in this harness. Deferred-tool search returns no match, and the
loaded tool set has no spawning mechanism. The mission spec assumes parallel
subagent fan-out — this is not possible here.

**Adapted plan:** the manager will act AS the worker for each benchmark,
applying the worker protocol (read SHARED_LEARNINGS, run build, diagnose,
patch, max 4 iterations) sequentially within each wave. Cross-wave learnings
will still be summarised between waves. Wave-level parallelism becomes
sequential — wall time inflates, but the per-benchmark protocol is
preserved end-to-end.

Beginning Wave 1.

---

## Wave 1 retrospective — 2026-05-21 (Claude took over after the in-harness manager hit context limit)

**Dispatched (Claude spawned 4 background sub-agents, this DID work for spawning;
later determined that the `Agent` tool IS in scope at the top level, just not
nested. The "manager-spawned worker" pattern fails; "Claude-spawned worker"
pattern works.):**
- isaac_ros_apriltag (had passed earlier — re-spawn skipped)
- isaac_ros_bi3d
- isaac_ros_bi3d_freespace
- isaac_ros_stereo_image_proc
- isaac_ros_h264_decoder

**Results:**
- ✅ `apriltag` — build_only, 332s, 0 patches (also live smoke-tested by manager
  with dataset bind-mount: **273 fps GPU, 4ms latency, 0 dropped frames**)
- ✅ `image_proc` — build_only, base was sufficient (built by the original in-harness
  manager before Claude took over)
- ✅ `stereo_image_proc` — build_only, 604s, 0 patches (Wave 1 worker, 1 iter)
- ⏭️ `h264_decoder` — DEFERRED `arm_only`. `isaac_ros_compression` hard-codes
  Jetson L4T multimedia libs (libcuvidv4l2 etc.) absent on x86. Stub-lib patch
  exists in `docker/_benchmark_patches/isaac_ros_h264_decoder.sh` for ARM retry.
- 🔁 `bi3d` / `bi3d_freespace` — agents wrote good patches (VPI_BACKEND_XAVIER→ORIN
  + tensorrt-dev apt install) but their docker builds kept failing with rc=137
  in 30s. Diagnosed as buildkit shared-layer-extraction race when 4 docker
  builds tried to apt-update concurrently. NOT OOM (dmesg confirmed).

**Cross-cutting findings (appended to SHARED_LEARNINGS):**
- ALL DNN-based benchmarks need TensorRT → built `fish-r2b-tensorrt-base:latest`
  (16.2 GB fish-r2b-base + 7 GB TensorRT) once, so per-benchmark builds skip
  the ~5 GB / ~10 min libnvinfer download.
- `build_benchmark_image.sh` now FROMs fish-r2b-tensorrt-base. Non-DNN benchmarks
  carry unused TensorRT (disk overhead, not functional).
- Dataset bind-mount fix landed in `build_benchmark_image.sh`: it now mounts
  /home/tue037807/r2bdataset2023_v3 + 2024_v1 and symlinks subbags into
  /root/ros_ws/.../r2b_dataset/ during smoke. Workers should drop SKIP_RUN=1.

**Cost:** ~3 hours wall time across 15 fish-r2b-base rebuild iterations + agent
spawn + ghost cleanup. Roughly 4/24 benchmarks PASSED.

**Pivot:** dropped Claude background agents for subsequent waves (they kept
re-firing as "completed" on monitor events, sometimes spawning duplicate builds).
Manager (Claude) now drives `build_benchmark_image.sh` directly via Bash, writes
journals + SHARED_LEARNINGS itself.

---

## Wave 2 in flight — 2026-05-21 20:28
**Dispatched (6 parallel, direct Bash, no agents):**
isaac_ros_dnn_image_encoder, isaac_ros_visual_slam, isaac_ros_ess,
isaac_ros_detectnet, isaac_ros_rtdetr, isaac_ros_centerpose

**Early results:**
- ❌ `ess` — failed 63s. `ISAAC_ROS_WS not set` in `*_models_install` package.
  Fix: COLCON_IGNORE the models_install dir (model weights are runtime-only).
- ❌ `rtdetr` — failed 294s. TensorRT 10.x changed `NvInferVersion.h` layout
  — `NV_TENSORRT_MAJOR` redirects through `TRT_MAJOR_ENTERPRISE`. Isaac ROS
  3.2's `FindTENSORRT.cmake` regex breaks. Fix: sed-patch the installed
  FindTENSORRT.cmake to read `TRT_*_ENTERPRISE` instead.
- 🔧 dnn_image_encoder, visual_slam, detectnet, centerpose still building.

Both cross-cutting findings captured in `docs/SHARED_LEARNINGS.md` "Wave 2
cross-cutting issues" section for the second-pass retries.
