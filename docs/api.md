# API Reference

Auto-generated API documentation for the FISH Python codebase. Each page
below is built directly from the source with `sphinx.ext.autodoc` and
`sphinx.ext.autosummary`, so it always reflects the current docstrings,
signatures, and class hierarchies.

## Packages

- [`fish`](api/fish/modules.rst) — runtime module that ships inside the
  profiling container. Wraps ROS 2 launch files, drives NVIDIA Nsight
  Systems / `nvidia-smi` collection, snapshots container state, and
  exposes the `fish` CLI.
- [`postprocess`](api/postprocess/modules.rst) — offline analysis
  pipeline. Ingests raw LTTng / GPU / snapshot artefacts into MongoDB
  and InfluxDB, builds the FISH graph model, and emits visualisations
  (dot/SVG/PDF, radial, staircase, matrix, JSON).

```{toctree}
:maxdepth: 2
:caption: fish (runtime)

api/fish/modules
```

```{toctree}
:maxdepth: 2
:caption: postprocess (offline)

api/postprocess/modules
```
