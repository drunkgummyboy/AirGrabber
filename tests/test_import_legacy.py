"""tools.import_legacy against a real SQLite file and hand-written 1.x JSON."""

from __future__ import annotations

import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from app.database import Database

# tools/ is not a package; importing by name keeps mypy from seeing the module twice.
import_legacy = importlib.import_module("tools.import_legacy").import_legacy


class ImportLegacyTest(unittest.TestCase):
    def test_auto_flag_is_ignored(self) -> None:
        async def run(tmp: Path) -> None:
            followed = {"101": {"name": "On", "auto": True}, "102": {"name": "Off", "auto": False}}
            (tmp / "followed_shows.json").write_text(json.dumps(followed), encoding="utf-8")
            db = await Database.open(tmp / "library.db")
            try:
                report = await import_legacy(tmp, db)
                self.assertEqual(report.shows, 2)
                rows = await db.fetch_all(
                    "SELECT show_id, auto_download, auto_since FROM tracked_shows ORDER BY show_id"
                )
                self.assertEqual(
                    [(r["show_id"], r["auto_download"], r["auto_since"]) for r in rows],
                    [(101, 0, None), (102, 0, None)],
                )
            finally:
                await db.close()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
