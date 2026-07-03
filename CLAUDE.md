# Alliance Canada Cluster Outage Calendar

## Goal
Automated iCalendar (`.ics`) feed of scheduled outages / maintenance for the
Digital Research Alliance of Canada (formerly Compute Canada) clusters, scraped
from https://status.alliancecan.ca/ and published via GitHub Pages so a calendar
app can subscribe by URL and refresh automatically.

## How it works
- `drac_outages_ics.py` fetches the status home page, collects every incident
  linked under the "Scheduled events" section, fetches each incident page, and
  extracts dates — from the structured Start/End fields when present, otherwise
  by parsing the free-text Summary (e.g. "June 22-25, 2026, starting at 4:00 AM
  EDT"). It writes `outages.ics` with stable per-incident UIDs so re-runs update
  events rather than duplicating them.
- The status site is a self-hosted Cachet instance. There is NO public RSS/iCal
  feed, which is why scraping is necessary. Incident date fields are often blank,
  so prose parsing is the fallback and matters.
- Incidents with no parseable date are omitted from the .ics (a build-time
  placeholder would show a bogus "now" slot; the real outage is at some unknown
  future time). They're still logged to the run output (and counted in the
  final summary as "N undated, omitted") so they're visible in CI logs.
- `--service NAME` filters to incidents whose service name contains NAME
  (case-insensitive) in its service name, title, OR summary; `--calname` sets
  the calendar's display title. These let one script produce both the
  all-clusters feed and per-cluster feeds. Matching the title/summary and not
  just the structured service field is deliberate: a multi-cluster outage (e.g.
  a SciNet maintenance that names Killarney among the systems it takes down) is
  often filed under a different service, and a per-cluster feed should still
  include it. `matches_service()` is the shared predicate.
- `--include-unplanned` (opt-in, default off) also emits *unplanned* outages, in
  addition to scheduled maintenance. These come from the home page's status
  table ("Current incidents" column) rather than the "Scheduled events" block.
  Unplanned incidents rarely have a parseable prose date, so they're dated from
  the incident page's own timestamps (`date_unplanned()`) — start = when it was
  created (parsed from the `change_date_full("YYYY-MM-DD HH:MM", ..)` script the
  page emits); end = its last update (≈ resolution) once it has one. With no
  resolution timestamp, the end depends on whether the incident is live: a
  *live* one (still on the status page, `ongoing=True`) is in progress, so its
  end is projected to *now + `DEFAULT_DURATION` (24h)* — past the present, so it
  doesn't read as already finished in a calendar (a bare "now" end sits in the
  viewer's past). It moves forward each daily run, and when it resolves and drops
  off the table the `--merge-from` carry-forward truncates it back to ~the
  resolution time (to the daily polling interval), the same lifecycle as a
  scheduled "in progress → truncate to now" event. As a further cue its title
  gets an `ONGOING_MARKER` ("(unresolved)") inserted after the `[service]` prefix
  (e.g. `[Fir] (unresolved) Filesystem problem` — kept off the end, where a long
  title would truncate it away), and its description opens with an `ONGOING_NOTE`
  ("Unresolved: This is an ongoing issue without a definitive end date.") that
  spells the same thing out in the body; the merge strips both the marker and the
  note when it finalizes the event (carried/truncated), since it's no longer live. A *past* one (`ongoing=False` — reached
  via the gap scan or backfill, already off the page, so already resolved) is
  not still running; its end is left unset so `build_calendar` applies the
  `DEFAULT_DURATION` (24h) rather than stretching a long-resolved outage to now.
  Every event carries `CATEGORIES:SCHEDULED` or `CATEGORIES:UNPLANNED` so a
  calendar can style or filter them. The deployed workflow does not pass this
  flag, so the published feeds stay scheduled-only unless that changes.
