#!/usr/bin/env python3
"""Unit tests for drac_outages_ics.

No network: every test builds its inputs in memory. Run with

    python -m unittest

(or `pytest`, which also discovers unittest.TestCase classes).
"""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

import drac_outages_ics as M

TORONTO = ZoneInfo("America/Toronto")


def _event(uid, start, end):
    ev = Event()
    ev.add("uid", f"drac-incident-{uid}@status.alliancecan.ca")
    ev.add("summary", f"[Test] event {uid}")
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("dtstamp", datetime(2026, 1, 1, tzinfo=TORONTO))
    return ev


def _calendar(*events):
    cal = Calendar()
    for ev in events:
        cal.add_component(ev)
    return cal


def _uids(cal):
    return {str(ev.get("uid")) for ev in cal.walk("VEVENT")}


class MergeHistoryTests(unittest.TestCase):
    # A fixed "now" so the past/in-progress/future split is deterministic.
    NOW = datetime(2026, 6, 30, 12, 0, tzinfo=TORONTO)

    def test_elapsed_event_is_carried_forward(self):
        # In the previous state, finished before now, gone from the scrape.
        prev = _calendar(
            _event(
                "100",
                datetime(2026, 6, 20, 9, 0, tzinfo=TORONTO),
                datetime(2026, 6, 20, 17, 0, tzinfo=TORONTO),
            )
        )
        fresh = _calendar()  # scrape no longer lists it
        self.assertEqual(
            M.merge_history(fresh, prev, "America/Toronto", now=self.NOW),
            (1, 0, 0, 0),
        )
        self.assertIn("drac-incident-100@status.alliancecan.ca", _uids(fresh))

    def test_future_event_is_dropped_as_cancelled(self):
        prev = _calendar(
            _event(
                "200",
                datetime(2026, 7, 15, 9, 0, tzinfo=TORONTO),
                datetime(2026, 7, 15, 17, 0, tzinfo=TORONTO),
            )
        )
        fresh = _calendar()
        self.assertEqual(
            M.merge_history(fresh, prev, "America/Toronto", now=self.NOW),
            (0, 0, 1, 0),
        )
        self.assertEqual(_uids(fresh), set())

    def test_in_progress_event_is_truncated_to_now(self):
        prev = _calendar(
            _event(
                "300",
                datetime(2026, 6, 28, 0, 0, tzinfo=TORONTO),  # started before now
                datetime(2026, 7, 5, 0, 0, tzinfo=TORONTO),  # would end after now
            )
        )
        fresh = _calendar()
        self.assertEqual(
            M.merge_history(fresh, prev, "America/Toronto", now=self.NOW),
            (0, 1, 0, 0),
        )
        ev = next(iter(fresh.walk("VEVENT")))
        self.assertEqual(M._as_dt(ev.get("dtend").dt, TORONTO), self.NOW)

    def test_scraped_event_is_left_to_fresh_data(self):
        # Same UID in both: the previous (stale) copy must NOT be merged in;
        # the fresh event stays as-is so reschedules win.
        stale = _event(
            "400",
            datetime(2026, 7, 12, 7, 0, tzinfo=TORONTO),
            datetime(2026, 7, 13, 16, 0, tzinfo=TORONTO),
        )
        fresh_ev = _event(
            "400",
            datetime(2026, 7, 12, 7, 0, tzinfo=TORONTO),
            datetime(2026, 7, 13, 12, 0, tzinfo=TORONTO),
        )  # new end
        prev = _calendar(stale)
        fresh = _calendar(fresh_ev)
        # UID match takes precedence over the content check, so no supersede.
        self.assertEqual(
            M.merge_history(fresh, prev, "America/Toronto", now=self.NOW),
            (0, 0, 0, 0),
        )
        ends = [M._as_dt(ev.get("dtend").dt, TORONTO) for ev in fresh.walk("VEVENT")]
        self.assertEqual(ends, [datetime(2026, 7, 13, 12, 0, tzinfo=TORONTO)])

    def test_mixed_scenario_counts(self):
        prev = _calendar(
            _event(
                "a",
                datetime(2026, 6, 20, 9, 0, tzinfo=TORONTO),
                datetime(2026, 6, 20, 17, 0, tzinfo=TORONTO),
            ),  # elapsed
            _event(
                "b",
                datetime(2026, 6, 28, 0, 0, tzinfo=TORONTO),
                datetime(2026, 7, 5, 0, 0, tzinfo=TORONTO),
            ),  # in progress
            _event(
                "c",
                datetime(2026, 7, 15, 9, 0, tzinfo=TORONTO),
                datetime(2026, 7, 15, 17, 0, tzinfo=TORONTO),
            ),  # future
        )
        fresh = _calendar()
        self.assertEqual(
            M.merge_history(fresh, prev, "America/Toronto", now=self.NOW),
            (1, 1, 1, 0),
        )

    def test_reid_ongoing_event_is_superseded_not_duplicated(self):
        # Same outage (same service + start day) republished under a new id:
        # the stale cached copy must be dropped, not carried as a duplicate.
        prev = _calendar(
            _event(
                "1648",
                datetime(2026, 6, 29, 8, 0, tzinfo=TORONTO),
                datetime(2026, 6, 29, 12, 0, tzinfo=TORONTO),  # frozen yesterday
            )
        )
        fresh = _calendar(
            _event(
                "1652",  # new id, still ongoing (ends at now)
                datetime(2026, 6, 29, 8, 5, tzinfo=TORONTO),
                self.NOW,
            )
        )
        carried, truncated, dropped, superseded = M.merge_history(
            fresh, prev, "America/Toronto", now=self.NOW
        )
        self.assertEqual((carried, truncated, dropped, superseded), (0, 0, 0, 1))
        self.assertEqual(_uids(fresh), {"drac-incident-1652@status.alliancecan.ca"})


