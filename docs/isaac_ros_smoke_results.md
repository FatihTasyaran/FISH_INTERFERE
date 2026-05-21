# Isaac ROS benchmark smoke-test results

> Each built `fish-r2b-<bench>:latest` image was launched with the r2b dataset
> bind-mounted; `launch_test` ran the benchmark's primary script under a wall-clock
> timeout. Pass = workload emitted a benchmark metric (Mean Frame Rate, latency,
> etc.) before exit. Fail = no metric, reason captured from log tail.

| Benchmark | Smoke result | Detail |
|---|---|---|
| `isaac_ros_apriltag` | ✅ PASS | 191s — benchmark metrics emitted (rc=0) |
| `isaac_ros_bi3d` | ❌ FAIL | 2s rc=2 — launch_test: error: [Errno 2] No such file or directory: '/usr/src/tensorrt/bin/trtexec' |
| `isaac_ros_bi3d_freespace` | ❌ FAIL | 1s rc=2 — launch_test: error: [Errno 2] No such file or directory: '/usr/src/tensorrt/bin/trtexec' |
| `isaac_ros_centerpose` | ❌ FAIL | 2s rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/models/centerpose_shoe/config.pbtxt' |
| `isaac_ros_detectnet` | ❌ FAIL | 2s rc=2 — launch_test: error: [Errno 2] No such file or directory: '/root/ros_ws/src/ros2_benchmark/assets/models/peoplenet/config.pbtxt' |
| `isaac_ros_dnn_image_encoder` | ✅ PASS | 206s — benchmark metrics emitted (rc=0) |
| `isaac_ros_dope` | ❌ FAIL | 1s rc=2 — launch_test: error: [Errno 2] No such file or directory: '/usr/src/tensorrt/bin/trtexec' |
| `isaac_ros_ess` | ❌ FAIL | 18s rc=1 — [component_container_mt-1] terminate called after throwing an instance of 'std::error_code' |
