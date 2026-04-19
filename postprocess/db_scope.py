"""Scope-aware MongoDB database wrapper.

In the new session-per-DB layout, raw-trace collections are stored
per container with a role suffix, e.g. ``ros2_trace_aircraft`` inside
the ``fish_compose_YYYYMMDD_HHMMSS`` database. Callers that were
written against the old single-container layout use bare names like
``mongo["ros2_trace"]``.

ScopedMongo is a thin proxy: it forwards attribute and item access
to the wrapped ``pymongo.database.Database`` but rewrites a known
set of collection names to include the role suffix when a role is
set. Unknown collection names (e.g. graph_meta, graph_nodes) pass
through unchanged, which is the correct behaviour for the
session-scoped graph store.

Example:

    >>> mongo = ScopedMongo(client["fish_compose_20260419_161633"],
    ...                     role="aircraft")
    >>> mongo["ros2_trace"]       # → ros2_trace_aircraft
    >>> mongo["graph_meta"]       # → graph_meta (unchanged)
    >>> mongo.list_collection_names()  # passes through

When ``role`` is ``None`` the wrapper is a plain pass-through — used
for standalone (single-container) sessions that keep unsuffixed
collection names.
"""
from __future__ import annotations


# Collection names that get suffixed with the role when a role is set.
# Everything else (graph_meta, graph_nodes, graph_edges, graph_mutations,
# any admin collection) passes through without modification.
_ROLE_SCOPED_COLLECTIONS = frozenset({
    "ros2_trace",
    "snapshot",
    "node_info",
    "topic_info",
    "topic_hz",
    "component_list",
    "node_list",
    "fish_events",
    "process_tree",
})


class ScopedMongo:
    """Wrap a pymongo Database, auto-suffixing role-scoped collections."""

    def __init__(self, db, role: str | None = None):
        self._db = db
        self._role = role

    # --- read-only properties ------------------------------------------

    @property
    def role(self) -> str | None:
        return self._role

    @property
    def name(self) -> str:
        return self._db.name

    @property
    def raw(self):
        """The underlying pymongo Database, for direct access when needed."""
        return self._db

    # --- collection access ---------------------------------------------

    def collection_name(self, base: str) -> str:
        """Return the actual collection name `base` resolves to for this scope."""
        if self._role and base in _ROLE_SCOPED_COLLECTIONS:
            return f"{base}_{self._role}"
        return base

    def __getitem__(self, name: str):
        return self._db[self.collection_name(name)]

    def create_collection(self, name: str, *args, **kwargs):
        """Wrap Database.create_collection to honour the role suffix."""
        return self._db.create_collection(
            self.collection_name(name), *args, **kwargs)

    def __getattr__(self, attr):
        """Forward everything else (e.g. list_collection_names, command)
        to the underlying Database."""
        return getattr(self._db, attr)

def is_scoped(name: str) -> bool:
    """True if ``name`` is a collection that gets role-suffixed."""
    return name in _ROLE_SCOPED_COLLECTIONS
