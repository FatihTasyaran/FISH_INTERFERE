
## §5 Roadmap — to write (2026-09-09)
* **Multiple hosts / multiple containerised applications.** The host layer exists
  so the model extends to them naturally: today one host holds the whole
  deployment; nothing in the hierarchy assumes there is only one. This is the
  justification §3 deliberately does NOT give (it only names the layer).
* ARM-based SoC devices (LTTng, ROS 2 and nsys all available on arm) — labour,
  not research.
* Lighter GPU profiling via CUPTI injection instead of nsys; also the path to
  AMD (roc) and ARM Mali.
* Correctness condition (chi, C1/C2) — dropped from §3; mention here if wanted.

## §5 Roadmap — verifying the one assumption (2026-09-10)
`state-inferred` is the model's **only assumption and only over-approximation**.
Everything else is observed: `msg`/`dep` from registration + link events,
`state-polled` from an `rmw_take` inside the reader's callback window, `join`
from which input completed the set.

* **What is assumed.** Inside a node, every deposit-only subscription callback
  (fired, publishes nothing, not a join member) is linked to every callback that
  publishes a data topic. The in-memory read crosses no ROS layer, so no event
  exists; the age is taken from the two callbacks' start times. The link is
  cartesian within those filters, so a publisher that reads only one of several
  deposited values still gets an edge from each — an over-approximation.
* **Why it exists at all.** Without it the graph breaks exactly at the
  timer-driven / latest-value node, which is the common Autoware shape; the
  end-to-end chain the deadline is defined on would be severed there. FISH keeps
  the trigger chain honest (a `state` edge is not precedence, FT grouping
  ignores it) while preserving the data dependency and its age.
  CARET reports the same pattern as a limitation it cannot handle:
  "Some Autoware.Universe nodes received messages, stored them in buffers, and
  then published the messages. In this case, the node latency could not be
  calculated using the callback chain method."
* **How to verify.** uprobe on the member accesses —
  `notes/plan_uprobe_join_verification.txt`. That would turn each inferred link
  into an observed one, or remove it.
