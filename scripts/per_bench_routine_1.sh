#!/bin/bash
# =============================================================================
# per-bench-routine-1
# =============================================================================
# Per-benchmark autonomous routine. For each name passed (or for the default
# 23-benchmark list), runs TWO passes inside a fresh container:
#
#   pass=fishoff  → FISH_ENABLED=0   captures only Isaac ROS perf JSON
#   pass=fishon   → FISH_ENABLED=1   captures perf JSON + LTTng trace + nsys
#
# Output layout per benchmark:
#   DEST_BASE/<name>/
#     ├── fishoff/
#     │     ├── launch.log
#     │     └── r2b-log-*.json
#     └── fishon/
#           ├── launch.log
#           ├── r2b-log-*.json
#           ├── fish_<ts>/                      (ros2/ + nsys/ + snapshot/ + fishlog/)
#           └── fish_graph.json                 (after ingest+extract; written by
#                                                postprocess step, not by this
#                                                script)
#
# CSV summary: DEST_BASE/summary.csv with columns
#   name,fishoff_rc,fishon_rc,fishoff_json,fishon_json,fish_session
#
# Usage:
#   ./per_bench_routine_1.sh [name1 name2 ...]
#
# Notes:
#   - Source is mounted from REPO_ROOT (= host fish_interfere) into /host/fish_src,
#     and setup_fish.sh is run inside the container so the patched
#     install_fish_tracepoints with the GenericSubscription tracepoint patch is
#     applied on every fresh container.
#   - We DON'T rebuild ros2_benchmark/isaac_ros_benchmark by default — the
#     pre-built image's libs still use the unpatched GenericSubscription ctor
#     for a single MonitorNode sub. That is one F per benchmark; the rest of
#     the graph is unaffected.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_BASE=${DEST_BASE:-/tmp/per_bench_routine_1}
TRT_CACHE_HOST=${TRT_CACHE_HOST:-/home/tue037807/trt_cache_v3}
NGC_ASSETS=${NGC_ASSETS:-/home/tue037807/isaac_ros_assets}
R2B_DATASET=${R2B_DATASET:-/home/tue037807/r2bdataset2023_v3}
R2B_DATASET_2024=${R2B_DATASET_2024:-/home/tue037807/r2bdataset2024_v1}
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-900}

mkdir -p "$DEST_BASE" "$TRT_CACHE_HOST"

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*" >&2; }

# ── Benchmark → image and launch_test script ────────────────────────────────
declare -A IMG_OF=(
    [isaac_ros_apriltag]="fish-r2b-apriltag:latest"
    [isaac_ros_dnn_image_encoder]="fish-r2b-dnn_image_encoder:latest"
    [isaac_ros_image_proc]="fish-r2b-image_proc:latest"
    [isaac_ros_stereo_image_proc]="fish-r2b-stereo_image_proc:latest"
    [isaac_ros_nvblox]="fish-r2b-nvblox:latest"
    [isaac_ros_visual_slam]="fish-r2b-visual_slam:latest"
    [isaac_ros_h264_decoder]="fish-r2b-h264_decoder:latest"
    [isaac_ros_h264_encoder]="fish-r2b-h264_encoder:latest"
    [isaac_ros_nitros_bridge]="fish-r2b-nitros_bridge:latest"
    [isaac_ros_detectnet]="fish-r2b-detectnet:latest"
    [isaac_ros_ess]="fish-r2b-ess:latest"
    [isaac_ros_segformer]="fish-r2b-segformer:latest"
    [isaac_ros_unet]="fish-r2b-unet:latest"
    [isaac_ros_pynitros]="fish-r2b-pynitros:latest"
    [isaac_ros_tensor_rt]="fish-r2b-tensor_rt:latest"
    [isaac_ros_triton]="fish-r2b-triton:latest"
    [isaac_ros_bi3d]="fish-r2b-bi3d:latest"
    [isaac_ros_bi3d_freespace]="fish-r2b-bi3d_freespace:latest"
    [isaac_ros_foundationpose]="fish-r2b-foundationpose:latest"
    [isaac_ros_centerpose]="fish-r2b-centerpose:latest"
    [isaac_ros_rtdetr]="fish-r2b-rtdetr:latest"
    [isaac_ros_dope]="fish-r2b-dope:latest"
    [isaac_ros_segment_anything]="fish-r2b-segment_anything:latest"
)

