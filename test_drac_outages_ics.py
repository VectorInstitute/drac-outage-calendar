#!/usr/bin/env python3
"""Unit tests for drac_outages_ics.

No network: every test builds its inputs in memory. Run with

    python -m unittest

(or `pytest`, which also discovers unittest.TestCase classes).
"""

import unittest
from datetime import date, datetime, timedelta
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
        prev = _calendar(_event(
            "100",
            datetime(2026, 6, 20, 9, 0, tzinfo=TORONTO),
            datetime(2026, 6, 20, 17, 0, tzinfo=TORONTO),
        ))
        fresh = _calendar()  # scrape no longer lists it
        carried, truncated, dropped = M.merge_history(
            fresh, prev, "America/Toronto", now=self.NOW)
        self.assertEqual((carried, truncated, dropped), (1, 0, 0))
        self.assertIn("drac-incident-100@status.alliancecan.ca", _uids(fresh))

    def test_future_event_is_dropped_as_cancelled(self):
        prev = _calendar(_event(
            "200",
            datetime(2026, 7, 15, 9, 0, tzinfo=TORONTO),
            datetime(2026, 7, 15, 17, 0, tzinfo=TORONTO),
        ))
        fresh = _calendar()
        carried, truncated, dropped = M.merge_history(
            fresh, prev, "America/Toronto", now=self.NOW)
        self.assertEqual((carried, truncated, dropped), (0, 0, 1))
        self.assertEqual(_uids(fresh), set())

    def test_in_progress_event_is_truncated_to_now(self):
        prev = _calendar(_event(
            "300",
            datetime(2026, 6, 28, 0, 0, tzinfo=TORONTO),   # started before now
            datetime(2026, 7, 5, 0, 0, tzinfo=TORONTO),    # would end after now
        ))
        fresh = _calendar()
        carried, truncated, dropped = M.merge_history(
            fresh, prev, "America/Toronto", now=self.NOW)
        self.assertEqual((carried, truncated, dropped), (0, 1, 0))
        ev = next(iter(fresh.walk("VEVENT")))
        self.assertEqual(M._as_dt(ev.get("dtend").dt, TORONTO), self.NOW)

    def test_scraped_event_is_left_to_fresh_data(self):
        # Same UID in both: the previous (stale) copy must NOT be merged in;
        # the fresh event stays as-is so reschedules win.
        stale = _event("400",
                       datetime(2026, 7, 12, 7, 0, tzinfo=TORONTO),
                       datetime(2026, 7, 13, 16, 0, tzinfo=TORONTO))
        fresh_ev = _event("400",
                          datetime(2026, 7, 12, 7, 0, tzinfo=TORONTO),
                          datetime(2026, 7, 13, 12, 0, tzinfo=TORONTO))  # new end
        prev = _calendar(stale)
        fresh = _calendar(fresh_ev)
        carried, truncated, dropped = M.merge_history(
            fresh, prev, "America/Toronto", now=self.NOW)
        self.assertEqual((carried, truncated, dropped), (0, 0, 0))
        ends = [M._as_dt(ev.get("dtend").dt, TORONTO)
                for ev in fresh.walk("VEVENT")]
        self.assertEqual(ends, [datetime(2026, 7, 13, 12, 0, tzinfo=TORONTO)])

    def test_mixed_scenario_counts(self):
        prev = _calendar(
            _event("a", datetime(2026, 6, 20, 9, 0, tzinfo=TORONTO),
                   datetime(2026, 6, 20, 17, 0, tzinfo=TORONTO)),   # elapsed
            _event("b", datetime(2026, 6, 28, 0, 0, tzinfo=TORONTO),
                   datetime(2026, 7, 5, 0, 0, tzinfo=TORONTO)),     # in progress
            _event("c", datetime(2026, 7, 15, 9, 0, tzinfo=TORONTO),
                   datetime(2026, 7, 15, 17, 0, tzinfo=TORONTO)),   # future
        )
        fresh = _calendar()
        self.assertEqual(
            M.merge_history(fresh, prev, "America/Toronto", now=self.NOW),
            (1, 1, 1))


class SortEventsTests(unittest.TestCase):
    def test_events_sorted_by_start(self):
        cal = _calendar(
            _event("late", datetime(2026, 8, 1, tzinfo=TORONTO),
                   datetime(2026, 8, 2, tzinfo=TORONTO)),
            _event("early", datetime(2026, 5, 1, tzinfo=TORONTO),
                   datetime(2026, 5, 2, tzinfo=TORONTO)),
        )
        M.sort_events(cal, "America/Toronto")
        starts = [ev.get("dtstart").dt for ev in cal.walk("VEVENT")]
        self.assertEqual(starts, sorted(starts))


class ProseDateTests(unittest.TestCase):
    REF = datetime(2026, 6, 30, tzinfo=TORONTO)

    def test_iso_range(self):
        start, end = M.parse_dates_from_prose(
            "Nibi will be unavailable from 2026-07-12 7AM to 2026-07-13 12PM.",
            ref=self.REF)
        self.assertEqual(start, datetime(2026, 7, 12, 7, 0))
        self.assertEqual(end, datetime(2026, 7, 13, 12, 0))

    def test_single_day_time_range_with_tz(self):
        start, end = M.parse_dates_from_prose(
            "FRDR maintenance May 27 (2:00 PM - 2:30 PM CST).", ref=self.REF)
        self.assertEqual(start.replace(tzinfo=None), datetime(2026, 5, 27, 14, 0))
        self.assertEqual(start.tzinfo, ZoneInfo("America/Winnipeg"))
        self.assertEqual(end.replace(tzinfo=None), datetime(2026, 5, 27, 14, 30))

    def test_multi_day_range_with_year(self):
        start, end = M.parse_dates_from_prose(
            "Outage June 22-25, 2026 starting at 4:00 AM EDT.", ref=self.REF)
        self.assertEqual(start.replace(tzinfo=None), datetime(2026, 6, 22, 4, 0))
        self.assertEqual(start.tzinfo, ZoneInfo("America/Toronto"))
        # A multi-day range spans through the end of the last day.
        self.assertEqual(end, datetime(2026, 6, 26, 0, 0))

    def test_unparseable_returns_none(self):
        self.assertEqual(M.parse_dates_from_prose("no date here", ref=self.REF),
                         (None, None))


class InferYearTests(unittest.TestCase):
    def test_picks_year_closest_to_reference(self):
        # Mid-January reference: "December 25" belongs to the prior year.
        ref = datetime(2026, 1, 15, tzinfo=TORONTO)
        self.assertEqual(M.infer_year(12, 25, ref), 2025)
        # Same reference, "February 1" belongs to the same year.
        self.assertEqual(M.infer_year(2, 1, ref), 2026)


if __name__ == "__main__":
    unittest.main()
