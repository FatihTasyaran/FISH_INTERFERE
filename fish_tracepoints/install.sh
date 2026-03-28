#!/bin/bash
# =============================================================================
# FISH Action Server Tracepoint Installer
# =============================================================================
# Installs 4 custom LTTng tracepoints for ROS 2 action server dispatch into
# a ROS 2 Humble environment via an overlay workspace.
#
# Tracepoints added:
#   ros2:rclcpp_action_server_init    — links action handle to name + services
#   ros2:rclcpp_action_execute_goal   — goal request dispatch entry
#   ros2:rclcpp_action_execute_cancel — cancel request dispatch entry
#   ros2:rclcpp_action_execute_result — result request dispatch entry
#
# Also patches tracetools_trace/tools/names.py so `ros2 trace` captures them.
#
# Target: aircraft-fish-image:latest (ROS 2 Humble)
# Packages: tracetools 4.1.2, rclcpp 16.0.19, rcl 5.3.12
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
log "[1/7] Creating overlay workspace at $OVERLAY_WS ..."
mkdir -p "$OVERLAY_WS/src"

# ─── Step 2: Clone source repositories ───────────────────────────────────────
log "[2/7] Cloning source repositories ..."
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
log "[3/7] Copying packages into workspace ..."

# tracetools
if [ -d "$OVERLAY_WS/src/tracetools" ]; then
    warn "  $OVERLAY_WS/src/tracetools already exists, removing for clean state"
    rm -rf "$OVERLAY_WS/src/tracetools"
fi
cp -r "$CLONE_DIR/ros2_tracing/tracetools" "$OVERLAY_WS/src/tracetools"

# rclcpp_action
if [ -d "$OVERLAY_WS/src/rclcpp_action" ]; then
    warn "  $OVERLAY_WS/src/rclcpp_action already exists, removing for clean state"
    rm -rf "$OVERLAY_WS/src/rclcpp_action"
fi
cp -r "$CLONE_DIR/rclcpp/rclcpp_action" "$OVERLAY_WS/src/rclcpp_action"

# Copy private rcl_action header (needed to access action_server_impl struct)
cp "$CLONE_DIR/rcl/rcl_action/src/rcl_action/action_server_impl.h" \
   "$OVERLAY_WS/src/rclcpp_action/src/action_server_impl.h"
log "  Copied action_server_impl.h from rcl source"

# ─── Step 4: Apply tracetools patches ────────────────────────────────────────
log "[4/7] Patching tracetools (tp_call.h, tracetools.h, tracetools.c) ..."

# --- tp_call.h: Add 4 TRACEPOINT_EVENT definitions before final #endif ---
cd "$OVERLAY_WS/src/tracetools"
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_TP_CALL'
--- a/include/tracetools/tp_call.h
+++ b/include/tracetools/tp_call.h
@@ -408,6 +408,58 @@
   )
 )

+TRACEPOINT_EVENT(
+  TRACEPOINT_PROVIDER,
+  rclcpp_action_server_init,
+  TP_ARGS(
+    const void *, action_server_handle_arg,
+    const char *, action_name_arg,
+    const void *, goal_service_handle_arg,
+    const void *, cancel_service_handle_arg,
+    const void *, result_service_handle_arg
+  ),
+  TP_FIELDS(
+    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
+    ctf_string(action_name, action_name_arg)
+    ctf_integer_hex(const void *, goal_service_handle, goal_service_handle_arg)
+    ctf_integer_hex(const void *, cancel_service_handle, cancel_service_handle_arg)
+    ctf_integer_hex(const void *, result_service_handle, result_service_handle_arg)
+  )
+)
+
+TRACEPOINT_EVENT(
+  TRACEPOINT_PROVIDER,
+  rclcpp_action_execute_goal,
+  TP_ARGS(
+    const void *, action_server_handle_arg
+  ),
+  TP_FIELDS(
+    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
+  )
+)
+
+TRACEPOINT_EVENT(
+  TRACEPOINT_PROVIDER,
+  rclcpp_action_execute_cancel,
+  TP_ARGS(
+    const void *, action_server_handle_arg
+  ),
+  TP_FIELDS(
+    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
+  )
+)
+
+TRACEPOINT_EVENT(
+  TRACEPOINT_PROVIDER,
+  rclcpp_action_execute_result,
+  TP_ARGS(
+    const void *, action_server_handle_arg
+  ),
+  TP_FIELDS(
+    ctf_integer_hex(const void *, action_server_handle, action_server_handle_arg)
+  )
+)
+
 #endif  // _TRACETOOLS__TP_CALL_H_

 #include <lttng/tracepoint-event.h>
