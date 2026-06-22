#!/usr/bin/env python3
"""
drac_outages_ics.py
-----------------------
Scrapes the Digital Research Alliance of Canada status page
(https://status.alliancecan.ca/) and produces an iCalendar (.ics) feed of
scheduled cluster outages / maintenance events.

The status site is a self-hosted Cachet instance. It exposes no public
RSS/iCal feed, and the structured Start/End date fields on incident pages are
frequently blank -- the real dates live in the free-text Summary. This script
therefore:
  1. reads the home page, collects every incident linked under "Scheduled events",
  2. fetches each incident page,
  3. uses the structured Start/End dates when present, otherwise parses dates
     out of the Summary prose,
  4. writes outages.ics.

Run it on a schedule (cron / GitHub Actions) and publish outages.ics somewhere
your calendar app can subscribe to by URL, so events refresh automatically.

Usage:
    python drac_outages_ics.py                 # writes ./outages.ics
    python drac_outages_ics.py -o path.ics
    python drac_outages_ics.py --tz America/Toronto
"""

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event
from zoneinfo import ZoneInfo

BASE = "https://status.alliancecan.ca/"
HOME = BASE
DEFAULT_TZ = "America/Toronto"          # EST/EDT, what the site quotes times in
HEADERS = {"User-Agent": "alliance-outage-calendar/1.0 (personal use)"}

# Map the abbreviations the site prints to tz names, so "4:00 AM EDT" resolves.
TZINFOS = {
    "EDT": ZoneInfo("America/Toronto"), "EST": ZoneInfo("America/Toronto"),
    "ADT": ZoneInfo("America/Halifax"), "AST": ZoneInfo("America/Halifax"),
    "CDT": ZoneInfo("America/Winnipeg"), "CST": ZoneInfo("America/Winnipeg"),
    "MDT": ZoneInfo("America/Edmonton"), "MST": ZoneInfo("America/Edmonton"),
    "PDT": ZoneInfo("America/Vancouver"), "PST": ZoneInfo("America/Vancouver"),
    "UTC": ZoneInfo("UTC"),
}


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def get_scheduled_incident_urls(home_html):
    """Return [(service_name, incident_url)] from the Scheduled events block."""
    soup = BeautifulSoup(home_html, "html.parser")
    # Find the "Scheduled events" heading, then walk forward collecting
    # alternating <strong>service</strong> ... <a>title</a> until the footer.
    heading = soup.find(
        lambda t: t.name in ("h2", "h3", "h4", "h5")
        and "scheduled events" in t.get_text(strip=True).lower()
    )
    out, seen = [], set()
    if not heading:
        return out
    current_service = None
    for el in heading.find_all_next():
        if el.name in ("h2", "h3", "h4", "h5"):  # next section -> stop
            if el is not heading:
                break
        if el.name in ("strong", "b"):
            current_service = el.get_text(strip=True)
        elif el.name == "a" and el.get("href", "").startswith("/view_incident"):
            url = urljoin(BASE, el["href"])
            if url not in seen:
                seen.add(url)
                out.append((current_service or "Alliance", url))
    return out


def parse_incident(html, url):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    # --- service, structured start/end from the description table ---
    service = start = end = None
    table = soup.find("table")
    if table:
        rows = table.find_all("tr")
        if len(rows) >= 2:
            cells = [c.get_text(strip=True) for c in rows[1].find_all(("td", "th"))]
            if len(cells) >= 4:
                service = cells[0] or service
                start = cells[2] or None
                end = cells[3] or None

    # --- title ---
    title = None
    m = re.search(r"Title\s*\n+\s*(.+)", text)
    if m:
        title = m.group(1).strip()

    # --- summary ---
    summary = ""
    m = re.search(r"Summary\s*\n+(.*?)(?:\nUpdated by|\nBack|\Z)", text, re.S)
    if m:
        summary = re.sub(r"\s+", " ", m.group(1)).strip()

    dt_start = dt_end = None
    if start:
        dt_start = safe_parse(start)
    if end:
        dt_end = safe_parse(end)
    if not dt_start:
        dt_start, dt_end = parse_dates_from_prose(summary)

    return {
        "service": service or "Alliance",
        "title": title or "Scheduled outage",
        "summary": summary,
        "url": url,
        "start": dt_start,
        "end": dt_end,
    }


