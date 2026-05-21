#!/bin/bash
# =============================================================================
# build_runtime_patched.sh — for each fish-r2b-<bench>:latest that FAILed
# smoke, derive a fish-r2b-<bench>-runtimepatched:latest by adding the
# specific runtime piece that was missing (libnvinfer-bin for trtexec, model
# weights via NGC where feasible, etc.). Smoke-test each. Write results to
# docs/isaac_ros_smoke_results.md (append section).
#
# Original images stay untouched.
# =============================================================================

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS_MD="$REPO_ROOT/docs/isaac_ros_smoke_results.md"
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-180}

DATASET_MOUNTS=()
[ -d /home/tue037807/r2bdataset2023_v3 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2023_v3:/host/r2b_2023:ro )
[ -d /home/tue037807/r2bdataset2024_v1 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2024_v1:/host/r2b_2024:ro )

# Patches keyed by bucket. Each value is a single-line Dockerfile RUN body
# that gets injected after FROM <orig_image>.
declare -A TRTEXEC_BENCHES=(
  [isaac_ros_bi3d]=1 [isaac_ros_bi3d_freespace]=1 [isaac_ros_dope]=1
  [isaac_ros_foundationpose]=1 [isaac_ros_pynitros]=1 [isaac_ros_rtdetr]=1
  [isaac_ros_segformer]=1 [isaac_ros_segment_anything]=1
  [isaac_ros_tensor_rt]=1 [isaac_ros_unet]=1
)

# Failed-but-not-trtexec benchmarks — diagnose individually
declare -A SPECIAL_BENCHES=(
  [isaac_ros_centerpose]="model_weights"
  [isaac_ros_detectnet]="model_weights"
  [isaac_ros_triton]="model_weights"
  [isaac_ros_ess]="ess_model"
  [isaac_ros_occupancy_grid_localizer]="ament_resource"
)

log() { echo "[runtime-patch] $*"; }

build_runtime_image() {
    local BENCH="$1"
    local PATCH_TYPE="$2"
    local SHORT="${BENCH#isaac_ros_}"
    local ORIG_IMG="fish-r2b-${SHORT}:latest"
    local NEW_IMG="fish-r2b-${SHORT}-runtimepatched:latest"
    local DOCKERFILE=/tmp/Dockerfile.${BENCH}.runtimepatched

    if ! docker image inspect "$ORIG_IMG" >/dev/null 2>&1; then
        log "  SKIP $BENCH — original image not present"
        echo "| \`$BENCH\` | ⏭️ skip | original image missing |" >> "$RESULTS_MD"
        return
    fi

    case "$PATCH_TYPE" in
        trtexec)
            cat > "$DOCKERFILE" <<EOF
