# Phase 1 — Get all 22 viable benchmarks to baseline-PASS

Incremental: For each smoke-FAIL benchmark, build a `-modelsfetched:latest`
image that runs the upstream `isaac_ros_*_models_install` asset scripts
(with `ISAAC_ROS_ACCEPT_EULA=1`) to fetch ONNX weights + model configs from
NGC, then smoke-test. Originals stay untouched.

| Image | Result | Detail |
|---|---|---|
| `fish-r2b-tensor_rt-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-centerpose-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/models/centerpose_shoe/config.pbtxt' |
| `fish-r2b-bi3d_freespace-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-segformer-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-pynitros-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-ess-modelsfetched:latest` | ❌ FAIL | rc=1 — [component_container_mt-1] terminate called after throwing an instance of 'std::error_code' |
| `fish-r2b-rtdetr-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-detectnet-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/models/peoplenet/config.pbtxt' |
| `fish-r2b-triton-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/models/ketchup/config.pbtxt' |
| `fish-r2b-segment_anything-modelsfetched:latest` | ❌ FAIL | rc=1 —  |
| `fish-r2b-unet-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-bi3d-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-dope-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-foundationpose-modelsfetched:latest` | ❌ FAIL | rc=2 — launch_test: error: trt-converter failed to convert with status: 1. |
| `fish-r2b-occupancy_grid_localizer` | ❌ pending | needs r2b_galileo dataset overlay (no models_install) |
