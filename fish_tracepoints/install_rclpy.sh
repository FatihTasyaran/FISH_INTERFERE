#!/bin/bash
# =============================================================================
# FISH rclpy Tracepoint Installer
# =============================================================================
# Adds 5 new LTTng tracepoints for rclpy callback chain visibility,
# creates a ctypes bridge module, and patches rclpy source files.
#
# Tracepoints added:
#   ros2:rclpy_subscription_callback_added — subscription handle → callback
#   ros2:rclpy_service_callback_added      — service handle → callback
#   ros2:rclpy_timer_callback_added        — timer handle → callback
#   ros2:rclpy_timer_link_node             — timer handle → node handle
#   ros2:rclpy_callback_register           — callback → symbol string
#
# Reuses existing (no modification):
#   ros2:callback_start, ros2:callback_end
#
# Prerequisite: install.sh must have been run first (action tracepoints).
#
# Usage:
#   chmod +x install_rclpy.sh && ./install_rclpy.sh
#   source /root/trace_overlay_ws/install/setup.bash
# =============================================================================
set -euo pipefail

OVERLAY_WS="${OVERLAY_WS:-/root/trace_overlay_ws}"
RCLPY_DIR="/opt/ros/humble/local/lib/python3.10/dist-packages/rclpy"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[FISH]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*" >&2; }

# ─── Pre-flight checks ───────────────────────────────────────────────────────
if [ ! -d "$OVERLAY_WS/install/tracetools" ]; then
    log "Action tracepoints not found. Running install.sh first ..."
    if [ -f "$SCRIPT_DIR/install.sh" ]; then
        bash "$SCRIPT_DIR/install.sh"
    else
        err "install.sh not found in $SCRIPT_DIR. Run it first."
        exit 1
    fi
fi

if [ ! -d "$RCLPY_DIR" ]; then
    err "rclpy not found at $RCLPY_DIR. Aborting."
    exit 1
fi

set +u && source /opt/ros/humble/setup.bash && set -u

# ─── Step 1: Patch tracetools C source ───────────────────────────────────────
log "[1/5] Adding rclpy tracepoint definitions to tracetools ..."

# Use Python for reliable multi-line insertion at anchor points
python3 <<'PATCH_C_FILES'
import sys

OVERLAY = "/root/trace_overlay_ws/src/tracetools"

# ── tp_call.h ──
tp_call_path = f"{OVERLAY}/include/tracetools/tp_call.h"
with open(tp_call_path) as f:
    content = f.read()

if "rclpy_subscription_callback_added" in content:
    print("  tp_call.h already patched, skipping")
else:
    INSERT_BEFORE = "#endif  // _TRACETOOLS__TP_CALL_H_"
    new_events = """
TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclpy_subscription_callback_added,
  TP_ARGS(
    const void *, subscription_handle_arg,
    const void *, callback_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, subscription_handle, subscription_handle_arg)
    ctf_integer_hex(const void *, callback, callback_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclpy_service_callback_added,
  TP_ARGS(
    const void *, service_handle_arg,
    const void *, callback_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, service_handle, service_handle_arg)
    ctf_integer_hex(const void *, callback, callback_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclpy_timer_callback_added,
  TP_ARGS(
    const void *, timer_handle_arg,
    const void *, callback_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, timer_handle, timer_handle_arg)
    ctf_integer_hex(const void *, callback, callback_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclpy_timer_link_node,
  TP_ARGS(
    const void *, timer_handle_arg,
    const void *, node_handle_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, timer_handle, timer_handle_arg)
    ctf_integer_hex(const void *, node_handle, node_handle_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclpy_callback_register,
  TP_ARGS(
    const void *, callback_arg,
    const char *, symbol_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, callback, callback_arg)
    ctf_string(symbol, symbol_arg)
  )
)

"""
    content = content.replace(INSERT_BEFORE, new_events + INSERT_BEFORE)
    with open(tp_call_path, "w") as f:
        f.write(content)
    print("  tp_call.h patched — 5 TRACEPOINT_EVENT added")

