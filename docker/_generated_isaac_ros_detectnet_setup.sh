#!/bin/bash
# Auto-generated workload setup for isaac_ros_detectnet
set -e

bash /tmp/setup_isaac_workspace.sh --gpu --datasets /nonexistent

cd /root/ros_ws/src
git clone --depth 1 --branch release-3.2 https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_object_detection.git || true
git clone --depth 1 --branch release-3.2 https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_dnn_inference.git || true

cd /root/ros_ws
source /opt/ros/humble/setup.bash
rosdep install -i -r --from-paths src --rosdistro humble -y 2>&1 | tail -5 || true

# Real colcon build — target the benchmark package; deps cascade in.
# CMAKE_POLICY_VERSION_MINIMUM=3.30 makes CMake 4.x compatible with cv_bridge's legacy FindBoost.
colcon build --packages-up-to isaac_ros_detectnet_benchmark isaac_ros_benchmark --cmake-args -DCMAKE_POLICY_VERSION_MINIMUM=3.30
