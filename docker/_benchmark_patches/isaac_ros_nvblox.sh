# Patch for isaac_ros_nvblox
# Anticipated: nvblox uses Boost components (thread, system) → apt deps.
# Also may need VPI_BACKEND_XAVIER fix if it pulls in image_pipeline.

PATCH_EXTRA_APT="libboost-thread-dev libboost-system-dev ros-humble-rviz-default-plugins libgflags-dev libgoogle-glog-dev libbenchmark-dev libsqlite3-dev"

patch_pre_build() {
    # Downgrade CMake to 3.25.x for nvblox build only. The stdgpu FILE_SET HEADERS
    # strict install(EXPORT) check was added in CMake 3.27. 3.25 is permissive
    # and the standard CMake ABI for that era. pip install shadows base's 3.27.9.
    python3 -m pip install --quiet --no-cache-dir cmake==3.25.0 2>&1 | tail -2 || true
    hash -r
    echo "[patch:nvblox] cmake pinned to $(cmake --version | head -1)"
    # CMake 3.25 doesn't trigger the FILE_SET HEADERS check → skip the
    # install(TARGETS) removal sed. Return early before that destructive patch.
    USE_NUCLEAR_INSTALL_REMOVAL=0

    # Apply VPI_BACKEND_XAVIER->ORIN fix preemptively to gxf_isaac_sgm if present
    local sgm_dir=/root/ros_ws/src/isaac_ros_image_pipeline/isaac_ros_gxf_extensions/gxf_isaac_sgm
    if [ -d "$sgm_dir" ]; then
        sed -i 's/VPI_BACKEND_XAVIER/VPI_BACKEND_ORIN/g' \
            "$sgm_dir/gxf/gems/vpi/constants.cpp" \
            "$sgm_dir/gxf/extensions/sgm/sgm_disparity.cpp" 2>/dev/null || true
    fi
    find /root/ros_ws/src -type d -name "*_models_install" -exec touch {}/COLCON_IGNORE \; 2>/dev/null
    # nvblox_rviz_plugin is for visualization only — not needed for benchmark
    [ -d /root/ros_ws/src/isaac_ros_nvblox/nvblox_rviz_plugin ] && \
        touch /root/ros_ws/src/isaac_ros_nvblox/nvblox_rviz_plugin/COLCON_IGNORE
    # nvblox_core is a git submodule — `git clone --depth 1` skipped it.
    # Init it inside the existing checkout.
    if [ -d /root/ros_ws/src/isaac_ros_nvblox ] && \
       [ ! -f /root/ros_ws/src/isaac_ros_nvblox/nvblox_ros/nvblox_core/CMakeLists.txt ]; then
        git -C /root/ros_ws/src/isaac_ros_nvblox submodule update --init --recursive 2>&1 | tail -5
    fi
    # CMake 3.27 strict-check: install(TARGETS ... stdgpu EXPORT ...) fails
    # because stdgpu declares FILE_SET HEADERS that aren't re-exported.
    # We're a build_only sweep — no downstream find_package(nvblox_ros) needed.
    # Simplest fix: drop the `EXPORT ${PROJECT_NAME}Targets` line from the
    # offending install commands so nothing gets exported, sidestepping the
    # strict check entirely. Library files still install to lib/.
    # stdgpu declares FIVE FILE_SETs on its target (stdgpu_headers,
    # stdgpu_header_implementations, stdgpu_config_header, stdgpu_backend_headers,
    # stdgpu_backend_header_implementations). The parent's install(TARGETS ...
    # stdgpu EXPORT ...) must explicitly include EACH OF THEM with a DESTINATION,
    # otherwise CMake errors with "all of its interface file sets are installed".
    # Inject all five before the closing ')'.
    for f in /root/ros_ws/src/isaac_ros_nvblox/nvblox_ros/CMakeLists.txt \
             /root/ros_ws/src/isaac_ros_nvblox/nvblox_ros/nvblox_core/nvblox/CMakeLists.txt; do
        [ -f "$f" ] || continue
        if grep -q "stdgpu" "$f" && ! grep -q "FILE_SET stdgpu_headers DESTINATION" "$f"; then
            python3 -c "
import re
with open('$f') as fin: text = fin.read()
INSERT = '''    FILE_SET stdgpu_headers DESTINATION include
    FILE_SET stdgpu_header_implementations DESTINATION include
    FILE_SET stdgpu_config_header DESTINATION include
    FILE_SET stdgpu_backend_headers DESTINATION include
    FILE_SET stdgpu_backend_header_implementations DESTINATION include
'''
def add_filesets(m):
    inner = m.group(0)
    return inner.replace(')', INSERT + ')', 1) if inner.rstrip().endswith(')') else inner
text2 = re.sub(r'install\([^)]*\bstdgpu\b[^)]*\)', add_filesets, text, flags=re.DOTALL)
with open('$f', 'w') as fout: fout.write(text2)
"
            echo '[patch:nvblox] added 5 stdgpu FILE_SETs to install in '"$(basename "$f")"
        fi
    done
    # Skip the legacy nuclear removal branch — the FILE_SET HEADERS addition is the canonical fix
    return 0
    # OLD UNUSED CODE BELOW (kept for journal trail)
    if [ "${USE_NUCLEAR_INSTALL_REMOVAL:-1}" = "0" ]; then
        echo "[patch:nvblox] CMake 3.25 in use — skipping nuclear install(TARGETS) removal"
        return 0
    fi
    for f in /root/ros_ws/src/isaac_ros_nvblox/nvblox_ros/CMakeLists.txt \
             /root/ros_ws/src/isaac_ros_nvblox/nvblox_ros/nvblox_core/nvblox/CMakeLists.txt; do
        [ -f "$f" ] || continue
        if grep -q "stdgpu" "$f"; then
            # Nuclear approach: comment out the entire install(TARGETS ... stdgpu ...)
            # block + any install(EXPORT ...) + ament_export_targets(...). stdgpu
            # is still statically linked into nvblox_lib, just not installed/exported.
            # Build_only sweep doesn't need downstream find_package(nvblox).
            awk '
              BEGIN{depth=0; in_offending=0}
              /^[[:space:]]*install[[:space:]]*\(/ && (depth==0) {
                  buf = $0; depth = gsub(/\(/, "&", $0) - gsub(/\)/, "&", $0);
                  in_offending = ($0 ~ /TARGETS/ || $0 ~ /EXPORT/) ? 1 : 0;
                  if (in_offending) { print "# DISABLED: "$0 } else { print }
                  if (depth == 0) { in_offending = 0; depth = 0 }
                  next
              }
              depth > 0 {
                  depth += gsub(/\(/, "&", $0) - gsub(/\)/, "&", $0);
                  if (in_offending) print "# DISABLED: "$0; else print;
                  if (depth <= 0) { depth = 0; in_offending = 0 }
                  next
              }
              /^[[:space:]]*ament_export_targets[[:space:]]*\(/ {
                  depth = gsub(/\(/, "&", $0) - gsub(/\)/, "&", $0);
                  in_offending = 1;
                  print "# DISABLED: "$0;
                  if (depth == 0) { in_offending = 0 }
                  next
              }
              {print}
            ' "$f" > "$f.new"
            # Only check stdgpu install commands (preserve other install() calls)
            # Reset: just comment lines that are part of install(TARGETS ... stdgpu ...) block
            python3 -c "
import re, sys
with open('$f') as fin: text = fin.read()
def disable_block(m):
    return '\n'.join('# DISABLED: ' + ln for ln in m.group(0).split('\n'))
# Match install(TARGETS ...stdgpu... ) — balanced parens, multi-line
text2 = re.sub(r'install\([^)]*\bstdgpu\b[^)]*\)', disable_block, text, flags=re.DOTALL)
# Match install(EXPORT ...Targets ...) — balanced parens
text2 = re.sub(r'install\([^)]*\bEXPORT\b[^)]*Targets[^)]*\)', disable_block, text2, flags=re.DOTALL)
# Match ament_export_targets(...)
text2 = re.sub(r'ament_export_targets\s*\([^)]*\)', disable_block, text2, flags=re.DOTALL)
with open('$f', 'w') as fout: fout.write(text2)
" && rm -f "$f.new"
            echo "[patch:nvblox] python-regex DISABLED stdgpu install + EXPORT in $(basename "$f")"
        fi
    done
}