declare -A SCRIPT_OF=(
    [isaac_ros_apriltag]="isaac_ros_apriltag_node"
    [isaac_ros_dnn_image_encoder]="isaac_ros_dnn_image_encoder_node"
    [isaac_ros_image_proc]="isaac_ros_rectify_node"
    [isaac_ros_stereo_image_proc]="isaac_ros_disparity_node"
    [isaac_ros_nvblox]="isaac_ros_nvblox_node"
    [isaac_ros_visual_slam]="isaac_ros_visual_slam_node"
    [isaac_ros_h264_decoder]="isaac_ros_h264_decoder_node"
    [isaac_ros_h264_encoder]="isaac_ros_h264_encoder_iframe_node"
    [isaac_ros_nitros_bridge]="isaac_ros_nitros_bridge"
    [isaac_ros_detectnet]="isaac_ros_detectnet_graph"
    [isaac_ros_ess]="isaac_ros_ess_node"
    [isaac_ros_segformer]="isaac_ros_segformer_graph"
    [isaac_ros_unet]="isaac_ros_unet_graph"
    [isaac_ros_pynitros]="isaac_ros_pynitros_unet_graph"
    [isaac_ros_tensor_rt]="isaac_ros_tensor_rt_ps_node"
    [isaac_ros_triton]="isaac_ros_triton_ps_node"
    [isaac_ros_bi3d]="isaac_ros_bi3d_node"
    [isaac_ros_bi3d_freespace]="isaac_ros_bi3d_fs_node"
    [isaac_ros_foundationpose]="isaac_ros_foundationpose_node"
    [isaac_ros_centerpose]="isaac_ros_centerpose_graph"
    [isaac_ros_rtdetr]="isaac_ros_rtdetr_graph"
    [isaac_ros_dope]="isaac_ros_dope_graph"
    [isaac_ros_segment_anything]="isaac_ros_mobile_segment_anything_graph"
)

declare -A MODEL_WIRE
_MANUAL=/host/isaac_assets/manual_models
_BENCH_MODELS=/root/ros_ws/src/ros2_benchmark/assets/models
MODEL_WIRE[isaac_ros_detectnet]="mkdir -p $_BENCH_MODELS/peoplenet && cp $_MANUAL/peoplenet/* $_BENCH_MODELS/peoplenet/"
MODEL_WIRE[isaac_ros_ess]="mkdir -p $_BENCH_MODELS/ess/plugins/x86_64 && cp $_MANUAL/ess/*.onnx $_BENCH_MODELS/ess/ && cp $_MANUAL/ess/plugins/x86_64/ess_plugins.so $_BENCH_MODELS/ess/plugins/x86_64/"
MODEL_WIRE[isaac_ros_unet]="mkdir -p $_BENCH_MODELS/peoplesemsegnet_shuffleseg && cp $_MANUAL/peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx $_BENCH_MODELS/peoplesemsegnet_shuffleseg/"
MODEL_WIRE[isaac_ros_pynitros]=${MODEL_WIRE[isaac_ros_unet]}
MODEL_WIRE[isaac_ros_tensor_rt]=${MODEL_WIRE[isaac_ros_unet]}
MODEL_WIRE[isaac_ros_triton]="mkdir -p $_BENCH_MODELS/peoplesemsegnet_shuffleseg && cp $_MANUAL/peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx $_BENCH_MODELS/peoplesemsegnet_shuffleseg/ && cp $_MANUAL/peoplesemsegnet_shuffleseg/config.pbtxt $_BENCH_MODELS/peoplesemsegnet_shuffleseg/"
MODEL_WIRE[isaac_ros_segformer]="mkdir -p $_BENCH_MODELS/peoplesemsegformer && cp $_MANUAL/peoplesemsegformer/peoplesemsegformer.onnx $_BENCH_MODELS/peoplesemsegformer/"
MODEL_WIRE[isaac_ros_bi3d]="mkdir -p $_BENCH_MODELS/bi3d && cp $_MANUAL/bi3d/*.onnx $_BENCH_MODELS/bi3d/"
MODEL_WIRE[isaac_ros_bi3d_freespace]=${MODEL_WIRE[isaac_ros_bi3d]}
MODEL_WIRE[isaac_ros_foundationpose]="mkdir -p $_BENCH_MODELS/foundationpose $_BENCH_MODELS/sdetr && cp $_MANUAL/foundationpose/*.onnx $_BENCH_MODELS/foundationpose/ && cp $_MANUAL/sdetr/sdetr_grasp.onnx $_BENCH_MODELS/sdetr/"
MODEL_WIRE[isaac_ros_centerpose]="mkdir -p $_BENCH_MODELS/centerpose_shoe && cp $_MANUAL/centerpose_shoe/* $_BENCH_MODELS/centerpose_shoe/"
MODEL_WIRE[isaac_ros_rtdetr]="mkdir -p $_BENCH_MODELS/sdetr && cp $_MANUAL/sdetr/sdetr_grasp.onnx $_BENCH_MODELS/sdetr/"
MODEL_WIRE[isaac_ros_dope]="mkdir -p $_BENCH_MODELS/ketchup && cp $_MANUAL/ketchup/ketchup.onnx $_BENCH_MODELS/ketchup/"
MODEL_WIRE[isaac_ros_segment_anything]="mkdir -p $_BENCH_MODELS/segment_anything $_BENCH_MODELS/sdetr && cp $_MANUAL/segment_anything/mobile_sam.onnx $_BENCH_MODELS/segment_anything/ && cp $_MANUAL/segment_anything/config.pbtxt $_BENCH_MODELS/segment_anything/ && cp $_MANUAL/sdetr/sdetr_grasp.onnx $_BENCH_MODELS/sdetr/"
for b in "${!IMG_OF[@]}"; do
    [[ -z "${MODEL_WIRE[$b]:-}" ]] && MODEL_WIRE[$b]=":"
