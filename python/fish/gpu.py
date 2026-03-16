"""
FISH GPU profiling module.

Provides:
  - GPU process detection via /proc/*/maps scanning
  - Kill-and-relaunch with nsys profiling
  - Background daemon that manages both GPU (nsys) and CPU (perf) profiling
  - LTTng-compatible event logging for post-processing correlation

The daemon uses a two-phase detection strategy:
  1. Detect new ROS 2 node processes
  2. Wait settle_time for initialization (CUDA libraries load during init)
  3. Re-check: if CUDA loaded → nsys kill-relaunch, else → perf attach

This avoids misclassifying GPU nodes as CPU-only when CUDA hasn't
loaded yet at detection time.

Output structure (per session):
  /tmp/fish_traces/<session>/
    ├── ros2/       — managed by trace_session.sh
    ├── nsys/       — nsys reports (.nsys-rep, .sqlite)
    ├── perf/       — perf call-graph data (.perf.data)
    └── fishlog/    — FISH event logs
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

from fish.settings import settings
from fish.perf import (
    attach_perf,
    stop_perf,
    scan_ros2_processes,
    display_ros2_processes,
    CUDA_LIBRARIES,
)


FISH_SESSION_DIR_FILE = "/tmp/fish_session_dir"
FISH_SESSION_NAME_FILE = "/tmp/fish_session_name"
FISH_DAEMON_PID_FILE = "/tmp/fish_daemon_pid"
FISH_DAEMON_LOCK_FILE = "/tmp/fish_daemon.lock"


def get_session_dir() -> str:
    """Read the current session directory from the shared state file."""
    try:
        return Path(FISH_SESSION_DIR_FILE).read_text().strip()
    except FileNotFoundError:
        fallback = settings.get("trace", "output_dir") + "/unsorted"
        os.makedirs(fallback, exist_ok=True)
        return fallback


def get_nsys_dir() -> str:
    d = os.path.join(get_session_dir(), "nsys")
    os.makedirs(d, exist_ok=True)
    return d


def get_fishlog_dir() -> str:
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

    Format: <monotonic_ns> <action> <killed_pid> <resurrected_pid> <process_name> <cmd>
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
        return int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1_000_000_000)

    def log_kill(self, pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        with open(self.log_path, "a") as f:
            f.write(f"{ts} kill {pid} - {process_name} {cmd}\n")

    def log_resurrect(self, killed_pid: int, new_pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        with open(self.log_path, "a") as f:
            f.write(f"{ts} resurrect {killed_pid} {new_pid} {process_name} {cmd}\n")

    def log_detect(self, pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        with open(self.log_path, "a") as f:
            f.write(f"{ts} detect {pid} - {process_name} {cmd}\n")

    def log_perf_attach(self, pid: int, process_name: str, cmd: str) -> None:
        ts = self._monotonic_ns()
        with open(self.log_path, "a") as f:
            f.write(f"{ts} perf_attach {pid} - {process_name} {cmd}\n")

    def log_daemon(self, message: str) -> None:
        ts = self._monotonic_ns()
        with open(self.log_path, "a") as f:
            f.write(f"{ts} daemon - - fish_daemon {message}\n")


def check_pid_has_cuda(pid: int) -> list[str]:
    """
    Check if a process has loaded CUDA libraries by reading /proc/PID/maps.

    Returns list of found CUDA library names, empty if none.
    """
    try:
        with open(f"/proc/{pid}/maps", "r") as f:
            maps_content = f.read()
        return [lib for lib in CUDA_LIBRARIES if lib in maps_content]
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        return []


def read_cmdline(pid: int) -> str:
    """Read the command line of a process from /proc/PID/cmdline."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        return ""


def scan_gpu_processes() -> list[GpuProcess]:
    """Scan /proc/*/maps to find processes with CUDA libraries loaded."""
    results = []

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue

        pid = int(entry)
        if pid == os.getpid() or pid == os.getppid():
            continue

        found_libs = check_pid_has_cuda(pid)
        if not found_libs:
            continue

        cmdline = read_cmdline(pid)
        if not cmdline:
            continue

        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                proc_name = f.read().strip()
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            proc_name = cmdline.split()[0].split("/")[-1]

        skip_patterns = ["nsys", "fish.cli", "fish/python", "fish_daemon", "perf record"]
        if any(skip in cmdline for skip in skip_patterns):
            continue

        results.append(GpuProcess(
            pid=pid, cmdline=cmdline, process_name=proc_name, gpu_libs=found_libs,
        ))

    return results


