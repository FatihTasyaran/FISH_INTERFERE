# Patch for isaac_ros_ess
# Two issues:
# 1. gxf_isaac_sgm (transitive dep via isaac_ros_image_pipeline) uses
#    VPI_BACKEND_XAVIER, removed in VPI 4 → sed-replace with VPI_BACKEND_ORIN.
# 2. isaac_ros_ess_models_install requires ISAAC_ROS_WS + network →
#    COLCON_IGNORE the models_install pkg (weights are runtime concern).

patch_pre_build() {
    # Fix VPI_BACKEND_XAVIER in gxf_isaac_sgm (cross-cutting)
    local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
    if [ -d "$sgm_dir" ]; then
        sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
            "$sgm_dir/gxf/gems/vpi/constants.cpp" \
            "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" || true
        echo "[patch:ess] sed VPI_BACKEND_XAVIER -> VPI_BACKEND_ORIN in gxf_isaac_sgm"
    fi
    # COLCON_IGNORE models_install (build-only, no need to download weights)
    find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \; 2>/dev/null
    echo "[patch:ess] COLCON_IGNORE all *_models_install dirs"
}
