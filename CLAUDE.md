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
  (case-insensitive); `--calname` sets the calendar's display title. These let
  one script produce both the all-clusters feed and per-cluster feeds.
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
- Two merge guards protect the accumulated history: if `--merge-from` points at
  a file that exists but won't parse, the run aborts rather than overwrite it;
  and if the scrape returns zero incidents at all (fetch failed / layout
  changed), the merge aborts so future events aren't dropped as if cancelled. A
  per-cluster feed legitimately filtering to zero is fine — the guard keys off
  the unfiltered scrape count, not the post-filter one.

## Deployment
- Hosted as a public repo under the VectorInstitute GitHub org.
- `.github/workflows/outages.yml` runs daily (cron) + on manual dispatch, builds
  `public/outages.ics` (all clusters) and `public/killarney.ics` (Killarney
  only), and deploys via the official `upload-pages-artifact` / `deploy-pages`
  actions. The script is invoked once per feed, so the site is scraped twice per
  daily run — still well within polite limits.
- History lives on the `calendar-state` orphan branch (no shared history with
  `main`), which holds just `outages.ics` + `killarney.ics` (plus a README and a
  `.gitattributes` marking `*.ics -text` so CRLF line endings are byte-preserved
  — the iCal spec wants CRLF). Each run checks that branch out into `state/`,
  builds with `--merge-from state/<feed>.ics`, copies the merged result back, and
  commits + pushes it to `calendar-state` (only when it changed). This is the
  durable, version-controlled store of past events; the Pages CDN copy is just an
  output. The workflow needs `contents: write` for the commit-back.
- Pages source must be set to "GitHub Actions" in repo Settings -> Pages.
- Subscribe URLs (project site):
  - all clusters: https://vectorinstitute.github.io/<repo-name>/outages.ics
  - Killarney only: https://vectorinstitute.github.io/<repo-name>/killarney.ics
  (could differ if the org has a custom Pages domain).

## Run locally
    pip install -r requirements.txt
    python drac_outages_ics.py            # writes ./outages.ics (all clusters)
    python drac_outages_ics.py -o out.ics --tz America/Toronto
    python drac_outages_ics.py -o killarney.ics \
        --service Killarney --calname "Killarney Cluster Outages"
    python drac_outages_ics.py -o outages.ics \
        --merge-from outages.ics      # carry past events forward (in-place merge)

## Known caveats / open items
- Depends on the current status-page HTML layout; brittle if Cachet markup changes.
- Depends on outage dates being written parseably in incident summaries.
- Be a polite scraper: keep the schedule modest (daily is plenty). No official ToS feed.
- Org policy must permit public Pages; a custom org Pages domain would change the URL.
- Times are interpreted in America/Toronto (EST/EDT) by default; the site quotes
  several Canadian timezones, mapped in TZINFOS in the script.