def get_all_descendant_pids(parent_pid: int) -> set[int]:
    """Get all descendant PIDs of a process recursively."""
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
    """Build an nsys profile command from settings."""
    nsys_dir = get_nsys_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    short_name = extract_short_name(original_cmd)
    output_path = os.path.join(nsys_dir, f"nsys_{short_name}_{timestamp}")

    nsys_cmd = ["nsys", "profile"]
    nsys_cmd.append(f"--trace={settings.get('nsys', 'trace')}")
    nsys_cmd.append(f"--cuda-memory-usage={settings.get('nsys', 'cuda_memory_usage')}")
    nsys_cmd.append(f"--cudabacktrace={settings.get('nsys', 'cudabacktrace')}")
    nsys_cmd.append(f"--python-backtrace={settings.get('nsys', 'python_backtrace')}")
    nsys_cmd.append(f"--python-sampling={settings.get('nsys', 'python_sampling')}")
    nsys_cmd.append(f"--pytorch={settings.get('nsys', 'pytorch')}")
    nsys_cmd.append(f"--sample={settings.get('nsys', 'sample')}")
    nsys_cmd.append(f"--cpuctxsw={settings.get('nsys', 'cpuctxsw')}")
    nsys_cmd.append(f"--export={settings.get('nsys', 'export')}")
    nsys_cmd.append("--force-overwrite=true")
    nsys_cmd.append("--stats=true")
    nsys_cmd.append("--show-output=true")
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
            logger.log_resurrect(process.pid, proc.pid, process.process_name, process.cmdline)
        return proc
    except FileNotFoundError:
        print("[FISH] ERROR: nsys not found.")
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
            ["lttng", "list", name], capture_output=True, text=True
        )
        return result.returncode == 0
    except (FileNotFoundError, PermissionError):
        return False


def register_nsys_process_tree(nsys_proc: subprocess.Popen,
                                known_pids: set[int],
                                settle_time: float) -> None:
    """Register nsys and all its children in known_pids to prevent re-detection."""
    time.sleep(settle_time)
    known_pids.add(nsys_proc.pid)

    descendants = get_all_descendant_pids(nsys_proc.pid)
    for dpid in descendants:
        known_pids.add(dpid)
        print(f"[FISH] Registered nsys child process: PID={dpid}")

    for proc in scan_gpu_processes():
        if proc.pid not in known_pids:
            known_pids.add(proc.pid)
            print(f"[FISH] Registered post-relaunch GPU process: PID={proc.pid} {proc.process_name}")


