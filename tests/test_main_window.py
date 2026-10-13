"""Main window helpers: which settings changes reload the pages, the Prowlarr setup message and
the notice after an update."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from app.models import ProwlarrSetupResult, Settings
from app.ui.main_window import prowlarr_setup_message, settings_affect_pages, update_notice

# Settings no page shows: connections, how downloads are handed over, the first-run marker,
# updates.
OFF_PAGE_CHANGES: dict[str, Any] = {
    "prowlarr_url": "http://127.0.0.1:9696",
    "prowlarr_api_key": "0123456789abcdef0123456789abcdef",
    "prowlarr_seeded_url": "http://127.0.0.1:9696",
    "ds_url": "https://nas.local:5001",
    "ds_username": "beau",
    "ds_password": "secret",
    "ds_verify_tls": False,
    "ds_destination": "video/tv",
    "download_mode": "app",
    "setup_done": True,
    "auto_update": False,
    "last_run_version": "2.0.0",
}

PAGE_CHANGES: dict[str, Any] = {
    "quality": "720p",
    "download_dir": Path("Z:/watch"),
    "first_day": "Sunday",
    "weeks_to_show": 5,
    "show_suggestions": False,
    "tmdb_api_key": "tmdb",
    "excluded_keywords": "cam",
    "preferred_groups": "ELiTE",
    "disabled_indexers": "yts",
}


class SettingsAffectPagesTests(unittest.TestCase):
    def test_every_setting_is_classified(self) -> None:
        self.assertEqual(set(OFF_PAGE_CHANGES) | set(PAGE_CHANGES), set(Settings.model_fields))

    def test_off_page_settings_leave_the_pages_alone(self) -> None:
        before = Settings()
        for name, value in OFF_PAGE_CHANGES.items():
            with self.subTest(name):
                after = Settings(**{name: value})
                self.assertNotEqual(after, before)
                self.assertFalse(settings_affect_pages(before, after))
        everything = Settings(**OFF_PAGE_CHANGES)
        self.assertFalse(settings_affect_pages(before, everything))
        self.assertFalse(settings_affect_pages(everything, before))

    def test_other_settings_reload_the_pages(self) -> None:
        before = Settings()
        for name, value in PAGE_CHANGES.items():
            with self.subTest(name):
                after = Settings(**{name: value})
                self.assertTrue(settings_affect_pages(before, after))
                # Also when off-page settings change in the same save.
                mixed = Settings(**{name: value, **OFF_PAGE_CHANGES})
                self.assertTrue(settings_affect_pages(before, mixed))

    def test_no_change(self) -> None:
        self.assertFalse(settings_affect_pages(Settings(), Settings()))


class ProwlarrSetupMessageTests(unittest.TestCase):
    def test_messages(self) -> None:
        cases = {
            ProwlarrSetupResult(): None,
            ProwlarrSetupResult(connected=True): "Connected to Prowlarr on this PC",
            ProwlarrSetupResult(added=("Knaben", "LimeTorrents")): (
                "Prowlarr: added Knaben, LimeTorrents to your sites"
            ),
            ProwlarrSetupResult(connected=True, added=("Knaben",)): (
                "Connected to Prowlarr on this PC · added Knaben to your sites"
            ),
        }
        for result, message in cases.items():
            with self.subTest(result=result):
                self.assertEqual(prowlarr_setup_message(result), message)


class UpdateNoticeTests(unittest.TestCase):
    def test_shown_once_the_version_changed(self) -> None:
        self.assertEqual(update_notice("2.0.0", "2.1.0"), "Updated to v2.1.0")

    def test_not_shown_on_a_first_start_or_the_same_version(self) -> None:
        self.assertIsNone(update_notice("", "2.0.0"))  # first start, or before this existed
        self.assertIsNone(update_notice("2.0.0", "2.0.0"))

    def test_settings_defaults(self) -> None:
        settings = Settings.model_validate({"quality": "1080p"})  # stored before these existed
        self.assertTrue(settings.auto_update)
        self.assertEqual(settings.last_run_version, "")


if __name__ == "__main__":
    unittest.main()
