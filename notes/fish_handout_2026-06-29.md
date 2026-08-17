# FISH — Agent Handout (snapshot 2026-06-29)

A current-state briefing for an agent who needs to be productive on FISH
quickly. Skim this first, then dive into the files cited. For deeper
material see `notes/fish_manual.md` (user-facing install + commands) and
`notes/important_details.txt` (instrumentation deep dives).

Repo root: `/home/tue037807/fish_interfere/`.


## 1. What FISH is

FISH = Function-level Inspection of Scheduling Hierarchies. A profiler /
observability framework for ROS 2 systems. From inside a "tracing-ready"
container, FISH captures:

- LTTng userspace tracepoints (vanilla ros2_tracing + ~12 FISH-custom
  `ros2:fish_*` tracepoints we patch into rclcpp / rclpy / NITROS).
- Nsight Systems / nsys GPU traces (CUDA API, kernel launches, optional
  `--cuda-event-trace` for stream sync graphs).
- /proc snapshots (cmdline, status, fd, taskset) for every PID.
- Process / container topology.

It then builds a 4-layer graph model of the running system and
visualises it. End audience: ROS 2 perf engineers + the FISH paper.


## 2. The 4-layer entity model

| Layer | Symbol | What lives here |
|-------|--------|-----------------|
| L-1   | CN     | container (cgroup) |
| L0    | EX     | rclcpp::Executor (one per ros2 launch component) |
| L1    | N      | rclcpp::Node |
| L2    | E      | entity: `sub` / `pub` / `srv` / `cli` / `tmr` / **`wait`** (Waitable) |
| L3    | F      | function / callback (one cb_addr) |

Edges:

- `contains` edges hierarchically link layers (CN → EX → N → E → F).
- `comm` edges run between F's: receive_link (msg arrives at sub) and
  publish_link (msg published from active callback), joined on the
  `__fish_active_callback` thread-local.
- NITROS GXF "intra-node" edges: receiver→codelet→transmitter, derived
  from two static-topology tracepoints (sub_link, pub_link) we fire in
  NITROS init. Pipeline structure inside one container.

A "WCC" / "FT" (renamed for the talk: Fish Task) is a weakly-connected
component on the F-subgraph using only comm + GXF intra-node edges —
each FT is one pipeline-of-callbacks-and-publishers, conceptually one
task graph.

### Waitable as 5th entity

Recent: Waitable is now first-class peer to sub/srv/tmr. Reason: rclcpp's
Executor::execute_any_executable has 5 dispatch branches, and the 5th
(`any_exec.waitable->execute(...)`) covered NITROS publish hot path,
rclcpp_action Client/Server, QoSEventHandler, rosbag2 PlayerImpl, and
intra-process subs. The previous heuristic-based attribution mis-assigned
their callbacks. See `notes/noticing_waitables.txt` for the postmortem.

The 5th category is captured by a generic callback_start/_end wrap
around `any_exec.waitable->execute` in `executor.cpp` plus an init
tracepoint at `NodeWaitables::add_waitable()`.


## 3. Pipeline

```
┌─ host: launch_wrap orchestrator ─┐
│  ros2 launch / launch_test       │
│  ↓                                │
│  fresh docker container          │
│  ↓                                │
│  LTTng (ros2_trace) + nsys       │
│  ↓                                │
│  /home/tue037807/fish_traces/<session_id>/
│     trace/        (CTF/LTTng)
│     nsys.nsys-rep
│     model.json    (snapshots)
└──────────────────────────────────┘
       ↓ (host)
postprocess/ingest_pg.py
  → ros2_trace + cuda_* + snapshots → Postgres "fish" DB
       ↓
postprocess/model_improved_pg.py
  → graph_nodes + graph_edges + graph_meta + mark_phases
       ↓
fish_viz_server.py :8783  +  Grafana dashboard t0 (uid adq49gh)
```

### Hard rules (verbatim from user; never violate)

- **"her zaman fresh container! zombie process istemiyoruz"** — for every
  benchmark run, spawn a fresh container. Never reuse a long-lived one;
  ros2_daemon residue from a prior run can poison the trace.
- **"ASLA elimizdeki başarıyla build image'lara dokunmuyoruz"** /
  **"hiçbir zaman bir image'i kendi adıyla tekrar üstüne kaydetmiyoruz"**
  — once an image works, name a new image for the next iteration. E.g.
  the most recent rebuild was `autoware-dev-trt-a1000-fishwait:latest`
  on top of `autoware-dev-trt-a1000:latest`, leaving the old `-fish`
  variant intact.
- **"notes.txt'de refer edilen bir veriyi silmeyelim, onun yerine
  arşivleyelim"** — never delete a session referenced anywhere in notes.
  Move to `archive/` if it must go.
- **"yalan söyleme"** — don't fabricate verification. If you can't
  observe it, say so.
- Verify ROS executor concurrency via tracepoints/PG, not by counting
  processes or looking at the gantt picture.


## 4. Tracepoint inventory (the FISH custom ones)

