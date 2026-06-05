# AGENTS.md — Instructions for artificial friends using FISH

> Filename kept as `AGENTS.md` so the autonomous tooling that looks
> for one finds it. Humans (or biological agents — your call) should
> read `README.md`; artificial friends should read this file.

This document is the agent-facing entry point to FISH. It is short on
narrative, heavy on commands, paths, and decision rules.

If you are an artificial friend asked to "profile X with FISH",
"explain why callback Y is slow", "compare two sessions", or anything
similar, follow the recipes below. Do not invent paths or commands —
everything below has been verified against the current repo.

---

## 1. What FISH does in one paragraph

FISH captures three things while a ROS 2 application runs **inside a
target container**: (a) LTTng kernel+userspace traces of every ROS 2
callback boundary, (b) NVIDIA Nsight Systems GPU profiling data
(`gpu_kernels`, `cuda_runtime`, `gpu_memcpy`, ...), and (c) a system
snapshot at trace stop. **On the host**, FISH ingests these into
MongoDB (one DB per session) and a shared InfluxDB database called
`fish`, then reconstructs a typed task graph
(CN → EX → N → E → F → GPU-DAG) and exports it for analysis and
visualisation.

---

## 2. Capabilities at a glance

| If the user wants to ...                                                        | Run                                                                            |
|---------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| Install FISH inside an already-running container                                | `./scripts/container_install.sh <container_name>`                              |
| Set up FISH from inside a fresh container                                       | `./scripts/setup_fish.sh` (or `--yes` for non-interactive)                     |
| Bake a FISH image from a base image                                             | `./scripts/build_fish_image.sh <base_image>`                                   |
| Run a scripted AAS mission                                                      | `./scripts/fish_mission.sh`                                                    |
| Trace a ROS 2 launch (inside container)                                         | `FISH_ENABLED=1 ros2 launch <pkg> <launchfile>`                                |
| Ingest a captured trace                                                         | `python3 -m postprocess.ingest ~/fish_traces/<session_name>`                   |
| Build the FISH typed graph                                                      | `python3 -m postprocess.model_improved --session <session>`                    |
| Run multi-container compose flow end-to-end                                     | `python3 -m postprocess.fish_compose ~/fish_traces/<compose_session>`          |
| Extract per-callback GPU DAGs + shape clusters                                  | `python3 postprocess/gpu_dag.py <session> <scope>`                             |
| Export graph for `fish_viz.html`                                                | `python3 postprocess/graph_store.py export <session> <scope> -o fish_graph.json` |
| List all session DBs                                                            | `python3 postprocess/graph_store.py sessions`                                  |
| List scopes inside a session                                                    | `python3 postprocess/graph_store.py list <session>`                            |

`<session>` = MongoDB database name = trace directory basename
(e.g. `fish_20260427_165116`, `fish_compose_20260419_161633`).
`<scope>` = `__main__` for single-container sessions, the role name
(`aircraft`, `simulation`, `ground`, ...) for one container of a
compose session, or `__composed__` for the merged multi-container
graph.

---

## 3. Repository layout (where to look)

```
fish_interfere/
├── scripts/        # All user-facing shell scripts (install + mission)
├── config/         # fish_settings.ini, fish_events.txt — runtime config
├── examples/       # Example mission YAMLs for fish_orchestrator.py
├── postprocess/    # Host-side Python pipeline — the analysis lives here
├── python/fish/    # In-container Python package (CLI, GPU daemon, snapshot)
├── fish_tracepoints/  # Custom LTTng tracepoint installer + sources
├── docs/           # Sphinx docs (user-facing)
├── notes/          # Development notes — design rationale, paper-relevant
├── tests/          # Reserved for the upcoming test pyramid (planned)
├── fish_orchestrator.py  # Multi-container mission orchestrator
├── README.md       # Human-facing intro + capability inventory
├── AGENTS.md       # THIS FILE
└── CHANGELOG.md / LICENSE / CITATION.cff / pyproject.toml
```

When you need code-level detail, look in `postprocess/`. When you need
design / paper rationale, look in `notes/`. When you need the user-
facing story, look in `README.md` or `docs/`.

---

## 4. Data sources after ingest

### MongoDB (one DB per session)

Connection: `mongodb://localhost:27017`, no auth.

Collections per session DB:

| Collection                  | Holds                                                                        |
|-----------------------------|------------------------------------------------------------------------------|
| `ros2_trace`                | LTTng events (timeseries). Suffixed `_<role>` for compose sessions.          |
| `process_tree`              | `ps_tree.txt` parsed (PID/PPID/LWP, threads). Suffixed by role.              |
| `node_info`                 | `ros2 node info` output per node. Suffixed by role.                          |
| `topic_info`                | Topic → message type + pub/sub count. Suffixed by role.                      |
| `topic_hz`                  | Per-topic measured publish rate. Suffixed by role.                           |
| `component_list`            | Composable container → loaded plugin nodes. Suffixed by role.                |
| `node_list`                 | Process → nodes mapping. Suffixed by role.                                   |
| `fish_events`               | FISH daemon kill / resurrect / settle log. Suffixed by role.                 |
| `graph_meta`                | Per-scope graph metadata (NOT suffixed; has `scope` field).                  |
| `graph_nodes`               | Per-scope graph vertices (NOT suffixed; has `scope` field).                  |
| `graph_edges`               | Per-scope graph edges (NOT suffixed; has `scope` field).                     |
| `graph_mutations`           | Append-only audit log of graph mutations (NOT suffixed).                     |
| `cuda_streams`, `gpu_info`  | nsys metadata copied at ingest. Suffixed by role for compose.                |
| `nsys_enums`                | nsys ENUM_* table copies (NOT suffixed; shared per session).                 |

`ScopedMongo` (postprocess/db_scope.py) maps bare collection names
(`mongo["ros2_trace"]`) to the role-suffixed form transparently when
a `role` is set.

### InfluxDB 3 (single shared `fish` DB across ALL sessions)

Connection: `http://127.0.0.1:8181`, token in `~/inf.tok`.

Measurements (all carry mandatory tags `session` and `container`):

| Measurement      | Holds                                                  |
|------------------|--------------------------------------------------------|
| `gpu_kernels`    | CUPTI kernel executions (grid/block, regs, shmem, dur) |
| `gpu_memcpy`     | CUPTI memcpy (copyKind, bytes, dur)                    |
| `gpu_memset`     | CUPTI memset (Volta+ fill kernel)                      |
| `gpu_sync`       | CUPTI synchronization events                           |
| `gpu_overhead`   | CUPTI's own profiling cost                             |
| `gpu_mem_usage`  | GPU allocation events                                  |
| `cuda_runtime`   | Host-side CUDA runtime API calls                       |
| `nvtx_events`    | NVTX markers / ranges                                  |
| `cuda_callchain` | Reference: per-callchainId stack frames                |

**Every query MUST filter both `session` and `container`.** Forgetting
the filter aggregates across unrelated runs. Example:

```sql
SELECT COUNT(*) FROM gpu_kernels
WHERE session='fish_compose_20260419_161633' AND container='aircraft'
```

The Python helpers in `postprocess/gpu_dag.py` and
`postprocess/model_improved.detect_oort_threads` already enforce this.

---

## 5. Typed graph schema (FISH model)

FISH builds a hierarchical task graph with five layers:

| Layer | Vertex type | What it is                                                       |
|------:|-------------|------------------------------------------------------------------|
|  L-1  | **CN**      | Container / process boundary (the docker container)              |
|   L0  | **EX**      | rclcpp / rclpy executor instance (ST / MT / StaticST)            |
|   L1  | **N**       | ROS 2 node (rclcpp::Node / rclpy.Node instance)                  |
|   L2  | **E**       | Entity: publisher / subscription / service / client / timer      |
|   L3  | **F**       | Callback (function bound to the entity by `rclcpp_callback_register`) |

Per-vertex attributes live in `A_v` (a dict). Common keys:

| Vertex | Frequent `A_v` keys                                                                 |
|--------|-------------------------------------------------------------------------------------|
| EX     | `pid`, `executor_addr`, `executor_type`, `num_threads`, `cb_groups`                 |
| N      | `node_name`, `namespace`, `vpid`                                                    |
| E      | `etype` (`pub`/`sub`/`srv`/`cli`/`timer`/`oore`), `topic_name`, `msg_type`, `qos`   |
| F      | `callback`, `symbol`, `invocation_count`, `duration_dist`, `gpu_node` (bool), `gpu_dags` |

Edge `rel` values: `contains`, `msg` (pub→sub), `dep` (callback → entity), `external` (cross-container pub→sub).

Two special pseudo-vertex types appear when GPU work happens outside
any callback (yolo_py main loop pattern, ONNX Runtime internal
threads, etc.):
- `etype: "oore"` — placeholder entity for an "out-of-ROS" thread
- `ptype: "oort"` — the thread itself, marked `gpu_node=True`

When the user mentions "shapes", "skeletons", or "phase classes",
they are talking about **per-callback Typed DAG aggregations**
attached at `F.A_v["gpu_dags"]`. See `postprocess/shape_phase_classifier.py`
and `postprocess/shape_skeleton.py`.

---

## 6. Pipeline stages — what each does

```
target container         host
─────────────────        ────────────────────────────────────────────────
LTTng + nsys             ingest.py     →  Mongo per-session, InfluxDB `fish`
session snapshot         model_improved →  graph_store (CN/EX/N/E/F vertices)
                         gpu_dag.py    →  per-callback GPU DAGs + shape classes
                         shape_skeleton.py →  LCS skeleton + conditional graph
                         fish_compose.py →  multi-container merge (compose runs)
                         graph_store.py export →  fish_graph.json for viz
```

