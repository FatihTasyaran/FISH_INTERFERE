#!/bin/bash
# =============================================================================
# build_fish_overlay.sh — for each smoke-PASS image (original or runtime-
# patched), derive a -fishinstalled:latest variant where FISH is ACTIVATED
# at launch (FISH framework is already in fish-r2b-tensorrt-base, we just
# need to source its env). Smoke-test under FISH. Compare metrics with
# baseline (FISH-off) smoke results.
#
# Result: docs/isaac_ros_fish_overlay_results.md with preliminary overhead
# table. Logs of failures kept verbatim for tomorrow.
# =============================================================================

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY_MD="$REPO_ROOT/docs/isaac_ros_fish_overlay_results.md"
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-240}
BASELINE_JSON="$REPO_ROOT/tests/smoke_results.json"
OVERLAY_JSON="$REPO_ROOT/tests/fish_overlay_results.json"

DATASET_MOUNTS=()
[ -d /home/tue037807/r2bdataset2023_v3 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2023_v3:/host/r2b_2023:ro )
[ -d /home/tue037807/r2bdataset2024_v1 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2024_v1:/host/r2b_2024:ro )

log() { echo "[fish-overlay] $*"; }

# Discover all PASSing images: originals + -runtimepatched (after rp sweep)
discover_pass_images() {
    # Originals from smoke_results.json
    PASS_BENCHES=()
    python3 -c "
import json
d = json.load(open('$BASELINE_JSON'))
for k, v in d['smokes'].items():
    if 'PASS' in v.get('result',''):
        print(k)
" > /tmp/pass_originals.txt 2>/dev/null || true

    # -runtimepatched images from docker (look for the suffix)
    docker images --format '{{.Repository}}' 2>/dev/null | \
        grep '^fish-r2b-.*-runtimepatched$' > /tmp/pass_rp.txt || true
}

