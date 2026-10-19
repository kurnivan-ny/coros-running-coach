"""Streamlit dashboard: KPIs, weekly volume, pipeline freshness, and the coach.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

# Allow `streamlit run app.py` from a checkout without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from corach.coach.chat import answer  # noqa: E402
from corach.coach.llm import build_client  # noqa: E402
from corach.coach.tools import call_tool  # noqa: E402
from corach.ingest import load_silver  # noqa: E402
from corach.store import Store  # noqa: E402

st.set_page_config(page_title="COROS Running Coach", page_icon="🏃", layout="wide")

ACCENT = "#2f6df6"

# Streamlit Community Cloud supplies configuration through st.secrets, not the
# environment. Copy it across before anything reads os.environ, so Store() and
# build_client() behave identically when deployed and when run locally.
# Existing environment variables win, so a local .env is not overridden.
_SETTINGS = ("DATABASE_URL", "CORACH_SQLITE_PATH", "CORACH_SOURCE",
             "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT")
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


def fmt(value: object, suffix: str = "") -> str:
    """Render a value, showing an explicit dash when it is unavailable."""
    return "—" if value is None else f"{value}{suffix}"


store = get_store()
rows = load_silver(store)

st.title("🏃 COROS Running Coach")

if not rows:
    st.warning(
        "No activities in the database yet. Run `python -m corach.ingest --source synthetic` "
        "to load SYNTHETIC demo data."
    )
    st.stop()

coverage = call_tool(store, "data_coverage")

if coverage["is_synthetic"]:
    st.error(
        "⚠️ **SYNTHETIC DEMO DATA** — these numbers are generated, not real activity.",
        icon="⚠️",
    )

# -- pipeline freshness ----------------------------------------------------
if coverage["pipeline_is_fresh"]:
    st.success(
        f"Pipeline fresh — last success {coverage['pipeline_age_hours']} h ago "
        f"({coverage['activity_count']} activities, "
        f"{coverage['first_activity_date']} → {coverage['last_activity_date']})"
    )
elif coverage["last_successful_run_at"]:
    st.warning(f"Pipeline stale — last success {coverage['pipeline_age_hours']} h ago")
else:
    st.warning("No successful pipeline run recorded.")

# -- KPI cards -------------------------------------------------------------
w7 = call_tool(store, "window", days=7)
w28 = call_tool(store, "window", days=28)
longest = call_tool(store, "longest_run")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Last 7 days", f"{w7['distance_km']} km", f"{w7['runs']} runs")
c2.metric("Last 28 days", f"{w28['distance_km']} km", f"{w28['runs']} runs")
c3.metric("7-day pace", fmt(w7["pace_per_km"], " /km"))
c4.metric("7-day avg HR", fmt(w7["avg_hr"], " bpm"))

if not w28["sufficient_history"]:
    st.caption("Note: history is shorter than 28 days, so that window is incomplete.")

# -- weekly volume ---------------------------------------------------------
st.subheader("Weekly volume")
weeks = st.slider("Weeks to show", min_value=4, max_value=26, value=12, step=1)
weekly = call_tool(store, "weekly_volume", weeks=weeks)
series = weekly["series"]

fig = go.Figure()
fig.add_bar(
    x=[w["week_start"] for w in series],
    y=[w["distance_km"] for w in series],
    marker_color=ACCENT,
    name="Distance",
    hovertemplate="Week of %{x}<br>%{y:.1f} km<extra></extra>",
)
fig.update_layout(
    height=320,
    margin=dict(l=8, r=8, t=8, b=8),
    xaxis_title=None,
    yaxis_title="km",
    showlegend=False,
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
)
fig.update_yaxes(gridcolor="rgba(138,148,166,0.2)", zerolinecolor="rgba(138,148,166,0.35)")
fig.update_xaxes(showgrid=False)
st.plotly_chart(fig, width='stretch')

wow = weekly["week_over_week_pct"]
st.caption(
    f"Latest week: {weekly['latest_week_km']} km · "
    + ("week-over-week unavailable (prior week had no runs)" if wow is None
       else f"{wow:+}% vs previous week")
)

# -- activity table --------------------------------------------------------
st.subheader("Activities")
frame = pd.DataFrame([
    {
        "Date": r["start_date_utc"],
        "Distance (km)": round(r["distance_m"] / 1000.0, 2),
        "Duration (min)": round(r["duration_s"] / 60.0, 1),
        "Avg HR": r["avg_hr"],  # stays None/NaN when not reported
        "Source": r["source"],
    }
    for r in reversed(rows)
])
st.dataframe(frame, width='stretch', hide_index=True)

rejects = store.query("SELECT reason, COUNT(*) AS n FROM silver_rejects GROUP BY reason")
if rejects:
    with st.expander(f"Quarantined rows ({sum(r['n'] for r in rejects)})"):
        st.caption("Rows that failed validation. They never reach the metrics above.")
        st.dataframe(pd.DataFrame(rejects), width='stretch', hide_index=True)

# -- chat ------------------------------------------------------------------
st.subheader("Ask your data")

client = build_client()
mode_label = "LLM phrasing enabled" if client else "No LLM configured — deterministic answers"
st.caption(
    f"{mode_label}. Every number comes from whitelisted read-only tools; "
    "answers containing unverified numbers are rejected."
)

question = st.text_input(
    "Question",
    placeholder="e.g. What was my longest run? How far did I run this week? Am I improving?",
)

use_llm = st.toggle("Use LLM phrasing", value=bool(client), disabled=not client)

if question:
    result = answer(store, question, client=client if use_llm else None, today=date.today())
    st.markdown(f"**{result.answer}**")

    badge = {
        "deterministic": "Rule-based answer",
        "llm": "LLM-phrased (numbers verified)",
        "llm_rejected": "LLM answer rejected — fell back to rule-based",
        "error": "Invalid request",
    }.get(result.mode, result.mode)
    st.caption(f"Answer mode: {badge}" + (f" · {result.note}" if result.note else ""))

    with st.expander("Verified facts used"):
        st.json(result.facts)
