# Welcome to the Function-level Inspection of Scheduling Hierarchies (FISH) repository! 🐟

FISH is a retrospection and task-generation tool that produces
[SAG-ready](https://github.com/SAG-org) tasksets and rich
visualisations for **any** ROS 2 application, with first-class
support for GPU-accelerated workloads.

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
