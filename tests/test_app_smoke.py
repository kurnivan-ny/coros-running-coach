"""Smoke test for the Streamlit dashboard.

Runs the real app.py through Streamlit's AppTest harness against a temporary
SQLite database, so a crash in the dashboard logic fails CI rather than only
showing up in a browser. Skipped automatically when Streamlit is absent.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover - streamlit is an app-only dependency
    AppTest = None  # type: ignore[assignment]

from corach.ingest import run_ingest
from corach.sources.synthetic import SyntheticSource
from corach.store import Store

APP = Path(__file__).resolve().parents[1] / "app.py"


@unittest.skipIf(AppTest is None, "streamlit not installed")
class AppSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "smoke.db")
        self._prior = os.environ.get("CORACH_SQLITE_PATH")
        os.environ["CORACH_SQLITE_PATH"] = self.db
        # Drop any Store cached by a previous test, so each test really uses its
        # own database rather than silently reusing the last one.
        import streamlit as st

        st.cache_resource.clear()

    def tearDown(self) -> None:
        if self._prior is None:
            os.environ.pop("CORACH_SQLITE_PATH", None)
        else:
            os.environ["CORACH_SQLITE_PATH"] = self._prior
        # The app caches its Store via st.cache_resource, which keeps the SQLite
        # file open; Windows refuses to delete it until the cache is cleared.
        try:
            import streamlit as st

            st.cache_resource.clear()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        try:
            self.tmp.cleanup()
        except PermissionError:  # pragma: no cover - Windows file locking
            pass

    def _seed(self) -> None:
        store = Store(self.db)
        store.init_schema()
        run_ingest(store, SyntheticSource(days=60))
        store.close()

    def test_app_renders_with_data(self) -> None:
        self._seed()
        at = AppTest.from_file(str(APP), default_timeout=90).run()

        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertTrue(any("COROS Running Coach" in t.value for t in at.title))
        # Synthetic data must be labelled as such.
        self.assertTrue(any("SYNTHETIC" in e.value for e in at.error))
        self.assertGreaterEqual(len(at.metric), 4)  # the four KPI cards

    def test_app_handles_empty_database(self) -> None:
        Store(self.db).init_schema()  # schema only, no activities
        at = AppTest.from_file(str(APP), default_timeout=90).run()

        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        self.assertTrue(any("No activities" in w.value for w in at.warning))

    def test_chat_box_answers_without_llm(self) -> None:
        self._seed()
        at = AppTest.from_file(str(APP), default_timeout=90).run()
        at.text_input[0].set_value("What was my longest run?").run()

        self.assertFalse(at.exception, f"app raised: {[str(e) for e in at.exception]}")
        rendered = " ".join(m.value for m in at.markdown)
        self.assertIn("longest run", rendered.lower())


if __name__ == "__main__":
    unittest.main()
