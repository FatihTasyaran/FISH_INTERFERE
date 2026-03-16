"""
FISH perf profiling module.

Attaches perf record to running ROS 2 nodes for CPU call-graph sampling.
Unlike nsys GPU profiling, perf can attach to running processes without
kill-relaunch — no disruption to the running system.

The call-graph data combined with LTTng callback timestamps provides
visibility into what each callback does internally:
  - LTTng: callback_start=T1, callback_end=T2  (exact boundaries)
  - perf:  call stacks sampled between T1 and T2 (internal structure)

Post-processing correlates perf samples with LTTng callback intervals
to reconstruct per-callback call graphs.
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fish.settings import settings

FISH_SESSION_DIR_FILE = "/tmp/fish_session_dir"

# Libraries that indicate a ROS 2 node (C++ or Python)
ROS2_LIBRARIES = [
    "librclcpp.so",
    "librclpy",
    "librcl.so",
    "rclpy",
]

# Libraries that indicate GPU compute usage (exclude these — handled by nsys)
CUDA_LIBRARIES = [
    "libcuda.so",
    "libnvrtc",
    "libnvinfer",
    "libcudart",
    "libcublas",
    "libcudnn",
]

# Processes to skip — these are wrappers, FISH internals, or profilers,
# not actual ROS 2 node executables.
SKIP_PATTERNS = [
    # Profiling tools
    "nsys",
    "perf record",
    "lttng",
    # FISH internals
    "fish.cli",
    "fish/python",
    "fish_daemon",
    "fish/bin/ros2",
    # ROS 2 CLI wrappers — these are Python scripts that launch the actual nodes,
    # not the nodes themselves. The real node binary is a child process.
    "/opt/ros/humble/bin/ros2",
    # tmuxinator / tmux internals
    "tmuxinator",
    "tmux",
]


def get_session_dir() -> str:
    """Read the current session directory from the shared state file."""
    try:
        return Path(FISH_SESSION_DIR_FILE).read_text().strip()
    except FileNotFoundError:
        fallback = "/tmp/fish_traces/unsorted"
        os.makedirs(fallback, exist_ok=True)
        return fallback


def get_perf_dir() -> str:
    """Get the perf output directory for the current session."""
    d = os.path.join(get_session_dir(), "perf")
    os.makedirs(d, exist_ok=True)
    return d


@dataclass
class Ros2Process:
    """Represents a ROS 2 process suitable for perf profiling."""
    pid: int
    cmdline: str
    process_name: str
    is_gpu: bool


def scan_ros2_processes() -> list[Ros2Process]:
    """
    Scan /proc/*/maps to find ROS 2 node processes.
    Classifies each as GPU or CPU-only based on loaded libraries.
    Filters out wrappers, CLI tools, and FISH internals.
    """
    results = []

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue

        pid = int(entry)
        if pid == os.getpid() or pid == os.getppid():
            continue

        maps_path = f"/proc/{pid}/maps"
        cmdline_path = f"/proc/{pid}/cmdline"

        try:
            with open(maps_path, "r") as f:
                maps_content = f.read()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue

        # Check if this is a ROS 2 node
        is_ros2 = any(lib in maps_content for lib in ROS2_LIBRARIES)
        if not is_ros2:
            continue

        # Read command line
        try:
            with open(cmdline_path, "rb") as f:
                raw = f.read()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue

        if not cmdline:
            continue

        # Skip wrappers, profilers, and FISH internals
        if any(skip in cmdline for skip in SKIP_PATTERNS):
            continue

        # Get short process name
        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                proc_name = f.read().strip()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            proc_name = cmdline.split()[0].split("/")[-1]

        is_gpu = any(lib in maps_content for lib in CUDA_LIBRARIES)

        results.append(Ros2Process(
            pid=pid,
            cmdline=cmdline,
            process_name=proc_name,
            is_gpu=is_gpu,
        ))

    return results


def attach_perf(
    pid: int,
    process_name: str,
    frequency: Optional[int] = None,
    call_graph_method: Optional[str] = None,
) -> Optional[subprocess.Popen]:
    """
    Attach perf record to a running process for call-graph sampling.

    No kill-relaunch needed — perf attaches non-intrusively to the
    running process.

    Args:
        pid: Target process ID.
        process_name: Short name for output file naming.
        frequency: Sampling frequency in Hz (from settings if None).
        call_graph_method: Backtrace method (from settings if None).

    Returns:
        Popen object for the perf process, or None on failure.
    """
    if frequency is None:
        frequency = settings.getint("perf", "sampling_frequency")
    if call_graph_method is None:
        call_graph_method = settings.get("perf", "call_graph_method")

    perf_dir = get_perf_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(perf_dir, f"perf_{process_name}_{pid}_{timestamp}.perf.data")

    cmd = [
        "perf", "record",
        "-g",
        f"--call-graph={call_graph_method}",
        f"-F{frequency}",
        "-p", str(pid),
        "-o", output_path,
    ]

    print(f"[FISH] Attaching perf to PID {pid} ({process_name}) @ {frequency}Hz")
    print(f"[FISH] Output: {output_path}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Brief check that perf started OK
        time.sleep(0.5)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode("utf-8", errors="replace")
            print(f"[FISH] WARNING: perf exited immediately: {stderr.strip()}")
            return None
        return proc
    except FileNotFoundError:
        print("[FISH] ERROR: perf not found. Install linux-tools-generic.")
        return None
    except Exception as e:
        print(f"[FISH] ERROR: Failed to attach perf: {e}")
        return None


def stop_perf(perf_proc: subprocess.Popen, timeout: float = 10.0) -> bool:
    """
    Stop a perf record process gracefully with SIGINT.

    SIGINT causes perf to flush and write the .perf.data file.

    Args:
        perf_proc: The perf Popen object.
        timeout: Seconds to wait for perf to finish writing.

    Returns:
        True if perf stopped cleanly.
    """
    if perf_proc.poll() is not None:
        return True

    try:
        os.kill(perf_proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        return True

    try:
        perf_proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        perf_proc.kill()
        perf_proc.wait()
        return False


def display_ros2_processes(processes: list[Ros2Process]) -> None:
    """Print discovered ROS 2 processes with GPU/CPU classification."""
    if not processes:
        print("[FISH] No ROS 2 node processes found.")
        return

    gpu_procs = [p for p in processes if p.is_gpu]
    cpu_procs = [p for p in processes if not p.is_gpu]

    if gpu_procs:
        print(f"\n[FISH] GPU nodes ({len(gpu_procs)}) — profiled by nsys:\n")
        for proc in gpu_procs:
            print(f"  PID={proc.pid}  {proc.process_name}")
            print(f"    CMD: {proc.cmdline}")

    if cpu_procs:
        print(f"\n[FISH] CPU nodes ({len(cpu_procs)}) — profiled by perf:\n")
        for proc in cpu_procs:
            print(f"  PID={proc.pid}  {proc.process_name}")
            print(f"    CMD: {proc.cmdline}")

    print()