done

# ── One-pass run: $1=name, $2=pass (fishoff|fishon) ─────────────────────────
run_pass() {
    local NAME=$1 PASS=$2
    local IMG="${IMG_OF[$NAME]}"
    local SCRIPT_STEM="${SCRIPT_OF[$NAME]}"
    local MODEL_CMD="${MODEL_WIRE[$NAME]}"
    local OUT_DIR="$DEST_BASE/$NAME/$PASS"
    mkdir -p "$OUT_DIR"
    local LOG="$OUT_DIR/launch.log"

    local FISH_ON=0
    [[ "$PASS" == "fishon" ]] && FISH_ON=1

    log "  pass=$PASS image=$IMG FISH_ENABLED=$FISH_ON"

    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        -v "$REPO_ROOT":/host/fish_src:ro \
        -v "$NGC_ASSETS":/host/isaac_assets:ro \
        -v "$R2B_DATASET":/host/r2bdataset:ro \
        -v "$R2B_DATASET_2024":/host/r2bdataset_2024:ro \
        -v "$NGC_ASSETS/apt_cache":/host/apt_cache:ro \
        -v "$OUT_DIR":/root/fish_traces \
        -v "$TRT_CACHE_HOST":/root/.cache \
        -e ISAAC_ROS_ACCEPT_EULA=1 \
        -e ISAAC_ROS_WS=/root/ros_ws \
        -e FISH_ENABLED="$FISH_ON" \
        -e FISH_CUDA_EVENT_TRACE=1 \
        -e FISH_NSYS_DRAIN=15 \
        "$IMG" bash -lc "
            set -e
            export PYTHONUNBUFFERED=1

            # Pre-flight: trtexec for vision benchmarks
            if [ ! -x /usr/src/tensorrt/bin/trtexec ]; then
                NVINFER_VER=\$(dpkg-query -W -f='\${Version}' libnvinfer-dev 2>/dev/null || true)
                if [ -n \"\$NVINFER_VER\" ]; then
                    apt-get update >/tmp/apt_update.log 2>&1 || true
                    apt-get install -y \"libnvinfer-bin=\$NVINFER_VER\" >/tmp/trtexec_install.log 2>&1 || true
                fi
            fi
            if [ ! -f /usr/lib/x86_64-linux-gnu/libdcgm.so.3 ]; then
                DCGM_DEB=/host/apt_cache/datacenter-gpu-manager_3.3.9_amd64.deb
                if [ -f \"\$DCGM_DEB\" ]; then
                    dpkg -i \"\$DCGM_DEB\" >/tmp/dcgm_install.log 2>&1 || true
                fi
            fi

            # FISH install if measure pass; for fishoff pass we still install FISH so
            # the launch_test wrapper logic is identical (and FISH_ENABLED=0 disables
            # tracing inside the wrapper without skipping the script).
            rm -rf /root/fish_interfere
            cp -rT /host/fish_src /root/fish_interfere
            bash /root/fish_interfere/scripts/setup_fish.sh --yes >/tmp/fish_install.log 2>&1 || {
                echo '[pbr1] FISH install FAILED'; tail -40 /tmp/fish_install.log
                exit 90
            }
            source /opt/ros/humble/setup.bash
            source /root/trace_overlay_ws/install/setup.bash
            export PATH=/opt/ros/humble/fish/bin:\$PATH
            export PYTHONPATH=/opt/ros/humble/fish/python:\$PYTHONPATH
            # Image bakes FISH_ENABLED=1 via ENV + /etc/profile.d/ros-fish.sh — override here:
            export FISH_ENABLED=${FISH_ON}
            echo \"[pbr1] FISH_ENABLED=\$FISH_ENABLED (pass=${PASS})\"

            sed -i 's|^command = .*|command = sleep 60|' /opt/ros/humble/fish/fish_settings.ini

            mkdir -p /workspaces
            ln -sfn /root/ros_ws /workspaces/isaac_ros-dev

            DEST_DS=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST_DS
            for bag in /host/r2bdataset/*; do
                [ -d \"\$bag\" ] && ln -sfn \"\$bag\" \"\$DEST_DS/\$(basename \"\$bag\")\" 2>/dev/null
            done
            for bag in /host/r2bdataset_2024/*; do
                [ -d \"\$bag\" ] && ln -sfn \"\$bag\" \"\$DEST_DS/\$(basename \"\$bag\")\" 2>/dev/null
            done
            mkdir -p /tmp/cp_extract

            ${MODEL_CMD}

            SCRIPT=\$(find /root/ros_ws/src/isaac_ros_benchmark -name '${SCRIPT_STEM}.py' -type f 2>/dev/null | head -1)
            [ -z \"\$SCRIPT\" ] && { echo '[pbr1] script not found: ${SCRIPT_STEM}.py'; exit 91; }
            echo '[pbr1] using: '\$SCRIPT

            set +e
            launch_test \"\$SCRIPT\"
            lt_rc=\$?
            echo \"[pbr1] launch_test rc=\$lt_rc\"

            # Copy perf JSON out so the host can pick it up after the container exits.
            cp /tmp/r2b-log-*.json /root/fish_traces/ 2>/dev/null
            cp_rc=\$?
            echo \"[pbr1] cp r2b-log-*.json rc=\$cp_rc (host /root/fish_traces is bound mount)\"
            ls -l /root/fish_traces/ 2>&1 | head -10
            exit \$lt_rc
        " > "$LOG" 2>&1
    local rc=$?
    log "  pass=$PASS rc=$rc — log: $LOG"
    return $rc
}

# ── Driver per benchmark ────────────────────────────────────────────────────
do_bench() {
    local NAME=$1
    if [[ -z "${IMG_OF[$NAME]:-}" ]]; then
        log "$NAME : unknown benchmark, skipping"
        return 1
    fi
    log "==== $NAME ===="
    local off_rc=0 on_rc=0
    set +e
    run_pass "$NAME" "fishoff"; off_rc=$?
    sleep 5
    run_pass "$NAME" "fishon"; on_rc=$?
    set -e

    local fishoff_json=$(ls "$DEST_BASE/$NAME/fishoff"/r2b-log-*.json 2>/dev/null | head -1)
    local fishon_json=$(ls "$DEST_BASE/$NAME/fishon"/r2b-log-*.json 2>/dev/null | head -1)
    local fish_session=$(ls -d "$DEST_BASE/$NAME/fishon"/fish_* 2>/dev/null | head -1)
    log "$NAME : off_rc=${off_rc:-?} on_rc=${on_rc:-?} off_json=$([ -n "$fishoff_json" ] && echo Y || echo N) on_json=$([ -n "$fishon_json" ] && echo Y || echo N) sess=$([ -n "$fish_session" ] && echo Y || echo N)"
    echo "$NAME,${off_rc:-?},${on_rc:-?},$([ -n "$fishoff_json" ] && echo Y || echo N),$([ -n "$fishon_json" ] && echo Y || echo N),$([ -n "$fish_session" ] && basename "$fish_session" || echo -)" >> "$DEST_BASE/summary.csv"
}

# ── Main ────────────────────────────────────────────────────────────────────
DEFAULT_LIST=(
    isaac_ros_apriltag
    isaac_ros_dnn_image_encoder
    isaac_ros_image_proc
    isaac_ros_stereo_image_proc
    isaac_ros_nvblox
    isaac_ros_visual_slam
    isaac_ros_h264_decoder
    isaac_ros_h264_encoder
    isaac_ros_nitros_bridge
    isaac_ros_detectnet
    isaac_ros_ess
    isaac_ros_segformer
    isaac_ros_unet
    isaac_ros_pynitros
    isaac_ros_tensor_rt
    isaac_ros_triton
    isaac_ros_bi3d
    isaac_ros_bi3d_freespace
    isaac_ros_foundationpose
    isaac_ros_centerpose
    isaac_ros_rtdetr
    isaac_ros_dope
    isaac_ros_segment_anything
)

LIST=("$@")
[[ ${#LIST[@]} -eq 0 ]] && LIST=("${DEFAULT_LIST[@]}")

if [[ ! -s "$DEST_BASE/summary.csv" ]]; then
    echo "name,fishoff_rc,fishon_rc,fishoff_json,fishon_json,fish_session" > "$DEST_BASE/summary.csv"
fi

for NAME in "${LIST[@]}"; do
    do_bench "$NAME"
done

log "ALL DONE. summary: $DEST_BASE/summary.csv"
