#!/bin/bash
# AWSIM (Unity, Vulkan) rendered by Mesa lavapipe (CPU) so the GPU keeps its VRAM for
# Autoware; RGL lidar still runs on CUDA. Host lavapipe .so + LLVM mounted read-only.
BIN=${1:-Lightweight}   # Lightweight | Full
docker rm -f awsim-sim >/dev/null 2>&1
docker run -d --rm --name awsim-sim --privileged --runtime=nvidia --gpus all --device /dev/dri --net host --shm-size=1g \
 -e DISPLAY=:1 -e XAUTHORITY=/root/.Xauthority -v $HOME/.Xauthority:/root/.Xauthority:ro -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
 -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all -e XDG_RUNTIME_DIR=/tmp/runtime-root \
 -v /usr/lib/x86_64-linux-gnu/libvulkan_lvp.so:/opt/lvp/libvulkan_lvp.so:ro -v /lib/x86_64-linux-gnu/libLLVM-15.so.1:/opt/lvp/libLLVM-15.so.1:ro -v /usr/share/vulkan/icd.d/lvp_icd.x86_64.json:/opt/lvp/lvp_icd.json:ro -e LD_LIBRARY_PATH=/opt/lvp -e LP_NUM_THREADS=${LP_NUM_THREADS:-6} \
 -v $(cd $(dirname "$0") && pwd)/awsim-min-config.json:/root/awsim-min-config.json:ro -v $HOME/AWSIM-Demo:/root/AWSIM-Demo-Full:ro \
 -e ROS_DOMAIN_ID=0 -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
 autoware-awsim-fish:latest bash -c 'mkdir -p /tmp/runtime-root; sed "s|/usr/lib/x86_64-linux-gnu/libvulkan_lvp.so|/opt/lvp/libvulkan_lvp.so|" /opt/lvp/lvp_icd.json > /tmp/lvp.json; export VK_ICD_FILENAMES=/tmp/lvp.json; if [ "'"$BIN"'" = Full ]; then cd /root/AWSIM-Demo-Full && ./AWSIM-Demo.x86_64 --json_path /root/awsim-min-config.json -batchmode -screen-width 320 -screen-height 240 -screen-quality Fastest > /root/awsim.log 2>&1; else cd /root/AWSIM-Demo-Lightweight && ./AWSIM-Demo-Lightweight.x86_64 --json_path /root/awsim-min-config.json -batchmode -screen-width 320 -screen-height 240 -screen-quality Fastest > /root/awsim.log 2>&1; fi'
