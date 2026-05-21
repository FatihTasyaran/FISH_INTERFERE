#!/bin/bash
# =============================================================================
# build_benchmarks_parallel.sh — fan-out builder for the inventory
# =============================================================================
# Runs build_benchmark_image.sh on every viable (and optionally vram_risky)
# entry from tests/benchmark_inventory.json, with a concurrency cap.
#
# Usage:
#   ./scripts/build_benchmarks_parallel.sh                  # default N=4 viable
#   ./scripts/build_benchmarks_parallel.sh --jobs 6         # tighter / wider cap
#   ./scripts/build_benchmarks_parallel.sh --include vram_risky
#   ./scripts/build_benchmarks_parallel.sh --skip isaac_ros_apriltag,isaac_ros_unet
#   ./scripts/build_benchmarks_parallel.sh --only isaac_ros_h264_decoder
#
# Per-benchmark logs land at /tmp/fish-r2b-build-<name>.log so you can
# `tail -f` any of them. Status JSON is updated atomically (flock).
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INV="$REPO_ROOT/tests/benchmark_inventory.json"

JOBS=4
INCLUDE_VRAM=0
SKIP_LIST=""
ONLY_LIST=""

while [ $# -gt 0 ]; do
    case "$1" in
        -j|--jobs)        shift; JOBS="$1" ;;
        --include)        shift; [ "$1" = "vram_risky" ] && INCLUDE_VRAM=1 ;;
        --skip)           shift; SKIP_LIST="$1" ;;
        --only)           shift; ONLY_LIST="$1" ;;
        -h|--help)
            sed -n '2,/^# ====/p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

# Build the list of benchmarks to process
mapfile -t QUEUE < <(python3 -c "
import json, os
inv = json.load(open('$INV'))
include_vram = $INCLUDE_VRAM == 1
skip = set('$SKIP_LIST'.split(','))
only = set('$ONLY_LIST'.split(','))
for b in inv['benchmarks']:
    name = b['name']
    cat = b['category']
    if only and name not in only:           # explicit only-list filter
        continue
    if name in skip:                        # explicit skip-list filter
        continue
    if cat == 'hardware_skip':              # never try these
        continue
    if cat == 'vram_risky' and not include_vram:
        continue
    print(name)
")

TOTAL=${#QUEUE[@]}
echo "[parallel] $TOTAL benchmark(s) queued, concurrency=$JOBS"
echo "[parallel] logs: /tmp/fish-r2b-build-<name>.log"
echo "[parallel] queue:"
printf '  - %s\n' "${QUEUE[@]}"
echo

# Fan-out with xargs -P. Each worker invokes build_benchmark_image.sh
# which writes its own log + updates status via the flock-protected
# _status_record.py.
printf '%s\n' "${QUEUE[@]}" \
  | xargs -I{} -P "$JOBS" bash -c '
        BENCH="$1"
        LOG="/tmp/fish-r2b-build-${BENCH}.log"
        echo "[parallel:start] $BENCH (log: $LOG)"
        if "'"$REPO_ROOT"'"/scripts/build_benchmark_image.sh "$BENCH" > "$LOG" 2>&1; then
            echo "[parallel:done]  $BENCH OK"
        else
            echo "[parallel:done]  $BENCH FAILED (see $LOG)"
        fi
    ' _ {}

echo
echo "[parallel] all workers finished."
echo "See tests/benchmark_status.md for the final table."
