# Patch for isaac_ros_bi3d
# Issue: gxf_isaac_sgm (built transitively via isaac_ros_image_pipeline pulled in
# by setup_isaac_workspace.sh) references VPI_BACKEND_XAVIER which was removed
# in VPI 4. The Xavier backend was Jetson-Xavier-specific; the modern Orin
# backend is the successor. Substitute VPI_BACKEND_XAVIER -> VPI_BACKEND_ORIN
# so the package compiles on x86 + VPI 4. The runtime check
# (vpi_backends == VPI_BACKEND_*) is a Jetson-only dispatch path that won't
# fire on x86 (where we use CUDA), so the symbol identity does not matter —
# it just needs to exist.
#
# Same fix as isaac_ros_stereo_image_proc; gxf_isaac_sgm is not pre-built in
# fish-r2b-base, so any benchmark whose colcon target graph reaches it must
# carry this sed.
#
# Additional issue: gxf_isaac_bi3d depends on TensorRT (NvInferVersion.h +
# nvinfer/nvinfer_plugin/nvonnxparser libs) via isaac_ros_common's
# FindTENSORRT.cmake. TensorRT is NOT pre-installed in fish-r2b-base.
# Use the minimal set instead of the `tensorrt-dev` metapackage, which pulls
# dispatch/lean/vc-plugin/win-builder-resource (~10GB total). The Find module
# only needs NvInferVersion.h + nvinfer / nvinfer_plugin / nvonnxparser libs.

PATCH_EXTRA_APT="libnvinfer-headers-dev libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev"

patch_pre_build() {
  local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
  if [ -d "$sgm_dir" ]; then
    sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
      "$sgm_dir/gxf/gems/vpi/constants.cpp" \
      "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" || true
  fi
}
