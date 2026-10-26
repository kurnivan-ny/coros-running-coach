"""Smoke test for the Streamlit dashboard.

Runs the real app.py through Streamlit's AppTest harness against a temporary
SQLite database holding hand-written gold rows, so a crash in the dashboard
logic fails CI rather than only showing up in a browser. Skipped automatically
when Streamlit is absent.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover - streamlit is an app-only dependency
    AppTest = None  # type: ignore[assignment]

from corach.store import Store
from helpers import add_activity, add_load, add_weekly, gold_store  # noqa: F401

APP = Path(__file__).resolve().parents[1] / "app.py"


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


@unittest.skipIf(AppTest is None, "streamlit not installed")
class AppSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "smoke.db")
        self._prior = {k: os.environ.get(k) for k in
                       ("CORACH_SQLITE_PATH", "DATABASE_URL", "PGHOST")}
        os.environ["CORACH_SQLITE_PATH"] = self.db
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("PGHOST", None)
        import streamlit as st

        st.cache_resource.clear()
        st.cache_data.clear()

    def tearDown(self) -> None:
        for key, value in self._prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            import streamlit as st

            st.cache_resource.clear()  # releases the open SQLite file (Windows)
            st.cache_data.clear()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        try:
            self.tmp.cleanup()
        except PermissionError:  # pragma: no cover - Windows file locking
            pass

    def _seed(self, source: str = "coros_snapshot") -> None:
        store = Store(self.db)
        store.init_schema()
        conn = store.connect()
        from helpers import _TABLES

        conn.executescript(_TABLES)
        conn.commit()
        today = date.today()
        for i in range(1, 60, 2):
            day = (today - timedelta(days=i)).isoformat()
            add_activity(store, f"r{i}", day, distance_km=8.0 + i % 5, duration_min=50,
                         trimp=55, source=source)
        add_activity(store, "s1", (today - timedelta(days=2)).isoformat(), sport="strength",
                     name="Strength", distance_km=None, duration_min=40, sets=18,
                     avg_hr=110, trimp=12, source=source)
        for w in range(1, 9):
            add_weekly(store, (monday(today) - timedelta(weeks=w)).isoformat(),
                       distance_km=28.0 + w)
        add_load(store, today.isoformat(), acwr=0.9, zone="optimal")
        store.close()

    def run_app(self):
        return AppTest.from_file(str(APP), default_timeout=90).run()

    def test_app_renders_all_tabs_with_data(self) -> None:
        self._seed()
        at = self.run_app()
        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertTrue(any("COROS Coach" in t.value for t in at.title))
        self.assertEqual(len(at.tabs), 8)
        self.assertGreaterEqual(len(at.metric), 4)
        self.assertFalse(any("SYNTHETIC" in e.value for e in at.error))

    def test_synthetic_data_is_labelled(self) -> None:
        self._seed(source="synthetic")
        at = self.run_app()
        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertTrue(any("SYNTHETIC" in e.value for e in at.error))

    def test_app_explains_a_missing_gold_layer(self) -> None:
        Store(self.db).init_schema()  # bronze only, dbt never ran
        at = self.run_app()
        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertTrue(any("gold layer" in e.value for e in at.error))

    def test_app_handles_empty_gold_tables(self) -> None:
        store = Store(self.db)
        store.init_schema()
        from helpers import _TABLES

        store.connect().executescript(_TABLES)
        store.close()
        at = self.run_app()
        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertTrue(any("No activities" in w.value for w in at.warning))

    def test_chat_box_answers_without_llm(self) -> None:
        self._seed()
        at = self.run_app()
        at.text_input[0].set_value("What was my longest run?").run()
        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertIn("longest run", " ".join(m.value for m in at.markdown).lower())


if __name__ == "__main__":
    unittest.main()