Ground truth is `notes/important_details.txt` §1 ("FISH Tracepoint
Inventory"). At a glance, current FISH-custom tracepoints (the
`ros2:fish_*` family):

- `fish_rclcpp_publish_link`, `fish_rclcpp_receive_link` — callback-bound
  publish + receive attribution (dedup'd, NULL-guarded).
- `fish_rclcpp_callback_group_init`, `fish_rclcpp_cbgroup_add`,
  `fish_rclcpp_executor_init`, `fish_rclcpp_executor_add_cbgroup` —
  callback-group / executor structure capture.
- `fish_rclcpp_client_request_sent`, `fish_rclcpp_client_response_received`
  — client request/response correlation.
- `fish_rclcpp_timer_init` + `fish_rclpy_timer_init` — atomic timer-init
  events (replace heuristic timer-add fallback).
- `fish_rclcpp_waitable_init` — Waitable registration event.
  **Coverage gap currently being fixed (see §7).**
- `fish_nitros_sub_link`, `fish_nitros_pub_link` — NITROS GXF intra-node
  topology (Receiver / Codelet / Transmitter wiring).
- rclpy mirrors: `fish_rclpy_callback_register`,
  `fish_rclpy_service_callback_added`, etc.

Whitelist enforcement: two files —
`/opt/ros/humble/fish/config/fish_events.txt` (LTTng channel filter)
and `fish_tracepoints/install_fish_tracepoints` (the patcher).
**If you add a new tracepoint, both must be updated** or you'll get
silent drops.


## 5. Postgres schema (DB `fish`, user `fish` pw `fish`)

Tables in use (run `\dt` to confirm; schema lives in
`scripts/init_fish_pg.sql`):

- `sessions` — one row per run. `start_ts_ns`, `duration_ns`,
  `components_loaded` (jsonb, includes the `__gpu_containers__` and
  `__gpu_nodes__` keys produced by launch_wrap GPU PID detection).
- `ros2_trace` — hypertable. Columns: `ts_ns`, `session_id`, `event`,
  `cpu_id`, `vpid`, `vtid`, `host_name`, `procname`. The `event`
  column names like `ros2:callback_start`, `ros2:rcl_publish`,
  `ros2:fish_rclcpp_publish_link` etc. There is also a generated
  `ts TIMESTAMPTZ` column (since task #124) for Grafana time-axis
  queries.
- `cuda_api_event`, `cuda_kernel`, `cuda_memcpy`, `cuda_stream_sync` —
  nsys-derived tables.
- `graph_nodes` — model output, layered. `type` column is `CN|EX|N|E|F`.
  `attrs` jsonb holds per-type details (entity type in `etype`, phase
  in `attrs->>'phase'` after mark_phases runs, etc.).
- `graph_edges` — `src`, `dst`, `kind` (`contains`, `comm`,
  `gxf_intra`, ...). Endpoints are `graph_nodes.node_id` pairs.
- `graph_meta` — per-(session_id, scope) record of last model run.
- `graph_mutations` — audit trail of model recomputes.

Conventions:

- `scope` = `__main__` (default) or `__composed__` (one composable
  container). Multi-container sessions get one scope per container.
- `attrs->>'phase'` ∈ {`data`, `init`, `zero_exec`, NULL=unknown}.
  Computed by `mark_phases` (since task #142) based on the first non-
  one-shot timer fire per executor. Sessions modelled before #142 have
  all-NULL phase — they appear in the DB but FT view filters them out
  by default.


## 6. Visualization

### fish_viz_server (port 8783)

`postprocess/fish_viz_server.py` — single-process HTTP server. Routes:

| Path | Serves |
|------|--------|
| `/gantt-tid` | gantt.html — per-thread callback gantt |
| `/gantt-cb` | gantt_cb.html — per-callback gantt with layer tree |
| `/ft` (=`/wcc`) | wcc_view.html — FT (=WCC) topology graphs |
| `/cb-stats` | cb_stats.html — callback duration distribution |
| `/api/gantt`, `/api/wcc`, `/api/wcc-svg`, `/api/cb-stats`, `/api/causal`, `/api/taskset` | JSON / SVG endpoints feeding the views |

The server reads HTML files from disk on every request — no restart
needed after HTML edits. `/api/wcc-svg` renders DOT via the system
`dot` binary; `newrank=true` is set so big graphs (252+ nodes)
render successfully.

URL convention (all views): `?session_id=<sid>&scope=<scope>` —
matches Grafana dashboard link template. Both `session_id` and
`session` accepted as URL params on the FT page.

### Grafana dashboard t0 (uid `adq49gh`)

Auth: HTTP Basic `admin:<GRAFANA_ADMIN_PW>  (local Grafana; see GRAFANA_AUTH env in scripts)`. Use `scripts/push_gapit_panel.py`
for panel edits.

Top toolbar `dashboard.links`: `⚡ gantt-tid` / `⚡ gantt-cb` /
`⚡ cb-stats` / `⚡ ft` (added in version 49).

Top-left "Session" stat panel (id=5): just patched in v50 to
`SELECT '${session_id}'::text AS session_id` so it echoes the
selected variable instead of running a static `component_list`
query that ignored the var.

Templating vars: `session_id` (query from `sessions` table),
`scope` (`__main__|__composed__`), `apriltag_trace_id` (constant
for one demo trace).


## 7. Open coverage gaps

### Intra-process Waitable init (BEING FIXED right now)

**Status: in progress 2026-06-29.** The existing
`fish_rclcpp_waitable_init` patch lives at
`NodeWaitables::add_waitable()`. Intra-process subscription Waitables
(`SubscriptionIntraProcessBase`) bypass that and register via
`IntraProcessManager::add_subscription()` (which is a template method
in `include/rclcpp/experimental/intra_process_manager.hpp`, not in
the `.cpp` despite my earlier note saying "line 62 of .cpp" — was
wrong).

Symptom: in trace, the Waitable's `this` appears as a `cb_addr` in
`callback_start` events with no matching `cb_to_entity` entry. Inner
nested `AnySubscriptionCallback::dispatch` callback_start
(sub_cb_addr) still fires correctly, and `__fish_active_callback` is
set to that during user-cb execution, so publish_link attribution is
not broken — `0 dropped — unknown cb` preserved
(verified on fish_20260626_172710).

Fix in flight (3 steps; see `notes/immediate_work.txt` §0 + tasks
#156-#158):

1. Patch `add_subscription` in the HEADER (template method) to fire
   `TRACEPOINT(fish_rclcpp_waitable_init, subscription.get(), nullptr,
   nullptr)`.
2. In `model_improved_pg.identify_entities`, drop waitable_init rows
   whose ptr matches any `rclcpp_subscription_init.subscription` —
   those are the intra-proc variants, already covered by their parent
   sub entity. Otherwise they'd create singleton Waitable nodes that
   pollute FT graphs.
3. Rebuild fresh image, re-run an intra-proc-heavy session
   (disparity_graph), confirm waitable_init count goes up by N, dropped
   still 0, no new singletons.

### Other open items (lower priority, lives in `notes/immediate_work.txt`)

- WCC shape detection — per-probe binary signatures pattern, see
  `noticing_waitables.txt` / `immediate_work.txt`.
- Phase detection formalization (paper-ready algorithm).
- `--cuda-event-trace` short-rerun validation.
- Tracepoint whitelist inventory clean-up.


## 8. Recent sessions worth knowing about

(Don't rely on this list for "current" — run `psql -c "SELECT
session_id, start_utc FROM sessions ORDER BY start_utc DESC LIMIT
10"` to get a live list.)

- `fish_20260629_115617` — most recent Autoware logging_simulator run
  via `autoware-dev-trt-a1000-fishwait:latest` image. Used to validate
  that Waitable refactor + NITROS GXF tracepoints work outside of
  Isaac ROS benchmark suite.
- `fish_20260626_172710` — disparity_graph multi-container session;
  reference "0 dropped — unknown cb" benchmark for Waitable validation.
- `fish_20260626_151430` — older multi-container stereo session.
  **NOT FT-visible by default** — modelled before mark_phases was
  integrated; all F-nodes have NULL phase, so default `data` filter
  shows 0 WCCs. Open with `?include_unknown=1` or re-model.


## 9. Where to read what

- This file — current state snapshot.
- `notes/fish_manual.md` — install + usage runbook for end users.
- `notes/important_details.txt` — instrumentation deep-dive: every
  tracepoint, where it's emitted, dedup logic, etc.
- `notes/noticing_waitables.txt` — postmortem on why Waitable was an
  unseen dispatch category. Read this before any "dispatch coverage"
  conversation.
- `notes/immediate_work.txt` — open tasks in priority order. Top item
  changes often.
- `notes/db_structure.txt` — DB schema map + naming conventions.
- `notes/architecture.svg` (rendered from .mmd) — visual layer model.
- `scripts/init_fish_pg.sql` — authoritative Postgres schema.
- `fish_tracepoints/install_fish_tracepoints` — every tracepoint
  patch site, in one file. Read this to understand the runtime
  modifications FISH makes.
- `postprocess/model_improved_pg.py` — graph extraction from
  ros2_trace + snapshots. Where heuristics live.
- `postprocess/fish_viz_server.py` — viz server.
- `examples/grafana_gapit/` — Grafana dashboard JSON + the gapit
  htmlgraphics panel embedding fish_viz.
- `MEMORY.md` (in `~/.claude/projects/-home-tue037807/memory/`) —
  the user's auto-memory index. Pointers to user profile, working
  style, prior decisions.


## 10. Style + interaction notes

- The user (Fatih) is a senior systems researcher; talk shop, skip
  basics. Turkish is common; switch language freely.
- He prefers concrete-next-step framing on resume.
- Don't infer state from process count / visuals — verify via
  tracepoints or PG queries.
- When restarting fish_viz_server, use the session scratchpad at
  `/tmp/claude-1000/-home-tue037807/<session>/scratchpad/` for logs,
  not the project `scratchpad/` (which doesn't exist at repo root).
