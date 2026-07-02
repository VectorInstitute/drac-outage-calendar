#!/usr/bin/env python3
"""One-off backfill: crawl historic DRAC incident pages 252..1644.

Fetches each view_incident page once (cached to CACHE_DIR), then classifies,
parses dates, dedups, and writes historic.ics. Re-running only re-parses the
cache -- no re-fetching -- so we can iterate on the parse/dedup logic freely.
"""
import argparse
import os
import re
import sys
import time

import requests

import drac_outages_ics as M

# Where fetched incident pages are cached (one .html per id). Overridable with
# --cache-dir; the crawl only hits the network for ids not already cached.
CACHE_DIR = os.environ.get("BACKFILL_CACHE", "historic_cache")
LO, HI = 252, 1644
HEADERS = {"User-Agent": "drac-outage-calendar/1.0 (personal use)"}
DELAY = 0.5

SCHED_MARKERS = re.compile(r"planned|planifi|maintenance|scheduled", re.I)


def cache_path(n):
    return os.path.join(CACHE_DIR, f"{n}.html")


def get_html(n, fetch=True):
    """Return cached HTML for incident n, fetching + caching if missing."""
    p = cache_path(n)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    if not fetch:
        return None
    url = f"https://status.alliancecan.ca/view_incident?incident={n}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    with open(p, "w", encoding="utf-8") as f:
        f.write(r.text)
    time.sleep(DELAY)
    return r.text


def is_incident(html):
    """Return True for a real incident page (vs the empty-id home page)."""
    return "Incident description" in html


def is_scheduled(html):
    """Planned-maintenance page (unplanned incidents carry no such marker)."""
    return bool(SCHED_MARKERS.search(html))


def crawl():
    os.makedirs(CACHE_DIR, exist_ok=True)
    done = 0
    for n in range(LO, HI + 1):
        if os.path.exists(cache_path(n)):
            continue
        get_html(n)
        done += 1
        if done % 50 == 0:
            print(f"  fetched {done} new pages (at id {n})", flush=True)
    print(f"crawl complete: {done} newly fetched, cache in {CACHE_DIR}", flush=True)


def analyse(include_unplanned=False):
    rows = []  # dated incidents (scheduled, plus unplanned when requested)
    n_empty = n_incident = n_sched = n_sched_dated = n_unplanned = n_parse_err = 0
    n_unplanned_dated = 0
    for n in range(LO, HI + 1):
        html = get_html(n, fetch=False)
        if html is None:
            print(f"  MISSING cache for {n}", file=sys.stderr)
            continue
        if not is_incident(html):
            n_empty += 1
            continue
        n_incident += 1
        url = f"https://status.alliancecan.ca/view_incident?incident={n}"
        try:
            # parse_incident anchors the year to the incident's created date,
            # so undated-year historic events (2019..) don't collapse onto now.
            inc = M.parse_incident(html, url)
        except Exception as e:
            n_parse_err += 1
            print(f"  PARSE ERROR id {n}: {e}", file=sys.stderr)
            continue
        inc["id"] = n

        if not is_scheduled(html):
            n_unplanned += 1
            if not include_unplanned:
                continue
            inc["kind"] = "unplanned"
            # Unplanned outages rarely carry a parseable date -- fall back to
            # the incident's timestamps (began when created, ended at last update).
            if inc["start"] is None:
                inc["start"] = inc["created"]
            if (
                inc["start"] is not None
                and inc["end"] is None
                and inc["updated"] is not None
                and inc["updated"] > inc["start"]
            ):
                inc["end"] = inc["updated"]
            if inc["start"] is not None:
                n_unplanned_dated += 1
        else:
            n_sched += 1
            inc["kind"] = "scheduled"
            if inc["start"] is not None:
                n_sched_dated += 1

        if inc["start"] is None:
            continue
        rows.append(inc)

    print("\n=== crawl summary ===")
    print(f"  ids scanned:            {HI - LO + 1}")
    print(f"  empty / no event:       {n_empty}")
    print(f"  real incidents:         {n_incident}")
    print(f"    parse errors:         {n_parse_err}")
    tag = "included" if include_unplanned else "excluded"
    print(f"    unplanned ({tag}):  {n_unplanned}")
    if include_unplanned:
        print(f"      unplanned + dated:  {n_unplanned_dated}")
    print(f"    scheduled:            {n_sched}")
    print(f"      scheduled + dated:  {n_sched_dated}")
    print(f"      scheduled undated:  {n_sched - n_sched_dated}")
    return rows