# ── tracetools.h ──
tt_h_path = f"{OVERLAY}/include/tracetools/tracetools.h"
with open(tt_h_path) as f:
    content = f.read()

if "rclpy_subscription_callback_added" in content:
    print("  tracetools.h already patched, skipping")
else:
    # Insert before the closing #ifdef __cplusplus
    # Find the LAST occurrence (after action tracepoints)
    INSERT_BEFORE = "#ifdef __cplusplus\n}\n#endif\n\n#endif  // TRACETOOLS__TRACETOOLS_H_"
    new_decls = """/// `rclpy_subscription_callback_added`
/**
 * Link a rclpy subscription to its Python callback.
 *
 * \\param[in] subscription_handle pointer to the rcl_subscription_t handle
 * \\param[in] callback Python callback id (id(callable))
 */
DECLARE_TRACEPOINT(
  rclpy_subscription_callback_added,
  const void * subscription_handle,
  const void * callback)

/// `rclpy_service_callback_added`
/**
 * Link a rclpy service to its Python callback.
 *
 * \\param[in] service_handle pointer to the rcl_service_t handle
 * \\param[in] callback Python callback id
 */
DECLARE_TRACEPOINT(
  rclpy_service_callback_added,
  const void * service_handle,
  const void * callback)

/// `rclpy_timer_callback_added`
/**
 * Link a rclpy timer to its Python callback.
 *
 * \\param[in] timer_handle pointer to the rcl_timer_t handle
 * \\param[in] callback Python callback id
 */
DECLARE_TRACEPOINT(
  rclpy_timer_callback_added,
  const void * timer_handle,
  const void * callback)

/// `rclpy_timer_link_node`
/**
 * Link a rclpy timer to its parent node.
 *
 * \\param[in] timer_handle pointer to the rcl_timer_t handle
 * \\param[in] node_handle pointer to the rcl_node_t handle
 */
DECLARE_TRACEPOINT(
  rclpy_timer_link_node,
  const void * timer_handle,
  const void * node_handle)

/// `rclpy_callback_register`
/**
 * Register a Python callback symbol.
 *
 * \\param[in] callback Python callback id
 * \\param[in] symbol Python qualname of the callback
 */
DECLARE_TRACEPOINT(
  rclpy_callback_register,
  const void * callback,
  const char * symbol)

"""
    content = content.replace(INSERT_BEFORE, new_decls + INSERT_BEFORE)
    with open(tt_h_path, "w") as f:
        f.write(content)
    print("  tracetools.h patched — 5 DECLARE_TRACEPOINT added")

# ── tracetools.c ──
tt_c_path = f"{OVERLAY}/src/tracetools.c"
with open(tt_c_path) as f:
    content = f.read()

if "rclpy_subscription_callback_added" in content:
    print("  tracetools.c already patched, skipping")
else:
    INSERT_BEFORE = "#ifndef _WIN32\n# pragma GCC diagnostic pop"
    new_impls = """void TRACEPOINT(
  rclpy_subscription_callback_added,
  const void * subscription_handle,
  const void * callback)
{
  CONDITIONAL_TP(
    rclpy_subscription_callback_added,
    subscription_handle,
    callback);
}

void TRACEPOINT(
  rclpy_service_callback_added,
  const void * service_handle,
  const void * callback)
{
  CONDITIONAL_TP(
    rclpy_service_callback_added,
    service_handle,
    callback);
}

void TRACEPOINT(
  rclpy_timer_callback_added,
  const void * timer_handle,
  const void * callback)
{
  CONDITIONAL_TP(
    rclpy_timer_callback_added,
    timer_handle,
    callback);
}

void TRACEPOINT(
  rclpy_timer_link_node,
  const void * timer_handle,
  const void * node_handle)
{
  CONDITIONAL_TP(
    rclpy_timer_link_node,
    timer_handle,
    node_handle);
}

void TRACEPOINT(
  rclpy_callback_register,
  const void * callback,
  const char * symbol)
{
  CONDITIONAL_TP(
    rclpy_callback_register,
    callback,
    symbol);
}

"""
    content = content.replace(INSERT_BEFORE, new_impls + INSERT_BEFORE)
    with open(tt_c_path, "w") as f:
        f.write(content)
    print("  tracetools.c patched — 5 TRACEPOINT implementations added")

