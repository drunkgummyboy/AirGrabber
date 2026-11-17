"""Settings → Updates' "Report a problem": the logs in one zip on the desktop."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.models import Settings
from app.services.problem_report import (
    ABOUT_FILE,
    ProblemReports,
    make_report,
    report_name,
)
from app.services.settings import SettingsService
from app.ui.settings_dialog import (
    REPORT_FAILED,
    REPORT_IN_FOLDER,
    REPORT_ON_DESKTOP,
    SECTION_UPDATES,
    SettingsDialog,
)
from tests.test_settings_dialog import DialogCase, FakeProwlarr, FakeRepository, FakeShortcut

NOW = datetime(2026, 11, 16, 19, 30, 5)


class Folders(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()


class MakeReportTests(Folders):
    def test_the_logs_and_the_about_file_go_in(self) -> None:
        (self.logs / "airgrabber.log").write_bytes(b"today\n")
        (self.logs / "airgrabber.log.1").write_text("yesterday\n", encoding="utf-8")
        (self.logs / "install.log").write_text("pip\n", encoding="utf-8")
        (self.logs / "other.txt").write_text("not this\n", encoding="utf-8")

        path = make_report(self.logs, self.desktop, version="3.17.1", now=NOW)

        self.assertEqual(path, self.desktop / "AirGrabber problem report 2026-11-16 19-30-05.zip")
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                [ABOUT_FILE, "airgrabber.log", "airgrabber.log.1", "install.log"],
            )
            self.assertEqual(archive.read("airgrabber.log"), b"today\n")
            about = archive.read(ABOUT_FILE).decode()
        self.assertIn("AirGrabber 3.17.1", about)
        self.assertIn("2026-11-16T19:30:05", about)

    def test_without_logs_only_the_about_file(self) -> None:
        path = make_report(self.logs, self.desktop, version="3.17.1", now=NOW)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), [ABOUT_FILE])

    def test_a_folder_that_cant_be_written_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            make_report(self.logs, self.root / "missing", version="3.17.1", now=NOW)

    def test_names_differ_by_the_second(self) -> None:
        self.assertNotEqual(report_name(NOW), report_name(NOW.replace(second=6)))


class ServiceTests(Folders, unittest.IsolatedAsyncioTestCase):
    async def test_on_the_desktop(self) -> None:
        reports = ProblemReports(
            self.logs, self.root, "3.17.1", desktop=lambda: self.desktop, now=lambda: NOW
        )
        path, on_desktop = await reports.save()
        self.assertTrue(on_desktop)
        self.assertEqual(path.parent, self.desktop)
        self.assertTrue(path.is_file())

    async def test_without_a_desktop_in_the_data_folder(self) -> None:
        reports = ProblemReports(
            self.logs, self.root, "3.17.1", desktop=lambda: None, now=lambda: NOW
        )
        path, on_desktop = await reports.save()
        self.assertFalse(on_desktop)
        self.assertEqual(path.parent, self.root)


class ReportBoxTests(DialogCase):
    def report_dialog(self, reports: Any) -> SettingsDialog:
        settings = SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.4", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=self.discovery,
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            problem_reports=reports,
            section=SECTION_UPDATES,
        )
        self.addCleanup(self._close, dialog)
        dialog.page(SECTION_UPDATES)
        return dialog

    async def settle(self, dialog: SettingsDialog) -> None:
        for _ in range(100):
            await asyncio.sleep(0.01)
            assert dialog.report_result is not None
            if dialog.report_result.cget("text"):
                return
        self.fail("no result")

    async def test_no_box_without_the_service(self) -> None:
        dialog = self.report_dialog(None)
        self.assertIsNone(dialog.report_button)

    async def test_made_on_the_desktop_and_shown_in_explorer(self) -> None:
        path = self.folder / "report.zip"
        reports = mock.MagicMock(save=mock.AsyncMock(return_value=(path, True)))
        dialog = self.report_dialog(reports)
        with mock.patch("app.forge.ui.file_browser._start") as start:
            assert dialog.report_button is not None
            dialog.report_button.invoke()
            await self.settle(dialog)
        start.assert_called_once_with(f'explorer /select,"{path}"')
        assert dialog.report_result is not None
        self.assertEqual(dialog.report_result.cget("text"), REPORT_ON_DESKTOP)
        self.assertEqual(dialog.report_button.cget("state"), "normal")

    async def test_in_the_data_folder_says_so(self) -> None:
        reports = mock.MagicMock(save=mock.AsyncMock(return_value=(self.folder / "r.zip", False)))
        dialog = self.report_dialog(reports)
        with mock.patch("app.forge.ui.file_browser._start"):
            assert dialog.report_button is not None
            dialog.report_button.invoke()
            await self.settle(dialog)
        assert dialog.report_result is not None
        self.assertEqual(dialog.report_result.cget("text"), REPORT_IN_FOLDER)

    async def test_a_failure_says_so_and_opens_nothing(self) -> None:
        reports = mock.MagicMock(save=mock.AsyncMock(side_effect=PermissionError()))
        dialog = self.report_dialog(reports)
        with mock.patch("app.forge.ui.file_browser._start") as start:
            assert dialog.report_button is not None
            dialog.report_button.invoke()
            await self.settle(dialog)
        start.assert_not_called()
        assert dialog.report_result is not None
        self.assertEqual(dialog.report_result.cget("text"), REPORT_FAILED)


if __name__ == "__main__":
    unittest.main()