def graceful_stop_nsys(nsys_children: list[subprocess.Popen],
                        logger: Optional[FishLogger] = None) -> None:
    """Stop nsys processes with SIGINT and wait for report generation."""
    timeout = settings.getint("daemon", "nsys_report_timeout")
    active = [c for c in nsys_children if c.poll() is None]
    if not active:
        return

    for child in active:
        print(f"[FISH] Sending SIGINT to nsys PID {child.pid} (triggering report generation)")
        try:
            os.kill(child.pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass

    for child in active:
        try:
            child.wait(timeout=timeout)
            print(f"[FISH] nsys PID {child.pid} finished (report written)")
            if logger:
                logger.log_daemon(f"nsys_report_complete pid={child.pid}")
        except subprocess.TimeoutExpired:
            print(f"[FISH] nsys PID {child.pid} timed out, force killing")
            child.kill()
            child.wait()


def graceful_stop_perf(perf_children: list[subprocess.Popen]) -> None:
    """Stop all perf record processes with SIGINT and wait for data flush."""
    active = [c for c in perf_children if c.poll() is None]
    if not active:
        return

    for child in active:
        print(f"[FISH] Stopping perf PID {child.pid}")
        stop_perf(child, timeout=10.0)


def daemon_loop(poll_interval: float = None, settle_time: float = None) -> None:
    """
    FISH daemon main loop.

    Uses two-phase detection for new ROS 2 nodes:
      Phase 1: Detect new ROS 2 process (via rclcpp/rclpy in /proc/maps)
      Phase 2: Wait settle_time, then re-check for CUDA libraries
               → CUDA found: nsys kill-relaunch (GPU profiling)
               → No CUDA: perf attach (CPU call-graph sampling)

    This two-phase approach is necessary because Python-based GPU nodes
    (like YOLO) load CUDA libraries several seconds after process start,
    during torch/tensorrt import. Without the re-check, the daemon would
    misclassify them as CPU-only nodes.
    """
    if poll_interval is None:
        poll_interval = settings.getfloat("daemon", "poll_interval")
    if settle_time is None:
        settle_time = settings.getfloat("daemon", "settle_time")

    perf_auto_attach = settings.getboolean("perf", "auto_attach")

    logger = FishLogger()
    logger.log_daemon("started")
    print(f"[FISH] Daemon started (poll={poll_interval}s, settle={settle_time}s, perf={perf_auto_attach})")

    known_pids: set[int] = set()
    nsys_children: list[subprocess.Popen] = []
    perf_children: list[subprocess.Popen] = []

    # Mark pre-existing processes (started before daemon)
    for proc in scan_ros2_processes():
        known_pids.add(proc.pid)
        label = "GPU" if proc.is_gpu else "CPU"
        print(f"[FISH] Pre-existing {label} node: PID={proc.pid} {proc.process_name} (skipped)")

    def shutdown(signum, frame):
        logger.log_daemon("stopping")
        print("\n[FISH] Daemon shutting down...")
        graceful_stop_nsys(nsys_children, logger)
        graceful_stop_perf(perf_children)
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

        # Clean up finished children
        nsys_children = [c for c in nsys_children if c.poll() is None]
        perf_children = [c for c in perf_children if c.poll() is None]

        # Scan for new ROS 2 nodes (unified scan — both GPU and CPU)
        current_nodes = scan_ros2_processes()
        for proc in current_nodes:
            if proc.pid in known_pids:
                continue

            # Phase 1: New ROS 2 node detected
            logger.log_detect(proc.pid, proc.process_name, proc.cmdline)
            print(f"[FISH] New ROS 2 node detected: PID={proc.pid} {proc.process_name}")
            print(f"[FISH] Waiting {settle_time}s for initialization...")
            known_pids.add(proc.pid)
            time.sleep(settle_time)

            # Verify still alive
            try:
                os.kill(proc.pid, 0)
            except ProcessLookupError:
                print(f"[FISH] PID {proc.pid} exited during settle, skipping")
                continue

            # Re-read cmdline (may have changed during init)
            cmdline = read_cmdline(proc.pid)
            if cmdline:
                proc.cmdline = cmdline

            # Phase 2: Re-check for CUDA libraries after settle
            cuda_libs = check_pid_has_cuda(proc.pid)

            if cuda_libs:
                # GPU node → nsys kill-relaunch
                print(f"[FISH] PID {proc.pid} has CUDA ({', '.join(cuda_libs)}) → nsys relaunch")
                gpu_proc = GpuProcess(
                    pid=proc.pid,
                    cmdline=proc.cmdline,
                    process_name=proc.process_name,
                    gpu_libs=cuda_libs,
                )
                nsys_proc = relaunch_with_nsys(gpu_proc, logger=logger)
                if nsys_proc:
                    nsys_children.append(nsys_proc)
                    register_nsys_process_tree(nsys_proc, known_pids, settle_time)
            elif perf_auto_attach:
                # CPU-only node → perf attach (no kill)
                print(f"[FISH] PID {proc.pid} is CPU-only → perf attach")
                logger.log_perf_attach(proc.pid, proc.process_name, proc.cmdline)
                perf_proc = attach_perf(proc.pid, proc.process_name)
                if perf_proc:
                    perf_children.append(perf_proc)

        # Mark all current node PIDs as known
        for proc in current_nodes:
            known_pids.add(proc.pid)

        time.sleep(poll_interval)


def start_daemon() -> int:
    """Start the FISH daemon with flock to prevent duplicate instances."""
    lock_fd = open(FISH_DAEMON_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        print("[FISH] Daemon start already in progress")
        return 0

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
        Path(FISH_DAEMON_PID_FILE).write_text(str(pid))
        print(f"[FISH] Daemon started in background (PID {pid})")
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        return 0
    else:
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
    """Stop the FISH daemon and wait for reports."""
    if not os.path.exists(FISH_DAEMON_PID_FILE):
        print("[FISH] No daemon running")
        return 0

    timeout = settings.getint("daemon", "nsys_report_timeout")
    try:
        pid = int(Path(FISH_DAEMON_PID_FILE).read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"[FISH] Daemon (PID {pid}) stopping, waiting for reports...")
        deadline = time.time() + timeout + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(1)
            except ProcessLookupError:
                break
        print("[FISH] Daemon stopped")
        for f in (FISH_DAEMON_PID_FILE, FISH_DAEMON_LOCK_FILE):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        return 0
    except ProcessLookupError:
        print("[FISH] Daemon was not running")
        for f in (FISH_DAEMON_PID_FILE, FISH_DAEMON_LOCK_FILE):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        return 0
    except ValueError:
        print("[FISH] Invalid daemon PID file")
        return 1


def daemon_status() -> int:
    """Check if the FISH daemon is running."""
    if not os.path.exists(FISH_DAEMON_PID_FILE):
        print("[FISH] Daemon not running")
        return 1
    try:
        pid = int(Path(FISH_DAEMON_PID_FILE).read_text().strip())
        os.kill(pid, 0)
        print(f"[FISH] Daemon running (PID {pid})")
        return 0
    except (ProcessLookupError, ValueError):
        print("[FISH] Daemon not running (stale PID file)")
        try:
            os.remove(FISH_DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        return 1

