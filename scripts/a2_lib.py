#!/usr/bin/env python3
"""Reusable A2 sheet building helpers + per-node-type formulas.

Each benchmark's A2 spreadsheet has the SAME REFERENCE catalog. The per-bench
expected node table is composed from these primitives.

Node-type primitives (all derived from validated apriltag work + nitros_inheritance.txt):
  VCC1_RCLCPP       = (7,7,2)   # rclcpp::Node: 1 /parameter_events sub + 6 param svc; pub_aspect: /rosout + /parameter_events
  VCC1_RCLPY        = (6,6,2)   # rclpy.node.Node: 6 param svc, no TimeSource auto-attach
  VCC1_CM           = (1,1,1)   # rclcpp_components::ComponentManager (TimeSource sub + /rosout only)
  CONTAINER_SVCS    = (3,3,0)   # ComponentManager adds 3 services (_container/load_node, _container/unload_node, _container/list_nodes)
  ROS_TRANSFORM_LISTENER = (3,3,1)  # tf2_ros::transform_listener internal Node (minimal /tf + /tf_static + 1 param svc)
  ROS_STATIC_TRANSFORM_PUBLISHER = (7,7,2)  # full rclcpp Node + /tf_static pub

Compose helpers:
  nitros_node(n_in, n_out, runtime_extra=0): returns expected for a NITROS NEGOTIATED node
  nitros_playback(n_formats): NitrosPlaybackNode with n NITROS formats
  nitros_monitor(mode, peer_topic_count=1): mode in {nitros_neg, nitros_compat_only, generic, ros_type}
  data_loader_node(): same as ros2_benchmark::DataLoaderNode
"""

VCC1_RCLCPP = (7, 7, 2)
VCC1_RCLPY  = (6, 6, 2)
VCC1_CM     = (1, 1, 1)

def add(a, b):
    return tuple(x+y for x, y in zip(a, b))

def container_expected():
    """ComponentManager: TimeSource sub + 3 _container/* services."""
    return add(VCC1_CM, (3, 3, 0))  # (4, 4, 1)

def controller_expected():
    """ros2_benchmark Controller (rclpy)."""
    return VCC1_RCLPY  # (6, 6, 2)

def launch_ros_expected():
    """launch_ros internal rclpy Node."""
    return VCC1_RCLPY  # (6, 6, 2)

def data_loader_expected():
    """ros2_benchmark::DataLoaderNode: VCC1 + 4 user services."""
    return add(VCC1_RCLCPP, (4, 4, 0))  # (11, 11, 2)

def nitros_node(n_in, n_out, runtime_extra_E=0, runtime_extra_F=0):
    """NITROS NEGOTIATED node (NitrosNode subclass) expected E,F,pub_aspect.

    Formula:
        E = 7 + 1 + 2*n_in + 2*n_out
            VCC1 (7) + VCCI2 negotiation_timer (1) + each input compat sub + NegSub (2) + each output graph_change_timer + _supported_types sub (2)
        F = same as E
        pub_aspect = 2 + n_in + 2*n_out
            VCC1 (2) + per input _supported_types pub (1) + per output (compat pub + NegPub TopicsInfo pub, 2)
    """
    E = 7 + 1 + 2*n_in + 2*n_out + runtime_extra_E
    F = 7 + 1 + 2*n_in + 2*n_out + runtime_extra_F
    P = 2 + n_in + 2*n_out
    return (E, F, P)

def nitros_playback_node_generic(n_formats):
    """NitrosPlaybackNode where ALL data_formats are ROS msg type names (not NITROS supported).

    Each format → CreateGenericPubSub (parent's method):
        - 1 GenericPublisher on inputN: VCC_GP → 1 pub_aspect
        - 1 GenericSubscription on buffer/inputN: VCC_GS → 1 E + 1 F
    Per format: +1 E + 1 F + 1 pub_aspect

    Base: VCC1 + 3 user services = 10 E + 10 F + 2 pub_aspect
    """
    E = 7 + 3 + n_formats
    F = 7 + 3 + n_formats
    P = 2 + n_formats
    return (E, F, P)


def transform_listener_impl():
    """tf2_ros::TransformListener internal Node — minimal.

    Spawns a 'transform_listener_impl_<hash>' Node with only:
        - 1 /parameter_events sub (TimeSource)
        - 2 subs: /tf, /tf_static
    NO 6 param services (created with NodeOptions that disable them).
    """
    return (3, 3, 1)  # 3 subs, 0 svc; 1 pub_aspect for /rosout


def static_transform_publisher():
    """ros2 run tf2_ros static_transform_publisher — full rclcpp Node + /tf_static pub."""
    return (7, 7, 3)  # VCC1 (7E+7F+2P) + 1 pub_aspect for /tf_static


