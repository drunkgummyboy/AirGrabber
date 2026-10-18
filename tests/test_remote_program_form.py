"""The NAS / other computer form: its pure helpers, the widget, Settings → General's three
download choices, the welcome's third option card, and RemoteDownloads picking the client."""

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
from pydantic import ValidationError

from app.models import DownloadMode, ProwlarrState, ProwlarrStatus, RemoteKind, Settings
from app.services import RemoteDownloads, RemoteError, SettingsService
from app.services.qbittorrent import QBittorrentClient
from app.services.synology import DownloadStationClient
from app.services.transmission import TransmissionClient
from app.ui import theme
from app.ui.remote_program_form import (
    ADDRESS_HINTS,
    PROGRAM_NAMES,
    TESTING_TEXT,
    RemoteProgramForm,
    describe_error,
    folder_note,
    kind_from_name,
    program_name,
    remote_changes,
    result_look,
)
from app.ui.settings_dialog import SettingsDialog
from app.ui.welcome_dialog import REMOTE_NOTE, REMOTE_TITLE, changes_error, download_changes


class FakeClient:
    def __init__(self, kind: RemoteKind) -> None:
        self.kind = kind
        self.error: RemoteError | None = None
        self.tests = 0

    async def test(self) -> str:
        self.tests += 1
        if self.error is not None:
            raise self.error
        return f"{self.kind.program} 9.9"

    async def add_magnet(self, magnet: str) -> None:
        raise AssertionError("not used here")

    async def add_torrent(self, content: bytes, filename: str) -> None:
        raise AssertionError("not used here")


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saved.append(settings)


class FakeProwlarr:
    async def status(self) -> ProwlarrStatus:
        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        raise AssertionError("not changed here")


def validation_error(**fields: object) -> ValidationError:
    try:
        Settings.model_validate(fields)
    except ValidationError as exc:
        return exc
    raise AssertionError(f"{fields} validated")


class HelperTests(unittest.TestCase):
    def test_program_menu(self) -> None:
        self.assertEqual(
            list(PROGRAM_NAMES.values()),
            ["Synology Download Station", "qBittorrent", "Transmission"],
        )
        for kind in RemoteKind:
            self.assertIs(kind_from_name(program_name(kind)), kind)
        with self.assertRaises(ValueError):
            kind_from_name("uTorrent")

    def test_address_hints_use_each_programs_port(self) -> None:
        self.assertEqual(ADDRESS_HINTS[RemoteKind.SYNOLOGY], "http://192.168.1.x:5000")
        self.assertEqual(ADDRESS_HINTS[RemoteKind.QBITTORRENT], "http://192.168.1.x:8080")
        self.assertEqual(ADDRESS_HINTS[RemoteKind.TRANSMISSION], "http://192.168.1.x:9091")

    def test_changes_validate_into_settings(self) -> None:
        changes = remote_changes(
            RemoteKind.TRANSMISSION, " http://nas:9091/ ", " beau ", " pw ", False, " /dl/tv "
        )
        settings = Settings(**changes)
        self.assertIs(settings.remote_kind, RemoteKind.TRANSMISSION)
        self.assertEqual(settings.remote_url, "http://nas:9091")
        self.assertEqual(settings.remote_username, "beau")
        self.assertEqual(settings.remote_password.get_secret_value(), " pw ")
        self.assertFalse(settings.remote_verify_tls)
        self.assertEqual(settings.remote_destination, "/dl/tv")

    def test_folder_note(self) -> None:
        self.assertEqual(
            folder_note(RemoteKind.SYNOLOGY, True),
            "Magnet-only releases are sent to your Download Station.",
        )
        self.assertEqual(
            folder_note(RemoteKind.QBITTORRENT, True),
            "Magnet-only releases are sent to your qBittorrent.",
        )
        self.assertIsNone(folder_note(RemoteKind.TRANSMISSION, False))

    def test_result_lines(self) -> None:
        self.assertEqual(result_look(), (TESTING_TEXT, theme.TEXT_MUTED))
        self.assertEqual(
            result_look(description="qBittorrent v5.0.2"), ("✓ qBittorrent v5.0.2", theme.SUCCESS)
        )
        self.assertEqual(result_look(error="Nope."), ("✗ Nope.", theme.DANGER))

    def test_error_labels(self) -> None:
        self.assertEqual(
            describe_error(validation_error(remote_url="nas:5000")),
            "Address: Use a full address like https://nas.local:5001",
        )


