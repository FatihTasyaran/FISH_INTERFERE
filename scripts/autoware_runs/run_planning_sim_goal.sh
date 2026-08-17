#!/bin/bash
# r18: Autoware planning_simulator (Autoware's own vehicle plant + dummy perception,
# no sensors, no AWSIM) with initial pose + goal + autonomous engage, on -fishwait5 +
# CURRENT fish_interfere. Purpose: activate Planning→Control and close the loop
# (control_cmd → simple_planning_simulator → localization) so FISH can measure them.
set -e
DEST=$HOME/fish_traces; mkdir -p "$DEST"
IMG=autoware-dev-trt-a1000-fishwait5:latest
FISH_SRC=/home/tue037807/fish_interfere
NAME=autoware-fishwait5-run-r18
T=$(cd $(dirname "$0") && pwd)
docker rm -f $NAME 2>/dev/null || true
docker run -d --gpus all --privileged --net host --shm-size=2g --name $NAME \
    -v "$DEST:/root/fish_traces" \
    -e FISH_ENABLED=1 -e FISH_CUDA_EVENT_TRACE=1 -e FISH_NSYS_DRAIN=15 \
    $IMG bash -c 'sleep 7200' >/dev/null
trap "docker rm -f $NAME >/dev/null 2>&1 || true" EXIT
docker exec $NAME rm -rf /root/fish_interfere
docker cp "$FISH_SRC" $NAME:/root/fish_interfere
docker cp $T/psim_init.txt $NAME:/root/psim_init.txt
docker cp $T/psim_goals.txt $NAME:/root/psim_goals.txt

docker exec -d $NAME bash -lc '
  source /opt/ros/humble/setup.bash; source /opt/autoware/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  LOG=/root/goal_injector.log; exec >$LOG 2>&1
  echo "[goal] waiting for /api/routing/set_route_points"
  for i in $(seq 1 100); do ros2 service list 2>/dev/null | grep -q /api/routing/set_route_points && break; sleep 5; done
  sleep 60; date
  for i in 1 2 3 4 5 6; do
    st=$(timeout 10 ros2 topic echo --once /api/localization/initialization_state 2>/dev/null | grep "^state" | head -1); echo "[goal] init state: $st"; [[ "$st" == *"3"* ]] && break
    timeout 60 ros2 service call /api/localization/initialize autoware_adapi_v1_msgs/srv/InitializeLocalization "$(cat /root/psim_init.txt)" 2>&1 | tail -1 | cut -c1-160
    sleep 10
  done
  timeout 6 ros2 topic echo --once /localization/kinematic_state | grep -A3 "position:" | head -4 | tr "\n" " " | xargs echo "[goal] ekf"
  ok=0
  while read -r req; do
    out=$(timeout 30 ros2 service call /api/routing/set_route_points autoware_adapi_v1_msgs/srv/SetRoutePoints "$req" 2>&1 | tail -1); echo "$out" | cut -c60-200
    if echo "$out" | grep -q "success=True"; then ok=1; echo "[goal] ROUTE OK: $req" | cut -c1-140; break; fi; sleep 3
  done < /root/psim_goals.txt
  echo "[goal] route ok=$ok"; date
  for i in $(seq 1 12); do
    st=$(timeout 10 ros2 topic echo --once /api/routing/state 2>/dev/null | grep "^state" | head -1); echo "[goal] routing $st"
    timeout 6 ros2 topic echo --once /planning/scenario_planning/trajectory --no-arr 2>/dev/null | grep points | xargs echo "[goal] traj"
    out=$(timeout 30 ros2 service call /api/operation_mode/change_to_autonomous autoware_adapi_v1_msgs/srv/ChangeOperationMode "{}" 2>&1 | tail -1); echo "$out" | cut -c60-200
    echo "$out" | grep -q "success=True" && { echo "[goal] AUTONOMOUS engaged"; date; break; }
    sleep 10
  done
  for i in $(seq 1 30); do
    timeout 8 ros2 topic echo --once /api/operation_mode/state 2>/dev/null | grep -E "^mode" | tr "\n" " "
    timeout 6 ros2 topic echo --once /localization/kinematic_state 2>/dev/null | grep -A2 "linear:" | grep -m1 " x:" | xargs echo "[goal] speed"
    sleep 5
  done
  timeout 8 ros2 topic echo --once /api/routing/state | grep "^state" | xargs echo "[goal] final routing"
'

timeout 1200 docker exec $NAME bash -lc '
    set -e; export PYTHONUNBUFFERED=1
    source /opt/ros/humble/setup.bash; source /opt/autoware/setup.bash; source /root/trace_overlay_ws/install/setup.bash
    cd /root/fish_interfere; FISH_SETUP_PARENT=1 source scripts/install_fish.sh 2>&1 | tail -3
    export PATH=/opt/ros/humble/fish/bin:$PATH PYTHONPATH=/opt/ros/humble/fish/python:$PYTHONPATH FISH_ENABLED=1
    unset RMW_IMPLEMENTATION CYCLONEDDS_URI
    INI=/opt/ros/humble/fish/fish_settings.ini
    sed -i "s|^rmw_implementation *=.*|rmw_implementation = rmw_cyclonedds_cpp|; s|^cyclonedds_uri *=.*|cyclonedds_uri = |; s|^per_instance *=.*|per_instance = true|; s|^replay_rosbag *=.*|replay_rosbag = false|" $INI
    echo "[run] ini:"; grep -nE "^(rmw_implementation|cyclonedds_uri|per_instance|replay_rosbag) *=" $INI
    set +e
    timeout 660 ros2 launch autoware_launch planning_simulator.launch.xml \
        map_path:=/root/autoware_map/sample-map-rosbag vehicle_model:=sample_vehicle sensor_model:=sample_sensor_kit rviz:=false
    rc=$?; echo "[AW-r18] ros2 launch rc=$rc"
    sleep 30
    cp /root/goal_injector.log $(ls -td /root/fish_traces/fish_2026* | head -1)/fishlog/ 2>/dev/null
    ls -la /root/fish_traces/ | tail -3
'
echo "[AW-r18] exec rc=$?"
