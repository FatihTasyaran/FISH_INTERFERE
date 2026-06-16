# FISH DTDL schemas

DTDL v3 + a small FISH extension (`dtmi:fish:context;1`) that adds **telemetry
pointer fields** so every Interface carries the explicit location of its live
data in the FISH PostgreSQL+TimescaleDB backend. Originally inspired by
P-MoVE's PMUName/SamplerName/DBName/FieldName encoding (P-MoVE paper §III.B,
Listing 1). After the 2026-06-16 migration to a single-database PG backend,
the previous `store`/`collection`/`measurement` split was collapsed into one
`fish:table` + `fish:column` + `fish:filter` triple.

## Files

```
fish_context.jsonld    # @context defining fish:source / fish:table /
                       #   fish:column / fish:filter / fish:eventName /
                       #   fish:recommendedViz / fish:unit / fish:derivation
host.jsonld            # dtmi:fish:host;1
session.jsonld         # dtmi:fish:session;1   ← session_id is the tenancy key
container.jsonld       # dtmi:fish:container;1
executor.jsonld        # dtmi:fish:executor;1  (EX)
node.jsonld            # dtmi:fish:node;1      (N — rclcpp / rclpy / container_manager)
entity.jsonld          # dtmi:fish:entity;1    (E — sub/pub/srv/cli/tmr/generic_sub/...)
callback.jsonld        # dtmi:fish:callback;1  (F — first-class)
cuda_stream.jsonld     # dtmi:fish:cuda_stream;1
gpu_kernel.jsonld      # dtmi:fish:gpu_kernel;1
```

## Relationship graph

```
host ──has_session──▶ session ──has_executor──▶ executor ──has_node──▶ node
                          │                          │                    │
                          └──runs_container──▶ container                  │
                                                     │                    │
                                                     └─owns_stream─▶ cuda_stream
                                                                          │
                                                                          ▼
                                                                    entity ──has_callback──▶ callback ──launches──▶ gpu_kernel ──on_stream──▶ cuda_stream
```

Ownership is strong (object lifetime contained in parent) except `on_stream`
which is a weak reference.

## Identifier scheme

| Layer    | Template                       | Instance example                                         |
|----------|--------------------------------|----------------------------------------------------------|
| Host     | `dtmi:fish:host;1`             | `dtmi:fish:host:odachi;1`                                |
| Session  | `dtmi:fish:session;1`          | `dtmi:fish:session:fish_20260616_172344;1`               |
| Container| `dtmi:fish:container;1`        | `dtmi:fish:container:apriltag-ctr;1`                     |
| Executor | `dtmi:fish:executor;1`         | `dtmi:fish:executor:pid_1390;1`                          |
| Node     | `dtmi:fish:node;1`             | `dtmi:fish:node:r2b_AprilTagNode;1`                      |
| Entity   | `dtmi:fish:entity;1`           | `dtmi:fish:entity:r2b_AprilTagNode__image_sub;1`         |
| Callback | `dtmi:fish:callback;1`         | `dtmi:fish:callback:0x7319BC15E3B0;1`                    |
| CUDA stm | `dtmi:fish:cuda_stream;1`      | `dtmi:fish:cuda_stream:16;1`                             |
| Kernel   | `dtmi:fish:gpu_kernel;1`       | `dtmi:fish:gpu_kernel:_blur_gradient;1`                  |

Instance Interfaces `extends` the template (e.g.
`"extends": "dtmi:fish:callback;1"`) and fill in Property values.
`fish:filter` placeholders like `{cb_addr}`, `{session_id}`, `{pid}`,
`{kernel_name}` are resolved at query time by the on-demand builder against
the instance's Properties (and chained-Relationship instances' Properties
when prefixed, e.g. `{session.session_id}`).

## Telemetry extension (`dtmi:fish:context;1`)

Each Telemetry entry carries:

| field                 | role                                                                                |
|-----------------------|-------------------------------------------------------------------------------------|
| `fish:source`         | datapoint origin: `lttng \| cupti \| proc \| ros2_cli \| dcgm \| derived`            |
| `fish:table`          | PostgreSQL table name (in the single `fish` database)                              |
| `fish:column`         | PG column for the value (typed col like `duration_ns`, or JSONB path)              |
| `fish:filter`         | `{column: value-or-placeholder}` WHERE clause applied at query time                |
| `fish:eventName`      | origin event identifier for documentation (e.g. `ros2:callback_start`)             |
| `fish:recommendedViz` | panel-factory hint: `timeseries \| heatmap \| stat \| timeline \| histogram \| roofline \| bargauge` |
| `fish:unit`           | physical unit                                                                       |
| `fish:derivation`     | for `fish:source=derived`: short expression of source telemetries + transform       |

## Storage map (where each FISH datapoint actually lives)

Everything lives in a single PostgreSQL database (`fish`), with TimescaleDB
hypertables for time-series and plain tables for snapshots. See
`scripts/init_fish_pg.sql` for the authoritative schema.

