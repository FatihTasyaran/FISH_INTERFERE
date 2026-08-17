#!/bin/bash
# r21 (TEST: awsim_sensor_kit velodyne_top_base_link yaw 1.575→0.0 container-local; AWSIM via lavapipe (CPU Vulkan), GPU only for RGL; FULL AWSIM-Demo nographics, init yaw +0.61 = EulerAngles z 35°; NDT NVTL threshold 2.3→1.5 in-container; init yaw -0.61 = -Unity_y, tight cov; TL empty pub; system_run_mode:=logging_simulation → pose_initializer stop_check off; AWSIM -nographics: no GNSS → explicit-pose init, gnss_enabled:=false; r12 + AWSIM -batchmode -nographics 478 MiB VRAM, goal_calls pre-generated): Autoware e2e_simulator against AWSIM (Unity, separate container awsim-sim),
# on -fishwait5 image + CURRENT fish_interfere (container_install route).
# VRAM budget: AWSIM takes ~3.7 GB of 4 GB → perception without GPU models
# (empty dynamic objects, no traffic-light recognition), rviz off.
set -e
DEST=$HOME/fish_traces; mkdir -p "$DEST"
IMG=autoware-dev-trt-a1000-fishwait5:latest
FISH_SRC=/home/tue037807/fish_interfere
NAME=autoware-fishwait5-run-r21
GOALS=$(dirname "$0")/goal_cands.json
docker ps --format '{{.Names}}' | grep -q '^awsim-sim$' || { echo "[run] awsim-sim not running"; exit 2; }
docker rm -f $NAME 2>/dev/null || true
docker run -d --gpus all --privileged --net host --shm-size=2g --name $NAME \
    -v "$DEST:/root/fish_traces" \
    -v "$HOME/autoware_map/Shinjuku-Map:/root/autoware_map/Shinjuku-Map:ro" \
    -e FISH_ENABLED=1 -e FISH_CUDA_EVENT_TRACE=1 -e FISH_NSYS_DRAIN=15 \
    $IMG bash -c 'sleep 7200' >/dev/null
trap "docker rm -f $NAME >/dev/null 2>&1 || true" EXIT
docker exec $NAME rm -rf /root/fish_interfere
docker cp "$FISH_SRC" $NAME:/root/fish_interfere
docker cp "$GOALS" $NAME:/root/goal_cands.json
docker cp $(dirname "$0")/goal_calls.txt $NAME:/root/goal_calls.txt

# --- goal injector (runs in parallel inside the container) ---
docker exec -d $NAME bash -lc '
  source /opt/ros/humble/setup.bash; source /opt/autoware/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  LOG=/root/goal_injector.log; exec >$LOG 2>&1
  echo "[goal] waiting for /api/routing/set_route_points + localization"
  for i in $(seq 1 100); do ros2 service list 2>/dev/null | grep -q /api/routing/set_route_points && break; sleep 5; done
  ros2 service list | grep -c /api/ | xargs echo "[goal] adapi services:"
  sleep 60   # let FISH stabilise (component list) before we start poking
  date; echo "[goal] localization initialize (GNSS)"
  for i in 1 2 3 4 5 6 7 8; do
    st=$(timeout 10 ros2 topic echo --once /api/localization/initialization_state 2>/dev/null | grep "^state" | head -1)
    echo "[goal] init state: $st"; [[ "$st" == *"3"* ]] && break
    timeout 60 ros2 service call /api/localization/initialize autoware_adapi_v1_msgs/srv/InitializeLocalization "{pose: [{header: {frame_id: map}, pose: {pose: {position: {x: 81381.727, y: 49920.189, z: 41.577}, orientation: {x: 0.0, y: 0.0, z: 0.300770, w: 0.953697}}, covariance: [1.0,0,0,0,0,0, 0,1.0,0,0,0,0, 0,0,0.5,0,0,0, 0,0,0,0.05,0,0, 0,0,0,0,0.05,0, 0,0,0,0,0,0.05]}}]}" | tail -2
    timeout 5 ros2 topic hz /sensing/vehicle_velocity_converter/twist_with_covariance 2>&1 | grep average | head -1 | xargs echo "[goal] stop-check twist hz:"
    sleep 15
  done
  timeout 6 ros2 topic echo --once /localization/kinematic_state | python3 -c "
