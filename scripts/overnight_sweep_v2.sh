#!/bin/bash
# =============================================================================
# overnight_sweep_v2.sh — fixed version
# Phase 1: For each unlockable benchmark, run the right install scripts AND
#          create the symlinks that map fetched files to launch_test's
#          expected paths.
# Phase 2: tag-format bug fixed (don't append :latest twice).
# Phase 3: postprocess/ingest.py from REPO_ROOT.
# =============================================================================

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-300}
DATASET_MOUNTS=(
    -v /home/tue037807/r2bdataset2023_v3:/host/r2b_2023:ro
    -v /home/tue037807/r2bdataset2024_v1:/host/r2b_2024:ro
)
log() { echo "[v2] $*" >&2; }

PHASE1_MD="$REPO_ROOT/docs/sweep_v2_phase1.md"
PHASE2_MD="$REPO_ROOT/docs/sweep_v2_phase2.md"
PHASE3_MD="$REPO_ROOT/docs/sweep_v2_phase3.md"
mkdir -p /tmp/fish_traces_v2

# ─── Phase 1: model fetch + symlink remap ─────────────────────────────────
cat > "$PHASE1_MD" <<'EOF'
# Phase 1 v2 — baseline-PASS sweep (modelsfetched-v2)

Only 3 install scripts exist in release-3.2:
- `isaac_ros_peoplesemseg_models_install` (peoplesemsegnet shuffleseg + vanilla)
- `isaac_ros_peoplenet_models_install` (peoplenet AMR)
- `isaac_ros_ess_models_install` (ESS stereo)

Other 9 benchmarks (bi3d, bi3d_freespace, centerpose, dope, foundationpose,
rtdetr, segformer, segment_anything, triton) lack install scripts in release-3.2
— need manual NGC URLs (deferred).

| Image | Result | Detail |
|---|---|---|
EOF

# Map: benchmark → (install_repo, expected_symlink_command)
declare -A UNLOCKABLE=(
  [isaac_ros_ess]="isaac_ros_dnn_stereo_depth:isaac_ros_ess_models_install:ess.onnx:ess/ess.onnx"
  [isaac_ros_detectnet]="isaac_ros_object_detection:isaac_ros_peoplenet_models_install:rsu_rs_480_640_mask:peoplenet/"
  [isaac_ros_unet]="isaac_ros_image_segmentation:isaac_ros_peoplesemseg_models_install:peoplesemsegnet_shuffleseg:peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx"
  [isaac_ros_pynitros]="isaac_ros_image_segmentation:isaac_ros_peoplesemseg_models_install:peoplesemsegnet_shuffleseg:peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx"
  [isaac_ros_tensor_rt]="isaac_ros_image_segmentation:isaac_ros_peoplesemseg_models_install:peoplesemsegnet_shuffleseg:peoplesemsegnet_shuffleseg/peoplesemsegnet_shuffleseg.onnx"
)

