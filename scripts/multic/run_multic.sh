#!/bin/bash
# One-off multi-container stereo run
SCRATCH=/tmp/claude-1000/-home-tue037807/bdd5870c-e89f-4475-8196-f1171c656e77/scratchpad
DEST=/tmp/multic_test
rm -rf "$DEST" && mkdir -p "$DEST"

timeout 1800 docker run --rm --gpus all --privileged --net host \
  -v /home/tue037807/fish_interfere:/host/fish_src:ro \
  -v /home/tue037807/isaac_ros_assets:/host/isaac_assets:ro \
  -v /home/tue037807/r2bdataset2023_v3:/host/r2bdataset:ro \
  -v /home/tue037807/isaac_ros_assets/apt_cache:/host/apt_cache:ro \
  -v "$SCRATCH/multic/isaac_ros_disparity_node.py:/host/multic_launch.py:ro" \
  -v "$DEST:/root/fish_traces" \
  -v /home/tue037807/trt_cache_v3:/root/.cache \
  -e ISAAC_ROS_ACCEPT_EULA=1 \
  -e ISAAC_ROS_WS=/root/ros_ws \
  -e FISH_ENABLED=1 \
  -e FISH_CUDA_EVENT_TRACE=1 \
  -e FISH_NSYS_DRAIN=15 \
  fish-r2b-stereo_image_proc:latest \
  bash -lc '
    set -e
    export PYTHONUNBUFFERED=1
    rm -rf /root/fish_interfere
    cp -rT /host/fish_src /root/fish_interfere
    bash /root/fish_interfere/scripts/setup_fish.sh --yes >/tmp/fish_install.log 2>&1 || { echo INSTALL_FAILED; tail -30 /tmp/fish_install.log; exit 90; }
    source /opt/ros/humble/setup.bash
    source /root/trace_overlay_ws/install/setup.bash
    export PATH=/opt/ros/humble/fish/bin:$PATH
    export PYTHONPATH=/opt/ros/humble/fish/python:$PYTHONPATH
    export FISH_ENABLED=1

    cp /host/multic_launch.py /root/ros_ws/src/isaac_ros_benchmark/benchmarks/isaac_ros_stereo_image_proc_benchmark/scripts/isaac_ros_disparity_node.py

    sed -i "s|^command = .*|command = sleep 60|" /opt/ros/humble/fish/fish_settings.ini

    mkdir -p /workspaces && ln -sfn /root/ros_ws /workspaces/isaac_ros-dev

    DEST_DS=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
    mkdir -p $DEST_DS
    for bag in /host/r2bdataset/*; do [ -d "$bag" ] && ln -sfn "$bag" "$DEST_DS/$(basename "$bag")" 2>/dev/null; done
    mkdir -p /tmp/cp_extract

    set +e
    launch_test /root/ros_ws/src/isaac_ros_benchmark/benchmarks/isaac_ros_stereo_image_proc_benchmark/scripts/isaac_ros_disparity_node.py
    echo "launch_test rc=$?"
    cp /tmp/r2b-log-*.json /root/fish_traces/ 2>/dev/null
    ls -l /root/fish_traces/ | head
  '
echo "DOCKER_RC=$?"