class SortEventsTests(unittest.TestCase):
    def test_events_sorted_by_start(self):
        cal = _calendar(
            _event(
                "late",
                datetime(2026, 8, 1, tzinfo=TORONTO),
                datetime(2026, 8, 2, tzinfo=TORONTO),
            ),
            _event(
                "early",
                datetime(2026, 5, 1, tzinfo=TORONTO),
                datetime(2026, 5, 2, tzinfo=TORONTO),
            ),
        )
        M.sort_events(cal, "America/Toronto")
        starts = [ev.get("dtstart").dt for ev in cal.walk("VEVENT")]
        self.assertEqual(starts, sorted(starts))


class ProseDateTests(unittest.TestCase):
    REF = datetime(2026, 6, 30, tzinfo=TORONTO)

    def test_iso_range(self):
        start, end = M.parse_dates_from_prose(
            "Nibi will be unavailable from 2026-07-12 7AM to 2026-07-13 12PM.",
            ref=self.REF,
        )
        self.assertEqual(start, datetime(2026, 7, 12, 7, 0))
        self.assertEqual(end, datetime(2026, 7, 13, 12, 0))

    def test_single_day_time_range_with_tz(self):
        start, end = M.parse_dates_from_prose(
            "FRDR maintenance May 27 (2:00 PM - 2:30 PM CST).", ref=self.REF
        )
        self.assertEqual(start.replace(tzinfo=None), datetime(2026, 5, 27, 14, 0))
        self.assertEqual(start.tzinfo, ZoneInfo("America/Winnipeg"))
        self.assertEqual(end.replace(tzinfo=None), datetime(2026, 5, 27, 14, 30))

    def test_multi_day_range_with_year(self):
        start, end = M.parse_dates_from_prose(
            "Outage June 22-25, 2026 starting at 4:00 AM EDT.", ref=self.REF
        )
        self.assertEqual(start.replace(tzinfo=None), datetime(2026, 6, 22, 4, 0))
        self.assertEqual(start.tzinfo, ZoneInfo("America/Toronto"))
        # A multi-day range spans through the end of the last day.
        self.assertEqual(end, datetime(2026, 6, 26, 0, 0))

    def test_unparseable_returns_none(self):
        self.assertEqual(
            M.parse_dates_from_prose("no date here", ref=self.REF), (None, None)
        )


