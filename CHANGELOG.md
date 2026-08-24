# Changelog

Notable changes per release. Pre-1.0 — interfaces may break between
minor versions.

## Unreleased

### Added (2026-08, Autoware campaign)
- Autoware A2 expected-vs-counted, mechanised (`scripts/aw_a2_expected.py`,
  `scripts/aw_a2_sheet.py`): per-node class identification, ctags-ranged
  scan of creation calls incl. Autoware wrapper rules, Google Sheet
  AUTOWARE-LOGSIM-R24-NODE-A2 (r24: 140 exact + 32 in-range of 198 nodes).
- Provenance layer (`python/fish/provenance.py`, `docs/provenance.md`):
  session → binaries (`snapshot/maps_libs_{stable,shutdown}.json`) → packages
  (ament index / pluginlib / install layout) → sources (universal-ctags over
  the sources the image was built from) → includes (`-E -H` with
  configure-only `compile_commands.json`). Image side
  `scripts/install_provenance.sh` → `autoware-dev-trt-a1000-fishwait6`
  (`scripts/build_fishwait6.sh`); ROS 2 core sources as a host-side extra root
  (`scripts/install_provenance_ros.sh`, `--prov-extra`). Header → TU mapping is
  restricted to the same package.
- Tracepoints: intra-process Waitable registration hook
  (`IntraProcessManager::add_subscription` → `fish_rclcpp_waitable_init` with
  node_handle=0x0) and ABI-safe intra-process publish attribution in
  `PublisherBase::get_intra_process_subscription_count()` (`fish_rclcpp_publish_link`).
- `[trace] per_instance` mode (`ros2:rcl_publish` + `ros2:rmw_take`, LTTng CLI
  channel 4×2 MB with `/dev/shm` guard, `fishlog/discards.txt`).
- Model: `detect_joins`, `detect_state_links` (inferred sample-and-hold),
  `detect_polled_subs` (observed sample-and-hold via `rmw_take` inside a foreign
  callback window), `measure_flows` (per-instance hop latency and data age),
  `graph_store_pg` ANALYZE after save; `join_analysis.py`, `gpu_coverage_check.py`.
- Viz: FT view namespace views, filters, delivery/join/polled badges, state
  edges; `/components` Layer-B component graph (architecture-row layout).
- Wrapper: RMW guard, `[ros] cyclonedds_uri`, `snapshot/env.txt`,
  `fishlog/launch_full.log`; `scripts/autoware_runs/` (bag, planning_sim+goal,
  AWSIM e2e, AWSIM via lavapipe).
- Docs: `operating_modes.md`, `images.md`, `pipeline.md`; `tracepoints.md`
  rewritten (36 events); `installation.md` refreshed.

### Fixed
- ISSUE-004 nsys session start second-precision offset (ingest_pg).
- ISSUE-005 empty trace with default Docker shm (guard + `--shm-size=2g`).

### Changed
- Top-level repo layout: shell scripts under `scripts/`, config files
  under `config/`, example missions under `examples/` (previously
  `missions/`). Install paths and docs updated accordingly. Python
  search path in `python/fish/settings.py` keeps the old top-level
  location as a fallback.
- InfluxDB consolidation finalised: all nsys / GPU data lives in one
  shared InfluxDB 3 database called `fish`, with `session` and
  `container` tags on every point. The previous "one InfluxDB DB per
  session" pattern broke against the InfluxDB 3 Core 5-DB cap. All
  post-processing queries scope by both tags.

### Added
- `tests/` test-strategy plan (see `notes/immediate_work.txt` #15):
  three-tier pyramid (pure-logic unit, fixture contract, golden-session
  smoke) with `paper_anchors.json` regression-anchor registry.
  Implementation pending.
- Repo root now ships `README.md`, `LICENSE` (TBD-text placeholder),
  this `CHANGELOG.md`, and a `pyproject.toml` stub.

### Notes
- No data deleted: legacy InfluxDB DBs were already dropped; the two
  unreferenced Mongo DBs (`fish_20260412_195042`,
  `fish_20260420_224756_h`) stay in place until the
  `mongodump → archive` flow lands.
