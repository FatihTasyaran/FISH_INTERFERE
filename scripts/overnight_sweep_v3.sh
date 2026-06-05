#!/bin/bash
# =============================================================================
# overnight_sweep_v3.sh
#
# Key differences vs v2:
#   1. Fresh FISH install inside each container (container_install.sh) — no
#      stale fishinstalled images, no FISH_ACTIVE phantom env var.
#   2. FISH_ENABLED=1 so the wrapper owns the session lifecycle — no manual
#      start_session/launch_test/stop_session sandwich. launch_test invocation
#      goes through fish/bin/launch_test which inspects + monkey-patches +
#      delegates to launch_testing.launch_test:main.
#   3. NGC model assets mounted from host (downloaded by download_*.sh).
#   4. r2bdataset2023_v3 mounted (canonical merged dataset).
#   5. Two-pass per benchmark: warmup (FISH off, TRT compile, prime caches)
#      then measure (FISH on). Persistent TRT cache mounted across both.
#   6. Per-benchmark model wiring via case dispatch (install scripts /
#      NGC asset symlinks / nothing-needed).
#
# Usage:
#   scripts/overnight_sweep_v3.sh [bench_name1 bench_name2 ...]
#   If no args, runs the full "first wave" list (21 benchmarks).
# =============================================================================

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-600}        # TRT compile can take 5-10 min
FISH_NSYS_DRAIN=${FISH_NSYS_DRAIN:-60}     # nsys finalize wait
DEST_BASE=${DEST_BASE:-/tmp/fish_traces_v3}
TRT_CACHE_HOST=${TRT_CACHE_HOST:-/home/tue037807/trt_cache_v3}
NGC_ASSETS=${NGC_ASSETS:-/home/tue037807/isaac_ros_assets}
R2B_DATASET=${R2B_DATASET:-/home/tue037807/r2bdataset2023_v3}
R2B_DATASET_2024=${R2B_DATASET_2024:-/home/tue037807/r2bdataset2024_v1}

mkdir -p "$DEST_BASE" "$TRT_CACHE_HOST"

PHASE_MD="$REPO_ROOT/docs/sweep_v3_results.md"

log() { echo "[v3 $(date +%H:%M:%S)] $*" >&2; }

# ── benchmark -> (image, script_name, model_recipe) ────────────────────────
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

# Benchmark script *file stem* (the .py basename without extension)
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

# Model wiring recipe — a fragment of bash run INSIDE the container before
# launch_test. Empty for benchmarks that need no model.
declare -A MODEL_WIRE

# Pre-acquired models live under /host/isaac_assets/manual_models/<dir>/
# The container expects /root/ros_ws/src/ros2_benchmark/assets/models/<dir>/.
_MANUAL=/host/isaac_assets/manual_models
_BENCH_MODELS=/root/ros_ws/src/ros2_benchmark/assets/models

