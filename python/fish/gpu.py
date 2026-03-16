"""
FISH GPU profiling module.

Provides:
  - GPU process detection via /proc/*/maps scanning
  - Kill-and-relaunch with nsys profiling
  - Background daemon that auto-detects and relaunches GPU processes
  - LTTng-compatible event logging for post-processing correlation

Output structure (per session):
  /tmp/fish_traces/<session>/
    ├── ros2/       — managed by trace_session.sh
    ├── nsys/       — nsys reports written here
    └── fishlog/    — FISH event logs written here
"""

import fcntl
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Default nsys profile flags for FISH GPU profiling.
# Designed for model extraction (overhead acceptable).
NSYS_DEFAULT_FLAGS = [
    "--trace=cuda,nvtx",
    "--cuda-memory-usage=true",
    "--cudabacktrace=kernel,memory,sync",
    "--python-backtrace=cuda",
    "--python-sampling=true",
    "--pytorch=autograd-nvtx",
    "--sample=process-tree",
    "--cpuctxsw=none",
    "--force-overwrite=true",
    "--stats=true",
    "--export=sqlite",
    "--show-output=true",
]

# Libraries that indicate GPU compute usage
CUDA_LIBRARIES = [
    "libcuda.so",
    "libnvrtc",
    "libnvinfer",
    "libcudart",
    "libcublas",
    "libcudnn",
]

# Grace period for nsys to finish writing reports (seconds).
NSYS_REPORT_TIMEOUT = 120

FISH_SESSION_DIR_FILE = "/tmp/fish_session_dir"
FISH_SESSION_NAME_FILE = "/tmp/fish_session_name"
FISH_DAEMON_PID_FILE = "/tmp/fish_daemon_pid"
FISH_DAEMON_LOCK_FILE = "/tmp/fish_daemon.lock"


def get_session_dir() -> str:
    """Read the current session directory from the shared state file."""
    try:
        return Path(FISH_SESSION_DIR_FILE).read_text().strip()
    except FileNotFoundError:
        fallback = "/tmp/fish_traces/unsorted"
        os.makedirs(fallback, exist_ok=True)
        return fallback


def get_nsys_dir() -> str:
    """Get the nsys output directory for the current session."""
    d = os.path.join(get_session_dir(), "nsys")
    os.makedirs(d, exist_ok=True)
    return d


def get_fishlog_dir() -> str:
    """Get the fishlog output directory for the current session."""
    d = os.path.join(get_session_dir(), "fishlog")
    os.makedirs(d, exist_ok=True)
    return d


@dataclass
class GpuProcess:
    """Represents a process using the GPU."""
    pid: int
    cmdline: str
    process_name: str
    gpu_libs: list


