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

All graph state lives in a single MongoDB database (default `fish_meta`,
separate from the per-session raw-trace DBs that `ingest.py` creates).

| Collection       | One doc per              | Purpose                          |
|------------------|--------------------------|----------------------------------|
| `fish_graphs`    | session\_name            | Metadata + stats + timestamps    |
| `fish_nodes`     | (session, node\_id)      | Vertex (CN/EX/N/E/F) attributes  |
| `fish_edges`     | (session, src, dst)      | Edge attributes (comm, contains) |
| `fish_mutations` | one per change           | Append-only audit log            |

Each vertex/edge stores FishVertex-compatible fields (`t_v`, `id_v`, `A_v`,
`Z_v`, `level`) flattened to top-level BSON keys so targeted MongoDB
`$set` updates work without re-writing whole documents.

Override defaults with environment variables:

```bash
export FISH_MONGO_URI="mongodb://localhost:27017"   # default
export FISH_META_DB="fish_meta"                     # default
```

## Writing the graph

`model_improved.py` writes automatically at the end of each run:

```bash
python3 -m postprocess.model_improved --session aircraft_1 \
    --container-role aircraft \
    --source-trace ~/fish_traces/fish_compose_20260414_003054/aircraft
```

Pass `--no-db` to skip the MongoDB write (keeps the local `fish_graph.json`).

`fish_compose.py` saves each per-container graph under
`<compose_name>_<role>` and the composed graph under `<compose_name>` with
`is_composed=True`. Composition can reuse per-container graphs from a
previous run:

```bash
# First run — ingest + extract + compose
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260414_003054

# Iterate on compose logic only — reuses cached per-container graphs
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260414_003054 \
    --from-cache

# Re-extract after a model_improved.py change, without re-ingesting traces
python3 -m postprocess.fish_compose ~/fish_traces/fish_compose_20260414_003054 \
    --skip-ingest --force-reextract
```

## Reading the graph

From Python, `load_graph` returns a NetworkX `DiGraph` whose node data
contains a `Vertex` object (FishVertex-compatible):

```python
from postprocess import graph_store

G = graph_store.load_graph("aircraft_1")
for nid, data in G.nodes(data=True):
    v = data["v"]
    print(v.t_v, v.id_v, v.A_v.get("label"), v.level)
```

Metadata / listing:

```python
graph_store.list_graphs()                 # summary of all stored graphs
graph_store.get_metadata("aircraft_1")    # single metadata doc
graph_store.graph_exists("aircraft_1")
```

## Mutating the graph

Every mutation appends a record to `fish_mutations` with the `actor`,
`note`, and a diff of what changed. Use `actor` to identify the caller
(`"roofline"`, `"manual_review"`, …) so later passes can query or revert.

```python
from postprocess import graph_store

# Update an existing node's attributes
graph_store.update_node(
    "aircraft_1", node_id=12,
    attrs={"wcet_ns": 1_500_000, "gpu_node": True},
    actor="roofline",
    note="derived from PMU counters",
)

# Annotate an edge with a measured rate
graph_store.update_edge(
    "aircraft_1", source=12, target=34,
    attrs={"avg_rate_hz": 20.0},
    actor="rate_analysis",
)

# Structural edits
graph_store.add_node(session, vertex, actor="manual")
graph_store.add_edge(session, u, v, {"rel": "comm", "topic": "/foo"},
                     actor="manual")
graph_store.remove_node(session, node_id, actor="manual")   # cascades edges
graph_store.remove_edge(session, u, v, actor="manual")
```

## Exporting for fish\_viz

`fish_viz.html` consumes the same JSON format the old `export_json` wrote.
Regenerate it from whatever is in the store:

```bash
python3 -m postprocess.graph_store export aircraft_1 -o fish_graph.json
```

or programmatically:

```python
data = graph_store.to_viz_json("aircraft_1")      # → {"nodes": [...], "edges": [...]}
graph_store.export_json("aircraft_1", "fish_graph.json")
```

## CLI

```bash
python3 -m postprocess.graph_store list
python3 -m postprocess.graph_store info      <session>
python3 -m postprocess.graph_store export    <session> [-o fish_graph.json]
python3 -m postprocess.graph_store mutations <session> [-n 20]
python3 -m postprocess.graph_store delete    <session> --yes
```

## Typical workflow

1. **Run the mission.** `fish_orchestrator.py` collects traces into a
   `fish_compose_<ts>/` directory.
2. **Compose.** `python3 -m postprocess.fish_compose <dir>` ingests each
   container, extracts per-container models into `fish_meta`, merges them
   into a composed graph (also in `fish_meta`), and drops a
   `fish_graph.json` for the viz.
3. **Analyze.** Load the composed graph in a notebook or analysis script,
   compute what you need, write results back via `update_node` /
   `update_edge`. Mutations accumulate in `fish_mutations`.
4. **Visualize.** Re-export with `graph_store export <session>` whenever
   you want the viz to reflect the latest state.

## Invariants

Graph\_store does **not** currently validate the FISH hierarchy (CN→EX→N→E→F
containment, allowed edge types, monotonic levels). Validation is an
opt-in follow-up — see `notes/immediate_work.txt` item 13.