class WelcomeChangesTests(unittest.TestCase):
    REMOTE = remote_changes(RemoteKind.QBITTORRENT, "http://nas:8080", "", "", True, "")

    def test_the_remote_choice_saves_the_form(self) -> None:
        current = Settings(download_dir=Path("C:/dl"))
        changes = download_changes(current, DownloadMode.REMOTE, Path("C:/dl"), self.REMOTE)
        self.assertEqual(changes, {"download_mode": DownloadMode.REMOTE, **self.REMOTE})
        self.assertIsNone(changes_error(current, changes))

    def test_other_choices_leave_the_form_alone(self) -> None:
        current = Settings(download_dir=Path("C:/dl"))
        for mode in (DownloadMode.APP, DownloadMode.FOLDER):
            changes = download_changes(current, mode, Path("C:/dl"), self.REMOTE)
            self.assertEqual(changes, {"download_mode": mode})

    def test_a_bad_address_is_reported_before_saving(self) -> None:
        current = Settings()
        bad = {**self.REMOTE, "remote_url": "nas:8080"}
        changes = download_changes(current, DownloadMode.REMOTE, current.download_dir, bad)
        self.assertEqual(
            changes_error(current, changes),
            "Address: Use a full address like https://nas.local:5001",
        )


class RemoteDownloadsTests(unittest.TestCase):
    def test_the_client_follows_the_settings(self) -> None:
        settings = SettingsService(FakeRepository(), Settings())  # type: ignore[arg-type]
        remote = RemoteDownloads(settings)
        self.assertIsInstance(remote.client(), DownloadStationClient)
        expected = {
            RemoteKind.QBITTORRENT: QBittorrentClient,
            RemoteKind.TRANSMISSION: TransmissionClient,
            RemoteKind.SYNOLOGY: DownloadStationClient,
        }
        for kind, cls in expected.items():
            with self.subTest(kind=kind):
                asyncio.run(settings.update(remote_kind=kind))
                self.assertIs(remote.kind, kind)
                self.assertIsInstance(remote.client(), cls)
                self.assertIs(remote.client().kind, kind)


class GuiTestCase(unittest.IsolatedAsyncioTestCase):
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
        self.clients = {kind: FakeClient(kind) for kind in RemoteKind}

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # collect Tk variables on the main thread

    def services(self, **values: Any) -> tuple[SettingsService, RemoteDownloads]:
        settings = SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **values),
        )
        return settings, RemoteDownloads(settings, clients=self.clients)

    @staticmethod
    def close(widget: Any) -> None:
        if widget.winfo_exists():
            widget.destroy()