- `--scan-state FILE` enables the catch-up gap scan, which captures outages the
  once-a-day poll skips entirely: an incident created and resolved between two
  runs never appears on the home page, but its (sequential) id sits in the gap.
  FILE holds the last-scanned id; each run fetches the ids from there up to the
  highest one currently visible, classifies + dates each the same way the live
  scrape does (scheduled always kept; unplanned kept only with
  `--include-unplanned`), and joins them to the scrape before the `--service`
  filter and merge. It runs on the unfiltered scrape, so a per-cluster feed
  still sees the whole gap. The new high-water id is written back to FILE only
  after a successful build. A missing FILE bootstraps silently to the current
  max (no history crawl — that's `backfill_historic.py`); `SCAN_CAP` (200) bounds
  one run so a reset FILE can't trigger a full crawl. Limitation: an outage
  whose id exceeds every currently-visible id (created after the newest visible
  incident, then resolved) is caught next run, once a higher visible id appears.
  The deployed job runs all feeds from ONE scrape (`build_feeds.py`) with one
  shared `--scan-state` (`state/scan-state.txt` on `calendar-state`), so the gap
  scan runs once for the whole run. (The standalone `drac_outages_ics.py
  --scan-state` still takes a per-invocation file; only give separate invocations
  separate files, since each would advance the mark.)
- `backfill_historic.py` is a one-off (not part of the daily job) that crawls the
  full incident-id range (252..1644), caching one page per id so re-runs don't
  re-fetch. It reuses this module's parsing, anchors each event's year to its
  created date (so undated-year events from 2019.. don't collapse onto the
  current year), and dedups the site's edit re-publications (a new incident id is
  minted on every edit) by service + identical start/end day. It writes
  `historic.ics` + `historic_killarney.ics`, and also honours `--include-unplanned`.
