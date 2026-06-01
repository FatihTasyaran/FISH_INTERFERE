# Phase 2 v2 — FISH-traced sweep

For each baseline-PASS image, derive `-fishtraced:latest` with FISH wrapper
PATH + trace_session start/stop wrap. Check (a) benchmark output (b) fish data.

| Image | benchmark output | fish data | Detail |
|---|---|---|---|
| `fish-r2b-apriltag-fishtraced:latest` | ✅ 304.275fps | ✅ fish_20260522_002340 (49 files) | rc=0  |
| `fish-r2b-dnn_image_encoder-fishtraced:latest` | ✅ 818.653fps | ✅ fish_20260522_002650 (44 files) | rc=0  |
| `fish-r2b-image_proc-fishtraced:latest` | ✅ 1255.965fps | ✅ fish_20260522_003036 (44 files) | rc=0  |
| `fish-r2b-nitros_bridge-fishtraced:latest` | ❌ | ✅ fish_20260522_003406 (44 files) | rc=1 — [ERROR] [1779410077.862952735] [r2b.Controller.Looping.Probing]: No messages were observed from the  |
| `fish-r2b-nvblox-fishtraced:latest` | ✅ 9.349fps | ✅ fish_20260522_003451 (44 files) | rc=0  |
| `fish-r2b-stereo_image_proc-fishtraced:latest` | ✅ 170.355fps | ✅ fish_20260522_003547 (53 files) | rc=0  |
| `fish-r2b-visual_slam-fishtraced:latest` | ✅ 33.342fps | ✅ fish_20260522_003909 (44 files) | rc=0  |
| `fish-r2b-unet-modelsfetched-v2-fishtraced:latest` | ✅ 422.884fps | ✅ fish_20260522_004007 (44 files) | rc=124  |
| `fish-r2b-detectnet-modelsfetched-v2-fishtraced:latest` | ❌ | ✅ fish_20260522_004523 (44 files) | rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/mod |
| `fish-r2b-ess-modelsfetched-v2-fishtraced:latest` | ❌ | ✅ fish_20260522_004529 (51 files) | rc=1 — [05/22/2026-00:45:31] [I] errorOnTimingCacheMiss: Disabled |
| `fish-r2b-pynitros-modelsfetched-v2-fishtraced:latest` | ❌ | ✅ fish_20260522_004558 (44 files) | rc=1 — [pynitros_dnn_image_encoder_node-2] ModuleNotFoundError: No module named 'cuda' |
| `fish-r2b-tensor_rt-modelsfetched-v2-fishtraced:latest` | ❌ | ✅ fish_20260522_004824 (44 files) | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