class FormTests(GuiTestCase):
    def form(self, settings: Settings, run_test: Any = None) -> RemoteProgramForm:
        async def no_test() -> str:
            raise AssertionError("no test here")

        form = RemoteProgramForm(
            self.root,
            settings=settings,
            run_test=run_test or no_test,
            label_width=120,
            field_width=260,
        )
        self.addCleanup(self.close, form)
        return form

    async def test_starts_from_the_settings_and_returns_its_changes(self) -> None:
        stored = Settings(
            remote_kind=RemoteKind.QBITTORRENT,
            remote_url="http://192.168.1.20:8080",
            remote_username="admin",
            remote_password="pw",
            remote_verify_tls=False,
            remote_destination="/downloads/tv",
        )
        form = self.form(stored)
        self.assertIs(form.kind, RemoteKind.QBITTORRENT)
        self.assertEqual(form._password.cget("show"), "•")
        self.assertEqual(
            form.changes(),
            remote_changes(
                RemoteKind.QBITTORRENT, "http://192.168.1.20:8080", "admin", "pw", False,
                "/downloads/tv",
            ),
        )
        self.assertTrue(form.ready)

    async def test_empty_fields_show_hints_but_store_nothing(self) -> None:
        form = self.form(Settings())
        self.assertEqual(form.changes()["remote_url"], "")
        self.assertEqual(form.changes()["remote_destination"], "")
        self.assertTrue(form.changes()["remote_verify_tls"])
        self.assertEqual(form._address._entry.get(), ADDRESS_HINTS[RemoteKind.SYNOLOGY])
        self.assertFalse(form.ready)

    async def test_the_program_menu_switches_the_hints(self) -> None:
        form = self.form(Settings())
        form._program.set("Transmission")
        form._program_changed("Transmission")
        self.assertIs(form.kind, RemoteKind.TRANSMISSION)
        self.assertEqual(form._address._entry.get(), "http://192.168.1.x:9091")
        self.assertEqual(form.changes()["remote_url"], "")
        self.assertIn("Transmission's default", form._destination._entry.get())

    async def test_test_button_shows_success_and_failure(self) -> None:
        answers: list[Any] = ["qBittorrent v5.0.2", RemoteError("Couldn't reach nas:8080.")]

        async def run_test() -> str:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return str(answer)

        form = self.form(Settings(), run_test)
        form._start_test()
        self.assertEqual(form._result.cget("text"), TESTING_TEXT)
        self.assertEqual(form._test_button.cget("state"), "disabled")
        assert form._test_task is not None
        await form._test_task
        self.assertEqual(form._result.cget("text"), "✓ qBittorrent v5.0.2")
        self.assertEqual(form._result.cget("text_color"), theme.SUCCESS)
        self.assertEqual(form._test_button.cget("state"), "normal")
        form._start_test()
        await form._test_task
        self.assertEqual(form._result.cget("text"), "✗ Couldn't reach nas:8080.")
        self.assertEqual(form._result.cget("text_color"), theme.DANGER)