print("  Done.")
PATCH_C_FILES

# ─── Step 2: Rebuild tracetools ──────────────────────────────────────────────
log "[2/5] Rebuilding tracetools with rclpy tracepoints ..."
cd "$OVERLAY_WS"

colcon build \
    --packages-select tracetools \
    --cmake-args -DBUILD_TESTING=OFF \
    2>&1 | tail -3

log "  tracetools rebuilt"

# Verify rclpy symbols exported
if nm -D "$OVERLAY_WS/install/tracetools/lib/libtracetools.so" 2>/dev/null | grep -q "ros_trace_rclpy_subscription_callback_added"; then
    log "  Symbol check: OK"
else
    err "  Symbol check: FAILED — rclpy tracepoint functions not exported"
    exit 1
fi

# ─── Step 3: Create ctypes bridge module ─────────────────────────────────────
log "[3/5] Creating fish_rclpy_trace.py ctypes bridge ..."

cat > "$RCLPY_DIR/fish_rclpy_trace.py" <<'PYBRIDGE'
"""
FISH rclpy tracepoint bridge.

Calls LTTng tracepoint functions in libtracetools.so via ctypes.
Provides the rclpy callback chain events that rclcpp fires natively
but rclpy does not.

Usage (from rclpy internals):
    from rclpy.fish_rclpy_trace import trace_sub_cb, trace_srv_cb, ...
"""
import ctypes
import os

_lib = None
_enabled = False

def _init():
    global _lib, _enabled
    try:
        # Try overlay first, then base install
        for path in [
            os.environ.get("FISH_TRACETOOLS_LIB", ""),
            "/root/trace_overlay_ws/install/tracetools/lib/libtracetools.so",
            "libtracetools.so",
        ]:
            if path and os.path.isfile(path):
                _lib = ctypes.CDLL(path)
                break
        if _lib is None:
            _lib = ctypes.CDLL("libtracetools.so")

        # Check if tracing is compiled in
        _lib.ros_trace_compile_status.restype = ctypes.c_bool
        if not _lib.ros_trace_compile_status():
            return

        # New rclpy tracepoints
        for name in [
            "ros_trace_rclpy_subscription_callback_added",
            "ros_trace_rclpy_service_callback_added",
            "ros_trace_rclpy_timer_callback_added",
            "ros_trace_rclpy_timer_link_node",
        ]:
            fn = getattr(_lib, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            fn.restype = None

        _lib.ros_trace_rclpy_callback_register.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p]
        _lib.ros_trace_rclpy_callback_register.restype = None

        # Existing tracepoints (reused for runtime)
        _lib.ros_trace_callback_start.argtypes = [
            ctypes.c_void_p, ctypes.c_bool]
        _lib.ros_trace_callback_start.restype = None

        _lib.ros_trace_callback_end.argtypes = [ctypes.c_void_p]
        _lib.ros_trace_callback_end.restype = None

        _enabled = True

    except (OSError, AttributeError):
        # Tracetools not available or missing symbols — silently disable
        _enabled = False

_init()


def _get_symbol(callback):
    """Extract a human-readable symbol from a Python callable."""
    sym = getattr(callback, '__qualname__', None)
    mod = getattr(callback, '__module__', None)
    if sym and mod:
        return f"{mod}.{sym}"
    elif sym:
        return sym
    return repr(callback)


