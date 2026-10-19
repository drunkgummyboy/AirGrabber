"""Settings model: Prowlarr and remote torrent program fields, URL validation, secret persistence,
the move from the Download Station (`ds_*`) settings."""

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
from app.models import DownloadMode, RemoteKind, Settings

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
        self.assertIs(settings.remote_kind, RemoteKind.SYNOLOGY)
        self.assertFalse(settings.remote_configured)
        self.assertEqual(settings.remote_url, "")
        self.assertEqual(settings.remote_username, "")
        self.assertEqual(settings.remote_password.get_secret_value(), "")
        self.assertTrue(settings.remote_verify_tls)
        self.assertEqual(settings.remote_destination, "")
        self.assertEqual(settings.disabled_indexers, ())

    def test_urls_are_normalized(self) -> None:
        cases = {
            "": "",
            "   ": "",
            " http://192.168.1.20:5000 ": "http://192.168.1.20:5000",
            "https://nas.local:5001/": "https://nas.local:5001",
            "HTTPS://NAS.local:5001//": "HTTPS://NAS.local:5001",
            "http://localhost:9696/prowlarr/": "http://localhost:9696/prowlarr",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(Settings(remote_url=raw).remote_url, expected)
                self.assertEqual(Settings(prowlarr_url=raw).prowlarr_url, expected)

    def test_urls_must_be_full_http_addresses(self) -> None:
        for raw in ("nas.local:5001", "192.168.1.20", "ftp://nas", "https://", "http:/nas"):
            for field in ("remote_url", "prowlarr_url"):
                with self.subTest(raw=raw, field=field):
                    with self.assertRaises(ValidationError) as caught:
                        Settings(**{field: raw})
                    self.assertIn(
                        "Use a full address like https://nas.local:5001", str(caught.exception)
                    )

    def test_secrets_persist_verbatim_but_stay_masked(self) -> None:
        settings = Settings(
            tmdb_api_key=" tmdb ", prowlarr_api_key=" prowlarr ", remote_password=" DSMPASS123 "
        )
        stored = settings.model_dump(mode="json")
        self.assertEqual(stored["tmdb_api_key"], "tmdb")
        self.assertEqual(stored["prowlarr_api_key"], "prowlarr")
        self.assertEqual(stored["remote_password"], " DSMPASS123 ")  # passwords are never stripped
        for text in (repr(settings), str(settings), repr(settings.model_dump())):
            self.assertNotIn("DSMPASS123", text)
            self.assertNotIn("prowlarr'", text)
        self.assertEqual(Settings.model_validate(stored), settings)

    def test_every_secret_field_is_persisted(self) -> None:
        secrets = [
            name for name, field in Settings.model_fields.items() if field.annotation is SecretStr
        ]
        self.assertEqual(
            set(secrets),
            {
                "tmdb_api_key",
                "prowlarr_api_key",
                "remote_password",
                "opensubtitles_password",
                "opensubtitles_own_key",
            },
        )
        settings = Settings(**{name: f"value-{name}" for name in secrets})
        stored = settings.model_dump(mode="json")
        for name in secrets:
            self.assertEqual(stored[name], f"value-{name}")

    def test_other_new_fields(self) -> None:
        settings = Settings(
            remote_username=" beau ",
            remote_destination=" /video/tv/ ",
            remote_verify_tls=False,
            disabled_indexers="EZTV, yts ,",
        )
        self.assertEqual(settings.remote_username, "beau")
        # Only spaces: qBittorrent and Transmission want absolute paths ("/downloads/tv").
        self.assertEqual(settings.remote_destination, "/video/tv/")
        self.assertFalse(settings.remote_verify_tls)
        self.assertEqual(settings.disabled_indexers, ("eztv", "yts"))


# The owner's stored settings from 2.0: FOLDER mode with Download Station as the magnet fallback.
DOWNLOAD_STATION_STORED: dict[str, Any] = {
    **OLD_STORED,
    "download_mode": "folder",
    "ds_url": "http://192.168.1.20:5000",
    "ds_username": "beau",
    "ds_password": "DSMPASS123",
}


class DownloadStationMigrationTests(unittest.TestCase):
    def test_ds_settings_become_the_remote_program(self) -> None:
        settings = Settings.model_validate(json.loads(json.dumps(DOWNLOAD_STATION_STORED)))
        self.assertIs(settings.remote_kind, RemoteKind.SYNOLOGY)
        self.assertEqual(settings.remote_url, "http://192.168.1.20:5000")
        self.assertEqual(settings.remote_username, "beau")
        self.assertEqual(settings.remote_password.get_secret_value(), "DSMPASS123")
        self.assertTrue(settings.remote_verify_tls)
        self.assertEqual(settings.remote_destination, "")
        self.assertIs(settings.download_mode, DownloadMode.FOLDER)  # the owner keeps the mode
        self.assertTrue(settings.remote_configured)  # and the magnet fallback
        self.assertNotIn("ds_url", settings.model_dump())

    def test_every_ds_setting_moves(self) -> None:
        settings = Settings.model_validate(
            {"ds_url": "https://nas.local:5001/", "ds_username": " beau ", "ds_password": "pw",
             "ds_verify_tls": False, "ds_destination": "video/tv"}
        )
        self.assertEqual(settings.remote_url, "https://nas.local:5001")
        self.assertEqual(settings.remote_username, "beau")
        self.assertFalse(settings.remote_verify_tls)
        self.assertEqual(settings.remote_destination, "video/tv")

    def test_remote_settings_win_over_leftover_ds_keys(self) -> None:
        settings = Settings.model_validate(
            {"ds_url": "http://old:5000", "remote_kind": "qbittorrent",
             "remote_url": "http://new:8080"}
        )
        self.assertIs(settings.remote_kind, RemoteKind.QBITTORRENT)
        self.assertEqual(settings.remote_url, "http://new:8080")

    def test_configured_needs_an_account_only_for_download_station(self) -> None:
        for kind, expected in (
            (RemoteKind.SYNOLOGY, False),
            (RemoteKind.QBITTORRENT, True),
            (RemoteKind.TRANSMISSION, True),
        ):
            with self.subTest(kind=kind):
                settings = Settings(remote_kind=kind, remote_url="http://nas:1234")
                self.assertEqual(settings.remote_configured, expected)
        self.assertTrue(
            Settings(remote_url="http://nas:5000", remote_username="u",
                     remote_password="p").remote_configured
        )
        self.assertFalse(Settings(remote_kind=RemoteKind.QBITTORRENT).remote_configured)

    def test_program_names(self) -> None:
        self.assertEqual(
            [kind.program for kind in RemoteKind],
            ["Download Station", "qBittorrent", "Transmission"],
        )


class SettingsStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_ds_keys_migrate_on_load_and_are_pruned_on_the_next_save(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.db"
        await (await Database.open(path)).close()  # create the schema
        with contextlib.closing(sqlite3.connect(path)) as raw, raw:
            raw.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                [(key, json.dumps(value)) for key, value in DOWNLOAD_STATION_STORED.items()],
            )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        loaded = await db.settings.load()
        self.assertEqual(loaded.remote_url, "http://192.168.1.20:5000")
        self.assertEqual(loaded.remote_password.get_secret_value(), "DSMPASS123")
        await db.settings.save(loaded.model_copy(update={"weeks_to_show": 4}))
        with contextlib.closing(sqlite3.connect(path)) as raw:
            stored = {row[0]: json.loads(row[1]) for row in raw.execute("SELECT key, value FROM settings")}
        self.assertFalse([key for key in stored if key.startswith("ds_")])
        self.assertEqual(stored["remote_kind"], "synology")
        self.assertEqual(stored["remote_url"], "http://192.168.1.20:5000")
        self.assertEqual(stored["remote_username"], "beau")
        self.assertEqual(stored["remote_password"], "DSMPASS123")
        reloaded = await db.settings.load()
        self.assertEqual(reloaded.remote_password.get_secret_value(), "DSMPASS123")
        self.assertTrue(reloaded.remote_configured)

    async def test_round_trip_through_the_database(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "settings.db")
        self.addAsyncCleanup(db.close)
        settings = Settings(
            remote_url="http://192.168.1.20:5000",
            remote_username="beau",
            remote_password="DSMPASS123",
            prowlarr_url="http://localhost:9696",
            prowlarr_api_key="abc123",
            remote_verify_tls=False,
            disabled_indexers=("yts",),
        )
        await db.settings.save(settings)
        loaded = await db.settings.load()
        self.assertEqual(loaded, settings)
        self.assertEqual(loaded.remote_password.get_secret_value(), "DSMPASS123")

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
