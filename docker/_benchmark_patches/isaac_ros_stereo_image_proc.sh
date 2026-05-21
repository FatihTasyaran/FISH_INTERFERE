# Patch for isaac_ros_stereo_image_proc
# Issue: gxf_isaac_sgm references VPI_BACKEND_XAVIER which was removed in VPI 4.
# The Xavier backend was Jetson-Xavier-specific; the modern Orin backend is the
# successor. Substitute VPI_BACKEND_XAVIER -> VPI_BACKEND_ORIN so the package
# compiles on x86 + VPI 4. The runtime check (vpi_backends == VPI_BACKEND_*)
# is a Jetson-only dispatch path that won't fire on x86 (where we use CUDA),
# so the symbol identity does not matter — it just needs to exist.

patch_pre_build() {
  local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
  if [ -d "$sgm_dir" ]; then
    sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
      "$sgm_dir/gxf/gems/vpi/constants.cpp" \
      "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" || true
  fi
}
