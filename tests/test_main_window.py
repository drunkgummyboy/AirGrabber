"""Main window helpers: which settings changes reload the pages, the Prowlarr setup message and
the notice after an update."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.models import AutoDownloaded, AutoError, AutoGaveUp, ProwlarrSetupResult, Settings
from app.ui.main_window import (
    AutoNotice,
    MainWindow,
    AutoNoticeThrottle,
    auto_notice,
    prowlarr_setup_message,
    settings_affect_pages,
    update_notice,
)

# Settings no page shows: connections, how downloads are handed over, the first-run marker,
# updates, the tray and starting with Windows.
OFF_PAGE_CHANGES: dict[str, Any] = {
    "prowlarr_url": "http://127.0.0.1:9696",
    "prowlarr_api_key": "0123456789abcdef0123456789abcdef",
    "prowlarr_seeded_url": "http://127.0.0.1:9696",
    "remote_kind": "qbittorrent",
    "remote_url": "https://nas.local:5001",
    "remote_username": "alex",
    "remote_password": "secret",
    "remote_verify_tls": False,
    "remote_destination": "/downloads/tv",
    "download_mode": "app",
    "auto_download_new_shows": True,
    "setup_done": True,
    "auto_update": False,
    "last_run_version": "2.0.0",
    "keep_in_tray": False,
    "tray_notice_shown": True,
    "start_with_windows": True,
    "subtitles_enabled": True,
    "subtitle_languages": ("en", "nl"),
    "subtitles_folder": r"\\NAS\video",
    "opensubtitles_username": "beau",
    "opensubtitles_password": "pw",
    "opensubtitles_own_key": "OWNKEY",
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
    "card_size": "s",  # the cards on every page but the calendar
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


class AutoNoticeTests(unittest.TestCase):
    def test_downloaded(self) -> None:
        self.assertEqual(
            auto_notice(AutoDownloaded(label="Severance S02E04", quality="1080p")),
            AutoNotice(
                title="Severance S02E04 is downloading",
                body="1080p · picked automatically",
                status="Severance S02E04 · downloading (picked automatically)",
            ),
        )

    def test_downloaded_without_a_known_quality(self) -> None:
        notice = auto_notice(AutoDownloaded(label="Severance S02E04"))
        self.assertEqual(notice.body, "Picked automatically")

    def test_gave_up(self) -> None:
        notice = auto_notice(AutoGaveUp(label="Severance S02E04"))
        self.assertEqual(notice.title, "Couldn't find Severance S02E04")
        self.assertEqual(
            notice.body,
            "No good download turned up in 3 days. Choose one yourself from the calendar.",
        )
        self.assertEqual(notice.status, f"{notice.title}. {notice.body}")

    def test_error(self) -> None:
        notice = auto_notice(AutoError(message="No torrent program is set up."))
        self.assertEqual(notice.title, "Automatic download didn't work")
        self.assertEqual(notice.body, "No torrent program is set up.")
        self.assertEqual(
            notice.status, "Automatic download didn't work: No torrent program is set up."
        )


class AutoNoticeThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.throttle = AutoNoticeThrottle(clock=lambda: self.now)

    def test_errors_at_most_once_per_hour(self) -> None:
        error = AutoError(message="x")
        self.assertTrue(self.throttle.allow(error))
        self.now += 3599
        self.assertFalse(self.throttle.allow(error))
        self.now += 1
        self.assertTrue(self.throttle.allow(error))
        self.assertFalse(self.throttle.allow(AutoError(message="another reason")))

    def test_other_events_are_never_held_back(self) -> None:
        self.assertTrue(self.throttle.allow(AutoError(message="x")))
        for _ in range(3):
            self.assertTrue(self.throttle.allow(AutoDownloaded(label="A S01E01")))
            self.assertTrue(self.throttle.allow(AutoGaveUp(label="A S01E02")))


class AutoEventRoutingTests(unittest.TestCase):
    """MainWindow._on_auto_event on a stand-in window: tray notification and/or status bar."""

    def window(self, *, tray_running: bool) -> Any:
        notes: list[tuple[str, str]] = []
        status: list[tuple[str, int]] = []
        tray = SimpleNamespace(
            running=tray_running, notify=lambda title, body: notes.append((title, body))
        )
        return SimpleNamespace(
            _auto_throttle=AutoNoticeThrottle(clock=lambda: 0.0),
            tray=tray,
            _report=lambda text, ms: status.append((text, ms)),
            notes=notes,
            status=status,
        )

    def test_with_the_tray(self) -> None:
        window = self.window(tray_running=True)
        MainWindow._on_auto_event(window, AutoDownloaded(label="Dune S01E01", quality="720p"))
        MainWindow._on_auto_event(window, AutoGaveUp(label="Dune S01E02"))
        MainWindow._on_auto_event(window, AutoError(message="Broken"))
        MainWindow._on_auto_event(window, AutoError(message="Broken"))  # within the hour
        self.assertEqual(
            window.notes,
            [
                ("Dune S01E01 is downloading", "720p · picked automatically"),
                (
                    "Couldn't find Dune S01E02",
                    "No good download turned up in 3 days. Choose one yourself from the calendar.",
                ),
                ("Automatic download didn't work", "Broken"),
            ],
        )
        self.assertEqual(
            window.status, [("Dune S01E01 · downloading (picked automatically)", 10_000)]
        )

    def test_without_the_tray_only_the_status_bar(self) -> None:
        window = self.window(tray_running=False)
        MainWindow._on_auto_event(window, AutoDownloaded(label="Dune S01E01", quality="720p"))
        MainWindow._on_auto_event(window, AutoError(message="Broken"))
        self.assertEqual(window.notes, [])
        self.assertEqual(
            [text for text, _ms in window.status],
            [
                "Dune S01E01 · downloading (picked automatically)",
                "Automatic download didn't work: Broken",
            ],
        )


if __name__ == "__main__":
    unittest.main()
