"""The tray: what closing the window does, and TrayIcon with a fake pystray (no real tray icon):
menu callbacks reach the asyncio loop's thread only through call_soon_threadsafe."""

from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import Callable
from typing import Any

from PIL import Image

from app.models import Settings
from app.ui.tray import (
    CHECK_NOW_LABEL,
    OPEN_LABEL,
    QUIT_LABEL,
    CloseAction,
    TrayIcon,
    close_action,
)

WAIT_S = 5.0


class CloseActionTests(unittest.TestCase):
    def test_keep_in_tray_by_notice_shown(self) -> None:
        cases = {
            (True, False): CloseAction.NOTICE_THEN_HIDE,  # the first close to the tray
            (True, True): CloseAction.HIDE,
            (False, False): CloseAction.QUIT,  # today's behaviour
            (False, True): CloseAction.QUIT,
        }
        for (keep, shown), expected in cases.items():
            with self.subTest(keep_in_tray=keep, notice_shown=shown):
                self.assertIs(
                    close_action(keep_in_tray=keep, tray_running=True, notice_shown=shown),
                    expected,
                )

    def test_without_a_tray_icon_close_quits(self) -> None:
        for keep in (True, False):
            for shown in (True, False):
                with self.subTest(keep_in_tray=keep, notice_shown=shown):
                    self.assertIs(
                        close_action(keep_in_tray=keep, tray_running=False, notice_shown=shown),
                        CloseAction.QUIT,
                    )

    def test_settings_defaults(self) -> None:
        settings = Settings.model_validate({"quality": "1080p"})  # stored before these existed
        self.assertTrue(settings.keep_in_tray)
        self.assertFalse(settings.tray_notice_shown)
        self.assertFalse(settings.start_with_windows)


# -- a fake pystray ------------------------------------------------------------------------------


class FakeMenuItem:
    def __init__(self, text: str, action: Callable[..., None], default: bool = False) -> None:
        self.text = text
        self.action = action
        self.default = default


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items: Any) -> None:
        self.items = items


class FakeIcon:
    """Runs like pystray's: run() blocks on the calling thread until stop(); setup() is called
    on another thread once it runs."""

    def __init__(
        self,
        name: str,
        icon: Image.Image,
        title: str,
        menu: FakeMenu,
        hold_setup: threading.Event | None = None,
    ) -> None:
        self.name, self.image, self.title, self.menu = name, icon, title, menu
        self.hold_setup = hold_setup  # setup waits for it: pystray's loop is still starting
        self.visible = False
        self.ever_visible = False
        self.notifications: list[tuple[str, str | None]] = []
        self.run_thread: threading.Thread | None = None
        self.setup_done = threading.Event()
        self.stopped = threading.Event()

    def run(self, setup: Callable[[Any], None] | None = None) -> None:
        self.run_thread = threading.current_thread()

        def call_setup() -> None:
            if self.hold_setup is not None:
                self.hold_setup.wait()
            if setup is not None:
                setup(self)
            self.ever_visible = self.visible
            self.setup_done.set()

        threading.Thread(target=call_setup).start()
        self.stopped.wait()
        self.visible = False

    def stop(self) -> None:
        self.stopped.set()

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((message, title))


class FakePystray:
    Menu = FakeMenu
    MenuItem = FakeMenuItem

    def __init__(self, *, fail: bool = False, hold_setup: threading.Event | None = None) -> None:
        self.icons: list[FakeIcon] = []
        self.fail = fail
        self.hold_setup = hold_setup

    def Icon(self, name: str, icon: Image.Image, title: str, menu: FakeMenu) -> FakeIcon:
        created = FakeIcon(name, icon, title, menu, self.hold_setup)
        if self.fail:
            created.run = _broken_run  # type: ignore[method-assign]
        self.icons.append(created)
        return created


def _broken_run(setup: Callable[[Any], None] | None = None) -> None:
    raise OSError("no notification area")


# -- TrayIcon ------------------------------------------------------------------------------------


class TrayIconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.calls: list[tuple[str, int]] = []  # (callback, thread id it ran on)
        self.backend = FakePystray()
        self.tray = self._tray(self.backend)
        self.addCleanup(self.tray.stop)

    def _tray(self, backend: Any, *, on_open: Callable[[], None] | None = None, **extra: Any) -> TrayIcon:
        return TrayIcon(
            self.loop,
            Image.new("RGBA", (16, 16)),
            on_open=on_open or self._record("open"),
            on_check_now=self._record("check"),
            on_quit=self._record("quit"),
            backend=backend,
            **extra,
        )

    def _record(self, name: str) -> Callable[[], None]:
        return lambda: self.calls.append((name, threading.get_ident()))

    def _started_icon(self) -> FakeIcon:
        self.tray.start()
        icon = self.backend.icons[0]
        self.assertTrue(icon.setup_done.wait(WAIT_S))
        return icon

    def _drain_loop(self) -> None:
        self.loop.run_until_complete(asyncio.sleep(0))

    def test_menu(self) -> None:
        icon = self._started_icon()
        self.assertEqual((icon.name, icon.title), ("AirGrabber", "AirGrabber"))
        items = icon.menu.items
        self.assertEqual(
            [item if item is FakeMenu.SEPARATOR else item.text for item in items],
            [OPEN_LABEL, CHECK_NOW_LABEL, FakeMenu.SEPARATOR, QUIT_LABEL],
        )
        self.assertEqual([item.text for item in items if getattr(item, "default", False)], [OPEN_LABEL])
        self.assertTrue(icon.visible)
        self.assertTrue(icon.run_thread is not None and icon.run_thread.daemon)
        self.assertTrue(self.tray.running)

    def test_menu_callbacks_run_on_the_loop_thread_only(self) -> None:
        icon = self._started_icon()
        actions = {item.text: item.action for item in icon.menu.items if item is not FakeMenu.SEPARATOR}

        def click_all() -> None:  # as pystray's thread would
            for text in (OPEN_LABEL, CHECK_NOW_LABEL, QUIT_LABEL):
                actions[text]()

        clicker = threading.Thread(target=click_all)
        clicker.start()
        clicker.join(WAIT_S)
        self.assertEqual(self.calls, [])  # nothing ran on the tray's thread
        self._drain_loop()
        loop_thread = threading.get_ident()
        self.assertEqual(self.calls, [("open", loop_thread), ("check", loop_thread), ("quit", loop_thread)])

    def test_a_failing_callback_is_logged_not_raised(self) -> None:
        tray = self._tray(FakePystray(), on_open=_raise)
        self.addCleanup(tray.stop)
        tray.start()
        icon = tray._icon
        self.assertTrue(icon.setup_done.wait(WAIT_S))
        icon.menu.items[0].action()
        with self.assertLogs("app.ui.tray", "ERROR"):
            self._drain_loop()

    def test_callbacks_after_the_loop_closed_are_dropped(self) -> None:
        icon = self._started_icon()
        self.loop.close()
        icon.menu.items[-1].action()  # no RuntimeError on the tray's thread
        self.assertEqual(self.calls, [])

    def test_stop_removes_the_icon_and_ends_its_thread(self) -> None:
        icon = self._started_icon()
        self.tray.stop()
        self.assertTrue(icon.stopped.is_set())
        self.assertFalse(icon.visible)
        self.assertIsNotNone(icon.run_thread)
        assert icon.run_thread is not None
        self.assertFalse(icon.run_thread.is_alive())
        self.assertFalse(self.tray.running)
        self.tray.stop()  # twice is fine

    def test_stop_before_start(self) -> None:
        self.tray.stop()
        self.tray.start()  # a start after stop does nothing
        self.assertEqual(self.backend.icons, [])

    def test_stop_while_the_icon_is_starting(self) -> None:
        hold = threading.Event()
        backend = FakePystray(hold_setup=hold)
        tray = self._tray(backend)
        tray.start()
        stopper = threading.Thread(target=tray.stop)  # it waits for the tray's thread to end
        stopper.start()
        while not tray._stopping:
            pass
        hold.set()  # pystray's loop is ready now: setup must stop it instead of showing it
        stopper.join(WAIT_S)
        self.assertFalse(stopper.is_alive())
        icon = backend.icons[0]
        self.assertTrue(icon.setup_done.is_set())
        self.assertFalse(icon.ever_visible)
        self.assertTrue(icon.stopped.is_set())

    def test_notify(self) -> None:
        icon = self._started_icon()
        self.tray.notify("Downloaded", "Severance S02E03")
        self.assertEqual(icon.notifications, [("Severance S02E03", "Downloaded")])
        self.tray.stop()
        self.tray.notify("Downloaded", "Severance S02E04")  # no icon any more: dropped
        self.assertEqual(len(icon.notifications), 1)

    def test_missing_pystray_raises_import_error(self) -> None:
        class Missing:
            def __getattr__(self, name: str) -> Any:
                raise ImportError("No module named 'pystray'")

        with self.assertRaises(ImportError):
            self._tray(Missing()).start()

    def test_a_dying_message_loop_reports_on_the_loop(self) -> None:
        failed = threading.Event()
        tray = self._tray(FakePystray(fail=True), on_failed=failed.set)
        self.addCleanup(tray.stop)
        with self.assertLogs("app.ui.tray", "ERROR"):
            tray.start()
            assert tray._thread is not None
            tray._thread.join(WAIT_S)
        self.assertFalse(tray.running)
        self.assertFalse(failed.is_set())  # not on the tray's thread...
        self._drain_loop()
        self.assertTrue(failed.is_set())  # ...but on the loop


def _raise() -> None:
    raise ValueError("boom")


if __name__ == "__main__":
    unittest.main()
