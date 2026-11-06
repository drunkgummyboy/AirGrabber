"""Settings' way back after an update, and what the main window does with it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import UpdateInfo
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SECTION_UPDATES, SettingsDialog
from tests.test_backup_dialog import DialogTestCase, FakeProwlarr, FakeShortcut


class GoBackSettingsTests(DialogTestCase):
    def dialog(self, previous: str | None, **extra: Any) -> SettingsDialog:
        dialog = SettingsDialog(
            self.root,
            self.settings(),
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.1.0", previous_version=previous),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            **extra,
        )
        self.keep(dialog)
        return dialog

    async def test_no_button_without_an_older_version(self) -> None:
        dialog = self.dialog(None)
        dialog.page(SECTION_UPDATES)
        self.assertIsNone(dialog.go_back_button)

    async def test_the_button_asks_first_then_goes_back(self) -> None:
        go_back = mock.Mock()
        dialog = self.dialog("2.0.0", on_go_back=go_back)
        dialog.page(SECTION_UPDATES)
        button = dialog.go_back_button
        assert button is not None
        self.assertEqual(button.cget("text"), "Go back to version 2.0.0")
        button.invoke()
        go_back.assert_not_called()
        asked = [w for w in dialog.winfo_children() if isinstance(w, ConfirmDialog)]
        self.assertEqual(len(asked), 1)
        asked[0].confirm()
        go_back.assert_called_once_with()

    async def test_a_version_gone_back_from_is_explained(self) -> None:
        dialog = self.dialog(None)
        dialog.page(SECTION_UPDATES)
        info = UpdateInfo(
            current="2.0.0", latest="2.1.0", available=False, installable=True, skipped=True
        )
        dialog._show_update(info)
        self.assertIn("2.1.0 was undone", dialog._update_result.cget("text"))
        self.assertIsInstance(dialog._update_button, ctk.CTkButton)
        self.assertEqual(dialog._update_button.cget("text"), "Check for updates")


class GoBackWindowTests(DialogTestCase):
    async def run_going_back(self, *, noted: bool) -> mock.MagicMock:
        window = mock.MagicMock()
        window._services.updates.go_back = mock.AsyncMock(return_value=noted)
        await MainWindow._run_going_back(window)
        return window

    async def test_it_restarts_once_noted(self) -> None:
        window = await self.run_going_back(noted=True)
        window.after.assert_called_once_with(1200, window.restart)

    async def test_it_stays_and_says_so_when_it_can_not(self) -> None:
        window = await self.run_going_back(noted=False)
        window.after.assert_not_called()
        self.assertIn("Could not go back", window.set_status.call_args.args[0])
