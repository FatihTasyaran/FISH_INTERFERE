#!/usr/bin/env python3
"""Extract a real-time taskset description from a FISH session.

For every callback (L3 F node) we attribute:
  - period_ns   the task's period
  - period_source  how it was derived:
        * 'tmr-nominal'         explicit period from rcl_timer_init, fired >= 2x
        * 'tmr-oneshot'         explicit period from rcl_timer_init, but only
                                fired < 2 times (timer->cancel()ed in cb, e.g.
                                NITROS startNitrosNode) — EXCLUDED from H_tmr/anchor
        * 'sub'                 subscription callback — no per-cb period;
                                characterized via fires/H_tmr & fires/H_session
        * 'serv-aperiodic'      service request-driven; no period
        * 'tmr-unknown'         timer entity but no period found
        * 'unknown'             no period derivable
  - exec_time_ns  {mean, p50, p99, max} measured from callback_start/_end pairs
  - n_executions  number of trace samples

Per executor (L0 EX = pid):
  - hyperperiod_ns = LCM of all task periods in that executor (only those with
    a well-defined period — aperiodic/unknown skipped)

Session-wide:
  - LCM of executor hyperperiods (equals LCM of all task periods)

Output: JSON to stdout (or --out path).

Usage:
  python3 postprocess/extract_taskset.py <session_id> [--scope __main__] [--out taskset.json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Optional

import psycopg2
import psycopg2.extras

PG_DSN = os.environ.get(
    "FISH_PG_DSN",
    "host=localhost port=5432 dbname=fish user=fish password=fish",
)


# ---- helpers ----------------------------------------------------------------


def _lcm_ns(periods: list[int]) -> Optional[int]:
    """LCM of a list of nanosecond periods. Returns None if empty.
    Practical safety cap at 1 hour (3.6e12 ns) — anything larger probably
    means coprime periods and indicates a real-time design smell."""
    if not periods:
        return None
    out = periods[0]
    for p in periods[1:]:
        out = math.lcm(out, p)
        if out > 3_600_000_000_000:  # 1 hour
            return out
    return out


_TIMER_LABEL_RE = re.compile(r"timer[_]?(\d+)(?:ns)?", re.IGNORECASE)


def _period_from_timer_label(label: str) -> Optional[int]:
    """Many timer entities have label of form 'timer_500000000ns' — fallback
    when we don't have rcl_timer_init.period_ns directly."""
    if not label:
        return None
    m = _TIMER_LABEL_RE.search(label)
    return int(m.group(1)) if m else None


def _round_to_nice(period_ns: int) -> int:
    """Snap empirical period to a 'nice' value to reduce hyperperiod blowups
    caused by mid-arrival jitter. Scheduling-relevant resolution shrinks as
    the period grows; 2-3% jitter on a 200ms period (==> 196-204ms range) is
    harmless for schedulability analysis but devastating for LCM.

        ≥ 500 ms : nearest 50 ms
        ≥ 50  ms : nearest 10 ms
        ≥ 1   ms : nearest 1 ms
        ≥ 1   µs : nearest 1 µs
        else      : nearest 100 ns
    """
    if period_ns >= 500_000_000:
        return round(period_ns / 50_000_000) * 50_000_000
    if period_ns >= 50_000_000:
        return round(period_ns / 10_000_000) * 10_000_000
    if period_ns >= 1_000_000:
        return round(period_ns / 1_000_000) * 1_000_000
    if period_ns >= 1_000:
        return round(period_ns / 1_000) * 1_000
    return round(period_ns / 100) * 100


# ---- main pipeline ----------------------------------------------------------


def fetch_callbacks(cur, session_id: str, scope: str) -> list[dict]:
    """Return one row per F callback with attached entity + node + executor metadata."""
    cur.execute("""
        SELECT
          f.node_id AS f_id, f.cb_addr, f.label AS cb_symbol,
          e.node_id AS e_id, e.etype, e.label AS entity_label, e.attrs AS e_attrs,
          n.label AS node_label,
          ex.label AS ex_label, ex.pid AS ex_pid, ex.attrs AS ex_attrs
        FROM graph_nodes f
        JOIN graph_edges ef ON ef.session_id=f.session_id AND ef.scope=f.scope
                           AND ef.target=f.node_id AND ef.rel='contains'
        JOIN graph_nodes e ON e.session_id=ef.session_id AND e.scope=ef.scope
                          AND e.node_id=ef.source AND e.type='E'
        LEFT JOIN graph_edges en ON en.session_id=e.session_id AND en.scope=e.scope
                                AND en.target=e.node_id AND en.rel='contains'
        LEFT JOIN graph_nodes n  ON n.session_id=en.session_id AND n.scope=en.scope
                                AND n.node_id=en.source AND n.type='N'
        LEFT JOIN graph_edges enex ON enex.session_id=n.session_id AND enex.scope=n.scope
                                  AND enex.target=n.node_id AND enex.rel='contains'
        LEFT JOIN graph_nodes ex   ON ex.session_id=enex.session_id AND ex.scope=enex.scope
                                  AND ex.node_id=enex.source AND ex.type='EX'
        WHERE f.session_id=%s AND f.scope=%s AND f.type='F'
          AND f.cb_addr IS NOT NULL AND f.cb_addr <> 'NA'
    """, (session_id, scope))
    return [dict(r) for r in cur.fetchall()]