def _register(callback):
    """Register callback ID → symbol mapping."""
    if not _enabled:
        return
    cb_id = id(callback)
    symbol = _get_symbol(callback)
    _lib.ros_trace_rclpy_callback_register(
        ctypes.c_void_p(cb_id),
        symbol.encode('utf-8'))


def trace_sub_cb(sub_handle_ptr, callback):
    """Fire rclpy_subscription_callback_added + rclpy_callback_register."""
    if not _enabled:
        return
    cb_id = ctypes.c_void_p(id(callback))
    _lib.ros_trace_rclpy_subscription_callback_added(
        ctypes.c_void_p(sub_handle_ptr), cb_id)
    _register(callback)


def trace_srv_cb(srv_handle_ptr, callback):
    """Fire rclpy_service_callback_added + rclpy_callback_register."""
    if not _enabled:
        return
    cb_id = ctypes.c_void_p(id(callback))
    _lib.ros_trace_rclpy_service_callback_added(
        ctypes.c_void_p(srv_handle_ptr), cb_id)
    _register(callback)


def trace_tmr_cb(tmr_handle_ptr, callback):
    """Fire rclpy_timer_callback_added + rclpy_callback_register."""
    if not _enabled:
        return
    cb_id = ctypes.c_void_p(id(callback))
    _lib.ros_trace_rclpy_timer_callback_added(
        ctypes.c_void_p(tmr_handle_ptr), cb_id)
    _register(callback)


def trace_tmr_node(tmr_handle_ptr, node_handle_ptr):
    """Fire rclpy_timer_link_node."""
    if not _enabled:
        return
    _lib.ros_trace_rclpy_timer_link_node(
        ctypes.c_void_p(tmr_handle_ptr),
        ctypes.c_void_p(node_handle_ptr))


def trace_cb_start(callback):
    """Fire callback_start (reused from existing tracetools)."""
    if not _enabled:
        return
    _lib.ros_trace_callback_start(ctypes.c_void_p(id(callback)), False)


def trace_cb_end(callback):
    """Fire callback_end (reused from existing tracetools)."""
    if not _enabled:
        return
    _lib.ros_trace_callback_end(ctypes.c_void_p(id(callback)))
PYBRIDGE

log "  Created $RCLPY_DIR/fish_rclpy_trace.py"

# ─── Step 4: Patch rclpy source files ────────────────────────────────────────
log "[4/5] Patching rclpy source files ..."

python3 <<PATCH_RCLPY
import re

RCLPY = "$RCLPY_DIR"

# ── subscription.py ──
path = f"{RCLPY}/subscription.py"
with open(path) as f:
    content = f.read()

if "fish_rclpy_trace" in content:
    print("  subscription.py already patched")
else:
    # Insert after self.callback = callback (and the event_handlers block)
    # Find the end of __init__ body — after event_handlers assignment
    old = "        self.event_handlers: QoSEventHandler = event_callbacks.create_event_handlers(\n            callback_group, subscription_impl, topic)"
    new = old + """

        # FISH: trace rclpy subscription callback chain
        try:
            from rclpy.fish_rclpy_trace import trace_sub_cb
            trace_sub_cb(self.handle.pointer, self.callback)
        except Exception:
            pass"""
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("  subscription.py patched")

# ── service.py ──
path = f"{RCLPY}/service.py"
with open(path) as f:
    content = f.read()

if "fish_rclpy_trace" in content:
    print("  service.py already patched")
else:
    old = "        self.qos_profile = qos_profile"
    new = old + """

        # FISH: trace rclpy service callback chain
        try:
            from rclpy.fish_rclpy_trace import trace_srv_cb
            trace_srv_cb(self.handle.pointer, self.callback)
        except Exception:
            pass"""
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("  service.py patched")

# ── timer.py ──
path = f"{RCLPY}/timer.py"
with open(path) as f:
    content = f.read()

if "fish_rclpy_trace" in content:
    print("  timer.py already patched")
