"""Main window helpers: which settings changes reload the pages, the Prowlarr setup message and
the notice after an update; MediaForge's wiring (app/ui/forge_link.py and the main window's
side of it) with fakes for the Forge host, the mailbox and the window."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.models import (
    AutoDownloaded,
    AutoError,
    AutoGaveUp,
    LibraryStats,
    PairedPhone,
    ProwlarrSetupResult,
    Settings,
)
from app.single_instance import ForgeRequest
from app.ui.forge_link import QuitGate, RequestPump, quits_after_forge
from app.ui.settings_dialog import SECTION_LOOKS
from app.i18n import t
from app.update_guard import WentBack
from app.ui.main_window import (
    AutoNotice,
    MainWindow,
    Mode,
    AutoNoticeThrottle,
    auto_notice,
    prowlarr_setup_message,
    settings_affect_pages,
    typing_in,
    update_notice,
    went_back_notice,
)

# Settings no page shows: connections, how downloads are handed over, the first-run marker,
# updates, the tray and starting with Windows.
OFF_PAGE_CHANGES: dict[str, Any] = {
    "prowlarr_url": "http://127.0.0.1:9696",
    "prowlarr_api_key": "0123456789abcdef0123456789abcdef",
    "prowlarr_seeded_url": "http://127.0.0.1:9696",
    "remote_kind": "qbittorrent",
    "remote_url": "https://nas.local:5001",
    "movie_remote_destination": "/downloads/movies",
    "remote_username": "alex",
    "remote_password": "secret",
    "remote_verify_tls": False,
    "remote_destination": "/downloads/tv",
    "download_mode": "app",
    "auto_download_new_shows": True,
    "setup_done": True,
    "get_started_shown": True,
    "auto_update": False,
    "last_run_version": "2.0.0",
    "keep_in_tray": False,
    "tray_notice_shown": True,
    "start_with_windows": True,
    "subtitles_enabled": True,
    "subtitle_languages": ("en", "nl"),
    "subtitles_folder": r"\\NAS\video",
    "subtitles_mode": "ask",
    "subtitles_hearing_impaired": "prefer",
    "subtitles_for": "films",
    "opensubtitles_username": "beau",
    "opensubtitles_password": "pw",
    "opensubtitles_own_key": "OWNKEY",
    # My shows re-arranges its own cards.
    "tracked_sort": "next_episode",
    "tracked_filter": "missing",
    # Discover and Movies re-filter their own shelves.
    "hide_owned_discover": True,
    "hide_owned_movies": True,
    # Light / dark: every widget repaints itself.
    "theme_mode": "light",
    # The language takes effect at the next start.
    "language": "nl",
    # The info window's layout: a window reads it when it opens.
    "info_layout": "tabs",
    # The spotlight band turning by itself: the band follows it.
    "spotlight_motion": False,
    # The accent colour applies at the next start.
    "accent": "ocean",
    # Who is selected: a switch reloads the pages itself (MainWindow._on_profiles_changed).
    "current_profile": 2,
    # Plex: a check that changes what counts as had reloads the pages itself.
    "plex_client_id": "00000000-0000-4000-8000-000000000001",
    "plex_token": "fake-plex-token",
    "plex_server_id": "a1b2",
    "plex_server_name": "Alex-NAS",
    "plex_server_uri": "http://192.168.1.20:32400",
    "plex_sections_off": ("3",),
    "phone_connection_on": True,
    "phone_pc_id": "a1b2",
    "phone_fingerprint": "c3d4",
    "recent_searches": ("Reacher",),
    "blocked_groups": ("FAKE",),
    "learned_groups": ("NTb",),
    "auto_wait_hours": 6,
    "stuck_swap_hours": 12,
    "text_size": "larger",
    "auto_paused_until": datetime(2026, 11, 8, 12, tzinfo=UTC),
    "phone_devices": (
        PairedPhone(
            id="a",
            name="Phone",
            token_hash="h",
            profile_id=1,
            paired_at=datetime(2026, 11, 6, tzinfo=UTC),
        ),
    ),
}

PAGE_CHANGES: dict[str, Any] = {
    "quality_tv": "720p",
    "quality_movie": "2160p",
    "size_tv_min_gb": 1.5,
    "size_tv_max_gb": 4,
    "size_movie_min_gb": 6,
    "size_movie_max_gb": 15,
    "download_dir": Path("Z:/watch"),
    "movie_download_dir": Path("Z:/films"),
    "first_day": "Sunday",
    "calendar_layout": "rolling",
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


class ClickOutsideTests(unittest.TestCase):
    @staticmethod
    def panel(*, mapped: bool = True) -> Any:
        panel = SimpleNamespace(destroyed=False, winfo_ismapped=lambda: mapped, winfo_exists=lambda: True)

        def destroy() -> None:
            panel.destroyed = True

        panel.destroy = destroy
        return panel

    def test_only_panels_on_screen_close(self) -> None:
        from app.ui.movie_details import MovieWindows
        from app.ui.show_details import ShowWindows

        for windows in (ShowWindows, MovieWindows):
            shown, just_opened = self.panel(), self.panel(mapped=False)
            fake: Any = SimpleNamespace(_live=lambda s=shown, j=just_opened: {1: s, 2: j})
            windows.close_shown(fake)
            self.assertTrue(shown.destroyed)
            self.assertFalse(just_opened.destroyed)

    def test_a_click_on_the_main_window_closes_panels_and_the_results(self) -> None:
        closed: list[str] = []
        results = self.panel()
        window: Any = SimpleNamespace(
            _close_info_panels=lambda: closed.append("panels"), _results_window=results
        )
        MainWindow._clicked_in_main(window)
        self.assertEqual(closed, ["panels"])
        self.assertTrue(results.destroyed)
        self.assertIsNone(window._results_window)

    def test_a_click_with_no_results_open_only_closes_panels(self) -> None:
        closed: list[str] = []
        window: Any = SimpleNamespace(
            _close_info_panels=lambda: closed.append("panels"), _results_window=None
        )
        MainWindow._clicked_in_main(window)
        self.assertEqual(closed, ["panels"])


class TypingInTests(unittest.TestCase):
    """F5 leaves text fields alone."""

    def test_text_fields(self) -> None:
        for name in ("Entry", "TEntry", "Text", "Spinbox", "TCombobox"):
            with self.subTest(name):
                self.assertTrue(typing_in(SimpleNamespace(winfo_class=lambda name=name: name)))

    def test_other_widgets(self) -> None:
        for name in ("Canvas", "Frame", "Button", "Tk", "Toplevel"):
            with self.subTest(name):
                self.assertFalse(typing_in(SimpleNamespace(winfo_class=lambda name=name: name)))
        self.assertFalse(typing_in(".!ctkframe.!entry"))  # a widget tkinter doesn't know


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
    def test_going_back_says_what_happened(self) -> None:
        asked = went_back_notice(WentBack("2.1.0", "2.0.0", asked=True))
        self.assertEqual(asked, "Back on v2.0.0. AirGrabber waits for a version newer than v2.1.0.")
        failed = went_back_notice(WentBack("2.1.0", "2.0.0", asked=False))
        self.assertIn("v2.1.0 didn't start", failed)

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


# -- MediaForge ----------------------------------------------------------------------------------

FILE_A = "C:\\Media\\Alex.S01E01.mkv"
FILE_B = "C:\\Media\\Alex.S01E02.mkv"


async def _until(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.005)


class FakeMailbox:
    """InstanceLock's mailbox: the event (`signal`) and the folder (`waiting`)."""

    def __init__(self, *waiting: ForgeRequest) -> None:
        self.waiting = list(waiting)
        self.signalled = False
        self.takes = 0

    def signal(self, *requests: ForgeRequest) -> None:
        self.waiting += requests
        self.signalled = True

    def forge_requested(self) -> bool:
        signalled, self.signalled = self.signalled, False
        return signalled

    def take_requests(self) -> list[ForgeRequest]:
        self.takes += 1
        taken, self.waiting = self.waiting, []
        return taken


class RequestPumpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.handled: list[ForgeRequest] = []
        self.now = 0.0

    def pump(self, mailbox: FakeMailbox | None, start: ForgeRequest | None = None) -> RequestPump:
        pump = RequestPump(
            mailbox,
            self.handled.append,
            start_request=start,
            poll_s=0.01,
            folder_poll_s=3.0,
            clock=lambda: self.now,
        )
        task = asyncio.create_task(pump.run())

        async def stop() -> None:
            pump.stop()
            await asyncio.wait_for(task, 2.0)

        self.addAsyncCleanup(stop)
        return pump

    async def test_the_start_request_first_then_what_waited(self) -> None:
        start = ForgeRequest((FILE_A,), "Plex", True)
        waiting = ForgeRequest((FILE_B,), "Plex", True)
        mailbox = FakeMailbox(waiting)
        self.pump(mailbox, start)
        await _until(lambda: len(self.handled) == 2)
        self.assertEqual(self.handled, [start, waiting])

    async def test_a_copy_of_the_start_request_in_the_folder_is_dropped_once(self) -> None:
        # Its sender couldn't delete its own file and started AirGrabber anyway.
        start = ForgeRequest((FILE_A,), "Plex", True)
        other = ForgeRequest((FILE_B,), "Plex", True)
        mailbox = FakeMailbox(start, other, start)
        self.pump(mailbox, start)
        await _until(lambda: mailbox.takes >= 1)
        self.assertEqual(self.handled, [start, other, start])  # a second copy is a new request
        # Later, the same request again (a second right-click) counts.
        mailbox.signal(start)
        await _until(lambda: len(self.handled) == 4)
        self.assertEqual(self.handled[-1], start)

    async def test_the_event_wakes_it(self) -> None:
        mailbox = FakeMailbox()
        self.pump(mailbox)
        await _until(lambda: mailbox.takes == 1)  # the look at once when it starts
        await asyncio.sleep(0.05)
        self.assertEqual(mailbox.takes, 1)  # no event, folder time not up: nothing read
        request = ForgeRequest((FILE_A,), None, True)
        mailbox.signal(request)
        await _until(lambda: self.handled == [request])

    async def test_the_folder_is_read_every_few_seconds_without_the_event(self) -> None:
        mailbox = FakeMailbox()
        self.pump(mailbox)
        await _until(lambda: mailbox.takes == 1)
        request = ForgeRequest((FILE_A,))
        mailbox.waiting.append(request)  # an elevated sender couldn't set the event
        await asyncio.sleep(0.05)
        self.assertEqual(self.handled, [])
        self.now += 3.0
        await _until(lambda: self.handled == [request])

    async def test_nothing_is_taken_once_stopped(self) -> None:
        mailbox = FakeMailbox()
        pump = self.pump(mailbox)
        await _until(lambda: mailbox.takes == 1)
        pump.stop()
        mailbox.signal(ForgeRequest((FILE_A,)))
        await asyncio.sleep(0.05)
        self.assertEqual(self.handled, [])
        self.assertEqual(len(mailbox.waiting), 1)  # still in the folder for the next start
        self.assertTrue(pump.stopped)

    async def test_a_stop_while_handing_on_leaves_the_rest(self) -> None:
        first, second = ForgeRequest((FILE_A,)), ForgeRequest((FILE_B,))
        mailbox = FakeMailbox(first, second)
        pump: RequestPump | None = None

        def handle(request: ForgeRequest) -> None:
            self.handled.append(request)
            assert pump is not None
            pump.stop()  # e.g. the request led to quitting

        pump = RequestPump(mailbox, handle, poll_s=0.01)
        await asyncio.wait_for(pump.run(), 2.0)
        self.assertEqual(self.handled, [first])

    async def test_without_a_mailbox_only_the_start_request(self) -> None:
        start = ForgeRequest()
        await asyncio.wait_for(RequestPump(None, self.handled.append, start_request=start).run(), 2.0)
        self.assertEqual(self.handled, [start])

    async def test_a_failing_handler_is_logged_and_the_pump_goes_on(self) -> None:
        mailbox = FakeMailbox(ForgeRequest((FILE_A,)))
        calls: list[ForgeRequest] = []

        def handle(request: ForgeRequest) -> None:
            calls.append(request)
            if len(calls) == 1:
                raise ValueError("boom")

        pump = RequestPump(mailbox, handle, poll_s=0.01)
        task = asyncio.create_task(pump.run())
        with self.assertLogs("app.ui.forge_link", "ERROR"):
            await _until(lambda: len(calls) == 1)
        mailbox.signal(ForgeRequest((FILE_B,)))
        await _until(lambda: len(calls) == 2)
        pump.stop()
        await asyncio.wait_for(task, 2.0)