def fetch_timer_init_periods(cur, session_id: str) -> dict[str, int]:
    """rclcpp_timer_init / rcl_timer_init events → timer_handle → period_ns."""
    cur.execute("""
        SELECT payload->>'timer_handle' AS th, payload->>'period' AS p
        FROM ros2_trace
        WHERE session_id=%s
          AND event IN (
            'ros2:rcl_timer_init',
            'ros2:fish_rclcpp_timer_init',
            'ros2:fish_rclpy_timer_init'
          )
          AND payload ? 'timer_handle' AND payload ? 'period'
    """, (session_id,))
    out: dict[str, int] = {}
    for r in cur.fetchall():
        th, p = r["th"], r["p"]
        if th and p:
            try:
                out[th] = int(p)
            except ValueError:
                pass
    return out


def fetch_callback_execs(cur, session_id: str) -> dict[str, list[tuple[int, int]]]:
    """cb_addr → list of (t_start_ns, t_end_ns) execution pairs from ros2_trace."""
    cur.execute("""
        WITH ev AS (
          SELECT vtid, ts_ns, event, payload->>'callback' AS cb
          FROM ros2_trace
          WHERE session_id=%s
            AND event IN ('ros2:callback_start','ros2:callback_end')
        ), paired AS (
          SELECT cb, vtid, ts_ns AS t0,
                 lead(ts_ns) OVER (PARTITION BY vtid ORDER BY ts_ns) AS t1,
                 lead(event)  OVER (PARTITION BY vtid ORDER BY ts_ns) AS next_event,
                 event
          FROM ev
        )
        SELECT cb, t0, t1
        FROM paired
        WHERE event='ros2:callback_start' AND next_event='ros2:callback_end'
          AND t1 IS NOT NULL
        ORDER BY cb, t0
    """, (session_id,))
    out: dict[str, list[tuple[int, int]]] = {}
    for r in cur.fetchall():
        out.setdefault(r["cb"], []).append((r["t0"], r["t1"]))
    return out


def derive_period(cb: dict, timer_period_by_handle: dict[str, int],
                  execs_for: dict[str, list[tuple[int, int]]]) -> tuple[Optional[int], str]:
    """Return (period_ns, period_source) for one callback row."""
    etype = cb["etype"]
    label = cb["entity_label"]
    cb_addr = cb["cb_addr"]
    e_attrs = cb.get("e_attrs") or {}

    # 1) Timer entity → nominal period
    if etype == "tmr":
        # Try entity attrs (some have aspects→period); fallback to label parse
        p = _period_from_timer_label(label)
        # Also try timer_handle in attrs.aspects[*].timer_handle
        if not p:
            for asp in (e_attrs.get("aspects") or []):
                th = asp.get("timer_handle")
                if th and th in timer_period_by_handle:
                    p = timer_period_by_handle[th]
                    break
        if not p:
            return (None, "tmr-unknown")
        # One-shot timers (e.g. NITROS startNitrosNode) call timer->cancel()
        # inside their first callback. They still appear as rcl_timer_init with
        # a nominal period, but they fire only once — keep the period for
        # display, downgrade source so they're excluded from LCM/anchor.
        n_fires = len(execs_for.get(cb_addr, []))
        if n_fires < 2:
            return (p, "tmr-oneshot")
        return (p, "tmr-nominal")

    # 2) Service → aperiodic
    if etype == "serv":
        return (None, "serv-aperiodic")

    # 3) Sub: no per-callback period. Subs are characterized by their fire
    #    count per executor H_tmr / session H_session window (computed
    #    downstream by /api/cb-stats), not by an inferred period of their own.
    if etype == "sub":
        return (None, "sub")

    return (None, "unknown")