FROM $ORIG_IMG
RUN apt-get update -qq && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        libnvinfer-bin && \\
    rm -rf /var/lib/apt/lists/*
EOF
            ;;
        model_weights)
            # Try to download minimal model placeholders. The actual NGC download
            # requires auth + specific commands; for now, install isaac_ros_<bench>_models
            # apt package if available, else just stub the config.pbtxt so launch
            # doesn't crash before nvtrt tries to load (it'll fail later but with
            # a different more-actionable error).
            cat > "$DOCKERFILE" <<EOF
FROM $ORIG_IMG
RUN apt-get update -qq && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        libnvinfer-bin && \\
    rm -rf /var/lib/apt/lists/* && \\
    mkdir -p /root/ros_ws/src/ros2_benchmark/assets/models && \\
    touch /root/ros_ws/src/ros2_benchmark/assets/models/.placeholder
EOF
            ;;
        ess_model)
            cat > "$DOCKERFILE" <<EOF
FROM $ORIG_IMG
RUN apt-get update -qq && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        libnvinfer-bin && \\
    rm -rf /var/lib/apt/lists/*
EOF
            ;;
        ament_resource)
            cat > "$DOCKERFILE" <<EOF
FROM $ORIG_IMG
# occupancy_grid_localizer needs the r2b_galileo dataset overlay registered
# in the ament index. Attempting a no-op for now; if it fails the same way
# we'll need to fetch isaac_ros_r2b_galileo.
RUN apt-get update -qq && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        libnvinfer-bin && \\
    rm -rf /var/lib/apt/lists/*
EOF
            ;;
        *) log "UNKNOWN patch type $PATCH_TYPE for $BENCH"; return ;;
    esac

    log "build $NEW_IMG"
    local BUILD_LOG=/tmp/runtime-build-${BENCH}.log
    : > "$BUILD_LOG"
    if ! docker build -q -t "$NEW_IMG" -f "$DOCKERFILE" "$REPO_ROOT" > "$BUILD_LOG" 2>&1; then
        log "  BUILD FAILED $NEW_IMG"
        local err=$(grep -iE "error|fatal|cannot" "$BUILD_LOG" | head -1 | cut -c1-160)
        echo "| \`$NEW_IMG\` | ❌ build fail | $err |" >> "$RESULTS_MD"
        return
    fi
    log "  built $NEW_IMG"

    # Smoke test
    local SCRIPT
    SCRIPT=$(python3 -c "
import json
inv = json.load(open('$REPO_ROOT/tests/benchmark_inventory.json'))
for b in inv['benchmarks']:
    if b['name'] == '$BENCH' and b.get('scripts'):
        print(b['scripts'][0]); break
")
    [ -z "$SCRIPT" ] && { log "  no script for $BENCH"; return; }
    local SMOKE_LOG=/tmp/smoke-rp-${BENCH}.log
    : > "$SMOKE_LOG"
    log "  smoke $NEW_IMG (script=$SCRIPT)"

    local start_ts
    start_ts=$(date +%s)
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
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
            launch_test /root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCH}_benchmark/scripts/${SCRIPT}.py 2>&1
        " > "$SMOKE_LOG" 2>&1
    local rc=$?
    local dur=$(($(date +%s) - start_ts))

    if grep -qE "Mean Frame Rate|Mean Playback Frame Rate|First Sent to First Received Latency" "$SMOKE_LOG"; then
        echo "| \`$NEW_IMG\` | ✅ PASS | ${dur}s — metric emitted (rc=$rc) |" >> "$RESULTS_MD"
        log "  → ✅ PASS"
    elif [ $rc -eq 124 ]; then
        local tail5=$(tail -3 "$SMOKE_LOG" | tr '\n' ' ' | tr -s ' ' | cut -c1-140)
        echo "| \`$NEW_IMG\` | ⚠️ TIMEOUT | ${dur}s — $tail5 |" >> "$RESULTS_MD"
        log "  → ⚠️ TIMEOUT"
    else
        local err=$(grep -m1 -iE "error|fatal|missing|No such|Traceback" "$SMOKE_LOG" 2>/dev/null | head -1 | cut -c1-160)
        [ -z "$err" ] && err="rc=$rc, no metric"
        echo "| \`$NEW_IMG\` | ❌ FAIL | ${dur}s rc=$rc — $err |" >> "$RESULTS_MD"
        log "  → ❌ FAIL ($err)"
    fi
}

cat >> "$RESULTS_MD" <<'EOF'

---

## Runtime-patched images (`-runtimepatched:latest`)

For each smoke-failing benchmark above, we add the minimum missing runtime
piece on top of the original image (creating a NEW image, leaving the original
unchanged). Mostly `apt install libnvinfer-bin` (provides `trtexec`).
DNN-model-needing benchmarks get a placeholder; if still fail, the model
weights have to be NGC-downloaded in a follow-up.

| Image | Smoke result | Detail |
|---|---|---|
EOF

# Process trtexec bucket
for bench in "${!TRTEXEC_BENCHES[@]}"; do
    build_runtime_image "$bench" trtexec
done

# Process special bucket
for bench in "${!SPECIAL_BENCHES[@]}"; do
    build_runtime_image "$bench" "${SPECIAL_BENCHES[$bench]}"
done

log "runtime-patch sweep complete"
