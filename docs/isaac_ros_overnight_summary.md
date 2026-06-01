# Overnight v2 sweep — final 3-table summary

# Phase 1 v2 — baseline-PASS sweep (modelsfetched-v2)

Only 3 install scripts exist in release-3.2:
- `isaac_ros_peoplesemseg_models_install` (peoplesemsegnet shuffleseg + vanilla)
- `isaac_ros_peoplenet_models_install` (peoplenet AMR)
- `isaac_ros_ess_models_install` (ESS stereo)

Other 9 benchmarks (bi3d, bi3d_freespace, centerpose, dope, foundationpose,
rtdetr, segformer, segment_anything, triton) lack install scripts in release-3.2
— need manual NGC URLs (deferred).

| Image | Result | Detail |
|---|---|---|
| `fish-r2b-tensor_rt-modelsfetched-v2:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-pynitros-modelsfetched-v2:latest` | ❌ FAIL | rc=1 — [pynitros_dnn_image_encoder_node-2] ModuleNotFoundError: No module named 'cuda' |
| `fish-r2b-ess-modelsfetched-v2:latest` | ❌ FAIL | rc=1 — [05/22/2026-00:17:06] [I] errorOnTimingCacheMiss: Disabled |
| `fish-r2b-detectnet-modelsfetched-v2:latest` | ❌ FAIL | rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/models/peoplenet/config.pbtxt' |
| `fish-r2b-unet-modelsfetched-v2:latest` | ✅ PASS | 419.641 fps |

## Not recoverable from release-3.2 alone (no models_install pkg)

| Benchmark | Needs | NGC URL (lookup required) |
|---|---|---|
| `bi3d`, `bi3d_freespace` | `bi3d/featnet.onnx` + `bi3d/segnet.onnx` | manual NGC download |
| `centerpose` | `centerpose_shoe/centerpose_shoe.onnx` + config.pbtxt | manual NGC |
| `dope`, `triton` (dope variant), `tensor_rt` (dope variant) | `ketchup/ketchup.onnx` (+ config.pbtxt for triton) | manual NGC |
| `foundationpose` | `foundationpose/refine_model.onnx`, `foundationpose/score_model.onnx`, `sdetr/sdetr_grasp.onnx` | manual NGC |
| `rtdetr` | `sdetr/sdetr_grasp.onnx` | manual NGC |
| `segformer` | `peoplesemsegformer/peoplesemsegformer.onnx` | manual NGC |
| `segment_anything` | `segment_anything/mobile_sam.onnx` + config.pbtxt, `sdetr/sdetr_grasp.onnx` | manual NGC |
| `occupancy_grid_localizer` | r2b_galileo dataset overlay (ament resource) | NVIDIA-ISAAC-ROS-DEV/isaac_ros_r2b_galileo |

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

# Phase 3 v2 — FISH ingest to InfluxDB

| Benchmark | Session dir | Ingest result |
|---|---|---|
| `isaac_ros_apriltag` | `fish_20260522_002340` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_apriltag/fish_20260522_002340 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_002340, container=fish_20260522_002340)  |
| `isaac_ros_detectnet` | `fish_20260522_004523` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_detectnet/fish_20260522_004523 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_004523, container=fish_20260522_004523)  |
| `isaac_ros_dnn_image_encoder` | `fish_20260522_002650` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_dnn_image_encoder/fish_20260522_002650 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_002650, container=fish_20260522_002650 |
| `isaac_ros_ess` | `fish_20260522_004529` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_ess/fish_20260522_004529 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_004529, container=fish_20260522_004529)  |
| `isaac_ros_image_proc` | `fish_20260522_003036` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_image_proc/fish_20260522_003036 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_003036, container=fish_20260522_003036)  |
| `isaac_ros_nitros_bridge` | `fish_20260522_003406` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_nitros_bridge/fish_20260522_003406 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_003406, container=fish_20260522_003406)  |
| `isaac_ros_nvblox` | `fish_20260522_003451` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_nvblox/fish_20260522_003451 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_003451, container=fish_20260522_003451)  |
| `isaac_ros_pynitros` | `fish_20260522_004558` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_pynitros/fish_20260522_004558 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_004558, container=fish_20260522_004558)  |
| `isaac_ros_stereo_image_proc` | `fish_20260522_003547` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_stereo_image_proc/fish_20260522_003547 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_003547, container=fish_20260522_003547 |
| `isaac_ros_tensor_rt` | `fish_20260522_004824` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_tensor_rt/fish_20260522_004824 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_004824, container=fish_20260522_004824)  |
| `isaac_ros_unet` | `fish_20260522_004007` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_unet/fish_20260522_004007 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_004007, container=fish_20260522_004007)  |
| `isaac_ros_visual_slam` | `fish_20260522_003909` | [FISH ingest] Session dir:     /tmp/fish_traces_v2/isaac_ros_visual_slam/fish_20260522_003909 [FISH ingest] InfluxDB database: fish  (session=fish_20260522_003909, container=fish_20260522_003909)  |