def _overlap(a, b, tz):
    """Return True if two incidents' [start,end) ranges overlap on one service."""
    if a["service"].lower() != b["service"].lower():
        return False
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tz)
    as_, ae = M._as_dt(a["start"], z), M._as_dt(a["end"] or a["start"], z)
    bs, be = M._as_dt(b["start"], z), M._as_dt(b["end"] or b["start"], z)
    return as_ < be and bs < ae


def _daykey(inc):
    """Group key: service + start-day + end-day (day granularity)."""
    s = inc["start"]
    e = inc["end"] or inc["start"]
    return (inc["service"].lower(), s.date(), e.date())


def dedup_report(rows, tz="America/Toronto"):
    """Group by service + identical start/end day; report multi-member groups.

    A tighter key than raw date-overlap: real edit-duplicates re-announce the
    same window, whereas distinct events that merely overlap (a short outage
    inside a long maintenance window, or unrelated events under the generic
    'DRAC' service) keep different days and stay separate.
    """
    from collections import OrderedDict

    buckets = OrderedDict()
    for inc in sorted(rows, key=lambda r: r["id"]):
        buckets.setdefault(_daykey(inc), []).append(inc)
    groups = list(buckets.values())

    dupes = [g for g in groups if len(g) > 1]
    print("\n=== dedup analysis (service + same start/end day, keep latest id) ===")
    print(f"  {len(rows)} scheduled+dated events -> {len(groups)} unique after dedup")
    print(f"  {len(dupes)} group(s) with >1 member:")
    for g in dupes:
        keep = max(g, key=lambda r: r["id"])
        print(f"\n  [{g[0]['service']}] group of {len(g)} -> keep id {keep['id']}")
        for m in sorted(g, key=lambda r: r["id"]):
            mark = "KEEP" if m is keep else "drop"
            print(
                f"    {mark} {m['id']}: {m['start']} -> {m['end']}  {m['title'][:50]!r}"
            )
    # deduped set = latest id from each group
    return [max(g, key=lambda r: r["id"]) for g in groups]


def main():
    global CACHE_DIR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-crawl", action="store_true", help="use cache only")
    ap.add_argument("--cache-dir", default=CACHE_DIR, help="dir of cached pages")
    ap.add_argument("-o", "--output", default="historic.ics")
    ap.add_argument(
        "--include-unplanned",
        action="store_true",
        help="also include unplanned outages, dated by their created/updated "
        "timestamps (default: scheduled maintenance only)",
    )
    args = ap.parse_args()
    CACHE_DIR = args.cache_dir

    if not args.no_crawl:
        crawl()
    rows = analyse(include_unplanned=args.include_unplanned)
    deduped = dedup_report(rows)

    cal = M.build_calendar(
        deduped, "America/Toronto", "DRAC Cluster Outages (historic)"
    )
    M.sort_events(cal, "America/Toronto")
    with open(args.output, "wb") as f:
        f.write(cal.to_ical())
    print(f"\nWrote {sum(1 for _ in cal.walk('VEVENT'))} event(s) to {args.output}")

    # --- Killarney-only historic feed ---
    # Match prose too (M.matches_service), so multi-cluster outages that name
    # Killarney only in the write-up are included -- same rule as the live feed.
    by_service = [r for r in deduped if "killarney" in r["service"].lower()]
    by_mention = [r for r in deduped if M.matches_service(r, "Killarney")]
    mention_only = [r for r in by_mention if r not in by_service]

    print("\n=== Killarney ===")
    print(f"  service == Killarney:        {len(by_service)}")
    print(
        f"  + mentioned in title/summary: {len(by_mention)} total "
        f"({len(mention_only)} mention-only)"
    )
    for r in mention_only:
        print(
            f"    mention-only id {r['id']} [{r['service']}] "
            f"{str(r['start'])[:16]}  {r['title'][:50]!r}"
        )

    kpath = os.path.join(os.path.dirname(args.output), "historic_killarney.ics")
    kcal = M.build_calendar(
        by_mention, "America/Toronto", "Killarney Cluster Outages (historic)"
    )
    M.sort_events(kcal, "America/Toronto")
    with open(kpath, "wb") as f:
        f.write(kcal.to_ical())
    print(f"  Wrote {sum(1 for _ in kcal.walk('VEVENT'))} event(s) to {kpath}")


if __name__ == "__main__":
    main()
