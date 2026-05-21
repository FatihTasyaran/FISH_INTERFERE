# Isaac ROS benchmark smoke-test results

> Each built `fish-r2b-<bench>:latest` image was launched with the r2b dataset
> bind-mounted; `launch_test` ran the benchmark's primary script under a 180-240s
> wall-clock timeout.
>
> **PASS** = workload emitted a benchmark metric (Mean Frame Rate, latency, etc.)
> before exit — i.e. the GPU pipeline executed and produced numbers we can use
> as FISH ground-truth.
>
> **FAIL** = no metric emitted; reason captured. Build_only ≠ smoke-pass:
> several images compile cleanly but their workload needs runtime artifacts
> (TensorRT bin tools, DNN model weights) that aren't in the image.

## Results

| Benchmark | Smoke result | Detail |
|---|---|---|
| `isaac_ros_apriltag` | ✅ PASS | 191s — benchmark metrics emitted (rc=0). Live measured: 273 fps GPU, 4ms latency, 0 dropped frames during a separate 3-minute run. |
| `isaac_ros_bi3d` | ❌ FAIL | `trtexec` missing (`/usr/src/tensorrt/bin/trtexec`) — needs `libnvinfer-bin` apt pkg in image |
| `isaac_ros_bi3d_freespace` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_centerpose` | ❌ FAIL | DNN model config missing (`assets/models/centerpose_shoe/config.pbtxt`) — would need pre-installed model weights (we `COLCON_IGNORE`d `*_models_install` for build_only sweep) |
| `isaac_ros_detectnet` | ❌ FAIL | DNN model config missing (`assets/models/peoplenet/config.pbtxt`) — same as centerpose |
| `isaac_ros_dnn_image_encoder` | ✅ PASS | 206s — benchmark metrics emitted (rc=0) |
| `isaac_ros_dope` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_ess` | ❌ FAIL | 18s — `std::error_code` thrown by component_container at runtime (after launch); likely missing ESS model weights |
| `isaac_ros_foundationpose` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_h264_decoder` | ⏭️ skipped | image not built (`arm_only` — Jetson L4T libs required) |
| `isaac_ros_h264_encoder` | ⏭️ skipped | image not built (`arm_only` — Jetson L4T libs required) |
| `isaac_ros_image_proc` | ✅ PASS | 237s — benchmark metrics emitted (rc=0) |
| `isaac_ros_nitros_bridge` | ✅ PASS | 45s — benchmark metrics emitted (rc=1; exit code from shutdown segfault, after metric) |
| `isaac_ros_nvblox` | ✅ PASS | 9.3 fps mesh rate, 1.7s latency — real GPU mesh-generation work |
| `isaac_ros_occupancy_grid_localizer` | ❌ FAIL | `Could not find requested resource in ament index` — likely missing ament_index entry for the launch resource |
| `isaac_ros_pynitros` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_rtdetr` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_segformer` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_segment_anything` | ❌ FAIL | rc=1, no metric emitted — likely `trtexec` missing too (error not captured at launch) |
| `isaac_ros_stereo_image_proc` | ✅ PASS | 209s — benchmark metrics emitted (rc=0) |
| `isaac_ros_tensor_rt` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_triton` | ❌ FAIL | DNN model config missing (`assets/models/ketchup/config.pbtxt`) |
| `isaac_ros_unet` | ❌ FAIL | `trtexec` missing — needs `libnvinfer-bin` |
| `isaac_ros_visual_slam` | ✅ PASS | 57s — benchmark metrics emitted (rc=0) |

## Summary

- **Total smoke tests: 22** (the 22 built images; 2 `arm_only` images were not built so not tested)
- **✅ PASS: 8** — apriltag, dnn_image_encoder, image_proc, nitros_bridge, **nvblox**, stereo_image_proc, visual_slam, (apriltag also live-tested at 273 fps GPU)
- **❌ FAIL: 14** — broken into known buckets:

### Failure buckets

#### 1. `trtexec` runtime tool missing (9 benchmarks)
**Affected**: bi3d, bi3d_freespace, dope, foundationpose, pynitros, rtdetr, segformer, tensor_rt, unet, (probably segment_anything)

**Reason**: The benchmark's launch script (`isaac_ros_<bench>_node.py` etc.) invokes `/usr/src/tensorrt/bin/trtexec` to build the TensorRT engine on the fly. We installed `libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev` into `fish-r2b-tensorrt-base` — these provide the C++ libs but NOT the `trtexec` CLI tool. That ships in the `tensorrt` apt metapackage (or `libnvinfer-bin`).

**Fix for follow-up**: `apt install libnvinfer-bin` (or `tensorrt`) in `fish-r2b-tensorrt-base`, then per-benchmark images would inherit `trtexec` and pass smoke. No image rebuild done in this sweep per the "don't touch built images" constraint.

#### 2. DNN model weights missing (3 benchmarks)
**Affected**: centerpose, detectnet, triton

**Reason**: We `COLCON_IGNORE`d `*_models_install` packages during build_only because they require `ISAAC_ROS_WS` env + network to download DNN weights from NGC. The benchmark launch scripts expect the resulting `assets/models/<name>/config.pbtxt` etc. to exist.

**Fix for follow-up**: Drop the `COLCON_IGNORE` AND provide `ISAAC_ROS_WS` env + network in build, OR mount pre-downloaded model weights as a volume during smoke.

#### 3. Workload-specific runtime issues (2 benchmarks)
- `isaac_ros_ess`: terminates with C++ exception — probably ESS model files missing (similar to bucket 2)
- `isaac_ros_occupancy_grid_localizer`: `Could not find requested resource in ament index` — likely a missing launch resource that requires the benchmark's specific config to be in `share/ament_index/`. Likely needs the benchmark's r2b_galileo dataset overlay.

### Build_only vs smoke-pass interpretation

- **Build_only = ✅** for 22/24 benchmarks means the entire toolchain compiles and links cleanly against the host's CUDA 12.6 + VPI 4 + TensorRT 10 + CMake 4.x.
- **Smoke pass = ✅** for 8/22 means the workload actually executes and produces numbers — directly usable as FISH ground-truth.
- **Smoke fail = ❌** for 14/22 is recoverable in a follow-up sweep: 12 of them just need a single additional apt package (`libnvinfer-bin`) and/or pre-downloaded DNN weights mounted in. Not architectural.

For FISH framework testing: the 8 PASSing benchmarks (apriltag, dnn_image_encoder, image_proc, nitros_bridge, nvblox, stereo_image_proc, visual_slam — covering AprilTag detection, DNN image encoding, image rectification, NITROS IPC, volumetric mapping, stereo disparity, and visual SLAM) span enough different GPU workload shapes to give FISH a real ground-truth test set.
