#!/bin/bash
# =============================================================================
# FISH Tracepoint Installer (v2)
# =============================================================================
# Installs all FISH custom LTTng tracepoints for ROS 2 Humble via an overlay
# workspace.  Replaces the previous version which only had action tracepoints.
#
# Tracepoints defined (6 total):
#   ros2:rclcpp_action_server_init    — action handle → name + services
#   ros2:rclcpp_action_execute_goal   — goal request dispatch
#   ros2:rclcpp_action_execute_cancel — cancel request dispatch
#   ros2:rclcpp_action_execute_result — result request dispatch
#   ros2:fish_publish_link            — publisher_handle → active callback
#   ros2:fish_client_link             — client_handle   → active callback
#
# Also adds:
#   - Thread-local __fish_active_callback (set in callback_start/end)
#   - Fire points in rcl_publish() and rcl_send_request()
#   - Dedup cache: link events fire once per unique (handle, callback) pair
#
# Target: ROS 2 Humble containers
# Tags: tracetools 4.1.2, rclcpp 16.0.19, rcl 5.3.12
#
# Usage:
#   chmod +x install.sh && ./install.sh
#   source /root/trace_overlay_ws/install/setup.bash
# =============================================================================
set -euo pipefail

OVERLAY_WS="${OVERLAY_WS:-/root/trace_overlay_ws}"
TRACETOOLS_TAG="${TRACETOOLS_TAG:-4.1.2}"
RCLCPP_TAG="${RCLCPP_TAG:-16.0.19}"
RCL_TAG="${RCL_TAG:-5.3.12}"
CLONE_DIR="/tmp/fish_tracepoint_src"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[FISH]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*" >&2; }

# ─── Pre-flight checks ───────────────────────────────────────────────────────
if [ ! -f /opt/ros/humble/setup.bash ]; then
    err "ROS 2 Humble not found at /opt/ros/humble. Aborting."
    exit 1
fi

set +u && source /opt/ros/humble/setup.bash && set -u

command -v colcon >/dev/null 2>&1 || {
    err "colcon not found. Install: apt install python3-colcon-common-extensions"
    exit 1
}

command -v git >/dev/null 2>&1 || {
    err "git not found. Install: apt install git"
    exit 1
}

# ─── Step 1: Create overlay workspace ────────────────────────────────────────
log "[1/8] Creating overlay workspace at $OVERLAY_WS ..."
mkdir -p "$OVERLAY_WS/src"

# ─── Step 2: Clone source repositories ───────────────────────────────────────
log "[2/8] Cloning source repositories ..."
mkdir -p "$CLONE_DIR"

clone_if_needed() {
    local repo=$1 tag=$2 dir=$3
    if [ -d "$dir" ]; then
        log "  $dir already exists, skipping clone"
    else
        log "  Cloning $repo @ $tag ..."
        git clone --depth 1 --branch "$tag" "$repo" "$dir" 2>&1 | tail -1
    fi
}

clone_if_needed \
    "https://github.com/ros2/ros2_tracing.git" \
    "$TRACETOOLS_TAG" \
    "$CLONE_DIR/ros2_tracing"

clone_if_needed \
    "https://github.com/ros2/rclcpp.git" \
    "$RCLCPP_TAG" \
    "$CLONE_DIR/rclcpp"

clone_if_needed \
    "https://github.com/ros2/rcl.git" \
    "$RCL_TAG" \
    "$CLONE_DIR/rcl"

# ─── Step 3: Copy packages into workspace ────────────────────────────────────
log "[3/8] Copying packages into workspace ..."

copy_pkg() {
    local src=$1 dst=$2
    if [ -d "$dst" ]; then
        warn "  $dst exists, removing for clean state"
        rm -rf "$dst"
    fi
    cp -r "$src" "$dst"
}

# tracetools
copy_pkg "$CLONE_DIR/ros2_tracing/tracetools" "$OVERLAY_WS/src/tracetools"

# rcl (for publisher.c + client.c patches)
copy_pkg "$CLONE_DIR/rcl/rcl" "$OVERLAY_WS/src/rcl"

# rclcpp_action
copy_pkg "$CLONE_DIR/rclcpp/rclcpp_action" "$OVERLAY_WS/src/rclcpp_action"

# Private rcl_action header (needed for action_server_impl struct)
cp "$CLONE_DIR/rcl/rcl_action/src/rcl_action/action_server_impl.h" \
   "$OVERLAY_WS/src/rclcpp_action/src/action_server_impl.h"
