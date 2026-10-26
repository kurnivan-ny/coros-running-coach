# Deployment guide

Deploying COROS Coach on free tiers: **Neon** (Postgres), **GitHub Actions**
(dbt build and CI) and **Streamlit Community Cloud** (dashboard). Total cost **$0**,
no credit card. The optional Claude API is the only paid piece.

> **Real data stays private.** Your COROS files contain GPS start points and heart
> rate. They live in `data/coros_raw/` (gitignored) and are loaded into Neon from your
> machine; GitHub never sees them. A public Streamlit app is **world-readable**, so
> use the one free **private** app for real data.

<figure>
<img src="deploy-flow.svg" alt="Five deployment steps: push to GitHub; create a Neon project and copy the pooled connection string; add it as the DATABASE_URL Actions secret, load bronze from your machine and run the dbt workflow once; deploy app.py on Streamlit Community Cloud with the same string in its Secrets panel; then let the daily schedule run. Each step lists what to check before moving on." width="760">
<figcaption>Each step has a check. If one fails, fix it before continuing: the symptom resurfaces more confusingly two steps later.</figcaption>
</figure>

---

## Before you begin

| You need | Why |
|---|---|
| A GitHub account | Hosts the repo, runs CI and the dbt build |
| A [Neon](https://neon.com) account | Postgres, free, no card |
| A [Streamlit Cloud](https://share.streamlit.io) account | Dashboard hosting; sign in with GitHub |
| Python 3.11+ locally | To load bronze from your machine |

---

## Step 1: Push to GitHub

```bash
git remote add origin https://github.com/<you>/coros-running-coach.git
git push -u origin main
```

A public repo gets unlimited Actions minutes (private: 2,000/month). Public is safe for
the *code*: CI fails the build if a `.env`, `*.db`, `*.fit`, or anything under `data/`
is ever tracked.

✅ **Check:** the **test** and **postgres** jobs go green under the Actions tab. The
`postgres` job is the first real run of the dbt models and `verify_postgres` on GitHub.

<details>
<summary>If a job fails</summary>

**Guard against tracked private data** failing means a data file was committed:

```bash
git rm --cached <the-file>
git commit -m "Remove private data from tracking"
```

For a `postgres` job failure, the log shows the failing dbt test or the
`[FAIL]` line from `verify_postgres`.
</details>

---

## Step 2: Create the Neon project

1. Create a project at [console.neon.tech](https://console.neon.tech).
2. Open **Connection Details** and enable **Connection pooling**.
3. Copy the string. It must contain `-pooler` and end with `?sslmode=require`:

```
postgresql://user:password@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require
```

**Use the pooled endpoint.** Streamlit reruns the whole script on every interaction, so a
direct connection exhausts the free connection limit quickly.

✅ **Check:** the host contains `-pooler` and the string ends `?sslmode=require`.

> **Treat the string like a password.** If it is ever pasted into a chat, a log, or a
> commit, reset the role password in Neon (**Roles → Reset password**) and update every
> place that uses it.

---

## Step 3: Add the secret, load bronze, build silver and gold

**3a. Store the connection string** in the repo:
**Settings → Secrets and variables → Actions → New repository secret**, name
`DATABASE_URL`.

**3b. Load bronze from your machine.** Put the same string in your local `.env`
(`DATABASE_URL=...`), save your COROS snapshot as described in
[DATA_IMPORT.md](DATA_IMPORT.md), then:

```powershell
$env:PYTHONPATH = "src"
python -m corach.ingest --source coros_snapshot
```

✅ **Check:** `ingest success: fetched=443 bronze_changed=443 watermark=...` the first
time; a second run prints `bronze_changed=0`.

**3c. Build silver and gold**: **Actions → Build silver and gold (dbt) → Run workflow**
(or locally: `python -m corach.transform_run`).

✅ **Check:** the log ends `Done. PASS=27 WARN=0 ERROR=0`.

<details>
<summary>If it fails</summary>

| Message | Cause | Fix |
|---|---|---|
| `DATABASE_URL secret is not set` | Secret missing or misnamed | Re-add it; the name is case-sensitive |
| `password authentication failed` | Credentials wrong or rotated | Re-copy from Neon |
| `could not translate host name` | Host truncated on copy | Copy the whole string |
| `SSL connection required` | `?sslmode=require` dropped | Append it |
| `too many connections` | Direct endpoint used | Switch to the `-pooler` host |
| A dbt **test** failure | Real data problem | The test name says which rule; check the Data quality tab |

Ingest failures are also recorded:

```sql
SELECT status, error, started_at FROM ops_pipeline_runs ORDER BY started_at DESC LIMIT 5;
```
</details>

---

## Step 4: Deploy the dashboard

1. At [share.streamlit.io](https://share.streamlit.io) choose **New app**.
2. Pick your repo, branch `main`, main file **`app.py`**, **Python 3.14**.
3. Open **Advanced settings → Secrets** *before* deploying and paste (see
   [`.streamlit/secrets.toml.example`](../.streamlit/secrets.toml.example)):

```toml
DATABASE_URL = "postgresql://user:password@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require"
```

**This panel is not optional.** Streamlit Community Cloud provides no environment
variables. `app.py` copies `st.secrets` into `os.environ` at startup; without it the app
cannot find your database.

4. **Manage app → Settings → Sharing → Private** (real data).

✅ **Check:** KPI cards show your numbers, the weekly chart has bars, the Workload tab
shows a ratio, and there is **no** red SYNTHETIC banner.

<details>
<summary>If the app shows "gold layer was not found"</summary>

It connected, but silver/gold have not been built, or it connected to a different
database. Confirm the secret is exactly `DATABASE_URL`, quoted TOML-style, and is the
**same** string step 3 used (a different Neon branch is a different database). Then run
step 3c. After editing secrets use **Manage app → Reboot**, since the connection is cached.
</details>

<details>
<summary>If the app crashes on startup</summary>

Open **Manage app → Logs**. A `ModuleNotFoundError` means a dependency is missing from
`requirements.txt` (dbt is deliberately *not* in it; the app does not need it). A
`psycopg.OperationalError` is the database; see the step 3 table.
</details>

---

## Step 5: Keep it fresh

[`.github/workflows/transform.yml`](../.github/workflows/transform.yml) (optionally syncs
Strava, then) runs `dbt build` daily at **03:17 UTC** (off the hour on purpose: GitHub's scheduler is busiest at `:00`)
and on any change under `dbt/`. It rebuilds silver and gold from whatever bronze holds
and runs every dbt test.

**It does not fetch new COROS data**, because GitHub cannot reach the COROS MCP server.
To bring in new activities, refresh the snapshot and run step 3b (about a minute; see
[DATA_IMPORT.md](DATA_IMPORT.md)), or enable the Strava sync below. The dashboard's banner shows the age of the last
ingest and turns amber when it is stale.

> GitHub disables scheduled workflows in repos with **no activity for 60 days**. Any
> commit re-enables them.

### Optional: unattended Strava sync

Strava, unlike COROS, can be pulled by the scheduled workflow. Set it up locally first
([DATA_IMPORT.md](DATA_IMPORT.md#alternative-strava-unattended)) and confirm
`python -m corach.ingest --source strava` succeeds, then:

| Where | Name | Value |
|---|---|---|
| Actions **secret** | `STRAVA_CLIENT_ID` | from your Strava API app |
| Actions **secret** | `STRAVA_CLIENT_SECRET` | from your Strava API app |
| Actions **secret** | `STRAVA_REFRESH_TOKEN` | the value the auth helper saved in `.env` |
| Actions **variable** | `STRAVA_ENABLED` | `true` |

Without `STRAVA_ENABLED=true` the Strava step is skipped, so the workflow never fails for
want of Strava. The Streamlit app needs none of these; it only reads Neon.

✅ **Check:** **Actions → Sync Strava and build silver and gold (dbt) → Run workflow**; the
"Sync Strava into bronze" step prints `ingest success: fetched=... bronze_changed=...`.

If the refresh token rotates, the log shows `::warning::Strava issued a NEW refresh token`;
update the secret before the next run.

---

## Optional: answer engines for the chat

The chat is fully functional with none of these (rule-based, zero cost). Add to the
Streamlit secrets only what you want:

| Engine | Secrets | Cost |
|---|---|---|
| Local Ollama (rephrases facts) | `LLM_BASE_URL=http://localhost:11434/v1`, `LLM_MODEL=...`. Local runs only; Streamlit Cloud cannot reach your machine | free |
| Hosted free tier (rephrases facts) | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`; see `.env.example` for Cloudflare / Ollama Cloud | free allowances; limits unpublished |
| Claude with tool calling | `ANTHROPIC_API_KEY`, optional `CLAUDE_MODEL` | **per-token; Claude Pro does not include API credit** |

Whatever the engine, only aggregates are sent, no model writes SQL, and any answer with a
number not in the verified facts is discarded.

✅ **Check:** ask a question; the caption reads *"Claude with tools (numbers verified)"* or
*"LLM-phrased (numbers verified)"*. *"fell back to rule-based"* means the guard rejected
the model's answer, which is the app working as designed.

---

## Operations

### Reprocess without re-fetching

If validation rules change, edit the dbt models and rebuild; bronze is untouched:

```bash
PYTHONPATH=src python -m corach.transform_run
```

### Change the load assumptions

Maximum heart rate (default 193, estimated as 220 minus age 27) and resting heart rate are in
`dbt/dbt_project.yml`. Set your measured values and rebuild, or one-off:

```bash
PYTHONPATH=src python -m corach.transform_run build --vars '{max_hr: 195}'
```

### Inspect quarantined rows

```sql
SELECT reason, COUNT(*) FROM silver.silver_rejects GROUP BY reason ORDER BY 2 DESC;
```

The dashboard shows the same under **Data quality**.

### Back up

No free tier includes automated backups. Bronze keeps the raw payloads and dbt rebuilds
silver and gold from them, but that does not survive losing the database:

```bash
pg_dump "$DATABASE_URL" > backup-$(date +%F).sql
```

---

## Free-tier limits worth knowing

| Limit | Effect | Mitigation |
|---|---|---|
| Neon: compute sleeps after 5 min | First query takes ~1 s | None needed; it wakes itself |
| Neon: 1 GB storage | Writes fail when full | This dataset is a few MB |
| Streamlit: sleeps after ~12 h idle | Visitor sees a wake-up page | None needed |
| Streamlit: 1 private app | Public apps are world-readable | Use the private slot for real data |
| Actions: 2,000 min/month (private) | Workflows stop running | Make the repo public |
| Actions: disabled after 60 days idle | Schedule stops silently | Any commit re-enables it |

---

## Verification status

Consistent with the [main README](../README.md):

- **Run:** the full pipeline against a real Neon database (ingest, `dbt build` with 27
  passing models and tests, reconciliation against the raw files, dashboard render), plus
  133 automated tests.
- **Not run:** the GitHub workflows have not executed on GitHub, dbt has not been run on
  Python 3.14 itself (only 3.13 locally; all dependencies have 3.14 wheels), and the app
  has not been deployed to Streamlit Cloud. Step 1 and step 4 are the first real runs of
  those, and the error text will say which cause above applies.