| source       | PG table             | shape       | typical use                       |
|--------------|----------------------|-------------|-----------------------------------|
| LTTng ros2_* | `ros2_trace`         | hypertable  | callback chain, entity init       |
| ros2 CLI     | `node_info`          | flat        | per-node metadata                 |
| ros2 CLI     | `node_endpoints`     | flat        | normalized (node, kind, topic)    |
| ros2 CLI     | `node_list`          | flat        | bare node names                   |
| ros2 CLI     | `topic_info`         | flat        | per-topic type + pub/sub counts   |
| ros2 CLI     | `topic_hz`           | hypertable  | per-topic rate samples            |
| ros2 CLI     | `component_list`     | flat        | container → children              |
| /proc        | `process_tree`       | hypertable  | per-PID rows over time            |
| /proc        | `process_tree_threads` | flat      | per-thread LWP rows               |
| daemon       | `fish_events`        | hypertable  | kill/resurrect, system_stable, ...|
| CUPTI kernel | `gpu_kernels`        | hypertable  | per-kernel duration               |
| CUPTI memcpy | `gpu_memcpy`         | hypertable  | host↔device transfers             |
| CUPTI memset | `gpu_memset`         | hypertable  | memset durations                  |
| CUPTI sync   | `gpu_sync`           | hypertable  | StreamWaitEvent / EventRecord     |
| CUPTI rt     | `cuda_runtime`       | hypertable  | cudaLaunchKernel / cudaMemcpy     |
| CUPTI overhd | `gpu_overhead`       | hypertable  | profiler self-overhead            |
| nsys mem use | `gpu_mem_usage`      | hypertable  | device mem allocation timeline    |
| CUDA bridge  | `cuda_callchain`     | hypertable  | callback↔kernel correlation       |
| NVTX         | `nvtx_events`        | hypertable  | user markers / ranges             |
| nsys static  | `gpu_info` / `cuda_session` / `cuda_context` / `cuda_device` / `cuda_streams` / `system_env` / `nsys_enums` | flat | per-session reference data |
| FISH model   | `graph_nodes` / `graph_edges` / `graph_meta` / `graph_mutations` | flat | persisted nx.DiGraph |

## Future extends (subtypes — first-class, not synthetic)

Per the design principle "everything is Interface, everything first-class":

- `dtmi:fish:node:rclcpp;1` extends `node` → adds `language="cpp"` Property
- `dtmi:fish:node:rclpy;1` extends `node` → adds `language="python"`, different init event filter
- `dtmi:fish:node:container_manager;1` extends `node` → adds `loads_component` Relationship
- `dtmi:fish:entity:sub;1` extends `entity` → adds `msg_take_rate` Telemetry from ros2:rclcpp_take
- `dtmi:fish:entity:tmr;1` extends `entity` → adds `fires_rate` derived Telemetry, `period_ns` becomes required
- `dtmi:fish:entity:generic_sub;1` extends `entity` → marks the create_generic_subscription path (per `notes/high_impact/generic_subscription_attribution_gap.txt`)
- `dtmi:fish:entity:action_srv;1` extends `entity` → adds `goal_active_duration_ns` Telemetry

Add as needed — each new subtype is one JSON-LD file with `extends` + extra
`contents`.

## How the on-demand builder consumes this

1. Load the 9 template Interfaces into memory (one-time).
2. For a given session, emit instance Interfaces (`scripts/emit_fish_dtdl.py`)
   that `extends` templates and fill in Properties — one big JSON-LD document.
3. Builder receives a query: `(view_type, root_instance_id, depth)`.
4. Walk the instance graph from `root_instance_id` following Relationships,
   collect matching instances.
5. For each instance, iterate its Telemetry entries → resolve placeholders in
   `fish:filter` against the instance's Properties → build a parameterized
   PostgreSQL SQL query of the form
   `SELECT $fish:column FROM $fish:table WHERE <filter clauses> AND $__timeFilter(ts_ns) ORDER BY ts_ns`.
6. Emit one Grafana panel JSON dict per Telemetry (using `fish:recommendedViz`
   to pick the panel type, `fish:unit` for axis formatting).
7. Auto-layout panels in a grid; POST the dashboard JSON to Grafana API.

The builder's resolution / factory / layout / uploader code does NOT change
when new subtypes or new telemetry types are added — only the Interface
JSON-LD files do.

## Migration notes (Mongo+Influx → PG)

The previous two-store layout used `fish:store`, `fish:dbName`,
`fish:collection`, `fish:measurement`, `fish:fieldName`, and `fish:tagSelector`.
After the 2026-06-16 PG migration:

| old field                   | new field                                          |
|-----------------------------|----------------------------------------------------|
| `fish:store`                | dropped (always PostgreSQL)                        |
| `fish:dbName`               | dropped (always `fish` database)                   |
| `fish:collection`           | merged into `fish:table`                            |
| `fish:measurement`          | merged into `fish:table`                            |
| `fish:fieldName`            | renamed `fish:column`                              |
| `fish:tagSelector`          | renamed `fish:filter`                              |
| `fish:source/eventName/recommendedViz/unit/derivation` | unchanged   |

If you have older emitted instance JSON-LD documents from before the
migration, regenerate them via `scripts/emit_fish_dtdl.py` against the new
PG-resident sessions.