class FishLogger:
    """
    Logs FISH events with LTTng-compatible monotonic timestamps.

    Output format (one event per line):
        <monotonic_ns> <action> <killed_pid> <resurrected_pid> <process_name> <cmd>

    The monotonic clock matches LTTng's default clock, so events
    can be directly correlated with ros2_trace output in post-processing.
    """

    def __init__(self):
        log_dir = get_fishlog_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(log_dir, f"fish_events_{timestamp}.log")

        with open(self.log_path, "w") as f:
            f.write("# FISH event log\n")
            f.write("# Clock: CLOCK_MONOTONIC (same as LTTng)\n")
            f.write("# Format: <monotonic_ns> <action> <killed_pid> <resurrected_pid> <process_name> <cmd>\n")
            f.write("#\n")

        print(f"[FISH] Event log: {self.log_path}")

    def _monotonic_ns(self) -> int:
        """Get current CLOCK_MONOTONIC timestamp in nanoseconds."""
        return int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1_000_000_000)

    def log_kill(self, pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        line = f"{ts} kill {pid} - {process_name} {cmd}\n"
        with open(self.log_path, "a") as f:
            f.write(line)

    def log_resurrect(self, killed_pid: int, new_pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        line = f"{ts} resurrect {killed_pid} {new_pid} {process_name} {cmd}\n"
        with open(self.log_path, "a") as f:
            f.write(line)

    def log_detect(self, pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        line = f"{ts} detect {pid} - {process_name} {cmd}\n"
        with open(self.log_path, "a") as f:
            f.write(line)

    def log_daemon(self, message: str) -> None:
        ts = self._monotonic_ns()
        line = f"{ts} daemon - - fish_daemon {message}\n"
        with open(self.log_path, "a") as f:
            f.write(line)


def scan_gpu_processes() -> list[GpuProcess]:
    """
    Scan /proc/*/maps to find processes that have loaded CUDA libraries.
    Works inside containers where nvidia-smi cannot see local PIDs.
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

        found_libs = [lib for lib in CUDA_LIBRARIES if lib in maps_content]
        if not found_libs:
            continue

        try:
            with open(cmdline_path, "rb") as f:
                raw = f.read()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue

        if not cmdline:
            continue

        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                proc_name = f.read().strip()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            proc_name = cmdline.split()[0].split("/")[-1]

        skip_patterns = ["nsys", "fish.cli", "fish/python", "fish_daemon"]
        if any(skip in cmdline for skip in skip_patterns):
            continue

        results.append(GpuProcess(
            pid=pid,
            cmdline=cmdline,
            process_name=proc_name,
            gpu_libs=found_libs,
        ))

    return results


def get_all_descendant_pids(parent_pid: int) -> set[int]:
    """
    Get all descendant PIDs of a given parent process.
    Uses pgrep to recursively find children.
    """
    descendants = set()
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split("\n"):
            if line.isdigit():
                child_pid = int(line)
                descendants.add(child_pid)
                descendants.update(get_all_descendant_pids(child_pid))
    except Exception:
        pass
    return descendants


def display_gpu_processes(processes: list[GpuProcess]) -> None:
    """Print discovered GPU processes."""
    if not processes:
        print("[FISH] No GPU compute processes found in this container.")
        return

    print(f"\n[FISH] Found {len(processes)} GPU process(es):\n")
    for i, proc in enumerate(processes):
        libs = ", ".join(proc.gpu_libs)
        print(f"  [{i}] PID={proc.pid}  {proc.process_name}")
        print(f"      CMD: {proc.cmdline}")
        print(f"      GPU libs: {libs}")
        print()


def kill_process(pid: int, timeout: float = 5.0) -> bool:
    """Kill a process gracefully (SIGTERM), then forcefully (SIGKILL)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        print(f"[FISH] Permission denied killing PID {pid}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.2)
        except ProcessLookupError:
            return True

    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        return True
    except (ProcessLookupError, PermissionError):
        return True


def extract_short_name(cmdline: str) -> str:
    """Extract a meaningful short name from a command line string."""
    parts = cmdline.split()
    for part in parts:
        name = part.split("/")[-1]
        if name not in ("python3", "python", "usr", "bin", "env", ""):
            return name
    return "unknown"


def build_nsys_command(
    original_cmd: str,
    extra_flags: Optional[list[str]] = None,
) -> list[str]:
    """Build an nsys profile command wrapping the original launch command."""
    nsys_dir = get_nsys_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    short_name = extract_short_name(original_cmd)
    output_path = os.path.join(nsys_dir, f"nsys_{short_name}_{timestamp}")

    nsys_cmd = ["nsys", "profile"]
    nsys_cmd.extend(NSYS_DEFAULT_FLAGS)
    nsys_cmd.append(f"--output={output_path}")

    if extra_flags:
        nsys_cmd.extend(extra_flags)

    nsys_cmd.extend(original_cmd.split())
    return nsys_cmd


def relaunch_with_nsys(
    process: GpuProcess,
    logger: Optional[FishLogger] = None,
    extra_flags: Optional[list[str]] = None,
) -> Optional[subprocess.Popen]:
    """Kill a GPU process and relaunch it under nsys profiling."""
    print(f"[FISH] Relaunching '{process.process_name}' (PID {process.pid}) with nsys...")

    if logger:
        logger.log_kill(process.pid, process.process_name, process.cmdline)

    if not kill_process(process.pid):
        print("[FISH] ERROR: Failed to kill process")
        return None

    print("[FISH] Waiting for DDS recovery...")
    time.sleep(2)

    nsys_cmd = build_nsys_command(process.cmdline, extra_flags=extra_flags)
    print(f"[FISH] Executing: {' '.join(nsys_cmd)}")

    try:
        proc = subprocess.Popen(nsys_cmd)
        print(f"[FISH] nsys launched (PID {proc.pid})")

        if logger:
            logger.log_resurrect(
                process.pid, proc.pid, process.process_name, process.cmdline
            )

        return proc
    except FileNotFoundError:
        print("[FISH] ERROR: nsys not found. Is Nsight Systems installed?")
        return None
    except Exception as e:
        print(f"[FISH] ERROR: Failed to launch nsys: {e}")
        return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

def is_trace_session_active() -> bool:
    """Check if a FISH LTTng trace session is currently active."""
    try:
        name = Path(FISH_SESSION_NAME_FILE).read_text().strip()
        if not name:
            return False
        result = subprocess.run(
            ["lttng", "list", name],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except (FileNotFoundError, PermissionError):
        return False


def register_nsys_process_tree(nsys_proc: subprocess.Popen,
                                known_pids: set[int],
                                settle_time: float) -> None:
    """
    After launching nsys, wait for it to spawn child processes
    and register all of them in known_pids to prevent re-detection.
    """
    time.sleep(settle_time)

    known_pids.add(nsys_proc.pid)

    descendants = get_all_descendant_pids(nsys_proc.pid)
    for dpid in descendants:
        known_pids.add(dpid)
        print(f"[FISH] Registered nsys child process: PID={dpid}")

    # Safety net: mark ALL currently visible GPU processes as known
    for proc in scan_gpu_processes():
        if proc.pid not in known_pids:
            known_pids.add(proc.pid)
            print(f"[FISH] Registered post-relaunch GPU process: PID={proc.pid} {proc.process_name}")


def graceful_stop_nsys(nsys_children: list[subprocess.Popen],
                        logger: Optional[FishLogger] = None) -> None:
    """
    Gracefully stop all nsys child processes by sending SIGINT
    and waiting for report generation to complete.

    nsys responds to SIGINT by flushing trace buffers and writing
    the .nsys-rep and .sqlite report files. SIGTERM causes nsys to
    exit without writing reports — so we must use SIGINT here.
    """
    if not nsys_children:
        return

    active = [c for c in nsys_children if c.poll() is None]
    if not active:
        return

    # Phase 1: Send SIGINT to all active nsys processes
    for child in active:
        print(f"[FISH] Sending SIGINT to nsys PID {child.pid} (triggering report generation)")
        try:
            os.kill(child.pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass

    # Phase 2: Wait for each to finish writing reports
    for child in active:
        try:
            child.wait(timeout=NSYS_REPORT_TIMEOUT)
            print(f"[FISH] nsys PID {child.pid} finished (report written)")
            if logger:
                logger.log_daemon(f"nsys_report_complete pid={child.pid}")
        except subprocess.TimeoutExpired:
            print(f"[FISH] nsys PID {child.pid} timed out after {NSYS_REPORT_TIMEOUT}s, force killing")
            child.kill()
            child.wait()


def daemon_loop(poll_interval: float = 1.0, settle_time: float = 3.0) -> None:
    """
    FISH GPU daemon main loop.

    Polls for new GPU compute processes and automatically
    relaunches them with nsys profiling. Each GPU process is
    relaunched exactly once.
    """
    logger = FishLogger()
    logger.log_daemon("started")
    print(f"[FISH] GPU daemon started (poll={poll_interval}s, settle={settle_time}s)")

    known_pids: set[int] = set()
    nsys_children: list[subprocess.Popen] = []

    # Mark pre-existing GPU processes (started before daemon)
    initial = scan_gpu_processes()
    for proc in initial:
        known_pids.add(proc.pid)
        print(f"[FISH] Pre-existing GPU process: PID={proc.pid} {proc.process_name} (skipped)")

    def shutdown(signum, frame):
        logger.log_daemon("stopping")
        print("\n[FISH] Daemon shutting down, waiting for nsys reports...")
        graceful_stop_nsys(nsys_children, logger)
        try:
            os.remove(FISH_DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        try:
            os.remove(FISH_DAEMON_LOCK_FILE)
        except FileNotFoundError:
            pass
        logger.log_daemon("stopped")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        if not is_trace_session_active():
            time.sleep(poll_interval)
            continue

        # Clean up finished nsys children
        nsys_children = [c for c in nsys_children if c.poll() is None]

        # Scan for new GPU processes
        current = scan_gpu_processes()
        for proc in current:
            if proc.pid in known_pids:
                continue

            logger.log_detect(proc.pid, proc.process_name, proc.cmdline)
            print(f"[FISH] New GPU process detected: PID={proc.pid} {proc.process_name}")
            print(f"[FISH] Waiting {settle_time}s for node initialization...")
            time.sleep(settle_time)

            try:
                os.kill(proc.pid, 0)
            except ProcessLookupError:
                print(f"[FISH] PID {proc.pid} exited during settle, skipping")
                known_pids.add(proc.pid)
                continue

            try:
                with open(f"/proc/{proc.pid}/cmdline", "rb") as f:
                    raw = f.read()
                proc.cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            except (FileNotFoundError, ProcessLookupError):
                known_pids.add(proc.pid)
                continue

            nsys_proc = relaunch_with_nsys(proc, logger=logger)
            known_pids.add(proc.pid)

            if nsys_proc:
                nsys_children.append(nsys_proc)
                register_nsys_process_tree(nsys_proc, known_pids, settle_time)

        for proc in current:
            known_pids.add(proc.pid)

        time.sleep(poll_interval)


def start_daemon() -> int:
    """
    Start the FISH GPU daemon as a background process.

    Uses flock() on a lock file to guarantee only one daemon instance
    can start, even when multiple ros2 run commands race to call this.
    """
    # Acquire exclusive lock — if another process already holds it, return
    lock_fd = open(FISH_DAEMON_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another process is already starting the daemon
        lock_fd.close()
        print("[FISH] Daemon start already in progress")
        return 0

    # Check if already running (under the lock, so no TOCTOU race)
    if os.path.exists(FISH_DAEMON_PID_FILE):
        try:
            pid = int(Path(FISH_DAEMON_PID_FILE).read_text().strip())
            os.kill(pid, 0)
            print(f"[FISH] Daemon already running (PID {pid})")
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            return 0
        except (ProcessLookupError, ValueError):
            os.remove(FISH_DAEMON_PID_FILE)

    pid = os.fork()
    if pid > 0:
        # Parent — write PID file, release lock, return
        Path(FISH_DAEMON_PID_FILE).write_text(str(pid))
        print(f"[FISH] GPU daemon started in background (PID {pid})")
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        return 0
    else:
        # Child — become the daemon
        # Release the inherited lock fd
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        os.setsid()
        try:
            daemon_loop()
        except Exception as e:
            print(f"[FISH] Daemon error: {e}")
            return 1
    return 0


def stop_daemon() -> int:
    """Stop the running FISH GPU daemon."""
    if not os.path.exists(FISH_DAEMON_PID_FILE):
        print("[FISH] No daemon running")
        return 0

    try:
        pid = int(Path(FISH_DAEMON_PID_FILE).read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"[FISH] Daemon (PID {pid}) stopping, waiting for nsys reports...")
        # Wait for daemon to finish (it will wait for nsys reports internally)
        deadline = time.time() + NSYS_REPORT_TIMEOUT + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(1)
            except ProcessLookupError:
                break
        print("[FISH] Daemon stopped")
        try:
            os.remove(FISH_DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        try:
            os.remove(FISH_DAEMON_LOCK_FILE)
        except FileNotFoundError:
            pass
        return 0
    except ProcessLookupError:
        print("[FISH] Daemon was not running")
        try:
            os.remove(FISH_DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        return 0
    except ValueError:
        print("[FISH] Invalid daemon PID file")
        return 1


def daemon_status() -> int:
    """Check if the FISH GPU daemon is running."""
    if not os.path.exists(FISH_DAEMON_PID_FILE):
        print("[FISH] GPU daemon not running")
        return 1

    try:
        pid = int(Path(FISH_DAEMON_PID_FILE).read_text().strip())
        os.kill(pid, 0)
        print(f"[FISH] GPU daemon running (PID {pid})")
        return 0
    except (ProcessLookupError, ValueError):
        print("[FISH] GPU daemon not running (stale PID file)")
        try:
            os.remove(FISH_DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        return 1
