"""PostgreSQL + TimescaleDB connection + bulk-insert helpers for FISH.

Single source of truth for:
  - DSN / connection config (overridable via FISH_PG_DSN env var)
  - Connection pool used by ingest_pg.py + model_improved_pg.py
  - COPY-from-iterator bulk inserter (fast bulk path)
  - register_session() / register_host() idempotent upserts
  - Small SQL helpers (fetch_all, fetch_one, exec)
"""
from __future__ import annotations

import io
import json
import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool


PG_DSN = os.environ.get(
    "FISH_PG_DSN",
    "host=localhost port=5432 dbname=fish user=fish password=fish",
)

_pool_lock = threading.Lock()
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def init_pool(minconn: int = 2, maxconn: int = 24, dsn: str | None = None):
    """Initialize the shared pool. Idempotent — safe to call multiple times."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn, maxconn, dsn or PG_DSN
            )
    return _pool


def close_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def get_conn():
    """Yield a connection borrowed from the pool. Returns it on exit."""
    pool = init_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(autocommit: bool = False):
    """Convenience: yield (conn, cur). autocommit=False uses a transaction."""
    with get_conn() as conn:
        conn.autocommit = autocommit
        cur = conn.cursor()
        try:
            yield conn, cur
        finally:
            cur.close()


# ----------------------------------------------------------------------------
# COPY bulk-insert path
# ----------------------------------------------------------------------------

def _to_pg_literal(v):
    """Format a Python value for the COPY text protocol.

    COPY uses tab-separated columns with \\N for NULL. We escape backslash,
    tab, newline, and carriage return.
    """
    if v is None:
        return r"\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        s = json.dumps(v, default=str, separators=(",", ":"))
    else:
        s = str(v)
    return (s.replace("\\", "\\\\")
              .replace("\t", "\\t")
              .replace("\n", "\\n")
              .replace("\r", "\\r"))


def copy_rows(cur, table: str, columns: list[str], rows: list[tuple]) -> int:
    """COPY a batch of rows into `table`. Returns the count written.

    `rows` may be a generator. Empty input → 0 rows, no work done.
    """
    if not rows:
        return 0
    buf = io.StringIO()
    n = 0
    for row in rows:
        buf.write("\t".join(_to_pg_literal(v) for v in row))
        buf.write("\n")
        n += 1
    if n == 0:
        return 0
    buf.seek(0)
    col_list = ", ".join(f'"{c}"' for c in columns)
    cur.copy_expert(f"COPY {table} ({col_list}) FROM STDIN", buf)
    return n


def copy_many(table: str, columns: list[str], rows, batch_size: int = 50_000) -> int:
    """COPY rows in batches using its own connection. Useful for parallel
    workers — each worker calls this independently.
    """
    total = 0
    buf_rows: list[tuple] = []
    with get_conn() as conn:
        conn.autocommit = False
        cur = conn.cursor()
        try:
            for row in rows:
                buf_rows.append(row)
                if len(buf_rows) >= batch_size:
                    total += copy_rows(cur, table, columns, buf_rows)
                    buf_rows.clear()
            if buf_rows:
                total += copy_rows(cur, table, columns, buf_rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return total


# ----------------------------------------------------------------------------
# Convenience query helpers
# ----------------------------------------------------------------------------

def execute(sql: str, params: tuple | None = None):
    with get_cursor(autocommit=True) as (_, cur):
        cur.execute(sql, params or ())


def fetch_all(sql: str, params: tuple | None = None) -> list[psycopg2.extras.DictRow]:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return cur.fetchall()


def fetch_one(sql: str, params: tuple | None = None):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return cur.fetchone()


def iter_rows(sql: str, params: tuple | None = None, batch_size: int = 10_000):
    """Server-side cursor iteration — for large result sets."""
    with get_conn() as conn:
        cur = conn.cursor(name=f"fish_cur_{os.getpid()}_{threading.get_ident()}",
                          cursor_factory=psycopg2.extras.RealDictCursor)
        cur.itersize = batch_size
        cur.execute(sql, params or ())
        for row in cur:
            yield row
        cur.close()


# ----------------------------------------------------------------------------
# Registry upserts
# ----------------------------------------------------------------------------

def register_host(host_name: str, **fields):
    """Idempotent UPSERT into hosts. Updates last_seen + any provided fields."""
    cols = ["host_name"] + list(fields.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    set_clause = ", ".join(
        [f"{c} = COALESCE(EXCLUDED.{c}, hosts.{c})" for c in cols if c != "host_name"]
    ) or "last_seen = now()"
    sql = (
        f"INSERT INTO hosts ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (host_name) DO UPDATE SET {set_clause}, last_seen = now()"
    )
    execute(sql, tuple([host_name] + list(fields.values())))


def register_session(session_id: str, **fields):
    """Idempotent UPSERT into sessions. Use Python None for missing values."""
    cols = ["session_id"] + list(fields.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    set_clause = ", ".join(
        [f"{c} = COALESCE(EXCLUDED.{c}, sessions.{c})" for c in cols if c != "session_id"]
    )
    if not set_clause:
        set_clause = "inserted_at = sessions.inserted_at"
    sql = (
        f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (session_id) DO UPDATE SET {set_clause}"
    )
    vals = []
    for k in cols:
        v = session_id if k == "session_id" else fields.get(k)
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        vals.append(v)
    execute(sql, tuple(vals))


def session_exists(session_id: str) -> bool:
    row = fetch_one("SELECT 1 FROM sessions WHERE session_id = %s", (session_id,))
    return row is not None


def delete_session(session_id: str):
    """Drop a session and all its dependent data (ON DELETE CASCADE handles
    the rest). Useful for re-ingesting from scratch.
    """
    execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))


if __name__ == "__main__":
    # Smoke test
    init_pool()
    row = fetch_one("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    print(f"timescaledb: {row['extversion'] if row else 'NOT INSTALLED'}")
    rows = fetch_all("SELECT count(*) AS n FROM sessions")
    print(f"sessions: {rows[0]['n']}")
    close_pool()