def safe_parse(s):
    try:
        return dtparser.parse(s, tzinfos=TZINFOS, fuzzy=True)
    except (ValueError, OverflowError):
        return None


MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}

# Map every tz abbreviation / word the site uses to an IANA zone. The site
# quotes Eastern/Central/Mountain/Pacific/Atlantic in both DST and standard
# forms, the bare "ET/CT/.." forms, the spelled-out "Pacific time", and the
# French "HNC" (heure normale du Centre = CST).
TZ_ZONE = {
    "EDT": "America/Toronto", "EST": "America/Toronto", "ET": "America/Toronto",
    "EASTERN": "America/Toronto",
    "ADT": "America/Halifax", "AST": "America/Halifax", "AT": "America/Halifax",
    "ATLANTIC": "America/Halifax",
    "CDT": "America/Winnipeg", "CST": "America/Winnipeg", "CT": "America/Winnipeg",
    "CENTRAL": "America/Winnipeg", "HNC": "America/Winnipeg",
    "MDT": "America/Edmonton", "MST": "America/Edmonton", "MT": "America/Edmonton",
    "MOUNTAIN": "America/Edmonton",
    "PDT": "America/Vancouver", "PST": "America/Vancouver", "PT": "America/Vancouver",
    "PACIFIC": "America/Vancouver",
    "UTC": "UTC",
}

# "Month DD" with optional "-DD" range and optional ", YYYY" (the year is often
# omitted on the site). Stray whitespace around the comma/dash is tolerated.
MONTH_RE = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?P<d1>\d{1,2})"
    r"(?:\s*(?:[-\u2013\u2014]|to)\s*(?P<d2>\d{1,2}))?"
    r"(?:\s*,?\s*(?P<year>\d{4}))?",
    re.I,
)

ISO_RE = re.compile(r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})")

# A clock time: "2:00 PM", "20:00", or "7AM" (bare hour requires AM/PM so we
# don't mistake a day number for a time). An optional tz token may follow.
TIME_RE = re.compile(
    r"(?:(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>AM|PM)?"
    r"|(?P<h2>\d{1,2})\s*(?P<ap2>AM|PM))",
    re.I,
)
TZ_RE = re.compile(
    r"\b(EDT|EST|ET|ADT|AST|AT|CDT|CST|CT|MDT|MST|MT|PDT|PST|PT|UTC|HNC|"
    r"Eastern|Atlantic|Central|Mountain|Pacific)\b", re.I)


def english_part(s):
    """The site appends a French translation after '//' and a system block
    after '======'; keep only the leading English for date parsing."""
    for sep in ("//", "======"):
        i = s.find(sep)
        if i != -1:
            s = s[:i]
    return s.strip()


def infer_year(month, day, ref):
    """Choose the year (ref +/- 1) that puts month/day closest to ref."""
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = date(y, month, day)
        except ValueError:
            continue
        diff = abs((cand - ref.date()).days)
        if best is None or diff < best[0]:
            best = (diff, y)
    return best[1] if best else ref.year


def _times_in(text, start, window=40):
    """Times appearing within `window` chars after position `start`, each as
    (hour, minute, tzname_or_None), in order of appearance."""
    chunk = text[start:start + window]
    out = []
    for tm in TIME_RE.finditer(chunk):
        if tm.group("h") is not None:
            h, mn, ap = int(tm["h"]), int(tm["m"]), tm["ap"]
        else:
            h, mn, ap = int(tm["h2"]), 0, tm["ap2"]
        if ap:
            ap = ap.upper()
            if ap == "PM" and h != 12:
                h += 12
            elif ap == "AM" and h == 12:
                h = 0
        tzm = TZ_RE.search(chunk[tm.end():tm.end() + 12])
        out.append((h, mn, TZ_ZONE.get(tzm.group(1).upper()) if tzm else None))
    # A tz stated once (often after the last time, e.g. "2:00 PM - 2:30 PM CST")
    # applies to every time in the range; backfill the ones without their own.
    fallback = next((tz for _, _, tz in out if tz), None)
    if fallback:
        out = [(h, mn, tz or fallback) for h, mn, tz in out]
    return out


def _combine(d, t):
    """Build a datetime from a date and an optional (h, mn, tzname) time."""
    if t is None:
        return datetime(d.year, d.month, d.day)
    h, mn, tzname = t
    dt = datetime(d.year, d.month, d.day, h, mn)
    return dt.replace(tzinfo=ZoneInfo(tzname)) if tzname else dt


