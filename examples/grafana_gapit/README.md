# Embedding fish_viz into a Grafana panel via gapit-htmlgraphics-panel

This directory is a port of `postprocess/fish_viz.html` (D3 force/tree/stair
layouts) to Grafana's gapit-htmlgraphics-panel. Reads the FISH model graph
from the PG `graph_nodes` + `graph_edges` tables (per-session, scope-keyed)
and renders the same interactive graph explorer inside a dashboard panel.

## Setup steps

1. **Add a dashboard variable `session_id`** (Dashboard → Settings → Variables → New variable):
   - Type: Query
   - Datasource: your PostgreSQL datasource
   - Query: `SELECT session_id FROM sessions ORDER BY inserted_at DESC`

2. **Add a dashboard variable `scope`** (optional, default `__main__`):
   - Type: Custom
   - Values: `__main__,__composed__`

3. **Add new panel → HTML Graphics**.

4. **Add two queries** (PostgreSQL datasource):

   **Query A** (refId = `A`) — nodes:
   ```sql
   SELECT
     node_id   AS id,
     type, label, level,
     children::text   AS children,
     pid, full_name, etype, ptype, cb_addr, external,
     attrs::text      AS attrs
   FROM graph_nodes
   WHERE session_id = '$session_id' AND scope = '$scope'
   ORDER BY node_id;
   ```

   **Query B** (refId = `B`) — edges:
   ```sql
   SELECT
     source, target, rel, level,
     attrs::text AS attrs
   FROM graph_edges
   WHERE session_id = '$session_id' AND scope = '$scope';
   ```

   In both queries Format must be **Table** (not Time series — this is a static graph).

5. **Paste the three sections into gapit's editors**:
   - **HTML** editor: contents of `panel.html`
   - **CSS** editor: contents of `panel.css`
   - **JavaScript** editor: contents of `panel.js`

6. **Save** the panel. Switch session_id from the dropdown — the graph
   re-renders automatically.

## What changed vs the standalone fish_viz.html

- `fetch('fish_graph.json')` → `buildRaw(data.series)` from Grafana queries
- `100vw / 100vh` → `100% / 100%` (panel sandbox is smaller than viewport)
- `position: fixed` → `position: absolute` on the controls + info + stats boxes,
  scoped to the panel root (which is set to `position: relative`)
- All DOM selectors scoped via `htmlNode.querySelector(...)` instead of
  `document.getElementById(...)` so multiple panels can coexist in one
  dashboard without ID collisions

## Troubleshooting

- **"No data" but query A/B run fine** → ensure Format = Table on both
  queries (not Time series). The JS expects rows in Frame.fields columnar
  shape.
- **Graph renders but external nodes missing** → toggle the *External*
  checkbox in the panel controls; default off.
- **Multiple panels on same dashboard show ghost interactions** → already
  handled by `htmlNode` scoping; if it persists, give each panel a unique
  HTML id at the wrapper div.