class FakeHost:
    """ForgeHost's side the gate uses: `may_close` says no while a run is going."""

    def __init__(self, *, busy: bool = False, shutdown_s: float = 0.0) -> None:
        self.busy = busy
        self.shutdown_s = shutdown_s
        self.retry: Callable[[], None] | None = None
        self.asked = 0
        self.events: list[str] = []

    def may_close(self, retry: Callable[[], None]) -> bool:
        self.asked += 1
        if self.busy:
            self.retry = retry
            return False
        return True

    async def shutdown(self) -> None:
        self.events.append("windows")
        await asyncio.sleep(self.shutdown_s)

    def run_ended(self) -> None:
        self.busy = False
        assert self.retry is not None
        self.retry()


class QuitGateTests(unittest.IsolatedAsyncioTestCase):
    def gate(self, host: FakeHost, *, close_s: float = 0.0, timeout_s: float = 5.0) -> QuitGate:
        self.host = host
        self.finished = asyncio.Event()

        async def close_forge() -> None:
            host.events.append("forge")
            await asyncio.sleep(close_s)
            host.events.append("forge closed")

        def finish() -> None:
            host.events.append("finish")
            self.finished.set()

        return QuitGate(
            host,
            close_forge=close_forge,
            finish=finish,
            on_asked=lambda: host.events.append("asked"),
            on_leaving=lambda: host.events.append("leaving"),
            timeout_s=timeout_s,
        )

    async def test_quits_at_once_when_forge_may_close(self) -> None:
        gate = self.gate(FakeHost())
        gate.request()
        self.assertTrue(gate.asked and gate.leaving)
        await asyncio.wait_for(self.finished.wait(), 2.0)
        self.assertEqual(
            self.host.events, ["asked", "leaving", "windows", "forge", "forge closed", "finish"]
        )
        gate.request()  # twice: nothing more
        await asyncio.sleep(0.01)
        self.assertEqual(self.host.events.count("finish"), 1)

    async def test_waits_for_a_forge_run_to_finish_its_file(self) -> None:
        host = FakeHost(busy=True)
        gate = self.gate(host)
        gate.request()
        gate.request()  # Quit clicked again meanwhile
        await asyncio.sleep(0.02)
        self.assertTrue(gate.asked)
        self.assertFalse(gate.leaving)
        self.assertEqual(host.events, ["asked"])  # the pump stopped; nothing closed yet
        host.run_ended()  # the run stopped after its current file: it calls retry
        await asyncio.wait_for(self.finished.wait(), 2.0)
        self.assertEqual(host.events[-1], "finish")
        self.assertEqual(host.events.count("asked"), 1)

    async def test_a_slow_forge_cannot_hold_quitting_up(self) -> None:
        gate = self.gate(FakeHost(shutdown_s=10.0), close_s=10.0, timeout_s=0.05)
        with self.assertLogs("app.ui.forge_link", "WARNING") as logs:
            gate.request()
            await asyncio.wait_for(self.finished.wait(), 2.0)
        self.assertEqual(self.host.events, ["asked", "leaving", "windows", "forge", "finish"])
        self.assertEqual(len(logs.records), 2)

    async def test_a_failing_shutdown_still_quits(self) -> None:
        host = FakeHost()

        async def broken() -> None:
            raise RuntimeError("boom")

        host.shutdown = broken  # type: ignore[method-assign]
        gate = self.gate(host)
        with self.assertLogs("app.ui.forge_link", "ERROR"):
            gate.request()
            await asyncio.wait_for(self.finished.wait(), 2.0)
        self.assertIn("forge closed", host.events)


