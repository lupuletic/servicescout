"""KuzuBackend regression tests.

These guard two bugs that were invisible because the rest of the suite only
exercised the JSON backend:
  - entity `confidence` was never stored/hydrated, so `min_confidence` filtering
    silently returned nothing on the (default) Kuzu backend;
  - the backend never reloaded after the DB was rebuilt on disk.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    import kuzu  # noqa: F401
    from servicescout.build_kuzu import build as build_kuzu
    from servicescout.storage_kuzu import KuzuBackend
    _KUZU_AVAILABLE = True
except Exception:  # noqa: BLE001 - environment without kuzu installed
    _KUZU_AVAILABLE = False


def _catalog(low_confidence: str = "low") -> dict:
    """Two components that both match 'payments'; one high, one tunable."""
    return {
        "summary": {"entities": 2, "relations": 0},
        "entities": [
            {
                "kind": "Component",
                "metadata": {"name": "payments-core", "description": "core payments service"},
                "spec": {"type": "service"},
                "confidence": "high",
            },
            {
                "kind": "Component",
                "metadata": {"name": "payments-edge", "description": "edge payments helper"},
                "spec": {"type": "service"},
                "confidence": low_confidence,
            },
        ],
        "relations": [],
    }


@unittest.skipUnless(_KUZU_AVAILABLE, "kuzu package not installed")
class KuzuConfidenceTests(unittest.TestCase):
    def _build(self, root: Path, catalog: dict) -> Path:
        cat_path = root / "catalog.json"
        cat_path.write_text(json.dumps(catalog), encoding="utf-8")
        db_path = root / "catalog.kuzu"
        build_kuzu(cat_path, db_path, dim=8, build_fts=True, build_vector=False)
        return db_path

    def test_confidence_hydrated_and_min_confidence_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._build(Path(tmp), _catalog(low_confidence="low"))
            backend = KuzuBackend(db_path)
            try:
                core = backend.describe("Component:payments-core")
                self.assertEqual(core.get("confidence"), "high")  # confidence now hydrated

                # No threshold → both match.
                refs = {h["ref"] for h in backend.search("payments", query_vector=None, limit=10)}
                self.assertIn("Component:payments-core", refs)
                self.assertIn("Component:payments-edge", refs)

                # min_confidence="high" → the low one is excluded (used to return []).
                hi = backend.search("payments", query_vector=None, limit=10, min_confidence="high")
                hi_refs = {h["ref"] for h in hi}
                self.assertIn("Component:payments-core", hi_refs)
                self.assertNotIn("Component:payments-edge", hi_refs)
                self.assertTrue(hi, "min_confidence='high' must not return an empty list")
            finally:
                backend._close()

    def test_reload_picks_up_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = self._build(root, _catalog(low_confidence="low"))
            backend = KuzuBackend(db_path)
            try:
                self.assertEqual(backend.describe("Component:payments-edge").get("confidence"), "low")
                # Release the file lock so we can rebuild in-process, then bump the
                # DB confidence and ensure the next call serves the fresh data.
                backend._close()
                self._build(root, _catalog(low_confidence="high"))
                # Guarantee mtime advanced so the change is detected deterministically.
                st = db_path.stat()
                os.utime(db_path, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))

                # describe() triggers _maybe_reload() → reopen with new data.
                self.assertEqual(backend.describe("Component:payments-edge").get("confidence"), "high")
            finally:
                backend._close()


if __name__ == "__main__":
    unittest.main()
