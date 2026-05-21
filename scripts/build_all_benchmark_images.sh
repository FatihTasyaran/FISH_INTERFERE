#!/bin/bash
# =============================================================================
# build_all_benchmark_images.sh — loop driver over the benchmark inventory
# =============================================================================
# Sequentially builds and smoke-tests every viable benchmark from
# tests/benchmark_inventory.json. Hardware-skip entries get marked
# `skipped` without attempting a build.
#
# Each result is appended to tests/benchmark_status.json + rendered into
# tests/benchmark_status.md. The script also `git commit`s the status
# file after each completion so progress is visible from outside.
#
# Usage:
#   ./scripts/build_all_benchmark_images.sh              # all viable
#   ./scripts/build_all_benchmark_images.sh --no-commit  # don't auto-commit
#   ./scripts/build_all_benchmark_images.sh --no-run     # build only, no run
#   ./scripts/build_all_benchmark_images.sh --include vram_risky  # also vram_risky
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INV="$REPO_ROOT/tests/benchmark_inventory.json"

DO_COMMIT=1
SKIP_RUN_FLAG=""
INCLUDE_VRAM_RISKY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-commit)         DO_COMMIT=0 ;;
        --no-run)            SKIP_RUN_FLAG="SKIP_RUN=1" ;;
        --include)           shift; [ "$1" = "vram_risky" ] && INCLUDE_VRAM_RISKY=1 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

# Build the working list
mapfile -t NAMES < <(python3 -c "
import json
with open('$INV') as f:
    inv = json.load(f)
for b in inv['benchmarks']:
    print(b['name'])
")

log()  { echo -e "\033[1;32m[all]\033[0m $*"; }
git_commit_status() {
    if [ "$DO_COMMIT" = "1" ]; then
        (cd "$REPO_ROOT" && \
         git add tests/benchmark_status.json tests/benchmark_status.md 2>/dev/null && \
         git commit --quiet -m "tests: benchmark status update — $(date -u +%H:%M:%SZ)" 2>/dev/null && \
         git push --quiet origin main 2>/dev/null) || true
    fi
}

TOTAL=${#NAMES[@]}
i=0
for name in "${NAMES[@]}"; do
    i=$((i+1))
    log "($i/$TOTAL) $name"
    if env $SKIP_RUN_FLAG "$REPO_ROOT/scripts/build_benchmark_image.sh" "$name"; then
        log "  result: ok / non-fatal"
    else
        log "  result: failed — continuing"
    fi
    git_commit_status
done

log "all done. See tests/benchmark_status.md"
