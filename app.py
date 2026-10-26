"""Streamlit dashboard over the dbt gold layer: all sports, workload, plan and coach.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

# Allow `streamlit run app.py` from a checkout without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from corach.coach.chat import answer  # noqa: E402
from corach.coach.claude import build_claude_client  # noqa: E402
from corach.coach.llm import build_client as build_llm_client  # noqa: E402
from corach.coach.tools import call_tool  # noqa: E402
from corach.store import Store  # noqa: E402

st.set_page_config(page_title="COROS Coach", page_icon="🏃", layout="wide")

ACCENT = "#2f6df6"
SPORT_COLORS = {"run": "#2f6df6", "strength": "#e8743b", "walk": "#2a9d8f",
                "swim": "#8e6bd8", "cycling": "#d4a017", "other": "#8a94a6"}
ZONE_LABEL = {
    "optimal": ("Optimal", "success"), "undertraining": ("Below usual load", "info"),
    "caution": ("Caution: elevated", "warning"), "high_risk": ("High risk: ease off", "error"),
    "insufficient_history": ("Not enough history yet", "info"),
}

# Streamlit Community Cloud supplies configuration through st.secrets, not the
# environment. Copy it across before anything reads os.environ, so Store() and
# build_client() behave identically when deployed and when run locally.
# Existing environment variables win, so a local .env is not overridden.
_SETTINGS = ("DATABASE_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE",
             "CORACH_SQLITE_PATH", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT",
             "ANTHROPIC_API_KEY", "CLAUDE_MODEL")
try:
    for _key in _SETTINGS:
        if _key in st.secrets and not os.environ.get(_key):
            os.environ[_key] = str(st.secrets[_key])
except Exception:  # noqa: BLE001 - no secrets.toml is the normal local case
    pass


@st.cache_resource
def get_store() -> Store:
    store = Store()
    store.init_schema()
    return store


store = get_store()


@st.cache_data(ttl=300, show_spinner=False)
def frame(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Read-only query helper. Every statement below is a constant in this file."""
    return pd.DataFrame(store.query(sql, params))


def fmt(value: object, suffix: str = "") -> str:
    """Render a value, showing an explicit dash when it is unavailable."""
    return "—" if value is None or value != value else f"{value}{suffix}"