Read these source files in order if you need to understand the
pipeline end-to-end:
1. `postprocess/ingest.py`
2. `postprocess/model_improved.py` (the heart of graph extraction)
3. `postprocess/gpu_dag.py`
4. `postprocess/shape_phase_classifier.py`
5. `postprocess/shape_skeleton.py`
6. `postprocess/fish_compose.py` (only for multi-container sessions)
7. `postprocess/graph_store.py` (read+write API for the persistent graph)

---

## 7. Common agent tasks — concrete recipes

### 7.1 "Ingest and analyse a freshly captured session"

```bash
SESSION=fish_20260427_165116
python3 -m postprocess.ingest ~/fish_traces/$SESSION
python3 -m postprocess.model_improved --session $SESSION
python3 postprocess/gpu_dag.py $SESSION __main__
python3 postprocess/graph_store.py export $SESSION __main__ \
    -o postprocess/fish_graph.json
```

`fish_viz.html` (open in a browser) consumes `fish_graph.json`.

### 7.2 "Find which callbacks did GPU work, and how much"

```python
from postprocess import graph_store
G = graph_store.load_graph(SESSION, "__main__")
for nid, data in G.nodes(data=True):
    v = data["v"]
    if v.t_v == "F" and v.A_v.get("gpu_node"):
        dags = v.A_v.get("gpu_dags", [])
        print(v.A_v.get("symbol"), "→", len(dags), "shape(s)")
```

### 7.3 "Get per-callback invocation count + duration stats"

`F.A_v["invocation_count"]` and `F.A_v["duration_dist"]` carry the
numbers. Already aggregated by `model_improved.py`.

### 7.4 "Compare two sessions for the same workload"

Both sessions live in their own Mongo DBs; both contribute to the
shared `fish` InfluxDB. To query the InfluxDB side cross-session:

```sql
SELECT session, COUNT(*) FROM gpu_kernels
WHERE session IN ('session_a', 'session_b') AND container='aircraft'
GROUP BY session
```

For graph-level diffing, load both with `graph_store.load_graph()`
and walk node/edge sets in Python.

### 7.5 "Explain why callback F#X is slow"

1. Check `F.A_v["duration_dist"]` — mean, p99, std.
2. Load its `gpu_dags` shapes. Inspect the per-token statistics
   (kernel grid/block, memcpy bytes, etc.) on each token.
3. Inspect the conditional graph via `shape_skeleton.py` output —
   shows alternative execution paths and their probabilities.
4. Check the callback group: if MX, no overlap → executor
   serialisation is a contributor.

### 7.6 "Run a fresh capture and the full analysis end-to-end"

```bash
# Inside the target container (FISH installed):
FISH_ENABLED=1 ros2 launch <pkg> <launchfile>
# Stop the launch normally — FISH stops the trace + writes snapshot.

# On the host:
ls ~/fish_traces/             # find the new fish_<ts>/ directory
python3 -m postprocess.ingest ~/fish_traces/fish_<ts>
python3 -m postprocess.model_improved --session fish_<ts>
python3 postprocess/gpu_dag.py fish_<ts> __main__
```

---

## 8. Settings (`config/fish_settings.ini`)

Sections used at runtime:

| Section   | Key                                | What it does                                     |
|-----------|------------------------------------|--------------------------------------------------|
| `[nsys]`  | `trace`, `cudabacktrace`, ...      | nsys CLI flags. Default traces CUDA + NVTX.      |
| `[daemon]`| `poll_interval`, `settle_time`, ...| FISH daemon timing thresholds.                   |
| `[replay]`| `replay_rosbag`, `command`         | Optional rosbag replay phase after stabilisation.|
| `[trace]` | `output_dir`                       | Where traces land. Default `~/fish_traces`.      |

The Python reader is `python/fish/settings.py`. It searches several
paths so the file works whether installed (`/opt/ros/humble/fish/`)
or from the repo checkout (`config/fish_settings.ini`).

---

## 9. Failure modes and how to recognise them