phase1_v2() {
    local BENCH="$1"
    local SHORT="${BENCH#isaac_ros_}"
    local NEW_IMG="fish-r2b-${SHORT}-modelsfetched-v2:latest"
    local ORIG_IMG
    if docker image inspect "fish-r2b-${SHORT}-runtimepatched:latest" >/dev/null 2>&1; then
        ORIG_IMG="fish-r2b-${SHORT}-runtimepatched:latest"
    else
        ORIG_IMG="fish-r2b-${SHORT}:latest"
    fi
    docker image inspect "$ORIG_IMG" >/dev/null 2>&1 || { echo "| \`$NEW_IMG\` | ⏭️ skip | base image missing |" >> "$PHASE1_MD"; return; }

    local SPEC="${UNLOCKABLE[$BENCH]}"
    IFS=':' read -r REPO PKG ASSET_GLOB EXPECTED <<< "$SPEC"
    local DOCKERFILE=/tmp/Dockerfile.${BENCH}.modelsfetched-v2

    cat > "$DOCKERFILE" <<EOF
FROM $ORIG_IMG
ENV ISAAC_ROS_ACCEPT_EULA=1
ENV ISAAC_ROS_WS=/root/ros_ws
RUN apt-get update -qq && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        libnvinfer-bin wget curl jq && \\
    rm -rf /var/lib/apt/lists/*
RUN bash -lc 'set -e; \\
    REPO_DIR=/root/ros_ws/src/$REPO; \\
    PKG_DIR=\$REPO_DIR/$PKG; \\
    if [ ! -d "\$PKG_DIR" ]; then \\
        cd /root/ros_ws/src && git clone --depth 1 --branch release-3.2 https://github.com/NVIDIA-ISAAC-ROS/${REPO}.git 2>&1 | tail -3; \\
    fi; \\
    rm -f \$PKG_DIR/COLCON_IGNORE; \\
    for s in \$PKG_DIR/asset_scripts/install_*.sh; do \\
        [ -f "\$s" ] || continue; \\
        chmod +x "\$s"; \\
        echo "[modelsfetched-v2] running \$(basename \$s)"; \\
        bash "\$s" 2>&1 | tail -5 || true; \\
    done; \\
    # Custom symlink for this benchmark, expected path EXPECTED
    EXPECTED_PATH=/root/ros_ws/src/ros2_benchmark/assets/models/$EXPECTED; \\
    mkdir -p "\$(dirname \$EXPECTED_PATH)"; \\
    # Find what got downloaded; try several candidates
    ONNX_FILE=\$(find /root/ros_ws/isaac_ros_assets/models -maxdepth 4 -name "*$ASSET_GLOB*" -name "*.onnx" 2>/dev/null | head -1); \\
    [ -z "\$ONNX_FILE" ] && ONNX_FILE=\$(find /root/ros_ws/isaac_ros_assets/models -maxdepth 4 -name "model.onnx" 2>/dev/null | head -1); \\
    if [ -n "\$ONNX_FILE" ]; then \\
        if [ -d "\$EXPECTED_PATH" ] || [ "\${EXPECTED_PATH: -1}" = "/" ]; then \\
            # EXPECTED is a directory — symlink the parent dir
            ln -sf "\$(dirname \$ONNX_FILE)" "\${EXPECTED_PATH%/}" 2>/dev/null || true; \\
            ln -sf "\$ONNX_FILE" "\${EXPECTED_PATH%/}/model.onnx" 2>/dev/null || true; \\
        else \\
            ln -sf "\$ONNX_FILE" "\$EXPECTED_PATH" 2>/dev/null || true; \\
        fi; \\
        echo "[modelsfetched-v2] symlinked \$ONNX_FILE -> \$EXPECTED_PATH"; \\
        ls -la "\$EXPECTED_PATH" 2>&1 | head -2; \\
    else \\
        echo "[modelsfetched-v2] no .onnx found after install"; \\
    fi'
EOF
    if ! docker build -q -t "$NEW_IMG" -f "$DOCKERFILE" "$REPO_ROOT" > /tmp/build-v2-${BENCH}.log 2>&1; then
        echo "| \`$NEW_IMG\` | ❌ build fail | $(grep -iE 'error' /tmp/build-v2-${BENCH}.log | head -1 | cut -c1-160) |" >> "$PHASE1_MD"
        return
    fi
    local SCRIPT
    SCRIPT=$(python3 -c "
import json
inv=json.load(open('$REPO_ROOT/tests/benchmark_inventory.json'))
for b in inv['benchmarks']:
    if b['name']=='$BENCH' and b.get('scripts'): print(b['scripts'][0]); break
")
    local SMOKE_LOG=/tmp/phase1-v2-smoke-${BENCH}.log
    : > "$SMOKE_LOG"
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" -e ISAAC_ROS_ACCEPT_EULA=1 \
        "$NEW_IMG" \
        bash -lc "
            DEST=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST
            for src in /host/r2b_2023 /host/r2b_2024; do
                [ -d \$src ] || continue
                for bag in \$src/*; do [ -d \$bag ] && ln -sf \$bag \$DEST/\$(basename \$bag) 2>/dev/null; done
            done
            launch_test /root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCH}_benchmark/scripts/${SCRIPT}.py 2>&1
        " > "$SMOKE_LOG" 2>&1
    local rc=$?
    local fps
    fps=$(python3 -c "
import re
try:
    with open('$SMOKE_LOG', 'rb') as f: data=f.read()
    text=''.join(chr(b) for b in data if b==10 or 32<=b<127)
    m=re.search(r'Mean Frame Rate \(fps\)\s*:\s*([0-9]+\.[0-9]+)', text)
    print(m.group(1) if m else '')
except: print('')
")
    if [ -n "$fps" ]; then
        echo "| \`$NEW_IMG\` | ✅ PASS | $fps fps |" >> "$PHASE1_MD"
        log "[phase1-v2] $BENCH PASS $fps fps"
    else
        local err=$(grep -m1 -iE "error|fatal|missing|No such" "$SMOKE_LOG" 2>/dev/null | head -1 | cut -c1-160)
        echo "| \`$NEW_IMG\` | ❌ FAIL | rc=$rc — $err |" >> "$PHASE1_MD"
        log "[phase1-v2] $BENCH FAIL: $err"
    fi
}

for bench in "${!UNLOCKABLE[@]}"; do
    phase1_v2 "$bench"
done

# Note the 9 unrecoverable
cat >> "$PHASE1_MD" <<'EOF'

## Not recoverable from release-3.2 alone (no models_install pkg)

| Benchmark | Needs | NGC URL (lookup required) |
|---|---|---|
| `bi3d`, `bi3d_freespace` | `bi3d/featnet.onnx` + `bi3d/segnet.onnx` | manual NGC download |
| `centerpose` | `centerpose_shoe/centerpose_shoe.onnx` + config.pbtxt | manual NGC |
| `dope`, `triton` (dope variant), `tensor_rt` (dope variant) | `ketchup/ketchup.onnx` (+ config.pbtxt for triton) | manual NGC |
| `foundationpose` | `foundationpose/refine_model.onnx`, `foundationpose/score_model.onnx`, `sdetr/sdetr_grasp.onnx` | manual NGC |
| `rtdetr` | `sdetr/sdetr_grasp.onnx` | manual NGC |
| `segformer` | `peoplesemsegformer/peoplesemsegformer.onnx` | manual NGC |
| `segment_anything` | `segment_anything/mobile_sam.onnx` + config.pbtxt, `sdetr/sdetr_grasp.onnx` | manual NGC |
| `occupancy_grid_localizer` | r2b_galileo dataset overlay (ament resource) | NVIDIA-ISAAC-ROS-DEV/isaac_ros_r2b_galileo |
EOF
log "phase1-v2 done"

# ─── Phase 2: FISH-traced overlay (tag bug fixed) ─────────────────────────
cat > "$PHASE2_MD" <<'EOF'
# Phase 2 v2 — FISH-traced sweep

For each baseline-PASS image, derive `-fishtraced:latest` with FISH wrapper
PATH + trace_session start/stop wrap. Check (a) benchmark output (b) fish data.

| Image | benchmark output | fish data | Detail |
|---|---|---|---|
EOF

phase2_v2() {
    local SRC_IMG="$1" BENCH="$2"
    # Strip :latest then append -fishtraced:latest
    local BASE_NAME="${SRC_IMG%:latest}"
    local NEW_IMG="${BASE_NAME}-fishtraced:latest"
    local DOCKERFILE=/tmp/Dockerfile.${BENCH}.fishtraced-v2
    cat > "$DOCKERFILE" <<EOF
FROM $SRC_IMG
ENV PATH=/opt/ros/humble/fish/bin:\$PATH
ENV FISH_ACTIVE=1
EOF
    if ! docker build -q -t "$NEW_IMG" -f "$DOCKERFILE" "$REPO_ROOT" > /tmp/phase2-v2-build-${BENCH}.log 2>&1; then
        echo "| \`$NEW_IMG\` | ❌ build fail | — | $(head -1 /tmp/phase2-v2-build-${BENCH}.log | cut -c1-160) |" >> "$PHASE2_MD"
        return
    fi
    local SCRIPT
    SCRIPT=$(python3 -c "
import json
inv=json.load(open('$REPO_ROOT/tests/benchmark_inventory.json'))
for b in inv['benchmarks']:
    if b['name']=='$BENCH' and b.get('scripts'): print(b['scripts'][0]); break
")
    local SMOKE_LOG=/tmp/phase2-v2-smoke-${BENCH}.log
    : > "$SMOKE_LOG"
    local SCRIPT_PATH="/root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCH}_benchmark/scripts/${SCRIPT}.py"
    # Use per-bench fish_traces dir
    rm -rf /tmp/fish_traces_v2/${BENCH}
    mkdir -p /tmp/fish_traces_v2/${BENCH}
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
        -v /tmp/fish_traces_v2/${BENCH}:/root/fish_traces \
        -e ISAAC_ROS_ACCEPT_EULA=1 \
        "$NEW_IMG" \
        bash -lc "
            DEST=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST
            for src in /host/r2b_2023 /host/r2b_2024; do
                [ -d \$src ] || continue
                for bag in \$src/*; do [ -d \$bag ] && ln -sf \$bag \$DEST/\$(basename \$bag) 2>/dev/null; done
            done
            source /opt/ros/humble/fish/scripts/trace_session.sh
            start_session 2>&1
            launch_test $SCRIPT_PATH 2>&1
            BENCH_RC=\$?
            stop_session 2>&1
            exit \$BENCH_RC
        " > "$SMOKE_LOG" 2>&1
    local rc=$?

    local fps
    fps=$(python3 -c "
import re
try:
    with open('$SMOKE_LOG', 'rb') as f: data=f.read()
    text=''.join(chr(b) for b in data if b==10 or 32<=b<127)
    m=re.search(r'Mean Frame Rate \(fps\)\s*:\s*([0-9]+\.[0-9]+)', text)
    print(m.group(1) if m else '')
except: print('')
")
    local out_col
    if [ -n "$fps" ]; then out_col="✅ ${fps}fps"; else out_col="❌"; fi

    local sess_dir n_files fish_col
    sess_dir=$(find /tmp/fish_traces_v2/${BENCH} -maxdepth 1 -type d -name 'fish_*' 2>/dev/null | head -1)
    n_files=$(find "${sess_dir:-/tmp/_nope}" -type f 2>/dev/null | wc -l)
    if [ -n "$sess_dir" ] && [ "$n_files" -gt 0 ]; then
        fish_col="✅ $(basename "$sess_dir") (${n_files} files)"
    else
        fish_col="❌ (no traces)"
    fi
    local err=""
    [ -z "$fps" ] && err=$(grep -m1 -iE "error|fatal|missing" "$SMOKE_LOG" 2>/dev/null | head -1 | cut -c1-100)
    echo "| \`$NEW_IMG\` | $out_col | $fish_col | rc=$rc ${err:+— $err} |" >> "$PHASE2_MD"
    log "[phase2-v2] $BENCH out=$out_col fish=$fish_col"
}

# Discover ALL PASS images now (originals + new v2 modelsfetched)
for b in apriltag dnn_image_encoder image_proc nitros_bridge nvblox stereo_image_proc visual_slam; do
    docker image inspect "fish-r2b-${b}:latest" >/dev/null 2>&1 && \
        phase2_v2 "fish-r2b-${b}:latest" "isaac_ros_${b}"
done
for img in $(docker images --format '{{.Repository}}' | grep '^fish-r2b-.*-modelsfetched-v2$' 2>/dev/null); do
    short=$(echo "$img" | sed -E 's/^fish-r2b-//; s/-modelsfetched-v2$//')
    phase2_v2 "${img}:latest" "isaac_ros_${short}"
done
log "phase2-v2 done"

# ─── Phase 3: ingest ───────────────────────────────────────────────────────
cat > "$PHASE3_MD" <<'EOF'
# Phase 3 v2 — FISH ingest to InfluxDB

| Benchmark | Session dir | Ingest result |
|---|---|---|
EOF

for trace_root in /tmp/fish_traces_v2/*/; do
    [ -d "$trace_root" ] || continue
    bench=$(basename "$trace_root")
    sess_dir=$(find "$trace_root" -maxdepth 1 -type d -name 'fish_*' 2>/dev/null | head -1)
    [ -z "$sess_dir" ] && continue
    INGEST_LOG=/tmp/phase3-v2-ingest-${bench}.log
    : > "$INGEST_LOG"
    cd "$REPO_ROOT" && python3 postprocess/ingest.py "$sess_dir" > "$INGEST_LOG" 2>&1 || true
    cd - >/dev/null
    sess_id=$(grep -iE "session|inserted|ingested" "$INGEST_LOG" 2>/dev/null | head -2 | tr '\n' ' ' | cut -c1-200)
    [ -z "$sess_id" ] && sess_id="(see /tmp/phase3-v2-ingest-${bench}.log)"
    echo "| \`$bench\` | \`$(basename "$sess_dir")\` | $sess_id |" >> "$PHASE3_MD"
    log "[phase3-v2] $bench → $sess_id"
done
log "phase3-v2 done"

# Final summary
SUMMARY="$REPO_ROOT/docs/isaac_ros_overnight_summary.md"
cat > "$SUMMARY" <<'EOF'
# Overnight v2 sweep — final 3-table summary

EOF
cat "$PHASE1_MD" >> "$SUMMARY"
echo "" >> "$SUMMARY"
cat "$PHASE2_MD" >> "$SUMMARY"
echo "" >> "$SUMMARY"
cat "$PHASE3_MD" >> "$SUMMARY"

log "v2 sweep complete"
