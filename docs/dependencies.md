# Dependencies

## Container-side (required)

These must be installed inside the container where ROS 2 nodes run.
Use `./install_fish_deps.sh` to check and install automatically.

| Package | What for |
|---------|----------|
| `lttng-tools` | LTTng session daemon and CLI |
| `liblttng-ust-dev` | LTTng-UST headers (overlay build) |
| `python3-lttng` | Python LTTng bindings |
| `ros-humble-tracetools` | Base libtracetools.so |
| `ros-humble-tracetools-trace` | Tracetools trace session tools |
| `ros-humble-ros2trace` | `ros2 trace` CLI command |
| `git` | Clone tracetools source for overlay |
| `python3-colcon-common-extensions` | Build overlay workspace |

## GPU profiling (optional)

| Package | What for |
|---------|----------|
| `nsight-systems-2025.6.3` | NVIDIA Nsight Systems for GPU kernel profiling |

`install_fish_deps.sh` will offer to install nsys from the NVIDIA CUDA apt
repository if not found.

## Host-side (post-processing)

These are needed on the host machine for trace analysis, not inside the
container.

| Package | What for |
|---------|----------|
| `pymongo` | MongoDB trace ingest |
| `influxdb_client_3` | InfluxDB3 GPU data ingest |
| `pandas` | Data analysis |
| `networkx` | Graph model |
| `graphviz` | Visualization rendering |
| `babeltrace2` | LTTng trace reading |
