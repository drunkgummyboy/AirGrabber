"""Settings → Automatic, "In the background": the start-with-Windows box shows the real
shortcut, and Save adds or removes it only when the box changed (a fake shortcut throughout;
the real Startup folder is never touched)."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import ProwlarrState, ProwlarrStatus, Settings
from app.services import SettingsService
from app.services.startup import SHORTCUT_NAME, StartupError
from app.ui.settings_dialog import (
    SettingsDialog,
    StartupResult,
    WindowsStartup,
    apply_start_with_windows,
)


class FakeShortcut:
    def __init__(self, enabled: bool, *, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.calls: list[bool] = []

    def is_enabled(self) -> bool:
        return self.enabled

    async def set_enabled(self, on: bool) -> None:
        self.calls.append(on)
        if self.fail:
            raise StartupError("AirGrabber could not be added to Windows startup.")
        self.enabled = on


class FakeProwlarr:
    """ProwlarrSetupService for the dialog's Prowlarr panel: never installed, never installs."""

    async def status(self) -> ProwlarrStatus:
        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)

    async def install(self, progress: object = None) -> bool:
        raise AssertionError("never installs")


class FakeSettingsRepository:
    def __init__(self) -> None:
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saved.append(settings)


class ApplyStartWithWindowsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_box_leaves_the_shortcut_alone(self) -> None:
        for state in (True, False):
            with self.subTest(state=state):
                shortcut = FakeShortcut(state)
                result = await apply_start_with_windows(state, state, shortcut)
                self.assertEqual(shortcut.calls, [])
                self.assertEqual(result, StartupResult(enabled=state, error=None))

    async def test_changed_box_adds_or_removes_it(self) -> None:
        for wanted in (True, False):
            with self.subTest(wanted=wanted):
                shortcut = FakeShortcut(not wanted)
                result = await apply_start_with_windows(wanted, not wanted, shortcut)
                self.assertEqual(shortcut.calls, [wanted])
                self.assertEqual(result, StartupResult(enabled=wanted, error=None))

    async def test_a_failure_reports_the_real_state(self) -> None:
        shortcut = FakeShortcut(False, fail=True)
        result = await apply_start_with_windows(True, False, shortcut)
        self.assertEqual(shortcut.calls, [True])
        self.assertFalse(result.enabled)
        self.assertEqual(result.error, "AirGrabber could not be added to Windows startup.")


class WindowsStartupTests(unittest.TestCase):
    def test_reads_the_injected_startup_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            shortcut = WindowsStartup(Path(folder))
            self.assertFalse(shortcut.is_enabled())
            (Path(folder) / SHORTCUT_NAME).write_bytes(b"")
            self.assertTrue(shortcut.is_enabled())
            asyncio.run(shortcut.set_enabled(False))
            self.assertFalse(shortcut.is_enabled())


class SettingsDialogStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        # Collect the dialog's Tk variables here, on the main thread: collected later on another
        # test's worker thread, their __del__ would call into Tcl from that thread.
        gc.collect()

    def dialog(self, shortcut: FakeShortcut, *, stored: bool) -> tuple[SettingsDialog, Any]:
        repository = FakeSettingsRepository()
        settings = SettingsService(
            repository,  # type: ignore[arg-type]
            Settings(download_dir=self.folder, start_with_windows=stored),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=shortcut,
        )
        self.addCleanup(self._close, dialog)
        return dialog, settings

    @staticmethod
    def _close(dialog: SettingsDialog) -> None:
        if dialog.winfo_exists():
            dialog.destroy()

    async def test_the_box_shows_the_real_shortcut_not_the_setting(self) -> None:
        for real, stored in ((True, False), (False, True)):
            with self.subTest(real=real, stored=stored):
                dialog, _settings = self.dialog(FakeShortcut(real), stored=stored)
                self.assertEqual(dialog._start_with_windows.get(), real)
                dialog.destroy()

    async def test_save_without_a_change_leaves_the_shortcut_and_stores_its_state(self) -> None:
        shortcut = FakeShortcut(True)
        dialog, settings = self.dialog(shortcut, stored=False)
        await dialog._save()
        self.assertEqual(shortcut.calls, [])
        self.assertTrue(settings.current.start_with_windows)
        self.assertFalse(dialog.winfo_exists())

    async def test_save_after_a_change_switches_the_shortcut(self) -> None:
        shortcut = FakeShortcut(False)
        dialog, settings = self.dialog(shortcut, stored=False)
        dialog._start_with_windows.set(True)
        dialog._keep_in_tray.set(False)
        dialog._auto_new_shows.set(True)
        await dialog._save()
        self.assertEqual(shortcut.calls, [True])
        self.assertTrue(settings.current.start_with_windows)
        self.assertFalse(settings.current.keep_in_tray)
        self.assertTrue(settings.current.auto_download_new_shows)

    async def test_a_failed_change_shows_in_the_error_line(self) -> None:
        shortcut = FakeShortcut(False, fail=True)
        dialog, settings = self.dialog(shortcut, stored=True)
        dialog._start_with_windows.set(True)
        await dialog._save()
        self.assertEqual(shortcut.calls, [True])
        self.assertTrue(dialog.winfo_exists())  # stays open
        self.assertEqual(
            dialog._error.cget("text"), "AirGrabber could not be added to Windows startup."
        )
        self.assertFalse(dialog._start_with_windows.get())
        self.assertFalse(settings.current.start_with_windows)  # the real state


if __name__ == "__main__":
    unittest.main()