def pace_text(seconds: float | None) -> str:
    if seconds is None or seconds != seconds:
        return "—"
    s = int(round(float(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def style(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=24, b=8),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.12))
    fig.update_yaxes(gridcolor="rgba(138,148,166,0.2)", zerolinecolor="rgba(138,148,166,0.35)")
    fig.update_xaxes(showgrid=False)
    return fig


# -- sidebar ---------------------------------------------------------------
with st.sidebar:
    st.header("Your plan settings")
    sessions_per_week = st.slider("Training days per week", 3, 6, 5)
    strength_per_week = st.slider("Strength sessions per week", 0, 2, 1)
    has_race = st.checkbox("I have a race coming up")
    weeks_to_race = None
    if has_race:
        race_day = st.date_input("Race date", value=date.today() + timedelta(weeks=8))
        weeks_to_race = max(0, (race_day - date.today()).days // 7)
        st.caption(f"{weeks_to_race} weeks to go")
    st.caption("Plans are general fitness guidance, not medical advice.")

st.title("🏃 COROS Coach")

try:
    coverage = call_tool(store, "data_coverage")
except Exception:  # noqa: BLE001 - gold tables missing is the expected first-run state
    st.error("The gold layer was not found. Load data and build it with: "
             "`python -m corach.ingest` then `python -m corach.transform_run`.")
    st.stop()

if not coverage["activity_count"]:
    st.warning("No activities yet. Load data with `python -m corach.ingest`, then run "
               "`python -m corach.transform_run`.")
    st.stop()

if coverage["is_synthetic"]:
    st.error("⚠️ **SYNTHETIC DEMO DATA** — these numbers are generated, not real activity.",
             icon="⚠️")

sports_text = ", ".join(f"{n} {s}" for s, n in coverage["sport_counts"].items())
if coverage["pipeline_is_fresh"]:
    st.success(f"Pipeline fresh — last ingest {coverage['pipeline_age_hours']} h ago · "
               f"{coverage['activity_count']} activities ({sports_text}) · "
               f"{coverage['first_activity_date']} → {coverage['last_activity_date']}")
elif coverage["last_successful_run_at"]:
    st.warning(f"Last ingest {coverage['pipeline_age_hours']} h ago · "
               f"{coverage['activity_count']} activities through {coverage['last_activity_date']}")
else:
    st.warning("No successful ingest recorded.")

tabs = st.tabs(["Overview", "Running", "Strength & other", "Workload", "Plan",
                "Sleep & recovery", "Ask your data", "Data quality"])

# -- Overview --------------------------------------------------------------
with tabs[0]:
    run7 = call_tool(store, "window", days=7, sport="run")
    run28 = call_tool(store, "window", days=28, sport="run")
    load = call_tool(store, "workload")
    all28 = call_tool(store, "sport_breakdown", days=28)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Run km, last 7 days", f"{run7['distance_km']}", f"{run7['sessions']} runs")
    c2.metric("Run km, last 28 days", f"{run28['distance_km']}", f"{run28['sessions']} runs")
    c3.metric("28-day sessions (all sports)", sum(s["sessions"] for s in all28["sports"]))
    c4.metric("Load ratio (ACWR)", fmt(load.get("acwr")) if load["found"] else "—",
              ZONE_LABEL.get(load.get("acwr_zone"), ("", ""))[0] or None, delta_color="off")

    weeks = st.slider("Weeks to show", 4, 52, 16, key="ov_weeks")
    wk = frame("SELECT week_start, sport_group, duration_min FROM gold.weekly_summary "
               "WHERE week_start >= ? ORDER BY week_start",
               ((date.today() - timedelta(weeks=weeks)).isoformat(),))
    if wk.empty:
        st.info("No activity in this period.")
    else:
        fig = go.Figure()
        for sport, grp in wk.groupby("sport_group"):
            fig.add_bar(x=grp["week_start"], y=grp["duration_min"].astype(float) / 60,
                        name=sport, marker_color=SPORT_COLORS.get(sport, "#8a94a6"))
        fig.update_layout(barmode="stack", yaxis_title="hours per week")
        st.plotly_chart(style(fig), width="stretch")

    st.subheader("Recent activities")
    recent = frame("SELECT local_date AS date, sport_name AS sport, distance_km, duration_min, "
                   "avg_hr, avg_pace_s_per_km, sets, trimp FROM gold.fct_activities "
                   "ORDER BY start_time_utc DESC LIMIT 15")
    if not recent.empty:
        recent["pace"] = recent["avg_pace_s_per_km"].map(pace_text)
        recent = recent.drop(columns="avg_pace_s_per_km").rename(columns={
            "distance_km": "km", "duration_min": "min", "avg_hr": "avg HR", "trimp": "load"})
        st.dataframe(recent.round(1), width="stretch", hide_index=True)

# -- Running ---------------------------------------------------------------
with tabs[1]:
    weeks = st.slider("Weeks to show", 4, 52, 26, key="run_weeks")
    series = call_tool(store, "weekly_volume", weeks=weeks, sport="run")
    df = pd.DataFrame(series["series"])
    fig = go.Figure()
    fig.add_bar(x=df["week_start"], y=df["distance_km"], marker_color=ACCENT, name="km",
                hovertemplate="Week of %{x}<br>%{y:.1f} km<extra></extra>")
    fig.update_layout(yaxis_title="km per week", showlegend=False)
    st.plotly_chart(style(fig), width="stretch")
    wow = series["week_over_week_pct"]
    st.caption(f"Last full week: {fmt(series['last_full_week_km'], ' km')} · "
               + ("week-over-week unavailable" if wow is None else f"{wow:+}% vs the week before")
               + f" · this week so far: {series['this_week_km']} km")

    left, right = st.columns(2)
    paced = df[df["pace_per_km"].notna()].copy()
    if not paced.empty:
        paced["pace_s"] = paced["pace_per_km"].map(
            lambda p: int(p.split(":")[0]) * 60 + int(p.split(":")[1]))
        fig = go.Figure(go.Scatter(x=paced["week_start"], y=paced["pace_s"] / 60,
                                   mode="lines+markers", line=dict(color=ACCENT)))
        fig.update_layout(yaxis_title="min per km (lower is faster)", yaxis_autorange="reversed")
        left.subheader("Weekly pace")
        left.plotly_chart(style(fig, 280), width="stretch")
    hr = df[df["avg_hr"].notna()]
    if not hr.empty:
        fig = go.Figure(go.Scatter(x=hr["week_start"], y=hr["avg_hr"], mode="lines+markers",
                                   line=dict(color="#e8743b")))
        fig.update_layout(yaxis_title="average HR (bpm)")
        right.subheader("Weekly average heart rate")
        right.plotly_chart(style(fig, 280), width="stretch")

    longest = call_tool(store, "longest_run")
    if longest["found"]:
        st.metric("Longest run", f"{longest['distance_km']} km",
                  f"{longest['date']} · {fmt(longest['pace_per_km'], ' /km')}", delta_color="off")

# -- Strength & other ------------------------------------------------------
with tabs[2]:
    other = frame("SELECT week_start, sport_group, sessions, duration_min, sets, distance_km "
                  "FROM gold.weekly_summary WHERE sport_group <> 'run' AND week_start >= ? "
                  "ORDER BY week_start", ((date.today() - timedelta(weeks=26)).isoformat(),))
    if other.empty:
        st.info("No strength, walking, swimming or cycling in the last 26 weeks.")
    else:
        fig = go.Figure()
        for sport, grp in other.groupby("sport_group"):
            fig.add_bar(x=grp["week_start"], y=grp["duration_min"].astype(float), name=sport,
                        marker_color=SPORT_COLORS.get(sport, "#8a94a6"))
        fig.update_layout(barmode="stack", yaxis_title="minutes per week")
        st.plotly_chart(style(fig), width="stretch")
    totals = frame("SELECT sport_group AS sport, COUNT(*) AS sessions, "
                   "ROUND(SUM(duration_min) / 60.0, 1) AS hours, ROUND(SUM(distance_km), 1) AS km, "
                   "SUM(sets) AS sets FROM gold.fct_activities GROUP BY sport_group "
                   "ORDER BY sessions DESC")
    st.subheader("All-time totals by sport")
    st.dataframe(totals, width="stretch", hide_index=True)

# -- Workload --------------------------------------------------------------
with tabs[3]:
    load = call_tool(store, "workload")
    if not load["found"]:
        st.info("No load data yet.")
    else:
        label, kind = ZONE_LABEL.get(load["acwr_zone"], (load["acwr_zone"], "info"))
        getattr(st, kind)(f"**{label}** — acute:chronic load ratio {fmt(load['acwr'])} "
                          f"as of {load['as_of']}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Acute load (7 d)", fmt(load["acute_load"]))
        m2.metric("Chronic load (28 d)", fmt(load["chronic_load"]))
        m3.metric("Monotony", fmt(load["monotony_7d"]))
        m4.metric("Strain", fmt(load["strain_7d"]))

        days = st.slider("Days to show", 30, 365, 120, key="load_days")
        d = frame("SELECT day, acute_load, chronic_load, acwr FROM gold.daily_load "
                  "WHERE day >= ? ORDER BY day",
                  ((date.today() - timedelta(days=days)).isoformat(),))
        fig = go.Figure()
        fig.add_scatter(x=d["day"], y=d["acute_load"], name="Acute (7 d)",
                        line=dict(color="#e8743b"))
        fig.add_scatter(x=d["day"], y=d["chronic_load"], name="Chronic (28 d)",
                        line=dict(color=ACCENT))
        fig.update_layout(yaxis_title="mean daily load (TRIMP)")
        st.plotly_chart(style(fig), width="stretch")

        fig = go.Figure()
        fig.add_hrect(y0=0.8, y1=1.3, fillcolor="rgba(42,157,143,0.15)", line_width=0)
        fig.add_scatter(x=d["day"], y=d["acwr"], name="ACWR", line=dict(color="#2a9d8f"))
        fig.update_layout(yaxis_title="acute : chronic ratio", showlegend=False)
        st.subheader("Acute : chronic ratio (green band = 0.8–1.3)")
        st.plotly_chart(style(fig, 260), width="stretch")
    with st.expander("How load is calculated"):
        st.markdown(
            "Each session's load is its **TRIMP** (Banister): minutes × heart-rate reserve × "
            "0.64 × e^(1.92 × reserve), from the session's *average* heart rate. It understates "
            "interval and strength sessions. Rest days count as zero. **Acute** is the 7-day mean, "
            "**chronic** the 28-day mean, and the ratio is shown once 28 days of history exist. "
            "Resting and maximum heart rate are set in `dbt/dbt_project.yml`; the maximum is an "
            "assumption until you set your measured value.")

# -- Plan ------------------------------------------------------------------
with tabs[4]:
    plan = call_tool(store, "training_plan", sessions_per_week=sessions_per_week,
                     strength_per_week=strength_per_week, weeks_to_race=weeks_to_race)
    if not plan["found"]:
        st.info("A plan needs a few weeks of recent running to build on.")
    else:
        p1, p2, p3 = st.columns(3)
        p1.metric("Planned weekly distance", f"{plan['planned_weekly_km']} km")
        p2.metric("Based on your recent average", f"{plan['basis_weekly_km']} km")
        p3.metric("Phase", plan["phase"].replace("_", " "))
        table = pd.DataFrame([{
            "Date": d["date"], "Day": d["weekday"], "Session": d["kind"],
            "Distance (km)": d["distance_km"], "Duration (min)": d["duration_min"],
            "What to do": d["guidance"],
        } for d in plan["days"]])
        st.dataframe(table, width="stretch", hide_index=True)
        for note in plan["notes"]:
            st.caption(note)
    with st.expander("Plan rules"):
        st.markdown(
            "Weekly distance = your last four complete weeks' average × 1.10 (×1.00 in the "
            "caution zone, ×0.75 at high risk; a race inside two weeks tapers it). The long run "
            "never exceeds 110% of your longest recent run. One quality session and one long "
            "run per week; everything else easy.")

# -- Sleep & recovery ------------------------------------------------------
with tabs[5]:
    st.info(
        "Sleep, HRV and resting-heart-rate trends are **not in the pipeline yet**. When this "
        "was built, the COROS export held almost no usable sleep data for this account (a "
        "handful of nights across 19 months, most days reporting no record), so any chart "
        "would be nearly empty. Wear the watch overnight and ask Claude to refresh the COROS "
        "snapshot; the sleep tables will be added once there is enough data to chart.")
    st.caption("Training load and the load ratio on the Workload tab already reflect your "
               "recovery state from training alone.")

# -- Ask your data ---------------------------------------------------------
with tabs[6]:
    llm_client = build_llm_client()
    claude_client = build_claude_client()
    engines = ["Rule-based (free, no API)"]
    if llm_client:
        engines.append("LLM phrasing (rephrases verified facts)")
    if claude_client:
        engines.append("Claude with tools (uses API credit)")
    engine = st.radio("Answer engine", engines, horizontal=True)
    st.caption("Every number comes from whitelisted read-only tools; an LLM answer containing "
               "an unverified number is rejected and replaced by the rule-based one.")
    question = st.text_input(
        "Question",
        placeholder="e.g. How is my training load? How many gym sessions last month? Plan my week")
    if question:
        settings = dict(sessions_per_week=sessions_per_week,
                        strength_per_week=strength_per_week, weeks_to_race=weeks_to_race)
        if engine.startswith("Claude"):
            result = claude_client.ask(store, question, today=date.today(), **settings)
        else:
            result = answer(store, question, today=date.today(), **settings,
                            client=llm_client if engine.startswith("LLM") else None)
        st.markdown(f"**{result.answer}**")
        badge = {
            "deterministic": "Rule-based answer",
            "llm": "LLM-phrased (numbers verified)",
            "claude": "Claude with tools (numbers verified)",
            "llm_rejected": "LLM answer rejected — fell back to rule-based",
            "error": "Invalid request",
        }.get(result.mode, result.mode)
        st.caption(f"Answer mode: {badge}" + (f" · {result.note}" if result.note else ""))
        with st.expander("Verified facts used"):
            st.json(result.facts)

# -- Data quality ----------------------------------------------------------
with tabs[7]:
    st.metric("Validated activities", coverage["activity_count"])
    st.metric("Quarantined rows", coverage["rejected_rows"])
    rejects = frame("SELECT reason, COUNT(*) AS rows FROM silver.silver_rejects GROUP BY reason")
    if not rejects.empty:
        st.subheader("Why rows were quarantined")
        st.dataframe(rejects, width="stretch", hide_index=True)
    flagged = frame("SELECT local_date AS date, sport_name AS sport, distance_km, duration_min, "
                    "quality_flag FROM gold.fct_activities WHERE quality_flag IS NOT NULL "
                    "ORDER BY local_date DESC LIMIT 25")
    if not flagged.empty:
        st.subheader("Kept but flagged")
        st.caption("Plausible but unusual, e.g. a very slow average pace. They stay in the "
                   "metrics; check whether they are real runs.")
        st.dataframe(flagged.round(2), width="stretch", hide_index=True)
    runs = frame("SELECT started_at, source, status, rows_fetched, rows_bronze, watermark "
                 "FROM ops_pipeline_runs ORDER BY started_at DESC LIMIT 10")
    st.subheader("Recent ingest runs")
    st.dataframe(runs, width="stretch", hide_index=True)