- `--merge-from PREV.ics` carries history forward. The status page drops events
  once they're over, so without this a past outage vanishes from the feed. With
  it, the script reads the previous published `.ics` and, for each event missing
  from the fresh scrape, decides by where *now* sits relative to the event:
  already finished (`end <= now`) → keep as-is; in progress (`start <= now <
  end`) → keep but truncate the end to now (it vanished mid-window, so the
  maintenance presumably finished early); still upcoming (`now < start`) → drop
  it (vanished while future ⇒ cancelled). Events still in the scrape are left to
  the fresh data, so reschedules / end-time changes update. Output is sorted by
  start time. Carried events keep their original `DTSTAMP`; only live events get
  a fresh stamp, so the file still changes (and commits) on most daily runs.
  The merge matches a previous event to the fresh scrape by UID (= incident id).
  Because the site mints a *new* id when an incident is edited, an ongoing
  outage re-published between runs would otherwise be carried forward under its
  old id *and* re-added under the new one — a duplicate. So the merge also drops
  a previous event as "superseded" when the fresh scrape already holds one with
  the same `(service, start-day)` (`_content_key`), even though its UID differs.
  This is a day-granularity heuristic: re-published copies keep the same service
  and start day (their `created` timestamps sit minutes apart), but two genuinely
  distinct same-service, same-day outages would be collapsed — an accepted, rare
  trade-off. Future scheduled events self-heal without it (a re-id'd future event
  vanishes while still future, so it's dropped as cancelled); the content check
  matters for in-progress / just-finished events, which are carried, not dropped.
- Two merge guards protect the accumulated history: if `--merge-from` points at
  a file that exists but won't parse, the run aborts rather than overwrite it;
  and if the scrape returns zero incidents at all (fetch failed / layout
  changed), `build_feed` preserves the previous feed file unchanged rather than
  merging an empty scrape (which would drop future events as if cancelled). A
  per-cluster feed legitimately filtering to zero is fine — the guard keys off
  the pre-filter scrape count (total for a full feed, scheduled-only for a
  planned feed), not the post-service-filter one.

## Deployment
- Hosted as a public repo under the VectorInstitute GitHub org.
- `.github/workflows/outages.yml` runs hourly (cron `0 * * * *`) + on
  manual dispatch. It calls `build_feeds.py`, which scrapes the site **once** and
  writes all four feeds from that shared incident set, then deploys via the
  official `upload-pages-artifact` / `deploy-pages` actions. Two are *full* feeds
  (scheduled + unplanned): `public/outages.ics` (all clusters) and
  `public/killarney.ics` (Killarney). Two are *planned-only* (scheduled
  maintenance): `public/outages-planned-only.ics` and
  `public/killarney-planned-only.ics`. One scrape per run × 24 runs/day — well
  within polite limits. (`build_feeds.py` holds the feed list and reuses
  `drac_outages_ics.scrape_incidents` / `build_feed`; the `drac_outages_ics.py`
  CLI still builds a single feed for local/manual use.)
- History lives on the `calendar-state` orphan branch (no shared history with
  `main`), which holds the feed `.ics` files and `scan-state.txt` (the gap-scan
  high-water id), plus a README and a `.gitattributes` marking `*.ics -text` so
  CRLF line endings are byte-preserved — the iCal spec wants CRLF. Each run
  checks that branch out into `state/`, builds with `--merge-from state/<feed>.ics`
  and one shared `--scan-state state/scan-state.txt`, copies the merged result
  back, and commits + pushes the whole `state/` (feeds + scan-state) to
  `calendar-state` (only when it changed). This is the durable, version-controlled
  store of past events; the Pages CDN copy is just an output. The branch was
  seeded from the historic backfill (`backfill_historic.py` regenerated from the
  cache), so the feeds start with the full 2019→now history; the gap scan then
  keeps them as complete as a fresh backfill — accumulating each new sub-day
  outage over time rather than ever re-crawling. The workflow needs
  `contents: write` for the commit-back.
- Pages source must be set to "GitHub Actions" in repo Settings -> Pages.
- Subscribe URLs (project site), `https://vectorinstitute.github.io/<repo-name>/`:
  - all clusters, full: `outages.ics` — scheduled only: `outages-planned-only.ics`
  - Killarney, full: `killarney.ics` — scheduled only: `killarney-planned-only.ics`
  (could differ if the org has a custom Pages domain).

## Run locally
    pip install -r requirements.txt
    python drac_outages_ics.py            # writes ./outages.ics (all clusters)
    python drac_outages_ics.py -o out.ics --tz America/Toronto
    python drac_outages_ics.py -o killarney.ics \
        --service Killarney --calname "Killarney Cluster Outages"
    python drac_outages_ics.py -o outages.ics \
        --merge-from outages.ics      # carry past events forward (in-place merge)
    python build_feeds.py --out-dir public --state-dir state   # all feeds, one scrape

## Known caveats / open items
- Depends on the current status-page HTML layout; brittle if Cachet markup changes.
- Depends on outage dates being written parseably in incident summaries.
- Be a polite scraper: keep the schedule modest. Currently hourly (24 runs/day,
  one scrape per run via `build_feeds.py`); don't push it much higher without a
  reason — there's no official ToS feed.
- Org policy must permit public Pages; a custom org Pages domain would change the URL.
- Timezone handling for a time's zone, in precedence order: (1) an explicit zone
  written in the summary (EDT, CST, PT, ...) mapped via TZINFOS/`TZ_ZONE`;
  (2) otherwise the cluster's home zone inferred from the service (`CLUSTER_TZ` /
  `cluster_tz()` — e.g. Fir/Cedar/Arbutus → Pacific, Vulcan → Mountain, the ON/QC
  clusters → Eastern); (3) otherwise the calendar default, America/Toronto
  (EST/EDT), also used for national / multi-site services (DRAC, FRDR, ...). The
  cluster inference is applied in `build_calendar` to any still-naive datetime,
  so it covers bare prose times (e.g. "4-8PM today") and zone-less structured
  fields alike. Cluster→zone entries are best-effort by host site.
