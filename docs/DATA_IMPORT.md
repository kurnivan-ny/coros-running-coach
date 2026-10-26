# Importing your COROS data

COROS has no public API this project can call on a schedule. Its **MCP server**
only runs inside a Claude session, so real data enters in two steps: a Claude
session saves the data, then the pipeline loads it. The saved files hold private
athlete data (GPS start points, heart rate) and are **gitignored**.

```
Claude session ── querySportRecords ──► data/coros_raw/sport_records_<year>.txt
                                              │
              python -m corach.ingest ────────┘   (bronze, Neon)
              python -m corach.transform_run      (silver + gold, dbt)
```

## Refresh the snapshot (about a minute)

Open Claude Code in this repository (with the COROS connector enabled) and ask:

> Refresh my COROS snapshot for this year and load it.

Claude calls `querySportRecords` with all sport types, saves the response as
`data/coros_raw/sport_records_<year>.txt`, then runs the two commands below. To
do it yourself after the files exist:

```powershell
$env:PYTHONPATH = "src"
python -m corach.ingest --source coros_snapshot     # bronze (incremental, idempotent)
python -m corach.transform_run                      # dbt: silver + gold + tests
```

(`bash`: `PYTHONPATH=src python -m corach.ingest ...`.) Set `DATABASE_URL`, or the
`PGHOST/PGUSER/PGPASSWORD/PGDATABASE` variables, in `.env` first; both commands load
`.env` from the current directory (variables already set in your shell win).

Re-running is safe: unchanged activities do not touch bronze, and a later file
with the same activity id supersedes an earlier one.

## Alternative: Strava (unattended)

COROS can sync every workout to Strava (COROS app → profile → connected apps). The
Strava API is an ordinary web API, so GitHub Actions can pull it daily with no Claude
session, which removes the main manual step above.

**Trade-offs.** Strava's activity list has no calories, no strength sets, and none of
COROS's own metrics; those fields stay NULL. API limits are about 100 requests per 15
minutes and 1,000 per day, plenty for a daily sync. Read Strava's current API agreement
before sending Strava data to an LLM engine.

**Set up once**

1. Create an API app at <https://www.strava.com/settings/api> (category anything,
   callback domain `localhost`). Put `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env`.
2. Authorise it with the helper. **Do not copy the refresh token shown on Strava's page:**
   it has `read` scope only and cannot list activities.

   ```powershell
   $env:PYTHONPATH = "src"
   python -m corach.strava_auth                       # prints the URL; open it, click Authorize
   python -m corach.strava_auth "<address bar URL>"   # the page fails to load; copy its address
   ```

   The helper saves `STRAVA_REFRESH_TOKEN` to `.env` and prints only the scopes granted.
   Codes are single-use and expire in minutes.
3. Load it: `python -m corach.ingest --source strava`, then `python -m corach.transform_run`.

**Both sources at once.** The same run then exists under two ids. Silver keeps one copy:
COROS wins, and the Strava copy lands in `silver_rejects` as `duplicate_activity` (same
sport group, start times within two minutes). Nothing is dropped silently.

**Daily and unattended.** Add `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` and
`STRAVA_REFRESH_TOKEN` as GitHub Actions secrets, then set the repository **variable**
`STRAVA_ENABLED` to `true`. The workflow then syncs before each dbt build.

**Token rotation.** Strava may issue a new refresh token on refresh. Locally the new one is
written to `.env`. On GitHub a workflow cannot update its own secret, so the log shows a
`::warning::`; update `STRAVA_REFRESH_TOKEN` and the next run works. If the old token stops
working, re-run step 2 and update the secret.

**If it returns 403 `Application ... Inactive`.** That is Strava refusing the *app*, not a
problem with scope or token, and nothing in this repository can fix it. Check the app's
status on the settings page and contact Strava developer support.

## What the snapshot contains

Verified against 443 real records (2025-03-13 to 2026-10-25):

| Field | Notes |
|---|---|
| activity id, sport type/name | COROS `LabelId` and `SportType` (100 outdoor run, 101 indoor run, 402 strength, 900 walk, 300/301 swim, ...) |
| local date, start/end time | The date is the athlete's local calendar date; times are Unix seconds (UTC) |
| duration | Workout time. It can be shorter than end minus start when the watch was paused (110 of 443 records) |
| distance | Kilometres or metres, converted to metres. Absent for strength |
| sets | Strength only |
| average heart rate, calories, location | Start coordinates are kept in bronze only |

**Not in the snapshot** (not returned by the list call): cadence, stride length,
ascent, COROS training load, aerobic/anaerobic training effect and training focus.
They exist per activity via `getActivityDetail` (one call each), which is not
imported yet. Load in this project is computed from heart rate instead (see the
README's metric definitions).

## Sleep, HRV and resting heart rate

Pulled and inspected, **not imported**. For this account COROS returned almost no
usable data: sleep detail for 7 nights across 19 months (most days say "not
available yet", several carry placeholder scores of 0 or -1 and a 1982 date),
resting heart rate and sleep HRV empty for the last week. Charting that would be
mostly empty. Daily steps, calories and stress are available for about 400 days
and can be added the same way when wanted. Wear the watch overnight and the sleep
tables become worth building.

## FIT files

Not implemented. Exporting FIT files from COROS and parsing them with the free
`fitparse` library would give per-second detail and work with no Claude session,
but no FIT file was available to verify a parser against, and guessing a binary
format would risk inventing data. The `Source` interface in
`src/corach/sources/__init__.py` is all a FIT source needs to implement: yield
`coros_record_v1` payloads (see `coros_snapshot.py`) and the rest of the pipeline
is unchanged.

## Removing synthetic data

If demo rows were loaded earlier, delete them (they carry `source = 'synthetic'`):

```sql
DELETE FROM bronze_activities WHERE source = 'synthetic';
```

then run `python -m corach.transform_run` to rebuild silver and gold.
