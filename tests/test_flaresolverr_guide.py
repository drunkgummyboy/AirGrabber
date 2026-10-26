"""The "Add FlareSolverr" guide: its texts, the Check result line, and the dialog with a fake
check (no network; nothing is downloaded or started)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from unittest import mock

import customtkinter as ctk

from app.i18n_nl import NL
from app.ui import theme
from app.ui.flaresolverr_guide import (
    DOWNLOAD_URL,
    STEP_DOWNLOAD,
    STEP_PROXY,
    STEP_TAG,
    FlareSolverrDialog,
    RunCheck,
    run_check_message,
)


class FakeFlareSolverr:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.checks = 0

    async def is_running(self) -> bool:
        self.checks += 1
        return self.running


class TextTests(unittest.TestCase):
    def test_result_lines(self) -> None:
        self.assertEqual(
            run_check_message(RunCheck.RUNNING), ("✓ FlareSolverr is running", theme.SUCCESS)
        )
        self.assertEqual(
            run_check_message(RunCheck.NOT_RUNNING),
            ("FlareSolverr isn't running on this PC yet.", theme.WARNING),
        )
        self.assertEqual(run_check_message(RunCheck.CHECKING), ("Checking…", theme.TEXT_MUTED))

    def test_official_download_page(self) -> None:
        self.assertEqual(
            DOWNLOAD_URL, "https://github.com/FlareSolverr/FlareSolverr/releases/latest"
        )
        self.assertIn("flaresolverr_windows_x64.zip", STEP_DOWNLOAD)

    def test_dutch_keeps_prowlarrs_labels_and_values(self) -> None:
        for label in ("Settings", "Indexers", "Indexer Proxies", "Tags", "Host", "Test", "Save"):
            with self.subTest(label=label):
                self.assertIn(f'"{label}"', NL[STEP_PROXY])
        for value in ("flaresolverr", "http://localhost:8191/"):
            self.assertIn(value, NL[STEP_PROXY])
        self.assertIn("flaresolverr", NL[STEP_TAG])
        self.assertIn("flaresolverr_windows_x64.zip", NL[STEP_DOWNLOAD])


class DialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def dialog(
        self, fake: FakeFlareSolverr, open_prowlarr: mock.Mock | None = None
    ) -> FlareSolverrDialog:
        dialog = FlareSolverrDialog(
            self.root,
            flaresolverr=fake,  # type: ignore[arg-type]
            open_prowlarr=open_prowlarr,
        )
        self.addCleanup(_close, dialog)
        return dialog

    async def check(self, dialog: FlareSolverrDialog) -> None:
        assert dialog._check_button is not None
        dialog._check_button.invoke()
        task = dialog._check_task
        assert task is not None
        await task

    async def test_running(self) -> None:
        fake = FakeFlareSolverr(running=True)
        dialog = self.dialog(fake)
        self.assertEqual(dialog._result.cget("text"), "")
        await self.check(dialog)
        self.assertEqual(fake.checks, 1)
        self.assertEqual(dialog._result.cget("text"), "✓ FlareSolverr is running")
        assert dialog._check_button is not None
        self.assertEqual(dialog._check_button.cget("state"), "normal")

    async def test_not_running(self) -> None:
        dialog = self.dialog(FakeFlareSolverr(running=False))
        await self.check(dialog)
        self.assertEqual(
            dialog._result.cget("text"), "FlareSolverr isn't running on this PC yet."
        )

    def test_open_prowlarr_button_only_with_an_address(self) -> None:
        def buttons(dialog: FlareSolverrDialog) -> list[str]:
            return [
                str(child.cget("text"))
                for child in dialog._steps.winfo_children()
                if isinstance(child, ctk.CTkButton)
            ]

        opener = mock.Mock()
        with_prowlarr = self.dialog(FakeFlareSolverr(True), opener)
        self.assertEqual(
            buttons(with_prowlarr), ["Open the download page", "Check", "Open Prowlarr"]
        )
        self.assertEqual(buttons(self.dialog(FakeFlareSolverr(True))), [
            "Open the download page", "Check"
        ])


def _close(dialog: FlareSolverrDialog) -> None:
    if dialog.winfo_exists():
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
