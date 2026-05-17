# Changelog

Notable changes per release. Pre-1.0 — interfaces may break between
minor versions.

## Unreleased

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