build_fish_overlay() {
    local IMG="$1"            # fish-r2b-<short>[-runtimepatched]:latest
    local BENCH="$2"          # isaac_ros_<bench>
    local NEW_IMG="${IMG%:latest}-fishinstalled:latest"
    local DOCKERFILE=/tmp/Dockerfile.${BENCH}.fishoverlay

    cat > "$DOCKERFILE" <<EOF
FROM $IMG
# FISH framework is already installed at /opt/ros/humble/fish. Auto-source it
# so any subsequent ros2/launch_test invocation gets the LTTng wrapper +
# nsys-prefixed component_container substitution active.
RUN printf '%s\n' \\
    '# fish overlay — auto-source FISH framework on shell entry' \\
    'if [ -f /opt/ros/humble/fish/scripts/fish_env.sh ]; then' \\
    '    source /opt/ros/humble/fish/scripts/fish_env.sh' \\
    'fi' \\
    > /etc/profile.d/fish-activate.sh && \\
    chmod +x /etc/profile.d/fish-activate.sh
ENV FISH_ACTIVE=1
EOF

    log "build $NEW_IMG"
    local BUILD_LOG=/tmp/fish-overlay-build-${BENCH}.log
    : > "$BUILD_LOG"
    if ! docker build -q -t "$NEW_IMG" -f "$DOCKERFILE" "$REPO_ROOT" > "$BUILD_LOG" 2>&1; then
        log "  BUILD FAIL"
        echo "| \`$NEW_IMG\` | ❌ build fail | $(head -1 $BUILD_LOG | cut -c1-160) |" >> "$OVERLAY_MD"
        return
    fi

    # Smoke test
    local SCRIPT
    SCRIPT=$(python3 -c "
import json
inv = json.load(open('$REPO_ROOT/tests/benchmark_inventory.json'))
for b in inv['benchmarks']:
    if b['name'] == '$BENCH' and b.get('scripts'):
        print(b['scripts'][0]); break
")
    [ -z "$SCRIPT" ] && return
    local SMOKE_LOG=/tmp/smoke-fish-${BENCH}.log
    : > "$SMOKE_LOG"
    log "  smoke under FISH"
    local start_ts=$(date +%s)
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
        -v /tmp/fish_traces:/root/fish_traces \
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

    # Extract metric
    local fps_fish=$(grep -m1 "Mean Frame Rate" "$SMOKE_LOG" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    local lat_fish=$(grep -m1 "First Sent to First Received Latency" "$SMOKE_LOG" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    local fps_base=$(grep -m1 "Mean Frame Rate" "/tmp/smoke-${BENCH}.log" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
    local lat_base=$(grep -m1 "First Sent to First Received Latency" "/tmp/smoke-${BENCH}.log" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
    [ -z "$fps_base" ] && fps_base="?"
    [ -z "$lat_base" ] && lat_base="?"

    if [ -n "$fps_fish" ]; then
        # Compute overhead
        local overhead="—"
        if [ "$fps_base" != "?" ]; then
            overhead=$(python3 -c "
b=$fps_base; f=$fps_fish
if b>0: print(f'{(b-f)/b*100:.1f}%')
else: print('—')
")
        fi
        echo "| \`$NEW_IMG\` | ✅ PASS-with-FISH | base ${fps_base} fps → fish ${fps_fish} fps (overhead ${overhead}) ; lat ${lat_base}→${lat_fish} ms |" >> "$OVERLAY_MD"
        log "  → ✅ PASS (base $fps_base → fish $fps_fish, $overhead overhead)"
    elif [ $rc -eq 124 ]; then
        echo "| \`$NEW_IMG\` | ⚠️ TIMEOUT | ${dur}s under FISH; log /tmp/smoke-fish-${BENCH}.log |" >> "$OVERLAY_MD"
        log "  → ⚠️ TIMEOUT"
    else
        local err=$(grep -m1 -iE "error|fatal|missing|traceback|terminate" "$SMOKE_LOG" 2>/dev/null | head -1 | cut -c1-160)
        [ -z "$err" ] && err="rc=$rc, no metric (FISH active)"
        echo "| \`$NEW_IMG\` | ❌ fail under FISH | $err — full log /tmp/smoke-fish-${BENCH}.log |" >> "$OVERLAY_MD"
        log "  → ❌ FAIL under FISH ($err)"
    fi
}

cat > "$OVERLAY_MD" <<'EOF'
# Isaac ROS benchmark — FISH overlay smoke results

> For each smoke-PASSing image (original or `-runtimepatched`), derive a
> `-fishinstalled` variant that auto-sources FISH at shell entry. Then run the
> same launch_test under FISH and compare metrics.
>
> FISH framework was already baked into `fish-r2b-tensorrt-base` (inherited
> from fish-base); the overlay only adds an `/etc/profile.d/fish-activate.sh`
> that sources FISH's env, plus `ENV FISH_ACTIVE=1` so the launch_wrap
> monkey-patch can detect it.
>
> Pass = benchmark metric still emits with FISH active.
> Fail = log captured verbatim in /tmp/smoke-fish-<bench>.log for tomorrow.

| Image | Smoke result | Detail |
|---|---|---|
EOF

discover_pass_images

# Originals
while read -r bench; do
    [ -z "$bench" ] && continue
    short="${bench#isaac_ros_}"
    img="fish-r2b-${short}:latest"
    if docker image inspect "$img" >/dev/null 2>&1; then
        build_fish_overlay "$img" "$bench"
    fi
done < /tmp/pass_originals.txt

# Runtimepatched (each one needs its bench name reverse-derived)
while read -r rp_img; do
    [ -z "$rp_img" ] && continue
    short=$(echo "$rp_img" | sed -E 's/^fish-r2b-//; s/-runtimepatched$//')
    bench="isaac_ros_${short}"
    full_img="${rp_img}:latest"
    if docker image inspect "$full_img" >/dev/null 2>&1; then
        build_fish_overlay "$full_img" "$bench"
    fi
done < /tmp/pass_rp.txt

log "FISH overlay sweep complete"
