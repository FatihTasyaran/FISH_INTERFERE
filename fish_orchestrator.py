#!/usr/bin/env python3
"""
FISH Orchestrator — generic multi-container mission executor.

Reads a YAML mission definition, connects to Docker containers,
waits for FISH readiness, executes a scripted action sequence,
stops FISH, and collects traces into a unified compose session.

Usage:
    python3 fish_orchestrator.py examples/aas_single_drone.yaml
    python3 fish_orchestrator.py examples/aas_single_drone.yaml --dry-run

The compose session directory is the input for fish_compose.py,
which merges per-container models into a unified FISH graph with
the L-1 container layer.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_lines = []
_interrupted = False


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level:5s}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker_exec(container, cmd, timeout=None):
    """Run a command in a container. Returns (returncode, stdout, stderr)."""
    if isinstance(cmd, str):
        full = ["docker", "exec", container, "bash", "-c", cmd]
    else:
        full = ["docker", "exec", container] + cmd
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def docker_exec_stream(container, cmd, timeout=None):
    """Run a command in a container, streaming stdout/stderr to terminal."""
    full = ["docker", "exec", container, "bash", "-c", cmd]
    try:
        r = subprocess.run(full, timeout=timeout)
        return r.returncode
    except subprocess.TimeoutExpired:
        return -1


def container_running(name):
    """Check if a named container is running."""
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and "true" in r.stdout


def wait_for_containers(names, timeout=120):
    """Block until all named containers are running."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _interrupted:
            return False
        missing = [n for n in names if not container_running(n)]
        if not missing:
            return True
        time.sleep(2)
    log(f"Timeout — containers not running: {missing}", "ERROR")
    return False


# ---------------------------------------------------------------------------
# FISH helpers
# ---------------------------------------------------------------------------

def wait_fish_stable(container, timeout=180):
    """Wait for FISH daemon to signal system stability.

    Checks for /tmp/fish_system_stable (launch mode) or
    /tmp/fish_gpu_handler_complete (run mode). If neither appears
    but the daemon process is running, considers it ready after
    the timeout with a warning.
    """
    deadline = time.time() + timeout
    daemon_seen = False
    while time.time() < deadline:
        if _interrupted:
            return False
        # Check signal files (launch mode creates fish_system_stable,
        # daemon creates fish_gpu_handler_complete after GPU handling)
        rc, _, _ = docker_exec(container,
            "test -f /tmp/fish_system_stable -o -f /tmp/fish_gpu_handler_complete",
            timeout=5)
        if rc == 0:
            return True
        # Check if daemon is running at all
        rc2, _, _ = docker_exec(container,
            "pgrep -f 'fish.cli.*daemon'", timeout=5)
        if rc2 == 0:
            if not daemon_seen:
                daemon_seen = True
                log(f"  FISH daemon detected in {container}, waiting for stable signal...")
        else:
            if time.time() - (deadline - timeout) > 30:  # only warn after 30s
                log(f"  FISH daemon not running in {container}", "WARN")
        time.sleep(5)

    if daemon_seen:
        log(f"  FISH daemon running but no stable signal — proceeding anyway", "WARN")
        return True  # daemon is there, just no signal file
    return False


def fish_stop(container, timeout=120):
    """Send FISH stop signal to a container."""
    rc, out, err = docker_exec(
        container,
        "/opt/ros/humble/fish/bin/ros2 run fish stop",
        timeout=timeout,
    )
    return rc == 0


def find_latest_session(container, trace_path="/root/fish_traces"):
    """Find the most recent fish_YYYYMMDD_HHMMSS directory in a container."""
    rc, out, _ = docker_exec(
        container,
        f"ls -1td {trace_path}/fish_* 2>/dev/null | head -1",
        timeout=10,
    )
    if rc == 0 and out:
        return os.path.basename(out)
    return None


