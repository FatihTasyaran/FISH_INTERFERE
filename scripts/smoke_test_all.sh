#!/bin/bash
# =============================================================================
# smoke_test_all.sh — for every built fish-r2b-<bench>:latest image, mount the
# r2b dataset and run launch_test of the bench's primary script. Record
# pass/fail/reason in docs/isaac_ros_smoke_results.md.
#
# Pass = launch_test exits 0 OR (exits non-zero AFTER producing a benchmark
# metric like "Mean Frame Rate" — i.e. the workload ran, just shutdown
# segfaulted, common with NITROS).
# Fail = no benchmark metric was emitted; record the last 10 stderr lines.
#
# Sequential to avoid GPU contention skewing results. No image modifications.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INVENTORY="$REPO_ROOT/tests/benchmark_inventory.json"
RESULTS_MD="$REPO_ROOT/docs/isaac_ros_smoke_results.md"
RESULTS_JSON="$REPO_ROOT/tests/smoke_results.json"
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-240}    # seconds per benchmark

DATASET_MOUNTS=()
[ -d /home/tue037807/r2bdataset2023_v3 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2023_v3:/host/r2b_2023:ro )
[ -d /home/tue037807/r2bdataset2024_v1 ] && \
    DATASET_MOUNTS+=( -v /home/tue037807/r2bdataset2024_v1:/host/r2b_2024:ro )

cat > "$RESULTS_MD" <<'EOF'
# Isaac ROS benchmark smoke-test results

> Each built `fish-r2b-<bench>:latest` image was launched with the r2b dataset
> bind-mounted; `launch_test` ran the benchmark's primary script under a wall-clock
> timeout. Pass = workload emitted a benchmark metric (Mean Frame Rate, latency,
> etc.) before exit. Fail = no metric, reason captured from log tail.

| Benchmark | Smoke result | Detail |
|---|---|---|
EOF

# Build the work list: for each image that exists, look up the benchmark
# entry's first script in inventory.json. Pass REPO_ROOT via env var.
REPO_ROOT="$REPO_ROOT" python3 - > /tmp/smoke_worklist <<'PYEOF'
import json, subprocess, os
inv = json.load(open(os.path.join(os.environ["REPO_ROOT"], "tests/benchmark_inventory.json")))
imgs = subprocess.check_output(["docker", "images", "--format", "{{.Repository}}"]).decode().splitlines()
imgs = {l for l in imgs if l.startswith("fish-r2b-") and l not in ("fish-r2b-base", "fish-r2b-tensorrt-base")}
for b in inv["benchmarks"]:
    short = b["name"].replace("isaac_ros_", "")
    img = f"fish-r2b-{short}"
    if img not in imgs:
        continue
    if not b.get("scripts"):
        print(f"{b['name']}|{img}|NO_SCRIPT|inventory has empty scripts list")
        continue
    print(f"{b['name']}|{img}|{b['scripts'][0]}|ok")
PYEOF

# Initialize JSON results
echo '{"smokes": {}}' > "$RESULTS_JSON"

while IFS='|' read -r BENCH IMG SCRIPT STATUS; do
    [ -z "$BENCH" ] && continue
    SHORT="${BENCH#isaac_ros_}"
    IMG_TAG="${IMG}:latest"
    LOG=/tmp/smoke-${BENCH}.log
    : > "$LOG"

    if [ "$STATUS" = "NO_SCRIPT" ]; then
        echo "[skip] $BENCH — $STATUS"
        echo "| \`$BENCH\` | ⏭️ skip | inventory has no scripts list |" >> "$RESULTS_MD"
        continue
    fi

    SCRIPT_PATH="/root/ros_ws/src/isaac_ros_benchmark/benchmarks/${BENCH}_benchmark/scripts/${SCRIPT}.py"
    echo "[run] $BENCH ($IMG_TAG, script=$SCRIPT, timeout=${SMOKE_TIMEOUT}s)"
    start=$(date +%s)
    timeout "$SMOKE_TIMEOUT" docker run --rm --gpus all --privileged --net host \
        "${DATASET_MOUNTS[@]}" \
        "$IMG_TAG" \
        bash -lc "
            DEST=/root/ros_ws/src/ros2_benchmark/assets/datasets/r2b_dataset
            mkdir -p \$DEST
            for src in /host/r2b_2023 /host/r2b_2024; do
                [ -d \$src ] || continue
                for bag in \$src/*; do
                    [ -d \$bag ] || continue
                    ln -sf \$bag \$DEST/\$(basename \$bag)
                done
            done
            launch_test $SCRIPT_PATH 2>&1
        " > "$LOG" 2>&1
    rc=$?
    end=$(date +%s)
    dur=$((end - start))

    # Detect metric emission
    if grep -qE "Mean Frame Rate|Mean Playback Frame Rate|First Sent to First Received Latency|Looping.Probing" "$LOG"; then
        result="✅ PASS"
        detail="${dur}s — benchmark metrics emitted (rc=$rc)"
    elif [ "$rc" -eq 124 ]; then
        result="⚠️ TIMEOUT"
        last5=$(grep -v "^$" "$LOG" | tail -5 | tr '\n' ' ' | tr -s ' ' | cut -c1-200)
        detail="${dur}s, no metric before timeout — tail: $last5"
    else
        result="❌ FAIL"
        # Find first error
        err=$(grep -m1 -iE "error|fatal|traceback|cannot|undefined|missing|No such" "$LOG" 2>/dev/null | head -1 | tr -s ' ' | cut -c1-200)
        [ -z "$err" ] && err="rc=$rc, no metric emitted"
        detail="${dur}s rc=$rc — $err"
    fi
    echo "  → $result ($detail)"
    echo "| \`$BENCH\` | $result | $detail |" >> "$RESULTS_MD"

    # Update JSON
    python3 -c "
import json
d = json.load(open('$RESULTS_JSON'))
d['smokes']['$BENCH'] = {'result': '$result', 'detail': '''$detail''', 'image': '$IMG_TAG', 'duration_s': $dur}
json.dump(d, open('$RESULTS_JSON', 'w'), indent=2)
"
done < /tmp/smoke_worklist

cat >> "$RESULTS_MD" <<'EOF'

## Summary
EOF
RESULTS_JSON="$RESULTS_JSON" python3 - >> "$RESULTS_MD" <<'PYEOF'
import json, os
d = json.load(open(os.environ["RESULTS_JSON"]))
buckets = {}
for k, v in d["smokes"].items():
    r = v["result"].split()[1] if " " in v["result"] else v["result"]
    buckets[r] = buckets.get(r, 0) + 1
print(f"- Total smoke tests: {len(d['smokes'])}")
for k, v in sorted(buckets.items()):
    print(f"- {k}: {v}")
PYEOF

echo
echo "smoke sweep complete. results: $RESULTS_MD"