PATCH_TP_CALL
log "  tp_call.h patched"

# --- tracetools.h: Add 4 DECLARE_TRACEPOINT blocks before #ifdef __cplusplus ---
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_TRACETOOLS_H'
--- a/include/tracetools/tracetools.h
+++ b/include/tracetools/tracetools.h
@@ -482,6 +482,55 @@
   rclcpp_executor_execute,
   const void * handle)

+/// `rclcpp_action_server_init`
+/**
+ * Initialization of an action server.
+ * Links the action server handle to its name and component service handles.
+ *
+ * \param[in] action_server_handle pointer to the rcl_action_server_t
+ * \param[in] action_name the name of the action
+ * \param[in] goal_service_handle pointer to the goal service
+ * \param[in] cancel_service_handle pointer to the cancel service
+ * \param[in] result_service_handle pointer to the result service
+ */
+DECLARE_TRACEPOINT(
+  rclcpp_action_server_init,
+  const void * action_server_handle,
+  const char * action_name,
+  const void * goal_service_handle,
+  const void * cancel_service_handle,
+  const void * result_service_handle)
+
+/// `rclcpp_action_execute_goal`
+/**
+ * Start of action goal request dispatch.
+ *
+ * \param[in] action_server_handle pointer to the rcl_action_server_t
+ */
+DECLARE_TRACEPOINT(
+  rclcpp_action_execute_goal,
+  const void * action_server_handle)
+
+/// `rclcpp_action_execute_cancel`
+/**
+ * Start of action cancel request dispatch.
+ *
+ * \param[in] action_server_handle pointer to the rcl_action_server_t
+ */
+DECLARE_TRACEPOINT(
+  rclcpp_action_execute_cancel,
+  const void * action_server_handle)
+
+/// `rclcpp_action_execute_result`
+/**
+ * Start of action result request dispatch.
+ *
+ * \param[in] action_server_handle pointer to the rcl_action_server_t
+ */
+DECLARE_TRACEPOINT(
+  rclcpp_action_execute_result,
+  const void * action_server_handle)
+
 #ifdef __cplusplus
 }
 #endif
PATCH_TRACETOOLS_H
log "  tracetools.h patched"

# --- tracetools.c: Add 4 void TRACEPOINT implementations before #pragma pop ---
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_TRACETOOLS_C'
--- a/src/tracetools.c
+++ b/src/tracetools.c
@@ -362,6 +362,50 @@
     handle);
 }

+void TRACEPOINT(
+  rclcpp_action_server_init,
+  const void * action_server_handle,
+  const char * action_name,
+  const void * goal_service_handle,
+  const void * cancel_service_handle,
+  const void * result_service_handle)
+{
+  CONDITIONAL_TP(
+    rclcpp_action_server_init,
+    action_server_handle,
+    action_name,
+    goal_service_handle,
+    cancel_service_handle,
+    result_service_handle);
+}
+
+void TRACEPOINT(
+  rclcpp_action_execute_goal,
+  const void * action_server_handle)
+{
+  CONDITIONAL_TP(
+    rclcpp_action_execute_goal,
+    action_server_handle);
+}
+
+void TRACEPOINT(
+  rclcpp_action_execute_cancel,
+  const void * action_server_handle)
+{
+  CONDITIONAL_TP(
+    rclcpp_action_execute_cancel,
+    action_server_handle);
+}
+
+void TRACEPOINT(
+  rclcpp_action_execute_result,
+  const void * action_server_handle)
+{
+  CONDITIONAL_TP(
+    rclcpp_action_execute_result,
+    action_server_handle);
+}
+
 #ifndef _WIN32
 # pragma GCC diagnostic pop
 #else