class DateUnplannedTests(unittest.TestCase):
    NOW = datetime(2026, 7, 2, 12, 0, tzinfo=TORONTO)

    def test_resolved_uses_created_and_updated(self):
        inc = {
            "start": None,
            "end": None,
            "created": datetime(2026, 6, 30, 8, 0, tzinfo=TORONTO),
            "updated": datetime(2026, 6, 30, 15, 0, tzinfo=TORONTO),
        }
        M.date_unplanned(inc, self.NOW)
        self.assertEqual(inc["start"], datetime(2026, 6, 30, 8, 0, tzinfo=TORONTO))
        self.assertEqual(inc["end"], datetime(2026, 6, 30, 15, 0, tzinfo=TORONTO))

    def test_ongoing_ends_at_now(self):
        # Still open (no later update) -> in progress, end = now.
        inc = {
            "start": None,
            "end": None,
            "created": datetime(2026, 5, 21, 20, 1, tzinfo=TORONTO),
            "updated": None,
        }
        M.date_unplanned(inc, self.NOW)
        self.assertEqual(inc["start"], datetime(2026, 5, 21, 20, 1, tzinfo=TORONTO))
        self.assertEqual(inc["end"], self.NOW)

    def test_undateable_left_none(self):
        inc = {"start": None, "end": None, "created": None, "updated": None}
        M.date_unplanned(inc, self.NOW)
        self.assertIsNone(inc["start"])

    def test_prose_dates_are_kept(self):
        # If the summary already yielded dates, don't override them.
        inc = {
            "start": datetime(2026, 6, 1, 9, 0, tzinfo=TORONTO),
            "end": datetime(2026, 6, 1, 11, 0, tzinfo=TORONTO),
            "created": datetime(2026, 5, 30, tzinfo=TORONTO),
            "updated": None,
        }
        M.date_unplanned(inc, self.NOW)
        self.assertEqual(inc["end"], datetime(2026, 6, 1, 11, 0, tzinfo=TORONTO))


class MatchesServiceTests(unittest.TestCase):
    def test_matches_on_service_field(self):
        inc = {"service": "Killarney", "title": "Planned Outage", "summary": ""}
        self.assertTrue(M.matches_service(inc, "killarney"))

    def test_matches_on_title_or_summary(self):
        # Filed under another service, but names Killarney in the write-up.
        inc = {
            "service": "Trillium",
            "title": "Planned Outage",
            "summary": "SciNet maintenance takes down Killarney and other systems.",
        }
        self.assertTrue(M.matches_service(inc, "Killarney"))

    def test_no_match(self):
        inc = {"service": "Cedar", "title": "Planned Outage", "summary": "Cedar only"}
        self.assertFalse(M.matches_service(inc, "Killarney"))


class PageTimestampTests(unittest.TestCase):
    HTML = """
    <small>Created by David Magda on
      <script>change_date_full("2026-05-22 20:16", "en");</script>
    </small>
    <i>Updated by David Magda on
      <script>change_date_full("2026-06-26 22:14", "en");</script>
    </i>
    """

    def test_extracts_created_and_updated(self):
        created, updated = M.page_timestamps(self.HTML, "America/Toronto")
        self.assertEqual(created, datetime(2026, 5, 22, 20, 16, tzinfo=TORONTO))
        self.assertEqual(updated, datetime(2026, 6, 26, 22, 14, tzinfo=TORONTO))

    def test_missing_timestamps_return_none(self):
        self.assertEqual(M.page_timestamps("<p>no timestamps</p>"), (None, None))


class CurrentIncidentTests(unittest.TestCase):
    HOME = """
    <table>
      <tr><th>Service</th><th>Status</th><th>Current incidents</th></tr>
      <tr><td>Arbutus</td><td>check</td><td></td></tr>
      <tr><td>Fir</td><td>warning</td>
          <td><a href="/view_incident?incident=1614">Filesystem problem</a></td></tr>
      <tr><td>Killarney</td><td>warning</td>
          <td><a href="/view_incident?incident=1648">Outage</a></td></tr>
    </table>
    """

    def test_collects_only_rows_with_an_incident_link(self):
        got = M.get_current_incident_urls(self.HOME)
        self.assertEqual(
            got,
            [
                ("Fir", "https://status.alliancecan.ca/view_incident?incident=1614"),
                (
                    "Killarney",
                    "https://status.alliancecan.ca/view_incident?incident=1648",
                ),
            ],
        )