def parse_dates_from_prose(summary, ref=None):
    """Best-effort extraction of a start/end datetime from free text."""
    if not summary:
        return None, None
    text = english_part(summary)
    ref = ref or datetime.now(ZoneInfo(DEFAULT_TZ))

    # ISO dates ("2026-07-12 7AM to 2026-07-13 4PM") are unambiguous; prefer them.
    iso = list(ISO_RE.finditer(text))
    if iso:
        d1 = date(int(iso[0]["y"]), int(iso[0]["mo"]), int(iso[0]["d"]))
        times1 = _times_in(text, iso[0].end())
        start = _combine(d1, times1[0] if times1 else None)
        if len(iso) >= 2:
            d2 = date(int(iso[1]["y"]), int(iso[1]["mo"]), int(iso[1]["d"]))
            times2 = _times_in(text, iso[1].end())
            end = _combine(d2, times2[0] if times2 else None)
        else:
            end = start + (timedelta(hours=1) if times1 else timedelta(days=1))
        return start, end

    # Otherwise a "Month DD[-DD][, YYYY]" form.
    m = MONTH_RE.search(text)
    if not m:
        return None, None
    month = MONTHS[m["month"].lower()]
    d1 = int(m["d1"])
    d2 = int(m["d2"]) if m["d2"] else None
    year = int(m["year"]) if m["year"] else infer_year(month, d1, ref)

    times = _times_in(text, m.end())
    t1 = times[0] if times else None
    t2 = times[1] if len(times) > 1 else None

    try:
        start = _combine(date(year, month, d1), t1)
    except ValueError:
        return None, None

    if d2:  # multi-day range; span through the end of the last day
        try:
            end = _combine(date(year, month, d2), None) + timedelta(days=1)
        except ValueError:
            end = start + timedelta(days=1)
    elif t2:  # single day with an explicit "start - end" time range
        end = _combine(date(year, month, d1), t2)
    elif t1:  # single day, one time given
        end = start + timedelta(hours=1)
    else:     # single all-day event
        end = start + timedelta(days=1)
    return start, end


def build_calendar(incidents, tzname):
    tz = ZoneInfo(tzname)
    cal = Calendar()
    cal.add("prodid", "-//Alliance Outage Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Alliance Canada Cluster Outages")
    cal.add("x-wr-timezone", tzname)

    for inc in incidents:
        ev = Event()
        uid = inc["url"].split("=")[-1]
        ev.add("uid", f"alliance-incident-{uid}@status.alliancecan.ca")
        desc = (inc["summary"] or "").strip()
        ev.add("description", f"{desc}\n\n{inc['url']}".strip())
        ev.add("url", inc["url"])

        start, end = inc["start"], inc["end"]
        title = f"[{inc['service']}] {inc['title']}"
        if start is None:
            # No parseable date -> placeholder today so it's still visible.
            start = datetime.now(tz)
            end = start + timedelta(hours=1)
            title += " (date TBD - check link)"
        ev.add("summary", title)
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end is None:
            end = start + timedelta(hours=8)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)

        ev.add("dtstart", start)
        ev.add("dtend", end)
        ev.add("dtstamp", datetime.now(tz))
        cal.add_component(ev)
    return cal


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="outages.ics")
    ap.add_argument("--tz", default=DEFAULT_TZ, help="IANA tz for naive times")
    args = ap.parse_args()

    try:
        home = fetch(HOME)
    except requests.RequestException as e:
        sys.exit(f"Could not fetch status page: {e}")

    urls = get_scheduled_incident_urls(home)
    if not urls:
        print("No scheduled events found (page layout may have changed).",
              file=sys.stderr)

    incidents = []
    for service, url in urls:
        try:
            inc = parse_incident(fetch(url), url)
            if inc["service"] in (None, "Alliance"):
                inc["service"] = service
            incidents.append(inc)
            when = inc["start"].isoformat() if inc["start"] else "date TBD"
            print(f"  • {inc['service']}: {inc['title']} — {when}")
        except requests.RequestException as e:
            print(f"  ! skip {url}: {e}", file=sys.stderr)

    cal = build_calendar(incidents, args.tz)
    with open(args.output, "wb") as f:
        f.write(cal.to_ical())
    print(f"Wrote {len(incidents)} event(s) to {args.output}")


if __name__ == "__main__":
    main()
