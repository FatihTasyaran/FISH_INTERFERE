# FISH DTDL schemas

DTDL v3 + a small FISH extension (`dtmi:fish:context;1`) that adds **telemetry
pointer fields** so every Interface carries the explicit location of its live
data. This is the FISH analog of P-MoVE's PMUName/SamplerName/DBName/FieldName
encoding (P-MoVE paper §III.B, Listing 1), generalized to FISH's two-store
reality (LTTng → MongoDB, CUPTI → InfluxDB).

## Files

```
fish_context.jsonld    # @context defining fish:source/store/dbName/collection/measurement/
                       #   fieldName/eventName/tagSelector/recommendedViz/unit/derivation
host.jsonld            # dtmi:fish:host;1
session.jsonld         # dtmi:fish:session;1   ← session_id is the per-session DB name
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
| Host     | `dtmi:fish:host;1`             | `dtmi:fish:host:dolap10;1`                               |
| Session  | `dtmi:fish:session;1`          | `dtmi:fish:session:fish_20260609_034512;1`               |
| Container| `dtmi:fish:container;1`        | `dtmi:fish:container:apriltag-ctr;1`                     |
| Executor | `dtmi:fish:executor;1`         | `dtmi:fish:executor:pid_105;1`                           |
| Node     | `dtmi:fish:node;1`             | `dtmi:fish:node:r2b_AprilTagNode;1`                      |
| Entity   | `dtmi:fish:entity;1`           | `dtmi:fish:entity:r2b_AprilTagNode__image_sub;1`         |
| Callback | `dtmi:fish:callback;1`         | `dtmi:fish:callback:0x589B594A0D00;1`                    |
| CUDA stm | `dtmi:fish:cuda_stream;1`      | `dtmi:fish:cuda_stream:0x7f3a40000020;1`                 |
| Kernel   | `dtmi:fish:gpu_kernel;1`       | `dtmi:fish:gpu_kernel:_Z6cudaNHWCBlobToNCHWFloat;1`      |

Instance Interfaces `extends` the template (e.g.
`"extends": "dtmi:fish:callback;1"`) and fill in Property values. Telemetry
`tagSelector` placeholders like `{cb_addr}`, `{session_id}`, `{pid}` are
resolved at query time by the on-demand builder against the instance's
Properties (and chained-Relationship instances' Properties when prefixed,
e.g. `{session.session_id}`).

## Telemetry extension (`dtmi:fish:context;1`)

Each Telemetry entry carries:

| field                 | role                                                                                |
|-----------------------|-------------------------------------------------------------------------------------|
| `fish:source`         | datapoint origin: `lttng \| cupti \| proc \| ros2_cli \| dcgm \| derived`            |
| `fish:store`          | backing store: `mongodb \| influxdb`                                                |
| `fish:dbName`         | database name; `{session_id}` literal placeholder for session-scoped, `fish` for GPU shared |
| `fish:collection`     | MongoDB collection (when store=mongodb)                                             |
| `fish:measurement`    | InfluxDB measurement (when store=influxdb)                                          |
| `fish:fieldName`      | InfluxDB field or MongoDB doc path                                                  |
| `fish:eventName`      | source event identifier (e.g. `ros2:callback_start`, `CUPTI_ACTIVITY_KIND_KERNEL`)   |
| `fish:tagSelector`    | `{tag_key: value-or-placeholder}` filter applied at query time                     |
| `fish:recommendedViz` | panel-factory hint: `timeseries \| heatmap \| stat \| timeline \| histogram \| roofline \| bargauge` |
| `fish:unit`           | physical unit                                                                       |
| `fish:derivation`     | for `fish:source=derived`: short expression of source telemetries + transform       |

## Storage map (where each FISH datapoint actually lives)

| source       | store    | dbName          | container               | typical use                       |
|--------------|----------|-----------------|-------------------------|-----------------------------------|
| LTTng ros2_* | mongodb  | `{session_id}`  | `ros2_trace` collection | callback chain, entity init       |
| ros2 CLI     | mongodb  | `{session_id}`  | `node_info`, `topic_*`  | snapshot metadata + topic hz      |
| /proc        | mongodb  | `{session_id}`  | `process_tree`          | per-pid CPU / RSS                 |
| CUPTI kernel | influxdb | `fish` (shared) | `gpu_kernels`           | per-kernel duration               |
| CUPTI memcpy | influxdb | `fish`          | `gpu_memcpy`            | host↔device transfers             |
| CUPTI memset | influxdb | `fish`          | `gpu_memset`            | memset durations                  |
| CUPTI sync   | influxdb | `fish`          | `gpu_sync`              | StreamWaitEvent / EventRecord     |
| CUPTI rt     | influxdb | `fish`          | `cuda_runtime`          | cudaLaunchKernel / cudaMemcpy     |
| nsys mem use | influxdb | `fish`          | `gpu_mem_usage`         | device mem allocation timeline    |
| FISH bridge  | influxdb | `fish`          | `cuda_callchain`        | callback→kernel correlation       |

## Future extends (subtypes — first-class, not synthetic)

Per the design principle "everything is Interface, everything first-class":

- `dtmi:fish:node:rclcpp;1` extends `node` → adds `language="cpp"` Property
- `dtmi:fish:node:rclpy;1` extends `node` → adds `language="python"`, different init event tagSelector
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
5. For each instance, iterate its Telemetry entries → resolve placeholders →
   call the panel factory with `(fish:store, fish:dbName, fish:measurement
   or fish:collection, fish:fieldName, fish:tagSelector, fish:recommendedViz)`
   → get a Grafana panel JSON dict.
6. Auto-layout panels in a grid; POST the dashboard JSON to Grafana API.

The builder's resolution / factory / layout / uploader code does NOT change
when new subtypes or new telemetry types are added — only the Interface
JSON-LD files do.