| Symptom                                                                             | Likely cause                                                              | Fix                                                                                                  |
|-------------------------------------------------------------------------------------|---------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| `ingest.py` finishes but no `gpu_kernels` rows present                              | nsys was not actually attached (PID not auto-detected)                    | Check `fish_events` collection for `launch_wrap` / `relaunch` entries. Verify `FISH_ENABLED=1`.      |
| `model_improved` produces oort threads = 0 on a known-GPU workload                  | InfluxDB filter mismatch — session/container tag did not match            | Verify `connect_influx(session, role)` args match what `ingest_session` wrote.                       |
| `gpu_dag.py` says "no callback_start events"                                        | Trace was captured without rclcpp tracetools instrumentation              | Confirm trace was captured INSIDE a container that ran `scripts/setup_fish.sh` first.                |
| `babeltrace2` deadlock during `ingest.py`                                           | stderr full from CTF warnings (large Autoware traces especially)          | Already fixed: stderr is redirected to a file. If reappears, check `bt2_*_stderr.log`.               |
| Cross-session contamination in queries                                              | InfluxDB SELECT without `session` + `container` WHERE clause              | Add the filter. `_scope_where()` helper in pre_analysis.py does this for you.                        |
| Composed graph missing executor info                                                | `fish_rclcpp_executor_init` tracepoint wasn't sourced when the process launched  | Verify FISH was sourced in the SAME shell the process ran in. Re-bake the image if needed.           |

---

## 10. Things NOT to do

- **Do not drop legacy session DBs** (`fish_20260322_*`, `fish_20260329_*`,
  ...). They are paper-evidence; see `notes/use_cases.txt`. If
  archival is needed, use `mongodump` first.
- **Do not query the shared `fish` InfluxDB without `session` +
  `container` filters.** You will silently aggregate across runs.
- **Do not mock the database** in any test you write. Prior incident
  in `feedback_fresh_container.md` etc.: mocked tests pass while
  production breaks because of schema drift.
- **Do not "fix" missing collections by silently recreating them.**
  Missing collections almost always mean the capture itself failed.
  Investigate `fish_events` and the snapshot first.
- **Do not edit the same `~/fish_traces/<session>` directory twice.**
  Re-ingest into a fresh DB if you need to reprocess.
- **Do not commit `~/.influxdb/data` or `~/inf.tok`.** They contain
  secrets / large local data.

---

## 11. Where to read next

Topic-indexed pointers. All paths relative to repo root.

| If you need to understand ...                                | Read                                              |
|--------------------------------------------------------------|---------------------------------------------------|
| What FISH actually measures (paper-style enumeration)        | `notes/fish_computable_properties.txt`            |
| Why the database layout is the way it is                     | `notes/db_structure.txt` + `docs/graph_store.md`  |
| Stream-bridge attribution rules (Rule A / B / C)             | `notes/stream_bridge.txt`                         |
| Phase classifier (init / dominant / variant / anomaly)       | `notes/phase_detector.txt`                        |
| Host-mediated GPU stream serialisation                       | `notes/host_mediated_stream_sync.txt`             |
| Skeleton extraction (LCS subseq + substring + conditional)   | `notes/EXPECTED_OBSERVED.txt` (skeleton section)  |
| Isaac ROS workload coverage plan                             | `notes/isaac_ros_coverage.txt`                    |
| Use-case suite + ros2_benchmark integration                  | `notes/use_cases.txt`                             |
| Per-callback PMU / roofline design (Gear 1 + Gear 2)         | `notes/per_callback_pmu_design.txt`               |
| Tracepoint inventory (what fires when)                       | `notes/tracepoint_inventory.txt`                  |
| Test strategy + GitHub Actions plan                          | `notes/immediate_work.txt` item 15                |
| Known issues / pending work                                  | `notes/immediate_work.txt` + `notes/follow_up.txt`|
| Day-by-day development journal                               | `notes/notes.txt`                                 |

---

## 12. State of the project

Pre-release. Post-processing pipeline is stable and load-bearing for
paper-in-preparation. CLI surface and on-disk schemas may shift
between releases until the first tagged version. The `tests/` tree
exists structurally but the test pyramid (unit + contract + smoke)
is being built; see `notes/immediate_work.txt` item 15. Confidence
matrix:

- Graph extraction (`model_improved.py`): high
- GPU DAG / shape classifier: high — backed by paper numbers
- Stream-bridge attribution: high
- Multi-container compose flow: medium — only AAS validated
- Isaac ROS integration: starting (see `notes/isaac_ros_coverage.txt`)
- Per-callback PMU sampling (roofline): design only, not implemented

When in doubt, prefer reading current code in `postprocess/` over
older notes — notes can drift, the code is the ground truth.

---

## 13. How to contribute changes as an agent

If you are making code changes:

1. Read the surrounding file completely first.
2. Match the existing style (single-line comments where non-obvious;
   no docstrings on internal helpers; descriptive variable names).
3. Run a smoke test against a known captured session (e.g.
   `fish_20260427_165116`) — confirm the numbers match the paper
   anchors before declaring the change safe.
4. Commit with a concise subject + a body that explains the **why**.
   Include `Co-Authored-By: <model> <email>` lines if appropriate.
5. Do not push to `main` if a pre-commit hook fails — diagnose and
   fix the underlying issue.

Last reviewed: 2026-05-17.
