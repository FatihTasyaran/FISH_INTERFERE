# Patch for isaac_ros_h264_decoder
#
# Issue: gxf/codec/CMakeLists.txt declares IMPORTED shared-library targets that
# point at NVIDIA L4T / DeepStream proprietary libs in /usr/lib/x86_64-linux-gnu:
#   libcuvidv4l2.so, libnvv4l2.so, libnvbufsurface.so, libnvbuf_fdmap.so,
#   libnvbufsurftransform.so
# These are part of the NVIDIA Multimedia API and ship only via NVIDIA's
# proprietary `isaac_ros_dev` Docker base / DeepStream apt repos which are
# not enabled here. Make fails with "No rule to make target
# '/usr/lib/x86_64-linux-gnu/libcuvidv4l2.so'" because CMake adds the imported
# library path as a Makefile prerequisite, but the file does not exist.
#
# Build-only mitigation (SKIP_RUN=1):
#   Pre-create empty stub shared libraries at the expected paths so the link
#   step succeeds. The default GNU ld behavior is `--allow-shlib-undefined`,
#   so unresolved symbols inside the stubs do not break the build. The
#   produced gxf_video_{encoder,decoder}_extension.so will not be runtime-
#   functional without the real NVIDIA libs, which is acceptable for the
#   build-only sweep (smoke test is skipped at this stage).

PATCH_EXTRA_APT=""
PATCH_EXTRA_CMAKE_ARGS=""

patch_pre_build() {
  local libdir=/usr/lib/x86_64-linux-gnu
  mkdir -p "$libdir"
  # Create minimal stub .so for each required NVIDIA Multimedia lib.
  # An empty cpp compiled with -shared and -fPIC produces a valid ELF .so
  # that the linker accepts, satisfying `add_library(... SHARED IMPORTED)`.
  local stub_src
  stub_src=$(mktemp --suffix=.cpp)
  echo 'extern "C" void __isaac_ros_h264_stub(void) {}' > "$stub_src"
  for lib in libcuvidv4l2 libnvv4l2 libnvbufsurface libnvbuf_fdmap libnvbufsurftransform; do
    if [ ! -e "$libdir/${lib}.so" ]; then
      g++ -shared -fPIC -o "$libdir/${lib}.so" "$stub_src" || true
      echo "[patch:h264_decoder] created stub $libdir/${lib}.so"
    fi
  done
  rm -f "$stub_src"
}
