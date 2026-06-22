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
- Incidents with no parseable date still appear, flagged "date TBD - check link".

## Deployment
- Hosted as a public repo under the VectorInstitute GitHub org.
- `.github/workflows/outages.yml` runs daily (cron) + on manual dispatch, builds
  `public/outages.ics`, and deploys via the official `upload-pages-artifact` /
  `deploy-pages` actions.
- Pages source must be set to "GitHub Actions" in repo Settings -> Pages.
- Subscribe URL (project site): https://vectorinstitute.github.io/<repo-name>/outages.ics
  (could differ if the org has a custom Pages domain).

## Run locally
    pip install -r requirements.txt
    python drac_outages_ics.py            # writes ./outages.ics
    python drac_outages_ics.py -o out.ics --tz America/Toronto

## Known caveats / open items
- Depends on the current status-page HTML layout; brittle if Cachet markup changes.
- Depends on outage dates being written parseably in incident summaries.
- Be a polite scraper: keep the schedule modest (daily is plenty). No official ToS feed.
- Org policy must permit public Pages; a custom org Pages domain would change the URL.
- Times are interpreted in America/Toronto (EST/EDT) by default; the site quotes
  several Canadian timezones, mapped in TZINFOS in the script.
