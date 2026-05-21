# Patch for isaac_ros_bi3d_freespace
#
# Two compile-time issues in this benchmark's transitive dep set:
#
# 1. gxf_isaac_sgm (from isaac_ros_image_pipeline, transitively pulled in by
#    bi3d_freespace_benchmark) references VPI_BACKEND_XAVIER — a symbol
#    removed in VPI 4. Replace with VPI_BACKEND_ORIN; the runtime check is a
#    Jetson-only dispatch path that does not fire on x86 (CUDA backend used).
#    Mirrors docker/_benchmark_patches/isaac_ros_stereo_image_proc.sh.
#
# 2. gxf_isaac_bi3d depends on TensorRT (FindTENSORRT.cmake -> NvInferVersion.h)
#    which is not installed in fish-r2b-base. Install the libnvinfer dev
#    packages (the minimum needed by gxf_isaac_bi3d: nvinfer + nvinfer_plugin
#    + nvonnxparser) from the NVIDIA CUDA apt repo (already enabled in base).
#    If the base image already supplies TensorRT (e.g. fish-r2b-tensorrt-base),
#    the install is skipped — keeps the build fast when the manager swaps in
#    a TensorRT-augmented base.

patch_pre_build() {
  local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
  if [ -d "$sgm_dir" ]; then
    sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
      "$sgm_dir/gxf/gems/vpi/constants.cpp" \
      "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" || true
  fi

  if [ ! -f /usr/include/x86_64-linux-gnu/NvInferVersion.h ] && \
     [ ! -f /usr/include/NvInferVersion.h ]; then
    apt-get update -qq && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev
  fi
}
