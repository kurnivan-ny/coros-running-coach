# COROS Running Coach

A personal training platform for COROS data: a **bronze → silver → gold** pipeline on
Neon Postgres, a Streamlit dashboard covering every sport (running, strength, walking,
swimming, cycling), workload monitoring, a rule-based training plan, and a
"chat with your data" coach that cannot make numbers up.

- **Bronze** (Python): raw COROS records land in Neon, deduplicated by content hash.
- **Silver** (dbt): typed, validated activities; bad rows are quarantined with a reason.
- **Gold** (dbt): activities with training load, a daily load series, weekly summaries.
- **App** (Streamlit + Plotly): eight tabs reading gold. **Coach**: whitelisted read-only
  tools, free by default, optional Claude API or local LLM.

Everything runs on free tiers. Costs and limits are listed [below](#costs-and-free-tier-limits).

---

## Status in one line

**161 tests pass**, ruff is clean, bandit reports no medium/high issues and pip-audit
finds no known vulnerabilities. The real pipeline has run end to end on **443 real COROS
activities** (2025-03-13 → 2026-10-25) in Neon, and the gold numbers were reconciled
against the raw files. See [Verification status](#verification-status) for exactly what
was and was not run.

---

## Architecture

<figure>
<img src="docs/pipeline.svg" alt="COROS data is saved from a Claude session as text snapshots; Python ingestion upserts raw payloads into bronze in Neon only when the payload hash changes; dbt builds silver and gold; whitelisted read-only tools, a deterministic coach and an optional number-guarded LLM feed the Streamlit dashboard." width="780">
<figcaption>Python owns bronze; dbt owns silver and gold. Bad rows are quarantined with a reason, never dropped or coerced.</figcaption>
</figure>

**Why this split.** Fetching, retrying, deduplicating and logging are imperative work,
so Python does them. Typing, validating and aggregating are set operations, so dbt does
them, with tests that run on every build (uniqueness, accepted values, and checks that
every bronze row lands in exactly one of silver or quarantine and that weekly and daily
totals equal the activity totals).

**Where each piece runs.** The COROS MCP server only runs inside a Claude session, so
real data enters from your machine ([docs/DATA_IMPORT.md](docs/DATA_IMPORT.md)). GitHub
Actions then rebuilds silver and gold on a schedule; Streamlit Cloud only reads gold.

<figure>
<img src="docs/deployment.svg" alt="GitHub Actions runs dbt build against Neon Postgres; Streamlit Community Cloud reads Neon through the pooled endpoint and optionally calls a hosted LLM over https, sending aggregates only." width="760">
<figcaption>Every component is free tier. Neon sleeps and wakes itself; Streamlit sleeps after about 12 hours and wakes on visit.</figcaption>
</figure>

### Layout

| Path | Purpose |
|---|---|
| `src/corach/sources/coros_snapshot.py` | Parses saved COROS `querySportRecords` output into `coros_record_v1` payloads |
| `src/corach/sources/strava.py` | Strava API source: OAuth refresh, pagination, rate-limit handling, sport mapping |
| `src/corach/strava_auth.py` | One-time Strava authorisation helper (saves the token to `.env`, prints no secrets) |
| `src/corach/sources/synthetic.py` | Multi-sport demo generator (clearly labelled), used by CI |
| `src/corach/ingest.py` | Incremental, idempotent bronze load; retries; run log |
| `src/corach/store.py` | Neon/Postgres (and SQLite for tests); bronze + ops tables; migrations |
| `src/corach/transform_run.py` | Runs `dbt build` from the same `DATABASE_URL` / `PG*` config |
| `src/corach/envfile.py` | `.env` loader for the CLIs |
| `dbt/models/silver/` | `silver_activities`, `silver_rejects` (+ `stg_activities` view) |
| `dbt/models/gold/` | `fct_activities`, `daily_load`, `weekly_summary` |
| `dbt/tests/` | Reconciliation tests |
| `src/corach/coach/tools.py` | 9 whitelisted read-only tools over gold |
| `src/corach/coach/plan.py` | Rule-based weekly plan (pure functions) |
| `src/corach/coach/chat.py` | Router, deterministic answers, number guard |
| `src/corach/coach/claude.py` | Optional Claude API with secure tool calling |
| `src/corach/coach/llm.py` | Optional OpenAI-compatible client (Ollama, Cloudflare, ...) |
| `app.py` | Streamlit dashboard |

---

## Quickstart

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements-dev.txt          # includes dbt-postgres
$env:PYTHONPATH = "src"

pytest -q                                    # 161 tests, no database needed
```

Copy `.env.example` to `.env` and set your Neon connection (`DATABASE_URL`, or
`PGHOST/PGUSER/PGPASSWORD/PGDATABASE`). Then:

```powershell
python -m corach.ingest --source coros_snapshot   # bronze  (needs data/coros_raw/*.txt)
python -m corach.transform_run                    # silver + gold + dbt tests
streamlit run app.py
```

To try it without COROS data: `python -m corach.ingest --source synthetic`, then the
same two commands. The dashboard labels synthetic data in red.

Getting your COROS data in is covered in **[docs/DATA_IMPORT.md](docs/DATA_IMPORT.md)**;
deploying on free tiers in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

### CLI

```bash
python -m corach.ingest --source coros_snapshot   # incremental (watermark + 3-day overlap)
python -m corach.ingest --source strava           # optional, unattended-capable
python -m corach.ingest --full-refresh            # ignore the watermark
python -m corach.transform_run                    # dbt build (models + tests)
python -m corach.transform_run run                # models only
python -m corach.transform_run build --vars '{max_hr: 195}'   # override an assumption
```

Exit codes are non-zero on failure; an ingest failure is recorded in
`ops_pipeline_runs` before being re-raised.

### Configuration

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres DSN (Neon **pooled** host, `?sslmode=require`). Quotes are tolerated |
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` | Alternative to `DATABASE_URL` |
| `CORACH_SQLITE_PATH` | SQLite file for bronze-only work and tests (silver/gold need Postgres) |
| `CORACH_SOURCE` | `coros_snapshot` (default), `strava` or `synthetic` |
| `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN` | Optional Strava source ([setup](docs/DATA_IMPORT.md#alternative-strava-unattended)) |
| `CORACH_SNAPSHOT_DIR` | Where saved COROS files live (default `data/coros_raw`) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | Optional Claude tool calling (paid API) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | Optional OpenAI-compatible rephrasing (free options) |

---

## The dashboard

| Tab | Shows |
|---|---|
| **Overview** | 7/28-day run km, all-sport sessions, load ratio; weekly hours stacked by sport; recent activities |
| **Running** | Weekly km, pace and heart-rate trends, longest run, week-over-week change |
| **Strength & other** | Weekly minutes for strength, walking, swimming, cycling; all-time totals |
| **Workload** | Acute vs chronic load, acute:chronic ratio with its safe band, monotony, strain |
| **Plan** | Next seven days built from your recent volume and load; settings in the sidebar |
| **Sleep & recovery** | Honest status: see [below](#what-is-not-built) |
| **Ask your data** | Chat with three engines: rule-based (free), LLM phrasing, Claude with tools |
| **Data quality** | Quarantine reasons, flagged-but-kept rows, recent ingest runs |

---

## Metric definitions

These are deliberate choices. The obvious alternative is wrong in each case.

**Dates are the athlete's local calendar date** (as COROS reports it), so a 6 a.m.
Jakarta run belongs to that day, not the previous UTC day. **Weeks** are ISO weeks
starting Monday on that local date.

**Pace** = *total duration ÷ total distance* over the period, **not** the mean of
per-activity paces, which over-weights short runs. 1 km at 6:00 plus 9 km at 5:00 is
3060 s over 10 km = **5:06/km**; averaging the two paces gives 5:30. Pace is only
reported for running, walking, hiking and cycling.

**Average heart rate** is **duration-weighted** across only the activities that report
heart rate. Activities without HR are excluded from numerator and denominator, never
counted as zero.

**Missing values stay NULL.** A missing distance, HR or load is NULL in silver and gold
and renders as `—`, never `0`. A reported `0` is a different thing: the validator rejects
a zero-distance run as implausible.

**Duration** is COROS workout time. It can be shorter than end minus start when the watch
was paused.

**Load (TRIMP, Banister).** `minutes × HRr × 0.64 × e^(1.92 × HRr)` with
`HRr = (avg_hr − rest_hr) / (max_hr − rest_hr)` clamped to 0–1. Resting HR is 45 (the
value COROS reports for this athlete). **Maximum HR is an estimate (193 = 220 − age 27); set your
measured value** in `dbt/dbt_project.yml` and rebuild. The 0.64/1.92 constants are the
male values; use 0.86/1.67 for female. It uses *average* heart rate, so it understates
interval and strength sessions. An activity without HR gets NULL load, not zero.

**Acute / chronic load.** Mean daily load over the last 7 and 28 days; rest days count
as zero. **ACWR** = acute ÷ chronic, shown only after 28 days of history.
Zones: below 0.8 *below usual load*, 0.8–1.3 *optimal*, 1.3–1.5 *caution*, above 1.5
*high risk*. These are widely used heuristics, not clinical thresholds, and the research
behind ACWR is debated; treat it as a prompt to look at your week, not a verdict.
(Cross-check: on the day it was built the ratio here was 0.80 and COROS's own load
ratio was 0.83.)

**Monotony** = 7-day mean ÷ standard deviation of daily load; **strain** = 7-day load ×
monotony. High monotony means the same load every day.

**Week-over-week** compares the last two *complete* weeks, and is undefined (not
infinite) when the earlier week had no distance. The current partial week is reported
separately.

**Rolling windows** (7/28/90 days) carry `sufficient_history = false` when the data is
shorter than the window, so a 28-day figure built from 3 days is never presented as
complete.

**Two sources, one copy.** If the same workout arrives from COROS and from Strava, silver keeps the COROS copy and quarantines the other as `duplicate_activity` (same sport group, start within two minutes). Strava rows have no calories or strength sets; those stay NULL.

**Data flagged, not dropped.** Runs slower than 12:00/km are kept but flagged
(`very_slow_pace`) and listed on the Data quality tab, because they may be walking or
standing time recorded as a run.

---

## Training plan rules

`coach/plan.py` is a pure function of your recent training; it never invents history.

- **Weekly distance** = mean of your last four *complete* weeks × **1.10** (the "10% rule"),
  × 1.00 in the caution zone, × 0.75 at high risk. A race inside two weeks tapers it
  (×0.7), and race week is ×0.5.
- The **long run** never exceeds 110% of your longest run in the last eight weeks.
- One **quality** session (threshold effort) and one long run per week; the rest easy.
- Default is 5 training days: 4 runs and 1 strength session, 2 rest days. Adjustable 3–6
  days and 0–2 strength sessions.
- With no recent running it returns "no plan" rather than a made-up baseline.

It is general fitness guidance, not medical advice. There is no automatic push to your
watch; ask Claude in a session to create the plan in COROS if you want it there.

---

## How the coach avoids making numbers up

1. A keyword router maps the question to one of nine **whitelisted** tools. Arguments are
   validated and bounded (windows are 7/28/90 days; sports come from a fixed list).
2. Each tool runs a **fixed, parameterised SQL statement** against gold and returns
   **verified facts** as JSON. No model ever writes SQL or names a table.
3. A **deterministic, rule-based answer is always produced first**. That is the free,
   zero-API-cost mode and the default.
4. Optionally an LLM may phrase the facts, or (Claude) choose which tools to call. Its
   text then passes a **number guard**: every numeric token must appear in the facts, or
   the answer is discarded in favour of the deterministic one. A heart rate invented for
   an activity that reported none is caught this way.

**Claude tool calling** (`coach/claude.py`): the model may only call the whitelisted tools;
undeclared arguments (`today`, `sql`, a table name) are refused, values are re-validated by
the tool, tool calls are capped, the endpoint is a fixed https constant and the key is
redacted from every error. Every deterministic answer passes its own guard in the tests.

**Zero-cost and local options:** the rule-based engine needs nothing. For phrasing, point
`LLM_BASE_URL` at a local Ollama (`http://localhost:11434/v1`, free, nothing leaves your
machine) or a free hosted tier. Claude Pro does **not** include API credit; the Claude API
is billed per token (the default model is the cheapest).

### LLM provider privacy

Only aggregates are sent, never raw activity rows. Verified against primary sources on
2026-10-17:

| Provider | Trains on inputs? | Retention |
|---|---|---|
| **Local Ollama** | n/a, runs on your machine | none |
| **Cloudflare Workers AI** | **No**: "does not use Customer Content processed by Workers AI to train AI models" | Not retained after the request |
| **Ollama Cloud** | **No**: "We do not use your inputs or outputs to train any AI models" | Transient only |
| **Google Gemini free tier** | **Yes**: content may be used to improve products and read by human reviewers | avoid for personal data |

---

## Costs and free-tier limits

| Layer | Free option | Constraint that actually bites |
|---|---|---|
| Database | **Neon** | 1 GB; compute sleeps after 5 min and **wakes itself** (first query ~1 s). This data is a few MB |
| Dashboard | Streamlit Community Cloud | Sleeps after ~12 h idle; **public apps are world-readable**, so use the one free private app for real data |
| Build | GitHub Actions + dbt | 2,000 min/month on a private repo, unlimited on a public one; a dbt build itself takes seconds |
| Chat | Rule-based coach, or local Ollama | none |
| **Paid, optional** | Claude API | per-token billing; not covered by Claude Pro |

No free tier includes backups. Bronze keeps the raw payloads and silver/gold rebuild from
them, but that does not survive losing the database: take a periodic `pg_dump`.

**Prefer Neon over Supabase here.** Supabase's free tier pauses the whole project after 7
days idle and needs a manual click to return; Neon only suspends compute and resumes on the
next connection.

---

## What is not built

Stated plainly, so nothing here is mistaken for working:

| Item | Status |
|---|---|
| **FIT file import** | Not implemented: no FIT file was available to verify a parser against. See [DATA_IMPORT.md](docs/DATA_IMPORT.md) |
| **Sleep, HRV, resting heart rate, recovery** | Not imported. COROS returned almost no usable data for this account (7 nights in 19 months), so the tab explains this instead of showing empty charts |
| **Per-activity detail** (cadence, COROS load, training effect) | Available from COROS one call per activity; not imported |
| **Automatic COROS refresh** | Needs a Claude session; there is no unattended path to the COROS MCP server. The optional Strava source is the unattended route |
| **Push plan to the watch** | Not automated |
| **Strava against the live API** | Written, tested with fake responses and its auth helper exercised, but **never completed a sync**: Strava answers every call from the test apps with `403 Application Status Inactive`, which only Strava can lift. The GitHub step is off until `STRAVA_ENABLED=true` |
| **Live Claude API call** | Written and tested against a fake transport only; no real call was made |

---

## Verification status

"Tested" means an automated test exercises it and it was run.

### Run and passing

| Check | Result |
|---|---|
| Unit and integration tests | 161 pass: ingest idempotency, watermark/overlap, retries, migration, snapshot parser (against the real record format), plan rules, tools, router, number guard, Claude tool calling (fake transport), LLM client, `.env` loader, dashboard smoke (AppTest) |
| `ruff`, `bandit -r src -ll`, `pip-audit` | clean / no medium-high / no known vulnerabilities (Pillow was bumped to 13 to clear new advisories) |
| **Real pipeline on Neon** | 443 real activities ingested; rerun wrote 0 bronze rows; `dbt build` passed all 27 models and tests; 0 rejects |
| **Reconciliation against raw files** | Run count (338), total km (2413.93), total hours (302.33), total load (46561.3) and a sample week all match exactly |
| **Dashboard on real data** | All 8 tabs render with no exception; the chat answered load, plan, strength and coverage questions from live gold |

### Written, not yet run

| Item | Why |
|---|---|
| **GitHub Actions workflows** (`ci.yml`, `transform.yml`) | Never executed on GitHub. All their steps were run locally. The CI `postgres` job (ingest, dbt build, `verify_postgres`) is the first run of `verify_postgres` |
| **dbt on Python 3.14** | CI and Streamlit Cloud use 3.14; every dbt dependency has a 3.14 wheel, but dbt was run locally on 3.13 |
| **Streamlit Cloud deployment** | Not deployed in this session |
| **Claude API and other LLM providers** | Only fake transports |

---

## Testing

```bash
pytest -q                          # 161 tests, no database or network
ruff check .
bandit -r src -ll
pip-audit --strict --requirement requirements.txt

# Against a real Postgres (after ingest + transform):
PYTHONPATH=src python -m corach.verify_postgres
```

No test touches the network. Unit tests run coach tools against hand-written gold rows on
SQLite, so they check the tools against known values rather than against a second
implementation of the dbt logic; dbt's own tests and the Postgres CI job cover the SQL.

---

## Privacy

`.gitignore` excludes `data/`, `*.db`, `*.fit`, `*.tcx`, `*.gpx` and `.env`, and CI fails if
any is ever tracked. Saved COROS files contain GPS start points and heart rate, so keep
them out of git and make the Streamlit app private. Only aggregates are sent to an LLM, and
API keys and connection strings are redacted from every error path.