# detectnet: needs resnet34_peoplenet (TAO model, not AMR) + config.pbtxt + labels.txt + int8 cache
MODEL_WIRE[isaac_ros_detectnet]="
    mkdir -p $_BENCH_MODELS/peoplenet &&
    cp $_MANUAL/peoplenet/* $_BENCH_MODELS/peoplenet/
"
# ess: pre-downloaded ess.onnx + light_ess.onnx + ess_plugins.so (NGC tarball)
# trtexec invocation needs --staticPlugins pointing at ess_plugins.so for x86_64.
MODEL_WIRE[isaac_ros_ess]="
    mkdir -p $_BENCH_MODELS/ess/plugins/x86_64 &&
    cp $_MANUAL/ess/*.onnx $_BENCH_MODELS/ess/ &&
    cp $_MANUAL/ess/plugins/x86_64/ess_plugins.so $_BENCH_MODELS/ess/plugins/x86_64/
"

# unet/pynitros/tensor_rt: pre-downloaded shuffleseg ONNX → assets/models/peoplesemsegnet_shuffleseg/
MODEL_WIRE[isaac_ros_unet]="
    mkdir -p $_BENCH_MODELS/peoplesemsegnet_shuffleseg &&
    cp $_MANUAL/peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx \\
       $_BENCH_MODELS/peoplesemsegnet_shuffleseg/
"
MODEL_WIRE[isaac_ros_pynitros]=${MODEL_WIRE[isaac_ros_unet]}
MODEL_WIRE[isaac_ros_tensor_rt]=${MODEL_WIRE[isaac_ros_unet]}

# triton: shuffleseg ONNX + config.pbtxt (Triton needs explicit model config)
MODEL_WIRE[isaac_ros_triton]="
    mkdir -p $_BENCH_MODELS/peoplesemsegnet_shuffleseg &&
    cp $_MANUAL/peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx \\
       $_BENCH_MODELS/peoplesemsegnet_shuffleseg/ &&
    cp $_MANUAL/peoplesemsegnet_shuffleseg/config.pbtxt \\
       $_BENCH_MODELS/peoplesemsegnet_shuffleseg/
"

# segformer: peoplesemsegformer.onnx from NGC nvidia/tao/peoplesemsegformer:deployable_v1.0
MODEL_WIRE[isaac_ros_segformer]="
    mkdir -p $_BENCH_MODELS/peoplesemsegformer &&
    cp $_MANUAL/peoplesemsegformer/peoplesemsegformer.onnx $_BENCH_MODELS/peoplesemsegformer/
"

MODEL_WIRE[isaac_ros_bi3d]="
    mkdir -p $_BENCH_MODELS/bi3d &&
    cp $_MANUAL/bi3d/*.onnx $_BENCH_MODELS/bi3d/
"
MODEL_WIRE[isaac_ros_bi3d_freespace]=${MODEL_WIRE[isaac_ros_bi3d]}

MODEL_WIRE[isaac_ros_foundationpose]="
    mkdir -p $_BENCH_MODELS/foundationpose $_BENCH_MODELS/sdetr &&
    cp $_MANUAL/foundationpose/*.onnx $_BENCH_MODELS/foundationpose/ &&
    cp $_MANUAL/sdetr/sdetr_grasp.onnx $_BENCH_MODELS/sdetr/
"

MODEL_WIRE[isaac_ros_centerpose]="
    mkdir -p $_BENCH_MODELS/centerpose_shoe &&
    cp $_MANUAL/centerpose_shoe/* $_BENCH_MODELS/centerpose_shoe/
"

MODEL_WIRE[isaac_ros_rtdetr]="
    mkdir -p $_BENCH_MODELS/sdetr &&
    cp $_MANUAL/sdetr/sdetr_grasp.onnx $_BENCH_MODELS/sdetr/
"

MODEL_WIRE[isaac_ros_dope]="
    mkdir -p $_BENCH_MODELS/ketchup &&
    cp $_MANUAL/ketchup/ketchup.onnx $_BENCH_MODELS/ketchup/
"

# segment_anything needs BOTH mobile_sam + sdetr_grasp
MODEL_WIRE[isaac_ros_segment_anything]="
    mkdir -p $_BENCH_MODELS/segment_anything $_BENCH_MODELS/sdetr &&
    cp $_MANUAL/segment_anything/mobile_sam.onnx $_BENCH_MODELS/segment_anything/ &&
    cp $_MANUAL/segment_anything/config.pbtxt $_BENCH_MODELS/segment_anything/ &&
    cp $_MANUAL/sdetr/sdetr_grasp.onnx $_BENCH_MODELS/sdetr/
"

# Default empty for the rest (no model)
for b in "${!IMG_OF[@]}"; do
    [[ -z "${MODEL_WIRE[$b]:-}" ]] && MODEL_WIRE[$b]=":"
done

# ── one-benchmark runner ────────────────────────────────────────────────────
run_one() {
    local NAME=$1
    local PASS=$2          # "warmup" | "measure"
    local IMG="${IMG_OF[$NAME]}"
    local SCRIPT_STEM="${SCRIPT_OF[$NAME]}"
    local MODEL_CMD="${MODEL_WIRE[$NAME]}"

    local TRACE_DIR="$DEST_BASE/$NAME/$PASS"
    mkdir -p "$TRACE_DIR"

    local FISH_ON=0
    [[ "$PASS" == "measure" ]] && FISH_ON=1

    local LOG="$TRACE_DIR/launch.log"

    log "----- $NAME [$PASS] FISH_ENABLED=$FISH_ON IMG=$IMG -----"

    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        -v "$REPO_ROOT":/host/fish_src:ro \
        -v "$NGC_ASSETS":/host/isaac_assets:ro \
        -v "$R2B_DATASET":/host/r2bdataset:ro \
        -v "$R2B_DATASET_2024":/host/r2bdataset_2024:ro \
        -v "$NGC_ASSETS/apt_cache":/host/apt_cache:ro \
        -v "$TRACE_DIR":/root/fish_traces \
        -v "$TRT_CACHE_HOST":/root/.cache \
        -e ISAAC_ROS_ACCEPT_EULA=1 \
        -e ISAAC_ROS_WS=/root/ros_ws \
        -e FISH_ENABLED="$FISH_ON" \
        -e FISH_CUDA_EVENT_TRACE=1 \
        -e FISH_NSYS_DRAIN="$FISH_NSYS_DRAIN" \
        "$IMG" bash -lc "
            set -e
            export PYTHONUNBUFFERED=1

            # ── A0) Install missing TensorRT binary (trtexec).
            # fish-r2b-* images ship libnvinfer-dev but not libnvinfer-bin.
            # Isaac ROS benchmark scripts hardcode /usr/src/tensorrt/bin/trtexec
            # for ONNX→engine conversion. Quick fix: apt install (~10 s).
            # NOTE: most images' apt lists are stale → `apt-get update` first.
            # Pin version to installed libnvinfer-dev (10.16.1.11-1+cuda13.2) to
            # avoid pulling 11.x which depends on libnvinfer11 (not installed).
            if [ ! -x /usr/src/tensorrt/bin/trtexec ]; then
                NVINFER_VER=\$(dpkg-query -W -f='\${Version}' libnvinfer-dev 2>/dev/null)
                apt-get update >/tmp/apt_update.log 2>&1 || true
                if [ -n \"\$NVINFER_VER\" ]; then
                    apt-get install -y \"libnvinfer-bin=\$NVINFER_VER\" >/tmp/trtexec_install.log 2>&1 || {
                        echo \"[v3] libnvinfer-bin=\$NVINFER_VER install FAILED\"; tail -20 /tmp/trtexec_install.log
                    }
                else
                    apt-get install -y libnvinfer-bin >/tmp/trtexec_install.log 2>&1 || {
                        echo '[v3] libnvinfer-bin install FAILED'; tail -20 /tmp/trtexec_install.log
                    }
                fi
            fi

            # ── A1) Install libdcgm.so.3 if missing (needed by Triton GXF extension).
            # Affected benchmarks: detectnet, centerpose, triton (all use TritonNode).
            # .deb cached at /host/apt_cache/ to avoid 911 MB re-download per run.
            if [ ! -f /usr/lib/x86_64-linux-gnu/libdcgm.so.3 ]; then
                DCGM_DEB=/host/apt_cache/datacenter-gpu-manager_3.3.9_amd64.deb
                if [ -f \"\$DCGM_DEB\" ]; then
                    dpkg -i \"\$DCGM_DEB\" >/tmp/dcgm_install.log 2>&1 || {
                        echo '[v3] dcgm dpkg -i FAILED'; tail -10 /tmp/dcgm_install.log
                    }
                fi
            fi

            # ── A) Fresh FISH install (always — picks up host's current code)
            # setup_fish.sh is the in-container installer (deps + tracepoints + wrapper).
            # IMPORTANT: fish-r2b-* images may already contain /root/fish_interfere
            # from a previous bake. cp -r without trailing-slash semantics would
            # nest into the existing dir. Remove first, then cp -rT (treat dest
            # as target, not parent).
            rm -rf /root/fish_interfere
            cp -rT /host/fish_src /root/fish_interfere
            bash /root/fish_interfere/scripts/setup_fish.sh --yes >/tmp/fish_install.log 2>&1 || {
                echo '[v3] FISH install FAILED'; tail -30 /tmp/fish_install.log
                exit 90
            }
            source /opt/ros/humble/setup.bash
            export PATH=/opt/ros/humble/fish/bin:\$PATH
            export PYTHONPATH=/opt/ros/humble/fish/python:\$PYTHONPATH

            # ── A2) Override fish_settings.ini for Isaac benchmark mode.
            # Default config assumes Autoware: play an external rosbag, auto-
            # stop on bag exit. For Isaac:
            #   * Don't try to play the autoware bag (it doesn't exist).
            #   * BUT keep auto_stop=true — the daemon's graceful_stop_nsys
            #     gives nsys time to finalize. With auto_stop=false the
            #     launch_testing harness sends SIGINT to nsys with only 5s
            #     grace, which is not enough to flush the .nsys-rep.
            #   * Replace replay command with 'sleep 60' so the daemon waits
            #     60 seconds after system_stable before triggering auto_stop,
            #     giving the Isaac benchmark time to warm up + collect GPU
            #     activity through nsys.
            sed -i 's|^command = .*|command = sleep 60|' /opt/ros/humble/fish/fish_settings.ini

            # ── A1) Isaac ROS Benchmark hardcodes /workspaces/isaac_ros-dev/src/...
            # for model file paths (see TRTConverter calls). Our images put the
            # workspace at /root/ros_ws/. Symlink so the hardcoded paths resolve.
            mkdir -p /workspaces
            ln -sfn /root/ros_ws /workspaces/isaac_ros-dev

            # ── B) Wire r2b datasets (2023 + 2024 — 2024 has robotarm/galileo/whitetunnel)
            DEST_DS=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST_DS
            for bag in /host/r2bdataset/*; do
                [ -d \"\$bag\" ] && ln -sf \"\$bag\" \"\$DEST_DS/\$(basename \"\$bag\")\" 2>/dev/null
            done
            for bag in /host/r2bdataset_2024/*; do
                [ -d \"\$bag\" ] && ln -sf \"\$bag\" \"\$DEST_DS/\$(basename \"\$bag\")\" 2>/dev/null
            done
            mkdir -p /tmp/cp_extract

            # ── C) Wire benchmark-specific models
            ${MODEL_CMD}

            # ── D) Locate the launch_test script
            SCRIPT=\$(find /root/ros_ws/src/isaac_ros_benchmark -name '${SCRIPT_STEM}.py' -type f 2>/dev/null | head -1)
            [ -z \"\$SCRIPT\" ] && {
                echo '[v3] script not found: ${SCRIPT_STEM}.py'; exit 91
            }
            echo '[v3] using: '\$SCRIPT

            # ── E) Run — wrapper owns session lifecycle when FISH_ENABLED=1
            launch_test \"\$SCRIPT\"
        " > "$LOG" 2>&1
    local rc=$?

    log "$NAME [$PASS] rc=$rc — log: $LOG"
    return $rc
}

# ── per-benchmark 2-pass driver ─────────────────────────────────────────────
sweep_one() {
    local NAME=$1
    if [[ -z "${IMG_OF[$NAME]:-}" ]]; then
        log "$NAME : unknown benchmark, skipping"
        return 1
    fi
    run_one "$NAME" warmup || true                # warmup may fail; ignore
    sleep 5                                       # let any residual GPU drain
    run_one "$NAME" measure
    local rc=$?

    # Quick sanity: was nsys captured?
    local nsys_count
    nsys_count=$(ls "$DEST_BASE/$NAME/measure"/fish_*/nsys/*.nsys-rep 2>/dev/null | wc -l)
    log "$NAME : measure rc=$rc, nsys files=$nsys_count"
    echo "$NAME,$rc,$nsys_count" >> "$DEST_BASE/summary.csv"
}

# ── main ────────────────────────────────────────────────────────────────────
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

echo "name,measure_rc,nsys_files" > "$DEST_BASE/summary.csv"
echo "# Isaac ROS sweep v3 — $(date)" > "$PHASE_MD"
echo "" >> "$PHASE_MD"
echo "| Benchmark | measure rc | nsys files | log |" >> "$PHASE_MD"
echo "|---|---:|---:|---|" >> "$PHASE_MD"

for NAME in "${LIST[@]}"; do
    sweep_one "$NAME"
    nf=$(ls "$DEST_BASE/$NAME/measure"/fish_*/nsys/*.nsys-rep 2>/dev/null | wc -l)
    rc=$(tail -1 "$DEST_BASE/summary.csv" | awk -F, '{print $2}')
    LOG="$DEST_BASE/$NAME/measure/launch.log"
    echo "| \`$NAME\` | $rc | $nf | $LOG |" >> "$PHASE_MD"
done

log "=========================================="
log "Sweep complete — see $PHASE_MD"
log "  summary: $DEST_BASE/summary.csv"
log "=========================================="