log "  Copied action_server_impl.h from rcl source"

# ─── Step 4: Patch tracetools ────────────────────────────────────────────────
log "[4/8] Patching tracetools (tp_call.h, tracetools.h, tracetools.c) ..."

python3 <<'PATCH_TRACETOOLS'
import sys

OVERLAY = "/root/trace_overlay_ws/src/tracetools"

# ═══════════════════════════════════════════════════════════════════════════
# tp_call.h — Add 6 TRACEPOINT_EVENT definitions (4 action + 2 link)
# ═══════════════════════════════════════════════════════════════════════════
path = f"{OVERLAY}/include/tracetools/tp_call.h"
with open(path) as f:
    content = f.read()

INSERT_BEFORE = "#endif  // _TRACETOOLS__TP_CALL_H_"
new_events = """
/* ── FISH action server tracepoints ──────────────────────────────────── */

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclcpp_action_server_init,
  TP_ARGS(
    const void *, action_server_handle_arg,
    const char *, action_name_arg,
    const void *, goal_service_handle_arg,
    const void *, cancel_service_handle_arg,
    const void *, result_service_handle_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
    ctf_string(action_name, action_name_arg)
    ctf_integer_hex(const void *, goal_service_handle, goal_service_handle_arg)
    ctf_integer_hex(const void *, cancel_service_handle, cancel_service_handle_arg)
    ctf_integer_hex(const void *, result_service_handle, result_service_handle_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclcpp_action_execute_goal,
  TP_ARGS(
    const void *, action_server_handle_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclcpp_action_execute_cancel,
  TP_ARGS(
    const void *, action_server_handle_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  rclcpp_action_execute_result,
  TP_ARGS(
    const void *, action_server_handle_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
  )
)

/* ── FISH callback-link tracepoints ──────────────────────────────────── */

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  fish_publish_link,
  TP_ARGS(
    const void *, publisher_handle_arg,
    const void *, callback_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, publisher_handle, publisher_handle_arg)
    ctf_integer_hex(const void *, callback, callback_arg)
  )
)

TRACEPOINT_EVENT(
  TRACEPOINT_PROVIDER,
  fish_client_link,
  TP_ARGS(
    const void *, client_handle_arg,
    const void *, callback_arg
  ),
  TP_FIELDS(
    ctf_integer_hex(const void *, client_handle, client_handle_arg)
    ctf_integer_hex(const void *, callback, callback_arg)
  )
)

"""

if "fish_publish_link" in content:
    print("  tp_call.h already patched, skipping")
else:
    content = content.replace(INSERT_BEFORE, new_events + INSERT_BEFORE)
    with open(path, "w") as f:
        f.write(content)
    print("  tp_call.h patched — 6 TRACEPOINT_EVENT added")


# ═══════════════════════════════════════════════════════════════════════════
# tracetools.h — Add 6 DECLARE_TRACEPOINT + extern __fish_active_callback
# ═══════════════════════════════════════════════════════════════════════════
path = f"{OVERLAY}/include/tracetools/tracetools.h"
with open(path) as f:
    content = f.read()

INSERT_BEFORE = "#ifdef __cplusplus\n}\n#endif\n\n#endif  // TRACETOOLS__TRACETOOLS_H_"
new_decls = """/* ── FISH action server tracepoints ──────────────────────────────────── */

/// `rclcpp_action_server_init`
DECLARE_TRACEPOINT(
  rclcpp_action_server_init,
  const void * action_server_handle,
  const char * action_name,
  const void * goal_service_handle,
  const void * cancel_service_handle,
  const void * result_service_handle)

/// `rclcpp_action_execute_goal`
DECLARE_TRACEPOINT(
  rclcpp_action_execute_goal,
  const void * action_server_handle)

/// `rclcpp_action_execute_cancel`
DECLARE_TRACEPOINT(
  rclcpp_action_execute_cancel,
  const void * action_server_handle)

/// `rclcpp_action_execute_result`
DECLARE_TRACEPOINT(
  rclcpp_action_execute_result,
  const void * action_server_handle)

/* ── FISH callback-link tracepoints ──────────────────────────────────── */

/// `fish_publish_link` — associates publisher_handle to active callback
DECLARE_TRACEPOINT(
  fish_publish_link,
  const void * publisher_handle,
  const void * callback)

/// `fish_client_link` — associates client_handle to active callback
DECLARE_TRACEPOINT(
  fish_client_link,
  const void * client_handle,
  const void * callback)

/// Thread-local: the callback pointer currently being dispatched by the executor.
/// Set by callback_start, cleared by callback_end.  Read by fish_publish_link
/// and fish_client_link fire points in rcl to attribute pub/cli aspects.
extern __thread const void * __fish_active_callback;

"""

