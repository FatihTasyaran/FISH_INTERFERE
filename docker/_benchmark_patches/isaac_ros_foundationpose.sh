# Patch for isaac_ros_foundationpose
# Issue 1: TensorRT 10.x — NV_TENSORRT_MAJOR redirects through TRT_MAJOR_ENTERPRISE.
#   Isaac ROS 3.2's FindTENSORRT.cmake reads NV_TENSORRT_MAJOR directly, gets
#   "TRT_MAJOR_ENTERPRISE" (text, not digit) → version=".." → fails ">=10" check.
#   Patch BOTH src/ AND install/ copies of FindTENSORRT.cmake — colcon rebuilds
#   isaac_ros_common which overwrites install/ from src/ if we only patch install/.
# Issue 2: COLCON_IGNORE *_models_install — they download DNN weights, network-only.
# Issue 3: gxf_isaac_foundationpose needs assimp (3D model importer).

PATCH_EXTRA_APT="libassimp-dev"

patch_pre_build() {
    # VPI_BACKEND_XAVIER → ORIN in gxf_isaac_sgm (pulled in via stereo_depth)
    local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
    if [ -d "$sgm_dir" ]; then
        sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
            "$sgm_dir/gxf/gems/vpi/constants.cpp" \
            "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" 2>/dev/null || true
    fi
    # foundationpose's package.xml has THREE missing build deps:
    # - isaac_ros_nitros_image_type (used in foundationpose_node.cpp)
    # - isaac_ros_nitros_tensor_list_type (used in foundationpose_node.cpp)
    # - isaac_ros_managed_nitros (used in foundationpose_selector_node.cpp,
    #   only declared as exec_depend — must be full <depend> for ament_auto
    #   to add it to the build-time include path)
    local FP_PKG=/root/ros_ws/src/isaac_ros_pose_estimation/isaac_ros_foundationpose/package.xml
    if [ -f "$FP_PKG" ] && ! grep -q "<depend>isaac_ros_nitros_image_type" "$FP_PKG"; then
        # Remove the redundant <exec_depend>isaac_ros_managed_nitros first
        sed -i '/<exec_depend>isaac_ros_managed_nitros<\/exec_depend>/d' "$FP_PKG"
        # Then add the 3 missing build+exec deps after camera_info_type
        sed -i '/<depend>isaac_ros_nitros_camera_info_type<\/depend>/a\  <depend>isaac_ros_nitros_image_type</depend>\n  <depend>isaac_ros_nitros_tensor_list_type</depend>\n  <depend>isaac_ros_managed_nitros</depend>' "$FP_PKG"
        echo "[patch:foundationpose] removed redundant exec_depend + added 3 missing nitros deps"
    fi
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
            echo "[patch:foundationpose] patched $ft for TensorRT 10.x"
        fi
    done
    find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \; 2>/dev/null
}
