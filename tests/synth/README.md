# tests/synth — synthetic task-chain generator + validation loop
Design: notes/synth_generator_design_2026-10-25.md (agreed with Fatih 2026-10-25). Programme: notes/big_task.txt (task 3).

Layout
  descriptor_schema.json   the ground-truth descriptor (logical callback graph + deployment); the oracle FISH is checked against
  synth_gen.py             graph families + code/data pattern assignment -> descriptor.yaml (seeded, reproducible)
  synth_emit.py            descriptor -> rclcpp (primary) / rclpy packages (jinja2 templates in templates/)
  lib/synth_lineage/       tiny LTTng-UST provider `synth:instance(cb, inst, n_in, in[], n_out, out[])` — the instance-level oracle
  lib/patterns/            C++ data-pattern + compute + GPU primitives shared by generated nodes
  run_synth.sh             generate -> build (colcon, ccache) -> run under FISH -> ingest -> model -> extract -> verify
  verify_synth.py          FISH extraction vs descriptor + lineage trace: structure P/R per mechanism, instance pairs, joins, drops, ages
  sweep_runner.py          seeds x families x sizes x deployments; results into PG (synth_runs) + report
Generated workspaces live OUTSIDE the repo: ~/fish_synth/<seed>/ (reproducible from the descriptor).
  reanalyze_synth.sh       static class analysis of the BUILT workspace -> map onto an existing session -> model + extractor with
                           FISH_SOURCE_FLOW -> verify; no new run (used for the night-of-2026-10-25 re-analyses)

Verifier categories (verify_synth.py, 2026-10-26)
  trigger   exact / found-not-exact / undersampled / inactive / MISSING
  join      observed / candidate-listed / undersampled / inactive / MISSING
  sampled   observed / found-inferred / candidate-listed / undersampled / inactive / MISSING
  undersampled = fewer than 5 oracle instances (synth:instance) inside the extractor's window on either side: the extractor's own
                 threshold, FISH cannot have >= 5 pairs; inactive = the consumer's deposit never happened (oracle deposit id count 0 and
                 no FISH vertex): nothing arrived (middleware / load), not a FISH miss; candidate-listed = FISH lists the edge as a
                 candidate (source-only, or pointer evidence without a retaining source deposit) — the programme's goal is observed OR
                 listed. VERDICT FAIL only on MISSING, found-not-exact or a false positive; "PASS (with ...)" names the rest.
  FISH vertices are matched per rcl subscription handle ('sub:/t#k' for a node subscribing twice to one topic; the descriptor callback is
  picked by the topics it publishes); timers by the lambda symbol {lambda()#k} (same-period timers of one node), else by period.
  A map_keyed_timeout join completed by its 250 ms timeout timer is satisfied by FISH's observed SAMPLED dependency deposit sub -> timer.

Emitter notes (synth_emit.py)
  every timer is created with a lambda (distinct registered symbol per timer); timer readers stamp outputs with now() — every other
  field_overwrite reader propagates its first deposit's header (the republish-latest identity flow); sources stay on the 100 ms stamp
  grid (ExactTime joins need equal stamps). The container shim (fishwait27) is generated from Autoware's PLT symbols and does not fire
  for the synthetic package's own template instantiations: deque copies can only be listed via the static source.

Scale runs (er50 / layered100 / er200 / ba500, 2026-10-25 night): starved — 600 kB 'pointcloud' messages at 20-100 Hz over DDS to 7
  processes (~1.4 GB/s from the timer sources alone), FISH_CDDS_XML=0 (Cyclone defaults, 14.7 kB MaxMessageSize), 6-P-core app set:
  10 ms timers at 2 Hz, 71 of 224 er200 trigger edges never delivered a message. For a real scale round: realistic traffic in the
  generator (pointclouds <= 10 Hz and intra-process, per-process bandwidth cap), FISH_CDDS_XML=1, 180 s.

### Night 2026-10-26 (scale re-analyses): what layered100 found
- `trigger MISSING` for a node-internal pub→sub hop whose topic has no other subscriber (pure intra-process: no DDS publish, no
  rmw_take, only `fish_rclcpp_intra_publish` / `_take`): the model's join-tie `add_edge` on the same (F, F) pair overwrote the
  propagated message hop (`nx.DiGraph`, one edge per ordered pair). Fixed in the model (`_l3_keep_hop`): ties become attributes of the hop.
- `trigger found-not-exact` (`mixed:message_address+order`) for intra hops into a starved join callback: the IPM ring buffer stays full,
  messages wait seconds, and the extractor's fixed 2 s staleness bound rejected the correct address identities. Replaced by the
  ring-buffer feasibility test (newer publishes between publish and take <= depth-1). Per-message records are now loaded from the
  data-phase start, not from a 1-2 s margin before the window (a message published before the margin and taken inside the window lost
  its identity).
- Verifier semantics reminder: `found-not-exact` fails the verdict — an edge with instance pairs but a non-exact provenance is a FISH
  defect (or a missing identity record), never acceptable for a trigger edge.
