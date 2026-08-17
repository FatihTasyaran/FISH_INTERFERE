#!/bin/bash
# Autoware logging_simulator on -fishwait5, "container_install" route:
# fresh container from the (unchanged) image, `docker cp` the CURRENT repo in,
# re-source install_fish.sh (regenerates ros2 wrapper + copies python/fish +
# fish_settings.ini; does NOT touch the tracepoint overlay), then launch.
#
# Exercises the 2026-08-16 changes without an image rebuild:
#   1. ros2 wrapper RMW guard  — RMW_IMPLEMENTATION is deliberately NOT
#      exported here; it must come from fish_settings.ini [ros].
#   2. snapshot/env.txt        — daemon env + DDS sysctls per session.
#   3. fishlog/launch_full.log — full launch stdout/stderr copied out of /tmp.
set -e

DEST=$HOME/fish_traces
mkdir -p "$DEST"
IMG=autoware-dev-trt-a1000-fishwait5:latest
FISH_SRC=/home/tue037807/fish_interfere
NAME=autoware-fishwait5-run-r11
docker image inspect $IMG >/dev/null 2>&1 || { echo "[run] $IMG not built"; exit 2; }
docker rm -f $NAME 2>/dev/null || true

# Bag ≈30 s @1.0x + ~2 min boot + ~30 s FISH stop + nsys drain. r3 needed
# ~4.5 min end-to-end; give 7 so the outer timeout never truncates the ls.
LAUNCH_TIMEOUT=1200   # outer guard only; the inner `timeout N ros2 launch` bounds the run

# Start detached so we can docker cp before the launch shell runs.
docker run -d --gpus all --privileged --net host --shm-size=2g \
    --name $NAME \
    -v "$DEST:/root/fish_traces" \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -e DISPLAY=$DISPLAY \
    -e FISH_ENABLED=1 \
    -e FISH_CUDA_EVENT_TRACE=1 \
    -e FISH_NSYS_DRAIN=15 \
    $IMG bash -c 'sleep 3600' >/dev/null
trap "docker rm -f $NAME >/dev/null 2>&1 || true" EXIT

echo "[run] copying current fish_interfere into container"
docker exec $NAME rm -rf /root/fish_interfere
docker cp "$FISH_SRC" $NAME:/root/fish_interfere

timeout $LAUNCH_TIMEOUT docker exec $NAME bash -lc '
    set -e
    export PYTHONUNBUFFERED=1
    source /opt/ros/humble/setup.bash
    source /opt/autoware/setup.bash
    source /root/trace_overlay_ws/install/setup.bash

    echo "[run] re-installing FISH from /root/fish_interfere (wrapper + python + ini only)"
    cd /root/fish_interfere
    FISH_SETUP_PARENT=1 source scripts/install_fish.sh 2>&1 | tail -5

    export PATH=/opt/ros/humble/fish/bin:$PATH
    export PYTHONPATH=/opt/ros/humble/fish/python:$PYTHONPATH
    export FISH_ENABLED=1
    unset RMW_IMPLEMENTATION   # guard test: must be resolved from ini

    INI=/opt/ros/humble/fish/fish_settings.ini
    sed -i "s|^rmw_implementation *=.*|rmw_implementation = rmw_cyclonedds_cpp|" $INI
    sed -i "s|^cyclonedds_uri *=.*|cyclonedds_uri = |" $INI
    sed -i "s|^per_instance *=.*|per_instance = true|" $INI
    unset CYCLONEDDS_URI       # guard test: must be resolved from ini
    ls -la /opt/ros/humble/fish/cyclonedds_autoware.xml
    sed -i "s|ros2 bag play ~/autoware_map/sample-rosbag -r 0.2|ros2 bag play ~/autoware_map/sample-rosbag -r 0.5|" $INI || true
    echo "[run] ini:"; grep -nE "^(rmw_implementation|cyclonedds_uri|command|per_instance) *=" $INI

    grep -q "FISH: intra-process Waitable registration" \
      /root/trace_overlay_ws/src/rclcpp/src/rclcpp/intra_process_manager.cpp \
      && echo "[run] intra-proc patch confirmed in image" \
      || echo "[run] WARN intra-proc patch NOT in image source"
    grep -c "RMW guard" /opt/ros/humble/fish/bin/ros2 | xargs echo "[run] wrapper has RMW guard lines:"

    set +e
    timeout 420 ros2 launch autoware_launch logging_simulator.launch.xml \
        map_path:=/root/autoware_map/sample-map-rosbag \
        vehicle_model:=sample_vehicle \
        sensor_model:=sample_sensor_kit
    rc=$?
    echo "[AW-fishwait5-v2] ros2 launch rc=$rc"
    sleep 25
    ls -la /root/fish_traces/ | tail -4
'
echo "[AW-fishwait5-v2] exec rc=$?"
