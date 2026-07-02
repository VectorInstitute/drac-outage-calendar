#!/usr/bin/env python3
"""Scrape the DRAC status page into an iCalendar (.ics) feed of outages.

Scrapes the Digital Research Alliance of Canada status page
(https://status.alliancecan.ca/) for scheduled cluster outages / maintenance
events.

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
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event

BASE = "https://status.alliancecan.ca/"
HOME = BASE
DEFAULT_TZ = "America/Toronto"  # EST/EDT, what the site quotes times in
DEFAULT_CALNAME = "DRAC Canada Cluster Outages"
# Inserted after the "[service]" prefix of a live, still-open outage's title so
# it doesn't read as finished in a calendar (its end is only the scan time) --
# e.g. "[Fir] (unresolved) Filesystem problem". Stripped once it resolves.
ONGOING_MARKER = "(unresolved)"
HEADERS = {"User-Agent": "drac-outage-calendar/1.0 (personal use)"}
# Event length used when an incident gives a start but no end -- and, for a
# *past* (resolved) outage with no recorded resolution time, the length it's
# shown as instead of being stretched to the scan time.
DEFAULT_DURATION = timedelta(hours=24)
# Cap on how many ids one gap scan will fetch, so a corrupt/reset state file
# can't trigger a full-history crawl (that's backfill_historic.py's job).
SCAN_CAP = 200
# An incident page is planned maintenance if its text carries one of these; an
# unplanned incident page carries none.
SCHED_MARKERS = re.compile(r"planned|planifi|maintenance|scheduled", re.I)

# Map the abbreviations the site prints to tz names, so "4:00 AM EDT" resolves.
TZINFOS = {
    "EDT": ZoneInfo("America/Toronto"),
    "EST": ZoneInfo("America/Toronto"),
    "ADT": ZoneInfo("America/Halifax"),
    "AST": ZoneInfo("America/Halifax"),
    "CDT": ZoneInfo("America/Winnipeg"),
    "CST": ZoneInfo("America/Winnipeg"),
    "MDT": ZoneInfo("America/Edmonton"),
    "MST": ZoneInfo("America/Edmonton"),
    "PDT": ZoneInfo("America/Vancouver"),
    "PST": ZoneInfo("America/Vancouver"),
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
                out.append((current_service or "DRAC", url))
    return out


def get_current_incident_urls(home_html):
    """Return [(service, incident_url)] for services with an active incident.

    Unplanned outages live in the home page's status table, one row per
    service, with a link in the trailing "Current incidents" column -- distinct
    from the "Scheduled events" block handled above.
    """
    soup = BeautifulSoup(home_html, "html.parser")
    out, seen = [], set()
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(("td", "th"))
            if len(cells) < 3:
                continue
            a = cells[-1].find("a", href=lambda h: h and h.startswith("/view_incident"))
            if not a:
                continue
            url = urljoin(BASE, a["href"])
            service = cells[0].get_text(strip=True) or "DRAC"
            if url not in seen:
                seen.add(url)
                out.append((service, url))
    return out


def incident_id(url):
    """Return the integer incident id from a view_incident URL."""
    return int(url.rstrip("/").split("=")[-1])


def is_incident_page(html):
    """Return True for a real incident page (vs the empty-id home stub)."""
    return "Incident description" in html


def is_scheduled_page(html):
    """Return True if the incident page is planned maintenance, not unplanned."""
    return bool(SCHED_MARKERS.search(html))


# Incident pages print their created/updated timestamps via a
# change_date_full("YYYY-MM-DD HH:MM", ..) script call. The one in the
# "Created by" block is when the incident began; the "Updated by" one is the
# last update (≈ resolution for a closed incident).
_TS_RE = re.compile(r'change_date_full\("(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})')


def _block_timestamp(html, label, tz):
    i = html.find(label)
    if i < 0:
        return None
    m = _TS_RE.search(html, i)
    if not m:
        return None
    y, mo, d, h, mn = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mn, tzinfo=ZoneInfo(tz))


def page_timestamps(html, tz=DEFAULT_TZ):
    """Return (created, updated) datetimes for an incident page (each or None)."""
    return (
        _block_timestamp(html, "Created by", tz),
        _block_timestamp(html, "Updated by", tz),
    )


def parse_incident(html, url, tz=DEFAULT_TZ):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    created, updated = page_timestamps(html, tz)

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
        # Anchor the year to when the incident was posted, not "now" -- matters
        # for historic incidents whose summary omits the year.
        dt_start, dt_end = parse_dates_from_prose(summary, ref=created)

    return {
        "service": service or "DRAC",
        "title": title or "Scheduled outage",
        "summary": summary,
        "url": url,
        "start": dt_start,
        "end": dt_end,
        "created": created,
        "updated": updated,
    }


def safe_parse(s):
    try:
        return dtparser.parse(s, tzinfos=TZINFOS, fuzzy=True)
    except (ValueError, OverflowError):
        return None


MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    )
}

# Map every tz abbreviation / word the site uses to an IANA zone. The site
# quotes Eastern/Central/Mountain/Pacific/Atlantic in both DST and standard
# forms, the bare "ET/CT/.." forms, the spelled-out "Pacific time", and the
# French "HNC" (heure normale du Centre = CST).
TZ_ZONE = {
    "EDT": "America/Toronto",
    "EST": "America/Toronto",
    "ET": "America/Toronto",
    "EASTERN": "America/Toronto",
    "ADT": "America/Halifax",
    "AST": "America/Halifax",
    "AT": "America/Halifax",
    "ATLANTIC": "America/Halifax",
    "CDT": "America/Winnipeg",
    "CST": "America/Winnipeg",
    "CT": "America/Winnipeg",
    "CENTRAL": "America/Winnipeg",
    "HNC": "America/Winnipeg",
    "MDT": "America/Edmonton",
    "MST": "America/Edmonton",
    "MT": "America/Edmonton",
    "MOUNTAIN": "America/Edmonton",
    "PDT": "America/Vancouver",
    "PST": "America/Vancouver",
    "PT": "America/Vancouver",
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
    r"Eastern|Atlantic|Central|Mountain|Pacific)\b",
    re.I,
)


def english_part(s):
    """Strip the French and system parts, keeping only the leading English.

    The site appends a French translation after '//' and a system block after
    '======'; only the leading English is kept, for date parsing.
    """
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
    """Find clock times within `window` chars after position `start`.

    Each is returned as (hour, minute, tzname_or_None), in order of appearance.
    """
    chunk = text[start : start + window]
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
        tzm = TZ_RE.search(chunk[tm.end() : tm.end() + 12])
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
    else:  # single all-day event
        end = start + timedelta(days=1)
    return start, end


def build_calendar(incidents, tzname, calname=DEFAULT_CALNAME):
    tz = ZoneInfo(tzname)
    cal = Calendar()
    cal.add("prodid", "-//DRAC Outage Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", calname)
    cal.add("x-wr-timezone", tzname)

    for inc in incidents:
        start, end = inc["start"], inc["end"]
        if start is None:
            # No parseable date -> omit. A placeholder at build time would show
            # a bogus "now" slot; the real outage is at some unknown future time.
            continue

        ev = Event()
        uid = inc["url"].split("=")[-1]
        ev.add("uid", f"drac-incident-{uid}@status.alliancecan.ca")
        desc = (inc["summary"] or "").strip()
        ev.add("description", f"{desc}\n\n{inc['url']}".strip())
        ev.add("url", inc["url"])

        if inc.get("ongoing"):
            summary = f"[{inc['service']}] {ONGOING_MARKER} {inc['title']}"
        else:
            summary = f"[{inc['service']}] {inc['title']}"
        ev.add("summary", summary)
        ev.add("categories", [inc.get("kind", "scheduled").upper()])
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end is None:
            end = start + DEFAULT_DURATION
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)

        ev.add("dtstart", start)
        ev.add("dtend", end)
        ev.add("dtstamp", datetime.now(tz))
        cal.add_component(ev)
    return cal


def date_unplanned(inc, now, ongoing=True):
    """Fill in an unplanned incident's start/end from its page timestamps.

    Unplanned outages rarely carry a parseable prose date, so fall back to the
    incident's own timeline:
      * start  -> when it was created (if prose gave nothing);
      * end    -> its last update once resolved (`updated` after `start`).
    With no resolution timestamp, the end depends on `ongoing`: a live incident
    (still on the status page) is in progress, so its end is projected to
    `now + DEFAULT_DURATION` -- past the present, so it doesn't read as already
    ended in a calendar (a bare "now" end sits in the viewer's past) -- and it
    moves forward each run until the incident resolves, when the carry-forward
    merge truncates it back to ~the resolution time. A *past* incident
    (`ongoing=False` -- reached via the gap scan or backfill, already off the
    page) is not still running, so its end is left unset for `build_calendar`'s
    `DEFAULT_DURATION` from its start, rather than projected forward. Leaves
    start None (undateable) for very old incidents with no created timestamp;
    such events are omitted.
    """
    if inc.get("start") is None:
        inc["start"] = inc.get("created")
    if inc.get("start") is None:
        return
    if inc.get("end") is None:
        upd = inc.get("updated")
        if upd is not None and upd > inc["start"]:
            inc["end"] = upd
        elif ongoing:
            # Still open: project past now so it doesn't read as already ended.
            inc["end"] = now + DEFAULT_DURATION
            inc["ongoing"] = True  # also tag the title
        # else: past, no resolution time -> leave end unset (default duration)


def matches_service(inc, needle):
    """Return True if `needle` appears in the incident's service, title, or summary.

    Matching the prose as well as the structured service field catches
    multi-cluster outages that name an affected cluster only in the write-up
    (e.g. a SciNet maintenance window that lists Killarney among the systems it
    takes down, but is filed under another service).
    """
    needle = needle.lower()
    blob = " ".join(str(inc.get(k, "")) for k in ("service", "title", "summary"))
    return needle in blob.lower()


def _as_dt(value, tz):
    """Coerce an icalendar date/datetime into a tz-aware datetime."""
    if isinstance(value, datetime):  # datetime is a subclass of date
        return value if value.tzinfo is not None else value.replace(tzinfo=tz)
    return datetime(value.year, value.month, value.day, tzinfo=tz)


def read_calendar(path):
    """Load a previous-state calendar for merging.

    Returns a Calendar, or None if the file does not exist (first run /
    bootstrap). Raises if the file exists but cannot be parsed -- the caller
    must abort rather than silently overwrite (and so destroy) accumulated
    history.
    """
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return Calendar.from_ical(f.read())


def _event_service(ev):
    """Service name from an event summary formatted '[service] title'."""
    summ = str(ev.get("summary", ""))
    if summ.startswith("[") and "]" in summ:
        return summ[1 : summ.index("]")].strip().lower()
    return summ.strip().lower()


def _strip_ongoing_marker(ev):
    """Remove the live-outage title marker from an event being finalized.

    A carried/truncated event has dropped out of the scrape, so it is no longer
    live -- its title should read as a finished outage.
    """
    s = str(ev.get("summary", ""))
    tag = f"] {ONGOING_MARKER} "
    if tag in s:
        ev.pop("summary", None)
        ev.add("summary", s.replace(tag, "] ", 1))


def _content_key(ev, tz):
    """Return a UID-independent identity: (service, start-day).

    Lets the merge recognise a re-published incident whose id (and thus UID)
    changed on edit -- the status site mints a new id per edit -- as the same
    outage: such copies keep the same service and start day.
    """
    ds = ev.get("dtstart")
    if ds is None:
        return None
    return (_event_service(ev), _as_dt(ds.dt, tz).date())


def merge_history(cal, prev_cal, tzname, now=None):
    """Carry forward events that have dropped out of the fresh scrape.

    For each event in the previous state that is absent from the new scrape,
    its fate is decided by where *now* sits relative to it:
      * already finished (end <= now)    -> keep as-is (elapsed, historical)
      * in progress (start <= now < end) -> keep, truncate end to now (it
                                            vanished mid-window, i.e. the
                                            maintenance finished early)
      * still upcoming (now < start)     -> drop (it vanished while future, so
                                            treat it as cancelled)
    Events still present in the scrape are left untouched -- the fresh data
    wins, so reschedules and end-time changes update. A previous event whose id
    changed on edit (new UID) but whose (service, start-day) still matches a
    fresh event is treated as superseded and dropped, so a re-published ongoing
    outage isn't carried forward as a stale duplicate of its new id. Returns the
    counts (carried, truncated, dropped, superseded).
    """
    tz = ZoneInfo(tzname)
    now = now or datetime.now(tz)
    have = {str(ev.get("uid")) for ev in cal.walk("VEVENT")}
    have_content = {_content_key(ev, tz) for ev in cal.walk("VEVENT")}
    have_content.discard(None)
    carried = truncated = dropped = superseded = 0
    for ev in prev_cal.walk("VEVENT"):
        if str(ev.get("uid")) in have:
            continue  # in the scrape -> fresh data wins
        if _content_key(ev, tz) in have_content:
            superseded += 1  # same outage, re-published under a new id
            continue
        ds = ev.get("dtstart")
        if ds is None:
            continue
        start = _as_dt(ds.dt, tz)
        de = ev.get("dtend")
        end = _as_dt(de.dt, tz) if de is not None else start
        if now < start:
            dropped += 1  # vanished while future -> cancelled
            continue
        if start <= now < end:  # vanished mid-window -> ended early
            ev.pop("dtend", None)
            ev.add("dtend", now)
            ev.pop("dtstamp", None)
            ev.add("dtstamp", now)
            truncated += 1
        else:
            carried += 1  # already over -> historical
        _strip_ongoing_marker(ev)  # no longer live -> title reads as finished
        cal.add_component(ev)
    return carried, truncated, dropped, superseded


def sort_events(cal, tzname):
    """Order VEVENTs by start time, for stable and readable output / diffs."""
    tz = ZoneInfo(tzname)
    events, others = [], []
    for c in cal.subcomponents:
        (events if c.name == "VEVENT" else others).append(c)
    events.sort(key=lambda e: _as_dt(e.get("dtstart").dt, tz))
    cal.subcomponents = others + events


def read_scan_state(path):
    """Return the last-scanned incident id from `path`, or None if unavailable.

    None means "no high-water mark yet" (missing / empty / unparseable file);
    the caller bootstraps rather than crawling the whole history.
    """
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_scan_state(path, last_id):
    """Persist the high-water incident id so the next run resumes after it."""
    with open(path, "w") as f:
        f.write(f"{last_id}\n")


def scan_gap(ids, tzname, include_unplanned, now, delay=0.5):
    """Fetch each incident id in `ids`, returning the dated outages among them.

    This is the catch-up crawl for outages the daily poll skipped: incidents
    created and resolved between two runs never appear on the home page, but
    their sequential ids sit in the gap since we last looked. Each page is
    classified (scheduled vs unplanned) and dated the same way the live scrape
    dates its incidents -- scheduled from the prose, unplanned from the
    created/updated timestamps -- so results flow through the same pipeline.
    Unplanned outages are only kept when `include_unplanned` is set; scheduled
    ones are always kept. Undated and non-incident (empty) ids are skipped.
    """
    out = []
    for i in ids:
        url = f"{BASE}view_incident?incident={i}"
        try:
            html = fetch(url)
        except requests.RequestException as e:
            print(f"  ! skip {url}: {e}", file=sys.stderr)
            continue
        time.sleep(delay)  # polite pacing between fetches
        if not is_incident_page(html):
            continue
        inc = parse_incident(html, url, tzname)
        if is_scheduled_page(html):
            inc["kind"] = "scheduled"
        elif include_unplanned:
            inc["kind"] = "unplanned"
            # Gap incidents are off the home page, i.e. already resolved -- so
            # don't stretch a missing end to now; use the default duration.
            date_unplanned(inc, now, ongoing=False)
        else:
            continue  # unplanned, but not requested
        if inc["start"] is None:
            continue
        out.append(inc)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-o", "--output", default="outages.ics")
    ap.add_argument("--tz", default=DEFAULT_TZ, help="IANA tz for naive times")
    ap.add_argument(
        "--service",
        default=None,
        help="only include incidents that mention this string "
        "(case-insensitive) in their service, title, or summary, "
        "e.g. 'Killarney' -- prose matching catches multi-cluster "
        "outages filed under another service",
    )
    ap.add_argument(
        "--calname",
        default=DEFAULT_CALNAME,
        help="calendar display name (X-WR-CALNAME)",
    )
    ap.add_argument(
        "--merge-from",
        default=None,
        metavar="ICS",
        help="previous-state .ics to merge in: events that have "
        "elapsed and dropped off the status page are carried "
        "forward so past outages aren't lost",
    )
    ap.add_argument(
        "--include-unplanned",
        action="store_true",
        help="also include current unplanned outages (from the status "
        "table), dated by the incident's created/updated timestamps",
    )
    ap.add_argument(
        "--scan-state",
        default=None,
        metavar="FILE",
        help="enable the catch-up gap scan: read the last-scanned incident "
        "id from FILE, fetch the newer ids up to the highest one currently "
        "visible to capture sub-day outages the daily poll skipped, then "
        "write the new high-water id back to FILE (bootstraps silently if "
        f"FILE is absent; at most {SCAN_CAP} ids per run)",
    )
    args = ap.parse_args()
    now = datetime.now(ZoneInfo(args.tz))

    try:
        home = fetch(HOME)
    except requests.RequestException as e:
        sys.exit(f"Could not fetch status page: {e}")

    urls = get_scheduled_incident_urls(home)
    if not urls:
        print(
            "No scheduled events found (page layout may have changed).", file=sys.stderr
        )

    incidents = []
    for service, url in urls:
        try:
            inc = parse_incident(fetch(url), url, args.tz)
            if inc["service"] in (None, "DRAC"):
                inc["service"] = service
            inc["kind"] = "scheduled"
            incidents.append(inc)
            when = inc["start"].isoformat() if inc["start"] else "date TBD"
            print(f"  • {inc['service']}: {inc['title']} — {when}")
        except requests.RequestException as e:
            print(f"  ! skip {url}: {e}", file=sys.stderr)

    if args.include_unplanned:
        scheduled_urls = {u for _, u in urls}
        for service, url in get_current_incident_urls(home):
            if url in scheduled_urls:
                continue  # already captured as a scheduled event
            try:
                inc = parse_incident(fetch(url), url, args.tz)
            except requests.RequestException as e:
                print(f"  ! skip {url}: {e}", file=sys.stderr)
                continue
            if inc["service"] in (None, "DRAC"):
                inc["service"] = service
            inc["kind"] = "unplanned"
            date_unplanned(inc, now)
            incidents.append(inc)
            when = inc["start"].isoformat() if inc["start"] else "date TBD"
            print(f"  ⚠ {inc['service']}: {inc['title']} — {when} (unplanned)")

    # Count what the scrape itself yielded *before* any service filter -- the
    # merge guard below keys off this to tell "the scrape failed" (zero found)
    # apart from "this cluster simply has nothing scheduled" (filtered to zero).
    n_scraped = len(incidents)

    # Catch-up gap scan: fetch the incident ids between the last one we scanned
    # and the highest currently visible, to capture sub-day outages the daily
    # poll skipped. Runs on the unfiltered scrape (so per-cluster feeds still
    # see the whole gap), and its results join `incidents` before the filter.
    new_scan_mark = None
    if args.scan_state:
        visible = {incident_id(u) for _, u in urls}
        visible |= {incident_id(u) for _, u in get_current_incident_urls(home)}
        if not visible:
            print("Scan-gap: no visible incidents; skipping.", file=sys.stderr)
        else:
            current_max = max(visible)
            last = read_scan_state(args.scan_state)
            if last is None:
                new_scan_mark = current_max
                print(f"Scan-gap: no prior state; mark bootstrapped to {current_max}.")
            else:
                gap = [i for i in range(last + 1, current_max + 1) if i not in visible]
                capped = len(gap) > SCAN_CAP
                to_scan = gap[:SCAN_CAP]
                found = scan_gap(to_scan, args.tz, args.include_unplanned, now)
                incidents.extend(found)
                # Advance only over the contiguous range actually covered, so a
                # capped run resumes at the right place next time.
                new_scan_mark = to_scan[-1] if capped else current_max
                for inc in found:
                    when = inc["start"].isoformat() if inc["start"] else "date TBD"
                    print(f"  + {inc['service']}: {inc['title']} — {when} (gap)")
                msg = (
                    f"Scan-gap: {len(to_scan)} id(s) in ({last}, {current_max}], "
                    f"{len(found)} outage(s) found"
                )
                if capped:
                    msg += f"; capped at {SCAN_CAP}, resuming after {new_scan_mark}"
                print(msg + ".")

    if args.service:
        incidents = [i for i in incidents if matches_service(i, args.service)]
        print(
            f"Filtered to ~ {args.service!r} (service/title/summary): "
            f"{len(incidents)} incident(s)"
        )

    cal = build_calendar(incidents, args.tz, args.calname)

    if args.merge_from:
        try:
            prev = read_calendar(args.merge_from)
        except Exception as e:
            sys.exit(
                f"--merge-from {args.merge_from!r} exists but could not be "
                f"parsed ({e}); aborting so accumulated history isn't lost."
            )
        if prev is None:
            print(f"No previous state at {args.merge_from} -- bootstrapping fresh.")
        elif n_scraped == 0:
            sys.exit(
                "Scrape found zero incidents (status page fetch failed or its "
                "layout changed); aborting merge so future events in the "
                "previous state aren't dropped as if cancelled."
            )
        else:
            carried, truncated, dropped, superseded = merge_history(cal, prev, args.tz)
            print(
                f"Merged previous state: {carried} carried forward, "
                f"{truncated} truncated, {dropped} dropped (cancelled), "
                f"{superseded} superseded (re-published under a new id)."
            )

    sort_events(cal, args.tz)
    with open(args.output, "wb") as f:
        f.write(cal.to_ical())
    written = sum(1 for c in cal.walk("VEVENT"))
    undated = sum(1 for i in incidents if i["start"] is None)
    note = f" ({undated} undated, omitted)" if undated else ""
    print(f"Wrote {written} event(s) to {args.output}{note}")

    # Persist the high-water id only after a successful build, and only when the
    # scrape actually saw something (new_scan_mark stays None on an empty scrape).
    if args.scan_state and new_scan_mark is not None:
        write_scan_state(args.scan_state, new_scan_mark)
        print(f"Scan-gap: state saved (last scanned id = {new_scan_mark}).")


if __name__ == "__main__":
    main()
