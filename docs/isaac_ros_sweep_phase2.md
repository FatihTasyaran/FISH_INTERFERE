# Phase 2 — Run each baseline-PASS benchmark under FISH (properly activated)

For each image that produced a benchmark metric in baseline (Phase 0 originals
+ Phase 1 -modelsfetched variants), build a `-fishtraced:latest` overlay that:
- Prepends `/opt/ros/humble/fish/bin` to `PATH` (FISH ros2 wrapper takes priority)
- Wraps `launch_test` with `trace_session.sh start_session` / `stop_session`
- Captures `~/fish_traces/<session>/` to the host

Result columns:
- **benchmark output**: did launch_test emit "Mean Frame Rate (fps)"?
- **fish data**: are `ros2/`, `nsys/`, `fishlog/`, `snapshot/` populated under fish_traces?

| Image | benchmark output | fish data | Detail |
|---|---|---|---|
| `fish-r2b-apriltag:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-apriltag:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-dnn_image_encoder:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-dnn_image_encoder:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-image_proc:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-image_proc:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-nitros_bridge:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-nitros_bridge:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-nvblox:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-nvblox:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-stereo_image_proc:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-stereo_image_proc:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-visual_slam:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-visual_slam:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-foundationpose-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-foundationpose-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-dope-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-dope-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-bi3d-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-bi3d-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-unet-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-unet-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-segment_anything-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-segment_anything-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-triton-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-triton-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-detectnet-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-detectnet-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-rtdetr-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-rtdetr-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-ess-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-ess-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-pynitros-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-pynitros-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-segformer-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-segformer-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-bi3d_freespace-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-bi3d_freespace-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-centerpose-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-centerpose-modelsfetched:latest-fishtraced:latest": invalid reference format |
| `fish-r2b-tensor_rt-modelsfetched:latest-fishtraced:latest` | ❌ build fail | — | ERROR: failed to build: invalid tag "fish-r2b-tensor_rt-modelsfetched:latest-fishtraced:latest": invalid reference format |