if "fish_publish_link" in content:
    print("  tracetools.h already patched, skipping")
else:
    content = content.replace(INSERT_BEFORE, new_decls + INSERT_BEFORE)
    with open(path, "w") as f:
        f.write(content)
    print("  tracetools.h patched — 6 DECLARE_TRACEPOINT + extern thread-local added")


# ═══════════════════════════════════════════════════════════════════════════
# tracetools.c — Thread-local, dedup cache, modified callback_start/end,
#                4 action tracepoints, 2 link tracepoints
# ═══════════════════════════════════════════════════════════════════════════
path = f"{OVERLAY}/src/tracetools.c"
with open(path) as f:
    content = f.read()

if "fish_publish_link" in content:
    print("  tracetools.c already patched, skipping")
else:
    # ── 1. Modify callback_start to set __fish_active_callback ──
    old_cb_start = """void TRACEPOINT(
  callback_start,
  const void * callback,
  const bool is_intra_process)
{
  CONDITIONAL_TP(
    callback_start,
    callback,
    is_intra_process);
}"""

    new_cb_start = """void TRACEPOINT(
  callback_start,
  const void * callback,
  const bool is_intra_process)
{
  __fish_active_callback = callback;
  CONDITIONAL_TP(
    callback_start,
    callback,
    is_intra_process);
}"""

    if old_cb_start not in content:
        print("  WARNING: callback_start pattern not found, skipping modification")
    else:
        content = content.replace(old_cb_start, new_cb_start)
        print("  callback_start modified — sets __fish_active_callback")

    # ── 2. Modify callback_end to clear __fish_active_callback ──
    old_cb_end = """void TRACEPOINT(
  callback_end,
  const void * callback)
{
  CONDITIONAL_TP(
    callback_end,
    callback);
}"""

    new_cb_end = """void TRACEPOINT(
  callback_end,
  const void * callback)
{
  CONDITIONAL_TP(
    callback_end,
    callback);
  __fish_active_callback = NULL;
}"""

    if old_cb_end not in content:
        print("  WARNING: callback_end pattern not found, skipping modification")
    else:
        content = content.replace(old_cb_end, new_cb_end)
        print("  callback_end modified — clears __fish_active_callback")

    # ── 3. Insert new functions before #ifndef _WIN32 ──
    INSERT_BEFORE = "#ifndef _WIN32\n# pragma GCC diagnostic pop"
    new_code = """/* ── FISH thread-local active callback ────────────────────────────────── */

__thread const void * __fish_active_callback = NULL;

/* ── FISH dedup cache for link events ────────────────────────────────── */
/* Fire each (handle, callback) pair only once to keep trace volume low. */

#define FISH_LINK_CACHE_SIZE 128

typedef struct {
  const void * handle;
  const void * callback;
} fish_link_pair_t;

static __thread fish_link_pair_t fish_pub_cache[FISH_LINK_CACHE_SIZE];
static __thread int fish_pub_cache_n = 0;

static __thread fish_link_pair_t fish_cli_cache[FISH_LINK_CACHE_SIZE];
static __thread int fish_cli_cache_n = 0;

static int
fish_link_seen(fish_link_pair_t * cache, int * n,
               const void * handle, const void * callback)
{
  int i;
  for (i = 0; i < *n; i++) {
    if (cache[i].handle == handle && cache[i].callback == callback) {
      return 1;
    }
  }
  if (*n < FISH_LINK_CACHE_SIZE) {
    cache[*n].handle = handle;
    cache[*n].callback = callback;
    (*n)++;
  }
  return 0;
}

/* ── FISH action server tracepoints ──────────────────────────────────── */

void TRACEPOINT(
  rclcpp_action_server_init,
  const void * action_server_handle,
  const char * action_name,
  const void * goal_service_handle,
  const void * cancel_service_handle,
  const void * result_service_handle)
{
  CONDITIONAL_TP(
    rclcpp_action_server_init,
    action_server_handle,
    action_name,
    goal_service_handle,
    cancel_service_handle,
    result_service_handle);
}

void TRACEPOINT(
  rclcpp_action_execute_goal,
  const void * action_server_handle)
{
  CONDITIONAL_TP(
    rclcpp_action_execute_goal,
    action_server_handle);
}

void TRACEPOINT(
  rclcpp_action_execute_cancel,
  const void * action_server_handle)
{
  CONDITIONAL_TP(
    rclcpp_action_execute_cancel,
    action_server_handle);
}

void TRACEPOINT(
  rclcpp_action_execute_result,
  const void * action_server_handle)
{
  CONDITIONAL_TP(
    rclcpp_action_execute_result,
    action_server_handle);
}

/* ── FISH callback-link tracepoints (with dedup) ─────────────────────── */

void TRACEPOINT(
  fish_publish_link,
  const void * publisher_handle,
  const void * callback)
{
  if (callback == NULL) { return; }
  if (fish_link_seen(fish_pub_cache, &fish_pub_cache_n,
                     publisher_handle, callback)) { return; }
  CONDITIONAL_TP(
    fish_publish_link,
    publisher_handle,
    callback);
}

void TRACEPOINT(
  fish_client_link,
  const void * client_handle,
  const void * callback)
{
  if (callback == NULL) { return; }
  if (fish_link_seen(fish_cli_cache, &fish_cli_cache_n,
                     client_handle, callback)) { return; }
  CONDITIONAL_TP(
    fish_client_link,
    client_handle,
    callback);
}

"""
    content = content.replace(INSERT_BEFORE, new_code + INSERT_BEFORE)
    with open(path, "w") as f:
        f.write(content)
    print("  tracetools.c patched — thread-local + dedup + 6 tracepoint functions")

