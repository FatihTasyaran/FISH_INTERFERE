# Graph Store

`postprocess/graph_store.py` provides a persistent, mutable store for FISH
graphs in MongoDB. It decouples graph extraction (expensive — minutes to
hours over a full trace) from downstream analysis (cheap — iterate over the
already-extracted graph).

## Why

Before the store, every analysis run re-ingested raw traces and re-extracted
the graph. For a single AAS container that takes ~1–3 minutes; for a
three-container compose run it adds up to 10+ minutes. Analysis scripts
(roofline, scheduling model, diff between mission variants) need to iterate
quickly, often re-running many times on the same graph. The store saves the
graph once and lets later passes load, mutate, and re-export in seconds.

## Architecture

**One MongoDB database per session.** Raw trace, per-container graphs, and
the composed multi-container graph all live together in the session's DB —
for a compose run, the DB is named after the session directory
(`fish_compose_YYYYMMDD_HHMMSS`); for a standalone run, it's the trace
session name (`fish_YYYYMMDD_HHMMSS`).

Raw trace collections are suffixed with the container role for multi-container
sessions: `ros2_trace_aircraft`, `snapshot_aircraft`, `node_info_aircraft`, etc.
Standalone sessions use unsuffixed names. The `ScopedMongo` helper
(`postprocess/db_scope.py`) transparently maps bare collection names for callers.

Graph data uses four collections with a `scope` field discriminating
per-container vs composed:

| Collection        | One doc per              | Purpose                          |
|-------------------|--------------------------|----------------------------------|
| `graph_meta`      | `scope`                  | Metadata + stats + timestamps    |
| `graph_nodes`     | `(scope, node_id)`       | Vertex (CN/EX/N/E/F) attributes  |
| `graph_edges`     | `(scope, src, dst)`      | Edge attributes (comm, contains) |
| `graph_mutations` | one per change           | Append-only audit log            |

`scope` values:

- `"aircraft"`, `"ground"`, `"simulation"`, … — per-container graph
- `"__composed__"` — unified multi-container graph built by `fish_compose`
- `"__main__"` — default scope for standalone (single-container) sessions

Each vertex/edge stores FishVertex-compatible fields (`t_v`, `id_v`, `A_v`,
`Z_v`, `level`) flattened to top-level BSON keys so targeted MongoDB `$set`
updates work without re-writing whole documents.

Override the Mongo URI via environment variable:

```bash
export FISH_MONGO_URI="mongodb://localhost:27017"   # default
```

See `notes/db_structure.txt` for the full rationale of the session-per-DB
layout.

## Writing the graph

`model_improved.py` writes automatically at the end of each run. The CLI
takes `--session` (MongoDB DB name) plus optionally `--role` (for
multi-container flows):

```bash
# Standalone trace (single container) — writes to DB "fish_20260322_212915"
# under scope "__main__":
python3 -m postprocess.model_improved --session fish_20260322_212915

# Multi-container, run manually for the aircraft container — writes to
# DB "fish_compose_20260419_161633" under scope "aircraft":
python3 -m postprocess.model_improved \
    --session fish_compose_20260419_161633 \
    --role aircraft \
    --source-trace ~/fish_traces/fish_compose_20260419_161633/aircraft
```

Pass `--no-db` to skip persisting (keeps the local `fish_graph.json`).

`fish_compose.py` orchestrates multi-container sessions — all three
containers share one DB; per-container graphs are saved under their role as
the scope; the composed graph is saved under `"__composed__"`:

```bash
# First run — ingest + extract + compose (all three containers into one DB)
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260419_161633

# Iterate on compose logic only — reuses cached per-container graphs
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260419_161633 \
    --from-cache

# Re-extract after a model_improved.py change, without re-ingesting traces
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260419_161633 \
    --skip-ingest --force-reextract
```

## Reading the graph

From Python, `load_graph(db_name, scope)` returns a NetworkX `DiGraph` whose
node data contains a `Vertex` object (FishVertex-compatible):

```python
from postprocess import graph_store

# Per-container graph
G = graph_store.load_graph("fish_compose_20260419_161633", "aircraft")

# Composed multi-container graph
U = graph_store.load_graph("fish_compose_20260419_161633",
                           graph_store.COMPOSED_SCOPE)

for nid, data in G.nodes(data=True):
    v = data["v"]
    print(v.t_v, v.id_v, v.A_v.get("label"), v.level)
```

Metadata / listing:

