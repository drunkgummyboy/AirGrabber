"""Settings model: new Prowlarr / Download Station fields, URL validation, secret persistence."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from app.database import Database
from app.models import Settings

# Preferences as stored before the Prowlarr / Download Station fields existed.
OLD_STORED: dict[str, Any] = {
    "quality": "1080p",
    "download_dir": "Z:\\watch",
    "first_day": "Monday",
    "weeks_to_show": 3,
    "prev_weeks_to_show": 1,
    "tmdb_api_key": "tmdbkey",
    "excluded_keywords": ["cam", "telesync"],
    "preferred_groups": [],
}


class SettingsModelTests(unittest.TestCase):
    def test_old_stored_settings_load_with_new_defaults(self) -> None:
        settings = Settings.model_validate(json.loads(json.dumps(OLD_STORED)))
        self.assertEqual(settings.tmdb_api_key.get_secret_value(), "tmdbkey")
        self.assertEqual(settings.weeks_to_show, 3)
        # The retired "previous weeks" setting is ignored; suggestions are on by default.
        self.assertNotIn("prev_weeks_to_show", settings.model_dump())
        self.assertTrue(settings.show_suggestions)
        self.assertEqual(settings.prowlarr_url, "")
        self.assertEqual(settings.prowlarr_api_key.get_secret_value(), "")
        self.assertEqual(settings.ds_url, "")
        self.assertEqual(settings.ds_username, "")
        self.assertEqual(settings.ds_password.get_secret_value(), "")
        self.assertTrue(settings.ds_verify_tls)
        self.assertEqual(settings.ds_destination, "")
        self.assertEqual(settings.disabled_indexers, ())

    def test_urls_are_normalized(self) -> None:
        cases = {
            "": "",
            "   ": "",
            " http://192.168.1.150:5000 ": "http://192.168.1.150:5000",
            "https://nas.local:5001/": "https://nas.local:5001",
            "HTTPS://NAS.local:5001//": "HTTPS://NAS.local:5001",
            "http://localhost:9696/prowlarr/": "http://localhost:9696/prowlarr",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(Settings(ds_url=raw).ds_url, expected)
                self.assertEqual(Settings(prowlarr_url=raw).prowlarr_url, expected)

    def test_urls_must_be_full_http_addresses(self) -> None:
        for raw in ("nas.local:5001", "192.168.1.150", "ftp://nas", "https://", "http:/nas"):
            for field in ("ds_url", "prowlarr_url"):
                with self.subTest(raw=raw, field=field):
                    with self.assertRaises(ValidationError) as caught:
                        Settings(**{field: raw})
                    self.assertIn(
                        "Use a full address like https://nas.local:5001", str(caught.exception)
                    )

    def test_secrets_persist_verbatim_but_stay_masked(self) -> None:
        settings = Settings(
            tmdb_api_key=" tmdb ", prowlarr_api_key=" prowlarr ", ds_password=" DSMPASS123 "
        )
        stored = settings.model_dump(mode="json")
        self.assertEqual(stored["tmdb_api_key"], "tmdb")
        self.assertEqual(stored["prowlarr_api_key"], "prowlarr")
        self.assertEqual(stored["ds_password"], " DSMPASS123 ")  # passwords are never stripped
        for text in (repr(settings), str(settings), repr(settings.model_dump())):
            self.assertNotIn("DSMPASS123", text)
            self.assertNotIn("prowlarr'", text)
        self.assertEqual(Settings.model_validate(stored), settings)

    def test_every_secret_field_is_persisted(self) -> None:
        secrets = [
            name for name, field in Settings.model_fields.items() if field.annotation is SecretStr
        ]
        self.assertEqual(set(secrets), {"tmdb_api_key", "prowlarr_api_key", "ds_password"})
        settings = Settings(**{name: f"value-{name}" for name in secrets})
        stored = settings.model_dump(mode="json")
        for name in secrets:
            self.assertEqual(stored[name], f"value-{name}")

    def test_other_new_fields(self) -> None:
        settings = Settings(
            ds_username=" beau ",
            ds_destination=" /video/tv/ ",
            ds_verify_tls=False,
            disabled_indexers="EZTV, yts ,",
        )
        self.assertEqual(settings.ds_username, "beau")
        self.assertEqual(settings.ds_destination, "video/tv")
        self.assertFalse(settings.ds_verify_tls)
        self.assertEqual(settings.disabled_indexers, ("eztv", "yts"))


class SettingsStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_through_the_database(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "settings.db")
        self.addAsyncCleanup(db.close)
        settings = Settings(
            ds_url="http://192.168.1.150:5000",
            ds_username="beau",
            ds_password="DSMPASS123",
            prowlarr_url="http://localhost:9696",
            prowlarr_api_key="abc123",
            ds_verify_tls=False,
            disabled_indexers=("yts",),
        )
        await db.settings.save(settings)
        loaded = await db.settings.load()
        self.assertEqual(loaded, settings)
        self.assertEqual(loaded.ds_password.get_secret_value(), "DSMPASS123")

    async def test_retired_keys_are_ignored_on_load_and_removed_on_save(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.db"
        await (await Database.open(path)).close()  # create the schema
        # A database written by an older version, with settings that no longer exist.
        with contextlib.closing(sqlite3.connect(path)) as raw, raw:
            raw.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                [
                    ("retired_limit", "500"),
                    ("retired_flag", "true"),
                    ("prev_weeks_to_show", "2"),
                    ("weeks_to_show", "4"),
                ],
            )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        loaded = await db.settings.load()
        self.assertEqual(loaded.weeks_to_show, 4)
        self.assertNotIn("retired_limit", loaded.model_dump())
        await db.settings.save(loaded)
        with contextlib.closing(sqlite3.connect(path)) as raw:
            keys = {row[0] for row in raw.execute("SELECT key FROM settings")}
        self.assertEqual(keys, set(Settings.model_fields))
        self.assertEqual(await db.settings.load(), loaded)


if __name__ == "__main__":
    unittest.main()