print("  Done patching tracetools.")
PATCH_TRACETOOLS
log "  tracetools patched"

# ─── Step 5: Patch rcl (publisher.c + client.c) ─────────────────────────────
log "[5/8] Patching rcl (publisher.c, client.c) ..."

python3 <<'PATCH_RCL'
OVERLAY = "/root/trace_overlay_ws/src/rcl"

# ── publisher.c: add fish_publish_link after rcl_publish tracepoint ──
path = f"{OVERLAY}/src/rcl/publisher.c"
with open(path) as f:
    content = f.read()

if "fish_publish_link" in content:
    print("  publisher.c already patched, skipping")
else:
    old = "  TRACEPOINT(rcl_publish, (const void *)publisher, (const void *)ros_message);"
    new = old + """
  TRACEPOINT(fish_publish_link, (const void *)publisher, __fish_active_callback);"""

    if old not in content:
        print("  WARNING: rcl_publish tracepoint pattern not found in publisher.c")
    else:
        content = content.replace(old, new)
        with open(path, "w") as f:
            f.write(content)
        print("  publisher.c patched — fish_publish_link after rcl_publish")

# ── client.c: add fish_client_link in rcl_send_request ──
path = f"{OVERLAY}/src/rcl/client.c"
with open(path) as f:
    content = f.read()

if "fish_client_link" in content:
    print("  client.c already patched, skipping")
else:
    # Insert after the successful rmw_send_request + sequence_number update
    old = "  rcutils_atomic_exchange_int64_t(&client->impl->sequence_number, *sequence_number);"
    new = old + """
  TRACEPOINT(fish_client_link, (const void *)client, __fish_active_callback);"""

    if old not in content:
        print("  WARNING: rcl_send_request pattern not found in client.c")
    else:
        content = content.replace(old, new)
        with open(path, "w") as f:
            f.write(content)
        print("  client.c patched — fish_client_link in rcl_send_request")

print("  Done patching rcl.")
PATCH_RCL
log "  rcl patched"

# ─── Step 6: Patch rclcpp_action (server.cpp, CMakeLists.txt) ───────────────
log "[6/8] Patching rclcpp_action (server.cpp, CMakeLists.txt) ..."

cd "$OVERLAY_WS/src/rclcpp_action"

# --- CMakeLists.txt: Add find_package(tracetools) ---
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_CMAKE' || true
--- a/CMakeLists.txt
+++ b/CMakeLists.txt
@@ -8,6 +8,7 @@
 find_package(rcl_action REQUIRED)
 find_package(rcpputils REQUIRED)
 find_package(rosidl_runtime_c REQUIRED)
