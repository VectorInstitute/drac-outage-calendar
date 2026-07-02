# DRAC Outage Calendar

A subscribable iCalendar (`.ics`) feed of scheduled outages and maintenance for
the clusters of the [Digital Research Alliance of Canada][drac] (the Alliance de
recherche numérique du Canada, formerly Compute Canada).

The Alliance status page at <https://status.alliancecan.ca/> lists scheduled
events but offers no RSS or calendar feed to subscribe to. This project
scrapes that page once a day, extracts the date and time of each scheduled
outage, and publishes the result as an `.ics` file on GitHub Pages — so you can
subscribe in your calendar app once and have outages show up (and stay up to
date) automatically. Past outages are kept on the calendar as a record even
after they drop off the status page.

[drac]: https://alliancecan.ca/

## Calendars

Two feeds are published:

| Calendar | Subscribe URL |
| --- | --- |
| **All clusters** | `https://vectorinstitute.github.io/drac-outage-calendar/outages.ics` |
| **Killarney only** | `https://vectorinstitute.github.io/drac-outage-calendar/killarney.ics` |

Both refresh daily.

## How to subscribe

### Google Calendar (must be done in a desktop browser)

The Google Calendar **mobile app cannot add a calendar by URL** — do this once
on a computer and it will then sync to your phone automatically.

1. Open the [add-by-URL page][addbyurl] directly, **or** in Google Calendar
   click **Other calendars** → **+** → **From URL**.
2. Paste the URL of the calendar you want (all clusters or Killarney only, from
   the table above).
3. Click **Add calendar**. It appears under "Other calendars" and syncs to all
   your devices.

[addbyurl]: https://calendar.google.com/calendar/u/0/r/settings/addbyurl

> **Don't use "Import."** Import does a one-time copy that never updates. Use
> **From URL** so the calendar stays in sync.

### Apple Calendar (macOS / iOS)

**File → New Calendar Subscription…** (macOS) or **Settings → Calendar →
Accounts → Add Account → Other → Add Subscribed Calendar** (iOS), then paste the
URL.

### Outlook

**Add calendar → Subscribe from web**, paste the URL, and give it a name.

## A note on refresh timing

Calendar apps cache subscribed feeds and refresh them on **their own schedule** —
typically a few hours, and sometimes up to ~24 hours for Google Calendar. So:

- A newly added calendar may take a while to first show events.
- Changes (or this feed's own daily rebuild) won't appear instantly.

This is the calendar app's behaviour, not the feed — the published `.ics` is
regenerated daily and is always current.

## Scope

Only clusters listed on the Alliance status page are covered. Systems that
aren't on that page (for example, Bon Echo and other internal clusters that
aren't part of DRAC) won't appear here.
Incidents whose summary has no parseable date are omitted rather than shown at
a misleading placeholder time.

## How it works

- A GitHub Actions workflow (`.github/workflows/outages.yml`) runs daily and
  on manual dispatch.
- `drac_outages_ics.py` fetches the status home page, follows each incident
  linked under "Scheduled events", and reads the date/time — from the structured
  Start/End fields when present, otherwise by parsing the free-text summary
  (no-year dates, ISO dates, ranges, and several Canadian time zones are all
  handled).
- Each incident gets a stable UID, so re-runs update existing events instead of
  creating duplicates.
- Past outages are kept. The status page drops events once they're over, but the
  feed carries an elapsed event forward so it stays on your calendar as a record
  of what happened. (An event that disappears while still in the future is
  treated as cancelled and removed.) The accumulated history is stored on a
  separate `calendar-state` branch, which the workflow reads and updates each
  run.
- The workflow builds `outages.ics` (all clusters) and `killarney.ics`
  (filtered to Killarney) and deploys them to GitHub Pages.

## Run locally

```bash
pip install -r requirements.txt

# All clusters -> ./outages.ics
python drac_outages_ics.py

# A single cluster, with a custom calendar title
python drac_outages_ics.py -o killarney.ics \
    --service Killarney --calname "Killarney Cluster Outages"
```

Options: `-o/--output` (output path), `--tz` (IANA time zone for times given
without one, default `America/Toronto`), `--service` (case-insensitive filter
that keeps incidents mentioning the string in their service, title, or summary,
so a per-cluster feed also catches multi-cluster outages filed under another
service), `--calname` (calendar display title), `--merge-from` (a previous
`.ics` whose elapsed events are carried into the new one), `--include-unplanned`
(also add current *unplanned* outages from the status table, dated by the
incident's created/updated timestamps and tagged `CATEGORIES:UNPLANNED`;
scheduled maintenance is tagged `SCHEDULED`), `--scan-state FILE` (catch-up scan
for sub-day outages — see below).

By default only scheduled maintenance is published. `--include-unplanned` is
opt-in because unplanned outages are dated only approximately (from when the
incident was posted and last updated). An outage that is still ongoing is shown
as running until "now" and extends each daily run, with an "(unresolved)" tag
inserted after the "[service]" prefix in its title (e.g. `[Fir] (unresolved)
Filesystem problem`) so it doesn't read as already finished; once it resolves
and drops off the status page, the carried-forward copy drops the tag and
freezes at its last-seen time. A past outage that has no recorded resolution time is given a
default 24-hour duration instead of being stretched to the scan time.

Because the scrape runs once a day, an outage that appears and clears between
two runs is never seen on the status page. `--scan-state FILE` closes that gap:
it remembers the last incident id it examined in FILE and, each run, fetches the
newer ids up to the highest one currently visible — catching those short
outages — then records the new high-water id. Each feed needs its own state
file. It's bounded (at most 200 ids per run) and bootstraps from the current max
on first use, so it never crawls the whole history.