PATCH_TRACETOOLS_C
log "  tracetools.c patched"

# ─── Step 5: Apply rclcpp_action patches ─────────────────────────────────────
log "[5/7] Patching rclcpp_action (server.cpp, CMakeLists.txt) ..."

cd "$OVERLAY_WS/src/rclcpp_action"

# --- CMakeLists.txt: Add find_package(tracetools) and ament_target_dependencies ---
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_CMAKE'
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
patch -p1 --forward --no-backup-if-mismatch <<'PATCH_SERVER'
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

# ─── Step 6: Build overlay workspace ─────────────────────────────────────────
log "[6/7] Building overlay workspace ..."
cd "$OVERLAY_WS"

# Build tracetools first, then rclcpp_action with explicit path to overlay tracetools
colcon build \
    --packages-select tracetools \
    --cmake-args -DBUILD_TESTING=OFF \
    2>&1 | tail -5

log "  tracetools built"

# Source tracetools so rclcpp_action can find it
set +u && source "$OVERLAY_WS/install/setup.bash" && set -u

colcon build \
    --packages-select rclcpp_action \
    --cmake-args \
        -DBUILD_TESTING=OFF \
        "-Dtracetools_DIR=$OVERLAY_WS/install/tracetools/share/tracetools/cmake" \
    2>&1 | tail -5

log "  rclcpp_action built"

# ─── Step 7: Patch tracetools_trace event list ───────────────────────────────
log "[7/7] Patching tracetools_trace event list (names.py) ..."

NAMES_PY=$(python3 -c "
import tracetools_trace.tools.names as m
import os
print(os.path.abspath(m.__file__))
")

if [ ! -f "$NAMES_PY" ]; then
    err "Could not find tracetools_trace names.py"
    exit 1
fi

# Check if already patched
if grep -q "rclcpp_action_server_init" "$NAMES_PY"; then
    log "  names.py already patched, skipping"
else
    python3 <<PATCHPY
import re

with open("$NAMES_PY", "r") as f:
    content = f.read()

# Find the last entry in DEFAULT_EVENTS_ROS and add our 4 entries after it
# The last standard entry is tracepoints.rclcpp_executor_execute
new_entries = '''    "ros2:rclcpp_action_server_init",
    "ros2:rclcpp_action_execute_goal",
    "ros2:rclcpp_action_execute_cancel",
    "ros2:rclcpp_action_execute_result",'''

# Insert after rclcpp_executor_execute line
content = content.replace(
    "    tracepoints.rclcpp_executor_execute,\n]",
    "    tracepoints.rclcpp_executor_execute,\n" + new_entries + "\n]"
)

with open("$NAMES_PY", "w") as f:
    f.write(content)

print("  Patched: added 4 action tracepoint events to DEFAULT_EVENTS_ROS")
PATCHPY
fi

# ─── Verify ──────────────────────────────────────────────────────────────────
log ""
log "=========================================="
log " FISH Action Tracepoints Installed"
log "=========================================="
log ""
log "Overlay workspace: $OVERLAY_WS"
log ""

# Verify symbols are exported
if nm -D "$OVERLAY_WS/install/tracetools/lib/libtracetools.so" 2>/dev/null | grep -q "ros_trace_rclcpp_action_server_init"; then
    log "Symbol check: OK (ros_trace_rclcpp_action_* found in libtracetools.so)"
else
    warn "Symbol check: FAILED — tracepoint functions not exported"
    warn "The build may have placed functions after #endif guard"
fi

# Verify event count
EVENT_COUNT=$(python3 -c "
source_overlay = True
try:
    from tracetools_trace.tools.names import DEFAULT_EVENTS_ROS
    print(len(DEFAULT_EVENTS_ROS))
except:
    print(0)
" 2>/dev/null || echo "0")

log "Event count: $EVENT_COUNT events in DEFAULT_EVENTS_ROS (expected: 32)"
log ""
log "To activate, add to your shell:"
log "  source $OVERLAY_WS/install/setup.bash"
log ""
log "Then restart your ROS 2 nodes and start a trace session:"
log "  ros2 trace -s fish_session -p /tmp/fish_trace"