class SettingsGeneralTests(GuiTestCase):
    def dialog(self, **values: Any) -> tuple[SettingsDialog, SettingsService]:
        settings, remote = self.services(**values)
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=remote,
            updates=SimpleNamespace(current="2.0.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        self.addCleanup(self.close, dialog)
        return dialog, settings

    def shown(self, widget: Any) -> bool:
        return bool(widget.winfo_manager())

    async def test_three_choices_and_their_rows(self) -> None:
        dialog, _settings = self.dialog(download_mode=DownloadMode.APP)
        radios = [
            w.cget("text")
            for w in dialog._remote_form.master.winfo_children()
            if isinstance(w, ctk.CTkRadioButton)
        ]
        self.assertEqual(
            radios,
            [
                "Open in my torrent program on this PC",
                "Send to a torrent program on my NAS\nor another computer",
                "Save .torrent files to a folder",
            ],
        )
        expected = {
            DownloadMode.APP: (True, False, False),
            DownloadMode.REMOTE: (False, True, False),
            DownloadMode.FOLDER: (False, False, True),
        }
        for mode, (status, form, folder) in expected.items():
            with self.subTest(mode=mode):
                dialog._mode.set(mode.value)
                dialog._show_download_rows()
                self.assertEqual(self.shown(dialog._torrent), status)
                self.assertEqual(self.shown(dialog._remote_form), form)
                self.assertEqual(self.shown(dialog._folder_row), folder)

    async def test_folder_note_names_the_configured_program(self) -> None:
        dialog, _settings = self.dialog(
            remote_kind=RemoteKind.QBITTORRENT, remote_url="http://192.168.1.20:8080"
        )
        self.assertTrue(self.shown(dialog._folder_note))
        self.assertEqual(
            dialog._folder_note.cget("text"), "Magnet-only releases are sent to your qBittorrent."
        )
        nothing, _ = self.dialog()  # Download Station without an address
        self.assertFalse(self.shown(nothing._folder_note))

    async def test_save_stores_the_form_and_the_mode(self) -> None:
        dialog, settings = self.dialog()
        dialog._mode.set(DownloadMode.REMOTE.value)
        form = dialog._remote_form
        form._program.set("Transmission")
        form._address.insert(0, "http://192.168.1.30:9091")
        form._destination.insert(0, "/downloads/tv")
        await dialog._save()
        current = settings.current
        self.assertIs(current.download_mode, DownloadMode.REMOTE)
        self.assertIs(current.remote_kind, RemoteKind.TRANSMISSION)
        self.assertEqual(current.remote_url, "http://192.168.1.30:9091")
        self.assertEqual(current.remote_destination, "/downloads/tv")

    async def test_test_saves_first_then_tests_the_chosen_program(self) -> None:
        dialog, settings = self.dialog()
        form = dialog._remote_form
        form._program.set("qBittorrent")
        form._address.insert(0, "http://192.168.1.20:8080")
        form._start_test()
        assert form._test_task is not None
        await form._test_task
        self.assertEqual(settings.current.remote_url, "http://192.168.1.20:8080")
        self.assertEqual(self.clients[RemoteKind.QBITTORRENT].tests, 1)
        self.assertEqual(form._result.cget("text"), "✓ qBittorrent 9.9")

    async def test_a_bad_address_fails_the_test_without_testing(self) -> None:
        dialog, _settings = self.dialog()
        form = dialog._remote_form
        form._address.insert(0, "192.168.1.20")
        form._start_test()
        assert form._test_task is not None
        await form._test_task
        self.assertEqual(
            form._result.cget("text"), "✗ Address: Use a full address like https://nas.local:5001"
        )
        self.assertEqual(self.clients[RemoteKind.SYNOLOGY].tests, 0)

    async def test_the_advanced_tab_has_no_nas_section_any_more(self) -> None:
        dialog, _settings = self.dialog()
        texts: list[str] = []
        stack: list[Any] = [dialog]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, ctk.CTkLabel):
                texts.append(str(widget.cget("text")))
        self.assertNotIn("Synology NAS (Download Station)", texts)
        self.assertNotIn("NAS address", texts)


class WelcomeOptionTests(GuiTestCase):
    async def test_third_card_shows_the_form_when_chosen(self) -> None:
        from app.ui.welcome_dialog import WelcomeDialog

        settings, remote = self.services()
        dialog = WelcomeDialog(
            self.root,
            settings=settings,
            torrent_app=mock.MagicMock(),
            discovery=mock.MagicMock(),
            airdates=mock.MagicMock(),
            library=mock.MagicMock(),
            remote=remote,
        )
        self.addCleanup(self.close, dialog)
        labels = [
            str(w.cget("text"))
            for w in _descendants(dialog._remote_card)
            if isinstance(w, ctk.CTkLabel)
        ]
        self.assertIn(REMOTE_TITLE, labels)
        self.assertIn(REMOTE_NOTE, labels)
        self.assertFalse(dialog._remote_card.extra.winfo_manager())
        dialog._select_mode(DownloadMode.REMOTE)
        self.assertTrue(dialog._remote_card.extra.winfo_manager())
        self.assertFalse(dialog._folder_card.extra.winfo_manager())
        form = dialog._remote_form
        form._program.set("qBittorrent")
        form._address.insert(0, "http://192.168.1.20:8080")
        form._start_test()
        assert form._test_task is not None
        await form._test_task
        self.assertEqual(form._result.cget("text"), "✓ qBittorrent 9.9")
        self.assertIs(settings.current.remote_kind, RemoteKind.QBITTORRENT)  # saved first


def _descendants(widget: Any) -> list[Any]:
    found: list[Any] = []
    stack = [widget]
    while stack:
        current = stack.pop()
        found.append(current)
        stack.extend(current.winfo_children())
    return found


if __name__ == "__main__":
    unittest.main()
