#!/bin/bash
# =============================================================================
# overnight_sweep.sh — three-phase orchestrator:
#
# Phase 1: For each failing baseline benchmark, derive
#          fish-r2b-<bench>-modelsfetched:latest by running the upstream
#          isaac_ros_*_models_install asset scripts (ISAAC_ROS_ACCEPT_EULA=1).
#          Smoke test. Pass → keep image. Fail → record reason.
# Phase 2: For each baseline-PASS image (orig + -modelsfetched), derive
#          fish-r2b-<bench>-fishtraced:latest that puts FISH wrapper first
#          in PATH and wraps launch_test in trace_session start/stop. Smoke
#          test under FISH:
#            * benchmark output emitted? AND
#            * ~/fish_traces/<session>/ contents present?
# Phase 3: For each FISH-traced image with both: copy traces out + run FISH
#          ingest.py → record InfluxDB session ID.
#
# Outputs:
#   docs/isaac_ros_sweep_phase1.md
#   docs/isaac_ros_sweep_phase2.md
#   docs/isaac_ros_sweep_phase3.md
#   docs/isaac_ros_overnight_summary.md  (final 3-table report)
# =============================================================================

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-300}

DATASET_MOUNTS=()
[ -d /home/tue037807/r2bdataset2023_v3 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2023_v3:/host/r2b_2023:ro )
[ -d /home/tue037807/r2bdataset2024_v1 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2024_v1:/host/r2b_2024:ro )

log() { echo "[overnight] $*" >&2; }

PHASE1_MD="$REPO_ROOT/docs/isaac_ros_sweep_phase1.md"
PHASE2_MD="$REPO_ROOT/docs/isaac_ros_sweep_phase2.md"
PHASE3_MD="$REPO_ROOT/docs/isaac_ros_sweep_phase3.md"
PHASE1_JSON="$REPO_ROOT/tests/sweep_phase1.json"
PHASE2_JSON="$REPO_ROOT/tests/sweep_phase2.json"
PHASE3_JSON="$REPO_ROOT/tests/sweep_phase3.json"