def nitros_playback_node(n_formats):
    """NitrosPlaybackNode: PlaybackNode(rclcpp, 3 user svc) + N CreateNitrosPubSub.

    Per format:
        - 1 NitrosPublisher NEGOTIATED on inputN: VCCI14 compat pub (pub_aspect)
          + VCCI15 (NegPub.ctor: NegTopicsInfo pub (pub_aspect) + graph_change_timer (1E+1F);
                    NegPub.start(): _supported_types sub (1E+1F))
          → 2 E + 2 F + 2 pub_aspect per format
        - 1 NITROS compat recording sub on buffer/inputN: 1 E + 1 F
        Total per format: 3 E + 3 F + 2 pub_aspect

    Base: VCC1 + 3 user services (start_recording, stop_recording, play_messages):
        7+3=10 E, 7+3=10 F, 2 pub_aspect
    """
    E = 7 + 3 + 3*n_formats
    F = 7 + 3 + 3*n_formats
    P = 2 + 2*n_formats
    return (E, F, P)

def nitros_monitor_node_generic(monitor_data_format):
    """NitrosMonitorNode using GenericSubscription path.

    Triggered when:
      - hasFormat(monitor_data_format) returns false (data format is a ROS msg type name, not a NITROS supported_type_name)
      - OR use_nitros_type_monitor_sub=False AND ros_type_name doesn't match the helper macro list

    Path: NitrosMonitorNode::CreateMonitorSubscriber → falls to MonitorNode::CreateGenericTypeMonitorSubscriber
        → create_generic_subscription("output", ...)

    Vertices: VCC1 (7+7) + 2 user services (start_monitoring, stop_monitoring) + 1 generic sub (1+1)
    """
    E = 7 + 2 + 1
    F = 7 + 2 + 1
    P = 2
    return (E, F, P)

def nitros_monitor_node_nitros_sub():
    """NitrosMonitorNode using NITROS NEGOTIATED sub path.

    Triggered when use_nitros_type_monitor_sub=True AND hasFormat(monitor_data_format)=True.

    Path: CreateNitrosMonitorSubscriber → NitrosSubscriber NEGOTIATED on "output"
        - VCCI8: 1 compat sub (1 E + 1 F)
        - VCCI9: 1 NegSub TopicsInfo sub (1 E + 1 F) + 1 _supported_types pub (pub_aspect)

    Vertices: VCC1 + 2 user svc + (2 E + 2 F + 1 pub_aspect from NitrosSubscriber)
    """
    E = 7 + 2 + 2
    F = 7 + 2 + 2
    P = 2 + 1
    return (E, F, P)

def nitros_monitor_node_ros_type():
    """NitrosMonitorNode using ROS type sub path (use_nitros_type_monitor_sub=False + matching ROS msg type).

    Path: CreateROSTypeMonitorSubscriber<T> → create_subscription<T>("output", ...)

    Vertices: VCC1 + 2 user svc + 1 plain sub (1 E + 1 F)
    """
    E = 7 + 2 + 1
    F = 7 + 2 + 1
    P = 2
    return (E, F, P)

# ─────────────────── Compose expected for whole benchmark ───────────────────

def managed_nitros_node(n_managed_in=1, n_managed_out=1):
    """Plain rclcpp::Node with ManagedNitrosSubscriber × n_in + ManagedNitrosPublisher × n_out.

    Used by ImageToTensorNode, ImageTensorNormalizeNode, etc — pure rclcpp::Node
    (not NitrosNode subclass) that uses ManagedNitros wrappers.

    Per ManagedSub: VCCI8 (1 E + 1 F) + VCCI9 (1 E + 1 F + 1 pub_aspect) = 2 E + 2 F + 1 pub_aspect
    Per ManagedPub: VCCI14 (1 pub_aspect) + VCCI15 (1 pub_aspect + 2 E + 2 F) = 2 E + 2 F + 2 pub_aspect

    No VCCI2 negotiation_timer (not a NitrosNode); no runtime gxf_heartbeat.
    """
    E = 7 + 2*n_managed_in + 2*n_managed_out
    F = 7 + 2*n_managed_in + 2*n_managed_out
    P = 2 + n_managed_in + 2*n_managed_out
    return (E, F, P)

def total(expected_dict):
    """Sum tuples across a dict of node_name → (E, F, P)."""
    tE = sum(v[0] for v in expected_dict.values())
    tF = sum(v[1] for v in expected_dict.values())
    tP = sum(v[2] for v in expected_dict.values())
    return (tE, tF, tP)
