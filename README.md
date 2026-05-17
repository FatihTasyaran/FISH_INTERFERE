# Welcome to the Function-level Inspection of Scheduling Hierarchies (FISH) repository! 🐟

FISH turns any running ROS 2 application — GPU-accelerated workloads
included — into [SAG-ready](https://github.com/SAG-org) tasksets and
interactive task-graph visualisations.

This README is a summary of FISH's capabilities and a quick start.
For everything else:

- 📖 [Full Documentation](#)   *(ReadTheDocs link — coming soon)*
- 🎬 [Live Demo](#)            *(hosted task-graph + Grafana panels — coming soon)*
- 📄 [Paper](#)                *(in preparation, mid-2026)*
- 🗺️ [Roadmap](notes/immediate_work.txt)
- 💬 [Discussions](https://github.com/FatihTasyaran/FISH_INTERFERE/discussions)

---

## What FISH actually does

Under the hood, FISH captures LTTng kernel/userspace traces, NVIDIA
Nsight Systems GPU profiling data, and system-state snapshots from
inside the container where your ROS 2 nodes run. It then reconstructs
a typed task-graph (executors → nodes → entities → callbacks → GPU
kernels) and exports it for analysis, scheduling-tool consumption,
and visualisation.

> **Status — pre-release.** FISH is research software. The
> post-processing pipeline is stable; tests, CI, and the installable
> Python package are landing now (May 2026). Expect the CLI surface
> and on-disk schemas to firm up over the next few releases. See
> `notes/immediate_work.txt` for what is next.

## Capability inventory

What FISH extracts from a trace today, and where each data point
stands in the visualisation pipeline. Every row is **derived** — the
"Aggregated from" column lists the raw events / nsys measurements /
snapshot files it is computed over. Source taxonomy:

- **LTTng tracepoints (MongoDB `ros2_trace`)** — standard
  rclcpp/rclpy events (`callback_start`, `rcl_*_init`, ...) and the
  FISH custom set (`fish_executor_init`, `fish_callback_group_init`,
  `fish_publish_link`, `fish_client_link`, action probes).
- **nsys CUPTI tables (InfluxDB `fish`)** — `gpu_kernels`,
  `gpu_memcpy`, `gpu_memset`, `gpu_sync`, `cuda_runtime`,
  `nvtx_events`, `cuda_callchain`.
- **Snapshot files (MongoDB `snapshot_*` + topic_info / node_info /
  process_tree)** — captured at trace stop.
- **FISH daemon log (MongoDB `fish_events`)** — kill / resurrect /
  stabilisation timestamps.

The Viz column is a deliberate placeholder — every row reads `X`
until we audit which data points already have a visual representation
and which still need one.

### Graph topology

| Data point | Viz |
|------------|-----|
| **Hierarchical task graph (CN → EX → N → E → F)** <br><sub>← `rcl_node_init`, `rcl_{publisher,subscription,service,client,timer}_init`, `rclcpp_callback_register`, `rclcpp_subscription_callback_added` (LTTng); `gpu_kernels.globalPid` ↔ `process_tree` for the CN layer</sub> | X |
| **Per-process executor count and types** (ST / MT / StaticST) <br><sub>← `fish_executor_init` (pid → executor_type, num_threads)</sub> | X |
| **Per-executor callback-group composition** (MutuallyExclusive vs Reentrant) <br><sub>← `fish_callback_group_init` (group_addr → type); `fish_cbgroup_add` (entity ↔ group); `fish_executor_add_cbgroup` (group ↔ executor)</sub> | X |
| **Per-entity QoS, message type, and peer counts** (pub fan-out, srv ↔ cli pairs) <br><sub>← `rcl_publisher_init.qos`, `rcl_subscription_init.qos` + msg type fields; snapshot `topic_info` (subscriber count per topic), `node_info`</sub> | X |
| **Cross-container external boundary edges** (process A pub → process B sub) <br><sub>← Snapshot `topic_info` across all container roles + topic-name matching during `fish_compose` merge</sub> | X |
| **Composed multi-container graph totals** (\|CN\|, \|EX\|, \|N\|, \|E\|, \|F\|, edge mix) <br><sub>← Aggregation over the per-container subgraphs by `fish_compose.compose_graphs`</sub> | X |
| **Out-of-ROS (oort) thread detection** — CUDA-active workers outside any executor <br><sub>← InfluxDB `SELECT DISTINCT tid FROM cuda_runtime` ∖ MongoDB `callback_start.vtid` (per pid)</sub> | X |

### Callback & entity timing (CPU)

| Data point | Viz |
|------------|-----|
| **Per-callback invocation count + duration distribution** <br><sub>← `callback_start` ↔ `callback_end` paired by (`callback`, `vtid`); duration = `end.ts` − `start.ts`</sub> | X |
| **Per-callback-group serialisation pattern** (MX → no overlap, RE → parallel allowed) <br><sub>← `fish_callback_group_init.type`</sub> | X |
| **Inter-callback gap → empirical period estimate** <br><sub>← Successive `callback_start` timestamps for the same callback handle</sub> | X |
| **Publisher → subscription latency** (via `fish_publish_link`) <br><sub>← `fish_publish_link` (pub_handle → sub_callback_handle); paired with the receiving `callback_start.ts`</sub> | X |
| **Service round-trip latency** (request_sent → response_received, paired by seq number) <br><sub>← `rclcpp_service_send_request` ↔ `rclcpp_service_receive_response` (paired on `sequence_number`); or `fish_client_link` for the deterministic variant</sub> | X |
| **Action latency per phase** (goal / cancel / result) <br><sub>← FISH custom probes `action_execute_goal`, `action_execute_cancel`, `action_execute_result`</sub> | X |
| **Split-callback durations** (`::part1` + `::continuation`) <br><sub>← `callback_start` → `client_request_sent` (part1); `client_response_received` → `callback_end` (continuation)</sub> | X |

### GPU side

| Data point | Viz |
|------------|-----|
| **Per-process totals**: CUDA runtime calls, kernels, memcpys, memsets, syncs <br><sub>← `COUNT(*)` over InfluxDB `cuda_runtime`, `gpu_kernels`, `gpu_memcpy`, `gpu_memset`, `gpu_sync` filtered by `session`, `container`</sub> | X |
| **Per-stream wait count + per-stream usage histogram** (which callbacks claim it) <br><sub>← `gpu_sync.streamId` (wait counts); `gpu_kernels.streamId` + `gpu_memcpy.streamId` (usage); claim assignments from Rule-A/B attribution</sub> | X |
| **Inter-stream DAG**: src → dst weighted edges <br><sub>← `gpu_sync` rows that bound cross-stream transitions (host-mediated regime); `cudaStreamWaitEvent` rows in `cuda_runtime` for the deterministic regime (currently absent in Autoware / yolo workloads)</sub> | X |
| **Per-callback Typed DAG shape signatures + distinct-shape count** <br><sub>← `cuda_runtime` rows inside `[callback_start, callback_end]` joined to `gpu_kernels` / `gpu_memcpy` / `gpu_memset` on `correlationId`; canonical structural hash per invocation</sub> | X |
| **Per-shape phase class** (dominant / variant / anomaly / init / shutdown) <br><sub>← Per-signature invocation count + `init_score` over API-name list + duration anomaly tests (mean+kσ ∨ modified z-score ∨ trivially-small)</sub> | X |
| **Per-token statistics**: duration, kernel dims, regs, shmem, memcpy bytes, memset bytes <br><sub>← `gpu_kernels.{gridX,gridY,gridZ,blockX,blockY,blockZ,registersPerThread,staticSharedMemory,dynamicSharedMemory,duration_ns}`; `gpu_memcpy.{copyKind,bytes,duration_ns}`; `gpu_memset.{bytes,value,duration_ns}`; `cuda_runtime.{api_name,duration_ns}`</sub> | X |
| **Conditional graph per skeleton position** (entry, continue, end probabilities) <br><sub>← Trie over canonical-token sequences from each invocation, keyed on skeleton position; per-node counts of (entries, continues, ends)</sub> | X |
| **LCS skeleton** (longest common subseq + substring) per callback <br><sub>← Per-invocation canonical-token sequence; multi-sequence LCS (subseq-with-gaps + contiguous-substring) across invocations of the same callback</sub> | X |

### Stream-bridge attribution

| Data point | Viz |
|------------|-----|
| **Rule A** — callback-window time fraction (same tid) <br><sub>← `gpu_kernels.duration_ns` + `gpu_memcpy.duration_ns` + `gpu_memset.duration_ns` inside `[callback_start, callback_end]` matching on `tid` ↔ `vtid`</sub> | X |
| **Rule B** — bridged-via-stream-claim fraction <br><sub>← Stream-ownership graph derived from prior Rule-A assignments; GPU work outside any callback window attributed via the stream's current owner</sub> | X |
| **Rule C** — oort residual fraction (and post-sync-release subset) <br><sub>← GPU work not attributable by A or B → tagged to oort threads; post-sync-release subset = oort work whose preceding event is a `cudaStreamSynchronize` boundary</sub> | X |
| **Per-callback attribution share** — how much of each callback's GPU work came from each rule <br><sub>← Per callback: numerator = sum of `duration_ns` attributed under each rule; denominator = the callback's total attributed GPU `duration_ns`</sub> | X |

### Trace-level meta

| Data point | Viz |
|------------|-----|
| **Event volume pre / post whitelist + ingest throughput** (docs/s) <br><sub>← LTTng session-stats counters; `ingest.py` per-second log timestamps (`docs/s` derived from cumulative insert count over wall clock)</sub> | X |
| **Run duration, stabilisation time, detected GPU PID count** <br><sub>← `fish_events` daemon log (start / settle / stop entries); distinct `gpu_kernels.globalPid` resolved via the nsys `PROCESSES` table</sub> | X |
| **Launch-wrap vs relaunch attribution** <br><sub>← `fish_events` daemon log entries (`launch_wrap`, `relaunch` actions per pid)</sub> | X |

### Pending (not yet computed)

The process-level period extractor lands next; once it does, the
following also become first-class data points:

- End-to-end frame latency + jitter
- Per-frame phase classification (INIT / WARMUP / STEADY / TAIL / COALESCED(N) / UNKNOWN)
- EDD-style task parameters per period (T, C_cpu, C_sm, C_ce, D, jitter)
- Bottleneck callback per period
- Per-period concurrency analysis (same-stream serial vs cross-stream parallel)
- Per-period anomaly correlation ("do anomaly shapes cluster in WARMUP periods?")

---

## Validated on (use-case suite)

A small, deliberate set of workloads FISH is captured against
end-to-end. Each one stresses a different part of the pipeline; each
one ships with a trimmed trace fixture in `tests/fixtures/` so the
CI/CD smoke job can re-derive its load-bearing numbers from scratch.
See [`notes/use_cases.txt`](notes/use_cases.txt) for rationale per
entry.

| Workload                                                                                    | Why it is in the suite                                       | Status |
|---------------------------------------------------------------------------------------------|---------------------------------------------------------------|--------|
| **NVIDIA Isaac ROS — AprilTag detection**                                                   | Clean, compact GPU-only reference pipeline                    | planned (mission: [`examples/isaac_apriltag.yaml`](examples/isaac_apriltag.yaml)) |
| **Autoware — `logging_simulator` + sample-rosbag**                                          | Scale: 3160-vertex graph, 13 GPU-active callbacks, 14.5k kernels | captured (`fish_20260427_165116`) |
| **Aerial Autonomy Stack (AAS) — yolo_py + PX4 SITL**                                        | Multi-container compose flow, oort thread pattern, mission scenario | captured (`fish_compose_20260419_161633`) |
| **demo_nodes_cpp talker / listener** (backup)                                               | Pure-CPU mini — proves FISH works without nsys                | planned |
| **rclpy 3-node chain** (backup)                                                             | GIL-bound executor profile, different from rclcpp             | planned (blocked on rclpy scheduler tracepoints) |

Once a workload moves from *planned* to *captured*, the corresponding
row in `tests/paper_anchors.json` locks in the counts the CI/CD job
asserts on every run — see [Roadmap](notes/immediate_work.txt) item 15.

---

## What FISH gives you

- **Hierarchical task graph** (CN → EX → N → E → F) for any ROS 2
  workload, multi-container compose flows included.
- **Per-callback GPU activity** — CUDA kernel / memcpy / memset
  signatures attached to the originating ROS 2 callback via
  stream-bridge attribution (Rule A/B/C).
- **Shape clustering** — per-callback per-invocation Typed DAGs,
  collapsed to dominant / variant / anomaly / init / shutdown
  classes, with LCS-skeleton and conditional-graph figures.
- **Persistent graph store** — MongoDB session DB you can mutate,
  re-export, diff, and visualise without re-ingesting raw traces.
- **Cross-session GPU corpus** — one shared InfluxDB `fish`
  database holds all nsys data, tagged by `session` + `container`.

## Quick start

Inside the container where your ROS 2 nodes run:

```bash
docker cp fish_interfere <container>:/root/fish_interfere
docker exec -it <container> bash
cd /root/fish_interfere && ./scripts/setup_fish.sh
source ~/.bashrc
```

`FISH_ENABLED=1` is now exported. Any `ros2 launch` / `ros2 run` from
this shell is wrapped with LTTng tracing + nsys GPU profiling and the
trace is dropped to `~/fish_traces/fish_<timestamp>/`.

Commit the container to bake FISH into your image:

```bash
docker commit <container> my-fish-image:latest
```

See `docs/installation.md` for the long form (individual install
scripts, baked image, host requirements).

## Repository layout

```
fish_interfere/
├── README.md  LICENSE  CHANGELOG.md  pyproject.toml
│
├── scripts/                # All user-facing shell scripts
│   ├── setup_fish.sh       # one-shot installer (runs inside container)
│   ├── install_fish_deps.sh
│   ├── install_fish.sh     # framework installer (sourced)
│   ├── install_all.sh      # host-side: run container_install.sh on each
│   ├── build_fish_image.sh # automated FISH image build
│   ├── container_install.sh# install FISH into a running container + commit
│   ├── fish_mission.sh     # scripted AAS mission
│   └── fish_auto_mission.sh
│
├── config/                 # Runtime configuration
│   ├── fish_settings.ini
│   └── fish_events.txt     # LTTng event whitelist
│
├── examples/               # Example mission definitions
│   └── aas_single_drone.yaml
│
├── docs/                   # Sphinx docs (api, installation, usage, ...)
│
├── fish_tracepoints/       # Custom LTTng tracepoint installer + sources
│
├── python/fish/            # In-container Python package
│                           # (CLI, GPU daemon, launch wrap, snapshot,
│                           #  settings reader)
│
├── postprocess/            # Host-side analysis pipeline
│                           # (ingest, model, graph_store, gpu_dag,
│                           #  shape classifier, viz)
│
├── fish_orchestrator.py    # Multi-container mission orchestrator
│
└── notes/                  # Development notes (chronological + topical)
```

## Documentation

The `docs/` tree is built with Sphinx (`make -C docs html`). Main
entry points:

- `docs/installation.md` — install scripts in detail
- `docs/usage.md` — running FISH against a workload
- `docs/mission.md` — automated AAS missions
- `docs/graph_store.md` — graph persistence + InfluxDB layout
- `docs/tracepoints.md` — custom LTTng tracepoints reference
- `docs/dependencies.md` — container + host requirements

For background, paper-relevant evidence, and design rationale see the
`notes/` directory (chronological journal in `notes/notes.txt`).

## Host post-processing

After capturing a trace:

```bash
# Ingest into MongoDB (per-session) + InfluxDB (shared `fish` DB)
python3 -m postprocess.ingest ~/fish_traces/fish_<ts>

# Build the typed graph
python3 -m postprocess.model_improved --session fish_<ts>

# Per-callback GPU DAG extraction + shape clustering
python3 postprocess/gpu_dag.py fish_<ts> __main__

# Or for multi-container compose flows:
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_<ts>
```

Then export the graph for the web viz:

```bash
python3 postprocess/graph_store.py export fish_<ts> __main__ \
  -o postprocess/fish_graph.json
# Open postprocess/fish_viz.html in a browser
```

## Citing FISH

A paper is in preparation (mid-2026). Until publication, please cite
the repository — see `CITATION.cff` (GitHub renders a "Cite this
repository" button from it):

```
Tasyaran, F. (2026). FISH: Function-level Inspection of Scheduling
Hierarchies. https://github.com/FatihTasyaran/FISH_INTERFERE
```

## License

MIT — see [`LICENSE`](LICENSE). Use it freely (including commercial
use); the only legal requirement is to keep the copyright notice
when redistributing. If you publish work that builds on FISH,
citing it via `CITATION.cff` is appreciated but not required by the
license.
