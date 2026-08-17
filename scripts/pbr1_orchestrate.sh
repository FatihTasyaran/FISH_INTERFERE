#!/bin/bash
# Orchestrator: walks /tmp/per_bench_routine_1/summary.csv as it grows.
# For each line, runs postprocess + sheet writer. Idempotent: re-runs are safe
# (postprocess re-writes summary, sheet writer reuses spreadsheet).
#
# Run in background. Stop with: touch /tmp/per_bench_routine_1/orchestrate.stop
set -u

DEST=/tmp/per_bench_routine_1
SUM=$DEST/summary.csv
DONE_FILE=$DEST/orchestrate_done.txt
STOP_FILE=$DEST/orchestrate.stop
touch "$DONE_FILE"

while true; do
    if [[ -f "$STOP_FILE" ]]; then
        echo "[orch] stop file present; exiting"
        rm -f "$STOP_FILE"
        exit 0
    fi
    if [[ -f "$SUM" ]]; then
        tail -n +2 "$SUM" | while IFS=, read -r name off_rc on_rc off_json on_json sess; do
            [[ -z "$name" ]] && continue
            if grep -qx "$name" "$DONE_FILE"; then continue; fi
            echo "[orch] $(date +%T) processing $name"
            python3 /home/tue037807/fish_interfere/scripts/pbr1_postprocess.py "$name" \
                > "$DEST/postprocess_${name}.log" 2>&1
            python3 /home/tue037807/fish_interfere/scripts/pbr1_sheet_writer.py "$name" \
                > "$DEST/sheetwrite_${name}.log" 2>&1
            echo "$name" >> "$DONE_FILE"
            echo "[orch] $(date +%T) DONE $name"
        done
    fi
    # Stop when sweep finishes AND all benchmarks processed
    if grep -q "ALL DONE" "$DEST/sweep.log" 2>/dev/null; then
        # final pass to catch the last benchmarks
        if [[ -f "$SUM" ]]; then
            csv_lines=$(($(wc -l < "$SUM") - 1))
            done_lines=$(wc -l < "$DONE_FILE")
            if (( csv_lines <= done_lines )); then
                echo "[orch] sweep ALL DONE and all benchmarks processed; exiting"
                exit 0
            fi
        fi
    fi
    sleep 30
done
