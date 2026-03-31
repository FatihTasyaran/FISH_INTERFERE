# Custom Tracepoints

FISH adds 11 custom LTTng tracepoints to ROS 2 to close visibility gaps
in the standard ros2_tracing instrumentation. These are installed via
`install_fish_tracepoints` and built in an overlay workspace.

## Action Server (4 events)

C++ patches to `rclcpp_action/src/server.cpp`. Captures action server
initialization and dispatch events that are invisible in standard tracing
because action servers use the Waitable interface, bypassing ServiceBase.

| Event | Payload | Description |
|-------|---------|-------------|
| `ros2:rclcpp_action_server_init` | action_server_handle, action_name, goal/cancel/result service handles | Links action handle to name and component services |
| `ros2:rclcpp_action_execute_goal` | action_server_handle | Goal request dispatch entry |
| `ros2:rclcpp_action_execute_cancel` | action_server_handle | Cancel request dispatch entry |
| `ros2:rclcpp_action_execute_result` | action_server_handle | Result request dispatch entry |

## rclpy Callback Chain (5 events)

Python ctypes bridge calling into libtracetools.so. Provides callback chain
visibility for Python nodes that is natively available only for C++ nodes.

| Event | Payload | Description |
|-------|---------|-------------|
| `ros2:rclpy_subscription_callback_added` | subscription_handle, callback | Links subscription to Python callback ID |
| `ros2:rclpy_service_callback_added` | service_handle, callback | Links service to Python callback ID |
| `ros2:rclpy_timer_callback_added` | timer_handle, callback | Links timer to Python callback ID |
| `ros2:rclpy_timer_link_node` | timer_handle, node_handle | Links timer to parent node |
| `ros2:rclpy_callback_register` | callback, symbol | Maps callback ID to Python qualname |

Existing `ros2:callback_start` and `ros2:callback_end` events are reused for
rclpy runtime callback boundaries (injected into `rclpy/executors.py`).

## Service Client (2 events)

C++ patches to `rclcpp/include/rclcpp/client.hpp` and Python patches to
`rclpy/client.py`, `rclpy/executors.py`, and `rclpy/action/client.py`.
Captures service request/response timing that is completely invisible in
standard tracing.

| Event | Payload | Description |
|-------|---------|-------------|
| `ros2:rclcpp_client_request_sent` | client_handle, sequence_number | Client sends a service request |
| `ros2:rclcpp_client_response_received` | client_handle, sequence_number, response_type | Client receives a service response |

`response_type`: 0 = future-only, 1 = callback, 2 = callback with request.

When a callback is present (type 1 or 2), existing `ros2:callback_start` and
`ros2:callback_end` events are fired around the callback invocation.

Round-trip latency can be measured as:
`response_received.ts - request_sent.ts`

### Patched code paths

| Code Path | Language | File | Status |
|-----------|----------|------|--------|
| Client.async_send_request | C++ | rclcpp/client.hpp | Patched |
| Client.handle_response | C++ | rclcpp/client.hpp | Patched |
| Client.call_async | Python | rclpy/client.py | Patched |
| Executor._execute_client | Python | rclpy/executors.py | Patched |
| ActionClient.send_goal_async | Python | rclpy/action/client.py | Patched |
| ActionClient.execute | Python | rclpy/action/client.py | Patched |

## Reused Standard Events

These existing ros2_tracing events are leveraged by FISH without modification:

| Event | Used for |
|-------|----------|
| `ros2:callback_start` / `callback_end` | Runtime callback timing (rclpy via ctypes, client callbacks) |
| `ros2:rcl_client_init` | Structural: client → service name |
| `ros2:rcl_service_init` | Structural: server → service name |
| `ros2:rcl_subscription_init` | Structural: subscriber → topic |
| `ros2:rcl_publisher_init` | Structural: publisher → topic |
| `ros2:rcl_timer_init` | Structural: timer → period |
| `ros2:rcl_publish` / `rcl_take` | Runtime: message flow |
| `ros2:rclcpp_executor_execute` | Runtime: executor dispatch |

## Event Count Summary

| Flag | Events | Total with base (28) |
|------|--------|---------------------|
| `--action` | 4 | 32 |
| `--rclpy` | 5 | 33 |
| `--client` | 2 | 30 |
| `--all` | 11 | 39 |
