# calendar-state branch

Machine-managed state for the **DRAC Outage Calendar**. This orphan branch is
**not** part of the project source and shares no history with `main`.

The GitHub Actions build workflow reads the `.ics` files here at the start of
each run, merges in the latest scrape — carrying forward events that have
already elapsed so they aren't lost when they drop off the status page — and
commits the updated files back here before deploying them to GitHub Pages.

Do not edit these files by hand; they will be overwritten by the next run.

- `outages.ics` — all clusters
- `killarney.ics` — Killarney only