run_smoke() {
    # $1 = image, $2 = bench, $3 = script, $4 = log_path, $5 = optional wrap
    local IMG="$1" BENCH="$2" SCRIPT="$3" LOGP="$4" WRAP="${5:-}"
    : > "$LOGP"
    local SCRIPT_PATH="/root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCH}_benchmark/scripts/${SCRIPT}.py"
    local INNER_CMD
    if [ -n "$WRAP" ]; then
        INNER_CMD="$WRAP launch_test $SCRIPT_PATH"
    else
        INNER_CMD="launch_test $SCRIPT_PATH"
    fi
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
        -v /tmp/fish_traces_collect:/root/fish_traces \
        -e ISAAC_ROS_ACCEPT_EULA=1 \
        "$IMG" \
        bash -lc "
            DEST=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST
            for src in /host/r2b_2023 /host/r2b_2024; do
                [ -d \$src ] || continue
                for bag in \$src/*; do
                    [ -d \$bag ] || continue
                    ln -sf \$bag \$DEST/\$(basename \$bag) 2>/dev/null
                done
            done
            $INNER_CMD 2>&1
        " > "$LOGP" 2>&1
    return $?
}

emit_metric() {
    # Read fps from a smoke log
    local LOGP="$1"
    python3 -c "
import re
try:
    with open('$LOGP', 'rb') as f: data = f.read()
    text = ''.join(chr(b) for b in data if b == 10 or 32 <= b < 127)
    m = re.search(r'Mean Frame Rate \(fps\)\s*:\s*([0-9]+\.[0-9]+)', text)
    print(m.group(1) if m else '')
except: print('')
"
}

# Map of failing benchmark → models_install package + workload repo
declare -A MODELS_PKG=(
  [isaac_ros_bi3d]="isaac_ros_bi3d_models_install:isaac_ros_depth_segmentation"
  [isaac_ros_bi3d_freespace]="isaac_ros_bi3d_models_install:isaac_ros_depth_segmentation"
  [isaac_ros_centerpose]="isaac_ros_centerpose_models_install:isaac_ros_pose_estimation"
  [isaac_ros_detectnet]="isaac_ros_peoplenet_models_install:isaac_ros_object_detection"
  [isaac_ros_dope]="isaac_ros_dope_models_install:isaac_ros_pose_estimation"
  [isaac_ros_ess]="isaac_ros_ess_models_install:isaac_ros_dnn_stereo_depth"
  [isaac_ros_foundationpose]="isaac_ros_foundationpose_models_install:isaac_ros_pose_estimation"
  [isaac_ros_pynitros]="isaac_ros_peoplesemsegnet_models_install:isaac_ros_image_segmentation"
  [isaac_ros_rtdetr]="isaac_ros_sdetr_models_install:isaac_ros_object_detection"
  [isaac_ros_segformer]="isaac_ros_peoplesemsegformer_models_install:isaac_ros_image_segmentation"
  [isaac_ros_segment_anything]="isaac_ros_mobilesam_models_install:isaac_ros_image_segmentation"
  [isaac_ros_tensor_rt]="isaac_ros_peoplesemsegnet_models_install:isaac_ros_image_segmentation"
  [isaac_ros_triton]="isaac_ros_dope_models_install:isaac_ros_pose_estimation"
  [isaac_ros_unet]="isaac_ros_peoplesemsegnet_models_install:isaac_ros_image_segmentation"
)

cat > "$PHASE1_MD" <<'EOF'
# Phase 1 — Get all 22 viable benchmarks to baseline-PASS

Incremental: For each smoke-FAIL benchmark, build a `-modelsfetched:latest`
image that runs the upstream `isaac_ros_*_models_install` asset scripts
(with `ISAAC_ROS_ACCEPT_EULA=1`) to fetch ONNX weights + model configs from
NGC, then smoke-test. Originals stay untouched.

| Image | Result | Detail |
|---|---|---|
EOF

# Phase 1: try to fetch models for each failing bench
echo '{"sweeps": {}}' > "$PHASE1_JSON"

phase1_bench() {
    local BENCH="$1"
    local SHORT="${BENCH#isaac_ros_}"
    local ORIG_RP="fish-r2b-${SHORT}-runtimepatched:latest"
    [ -z "${MODELS_PKG[$BENCH]:-}" ] && return
    local MAP="${MODELS_PKG[$BENCH]}"
    local PKG="${MAP%:*}" REPO="${MAP#*:}"
    local NEW_IMG="fish-r2b-${SHORT}-modelsfetched:latest"
    local DOCKERFILE=/tmp/Dockerfile.${BENCH}.modelsfetched

    log "[phase1] $BENCH — try $PKG (from $REPO)"
    if ! docker image inspect "$ORIG_RP" >/dev/null 2>&1; then
        # fall back to non-runtimepatched
        ORIG_RP="fish-r2b-${SHORT}:latest"
    fi
    if ! docker image inspect "$ORIG_RP" >/dev/null 2>&1; then
        echo "| \`$NEW_IMG\` | ⏭️ skip | no source image |" >> "$PHASE1_MD"
        return
    fi

    cat > "$DOCKERFILE" <<EOF
FROM $ORIG_RP
ENV ISAAC_ROS_ACCEPT_EULA=1
ENV ISAAC_ROS_WS=/root/ros_ws
RUN apt-get update -qq && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        wget curl jq && \\
    rm -rf /var/lib/apt/lists/*
RUN bash -lc 'set -e; \\
    REPO_DIR=/root/ros_ws/src/$REPO; \\
    PKG_DIR=\$REPO_DIR/$PKG; \\
    if [ ! -d "\$PKG_DIR" ]; then \\
        echo "[modelsfetched] cloning $REPO"; \\
        cd /root/ros_ws/src && git clone --depth 1 --branch release-3.2 https://github.com/NVIDIA-ISAAC-ROS/${REPO}.git 2>&1 | tail -3; \\
    fi; \\
    cd \$PKG_DIR && rm -f COLCON_IGNORE 2>/dev/null || true; \\
    for s in \$PKG_DIR/asset_scripts/install_*.sh; do \\
        [ -f "\$s" ] || continue; \\
        echo "[modelsfetched] running \$s"; \\
        chmod +x "\$s"; \\
        bash "\$s" 2>&1 | tail -20 || echo "[modelsfetched] script exit=\$?"; \\
    done; \\
    ls -la /root/ros_ws/isaac_ros_assets/ 2>&1 | tail -10; \\
    # Symlink the fetched models into the path the launch scripts expect
    if [ -d /root/ros_ws/isaac_ros_assets/models ]; then \\
        mkdir -p /root/ros_ws/src/ros2_benchmark/assets; \\
        ln -sfn /root/ros_ws/isaac_ros_assets/models /root/ros_ws/src/ros2_benchmark/assets/models 2>&1 || true; \\
    fi'
EOF
    local BUILD_LOG=/tmp/phase1-build-${BENCH}.log
    : > "$BUILD_LOG"
    if ! docker build -q -t "$NEW_IMG" -f "$DOCKERFILE" "$REPO_ROOT" > "$BUILD_LOG" 2>&1; then
        local err=$(grep -iE "error|fatal|cannot" "$BUILD_LOG" | head -1 | cut -c1-160)
        echo "| \`$NEW_IMG\` | ❌ image build fail | $err |" >> "$PHASE1_MD"
        return
    fi

    # Smoke test
    local SCRIPT
    SCRIPT=$(python3 -c "
import json
inv=json.load(open('$REPO_ROOT/tests/benchmark_inventory.json'))
for b in inv['benchmarks']:
    if b['name']=='$BENCH' and b.get('scripts'): print(b['scripts'][0]); break
")
    [ -z "$SCRIPT" ] && return
    local SMOKE_LOG=/tmp/phase1-smoke-${BENCH}.log
    run_smoke "$NEW_IMG" "$BENCH" "$SCRIPT" "$SMOKE_LOG"
    local rc=$?
    local fps
    fps=$(emit_metric "$SMOKE_LOG")
    if [ -n "$fps" ]; then
        echo "| \`$NEW_IMG\` | ✅ PASS | ${fps} fps (rc=$rc) |" >> "$PHASE1_MD"
        log "[phase1] $BENCH PASS ${fps} fps"
    else
        local err=$(grep -m1 -iE "error|fatal|missing|No such" "$SMOKE_LOG" 2>/dev/null | head -1 | cut -c1-160)
        echo "| \`$NEW_IMG\` | ❌ FAIL | rc=$rc — $err |" >> "$PHASE1_MD"
        log "[phase1] $BENCH FAIL: $err"
    fi
}

for bench in "${!MODELS_PKG[@]}"; do
    phase1_bench "$bench"
done

# Special case: occupancy_grid_localizer (no models needed; ament resource missing)
# Leave as-is for now; document.
echo "| \`fish-r2b-occupancy_grid_localizer\` | ❌ pending | needs r2b_galileo dataset overlay (no models_install) |" >> "$PHASE1_MD"

log "[phase1] done"

# ---------------------------------------------------------------------------
# Phase 2: For each baseline-PASS image, run smoke with FISH properly activated.
# ---------------------------------------------------------------------------
cat > "$PHASE2_MD" <<'EOF'
# Phase 2 — Run each baseline-PASS benchmark under FISH (properly activated)

For each image that produced a benchmark metric in baseline (Phase 0 originals
+ Phase 1 -modelsfetched variants), build a `-fishtraced:latest` overlay that:
- Prepends `/opt/ros/humble/fish/bin` to `PATH` (FISH ros2 wrapper takes priority)
- Wraps `launch_test` with `trace_session.sh start_session` / `stop_session`
- Captures `~/fish_traces/<session>/` to the host

Result columns:
- **benchmark output**: did launch_test emit "Mean Frame Rate (fps)"?
- **fish data**: are `ros2/`, `nsys/`, `fishlog/`, `snapshot/` populated under fish_traces?

| Image | benchmark output | fish data | Detail |
|---|---|---|---|
EOF

# Discover all PASS images (originals from baseline + new modelsfetched)
PASS_IMAGES=()
# Originals that PASSed (from earlier sweep)
for b in apriltag dnn_image_encoder image_proc nitros_bridge nvblox stereo_image_proc visual_slam; do
    docker image inspect "fish-r2b-${b}:latest" >/dev/null 2>&1 && \
        PASS_IMAGES+=("fish-r2b-${b}:latest:isaac_ros_${b}")
done
# Modelsfetched
for img in $(docker images --format '{{.Repository}}' | grep '^fish-r2b-.*-modelsfetched$' || true); do
    short=$(echo "$img" | sed -E 's/^fish-r2b-//; s/-modelsfetched$//')
    bench="isaac_ros_${short}"
    PASS_IMAGES+=("${img}:latest:${bench}")
done

phase2_image() {
    local IMG="$1" BENCH="$2"
    local SHORT="${BENCH#isaac_ros_}"
    local NEW_IMG="${IMG%:latest}-fishtraced:latest"
    local DOCKERFILE=/tmp/Dockerfile.${BENCH}.fishtraced
    cat > "$DOCKERFILE" <<EOF
FROM $IMG
# Make the FISH-wrapped ros2 take priority on PATH so any ros2 launch invocation
# is intercepted. (launch_test is python-direct, so we wrap manually below.)
ENV PATH=/opt/ros/humble/fish/bin:\$PATH
ENV FISH_ACTIVE=1
EOF
    local BUILD_LOG=/tmp/phase2-build-${BENCH}.log
    : > "$BUILD_LOG"
    if ! docker build -q -t "$NEW_IMG" -f "$DOCKERFILE" "$REPO_ROOT" > "$BUILD_LOG" 2>&1; then
        echo "| \`$NEW_IMG\` | ❌ build fail | — | $(head -1 $BUILD_LOG | cut -c1-160) |" >> "$PHASE2_MD"
        return
    fi

    # Smoke with manual FISH wrap
    local SCRIPT
    SCRIPT=$(python3 -c "
import json
inv=json.load(open('$REPO_ROOT/tests/benchmark_inventory.json'))
for b in inv['benchmarks']:
    if b['name']=='$BENCH' and b.get('scripts'): print(b['scripts'][0]); break
")
    [ -z "$SCRIPT" ] && return
    local SMOKE_LOG=/tmp/phase2-smoke-${BENCH}.log
    # Clear collect dir first
    rm -rf /tmp/fish_traces_collect/${BENCH}* 2>/dev/null
    mkdir -p /tmp/fish_traces_collect
    : > "$SMOKE_LOG"
    local SCRIPT_PATH="/root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCH}_benchmark/scripts/${SCRIPT}.py"
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
        -v /tmp/fish_traces_collect:/root/fish_traces \
        -e ISAAC_ROS_ACCEPT_EULA=1 \
        -e FISH_BENCH_NAME=${BENCH} \
        "$NEW_IMG" \
        bash -lc "
            DEST=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST
            for src in /host/r2b_2023 /host/r2b_2024; do
                [ -d \$src ] || continue
                for bag in \$src/*; do
                    [ -d \$bag ] || continue
                    ln -sf \$bag \$DEST/\$(basename \$bag) 2>/dev/null
                done
            done
            source /opt/ros/humble/fish/scripts/trace_session.sh
            start_session
            launch_test $SCRIPT_PATH 2>&1
            BENCH_RC=\$?
            stop_session 2>&1
            exit \$BENCH_RC
        " > "$SMOKE_LOG" 2>&1
    local rc=$?

    # Bench output?
    local fps
    fps=$(emit_metric "$SMOKE_LOG")
    local out_emoji="❌"
    [ -n "$fps" ] && out_emoji="✅ ${fps}fps"

    # FISH data?
    local fish_sess_count
    fish_sess_count=$(find /tmp/fish_traces_collect -maxdepth 1 -type d -name 'fish_*' 2>/dev/null | wc -l)
    local fish_emoji="❌"
    local fish_detail="no session dir"
    if [ "$fish_sess_count" -gt 0 ]; then
        local latest=$(ls -td /tmp/fish_traces_collect/fish_* 2>/dev/null | head -1)
        local n_files=$(find "$latest" -type f 2>/dev/null | wc -l)
        if [ "$n_files" -gt 0 ]; then
            fish_emoji="✅"
            fish_detail="$(basename "$latest") (${n_files} files)"
            # Tag it so phase 3 can find this benchmark's traces
            mv "$latest" "/tmp/fish_traces_collect/${BENCH}_$(basename "$latest")" 2>/dev/null || true
        fi
    fi

    local err=""
    [ -z "$fps" ] && err=$(grep -m1 -iE "error|fatal|missing|traceback" "$SMOKE_LOG" 2>/dev/null | head -1 | cut -c1-120)
    echo "| \`$NEW_IMG\` | ${out_emoji} | ${fish_emoji} ${fish_detail} | rc=$rc ${err:+— $err} |" >> "$PHASE2_MD"
    log "[phase2] $BENCH out=${out_emoji} fish=${fish_emoji}"
}

for entry in "${PASS_IMAGES[@]}"; do
    img="${entry%:*}"
    bench="${entry##*:}"
    phase2_image "$img:latest" "$bench"
done

log "[phase2] done"

# ---------------------------------------------------------------------------
# Phase 3: Digest fish_traces with FISH's ingest pipeline → InfluxDB session
# ---------------------------------------------------------------------------
cat > "$PHASE3_MD" <<'EOF'
# Phase 3 — Digest FISH-traced sessions into InfluxDB

For each Phase 2 benchmark that produced BOTH benchmark output AND FISH trace
data, run FISH's ingest pipeline → record InfluxDB session ID.

| Benchmark | Session dir | Ingest result | Notes |
|---|---|---|---|
EOF

phase3_bench() {
    local BENCH="$1"
    local trace_dir
    trace_dir=$(find /tmp/fish_traces_collect -maxdepth 1 -type d -name "${BENCH}_fish_*" 2>/dev/null | head -1)
    [ -z "$trace_dir" ] && { echo "| \`$BENCH\` | — | ⏭️ skip | no trace dir |" >> "$PHASE3_MD"; return; }

    # Run ingest on the host (FISH's python tools are in $REPO_ROOT/python/fish)
    local INGEST_LOG=/tmp/phase3-ingest-${BENCH}.log
    : > "$INGEST_LOG"
    if [ -f "$REPO_ROOT/postprocess/ingest.py" ]; then
        cd "$REPO_ROOT" && python3 postprocess/ingest.py "$trace_dir" > "$INGEST_LOG" 2>&1 || true
        cd - >/dev/null
    elif [ -f "$REPO_ROOT/scripts/ingest.py" ]; then
        python3 "$REPO_ROOT/scripts/ingest.py" "$trace_dir" > "$INGEST_LOG" 2>&1 || true
    fi
    local session_id
    session_id=$(grep -iE "session.id|inserted|ingested|influx" "$INGEST_LOG" 2>/dev/null | head -3 | tr '\n' ';' | cut -c1-200)
    [ -z "$session_id" ] && session_id="ingest log empty/unsupported"
    echo "| \`$BENCH\` | \`$(basename "$trace_dir")\` | $session_id |  |" >> "$PHASE3_MD"
}

# discover traced benchmarks from phase 2 collect
for trace_dir in /tmp/fish_traces_collect/*_fish_*; do
    [ -d "$trace_dir" ] || continue
    bench=$(basename "$trace_dir" | sed -E 's/_fish_.*//')
    phase3_bench "$bench"
done

log "[phase3] done"

# Final summary
SUMMARY="$REPO_ROOT/docs/isaac_ros_overnight_summary.md"
cat > "$SUMMARY" <<'EOF'
# Overnight sweep — final 3-table summary

EOF
cat "$PHASE1_MD" >> "$SUMMARY"
echo "" >> "$SUMMARY"
cat "$PHASE2_MD" >> "$SUMMARY"
echo "" >> "$SUMMARY"
cat "$PHASE3_MD" >> "$SUMMARY"

log "overnight sweep complete — see docs/isaac_ros_overnight_summary.md"
