# Deployment guide

Deploying the COROS Running Coach on free tiers: **Neon** (Postgres),
**Streamlit Community Cloud** (dashboard) and **GitHub Actions** (scheduled
ingestion). Total cost **$0**, no credit card.

> **Before you start:** the COROS MCP adapter is still a stub, so everything
> deployed here runs on **SYNTHETIC** data and is labelled as such in the UI.
> See [Pointing at real data](#pointing-at-real-data) before changing that.

<figure>
<img src="deploy-flow.svg" alt="Five deployment steps: push to GitHub; create a Neon project and copy the pooled connection string; add it as the DATABASE_URL Actions secret and run the ingest workflow once by hand; deploy app.py on Streamlit Community Cloud with the same string in its Secrets panel; then let the daily schedule run. Each step lists what to check before moving on." width="760">
<figcaption>Each step has a check. If one fails, fix it before continuing — the symptom resurfaces more confusingly two steps later.</figcaption>
</figure>

---

## Before you begin

| You need | Why |
|---|---|
| A GitHub account | Hosts the repo and runs ingestion |
| A [Neon](https://neon.com) account | Postgres, free, no card |
| A [Streamlit Cloud](https://share.streamlit.io) account | Dashboard hosting; sign in with GitHub |

Optional, for LLM phrasing: a [Cloudflare](https://developers.cloudflare.com/workers-ai/)
or [Ollama Cloud](https://ollama.com) account. The chat works without one.

---

## Step 1 — Push to GitHub

```bash
git remote add origin https://github.com/<you>/coros-running-coach.git
git push -u origin main
```

**Make the repo public.** Public repos get unlimited Actions minutes; private
repos get 2,000/month, which active development can exhaust. Public is safe
here — every committed row is synthetic, and CI fails the build if a `.env`,
`*.db`, `*.fit` or `data/` file is ever tracked.

✅ **Check:** the **test** job passes under the Actions tab.

<details>
<summary>If the <code>test</code> job fails</summary>

The **Guard against tracked private data** step failing means a real data file
got committed. Remove it from tracking and commit again:

```bash
git rm --cached <the-file>
git commit -m "Remove private data from tracking"
```

The file stays on disk; `.gitignore` keeps it out from now on.
</details>

---

## Step 2 — Create the Neon project

1. Create a project at [console.neon.tech](https://console.neon.tech).
2. Open **Connection Details**.
3. Enable **Connection pooling** and copy the string.

It must look like this — note `-pooler` in the host and the `sslmode` suffix:

```
postgresql://user:password@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require
```

**Use the pooled endpoint.** Streamlit reruns the whole script on every
interaction, so a direct connection exhausts the free connection limit quickly.

✅ **Check:** the host contains `-pooler` and the string ends `?sslmode=require`.

> **Why Neon rather than Supabase?** Supabase's free tier pauses the whole
> project after 7 days idle and needs a manual dashboard click to resume, which
> makes the scheduled job load-bearing. Neon only suspends compute and resumes
> on the next connection — the worst case is a slow first query, not an outage.
> Supabase works fine otherwise; see [`.env.example`](../.env.example).

---

## Step 3 — Add the secret and seed the database

**3a. Store the connection string.** In your repo:
**Settings → Secrets and variables → Actions → New repository secret**

| Field | Value |
|---|---|
| Name | `DATABASE_URL` |
| Secret | the pooled string from step 2 |

**3b. Run the ingest once by hand** to create the tables and load data:
**Actions → Scheduled ingest → Run workflow**.

✅ **Check:** the log ends with something like

```
ingest success: fetched=68 bronze_changed=68 silver=64 rejected=4 watermark=2026-10-18
```

`rejected=4` is **correct** — the synthetic source emits four deliberately bad
rows so the quarantine path is exercised on every run.

<details>
<summary>If the ingest fails</summary>

| Message | Cause | Fix |
|---|---|---|
| `DATABASE_URL secret is not set` | Secret missing or misnamed | Re-add it; the name is case-sensitive |
| `password authentication failed` | Credentials wrong | Re-copy from Neon; the password is in the string itself |
| `could not translate host name` | Host truncated on copy | Copy the whole string |
| `SSL connection required` | `?sslmode=require` dropped | Append it |
| `too many connections` | Direct endpoint used | Switch to the `-pooler` host |

The failure is also recorded in `ops_pipeline_runs` with its error, so you can
inspect it later:

```sql
SELECT status, error, started_at FROM ops_pipeline_runs ORDER BY started_at DESC LIMIT 5;
```
</details>

---

## Step 4 — Deploy the dashboard

1. At [share.streamlit.io](https://share.streamlit.io) choose **New app**.
2. Pick your repo, branch `main`, main file **`app.py`**.
3. Open **Advanced settings → Secrets** *before* deploying, and paste:

```toml
DATABASE_URL = "postgresql://user:password@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require"
```

**This panel is not optional.** Streamlit Community Cloud provides no
environment variables, so it is the only way the app receives configuration.
`app.py` copies `st.secrets` into `os.environ` at startup; without it the app
silently falls back to SQLite and shows an empty dashboard.

✅ **Check:** KPI cards show numbers, the weekly chart has bars, and a yellow
**SYNTHETIC DEMO DATA** banner appears.

<details>
<summary>If the app shows "No activities in the database yet"</summary>

The app connected to an empty database — almost always a missing or malformed
secret. Confirm:

- The secret is named exactly `DATABASE_URL`
- The value is quoted, TOML-style: `DATABASE_URL = "postgresql://..."`
- It is the **same** string step 3 used (a different Neon branch is a different database)

After editing secrets, use **Manage app → Reboot**. Streamlit caches the
connection via `@st.cache_resource`, so a reboot is required.
</details>

<details>
<summary>If the app crashes on startup</summary>

Open **Manage app → Logs**. A `ModuleNotFoundError` means a dependency is
missing from `requirements.txt`. A `psycopg.OperationalError` is the database —
see the step 3 table above.
</details>

---

## Step 5 — Let the schedule run

[`.github/workflows/ingest.yml`](../.github/workflows/ingest.yml) runs daily at
**03:17 UTC**. Nothing further is required.

The time is deliberately off the hour: GitHub's scheduler is most contended at
`:00`, where queued jobs are delayed or dropped.

✅ **Check tomorrow:** the dashboard banner still reads **Pipeline fresh**.

> GitHub disables scheduled workflows in repos with **no activity for 60 days**.
> A single commit re-enables them.

---

## Optional — LLM phrasing

The chat is fully functional without this: it produces deterministic,
rule-based answers from verified facts. An LLM only rephrases them, and any
answer containing a number absent from those facts is rejected.

Add to the **Streamlit secrets** panel:

```toml
LLM_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1"
LLM_MODEL = "@cf/meta/llama-3.1-8b-instruct"
LLM_API_KEY = "<token>"
```

Providers whose data policies were verified against primary sources
(2026-10-17/19):

| Provider | Trains on inputs? | Retention |
|---|---|---|
| **Cloudflare Workers AI** | **No** — explicit in docs | Not retained after the request |
| **Ollama Cloud** | **No** — explicit in policy | Transient only |
| **Gemini free tier** | **Yes** — avoid for personal data | Human reviewers may read inputs |

Only aggregates are sent, never raw activity rows. See
[`.env.example`](../.env.example) for exact URLs.

✅ **Check:** ask the chat a question; the caption should read
*"LLM-phrased (numbers verified)"*. If it says *"fell back to rule-based"*, the
guard rejected the model's answer — the app is working as designed.

---

## Pointing at real data

Two things must happen first, in order.

**1. Implement the COROS adapter.** `src/corach/sources/coros_mcp.py` is a stub
that raises on use. Its module docstring lists what to capture from a real MCP
session before writing any parser — tool names, auth flow, exact field names and
units. Guessing those would fabricate data.

**2. Make the dashboard private.** A public Streamlit app is **world-readable**.
The free tier allows **one** private app: **Manage app → Settings → Sharing →
Private**. Alternatively keep the public app on synthetic data and run a
separate private one for real data.

Then set the repository variable `CORACH_SOURCE` to `coros_mcp`
(**Settings → Secrets and variables → Actions → Variables**).

---

## Operations

### Run an ingest manually

**Actions → Scheduled ingest → Run workflow.** Tick **full_refresh** to ignore
the watermark and re-read all history — useful after correcting activities
upstream.

### Reprocess without re-fetching

If validation or parsing rules change, rebuild silver from the bronze payloads
already stored:

```bash
PYTHONPATH=src DATABASE_URL="postgresql://..." python -m corach.ingest --rebuild-silver
```

### Check pipeline health

```sql
SELECT status, rows_silver, rows_rejected, started_at, error
FROM ops_pipeline_runs ORDER BY started_at DESC LIMIT 10;
```

### Inspect quarantined rows

```sql
SELECT reason, COUNT(*) FROM silver_rejects GROUP BY reason ORDER BY 2 DESC;
```

The dashboard shows the same summary under **Quarantined rows**.

### Back up

No free tier includes automated backups. The design mitigates this — bronze
retains raw payloads and `rebuild_silver()` reprocesses from them — but that
does not survive losing the database. For real data:

```bash
pg_dump "$DATABASE_URL" > backup-$(date +%F).sql
```

---

## Free-tier limits worth knowing

| Limit | Effect | Mitigation |
|---|---|---|
| Neon: compute sleeps after 5 min | First query takes ~1s | None needed — it wakes itself |
| Neon: 1 GB storage | Writes fail when full | Years of running is well under 100 MB |
| Streamlit: sleeps after ~12 h idle | Visitor sees a wake-up page | None needed |
| Streamlit: 1 private app | Public apps are world-readable | Use the private slot for real data |
| Actions: 2,000 min/month (private) | Workflows stop running | Make the repo public |
| Actions: disabled after 60 days idle | Schedule stops silently | Any commit re-enables it |

---

## Verification status

Honest accounting, consistent with the [main README](../README.md):

- **Tested:** 102 automated tests covering ingestion, metrics, the coach and the
  dashboard; the CI data guard was confirmed to catch a planted `.env` and
  `data/*.db`.
- **Not verified:** the **Postgres backend has never executed** — no Postgres
  was available on the development machine. Step 3 is the first time this code
  touches a real server. The `postgres` CI job runs `corach.verify_postgres`
  against a throwaway Postgres 16 and checks the same properties.
- **Not verified:** no real LLM call has been made; `llm.py` is tested only
  against a fake transport.
- **Not implemented:** the COROS MCP adapter.

If step 3 fails, that is the expected place for a first genuine surprise — the
error text in the Actions log will say which of the causes above it is.
