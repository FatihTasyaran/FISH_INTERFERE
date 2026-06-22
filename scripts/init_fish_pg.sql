-- =============================================================================
-- FISH PostgreSQL + TimescaleDB schema
-- =============================================================================
-- Run as:
--   PGPASSWORD=fish psql -h localhost -U fish -d fish -f init_fish_pg.sql
--
-- Idempotent — uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
-- Order matters: registry tables first, then dependents, then hypertables.
--
-- Conventions (see schemas/README.md for the design rationale):
--   * ts_ns BIGINT NOT NULL  — absolute UTC nanoseconds since 1970.
--     LTTng+nsys time-alignment, ts/ts_nanos pairing, nsys session-start
--     offset are ALL resolved at ingest time. Readers see flat absolute ns.
--   * session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE
--   * JSONB for variant payloads.
--   * Hypertable chunks are 5 minutes (300e9 ns) wide.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- -----------------------------------------------------------------------------
-- A. Registry tables
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS hosts (
    host_name        TEXT        PRIMARY KEY,
    arch             TEXT,
    kernel_version   TEXT,
    ros_distro       TEXT,
    cpu_model        TEXT,
    cpu_cores        INT,
    total_ram_kb     BIGINT,
    first_seen       TIMESTAMPTZ DEFAULT now(),
    last_seen        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT        PRIMARY KEY,
    host_name         TEXT        REFERENCES hosts(host_name),
    role              TEXT,
    start_ts_ns       BIGINT,
    end_ts_ns         BIGINT,
    duration_ns       BIGINT      GENERATED ALWAYS AS (end_ts_ns - start_ts_ns) STORED,
    start_utc         TIMESTAMPTZ,
    target_image      TEXT,
    launch_script     TEXT,
    fish_version      TEXT,
    tracepoint_set    TEXT,
    tracepoint_count  INT,
    nsys_flags        TEXT,
    components_loaded JSONB,
    session_dir       TEXT,
    status            TEXT        DEFAULT 'ok',
    inserted_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sessions_host ON sessions(host_name);

-- -----------------------------------------------------------------------------
-- B. Snapshot / static (flat tables)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS node_info (
    id              BIGSERIAL   PRIMARY KEY,
    session_id      TEXT        NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    node_full_name  TEXT        NOT NULL,
    captured_ts_ns  BIGINT,
    UNIQUE (session_id, node_full_name)
);
CREATE INDEX IF NOT EXISTS idx_node_info_session ON node_info(session_id);

CREATE TABLE IF NOT EXISTS node_endpoints (
    id               BIGSERIAL   PRIMARY KEY,
    session_id       TEXT        NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    node_full_name   TEXT        NOT NULL,
    endpoint_kind    TEXT        NOT NULL,  -- sub | pub | srv_server | srv_client | act_server | act_client
    topic_or_service TEXT        NOT NULL,
    message_type     TEXT,
    UNIQUE (session_id, node_full_name, endpoint_kind, topic_or_service)
);
CREATE INDEX IF NOT EXISTS idx_node_endpoints_session_kind ON node_endpoints(session_id, endpoint_kind);
CREATE INDEX IF NOT EXISTS idx_node_endpoints_topic ON node_endpoints(session_id, topic_or_service);
CREATE INDEX IF NOT EXISTS idx_node_endpoints_node ON node_endpoints(session_id, node_full_name);

CREATE TABLE IF NOT EXISTS node_list (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    node        TEXT NOT NULL,
    PRIMARY KEY (session_id, node)
);

CREATE TABLE IF NOT EXISTS topic_info (
    session_id          TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    topic               TEXT NOT NULL,
    type                TEXT,
    publisher_count     INT,
    subscription_count  INT,
    PRIMARY KEY (session_id, topic)
);

CREATE TABLE IF NOT EXISTS component_list (
    id               BIGSERIAL PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    container_name   TEXT,
    child_index      INT,
    child_node       TEXT,
    is_empty         BOOLEAN DEFAULT false,
    note             TEXT,
    UNIQUE (session_id, container_name, child_index)
);

CREATE TABLE IF NOT EXISTS system_env (
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    value           TEXT,
    name_enum       INT,
    global_vid      BIGINT,
    dev_state_name  TEXT,
    PRIMARY KEY (session_id, name)
);

CREATE TABLE IF NOT EXISTS gpu_info (
    session_id                TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    gpu_id                    INT  NOT NULL,
    name                      TEXT,
    uuid                      TEXT,
    chip_name                 TEXT,
    bus_location              TEXT,
    is_discrete               BOOLEAN,
    total_memory_bytes        BIGINT,
    memory_bandwidth_bps      BIGINT,
    constant_memory_bytes     BIGINT,
    l2_cache_bytes            BIGINT,
    clock_rate_hz             BIGINT,
    sm_count                  INT,
    threads_per_warp          INT,
    async_engines             INT,
    max_warps_per_sm          INT,
    max_blocks_per_sm         INT,
    max_threads_per_block     INT,
    max_registers_per_block   INT,
    max_shmem_per_block       INT,
    max_shmem_per_block_optin INT,
    max_shmem_per_sm          INT,
    max_registers_per_sm      INT,
    compute_major             INT,
    compute_minor             INT,
    sm_major                  INT,
    sm_minor                  INT,
    extras                    JSONB,
    PRIMARY KEY (session_id, gpu_id)
);

CREATE TABLE IF NOT EXISTS cuda_session (
    session_id                  TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    utc_epoch_ns                BIGINT,       -- raw nsys value (may be tz-buggy on some toolchains)
    system_clock_ns             BIGINT,
    utc_time                    TEXT,
    local_time                  TEXT,
    session_start_ns_corrected  BIGINT        -- ISSUE-002 fix: tz-aware ISO-string → UTC ns
);

CREATE TABLE IF NOT EXISTS cuda_context (
    session_id        TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    context_id        INT  NOT NULL,
    hw_id             INT,
    vm_id             INT,
    process_id        INT,
    device_id         INT,
    parent_context_id INT,
    is_green_context  BOOLEAN,
    null_stream_id BIGINT,
    num_multiprocessors INT,
    PRIMARY KEY (session_id, context_id)
);

CREATE TABLE IF NOT EXISTS cuda_device (
    session_id           TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    cuda_id              INT  NOT NULL,
    gpu_id               INT,
    pid                  INT,
    uuid                 TEXT,
    num_multiprocessors  INT,
    PRIMARY KEY (session_id, cuda_id)
);

CREATE TABLE IF NOT EXISTS cuda_streams (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    stream_id   BIGINT NOT NULL,
    hw_id       INT,
    vm_id       INT,
    process_id  INT,
    context_id  INT,
    priority    BIGINT,
    flag        INT,
    PRIMARY KEY (session_id, stream_id)
);

CREATE TABLE IF NOT EXISTS nsys_enums (
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    enum_name    TEXT NOT NULL,
    entry_id     INT  NOT NULL,
    entry_name   TEXT,
    entry_label  TEXT,
    PRIMARY KEY (session_id, enum_name, entry_id)
);

-- -----------------------------------------------------------------------------
-- C. FISH model graph (normalized — one row per vertex / edge)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS graph_nodes (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,
    node_id     INT  NOT NULL,
    type        TEXT,
    label       TEXT,
    level       INT,
    children    INT[],
    attrs       JSONB,             -- A_v keys that don't have a typed column
    pid         INT,
    full_name   TEXT,
    etype       TEXT,
    ptype       TEXT,
    cb_addr     TEXT,
    external    BOOLEAN,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (session_id, scope, node_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_scope ON graph_nodes(session_id, scope, type);

CREATE TABLE IF NOT EXISTS graph_edges (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    scope      TEXT NOT NULL,
    source     INT  NOT NULL,
    target     INT  NOT NULL,
    rel        TEXT,
    level      TEXT,
    attrs      JSONB,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (session_id, scope, source, target, rel)
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_scope ON graph_edges(session_id, scope, level);

CREATE TABLE IF NOT EXISTS graph_meta (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,
    is_composed BOOLEAN DEFAULT false,
    source_trace TEXT,
    container_role TEXT,
    stats       JSONB,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (session_id, scope)
);

CREATE TABLE IF NOT EXISTS graph_mutations (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,
    ts          TIMESTAMPTZ DEFAULT now(),
    op          TEXT,
    actor       TEXT,
    note        TEXT,
    target      JSONB,
    changes     JSONB
);
CREATE INDEX IF NOT EXISTS idx_graph_mutations_scope ON graph_mutations(session_id, scope, ts);

-- -----------------------------------------------------------------------------
-- D. Hypertables (time-series)
-- -----------------------------------------------------------------------------

-- LTTng ros2_trace events
CREATE TABLE IF NOT EXISTS ros2_trace (
    id          BIGSERIAL,
    ts_ns       BIGINT      NOT NULL,
    ts          TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id  TEXT        NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    event       TEXT        NOT NULL,
    cpu_id      SMALLINT,
    vpid        INT,
    vtid        INT,
    host_name   TEXT,
    procname    TEXT,
    payload     JSONB,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('ros2_trace', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ros2_trace_ts    ON ros2_trace(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_ros2_trace_evt   ON ros2_trace(session_id, event, ts_ns);
CREATE INDEX IF NOT EXISTS idx_ros2_trace_vpid  ON ros2_trace(session_id, vpid, ts_ns);
CREATE INDEX IF NOT EXISTS idx_ros2_trace_cb    ON ros2_trace(session_id, (payload->>'callback'));
CREATE INDEX IF NOT EXISTS idx_ros2_trace_nh    ON ros2_trace(session_id, (payload->>'node_handle'));
CREATE INDEX IF NOT EXISTS idx_ros2_trace_topic ON ros2_trace(session_id, (payload->>'topic_name'));
CREATE INDEX IF NOT EXISTS idx_ros2_trace_payload_gin ON ros2_trace USING GIN(payload);

-- GPU kernels
CREATE TABLE IF NOT EXISTS gpu_kernels (
    id                  BIGSERIAL,
    ts_ns               BIGINT  NOT NULL,
    ts                  TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id          TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns         BIGINT,
    kernel_name         TEXT,
    kernel_short_name   TEXT,
    correlation_id      BIGINT,
    context_id          INT,
    device_id           INT,
    stream_id BIGINT,
    global_pid          BIGINT,
    grid_x              INT, grid_y INT, grid_z INT,
    block_x             INT, block_y INT, block_z INT,
    registers_per_thread INT,
    static_smem_bytes    INT,
    dynamic_smem_bytes   INT,
    container            TEXT,
    source               TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('gpu_kernels', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_gpu_kernels_name   ON gpu_kernels(session_id, kernel_name, ts_ns);
CREATE INDEX IF NOT EXISTS idx_gpu_kernels_corr   ON gpu_kernels(session_id, correlation_id);
CREATE INDEX IF NOT EXISTS idx_gpu_kernels_stream ON gpu_kernels(session_id, stream_id, ts_ns);

-- GPU memcpy
CREATE TABLE IF NOT EXISTS gpu_memcpy (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns BIGINT, bytes BIGINT, copy_kind INT,
    correlation_id BIGINT, context_id INT, device_id INT, stream_id BIGINT,
    global_pid BIGINT, container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('gpu_memcpy', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS gpu_memset (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns BIGINT, bytes BIGINT, value BIGINT, mem_kind INT,
    correlation_id BIGINT, context_id INT, device_id INT, stream_id BIGINT,
    global_pid BIGINT, container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('gpu_memset', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS gpu_sync (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns BIGINT,
    sync_type INT, event_id BIGINT, event_sync_id BIGINT,
    correlation_id BIGINT, context_id INT, device_id INT, stream_id BIGINT,
    global_pid BIGINT, container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('gpu_sync', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS cuda_runtime (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns BIGINT, api_name TEXT, return_value INT,
    correlation_id BIGINT, callchain_id BIGINT, event_class INT,
    global_tid BIGINT, tid INT,
    container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('cuda_runtime', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_cuda_runtime_corr ON cuda_runtime(session_id, correlation_id);
CREATE INDEX IF NOT EXISTS idx_cuda_runtime_tid  ON cuda_runtime(session_id, tid);

CREATE TABLE IF NOT EXISTS gpu_overhead (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns BIGINT, overhead_type INT, overhead_name TEXT,
    correlation_id BIGINT, event_class INT,
    global_tid BIGINT, tid INT, container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('gpu_overhead', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS gpu_mem_usage (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    bytes BIGINT, mem_kind INT, memory_operation_type INT,
    correlation_id BIGINT, context_id INT, device_id INT,
    global_pid BIGINT, container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('gpu_mem_usage', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS cuda_callchain (
    id           BIGSERIAL,
    ts_ns        BIGINT NOT NULL,
    ts           TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id   TEXT   NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    callchain_id BIGINT NOT NULL,
    stack_depth  INT,
    symbol       TEXT,
    original_ip  BIGINT,
    module       TEXT,
    unresolved   BOOLEAN,
    container    TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('cuda_callchain', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_cuda_callchain_id ON cuda_callchain(session_id, callchain_id, stack_depth);

CREATE TABLE IF NOT EXISTS nvtx_events (
    id BIGSERIAL, ts_ns BIGINT NOT NULL,
    ts TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    duration_ns BIGINT, domain_id INT, event_type INT,
    text TEXT, container TEXT, source TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('nvtx_events', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS fish_events (
    id           BIGSERIAL,
    ts_ns        BIGINT NOT NULL,
    ts           TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id   TEXT   NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    monotonic_ns BIGINT,
    action       TEXT,
    process_name TEXT,
    cmd          TEXT,
    killed_pid   INT,
    resurrected_pid INT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('fish_events', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_fish_events_action ON fish_events(session_id, action);

CREATE TABLE IF NOT EXISTS process_tree (
    id           BIGSERIAL,
    ts_ns        BIGINT NOT NULL,
    ts           TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id   TEXT   NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    pid          INT,
    ppid         INT,
    lwp          INT,
    uid          INT,
    username     TEXT,
    cmd          TEXT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('process_tree', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_process_tree_pid ON process_tree(session_id, pid);

CREATE TABLE IF NOT EXISTS process_tree_threads (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    pid         INT  NOT NULL,
    lwp         INT  NOT NULL,
    cmd         TEXT,
    captured_ts_ns BIGINT,
    UNIQUE (session_id, pid, lwp)
);
CREATE INDEX IF NOT EXISTS idx_process_tree_threads_pid ON process_tree_threads(session_id, pid);

CREATE TABLE IF NOT EXISTS topic_hz (
    id            BIGSERIAL,
    ts_ns         BIGINT NOT NULL,
    ts            TIMESTAMPTZ GENERATED ALWAYS AS (to_timestamp(ts_ns::double precision / 1000000000)) STORED,
    session_id    TEXT   NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    topic         TEXT,
    average_rate  DOUBLE PRECISION,
    min_period    DOUBLE PRECISION,
    max_period    DOUBLE PRECISION,
    std_dev       DOUBLE PRECISION,
    window_size   INT,
    PRIMARY KEY (session_id, ts_ns, id)
);
SELECT create_hypertable('topic_hz', 'ts_ns', chunk_time_interval => 300000000000, if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_topic_hz_topic ON topic_hz(session_id, topic);

-- =============================================================================
-- Done.
-- =============================================================================

-- Generated-column ts indexes for all the other hypertables (added 2026-06-16
-- when generated columns were introduced; idempotent thanks to IF NOT EXISTS).
CREATE INDEX IF NOT EXISTS idx_gpu_kernels_ts   ON gpu_kernels(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_gpu_memcpy_ts    ON gpu_memcpy(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_gpu_memset_ts    ON gpu_memset(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_gpu_sync_ts      ON gpu_sync(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_cuda_runtime_ts  ON cuda_runtime(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_gpu_overhead_ts  ON gpu_overhead(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_gpu_mem_usage_ts ON gpu_mem_usage(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_cuda_callchain_ts ON cuda_callchain(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_nvtx_events_ts   ON nvtx_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_fish_events_ts   ON fish_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_process_tree_ts  ON process_tree(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_topic_hz_ts      ON topic_hz(session_id, ts);