def collect_traces(container, remote_path, local_dest):
    """docker cp traces from container to local filesystem."""
    os.makedirs(local_dest, exist_ok=True)
    src = f"{container}:{remote_path}/."
    log(f"  Collecting: {src} -> {local_dest}")
    r = subprocess.run(["docker", "cp", src, local_dest],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  docker cp failed: {r.stderr[:200]}", "WARN")
        return False
    # Fix permissions (LTTng creates root-owned dirs)
    subprocess.run(["chmod", "-R", "u+rw", local_dest],
                   capture_output=True)
    return True


# ---------------------------------------------------------------------------
# Mission execution
# ---------------------------------------------------------------------------

def run_mission(config, dry_run=False):
    mission_name = config.get("name", "unnamed")
    log(f"=== FISH Orchestrator: {mission_name} ===")

    # --- Parse containers ---
    containers = {}
    for alias, cdef in config.get("containers", {}).items():
        containers[alias] = {
            "docker_name": cdef.get("docker_name", alias),
            "fish": cdef.get("fish", False),
            "trace_path": cdef.get("trace_path", "/root/fish_traces"),
            "role": cdef.get("role", alias),
        }
        log(f"  Container '{alias}': {cdef.get('docker_name', alias)} "
            f"{'[FISH]' if cdef.get('fish') else ''}")

    if dry_run:
        log("DRY RUN — actions that would execute:")
        for i, action in enumerate(config.get("actions", [])):
            if "sleep" in action:
                log(f"  {i+1}. sleep {action['sleep']}s")
            elif "exec" in action:
                a = action["exec"]
                log(f"  {i+1}. exec on {a['container']}: {a['run'][:80]}")
            elif "log" in action:
                log(f"  {i+1}. log: {action['log']}")
            elif "parallel" in action:
                log(f"  {i+1}. parallel: {len(action['parallel'])} commands")
        return None

    # --- Session directory ---
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_base = os.path.expanduser(config.get("output", "~/fish_traces"))
    session_dir = os.path.join(output_base, f"fish_compose_{ts}")
    os.makedirs(session_dir, exist_ok=True)
    log(f"Session: {session_dir}")

    # --- Phase 1: Launch (optional) ---
    launch_proc = None
    launch_cfg = config.get("launch", {})

    if launch_cfg.get("command"):
        cmd = launch_cfg["command"]
        log(f"Launching: {cmd}")
        launch_proc = subprocess.Popen(
            cmd, shell=True, stdin=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        docker_names = [c["docker_name"] for c in containers.values()]
        timeout = launch_cfg.get("timeout", 120)
        log(f"Waiting for {len(docker_names)} containers ({timeout}s timeout)...")
        if not wait_for_containers(docker_names, timeout):
            log("Aborting — containers did not start", "ERROR")
            if launch_proc:
                os.killpg(os.getpgid(launch_proc.pid), signal.SIGTERM)
            return None
        log("All containers running")
    else:
        # Verify existing containers
        all_ok = True
        for alias, c in containers.items():
            if container_running(c["docker_name"]):
                log(f"  {alias}: running")
            else:
                log(f"  {alias}: NOT RUNNING", "ERROR")
                all_ok = False
        if not all_ok:
            log("Aborting — missing containers", "ERROR")
            return None

    # --- Phase 2: Wait for FISH readiness ---
    ready_cfg = config.get("ready", {})
    stable_targets = ready_cfg.get("fish_stable", [])
    if not stable_targets:
        stable_targets = [a for a, c in containers.items() if c["fish"]]

    timeout = ready_cfg.get("timeout", 180)
    for alias in stable_targets:
        c = containers[alias]
        log(f"Waiting for FISH stable: {alias} ({c['docker_name']})...")
        if wait_fish_stable(c["docker_name"], timeout):
            log(f"  {alias}: stable")
        else:
            log(f"  {alias}: NOT stable after {timeout}s", "WARN")

    # --- Phase 3: Execute actions ---
    log("--- Mission actions ---")
    mission_start = time.time()
    action_log = []

    for i, action in enumerate(config.get("actions", [])):
        if _interrupted:
            log("Skipping remaining actions due to interrupt", "WARN")
            break
        t0 = time.time()

        if "sleep" in action:
            dur = action["sleep"]
            log(f"[{i+1}] sleep {dur}s")
            time.sleep(dur)
            action_log.append({
                "type": "sleep", "duration": dur,
                "time": time.time(),
            })

        elif "exec" in action:
            a = action["exec"]
            alias = a["container"]
            cmd = a["run"]
            wait = a.get("wait", True)
            tout = a.get("timeout", 300)
            c = containers[alias]

            short = cmd[:70] + ("..." if len(cmd) > 70 else "")
            log(f"[{i+1}] exec {alias}: {short}")

            if wait:
                rc = docker_exec_stream(c["docker_name"], cmd, timeout=tout)
            else:
                subprocess.Popen(
                    ["docker", "exec", c["docker_name"], "bash", "-c", cmd],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                rc = 0

            dt = time.time() - t0
            log(f"     -> {'ok' if rc==0 else f'exit={rc}'} ({dt:.1f}s)")
            action_log.append({
                "type": "exec", "on": alias, "cmd": cmd,
                "rc": rc, "duration": dt, "time": time.time(),
            })

        elif "log" in action:
            log(f"[{i+1}] {action['log']}")
            action_log.append({
                "type": "log", "msg": action["log"],
                "time": time.time(),
            })

        elif "parallel" in action:
            log(f"[{i+1}] parallel: {len(action['parallel'])} commands")
            procs = []
            for pa in action["parallel"]:
                if "exec" in pa:
                    a = pa["exec"]
                    c = containers[a["container"]]
                    p = subprocess.Popen(
                        ["docker", "exec", c["docker_name"], "bash", "-c", a["run"]],
                    )
                    procs.append((a["container"], a["run"], p))
            # Wait for all
            for alias, cmd, p in procs:
                p.wait()
                log(f"     {alias}: exit={p.returncode}")
            dt = time.time() - t0
            action_log.append({
                "type": "parallel", "count": len(procs),
                "duration": dt, "time": time.time(),
            })

    mission_end = time.time()
    log(f"--- Actions complete ({mission_end - mission_start:.1f}s) ---")

    # --- Phase 4: Stop FISH ---
    finish_cfg = config.get("finish", {})
    stop_targets = finish_cfg.get("fish_stop", "all")
    if stop_targets == "all":
        stop_targets = [a for a, c in containers.items() if c["fish"]]

    for alias in stop_targets:
        c = containers[alias]
        log(f"FISH stop: {alias} ({c['docker_name']})")
        if fish_stop(c["docker_name"]):
            log(f"  {alias}: stopped")
        else:
            log(f"  {alias}: stop failed (may not be running)", "WARN")

    wait_after = finish_cfg.get("wait_after_stop", 15)
    log(f"Waiting {wait_after}s for nsys reports...")
    time.sleep(wait_after)

    # --- Phase 5: Collect traces ---
    if finish_cfg.get("collect_traces", True):
        manifest_containers = {}
        for alias, c in containers.items():
            local_dir = os.path.join(session_dir, c["role"])

            if c["fish"]:
                # Find latest session — prefer host path (volume mount) over docker cp
                host_trace = c.get("host_trace_path")
                if host_trace:
                    host_trace = os.path.expanduser(host_trace)

                session_name = None
                if host_trace and os.path.isdir(host_trace):
                    # Find latest fish_* dir on host
                    sessions = sorted(
                        [d for d in os.listdir(host_trace) if d.startswith("fish_")],
                        reverse=True)
                    if sessions:
                        session_name = sessions[0]
                if not session_name:
                    session_name = find_latest_session(
                        c["docker_name"], c["trace_path"])

                if session_name:
                    log(f"Collecting {alias}: {session_name}")
                    if host_trace and os.path.isdir(os.path.join(host_trace, session_name)):
                        # Copy from host filesystem (faster, no docker cp)
                        src = os.path.join(host_trace, session_name)
                        log(f"  Host copy: {src} -> {local_dir}")
                        os.makedirs(local_dir, exist_ok=True)
                        subprocess.run(["cp", "-a", src + "/.", local_dir],
                                       capture_output=True)
                    else:
                        remote = f"{c['trace_path']}/{session_name}"
                        collect_traces(c["docker_name"], remote, local_dir)
                    manifest_containers[alias] = {
                        "docker_name": c["docker_name"],
                        "fish": True,
                        "role": c["role"],
                        "trace_session": session_name,
                    }
                else:
                    log(f"No trace session in {alias}", "WARN")
                    manifest_containers[alias] = {
                        "docker_name": c["docker_name"],
                        "fish": True,
                        "role": c["role"],
                        "trace_session": None,
                    }
            else:
                manifest_containers[alias] = {
                    "docker_name": c["docker_name"],
                    "fish": False,
                    "role": c["role"],
                }

        # Write manifest
        manifest = {
            "name": mission_name,
            "compose_session": os.path.basename(session_dir),
            "created": datetime.now().isoformat(),
            "containers": manifest_containers,
            "actions": action_log,
            "timing": {
                "mission_start_epoch": mission_start,
                "mission_end_epoch": mission_end,
                "duration_s": round(mission_end - mission_start, 1),
            },
        }
        manifest_path = os.path.join(session_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, default=str)
        log(f"Manifest: {manifest_path}")

        # Write orchestrator log
        log_path = os.path.join(session_dir, "orchestrator.log")
        with open(log_path, "w") as f:
            f.write("\n".join(_log_lines) + "\n")

    # --- Phase 6: Container cleanup (optional) ---
    if finish_cfg.get("stop_containers", False):
        for alias, c in containers.items():
            log(f"Stopping container: {c['docker_name']}")
            subprocess.run(["docker", "stop", c["docker_name"]],
                           capture_output=True)

    if launch_proc:
        log("Terminating launch script...")
        try:
            # Send newline to sim_run.sh "press any key" prompt
            launch_proc.stdin.write(b"\n")
            launch_proc.stdin.flush()
            launch_proc.wait(timeout=30)
        except Exception:
            try:
                os.killpg(os.getpgid(launch_proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    log(f"=== Mission complete: {session_dir} ===")
    return session_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="FISH Orchestrator — multi-container mission executor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 fish_orchestrator.py examples/aas_single_drone.yaml
  python3 fish_orchestrator.py examples/aas_single_drone.yaml --dry-run
        """,
    )
    ap.add_argument("mission", help="Path to mission YAML file")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print actions without executing")
    args = ap.parse_args()

    if not os.path.isfile(args.mission):
        print(f"Error: mission file not found: {args.mission}", file=sys.stderr)
        sys.exit(1)

    with open(args.mission) as f:
        config = yaml.safe_load(f)

    # Graceful interrupt: first Ctrl+C skips to cleanup, second force quits
    def handle_sigint(sig, frame):
        global _interrupted
        if _interrupted:
            log("Force quit.", "ERROR")
            sys.exit(1)
        _interrupted = True
        log("Interrupted — skipping to cleanup...", "WARN")

    signal.signal(signal.SIGINT, handle_sigint)

    run_mission(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
