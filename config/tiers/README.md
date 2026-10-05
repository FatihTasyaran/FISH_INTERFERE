# FISH tracing tiers (2026-10-03)
`FISH_TRACE_TIER=min|default|max` selects which per-instance events are added to the base whitelist
(`config/fish_events.txt`, the structural events: registrations, lifetimes, callback windows, one-shot links).
Everything the base does not give is then either measured (tier has the event) or inferred (model heuristic).

| tier | adds | what becomes exact | approx. UST volume (Autoware, 160 s) |
|---|---|---|---|
| min | nothing | structure, callback windows, GPU binding; hops only by time order (ipc_time), no release instants | ~1.5 M |
| default | rcl_publish, rmw_take(taken==1), fish_rcl_take, fish_rclcpp_intra_publish/_take, fish_rcl_publish, fish_tf_lookup | per-message hops and release instants (publish instant), polled-msg ages, tf ages, OpenMP regions (shim events are in the base) | ~4.2 M |
| max | + fish_dds_rhc_store (arrival), fish_rcl_wait_enter/exit, rclcpp_executor_wait_for_work/get_next_ready/execute, rcl_take, rclcpp_take, rclcpp_publish, rmw service request/response events, rcl_lifecycle_* | transport vs queue split, exact executor sleep/wake and dispatch decision (no kernel heuristic), service response instants | ~8 M |
Kernel session (sched_switch, sched_waking, futex) and the OpenMP shim are orthogonal knobs (FISH_KERNEL_SCHED, FISH_KERNEL_FUTEX, FISH_GOMP_SHIM).
The paper experiment: run the same workload at the three tiers, compare (a) overhead, (b) which model quantities change when a heuristic replaces an event.