else:
    # Insert after self._executor_event = False in Timer.__init__
    old = "        self._executor_event = False"
    # Only replace in Timer.__init__ context (first occurrence)
    idx = content.find(old)
    if idx >= 0:
        content = content[:idx] + old + """

        # FISH: trace rclpy timer callback chain
        try:
            from rclpy.fish_rclpy_trace import trace_tmr_cb
            trace_tmr_cb(self.handle.pointer, self.callback)
        except Exception:
            pass""" + content[idx + len(old):]
    with open(path, "w") as f:
        f.write(content)
    print("  timer.py patched")

# ── node.py ──
path = f"{RCLPY}/node.py"
with open(path) as f:
    content = f.read()

if "fish_rclpy_trace" in content:
    print("  node.py already patched")
else:
    # Insert after timer creation in create_timer, before callback_group.add_entity
    # Find: "timer = Timer(callback, callback_group, timer_period_nsec, clock, context=self.context)"
    # followed by blank line and "callback_group.add_entity(timer)"
    old = "        timer = Timer(callback, callback_group, timer_period_nsec, clock, context=self.context)\n\n        callback_group.add_entity(timer)"
    new = """        timer = Timer(callback, callback_group, timer_period_nsec, clock, context=self.context)

        # FISH: trace rclpy timer → node link
        try:
            from rclpy.fish_rclpy_trace import trace_tmr_node
            trace_tmr_node(timer.handle.pointer, self.handle.pointer)
        except Exception:
            pass

        callback_group.add_entity(timer)"""
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("  node.py patched")

# ── executors.py ──
path = f"{RCLPY}/executors.py"
with open(path) as f:
    content = f.read()

if "fish_rclpy_trace" in content:
    print("  executors.py already patched")
else:
    # Patch _execute_subscription
    old_sub = """    async def _execute_subscription(self, sub, msg):
        if msg:
            await await_or_execute(sub.callback, msg)"""
    new_sub = """    async def _execute_subscription(self, sub, msg):
        if msg:
            from rclpy.fish_rclpy_trace import trace_cb_start, trace_cb_end
            trace_cb_start(sub.callback)
            try:
                await await_or_execute(sub.callback, msg)
            finally:
                trace_cb_end(sub.callback)"""

    # Patch _execute_timer
    old_tmr = """    async def _execute_timer(self, tmr, ready):
        if ready:
            await await_or_execute(tmr.callback)"""
    new_tmr = """    async def _execute_timer(self, tmr, ready):
        if ready:
            from rclpy.fish_rclpy_trace import trace_cb_start, trace_cb_end
            trace_cb_start(tmr.callback)
            try:
                await await_or_execute(tmr.callback)
            finally:
                trace_cb_end(tmr.callback)"""

    # Patch _execute_service
    old_srv = """    async def _execute_service(self, srv, request_and_header):
        if request_and_header is None:
            return
        (request, header) = request_and_header
        if request:
            response = await await_or_execute(srv.callback, request, srv.srv_type.Response())
            srv.send_response(response, header)"""
    new_srv = """    async def _execute_service(self, srv, request_and_header):
        if request_and_header is None:
            return
        (request, header) = request_and_header
        if request:
            from rclpy.fish_rclpy_trace import trace_cb_start, trace_cb_end
            trace_cb_start(srv.callback)
            try:
                response = await await_or_execute(srv.callback, request, srv.srv_type.Response())
            finally:
                trace_cb_end(srv.callback)
            srv.send_response(response, header)"""

    content = content.replace(old_sub, new_sub)
    content = content.replace(old_tmr, new_tmr)
    content = content.replace(old_srv, new_srv)
    with open(path, "w") as f:
        f.write(content)
    print("  executors.py patched")

# Clear __pycache__ so patched files take effect
import shutil, os
cache = f"{RCLPY}/__pycache__"
if os.path.isdir(cache):
    shutil.rmtree(cache)
    print("  __pycache__ cleared")

