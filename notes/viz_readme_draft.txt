FISH Visualization Setup — Draft (2026-06-17)
================================================

End-to-end: trace dir on disk → PostgreSQL → Grafana panels (timeseries,
state timeline, stats, *and* the D3 fish_viz graph embedded via
gapit-htmlgraphics-panel).

This is a working draft — paste/clean into docs/ later.


1. Backend: PostgreSQL + TimescaleDB
====================================

Install + extension + fish db (one-shot, idempotent):

    sudo ./scripts/setup_postgres_fish.sh

  - Picks up an existing PG cluster's version via pg_lsclusters; defaults
    to PG 16 if none. Override: PG_VERSION=18 sudo -E ./...
  - Adds the pgdg + Timescale packagecloud repos
  - Installs postgresql-N + matching timescaledb extension (handles
    pgdg/packagecloud package-name conflict — uses whichever variant
    already shipped a .so)
  - Sets shared_preload_libraries='timescaledb' in postgresql.conf
  - Restarts PG, creates user `fish` + db `fish` + enables extension
  - PG 18 may not have a timescaledb-tools package yet; the script
    skips it (only nice-to-have for auto-tuning).

Verify:

    PGPASSWORD=fish psql -h localhost -U fish -d fish -c \
        "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
    # → plpgsql 1.0  +  timescaledb 2.27.x


2. Schema
=========

scripts/init_fish_pg.sql defines 30+ tables:
  - 13 hypertables (ros2_trace, gpu_kernels/memcpy/memset/sync/runtime/
                    overhead/mem_usage, cuda_callchain, nvtx_events,
                    fish_events, process_tree, topic_hz)
  - 17 flat tables (sessions, hosts, node_info, node_endpoints, node_list,
                    topic_info, component_list, system_env, gpu_info,
                    cuda_session, cuda_context, cuda_device, cuda_streams,
                    nsys_enums, graph_nodes, graph_edges, graph_meta,
                    graph_mutations, process_tree_threads)

Time convention: every hypertable has
  ts_ns BIGINT NOT NULL                      -- absolute UTC ns since epoch
  ts    TIMESTAMPTZ GENERATED ALWAYS AS …    -- derived; what Grafana sees

Grafana's $__timeFilter macro expects TIMESTAMPTZ — without the ts column
every panel query had to wrap ts_ns in to_timestamp(ts_ns/1e9). The
generated column is STORED + indexed (session_id, ts), so it's free at
query time and queries can do plain `WHERE $__timeFilter(ts)`.


3. Ingest
=========

    python3 -m postprocess.ingest_pg /path/to/session_dir [--force]

  - Reads `ros2/` LTTng CTF + `nsys/*.sqlite` + `snapshot/*` +
    `fishlog/*` + `launch_components.json`
  - Parallel: snapshot/proc/fishlog ingests sequential (small), ros2_trace
    (babeltrace2 stream) + nsys (per-CUPTI-table workers) concurrent
  - Resolves at ingest time (so readers never have to):
      ts/ts_nanos split  →  single ts_ns BIGINT
      nsys relative start +session_start_ns  →  absolute UTC ts_ns
      nsys utcEpochNs local-epoch-ns bug (ISSUE-002)  →  tz-aware ISO parse


4. Model extraction
===================

    python3 -m postprocess.model_improved_pg \
        --session <session_id> --out fish_graph.json

  - Mirror of model_improved.py, reads ros2_trace via SQL on PG.
  - Critical detail: NitrosNode registers 2 rclcpp_subscription_init for
    the same sub_handle (compat + negotiated). Mongo's natural iteration
    order happened to keep the FIRST-registered callback; PG's
    ORDER BY ts_ns, id + dict-comp's last-wins flipped this and dropped
    pub aspects. We use a _keep_first() helper on handle→callback chains
    to reproduce Mongo's behaviour exactly.
  - Verified: 256 nodes / 311 edges, 0 differing vertex sigs and 0
    differing edge sigs vs the original Mongo+Influx pipeline output for
    the same session.

Persisted to PG via postprocess/graph_store_pg.py
(graph_nodes / graph_edges / graph_meta / graph_mutations tables).


5. Grafana setup
================

5.1. PostgreSQL datasource
--------------------------

UI: Connections → Data sources → Add → PostgreSQL.
Settings:
  Name:                 FISH-Postgres
  Host URL:             localhost:5432
  Database:             fish
  Username:             fish
  Password:             fish
  TLS/SSL Mode:         disable
  PostgreSQL Version:   18 (or 16, depending on install)
  TimescaleDB:          ON

Or via REST:

    curl -s -u admin:<pw> -X POST http://localhost:3000/api/datasources \
      -H "Content-Type: application/json" -d '{
        "name":"FISH-Postgres","type":"postgres","access":"proxy",
        "url":"localhost:5432","database":"fish","user":"fish",
        "secureJsonData":{"password":"fish"},
        "jsonData":{"sslmode":"disable","postgresVersion":1800,"timescaledb":true},
        "basicAuth":false}'


5.2. Dashboard variables
------------------------

session_id  → Query: SELECT session_id FROM sessions ORDER BY inserted_at DESC
              (Value Field + Text Field empty → defaults to the one column)