import sys,math,re; t=sys.stdin.read(); o=re.search(r\"orientation:\\s+x: ([-\\d.e]+)\\s+y: ([-\\d.e]+)\\s+z: ([-\\d.e]+)\\s+w: ([-\\d.e]+)\",t); p=re.search(r\"position:\\s+x: ([-\\d.e]+)\\s+y: ([-\\d.e]+)\",t); print(\"[goal] ekf pos\",p.groups(),\"yaw\",round(2*math.atan2(float(o.group(3)),float(o.group(4))),3))"
  for k in 1 2 3; do timeout 5 ros2 topic echo --once /localization/pose_estimator/nearest_voxel_transformation_likelihood | grep data | xargs echo "[goal] NVTL"; timeout 5 ros2 topic echo --once /localization/pose_estimator/transform_probability | grep data | xargs echo "[goal] TP"; sleep 2; done
  (ros2 topic pub -r 10 /perception/traffic_light_recognition/traffic_signals autoware_perception_msgs/msg/TrafficLightGroupArray "{}" >/dev/null 2>&1 &)
  ok=0
  while read -r req; do
    echo "[goal] set_route_points: $req"
    out=$(timeout 30 ros2 service call /api/routing/set_route_points autoware_adapi_v1_msgs/srv/SetRoutePoints "$req" 2>&1 | tail -3); echo "$out"
    if echo "$out" | grep -q "success=True"; then ok=1; break; fi
    sleep 3
  done < /root/goal_calls.txt
  echo "[goal] route ok=$ok"; date
  for i in $(seq 1 12); do
    st=$(timeout 10 ros2 topic echo --once /api/routing/state 2>/dev/null | grep "^state" | head -1); echo "[goal] routing $st"
    timeout 6 ros2 topic echo --once /planning/scenario_planning/trajectory --no-arr 2>/dev/null | grep points | xargs echo "[goal] traj"
    out=$(timeout 30 ros2 service call /api/operation_mode/change_to_autonomous autoware_adapi_v1_msgs/srv/ChangeOperationMode "{}" 2>&1 | tail -2); echo "$out"
    echo "$out" | grep -q "success=True" && { echo "[goal] AUTONOMOUS engaged"; date; break; }
    sleep 10
  done
  timeout 8 ros2 topic hz /planning/scenario_planning/trajectory 2>&1 | grep average | head -1 | xargs echo "[goal] trajectory hz:"
  for i in $(seq 1 40); do
    timeout 10 ros2 topic echo --once /api/operation_mode/state 2>/dev/null | grep -E "^mode|is_autonomous_mode_available" | tr "\n" " "; echo
    timeout 6 ros2 topic hz /control/command/control_cmd 2>&1 | grep average | head -1
    timeout 6 ros2 topic echo --once /localization/kinematic_state 2>/dev/null | grep -A3 "^  twist:" | grep -m1 " x:" | xargs echo "[goal] speed"
    sleep 5
  done
'

timeout 1500 docker exec $NAME bash -lc '
    set -e; export PYTHONUNBUFFERED=1
    source /opt/ros/humble/setup.bash; source /opt/autoware/setup.bash; source /root/trace_overlay_ws/install/setup.bash
    cd /root/fish_interfere; FISH_SETUP_PARENT=1 source scripts/install_fish.sh 2>&1 | tail -3
    export PATH=/opt/ros/humble/fish/bin:$PATH PYTHONPATH=/opt/ros/humble/fish/python:$PYTHONPATH FISH_ENABLED=1
    unset RMW_IMPLEMENTATION CYCLONEDDS_URI
    INI=/opt/ros/humble/fish/fish_settings.ini
    sed -i "s|^rmw_implementation *=.*|rmw_implementation = rmw_cyclonedds_cpp|; s|^cyclonedds_uri *=.*|cyclonedds_uri = |; s|^per_instance *=.*|per_instance = true|; s|^replay_rosbag *=.*|replay_rosbag = false|" $INI
    echo "[run] ini:"; grep -nE "^(rmw_implementation|cyclonedds_uri|per_instance|replay_rosbag) *=" $INI
    NDTY=/opt/autoware/share/autoware_launch/config/localization/ndt_scan_matcher/ndt_scan_matcher.param.yaml
    sed -i "s|converged_param_nearest_voxel_transformation_likelihood: 2.3|converged_param_nearest_voxel_transformation_likelihood: 1.5|" $NDTY
    grep -n "converged_param_nearest_voxel_transformation_likelihood" $NDTY | xargs echo "[run] NDT threshold (container-local):"
    CAL=/opt/autoware/share/awsim_sensor_kit_description/config/sensor_kit_calibration.yaml
    python3 - <<PYC
import re
p="$CAL"; t=open(p).read()
t=re.sub(r"(velodyne_top_base_link:\n(?:.*\n){5}\s*yaw: )1\.575", r"\g<1>0.0", t)
open(p,"w").write(t)
PYC
    grep -n -A6 "velodyne_top_base_link" $CAL | grep yaw | xargs echo "[run] velodyne_top yaw (container-local):"
    set +e
    timeout 1000 ros2 launch autoware_launch e2e_simulator.launch.xml \
        vehicle_model:=sample_vehicle sensor_model:=awsim_sensor_kit \
        map_path:=/root/autoware_map/Shinjuku-Map/map \
        use_empty_dynamic_object_publisher:=true use_traffic_light_recognition:=false rviz:=false gnss_enabled:=false system_run_mode:=logging_simulation
    rc=$?; echo "[AW-r21] ros2 launch rc=$rc"
    sleep 30
    cp /root/goal_injector.log $(ls -td /root/fish_traces/fish_2026* | head -1)/fishlog/ 2>/dev/null
    ls -la /root/fish_traces/ | tail -3
'
echo "[AW-r21] exec rc=$?"