print("  Done.")
PATCH_RCLPY

# ─── Step 5: Update tracetools_trace event list ──────────────────────────────
log "[5/5] Updating tracetools_trace event list (names.py) ..."

NAMES_PY=$(python3 -c "
import tracetools_trace.tools.names as m
import os
print(os.path.abspath(m.__file__))
")

if grep -q "rclpy_subscription_callback_added" "$NAMES_PY"; then
    log "  names.py already has rclpy events, skipping"
else
    python3 <<PATCHNAMES
with open("$NAMES_PY") as f:
    content = f.read()

new_entries = '''    "ros2:rclpy_subscription_callback_added",
    "ros2:rclpy_service_callback_added",
    "ros2:rclpy_timer_callback_added",
    "ros2:rclpy_timer_link_node",
    "ros2:rclpy_callback_register",'''

# Insert after the last action tracepoint entry
content = content.replace(
    '    "ros2:rclcpp_action_execute_result",\n]',
    '    "ros2:rclcpp_action_execute_result",\n' + new_entries + '\n]'
)

with open("$NAMES_PY", "w") as f:
    f.write(content)
print("  Added 5 rclpy tracepoint events to DEFAULT_EVENTS_ROS")
PATCHNAMES
fi

# ─── Verify ──────────────────────────────────────────────────────────────────
log ""
log "=========================================="
log " FISH rclpy Tracepoints Installed"
log "=========================================="
log ""

# Symbol check
RCLPY_SYMS=$(nm -D "$OVERLAY_WS/install/tracetools/lib/libtracetools.so" 2>/dev/null | grep -c "ros_trace_rclpy_" || true)
log "rclpy symbols in libtracetools.so: $RCLPY_SYMS (expected: 5)"

# Event count
EVENT_COUNT=$(python3 -c "
try:
    from tracetools_trace.tools.names import DEFAULT_EVENTS_ROS
    print(len(DEFAULT_EVENTS_ROS))
except:
    print(0)
" 2>/dev/null || echo "0")
log "Total events in DEFAULT_EVENTS_ROS: $EVENT_COUNT (expected: 37)"

# Python module check
if python3 -c "from rclpy.fish_rclpy_trace import trace_sub_cb; print('  ctypes bridge: OK')" 2>/dev/null; then
    :
else
    warn "  ctypes bridge: import failed (will work after sourcing overlay)"
fi

# ─── Step 6: Ensure overlay is sourced at startup ────────────────────────────
log "[+] Ensuring overlay is sourced in .bashrc and tmuxinator ..."

BASHRC="/root/.bashrc"
SETUP_LINE="source $OVERLAY_WS/install/setup.bash"

# Remove any stale tracetools overlay source lines, add the correct one
if [ -f "$BASHRC" ]; then
    # Remove old/wrong overlay paths
    sed -i '\|source.*/tracetools_ws/install/setup.bash|d' "$BASHRC"
    sed -i '\|source.*/trace_overlay_ws/install/setup.bash|d' "$BASHRC"
    # Add correct one at the end
    echo "$SETUP_LINE" >> "$BASHRC"
    log "  .bashrc updated: $SETUP_LINE"
fi

# Patch tmuxinator config to add pre_window hook
TMUX_CFG="/aas/aircraft.yml.erb"
if [ -f "$TMUX_CFG" ]; then
    if grep -q "trace_overlay_ws" "$TMUX_CFG"; then
        log "  tmuxinator config already has overlay source"
    else
        # Add pre_window after "name: drone" line
        sed -i '/^name: drone$/a\pre_window: source /root/trace_overlay_ws/install/setup.bash' "$TMUX_CFG"
        log "  tmuxinator config patched: pre_window added"
    fi
fi

log ""
log "To activate NOW (without restart), source the overlay:"
log "  source $OVERLAY_WS/install/setup.bash"