class InferYearTests(unittest.TestCase):
    def test_picks_year_closest_to_reference(self):
        # Mid-January reference: "December 25" belongs to the prior year.
        ref = datetime(2026, 1, 15, tzinfo=TORONTO)
        self.assertEqual(M.infer_year(12, 25, ref), 2025)
        # Same reference, "February 1" belongs to the same year.
        self.assertEqual(M.infer_year(2, 1, ref), 2026)


class IncidentHelperTests(unittest.TestCase):
    def test_incident_id(self):
        self.assertEqual(
            M.incident_id("https://status.alliancecan.ca/view_incident?incident=1650"),
            1650,
        )

    def test_page_classifiers(self):
        self.assertTrue(M.is_incident_page("... Incident description ..."))
        self.assertFalse(M.is_incident_page("home page, no such marker"))
        self.assertTrue(M.is_scheduled_page("Planned Outage - Arrêt planifié"))
        self.assertFalse(M.is_scheduled_page("Filesystem problem - investigating"))


class ScanStateTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "last_scanned.txt")
            self.assertIsNone(M.read_scan_state(p))  # missing -> None
            M.write_scan_state(p, 1650)
            self.assertEqual(M.read_scan_state(p), 1650)

    def test_unparseable_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.txt")
            with open(p, "w") as f:
                f.write("not-a-number")
            self.assertIsNone(M.read_scan_state(p))


# Minimal incident pages, in the real page's element order (created block, then
# Title/Summary, then the Updated block), so parse_incident + the classifiers
# behave as they do on the live site.
_SCHEDULED_HTML = """
<p>Incident description</p>
<table><tr><th>h</th></tr>
<tr><td>Killarney</td><td>Closed</td><td></td><td></td></tr></table>
<p>Title</p><p>Planned Outage</p>
<p>Summary</p><p>Killarney maintenance on 2026-06-15 from 08:00 to 10:00.</p>
<p>Back</p>
"""

_UNPLANNED_HTML = """
<p>Incident description</p>
<table><tr><th>h</th></tr>
<tr><td>Fir</td><td>Open</td><td></td><td></td></tr></table>
<small>Created by X on
  <script>change_date_full("2026-06-15 09:00", "en")</script></small>
<p>Title</p><p>Network problem</p>
<p>Summary</p><p>Fir is unavailable, investigating.</p>
<i>Updated by X on
  <script>change_date_full("2026-06-15 09:00", "en")</script></i>
<p>Back</p>
"""


class ScanGapTests(unittest.TestCase):
    NOW = datetime(2026, 6, 20, 12, 0, tzinfo=TORONTO)
    PAGES = {
        "1": _SCHEDULED_HTML,
        "2": _UNPLANNED_HTML,
        "3": "<p>home stub, not an incident</p>",  # empty id
    }

    def _run(self, include_unplanned):
        def fake_fetch(url):
            return self.PAGES[url.split("=")[-1]]

        with (
            mock.patch.object(M, "fetch", fake_fetch),
            mock.patch.object(M.time, "sleep", lambda *_: None),
        ):
            return M.scan_gap([1, 2, 3], "America/Toronto", include_unplanned, self.NOW)

    def test_both_kinds_when_unplanned_included(self):
        got = self._run(include_unplanned=True)
        kinds = sorted(g["kind"] for g in got)
        self.assertEqual(kinds, ["scheduled", "unplanned"])  # id 3 skipped (empty)

    def test_unplanned_dropped_when_not_included(self):
        got = self._run(include_unplanned=False)
        self.assertEqual([g["kind"] for g in got], ["scheduled"])


if __name__ == "__main__":
    unittest.main()
