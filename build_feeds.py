#!/usr/bin/env python3
"""Build every published feed from a single scrape.

The status site is scraped once (scheduled + unplanned), then each feed is
derived from that one incident set by filtering -- so a run hits the site once,
not once per feed. Each feed carries its own history forward from the matching
file under ``--state-dir`` (same filename), and a shared ``--scan-state`` gap
scan (if enabled) runs once for all of them.

    python build_feeds.py --out-dir public --state-dir state
"""
import argparse
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import drac_outages_ics as M

# The published feeds. `scheduled_only` drops unplanned outages; `service` keeps
# only incidents that mention it (via matches_service). Output/merge-from
# filenames are shared: each feed merges from <state-dir>/<output>.
FEEDS = [
    {
        "output": "outages.ics",
        "calname": "DRAC Cluster Outages",
        "service": None,
        "scheduled_only": False,
    },
    {
        "output": "outages-planned-only.ics",
        "calname": "DRAC Cluster Scheduled Outages",
        "service": None,
        "scheduled_only": True,
    },
    {
        "output": "killarney.ics",
        "calname": "Killarney Cluster Outages",
        "service": "Killarney",
        "scheduled_only": False,
    },
    {
        "output": "killarney-planned-only.ics",
        "calname": "Killarney Cluster Scheduled Outages",
        "service": "Killarney",
        "scheduled_only": True,
    },
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=".", help="directory to write feeds into")
    ap.add_argument(
        "--state-dir",
        default=None,
        help="directory of previous feeds to carry history forward from "
        "(each feed merges from <state-dir>/<its filename>); omit to skip merge",
    )
    ap.add_argument("--tz", default=M.DEFAULT_TZ, help="IANA tz for naive times")
    ap.add_argument(
        "--scan-state",
        default=None,
        metavar="FILE",
        help="shared catch-up gap-scan state file (one for all feeds)",
    )
    args = ap.parse_args()

    now = datetime.now(ZoneInfo(args.tz))
    # One scrape for every feed -- always include unplanned (the superset); the
    # planned-only feeds drop them again when filtering.
    incidents, n_scheduled, n_total, new_mark = M.scrape_incidents(
        args.tz, include_unplanned=True, now=now, scan_state=args.scan_state
    )

    os.makedirs(args.out_dir, exist_ok=True)
    for feed in FEEDS:
        print(f"\n[{feed['output']}]")
        merge_from = (
            os.path.join(args.state_dir, feed["output"]) if args.state_dir else None
        )
        M.build_feed(
            incidents,
            n_scheduled if feed["scheduled_only"] else n_total,
            args.tz,
            os.path.join(args.out_dir, feed["output"]),
            calname=feed["calname"],
            service=feed["service"],
            scheduled_only=feed["scheduled_only"],
            merge_from=merge_from,
        )

    if args.scan_state and new_mark is not None:
        M.write_scan_state(args.scan_state, new_mark)
        print(f"\nScan-gap: state saved (last scanned id = {new_mark}).")


if __name__ == "__main__":
    main()