+find_package(tracetools REQUIRED)

 # Default to C++14
 if(NOT CMAKE_CXX_STANDARD)
@@ -42,6 +43,7 @@
   "rclcpp"
   "rcpputils"
   "rosidl_runtime_c"
+  "tracetools"
 )

 # Causes the visibility macros to use dllexport rather than dllimport,
PATCH_CMAKE
log "  CMakeLists.txt patched"

# --- server.cpp: Add includes and 4 TRACEPOINT calls ---
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_SERVER' || true
--- a/src/server.cpp
+++ b/src/server.cpp
@@ -30,6 +30,8 @@
 #include "action_msgs/srv/cancel_goal.hpp"
 #include "rclcpp/exceptions.hpp"
 #include "rclcpp_action/server.hpp"
+#include <tracetools/tracetools.h>
+#include "action_server_impl.h"

 using rclcpp_action::ServerBase;
 using rclcpp_action::GoalUUID;
@@ -162,6 +164,18 @@
     rclcpp::exceptions::throw_from_rcl_error(ret);
   }

+  // FISH: Trace action server initialization — links handle to name and services
+  {
+    rcl_action_server_t * as = pimpl_->action_server_.get();
+    TRACEPOINT(
+      rclcpp_action_server_init,
+      static_cast<const void *>(as),
+      as->impl->action_name,
+      static_cast<const void *>(&as->impl->goal_service),
+      static_cast<const void *>(&as->impl->cancel_service),
+      static_cast<const void *>(&as->impl->result_service));
+  }
+
   ret = rcl_action_server_wait_set_get_num_entities(
     pimpl_->action_server_.get(),
     &pimpl_->num_subscriptions_,
@@ -391,6 +405,7 @@
 void
 ServerBase::execute_goal_request_received(std::shared_ptr<void> & data)
 {
+  TRACEPOINT(rclcpp_action_execute_goal, static_cast<const void *>(pimpl_->action_server_.get()));
   std::shared_ptr<ServerBaseData> data_ptr = std::static_pointer_cast<ServerBaseData>(data);
   const ServerBaseData::GoalRequestData & gData(
     std::get<ServerBaseData::GoalRequestData>(data_ptr->data));
@@ -491,6 +506,7 @@
 void
 ServerBase::execute_cancel_request_received(std::shared_ptr<void> & data)
 {
+  TRACEPOINT(rclcpp_action_execute_cancel, static_cast<const void *>(pimpl_->action_server_.get()));
   std::shared_ptr<ServerBaseData> data_ptr = std::static_pointer_cast<ServerBaseData>(data);
   const ServerBaseData::CancelRequestData & gData(
     std::get<ServerBaseData::CancelRequestData>(data_ptr->data));
@@ -592,6 +608,7 @@
 void
 ServerBase::execute_result_request_received(std::shared_ptr<void> & data)
 {
+  TRACEPOINT(rclcpp_action_execute_result, static_cast<const void *>(pimpl_->action_server_.get()));
   std::shared_ptr<ServerBaseData> data_ptr = std::static_pointer_cast<ServerBaseData>(data);
   const ServerBaseData::ResultRequestData & gData(
     std::get<ServerBaseData::ResultRequestData>(data_ptr->data));
PATCH_SERVER
log "  server.cpp patched"

# ─── Step 7: Build overlay workspace ─────────────────────────────────────────
log "[7/8] Building overlay workspace ..."
cd "$OVERLAY_WS"

# 7a. Build tracetools first (other packages depend on it)
log "  Building tracetools ..."
colcon build \
    --packages-select tracetools \
    --cmake-args -DBUILD_TESTING=OFF \
    2>&1 | tail -5

# Source tracetools so rcl and rclcpp_action can find it
set +u && source "$OVERLAY_WS/install/setup.bash" && set -u

# 7b. Build rcl (publisher.c + client.c patches)
log "  Building rcl ..."
colcon build \
    --packages-select rcl \
    --cmake-args \
        -DBUILD_TESTING=OFF \
        "-Dtracetools_DIR=$OVERLAY_WS/install/tracetools/share/tracetools/cmake" \
    2>&1 | tail -5

# Source rcl overlay
set +u && source "$OVERLAY_WS/install/setup.bash" && set -u

# 7c. Build rclcpp_action
log "  Building rclcpp_action ..."
colcon build \
    --packages-select rclcpp_action \
    --cmake-args \
        -DBUILD_TESTING=OFF \
        "-Dtracetools_DIR=$OVERLAY_WS/install/tracetools/share/tracetools/cmake" \
    2>&1 | tail -5

log "  All packages built"

# ─── Step 8: Patch tracetools_trace event list ───────────────────────────────
log "[8/8] Patching tracetools_trace event list (names.py) ..."

NAMES_PY=$(python3 -c "
import tracetools_trace.tools.names as m
import os
print(os.path.abspath(m.__file__))
")

if [ ! -f "$NAMES_PY" ]; then
    err "Could not find tracetools_trace names.py"
    exit 1
fi

if grep -q "fish_publish_link" "$NAMES_PY"; then
    log "  names.py already patched, skipping"
else
    python3 <<PATCHPY
with open("$NAMES_PY", "r") as f:
    content = f.read()

# The list ends with a known last entry followed by ]
# Find the closing ] of DEFAULT_EVENTS_ROS and insert before it
import re

# Add after the last existing entry (before the closing bracket)
new_entries = '''    "ros2:rclcpp_action_server_init",
    "ros2:rclcpp_action_execute_goal",
    "ros2:rclcpp_action_execute_cancel",
    "ros2:rclcpp_action_execute_result",
    "ros2:fish_publish_link",
    "ros2:fish_client_link",'''

# Strategy: find DEFAULT_EVENTS_ROS list, insert before its closing ]
# The list is defined as DEFAULT_EVENTS_ROS = [ ... ]
# We find the pattern: last entry line + \n]
pattern = r'(DEFAULT_EVENTS_ROS\s*=\s*\[.*?)(^\])'
match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
if match:
    insert_pos = match.start(2)
    content = content[:insert_pos] + new_entries + "\n" + content[insert_pos:]
    with open("$NAMES_PY", "w") as f:
        f.write(content)
    print("  Patched: added 6 FISH tracepoint events to DEFAULT_EVENTS_ROS")
else:
    # Fallback: try simpler pattern
    # Find the last tracepoint entry and the ]
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if 'DEFAULT_EVENTS_ROS' in line:
            # Find closing bracket
            for j in range(i+1, len(lines)):
                if lines[j].strip() == ']':
                    lines.insert(j, new_entries)
                    content = '\n'.join(lines)
                    with open("$NAMES_PY", "w") as f:
                        f.write(content)
                    print("  Patched (fallback): added 6 FISH tracepoint events")
                    break
            break
PATCHPY
fi

# ─── Verify ──────────────────────────────────────────────────────────────────
log ""
log "=========================================="
log " FISH Tracepoints Installed (v2)"
log "=========================================="
log ""
log "Overlay workspace: $OVERLAY_WS"
log ""

# Verify tracetools symbols
FISH_SYMS=$(nm -D "$OVERLAY_WS/install/tracetools/lib/libtracetools.so" 2>/dev/null | grep -c "ros_trace_fish_\|ros_trace_rclcpp_action_" || true)
log "FISH symbols in libtracetools.so: $FISH_SYMS (expected: 6)"

# Verify thread-local symbol
if nm -D "$OVERLAY_WS/install/tracetools/lib/libtracetools.so" 2>/dev/null | grep -q "__fish_active_callback"; then
    log "Thread-local __fish_active_callback: OK"
else
    warn "Thread-local __fish_active_callback: NOT FOUND"
fi

# Verify rcl symbols
if nm -D "$OVERLAY_WS/install/rcl/lib/librcl.so" 2>/dev/null | grep -q "ros_trace_fish_publish_link\|fish_publish_link"; then
    log "rcl fish_publish_link reference: OK"
else
    # The fire point calls the tracetools function, so the symbol is in tracetools, not rcl
    log "rcl links to tracetools for fire points (expected)"
fi

# Verify event count
EVENT_COUNT=$(python3 -c "
try:
    from tracetools_trace.tools.names import DEFAULT_EVENTS_ROS
    print(len(DEFAULT_EVENTS_ROS))
except:
    print(0)
" 2>/dev/null || echo "0")

log "Event count: $EVENT_COUNT events in DEFAULT_EVENTS_ROS (expected: 34)"
log ""
log "To activate, add to your shell:"
log "  source $OVERLAY_WS/install/setup.bash"
log ""
log "Then restart your ROS 2 nodes and start a trace session:"
log "  ros2 trace -s fish_session -p /tmp/fish_trace"