class QuitsAfterForgeTests(unittest.TestCase):
    """D5: a start for Forge leaves when its last confirm window closes, unless it stays in the
    tray."""

    def quits(self, **changes: bool) -> bool:
        state = {
            "started_for_forge": True,
            "forge_windows_open": False,
            "main_hidden": True,
            "keep_in_tray": False,
            "tray_running": True,
        }
        state.update(changes)
        return quits_after_forge(**state)

    def test_keep_in_tray_decides(self) -> None:
        self.assertTrue(self.quits(keep_in_tray=False))
        self.assertFalse(self.quits(keep_in_tray=True))
        self.assertTrue(self.quits(keep_in_tray=True, tray_running=False))  # nothing to stay in

    def test_only_a_start_for_forge_with_nothing_else_showing(self) -> None:
        for name, value in (
            ("started_for_forge", False),  # a plain start (or --tray)
            ("forge_windows_open", True),  # another confirm window, or "Open in MediaForge"
            ("main_hidden", False),  # the owner opened AirGrabber meanwhile
        ):
            with self.subTest(name):
                self.assertFalse(self.quits(**{name: value}))


class ForgeWiringTests(unittest.TestCase):
    """The main window's side, on a stand-in window."""

    def window(
        self,
        *,
        started_for_forge: bool = True,
        hidden: bool = True,
        keep_in_tray: bool = False,
        windows_open: bool = False,
        asked: bool = False,
    ) -> Any:
        calls: list[Any] = []
        settings = SimpleNamespace(current=Settings(keep_in_tray=keep_in_tray))
        window: Any = SimpleNamespace(
            calls=calls,
            _started_for_forge=started_for_forge,
            _services=SimpleNamespace(settings=settings),
            _forge=SimpleNamespace(windows_open=windows_open),
            _quit_gate=SimpleNamespace(asked=asked, request=lambda: calls.append("quit")),
            _band=SimpleNamespace(set_forge_count=lambda count: calls.append(("badge", count))),
            tray=SimpleNamespace(
                running=True,
                show_window=lambda: calls.append("show"),
                set_forge_count=lambda count: calls.append(("count", count)),
                notify=lambda title, text: calls.append(("notify", title, text)),
            ),
            state=lambda: "withdrawn" if hidden else "normal",
            _open_settings=lambda section=None: calls.append(("settings", section)),
            restart_requested=False,
        )
        window.quit_app = lambda: MainWindow.quit_app(window)
        return window

    def test_the_last_confirm_window_closing_quits_a_start_for_forge(self) -> None:
        window = self.window()
        MainWindow._on_forge_closed(window)
        self.assertEqual(window.calls, ["quit"])

    def test_it_stays_in_the_tray_with_keep_in_tray(self) -> None:
        window = self.window(keep_in_tray=True)
        MainWindow._on_forge_closed(window)
        self.assertEqual(window.calls, [])

    def test_other_starts_and_open_windows_stay(self) -> None:
        for changes in (
            {"started_for_forge": False},
            {"hidden": False},
            {"windows_open": True},
            {"asked": True},  # quitting already
        ):
            with self.subTest(**changes):
                window = self.window(**changes)
                MainWindow._on_forge_closed(window)
                self.assertEqual(window.calls, [])

    def test_restart_waits_for_forge_too(self) -> None:
        window = self.window()
        MainWindow.restart(window)
        self.assertTrue(window.restart_requested)
        self.assertEqual(window.calls, ["quit"])  # through the gate, not destroy()

    def test_the_renamer_reaches_the_tray(self) -> None:
        window = self.window()
        MainWindow._on_forge_needs(window, 3)
        MainWindow._on_forge_notice(window, "MediaForge", "2 files need you")
        self.assertEqual(
            window.calls,
            [("badge", 3), ("count", 3), ("notify", "MediaForge", "2 files need you")],
        )

    def test_an_open_settings_dialog_goes_to_the_section(self) -> None:
        shown: list[Any] = []
        dialog = SimpleNamespace(
            winfo_exists=lambda: True,
            show_section=lambda section: shown.append(section),
            lift=lambda: shown.append("lift"),
            focus_force=lambda: shown.append("focus"),
        )
        window: Any = SimpleNamespace(_settings_dialog=dialog)
        MainWindow._open_settings(window, SECTION_LOOKS)
        self.assertEqual(shown, [SECTION_LOOKS, "lift", "focus"])
        shown.clear()
        MainWindow._open_settings(window)  # the band's ⚙: the page it is on
        self.assertEqual(shown, ["lift", "focus"])


