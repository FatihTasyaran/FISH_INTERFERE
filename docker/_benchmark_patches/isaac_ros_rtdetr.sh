# Patch for isaac_ros_rtdetr
# Issue 1: TensorRT 10.x — NV_TENSORRT_MAJOR redirects through TRT_MAJOR_ENTERPRISE.
#   Isaac ROS 3.2's FindTENSORRT.cmake reads NV_TENSORRT_MAJOR directly, gets
#   "TRT_MAJOR_ENTERPRISE" (text, not digit) → version=".." → fails ">=10" check.
#   Patch BOTH src/ AND install/ copies of FindTENSORRT.cmake — colcon rebuilds
#   isaac_ros_common which overwrites install/ from src/ if we only patch install/.
# Issue 2: COLCON_IGNORE *_models_install — they download DNN weights, network-only.

patch_pre_build() {
    local SRC_FT=/root/ros_ws/src/isaac_ros_common/isaac_ros_common/cmake/modules/FindTENSORRT.cmake
    local INST_FT=/root/ros_ws/install/isaac_ros_common/share/isaac_ros_common/cmake/modules/FindTENSORRT.cmake
    for ft in "$SRC_FT" "$INST_FT"; do
        if [ -f "$ft" ] && ! grep -q TRT_MAJOR_ENTERPRISE "$ft"; then
            sed -i '
              s/read_version(NV_TENSORRT_MAJOR/read_version(TRT_MAJOR_ENTERPRISE/;
              s/read_version(NV_TENSORRT_MINOR/read_version(TRT_MINOR_ENTERPRISE/;
              s/read_version(NV_TENSORRT_PATCH/read_version(TRT_PATCH_ENTERPRISE/;
              s/\${NV_TENSORRT_MAJOR}/${TRT_MAJOR_ENTERPRISE}/g;
              s/\${NV_TENSORRT_MINOR}/${TRT_MINOR_ENTERPRISE}/g;
              s/\${NV_TENSORRT_PATCH}/${TRT_PATCH_ENTERPRISE}/g
            ' "$ft"
            echo "[patch:rtdetr] patched $ft for TensorRT 10.x"
        fi
    done
    find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \; 2>/dev/null
}
