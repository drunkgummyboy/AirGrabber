"""The torrent-program status line with qBittorrent: found and set up ("installed and ready"),
found but still with its pop-up (the "Set up qBittorrent for AirGrabber" button), setting up,
waiting for qBittorrent to close, failing; installing it from here sets it up too; and the
"AirGrabber look in qBittorrent" switch (shown once set up, not over a theme of the user's own;
switching waits for qBittorrent to close). A fake TorrentAppService; nothing is installed or
changed on this PC."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from collections.abc import Callable
from unittest import mock

import customtkinter as ctk

from app.models import QBittorrentSetupResult, QBittorrentState, TorrentApp
from app.ui import theme
from app.ui import torrent_app_status as tas
from app.ui.torrent_app_status import (
    LOOK_SWITCH_TEXT,
    LOOK_WAITING_TEXT,
    SETUP_BUTTON_TEXT,
    TorrentAppStatus,
    TorrentStatus,
    found_status,
    look_line,
    setup_status,
    torrent_look,
)

QBIT = TorrentApp(name="qBittorrent")
QUIET = QBittorrentState(exists=True, quiet=True, save_path=r"C:\Users\Alex\Downloads")
NOISY = QBittorrentState(exists=True, quiet=False, save_path=r"C:\Users\Alex\Downloads")


class LookTests(unittest.TestCase):
    def test_what_a_look_finds(self) -> None:
        self.assertIs(found_status(None, None), TorrentStatus.MISSING)
        self.assertIs(found_status(TorrentApp(name="Deluge"), None), TorrentStatus.FOUND)
        self.assertIs(found_status(QBIT, QUIET), TorrentStatus.READY)
        self.assertIs(found_status(QBIT, NOISY), TorrentStatus.SETUP_NEEDED)
        self.assertIs(found_status(QBIT, None), TorrentStatus.SETUP_NEEDED)  # unreadable: offer it

    def test_after_a_setup_try(self) -> None:
        self.assertIs(setup_status(QBittorrentSetupResult.DONE), TorrentStatus.READY)
        self.assertIs(setup_status(QBittorrentSetupResult.ALREADY), TorrentStatus.READY)
        self.assertIs(setup_status(QBittorrentSetupResult.RUNNING), TorrentStatus.SETUP_WAITING)
        self.assertIs(setup_status(QBittorrentSetupResult.FAILED), TorrentStatus.SETUP_FAILED)

    def test_the_lines(self) -> None:
        ready = torrent_look(TorrentStatus.READY)
        self.assertEqual((ready.text, ready.color), ("✓ qBittorrent is installed and ready", theme.SUCCESS))
        self.assertFalse(ready.setup_button or ready.install_button)

        needed = torrent_look(TorrentStatus.SETUP_NEEDED)
        self.assertEqual((needed.text, needed.color), ("✓ qBittorrent found", theme.SUCCESS))
        self.assertTrue(needed.setup_button)

        waiting = torrent_look(TorrentStatus.SETUP_WAITING)
        self.assertEqual(
            (waiting.text, waiting.color),
            ("Close qBittorrent to finish: AirGrabber sets it up as soon as it's closed.", theme.WARNING),
        )
        self.assertFalse(waiting.setup_button)

        failed = torrent_look(TorrentStatus.SETUP_FAILED)
        self.assertEqual((failed.text, failed.color), ("Couldn't change qBittorrent's settings.", theme.DANGER))
        self.assertTrue(failed.setup_button)  # try again

        self.assertEqual(torrent_look(TorrentStatus.SETTING_UP).text, "Setting up qBittorrent…")
        self.assertFalse(torrent_look(TorrentStatus.FOUND, "Deluge").setup_button)
        self.assertEqual(SETUP_BUTTON_TEXT, "Set up qBittorrent for AirGrabber (no pop-up for each download)")


class FakeTorrentApp:
    """TorrentAppService: `app` found, qBittorrent's `state`, setup answers in turn; AirGrabber's
    `look` and the answers to switching it (a DONE / ALREADY one changes `look`)."""

    def __init__(
        self,
        app: TorrentApp | None,
        state: QBittorrentState | None = None,
        answers: list[QBittorrentSetupResult] | None = None,
        installed: bool = True,
        look: bool | None = None,
        look_answers: list[QBittorrentSetupResult] | None = None,
    ) -> None:
        self.app = app
        self.state = state
        self.answers = list(answers or [QBittorrentSetupResult.DONE])
        self.installed = installed
        self.setups = 0
        self.look = look
        self.look_answers = list(look_answers or [QBittorrentSetupResult.DONE])
        self.look_calls: list[bool] = []

    async def qbittorrent_look(self) -> bool | None:
        return self.look

    async def set_qbittorrent_look(self, on: bool) -> QBittorrentSetupResult:
        self.look_calls.append(on)
        answer = self.look_answers.pop(0) if len(self.look_answers) > 1 else self.look_answers[0]
        if answer in (QBittorrentSetupResult.DONE, QBittorrentSetupResult.ALREADY):
            self.look = on
        return answer

    async def find(self) -> TorrentApp | None:
        return self.app

    async def qbittorrent_state(self) -> QBittorrentState | None:
        return self.state

    async def install_qbittorrent(self) -> bool:
        if self.installed:
            self.app = QBIT
        return self.installed

    async def set_up_qbittorrent(self) -> QBittorrentSetupResult:
        self.setups += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class WidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def status(self, service: FakeTorrentApp) -> TorrentAppStatus:
        widget = TorrentAppStatus(self.root, torrent_app=service, wraplength=400)  # type: ignore[arg-type]
        widget.pack()
        return widget

    async def until(self, check: Callable[[], bool]) -> None:
        for _ in range(200):
            if check():
                return
            await asyncio.sleep(0.01)
        self.fail("never happened")

    def button_shows(self, widget: TorrentAppStatus) -> bool:
        return bool(widget._setup_button.winfo_manager())

    async def test_a_set_up_qbittorrent_is_ready(self) -> None:
        widget = self.status(FakeTorrentApp(QBIT, QUIET))
        widget.refresh()
        await self.until(lambda: widget.status is TorrentStatus.READY)
        self.assertEqual(widget._label.cget("text"), "✓ qBittorrent is installed and ready")
        self.assertFalse(self.button_shows(widget))

    async def test_the_button_sets_it_up(self) -> None:
        service = FakeTorrentApp(QBIT, NOISY)
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: widget.status is TorrentStatus.SETUP_NEEDED)
        self.assertTrue(self.button_shows(widget))
        widget._setup_button.invoke()
        await self.until(lambda: widget.status is TorrentStatus.READY)
        self.assertEqual(service.setups, 1)
        self.assertFalse(self.button_shows(widget))

    async def test_waits_for_qbittorrent_to_close(self) -> None:
        service = FakeTorrentApp(
            QBIT,
            NOISY,
            [QBittorrentSetupResult.RUNNING, QBittorrentSetupResult.RUNNING, QBittorrentSetupResult.DONE],
        )
        widget = self.status(service)
        with mock.patch.object(tas, "RETRY_SECONDS", 0.01):
            widget._set_up()
            await self.until(lambda: widget.status is TorrentStatus.SETUP_WAITING)
            widget.refresh()  # no look while waiting
            await self.until(lambda: widget.status is TorrentStatus.READY)
        self.assertEqual(service.setups, 3)

    async def test_stops_waiting_after_a_while(self) -> None:
        service = FakeTorrentApp(QBIT, NOISY, [QBittorrentSetupResult.RUNNING])
        widget = self.status(service)
        with mock.patch.object(tas, "RETRY_SECONDS", 0.01), mock.patch.object(tas, "RETRY_FOR_SECONDS", 0.05):
            widget._set_up()
            await self.until(lambda: widget.status is TorrentStatus.SETUP_NEEDED)
        self.assertTrue(self.button_shows(widget))  # for next time

    async def test_installing_sets_it_up_too(self) -> None:
        service = FakeTorrentApp(None)
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: widget.status is TorrentStatus.MISSING)
        widget._install_button.invoke()
        await self.until(lambda: widget.status is TorrentStatus.READY)
        self.assertEqual(service.setups, 1)

    async def test_another_program_has_no_button(self) -> None:
        widget = self.status(FakeTorrentApp(TorrentApp(name="Deluge")))
        widget.refresh()
        await self.until(lambda: widget.status is TorrentStatus.FOUND)
        self.assertEqual(widget._label.cget("text"), "✓ Deluge found")
        self.assertFalse(self.button_shows(widget))
        self.assertFalse(self.switch_shows(widget))

    # -- AirGrabber's look ---------------------------------------------------------------------

    def switch_shows(self, widget: TorrentAppStatus) -> bool:
        return bool(widget._look_switch.winfo_manager())

    async def test_ready_shows_the_look_switch(self) -> None:
        widget = self.status(FakeTorrentApp(QBIT, QUIET, look=True))
        widget.refresh()
        await self.until(lambda: widget.status is TorrentStatus.READY)
        self.assertTrue(self.switch_shows(widget))
        self.assertTrue(widget._look_var.get())
        self.assertEqual(widget._look_switch.cget("text"), LOOK_SWITCH_TEXT)

    async def test_no_switch_over_the_users_own_theme_or_before_the_setup(self) -> None:
        own = self.status(FakeTorrentApp(QBIT, QUIET, look=None))
        own.refresh()
        await self.until(lambda: own.status is TorrentStatus.READY)
        self.assertFalse(self.switch_shows(own))
        needed = self.status(FakeTorrentApp(QBIT, NOISY, look=False))
        needed.refresh()
        await self.until(lambda: needed.status is TorrentStatus.SETUP_NEEDED)
        self.assertFalse(self.switch_shows(needed))  # the setup button only

    async def test_the_setup_shows_the_look_it_gave(self) -> None:
        service = FakeTorrentApp(QBIT, NOISY, look=False)
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: widget.status is TorrentStatus.SETUP_NEEDED)
        service.look = True  # what the setup does to a qBittorrent without a theme
        widget._setup_button.invoke()
        await self.until(lambda: widget.status is TorrentStatus.READY)
        self.assertTrue(self.switch_shows(widget))
        self.assertTrue(widget._look_var.get())

    async def test_switching_it_off(self) -> None:
        service = FakeTorrentApp(QBIT, QUIET, look=True)
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: self.switch_shows(widget))
        widget._look_switch.toggle()
        await self.until(lambda: service.look is False and not widget._busy())
        self.assertEqual(service.look_calls, [False])
        self.assertFalse(widget._look_var.get())
        self.assertEqual(widget._label.cget("text"), "✓ qBittorrent is installed and ready")

    async def test_switching_waits_for_qbittorrent_to_close(self) -> None:
        service = FakeTorrentApp(
            QBIT, QUIET, look=True,
            look_answers=[QBittorrentSetupResult.RUNNING, QBittorrentSetupResult.RUNNING,
                          QBittorrentSetupResult.DONE],
        )  # fmt: skip
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: self.switch_shows(widget))
        with mock.patch.object(tas, "RETRY_SECONDS", 0.05):
            widget._look_switch.toggle()
            await self.until(lambda: widget._label.cget("text") == LOOK_WAITING_TEXT)  # English
            self.assertFalse(widget._look_var.get())  # the wish stays while it waits
            widget.refresh()  # no look for the program meanwhile
            await self.until(lambda: not widget._busy())
        self.assertEqual(service.look_calls, [False, False, False])
        self.assertIs(service.look, False)
        self.assertEqual(widget._label.cget("text"), "✓ qBittorrent is installed and ready")

    async def test_switched_back_while_waiting(self) -> None:
        service = FakeTorrentApp(
            QBIT, QUIET, look=True,
            look_answers=[QBittorrentSetupResult.RUNNING, QBittorrentSetupResult.ALREADY],
        )  # fmt: skip
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: self.switch_shows(widget))
        with mock.patch.object(tas, "RETRY_SECONDS", 0.05):
            widget._look_switch.toggle()  # off: qBittorrent runs
            await self.until(lambda: service.look_calls == [False])
            widget._look_switch.toggle()  # on again
            await self.until(lambda: not widget._busy())
        self.assertEqual(service.look_calls, [False, True])
        self.assertTrue(widget._look_var.get())

    async def test_a_failed_switch_shows_what_qbittorrent_has(self) -> None:
        service = FakeTorrentApp(QBIT, QUIET, look=True, look_answers=[QBittorrentSetupResult.FAILED])
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: self.switch_shows(widget))
        widget._look_switch.toggle()
        await self.until(lambda: not widget._busy())
        self.assertTrue(widget._look_var.get())  # still on in qBittorrent's settings
        self.assertEqual(widget._label.cget("text"), "Couldn't change qBittorrent's settings.")

    async def test_stops_waiting_for_the_switch_after_a_while(self) -> None:
        service = FakeTorrentApp(QBIT, QUIET, look=True, look_answers=[QBittorrentSetupResult.RUNNING])
        widget = self.status(service)
        widget.refresh()
        await self.until(lambda: self.switch_shows(widget))
        with mock.patch.object(tas, "RETRY_SECONDS", 0.01), mock.patch.object(tas, "RETRY_FOR_SECONDS", 0.05):
            widget._look_switch.toggle()
            await self.until(lambda: not widget._busy())
        self.assertTrue(widget._look_var.get())  # back to what qBittorrent has
        self.assertEqual(widget._label.cget("text"), "✓ qBittorrent is installed and ready")


class LookLineTests(unittest.TestCase):
    def test_the_lines_after_switching(self) -> None:
        waiting = look_line(QBittorrentSetupResult.RUNNING)
        assert waiting is not None
        self.assertEqual(
            (waiting.text, waiting.color),
            ("Close qBittorrent to finish: AirGrabber changes its look as soon as it's closed.", theme.WARNING),
        )
        failed = look_line(QBittorrentSetupResult.FAILED)
        assert failed is not None
        self.assertEqual((failed.text, failed.color), ("Couldn't change qBittorrent's settings.", theme.DANGER))
        self.assertIsNone(look_line(QBittorrentSetupResult.DONE))
        self.assertIsNone(look_line(QBittorrentSetupResult.ALREADY))


if __name__ == "__main__":
    unittest.main()
