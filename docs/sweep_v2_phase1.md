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
