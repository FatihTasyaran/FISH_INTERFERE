#!/usr/bin/env python3
"""audit_publisher_coverage — quantify the fish_rclcpp_publish_link gap.

For each rcl_publisher_init in a session, count how many
fish_rclcpp_publish_link events fired at runtime. Publishers that:
  - emit 0 link events       => GAP (publish path bypasses our patch entirely)
  - emit 1-4 link events     => LOW (likely negotiation-only, runtime path
                                 bypasses the patch)
  - emit many link events    => OK

This identifies which C++ publisher classes we need to instrument next.
The typical suspects are:
  - rosbag2_transport PlayerImpl publishers (Player::play_messages)
  - nvidia::isaac_ros::nitros::NitrosPublisher / type-adapter path
  - rmw_publish() at the C level (catches everything but harder to attribute)

Usage:
  python3 scripts/audit_publisher_coverage.py <session_id> [--scope __main__]
                                              [--out report.json]
                                              [--include-control]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

PG_DSN = os.environ.get(
    'FISH_PG_DSN',
    'host=localhost port=5432 dbname=fish user=fish password=fish',
)

CONTROL_TOPIC_PATTERNS = ('/parameter_events', '/rosout', '/_supported_types')


def _is_control(topic: str) -> bool:
    return any(p in topic for p in CONTROL_TOPIC_PATTERNS)


def audit(session_id: str, include_control: bool = False) -> dict:
    conn = psycopg2.connect(PG_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    cur.execute("""
        SELECT payload->>'publisher_handle' AS phandle,
               payload->>'topic_name'       AS topic,
               vpid                          AS pid,
               MIN(ts_ns)                    AS init_ts
        FROM ros2_trace
        WHERE session_id=%s AND event='ros2:rcl_publisher_init'
        GROUP BY phandle, topic, pid
    """, (session_id,))
    pubs = list(cur.fetchall())

    cur.execute("""
        SELECT payload->>'publisher_handle' AS phandle,
               COUNT(*)                              AS n_links,
               COUNT(DISTINCT payload->>'callback')  AS n_distinct_cbs,
               array_agg(DISTINCT payload->>'callback') AS cbs
        FROM ros2_trace
        WHERE session_id=%s AND event='ros2:fish_rclcpp_publish_link'
        GROUP BY phandle
    """, (session_id,))
    links_by_handle = {r['phandle']: r for r in cur.fetchall()}

    # ros2:rcl_publish is typically filtered out of FISH whitelists (high volume).
    # Use subscriber-side firing counts as a proxy for true publish activity:
    # if a topic has 4639 callback_starts on its subscriber, the publisher
    # must have published roughly that many times.
    cur.execute("""
        SELECT payload->>'topic_name' AS topic, COUNT(*) AS n_publish
        FROM ros2_trace
        WHERE session_id=%s AND event='ros2:rcl_publish'
        GROUP BY 1
    """, (session_id,))
    rcl_publish_by_topic = {r['topic']: r['n_publish'] for r in cur.fetchall()}

    # Subscriber-side fires per topic (via ros2 traces: subscription_init gives
    # cb_addr per topic; callback_start gives fire count per cb_addr).
    cur.execute("""
        WITH sub_init AS (
          -- rclcpp_subscription_init: subscription -> subscription_handle
          -- rcl_subscription_init  : subscription_handle -> topic_name
          -- rclcpp_subscription_callback_added: subscription -> callback (cb_addr)
          SELECT DISTINCT
                 ca.payload->>'callback' AS cb_addr,
                 ri.payload->>'topic_name' AS topic
          FROM ros2_trace ca
          JOIN ros2_trace si
            ON si.session_id = ca.session_id
           AND si.event = 'ros2:rclcpp_subscription_init'
           AND si.payload->>'subscription' = ca.payload->>'subscription'
          JOIN ros2_trace ri
            ON ri.session_id = si.session_id
           AND ri.event = 'ros2:rcl_subscription_init'
           AND ri.payload->>'subscription_handle' = si.payload->>'subscription_handle'
          WHERE ca.session_id=%s
            AND ca.event = 'ros2:rclcpp_subscription_callback_added'
        ),
        sub_fires AS (
          SELECT payload->>'callback' AS cb_addr, COUNT(*) AS n
          FROM ros2_trace
          WHERE session_id=%s AND event='ros2:callback_start'
          GROUP BY 1
        )
        SELECT si.topic, SUM(sf.n) AS n_sub_fires
        FROM sub_init si
        JOIN sub_fires sf USING(cb_addr)
        GROUP BY 1
    """, (session_id, session_id))
    sub_fires_by_topic = {r['topic']: r['n_sub_fires'] for r in cur.fetchall()}

    cur.execute("""
        SELECT payload->>'callback' AS cb, COUNT(*) AS n
        FROM ros2_trace
        WHERE session_id=%s AND event='ros2:callback_start'
        GROUP BY 1
    """, (session_id,))
    cb_fire = {r['cb']: r['n'] for r in cur.fetchall()}

    conn.close()

    rows = []
    counts = defaultdict(int)
    for p in pubs:
        topic = p['topic'] or ''
        if not include_control and _is_control(topic):
            continue
        link = links_by_handle.get(p['phandle'])
        n_links = link['n_links'] if link else 0
        n_cbs = link['n_distinct_cbs'] if link else 0
        cbs = list(link['cbs']) if link else []
        # Approximate true publish-rate from rcl_publish counts (a topic
        # appears once per actual rcl_publish call, regardless of patch
        # status — so a GAP topic with many rcl_publishes is the smoking
        # gun: real activity but no link event.)
        n_rcl = int(rcl_publish_by_topic.get(topic, 0))
        # Subscriber-side activity = approximate true publish volume for this topic
        n_sub = int(sub_fires_by_topic.get(topic, 0) or 0)
        # Coverage = ratio of publish_link events to true activity. If activity is
        # high but publish_link is ~0, the publisher uses a path our patch doesn't see.
        if n_sub == 0:
            # No downstream subscriber fires — either unused publisher OR
            # there is a sub but it's outside this scope; can't judge coverage
            cov = 'GAP' if n_links == 0 else 'low' if n_links < 5 else 'ok'
            cov_note = 'no_sub_activity'
        elif n_links == 0:
            cov = 'GAP'
            cov_note = f'{n_sub} sub fires, 0 link events → publish path is invisible'
        elif n_links < max(5, 0.1 * n_sub):
            cov = 'low'
            cov_note = f'{n_links} link events / {n_sub} sub fires = {n_links/n_sub:.1%} coverage'
        else:
            cov = 'ok'
            cov_note = f'{n_links} link / {n_sub} sub = {n_links/n_sub:.1%}'
        counts[cov] += 1
        rows.append({
            'topic': topic,
            'pid': p['pid'],
            'publisher_handle': p['phandle'],
            'rcl_publish_count': n_rcl,        # usually 0 (filtered out)
            'sub_fires_count': n_sub,           # proxy for true activity
            'publish_link_count': n_links,
            'distinct_publisher_cbs': n_cbs,
            'publisher_cbs': cbs,
            'coverage': cov,
            'note': cov_note,
        })
    rows.sort(key=lambda r: (r['publish_link_count'], r['topic']))
    return {
        'session_id': session_id,
        'n_publishers': len(rows),
        'coverage_counts': dict(counts),
        'rows': rows,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('session_id')
    ap.add_argument('--scope', default='__main__')   # accepted but unused (audit is session-wide)
    ap.add_argument('--out', help='write JSON to this path (default stdout)')
    ap.add_argument('--include-control', action='store_true',
                    help='do not skip /parameter_events, /rosout, _supported_types')
    args = ap.parse_args()

    report = audit(args.session_id, include_control=args.include_control)

    # Human-readable summary on stderr
    c = report['coverage_counts']
    print(f"[audit] {report['n_publishers']} non-control publishers",
          file=sys.stderr)
    print(f"[audit]   GAP={c.get('GAP', 0)}  "
          f"low={c.get('low', 0)}  "
          f"ok={c.get('ok', 0)}", file=sys.stderr)
    print(f"[audit]   (GAP = 0 publish_link events; "
          f"low = 1-4; ok = >=5)", file=sys.stderr)

    out_path = args.out
    payload = json.dumps(report, indent=2, default=str)
    if out_path:
        with open(out_path, 'w') as f:
            f.write(payload)
        print(f"[audit] wrote {out_path}", file=sys.stderr)
    else:
        print(payload)


if __name__ == '__main__':
    main()