class GoHomeTests(unittest.TestCase):
    """The start page, Series › Calendar (at start and back from the tray), on a stand-in."""

    def window(self, mode: Mode, tab: str) -> Any:
        calls: list[Any] = []
        tabs = SimpleNamespace(current=tab)
        tabs.get = lambda: tabs.current

        def set_tab(name: str) -> None:
            calls.append(("tab", name))
            tabs.current = name

        tabs.set = set_tab
        window: Any = SimpleNamespace(
            calls=calls,
            mode=mode,
            _series_tabs=tabs,
            _select_mode=lambda m: calls.append(("mode", m)),
            _on_tab_changed=lambda: calls.append("refresh"),
        )
        return window

    def test_from_another_page_and_tab(self) -> None:
        window = self.window(Mode.MOVIES, t("Tracked"))
        MainWindow._go_home(window)
        self.assertEqual(
            window.calls, [("mode", Mode.SERIES), ("tab", t("Calendar")), "refresh"]
        )

    def test_already_on_the_calendar(self) -> None:
        window = self.window(Mode.DOWNLOADS, t("Calendar"))
        MainWindow._go_home(window)
        self.assertEqual(window.calls, [("mode", Mode.SERIES)])


class _Settings:
    def __init__(self, settings: Settings) -> None:
        self.current = settings
        self.updates: list[dict[str, Any]] = []

    async def update(self, **changes: Any) -> Settings:
        self.updates.append(changes)
        self.current = self.current.model_copy(update=changes)
        return self.current


