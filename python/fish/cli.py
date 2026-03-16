"""
FISH CLI — Command-line interface for FISH profiling tools.

Invoked by the bash ros2 wrapper:
    ros2 run fish gpu              → list GPU processes
    ros2 run fish gpu list         → list GPU processes
    ros2 run fish gpu relaunch     → manual kill and relaunch
    ros2 run fish gpu daemon       → start background auto-relaunch daemon
    ros2 run fish gpu daemon stop  → stop the daemon
    ros2 run fish gpu daemon status → check daemon status
    ros2 run fish gpu daemon fg    → run daemon in foreground (debug)
"""

import sys

from fish.gpu import (
    daemon_status,
    daemon_loop,
    display_gpu_processes,
    relaunch_with_nsys,
    scan_gpu_processes,
    start_daemon,
    stop_daemon,
    FishLogger,
)


def cmd_gpu(args: list[str]) -> int:
    """GPU profiling subcommand dispatcher."""
    subcmd = args[0] if args else "list"

    if subcmd == "list":
        processes = scan_gpu_processes()
        display_gpu_processes(processes)
        return 0

    elif subcmd == "relaunch":
        processes = scan_gpu_processes()
        if not processes:
            print("[FISH] No GPU compute processes found.")
            return 1

        display_gpu_processes(processes)

        if len(processes) == 1:
            selection = 0
            print(f"[FISH] Auto-selecting: [{selection}]")
        else:
            try:
                raw = input("[FISH] Select process to relaunch [0]: ").strip()
                selection = int(raw) if raw else 0
            except (ValueError, EOFError):
                selection = 0

        if selection < 0 or selection >= len(processes):
            print(f"[FISH] Invalid selection: {selection}")
            return 1

        target = processes[selection]

        try:
            confirm = input(
                f"[FISH] Kill PID {target.pid} ({target.process_name}) "
                f"and relaunch with nsys? [y/N] "
            ).strip().lower()
        except EOFError:
            confirm = "n"

        if confirm != "y":
            print("[FISH] Cancelled.")
            return 0

        logger = FishLogger()
        nsys_proc = relaunch_with_nsys(target, logger=logger)

        if nsys_proc is None:
            return 1

        try:
            print("[FISH] Profiling active. Press Ctrl+C to stop nsys.")
            nsys_proc.wait()
        except KeyboardInterrupt:
            print("\n[FISH] Stopping nsys...")
            nsys_proc.terminate()
            nsys_proc.wait(timeout=10)

        print(f"[FISH] nsys exited with code {nsys_proc.returncode}")
        print(f"[FISH] Traces in /tmp/fish_traces/")
        return nsys_proc.returncode or 0

    elif subcmd == "daemon":
        daemon_cmd = args[1] if len(args) > 1 else "start"

        if daemon_cmd == "start":
            return start_daemon()
        elif daemon_cmd == "stop":
            return stop_daemon()
        elif daemon_cmd == "status":
            return daemon_status()
        elif daemon_cmd == "fg":
            # Run in foreground for debugging
            print("[FISH] Running daemon in foreground (Ctrl+C to stop)")
            try:
                daemon_loop()
            except KeyboardInterrupt:
                print("\n[FISH] Daemon stopped")
            return 0
        else:
            print(f"[FISH] Unknown daemon command: {daemon_cmd}")
            print("Available: start, stop, status, fg")
            return 1

    else:
        print(f"[FISH] Unknown gpu subcommand: {subcmd}")
        print("Available: list, relaunch, daemon")
        return 1


def main() -> int:
    """Main CLI dispatcher."""
    if len(sys.argv) < 2:
        print("FISH — Framework for Integrated Scheduling Hierarchy")
        print()
        print("Commands:")
        print("  gpu list            — List GPU compute processes")
        print("  gpu relaunch        — Manual kill and relaunch with nsys")
        print("  gpu daemon          — Start auto-relaunch daemon")
        print("  gpu daemon stop     — Stop the daemon")
        print("  gpu daemon status   — Check daemon status")
        print("  gpu daemon fg       — Run daemon in foreground (debug)")
        return 0

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "gpu":
        return cmd_gpu(args)
    else:
        print(f"[FISH] Unknown command: {cmd}")
        print("Available: gpu")
        return 1


if __name__ == "__main__":
    sys.exit(main())
