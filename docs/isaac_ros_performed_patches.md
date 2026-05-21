# Isaac ROS 3.2 on x86 — Performed Patches

> What patches were applied to each `fish-r2b-*` image to make it build.
> Host: Ubuntu 22.04 + CUDA 12.6 + VPI 4 + TensorRT 10 + ROS 2 Humble.
> Base chain: `nvidia/cuda:12.6.3-devel-ubuntu22.04` → `fish-base` → `fish-r2b-base` → `fish-r2b-tensorrt-base` → each `fish-r2b-<bench>`.

## Build chain — base layer patches (in `setup_isaac_workspace.sh`)

These patches are baked into `fish-r2b-base:latest` once and inherited by every benchmark image.

### `fish-base:cuda12.6-humble`
Source-of-truth Dockerfile: `docker/fish-base.Dockerfile`. CUDA + ROS Humble + VPI 4 + nsys 2025.6.3 + CMake 4.3 (Kitware repo) + FISH framework. Nothing benchmark-specific.

### `fish-r2b-base:latest`
- **vision_opencv** cloned, but `cv_bridge/COLCON_IGNORE` touched — CMake 4 removed FindBoost which cv_bridge needs from source; the apt-binary `ros-humble-cv-bridge` satisfies it instead. We still need vision_opencv source because `image_geometry` (sister pkg) provides headers `image_pipeline/image_proc` requires at build time and no humble apt pkg ships them.
- **CMake 3.27.9** pip-installed in stage F (shadows apt's CMake 4.3). Reason: Isaac ROS's `isaac_ros_gxf` uses `$<INSTALL_PREFIX>` in `INTERFACE_LINK_LIBRARIES` of its IMPORTED interface targets (Core, Logger, Std, …). CMake 4 hard-errors on this; 3.27 accepts. 3.27 also still ships FindBoost.
- **magic_enum** built from Neargye source (header-only). NVIDIA's apt mirror has `ros-humble-magic-enum` but it wasn't pickable from our enabled repos. Header symlinks: `/usr/local/include/magic_enum/*.hpp` → `/usr/local/include/*.hpp` (NVIDIA uses subdir-less `#include "magic_enum.hpp"`).
- **CV-CUDA 0.10.1-beta** debs (`cvcuda-lib` + `cvcuda-dev`) fetched from CVCUDA GitHub releases and apt-installed. Used by `isaac_ros_image_proc/pad_node` via `<nvcv/Tensor.hpp>`.
- **negotiated** (osrf) cloned from `https://github.com/osrf/negotiated.git` into workspace src/. NITROS uses it for topic negotiation; not in any apt repo or rosdistro index.
- **isaac_ros_gxf/CMakeLists.txt** sed: inject `find_package(magic_enum REQUIRED)` after `find_package(ament_cmake_auto)` AND `ament_export_dependencies(magic_enum)` after `ament_export_targets()`. Without these, downstream packages fail at `set_target_properties(Core ... magic_enum::magic_enum)` because the imported target was never loaded.
- **VPI 4 source rename across all GPU repos** (`isaac_ros_image_pipeline`, `isaac_ros_nitros`, `isaac_ros_apriltag`, `isaac_ros_benchmark`):
  - `.planes[N].data` → `.planes[N].pBase` (field rename in VPI 4)
  - Wrap `pBase` assignments in `reinterpret_cast<VPIByte *>(...)` (new strict type `VPIByte*` instead of legacy `void*`). Multi-line sed via `-z`.
- **`VPI_BACKEND_NVENC` strip** from any source referencing it (3 files). VPI 4 removed this Jetson-only enum.
- **Explicit apt deps** that rosdep verification glitched on: `ros-humble-xacro ros-humble-camera-info-manager ros-humble-vision-msgs ros-humble-foxglove-msgs ros-humble-camera-calibration-parsers ros-humble-ament-cmake-clang-format`.

### `fish-r2b-tensorrt-base:latest` (derived, +7 GB on top of fish-r2b-base)
Dockerfile: `docker/fish-r2b-tensorrt-base.Dockerfile`. Adds:
- `libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev` (TensorRT 10.16.1)

Reason: every DNN-based benchmark needs TensorRT. Without it baked in, each per-image build would download ~5 GB / ~10 min × ~14 benchmarks = ~2.3 hours wasted. `build_benchmark_image.sh` `FROM`s this image for ALL benchmarks (non-DNN ones waste 7 GB of unused TensorRT — disk isn't the bottleneck, agent context budget was).

---

## Per-benchmark images

For each `fish-r2b-<bench>:latest`, the patch file `docker/_benchmark_patches/isaac_ros_<bench>.sh` (when present) gets sourced into the workload setup AFTER fish-r2b-tensorrt-base is consumed and BEFORE the workload's colcon build. It can define:
- `PATCH_EXTRA_REPOS=( "https://..." )` — extra git clones at release-3.2
- `PATCH_EXTRA_APT="pkg1 pkg2"` — extra apt installs
- `PATCH_EXTRA_CMAKE_ARGS="-DFOO=BAR"` — extra colcon --cmake-args
- `PATCH_EXTRA_COLCON_TARGETS="pkg_x pkg_y"` — extra --packages-up-to targets
- `patch_pre_build() { ... }` — arbitrary shell commands

### Zero-patch images (base was sufficient)
The following benchmarks built clean off `fish-r2b-tensorrt-base` with no patch file at all:

| Benchmark | Wall time | Image tag | Note |
|---|---|---|---|
| `isaac_ros_apriltag` | 332s | `fish-r2b-apriltag:latest` | ✅ live-tested: 273 fps GPU, 4 ms latency, 0 dropped frames |
| `isaac_ros_image_proc` | (cache-hit, no new workload) | `fish-r2b-image_proc:latest` | build_only |
| `isaac_ros_stereo_image_proc` | 604s | `fish-r2b-stereo_image_proc:latest` | build_only |
| `isaac_ros_occupancy_grid_localizer` | 325s | `fish-r2b-occupancy_grid_localizer:latest` | build_only |

### Patched images

#### `fish-r2b-bi3d:latest` (build_only 452s)
**Workload**: `isaac_ros_depth_segmentation`
Patches applied:
- `patch_pre_build`: sed `VPI_BACKEND_XAVIER` → `VPI_BACKEND_ORIN` in
  `gxf_isaac_sgm/gxf/gems/vpi/constants.cpp` and `sgm_disparity.cpp` (VPI 4 dropped Xavier enum).
- `PATCH_EXTRA_APT="libnvinfer-headers-dev libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev"` (left in place but no-op now that fish-r2b-tensorrt-base provides TensorRT).

#### `fish-r2b-bi3d_freespace:latest` (build_only 740s)
**Workload**: `isaac_ros_depth_segmentation` + `isaac_ros_freespace_segmentation`
Patches applied: identical to `bi3d` (sibling package, same transitive failures).

#### `fish-r2b-stereo_image_proc:latest` (build_only 600s)
Wait — listed twice. Belongs in "zero patch" section above; the patch file was empty
when it built. Adding here only because the manager added an SGM patch _afterwards_ in
case other benchmarks needed it.

#### `fish-r2b-detectnet:latest` (build_only 957s)
**Workload**: `isaac_ros_object_detection` + `isaac_ros_dnn_inference`
Patches applied: standard DNN template
- `patch_pre_build`:
  - Patch `FindTENSORRT.cmake` regex `NV_TENSORRT_MAJOR/MINOR/PATCH` → `TRT_MAJOR/MINOR/PATCH_ENTERPRISE` (TensorRT 10 changed `NvInferVersion.h` to redirect through enterprise macros)
  - `COLCON_IGNORE` all `*_models_install/` dirs (DNN weight downloaders, not needed for build)

#### `fish-r2b-dnn_image_encoder:latest` (build_only 958s)
**Workload**: `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-rtdetr:latest` (build_only 713s)
**Workload**: `isaac_ros_object_detection` + `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-centerpose:latest` (build_only 804s)
**Workload**: `isaac_ros_pose_estimation` + `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-visual_slam:latest` (build_only 934s)
**Workload**: `isaac_ros_visual_slam`
Patches applied:
- `PATCH_EXTRA_APT="libboost-thread-dev"` — visual_slam uses `find_package(Boost COMPONENTS thread REQUIRED)`; the base only had `libboost-python1.74.0`.

#### `fish-r2b-ess:latest` (build_only 304s)
**Workload**: `isaac_ros_dnn_stereo_depth`
Patches applied:
- `patch_pre_build`:
  - sed `VPI_BACKEND_XAVIER` → `VPI_BACKEND_ORIN` in gxf_isaac_sgm
  - `COLCON_IGNORE` all `*_models_install/` dirs (`isaac_ros_ess_models_install` requires `ISAAC_ROS_WS` env + network for downloading DNN weights)

#### `fish-r2b-dope:latest` (build_only 2004s)
**Workload**: `isaac_ros_pose_estimation` + `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-segformer:latest` (build_only 1877s)
**Workload**: `isaac_ros_image_segmentation` + `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-unet:latest` (build_only 1924s)
**Workload**: `isaac_ros_image_segmentation` + `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-triton:latest` (build_only 1957s)
**Workload**: `isaac_ros_dnn_inference` + `isaac_ros_pose_estimation`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-tensor_rt:latest` (build_only 1950s)
**Workload**: `isaac_ros_dnn_inference` + `isaac_ros_pose_estimation`
Patches applied: same DNN template as `detectnet`.

#### `fish-r2b-pynitros:latest` (build_only 805s)
**Workload**: `isaac_ros_dnn_inference` + `isaac_ros_image_segmentation`
Patches applied: DNN template + extra Boost
- `PATCH_EXTRA_APT="libboost-dev libboost-system-dev libboost-filesystem-dev libboost-thread-dev"`
  (`isaac_ros_nitros_bridge_ros2` — pulled in transitively — includes `<boost/uuid/uuid.hpp>` and fish-r2b-tensorrt-base only had `libboost-python1.74.0`, no dev headers)
- DNN template patch_pre_build (FindTENSORRT.cmake regex + COLCON_IGNORE models_install).

#### `fish-r2b-nitros_bridge:latest` (build_only 461s)
**Workload**: implicit (`isaac_ros_nitros_bridge` is part of `isaac_ros_nitros` already in base, plus colcon target `isaac_ros_nitros_bridge_benchmark`)
Patches applied:
- `PATCH_EXTRA_APT="libboost-dev libboost-system-dev libboost-filesystem-dev"` — for `<boost/uuid/uuid.hpp>`.

#### `fish-r2b-segment_anything:latest` (build_only 1006s)
**Workload**: `isaac_ros_image_segmentation` + `isaac_ros_dnn_inference`
Patches applied: same DNN template as `detectnet`.

---

## Pattern catalog (cross-cutting)

For each pattern that appears in multiple benchmark patches, the canonical fix:

### Pattern A — TensorRT 10 `FindTENSORRT.cmake` version regex
Used by every DNN-based benchmark. Code:
```bash
patch_pre_build() {
    FT_SRC=/root/ros_ws/src/isaac_ros_common/isaac_ros_common/cmake/modules/FindTENSORRT.cmake
    FT_INST=/root/ros_ws/install/isaac_ros_common/share/isaac_ros_common/cmake/modules/FindTENSORRT.cmake
    for ft in "$FT_SRC" "$FT_INST"; do
        if [ -f "$ft" ] && ! grep -q TRT_MAJOR_ENTERPRISE "$ft"; then
            sed -i '
              s/read_version(NV_TENSORRT_MAJOR/read_version(TRT_MAJOR_ENTERPRISE/;
              s/read_version(NV_TENSORRT_MINOR/read_version(TRT_MINOR_ENTERPRISE/;
              s/read_version(NV_TENSORRT_PATCH/read_version(TRT_PATCH_ENTERPRISE/;
              s/\${NV_TENSORRT_MAJOR}/${TRT_MAJOR_ENTERPRISE}/g;
              s/\${NV_TENSORRT_MINOR}/${TRT_MINOR_ENTERPRISE}/g;
              s/\${NV_TENSORRT_PATCH}/${TRT_PATCH_ENTERPRISE}/g
            ' "$ft"
        fi
    done
}
```
**Must patch BOTH src and install copies** — colcon rebuilds isaac_ros_common which overwrites the install file from src.

### Pattern B — `VPI_BACKEND_XAVIER` → `ORIN`
Used by any benchmark that pulls in `gxf_isaac_sgm` transitively (bi3d, bi3d_freespace, stereo_image_proc, ess, foundationpose, nvblox):
```bash
sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
[ -d "$sgm_dir" ] && sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
    "$sgm_dir/gxf/gems/vpi/constants.cpp" \
    "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp"
```

### Pattern C — `COLCON_IGNORE *_models_install`
Used by every DNN benchmark (these dirs run `install_isaac_ros_asset()` which needs `ISAAC_ROS_WS` env + network to download pretrained DNN weights — not needed for build_only sweep):
```bash
find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \;
```

### Pattern D — Boost dev headers from apt
Two flavors:
- `libboost-thread-dev` for benchmarks doing `find_package(Boost COMPONENTS thread REQUIRED)` (visual_slam, nvblox).
- `libboost-dev libboost-system-dev libboost-filesystem-dev` for benchmarks reaching `<boost/uuid/uuid.hpp>` via `isaac_ros_nitros_bridge_ros2` (nitros_bridge, pynitros).

---

## Deferred benchmarks (script still in place — re-run on better host)

### `isaac_ros_h264_decoder` / `_h264_encoder` — `arm_only`
**Reason**: `isaac_ros_compression/gxf/codec/CMakeLists.txt` hard-codes IMPORTED SHARED targets at absolute paths in `/usr/lib/x86_64-linux-gnu/`:
`libcuvidv4l2.so`, `libnvv4l2.so`, `libnvbufsurface.so`, `libnvbuf_fdmap.so`, `libnvbufsurftransform.so` — Jetson L4T / DeepStream multimedia stack, not available on x86 outside NVIDIA's proprietary base images.

**Patch left in place** (`docker/_benchmark_patches/isaac_ros_h264_decoder.sh`): `patch_pre_build` creates empty stub `.so` files at the expected paths via `g++ -shared -fPIC`. CMake's `--allow-shlib-undefined` accepts the stubs, link step passes, build_only succeeds — but the resulting binary won't decode H.264 at runtime without the real NVIDIA libs.

**On ARM/Jetson**: drop the stub patch; the real libs are there natively.

### `isaac_ros_nvblox` — CMake 3.27 `FILE_SET HEADERS` strict install(EXPORT) check
**Reason**: `nvblox_core` (git submodule) embeds `stdgpu` (3rd-party GPU container lib). stdgpu declares `FILE_SET HEADERS` on its target. nvblox's `install(TARGETS ... stdgpu EXPORT nvbloxTargets ...)` doesn't re-export those file sets. CMake ≥ 3.27 rejects this with `install TARGETS target stdgpu is exported but not all of its interface file sets are installed`.

**16 iteration attempts captured in journal**, full patch in place. Tactic still being tested at the time of writing: pin CMake to 3.25.0 via pip in `patch_pre_build` (3.25 predates the strict check).

### `isaac_ros_foundationpose` — package.xml omissions cascade
**Reason**: foundationpose's `package.xml` is missing 3 build-time `<depend>` declarations:
- `isaac_ros_nitros_image_type` (used in foundationpose_node.cpp)
- `isaac_ros_nitros_tensor_list_type` (used in foundationpose_node.cpp)
- `isaac_ros_managed_nitros` (used in foundationpose_selector_node.cpp, declared only as `<exec_depend>` which doesn't add the include path at build time)

**Patch applied**: sed-edit package.xml to remove the redundant exec_depend and add full depends, plus DNN template patches + assimp apt + VPI_BACKEND_XAVIER sed. Still iterating at the time of writing.
