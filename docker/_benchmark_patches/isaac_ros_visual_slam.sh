# Patch for isaac_ros_visual_slam
# Issue: visual_slam CMakeLists does find_package(Boost COMPONENTS thread REQUIRED).
# fish-r2b-tensorrt-base has libboost-dev but not libboost-thread-dev.

PATCH_EXTRA_APT="libboost-thread-dev"