def _exec_time_stats(execs: list[tuple[int, int]]) -> dict:
    durs = sorted(t1 - t0 for t0, t1 in execs)
    n = len(durs)
    if n == 0:
        return {"mean_ns": 0, "p50_ns": 0, "p99_ns": 0, "max_ns": 0, "n": 0}
    return {
        "mean_ns": sum(durs) // n,
        "p50_ns": durs[n // 2],
        "p99_ns": durs[min(n - 1, int(0.99 * n))],
        "max_ns": durs[-1],
        "n": n,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id")
    ap.add_argument("--scope", default="__main__")
    ap.add_argument("--out", help="write JSON to this file (default stdout)")
    args = ap.parse_args()

    conn = psycopg2.connect(PG_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    print(f"[taskset] fetching graph for {args.session_id}", file=sys.stderr)
    cbs = fetch_callbacks(cur, args.session_id, args.scope)
    print(f"[taskset]   {len(cbs)} callbacks", file=sys.stderr)

    timer_handles = fetch_timer_init_periods(cur, args.session_id)
    print(f"[taskset]   {len(timer_handles)} timer_init period records", file=sys.stderr)

    execs_for = fetch_callback_execs(cur, args.session_id)
    print(f"[taskset]   {sum(len(v) for v in execs_for.values())} execution pairs "
          f"for {len(execs_for)} cb_addrs", file=sys.stderr)

    # Build per-executor task lists.
    per_ex: dict[str, dict] = {}
    period_source_counts: dict[str, int] = {}
    for cb in cbs:
        period_ns, source = derive_period(cb, timer_handles, execs_for)
        period_source_counts[source] = period_source_counts.get(source, 0) + 1
        ex_key = str(cb["ex_pid"]) if cb["ex_pid"] else "unknown"
        ex = per_ex.setdefault(ex_key, {
            "label": cb["ex_label"] or f"ex_pid_{ex_key}",
            "pid": cb["ex_pid"],
            "tasks": [],
        })
        execs = execs_for.get(cb["cb_addr"], [])
        ex["tasks"].append({
            "cb_addr": cb["cb_addr"],
            "node": cb["node_label"],
            "entity": f"{cb['etype']}:{cb['entity_label']}",
            "etype": cb["etype"],
            "period_ns": period_ns,
            "period_source": source,
            "exec_time": _exec_time_stats(execs),
            "symbol": cb["cb_symbol"],
        })

    # Hyperperiod = LCM of tmr-nominal periods only. Subs no longer claim a
    # period of their own; they're characterized by fires/H_tmr & fires/H_session
    # downstream (see /api/cb-stats).
    def _participates(task: dict) -> bool:
        return task["period_source"] == "tmr-nominal" and bool(task["period_ns"])

    for ex_key, ex in per_ex.items():
        for t in ex["tasks"]:
            t["in_hyperperiod"] = _participates(t)
        ps_tmr = [t["period_ns"] for t in ex["tasks"] if t["in_hyperperiod"]]
        ex["hyperperiod_tmr_only_ns"] = _lcm_ns(ps_tmr)
        ex["hyperperiod_ns"] = ex["hyperperiod_tmr_only_ns"]   # alias, kept for back-compat
        ex["n_tmr_tasks"] = len(ps_tmr)
        ex["n_periodic_tasks"] = ex["n_tmr_tasks"]
        ex["n_aperiodic_tasks"] = sum(1 for t in ex["tasks"] if not t["period_ns"])
        ex["n_excluded_sparse"] = 0

    session_periods_tmr = [t["period_ns"] for ex in per_ex.values()
                                            for t in ex["tasks"]
                                            if t["in_hyperperiod"]]
    session_hp_tmr = _lcm_ns(session_periods_tmr)
    session_hp = session_hp_tmr   # alias, kept for back-compat

    out = {
        "session_id": args.session_id,
        "scope": args.scope,
        "session_hyperperiod_ns": session_hp,
        "session_hyperperiod_tmr_only_ns": session_hp_tmr,
        "n_callbacks": len(cbs),
        "period_source_counts": period_source_counts,
        "executors": per_ex,
    }

    conn.close()

    text = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"[taskset] wrote {args.out} ({len(text)} bytes)", file=sys.stderr)
    else:
        print(text)

    def _fmt(h):
        if h is None: return "—"
        if h < 1e9:   return f"{h/1e6:.1f} ms"
        if h < 1e12:  return f"{h/1e9:.2f} s"
        return f"{h} ns"

    # Stderr summary
    print(f"\n[taskset] === SUMMARY ===", file=sys.stderr)
    print(f"[taskset] session hyperperiod         (all participating): {_fmt(session_hp)}", file=sys.stderr)
    print(f"[taskset] session hyperperiod (tmr-only / strict periodic): {_fmt(session_hp_tmr)}", file=sys.stderr)
    for src, n in sorted(period_source_counts.items()):
        print(f"[taskset]   {src}: {n}", file=sys.stderr)
    print(f"[taskset] per-EX hyperperiods:", file=sys.stderr)
    for ex_key, ex in sorted(per_ex.items()):
        h = ex["hyperperiod_ns"]
        h_str = (f"{h/1e6:.1f} ms" if h and h < 1e10 else (f"{h} ns" if h else "—"))
        excl = ex.get("n_excluded_sparse", 0)
        excl_str = f", {excl} sparse-excluded" if excl else ""
        h_tmr = ex.get("hyperperiod_tmr_only_ns")
        print(f"[taskset]   {ex['label']:30s} pid={ex_key:>6s}  "
              f"H={_fmt(h)}  H_tmr={_fmt(h_tmr)}  "
              f"({ex['n_periodic_tasks']} periodic, "
              f"{ex['n_tmr_tasks']} tmr, "
              f"{ex['n_aperiodic_tasks']} aperiodic{excl_str})",
              file=sys.stderr)


if __name__ == "__main__":
    main()