```python
# All session DBs in Mongo
graph_store.list_sessions()

# All scopes within one session DB
graph_store.list_graphs("fish_compose_20260419_161633")

# Single scope's metadata
graph_store.get_metadata("fish_compose_20260419_161633", "aircraft")
graph_store.graph_exists("fish_compose_20260419_161633", "__composed__")
```

## Mutating the graph

Every mutation appends a record to `graph_mutations` with the `actor`,
`note`, and a diff of what changed. Use `actor` to identify the caller
(`"roofline"`, `"manual_review"`, …) so later passes can query or revert.

```python
from postprocess import graph_store

DB = "fish_compose_20260419_161633"
SCOPE = "aircraft"

# Update an existing node's attributes
graph_store.update_node(
    DB, SCOPE, node_id=12,
    attrs={"wcet_ns": 1_500_000, "gpu_node": True},
    actor="roofline",
    note="derived from PMU counters",
)

# Annotate an edge with a measured rate
graph_store.update_edge(
    DB, SCOPE, source=12, target=34,
    attrs={"avg_rate_hz": 20.0},
    actor="rate_analysis",
)

# Structural edits
graph_store.add_node(DB, SCOPE, vertex, actor="manual")
graph_store.add_edge(DB, SCOPE, u, v, {"rel": "comm", "topic": "/foo"},
                     actor="manual")
graph_store.remove_node(DB, SCOPE, node_id, actor="manual")   # cascades edges
graph_store.remove_edge(DB, SCOPE, u, v, actor="manual")
```

## Exporting for fish\_viz

`fish_viz.html` consumes the same JSON format the old `export_json` wrote.
Regenerate it from whatever is in the store:

```bash
# Export the composed graph for a session
python3 graph_store.py export fish_compose_20260419_161633 __composed__ \
    -o fish_graph.json

# Export a single per-container graph
python3 graph_store.py export fish_compose_20260419_161633 aircraft \
    -o aircraft_only.json
```

or programmatically:

```python
data = graph_store.to_viz_json(DB, SCOPE)    # → {"nodes": [...], "edges": [...]}
graph_store.export_json(DB, SCOPE, "fish_graph.json")
```

## CLI

```bash
python3 graph_store.py sessions                          # list session DBs
python3 graph_store.py list      <session>               # scopes in a session
python3 graph_store.py info      <session> <scope>
python3 graph_store.py export    <session> <scope> [-o fish_graph.json]
python3 graph_store.py delete    <session> <scope> --yes # delete one scope
python3 graph_store.py drop      <session> --yes         # drop whole session DB
python3 graph_store.py mutations <session> <scope> [-n 20]
```

## Typical workflow

1. **Run the mission.** `fish_orchestrator.py` collects traces into a
   `fish_compose_<ts>/` directory.
2. **Compose.** `python3 fish_compose.py <dir>` ingests each container's
   raw trace (role-suffixed collections), extracts per-container models,
   merges them into a composed graph, and drops `fish_graph.json` for the
   viz — all inside a single new DB named like the session.
3. **Analyze.** Load the composed graph in a notebook or analysis script,
   compute what you need, write results back via `update_node` /
   `update_edge`. Mutations accumulate in `graph_mutations`.
4. **Visualize.** Re-export with `graph_store export <session> <scope>`
   whenever you want the viz to reflect the latest state.

## InfluxDB side

GPU/nsys data lives in a single shared InfluxDB 3 database called **`fish`**
(one per installation, never per-session). Measurements inside:

- `gpu_kernels`, `gpu_memcpy`, `cuda_runtime`, `gpu_mem_usage`, `nvtx_events`

Every point carries two extra tags so cross-session queries stay natural
and one session can be dropped without touching the others:

- `session` — e.g. `fish_compose_20260419_161633` (matches the MongoDB DB)
- `container` — e.g. `aircraft` (the role, or the session name for standalone)

Why one database: InfluxDB 3 Core enforces a 5-database limit. Consolidating
to a shared `fish` DB avoids that limit and avoids asking downstream users
to config-hack their InfluxDB. Example filter:

```sql
SELECT COUNT(*)
FROM gpu_kernels
WHERE session = 'fish_compose_20260419_161633'
  AND container = 'aircraft'
```

Deleting a session's GPU data:

```sql
DELETE FROM gpu_kernels WHERE session = 'fish_compose_20260419_161633'
-- repeat per measurement
```

## Invariants

Graph\_store does **not** currently validate the FISH hierarchy (CN→EX→N→E→F
containment, allowed edge types, monotonic levels). Validation is an
opt-in follow-up — see `notes/immediate_work.txt` item 13.