scope       → Custom: __main__,__composed__


5.3. Time range
---------------

Pick a range that includes the session timestamps. Example: today's
apriltag session captured at 19:23–19:25 local → Last 6 hours or
Last 24 hours works.


5.4. Generic time-series query pattern
--------------------------------------

    SELECT
      $__timeGroup(ts, $__interval) AS time,
      event,
      count(*) AS rate
    FROM ros2_trace
    WHERE session_id = '$session_id'
      AND $__timeFilter(ts)
    GROUP BY 1, 2
    ORDER BY 1;

For Time series panels: a column aliased `AS time` (or any timestamp
column) is REQUIRED — otherwise Grafana renders empty even though the
Table format shows rows. Most "no data" symptoms when switching Table →
Time series trace back to missing this alias.


6. fish_viz embed via gapit-htmlgraphics-panel
==============================================

Install plugin (one-time):

    sudo grafana-cli plugins install gapit-htmlgraphics-panel
    sudo systemctl restart grafana-server


6.1. CSP gotcha — REQUIRED
--------------------------

gapit's JavaScript editor loads scripts but Grafana's default CSP
silently blocks external D3 load. Even though `<script>.onload` fires,
`window.d3` is never populated. Symptom: panel HTML/CSS renders but
the SVG stays empty, console shows
`Uncaught ReferenceError: d3 is not defined`.

Fix — relax CSP in grafana.ini:

    sudo nano /etc/grafana/grafana.ini
    # [security]
    content_security_policy = false      # localhost dev — acceptable

Or surgical (just allow d3js.org):

    content_security_policy = true
    content_security_policy_template = """default-src 'self'; \
        script-src 'self' 'unsafe-eval' 'unsafe-inline' https://d3js.org; \
        style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; \
        font-src 'self' data:; connect-src 'self'; object-src 'none'"""

Then `sudo systemctl restart grafana-server`.


6.2. Panel setup
----------------

1. Add panel → Visualization: HTML Graphics
2. Two queries (PostgreSQL datasource, Format: Table — NOT Time series):

   Query A (refId = A) — nodes:

       SELECT
         node_id   AS id,
         type, label, level,
         children::text   AS children,
         pid, full_name, etype, ptype, cb_addr, external,
         attrs::text      AS attrs
       FROM graph_nodes
       WHERE session_id = '$session_id' AND scope = '$scope'
       ORDER BY node_id;

   Query B (refId = B) — edges:

       SELECT source, target, rel, level, attrs::text AS attrs
       FROM graph_edges
       WHERE session_id = '$session_id' AND scope = '$scope';

3. Panel editors:
     Root CSS      ← examples/grafana_gapit/panel.css   (replace default)
     HTML          ← examples/grafana_gapit/panel.html
     onInit        ← empty
     onRender      ← examples/grafana_gapit/panel.js

4. Save. Dashboard variable dropdown → session graph re-renders on change.


6.3. What the port did
----------------------

postprocess/fish_viz.html →  examples/grafana_gapit/{html,css,js}

  - fetch('fish_graph.json') replaced with buildRaw(data.series) — parses
    Grafana's DataFrame columnar shape (fields[].values + frame.length)
    into the {nodes:[], edges:[]} structure the D3 code expects.
  - 100vw/100vh viewport → 100%/100% panel container
  - position: fixed → position: absolute (controls/info/stats/legend
    pinned to the panel, not the viewport)
  - All getElementById / querySelector calls scoped to gapit's `htmlNode`
    (multiple panels per dashboard coexist without ID collisions).
  - D3 v7 loaded via polling (set interval until window.d3 appears), to
    tolerate gapit's onload firing before the global is attached. CSP
    must still allow the load (see 6.1).


7. Known caveats
================

- Time range default `now-6h` will be empty for older sessions; expand
  to `Last 24 hours` or pick explicit start/end.
- Some sqlite files lack CUDA_CALLCHAINS / CUPTI_OVERHEAD /
  GPU_MEMORY_USAGE tables when nsys SIGSEGV'd at shutdown. ingest_pg.py
  treats missing tables as 0-row, no error.
- Builder Format=Table auto-appends LIMIT 50. Switch to Code mode or
  Format=Time series to drop the limit.
- Grafana Builder UI doesn't surface our `ts` generated column in the
  Column dropdown until you Refresh schema (Connections → datasource →
  ⋮ → Refresh schema).
- gapit-htmlgraphics-panel ships v2.2.3 in our setup; if Plugin Options
  shows an "External Scripts" field in your version, prefer that over
  CSP relaxation (less invasive).


8. TODO (for the real README)
=============================

- One screenshot per panel type (timeseries, stat, state timeline, graph)
- Document the role of dashboard variables in the on-demand builder flow
- Cross-link to schemas/README.md for the DTDL Telemetry pointer schema
- Notes on multi-session dashboards (session variable as multi-value?)
- gapit panel: try moving D3 to onInit so that polling fires only on
  first render — minor optimisation, doesn't change correctness
- Replace fish_viz.html standalone usage notes in HANDOFF.md with a
  pointer to this draft