class GetStartedAfterWelcomeTests(unittest.IsolatedAsyncioTestCase):
    """"Let's get started" opens by itself once, right after the welcome, only when the
    selected person follows no shows; never for a library from before (on a stand-in)."""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0  # making a Tk root

    def window(self, *, follows: dict[int, int], **settings: Any) -> Any:
        opened: list[int] = []
        asked: list[int] = []

        async def stats(*, profile_id: int) -> LibraryStats:
            asked.append(profile_id)
            return LibraryStats(tracked_shows=follows.get(profile_id, 0), episodes=0)

        services = SimpleNamespace(
            settings=_Settings(Settings(**settings)),
            library=SimpleNamespace(stats=stats),
            profiles=SimpleNamespace(current_id=2),
        )
        window: Any = SimpleNamespace(
            _services=services,
            opened=opened,
            asked=asked,
            winfo_exists=lambda: True,
            open_get_started=lambda: opened.append(services.profiles.current_id),
        )
        return window

    async def test_opens_once_when_no_show_is_followed(self) -> None:
        window = self.window(follows={1: 4, 2: 0}, setup_done=True)
        await MainWindow._get_started_after_welcome(window)
        self.assertEqual(window.opened, [2])  # for the selected person
        self.assertEqual(window.asked, [2])
        self.assertTrue(window._services.settings.current.get_started_shown)
        await MainWindow._get_started_after_welcome(window)  # never again
        self.assertEqual(window.opened, [2])

    async def test_not_when_the_import_brought_shows(self) -> None:
        window = self.window(follows={2: 5}, setup_done=True)
        await MainWindow._get_started_after_welcome(window)
        self.assertEqual(window.opened, [])
        self.assertFalse(window._services.settings.current.get_started_shown)

    async def test_never_automatically_for_an_existing_library(self) -> None:
        # Set up before: the welcome doesn't show, so neither does the window.
        window = self.window(follows={2: 0}, setup_done=True)
        await MainWindow._first_run(window)
        self.assertEqual(window.opened, [])
        self.assertEqual(window._services.settings.updates, [])
        # A library from before the welcome existed (shows, setup not done): only marked done.
        window = self.window(follows={2: 3}, setup_done=False)
        await MainWindow._first_run(window)
        self.assertEqual(window._services.settings.updates, [{"setup_done": True}])
        self.assertEqual(window.opened, [])

    async def test_the_welcome_closing_starts_the_check(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        checks: list[str] = []

        async def check() -> None:
            checks.append("check")

        window: Any = SimpleNamespace(
            restart_requested=False,
            winfo_exists=lambda: True,
            _get_started_after_welcome=check,
        )
        welcome = tk.Toplevel(root)
        tk.Frame(welcome).pack()  # a child going doesn't count
        MainWindow._watch_welcome(window, welcome)
        welcome.destroy()
        await asyncio.sleep(0)
        self.assertEqual(checks, ["check"])
        # Closed by a restart: no check.
        window.restart_requested = True
        welcome = tk.Toplevel(root)
        MainWindow._watch_welcome(window, welcome)
        welcome.destroy()
        await asyncio.sleep(0)
        self.assertEqual(checks, ["check"])

    def test_an_open_window_comes_forward(self) -> None:
        calls: list[str] = []
        dialog = SimpleNamespace(
            winfo_exists=lambda: True,
            lift=lambda: calls.append("lift"),
            focus_force=lambda: calls.append("focus"),
        )
        window: Any = SimpleNamespace(_get_started=dialog)
        MainWindow.open_get_started(window)
        self.assertEqual(calls, ["lift", "focus"])


if __name__ == "__main__":
    unittest.main()
