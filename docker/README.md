# docker/

Dockerfiles and build helpers for FISH images.

## `fish-base.Dockerfile`

Minimal foundation image for FISH-based test fixtures (Isaac ROS,
ros2_benchmark, custom workloads). Built on
`nvidia/cuda:12.6.3-devel-ubuntu22.04`, adds ROS 2 Humble, the FISH
apt dependency set, NVIDIA Nsight Systems, and the FISH framework
itself.

Target size: ~10–12 GB.

### Build

From the repo root:

```bash
./docker/build_fish_base.sh           # tag: fish-base:cuda12.6-humble
IMAGE_TAG=v0.1 ./docker/build_fish_base.sh
```

Build context = repo root, because the Dockerfile copies
`scripts/`, `config/`, `fish_tracepoints/`, `python/`, and
`fish_orchestrator.py` from there.

First build takes ~15–30 minutes (apt installs + FISH tracepoint
overlay colcon build). Re-builds with only Dockerfile tweaks are
much faster thanks to layer caching.

### Smoke test

```bash
docker run --rm --gpus all fish-base:cuda12.6-humble bash -lc \
    'nvcc --version; ros2 --help | head -3; which ros2; echo FISH_ENABLED=$FISH_ENABLED'
```

Expected:
- `nvcc --version` → CUDA 12.6
- `ros2` resolves to `/opt/ros/humble/fish/bin/ros2` (the FISH wrapper)
- `FISH_ENABLED=1`

### Typical run for an Isaac ROS / ros2_benchmark fixture

The image keeps the per-workload colcon workspace OUTSIDE itself; mount it:

```bash
docker run --rm -it --gpus all \
    -v $HOME/ros_ws:/root/ros_ws \
    -v $HOME/r2bdataset2023_v3:/root/r2bdataset2023_v3:ro \
    -v $HOME/fish_traces:/root/fish_traces \
    fish-base:cuda12.6-humble
```

Inside, clone the workload source(s) into `~/ros_ws/src/`, then
`colcon build` and launch the benchmark with FISH wrapping.

### What stays IN the image

- CUDA 12.6 toolkit + nvcc + dev libs
- ROS 2 Humble (ros-base profile + tracetools / cv_bridge / image_transport / apriltag / ros-testing)
- LTTng + python3-lttng + babeltrace2 (FISH capture pipeline)
- python3-colcon-common-extensions (colcon build)
- Nsight Systems 2025.6.3 (`nsys`)
- FISH framework at `/opt/ros/humble/fish/`
- FISH tracepoint overlay at `/root/trace_overlay_ws/`
- `FISH_ENABLED=1` exported by default

### What stays OUT of the image

- The host-side post-processing pipeline (`postprocess/`) — runs on
  the host, not in this container.
- Per-workload colcon workspaces (Isaac ROS / ros2_benchmark
  source) — mounted at run time so the image stays stable.
- r2b dataset rosbags — too big and reusable, mount read-only.

### Notes

- Driver `libcuda.so` still comes from the host at run time via
  `--gpus all` (NVIDIA container runtime). The image carries the
  toolkit only.
- The image is x86_64 only for now; an aarch64/Jetson variant is
  future work — see `notes/isaac_ros_coverage.txt` priority P5.
