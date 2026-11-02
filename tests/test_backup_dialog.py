"""Backup & move in the UI (app/ui/backup_dialog.py): the summary and result texts, the save
dialog (keys and passwords off by default, the file dialog, a failure), the load dialog (what
the file holds, Replace / Add to what I have, a file that can't be loaded, the restart note),
Settings → Backup and the welcome's "Moving from another PC?" link, on a hidden Tk root with a
stand-in BackupService. The file dialogs are never opened."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.i18n import Language, current_language
from app.models import Accent, ProwlarrState, ProwlarrStatus, Settings
from app.models.backup import (
    FORMAT_NAME,
    FORMAT_VERSION,
    BackupFile,
    BackupLoadResult,
    BackupSummary,
    KeptDownloads,
    LoadMode,
    RestoreCounts,
)
from app.services import SettingsService
from app.services.backup import BackupError, BackupProblem
from app.ui import backup_dialog, theme
from app.ui.backup_dialog import (
    CHECK_FOLDERS,
    NO_SECRETS,
    NOTHING_CHANGED,
    SECRETS_CHECK,
    SECRETS_NOTE,
    LoadBackupDialog,
    SaveBackupDialog,
    detail_line,
    load_backup,
    restart_note,
    result_lines,
    result_notes,
    summary_line,
)
from app.ui.quick_setup import ACCENT_RESTART_TEXT, BOTH_RESTART_TEXT, RESTART_TEXT
from app.ui.settings_dialog import (
    BACKUP_TEXT,
    LOAD_BACKUP_BUTTON,
    SAVE_BACKUP_BUTTON,
    SECTION_BACKUP,
    SettingsDialog,
)
from app.ui.welcome_dialog import MOVING_LINK, WelcomeDialog
from tests.people import FakePeople

CREATED = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
SUMMARY = BackupSummary(
    shows=12, films=5, grabs=340, settings=True, secrets=False, created=CREATED,
    app_version="2.0.9",
)
FILE = BackupFile(
    format=FORMAT_NAME, version=FORMAT_VERSION, app_version="2.0.9", created=CREATED,
    settings={"language": "en"},
)


def result(
    *,
    mode: LoadMode = LoadMode.REPLACE,
    replaced: bool = True,
    secrets: bool = False,
    language: Language | None = None,
    accent: Accent | None = None,
    kept: KeptDownloads = KeptDownloads.NONE,
    **counts: int,
) -> BackupLoadResult:
    return BackupLoadResult(
        mode=mode,
        counts=RestoreCounts(**counts),
        settings_replaced=replaced,
        secrets_loaded=secrets,
        language=language if language is not None else current_language(),
        accent=accent if accent is not None else theme.current_accent(),
        downloads_kept=kept,
    )


class TextTests(unittest.TestCase):
    def test_the_summary_line(self) -> None:
        self.assertEqual(summary_line(SUMMARY), "12 shows, 5 films on the watchlist, settings")
        one = SUMMARY.model_copy(update={"shows": 1, "films": 1, "settings": False})
        self.assertEqual(summary_line(one), "1 show, 1 film on the watchlist")

    def test_the_detail_line(self) -> None:
        line = detail_line(SUMMARY)
        self.assertIn("by AirGrabber 2.0.9", line)
        self.assertIn("340 downloads in the history", line)
        self.assertTrue(line.endswith("without keys and passwords"))
        with_keys = detail_line(SUMMARY.model_copy(update={"secrets": True, "app_version": ""}))
        self.assertTrue(with_keys.startswith("Saved "))
        self.assertNotIn("AirGrabber", with_keys)
        self.assertTrue(with_keys.endswith("with keys and passwords"))

    def test_what_a_load_says(self) -> None:
        added = result(mode=LoadMode.ADD, replaced=False, shows_added=2, films_added=1)
        self.assertEqual(result_lines(added), ["Added: 2 shows, 1 film."])
        self.assertEqual(result_notes(added), [])
        replaced = result(shows_added=1, shows_removed=3, films_removed=1, grabs_added=40)
        self.assertEqual(
            result_lines(replaced),
            [
                "Added: 1 show, 0 films.",
                "Removed, as they aren't in the backup: 3 shows, 1 film.",
                "40 downloads added to the history.",
                "Your settings are now the backup's.",
            ],
        )
        self.assertEqual(result_notes(replaced), [CHECK_FOLDERS, NO_SECRETS])
        self.assertEqual(result_notes(result(secrets=True)), [CHECK_FOLDERS])

    def test_where_downloads_go_stayed(self) -> None:
        here = result_lines(result(kept=KeptDownloads.LOCAL))
        self.assertEqual(here[-1], "Your downloads keep using qBittorrent on this PC.")
        there = result_lines(result(kept=KeptDownloads.THIS_PC))
        self.assertEqual(
            there[-1],
            "Where downloads go stays as it was on this PC: the backup's qBittorrent was on the "
            "other PC.",
        )
        self.assertNotIn(here[-1], result_lines(result()))

    def test_the_restart_note(self) -> None:
        loaded = result(language=Language.NL, accent=Accent.OCEAN)
        self.assertIsNone(restart_note(loaded, Language.NL, Accent.OCEAN))
        self.assertEqual(restart_note(loaded, Language.EN, Accent.OCEAN), RESTART_TEXT)
        self.assertEqual(restart_note(loaded, Language.NL, Accent.SLATE), ACCENT_RESTART_TEXT)
        self.assertEqual(restart_note(loaded, Language.EN, Accent.SLATE), BOTH_RESTART_TEXT)


class FakeBackup:
    """Stands in for BackupService: records what was asked."""

    def __init__(
        self,
        *,
        opened: BackupFile | BackupError = FILE,
        loaded: BackupLoadResult | None = None,
        save_error: BackupError | None = None,
    ) -> None:
        self.opened = opened
        self.loaded = loaded if loaded is not None else result(shows_added=12, films_added=5)
        self.save_error = save_error
        self.saves: list[tuple[Path, bool]] = []
        self.loads: list[LoadMode] = []

    async def save(self, path: Path, *, include_secrets: bool) -> BackupSummary:
        self.saves.append((path, include_secrets))
        if self.save_error is not None:
            raise self.save_error
        return SUMMARY.model_copy(update={"secrets": include_secrets})

    async def open(self, path: Path) -> BackupFile:
        if isinstance(self.opened, BackupError):
            raise self.opened
        return self.opened

    async def load(self, backup: BackupFile, mode: LoadMode) -> BackupLoadResult:
        self.loads.append(mode)
        return self.loaded.model_copy(update={"mode": mode})


class FakeRepository:
    async def save(self, settings: Settings) -> None:
        pass


def texts(widget: Any, kind: Any = ctk.CTkLabel) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, kind):
            found.append(str(child.cget("text")))
        found += texts(child, kind)
    return found


def buttons(widget: Any) -> dict[str, ctk.CTkButton]:
    found: dict[str, ctk.CTkButton] = {}
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            found[str(child.cget("text"))] = child
        found.update(buttons(child))
    return found


class DialogTestCase(unittest.IsolatedAsyncioTestCase):
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
        # Building Settings or the welcome is slow.
        asyncio.get_running_loop().slow_callback_duration = 10.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(8):
            await asyncio.sleep(0.01)
            self.root.update()

    def keep(self, widget: Any) -> None:
        self.addCleanup(lambda: widget.destroy() if widget.winfo_exists() else None)

    def settings(self, **values: Any) -> SettingsService:
        return SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **values),
        )


class SaveDialogTests(DialogTestCase):
    def open(self, fake: FakeBackup) -> SaveBackupDialog:
        dialog = SaveBackupDialog(self.root, backup=fake)  # type: ignore[arg-type]
        self.keep(dialog)
        return dialog

    async def test_keys_and_passwords_are_off_at_first(self) -> None:
        dialog = self.open(FakeBackup())
        self.assertFalse(dialog.include_secrets.get())
        self.assertIn(SECRETS_CHECK, texts(dialog, ctk.CTkCheckBox))
        self.assertIn(SECRETS_NOTE, texts(dialog))
        self.assertEqual(set(buttons(dialog)), {"Cancel", "Save…"})

    async def test_saving_where_the_file_dialog_says(self) -> None:
        fake = FakeBackup()
        dialog = self.open(fake)
        target = self.folder / "AirGrabber backup 2026-10-31.airgrabber"
        with mock.patch.object(backup_dialog, "ask_save_path", return_value=target) as ask:
            dialog.choose_file()
        self.assertTrue(ask.call_args.args[1].startswith("AirGrabber backup "))
        self.assertTrue(ask.call_args.args[1].endswith(".airgrabber"))
        await self.settle()
        self.assertEqual(fake.saves, [(target, False)])
        self.assertEqual(dialog.heading.cget("text"), "Backup saved")
        self.assertIn("12 shows, 5 films on the watchlist, settings", texts(dialog))
        self.assertIn(str(target), texts(dialog))
        buttons(dialog)["OK"].invoke()
        self.assertFalse(dialog.winfo_exists())

    async def test_with_keys_and_passwords(self) -> None:
        fake = FakeBackup()
        dialog = self.open(fake)
        dialog.include_secrets.set(True)
        target = self.folder / "backup.airgrabber"
        with mock.patch.object(backup_dialog, "ask_save_path", return_value=target):
            dialog.choose_file()
        await self.settle()
        self.assertEqual(fake.saves, [(target, True)])
        self.assertIn(
            "The keys and passwords are in it: keep it somewhere safe.", texts(dialog)
        )

    async def test_a_cancelled_file_dialog_saves_nothing(self) -> None:
        fake = FakeBackup()
        dialog = self.open(fake)
        with mock.patch.object(backup_dialog, "ask_save_path", return_value=None):
            dialog.choose_file()
        await self.settle()
        self.assertEqual(fake.saves, [])
        self.assertEqual(dialog.heading.cget("text"), "Save a backup")

    async def test_a_failure_shows_why_and_stays(self) -> None:
        error = BackupError(BackupProblem.UNWRITABLE, "Couldn't save the backup there.")
        dialog = self.open(FakeBackup(save_error=error))
        with mock.patch.object(backup_dialog, "ask_save_path", return_value=self.folder / "x"):
            dialog.choose_file()
        await self.settle()
        self.assertEqual(dialog.heading.cget("text"), "Save a backup")
        self.assertIn("Couldn't save the backup there.", texts(dialog))
        self.assertEqual(str(buttons(dialog)["Save…"].cget("state")), "normal")


class LoadDialogTests(DialogTestCase):
    def open(self, fake: FakeBackup, **callbacks: Any) -> LoadBackupDialog:
        dialog = LoadBackupDialog(
            self.root, backup=fake, path=self.folder / "b.airgrabber", **callbacks  # type: ignore[arg-type]
        )
        self.keep(dialog)
        return dialog

    async def test_what_it_holds_then_replace(self) -> None:
        done: list[BackupLoadResult | None] = []
        fake = FakeBackup()
        dialog = self.open(fake, on_done=done.append)
        await self.settle()
        self.assertEqual(dialog.heading.cget("text"), "Load this backup?")
        shown = texts(dialog)
        self.assertIn("0 shows, 0 films on the watchlist, settings", shown)
        self.assertEqual(set(buttons(dialog)), {"Replace", "Add to what I have", "Cancel"})
        dialog.choose(LoadMode.REPLACE)
        await self.settle()
        self.assertEqual(fake.loads, [LoadMode.REPLACE])
        self.assertEqual(dialog.heading.cget("text"), "Backup loaded")
        shown = texts(dialog)
        self.assertIn("Added: 12 shows, 5 films.", shown)
        self.assertIn(CHECK_FOLDERS, shown)
        self.assertNotIn("Restart now", buttons(dialog))  # same language and accent colour
        buttons(dialog)["OK"].invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(len(done), 1)
        self.assertIs(done[0] and done[0].mode, LoadMode.REPLACE)

    async def test_add_to_what_i_have(self) -> None:
        loaded: list[BackupLoadResult] = []

        async def on_loaded(outcome: BackupLoadResult) -> None:
            loaded.append(outcome)

        fake = FakeBackup(loaded=result(mode=LoadMode.ADD, replaced=False, shows_added=1))
        dialog = self.open(fake, on_loaded=on_loaded)
        await self.settle()
        buttons(dialog)["Add to what I have"].invoke()
        await self.settle()
        self.assertEqual(fake.loads, [LoadMode.ADD])
        self.assertEqual([outcome.mode for outcome in loaded], [LoadMode.ADD])
        self.assertNotIn(CHECK_FOLDERS, texts(dialog))

    async def test_another_language_offers_a_restart(self) -> None:
        other = Language.NL if current_language() is Language.EN else Language.EN
        restarts: list[int] = []
        fake = FakeBackup(loaded=result(language=other))
        dialog = self.open(fake, on_restart=lambda: restarts.append(1))
        await self.settle()
        dialog.choose(LoadMode.REPLACE)
        await self.settle()
        self.assertIn("Restart AirGrabber to switch the language", texts(dialog))
        buttons(dialog)["Restart now"].invoke()
        self.assertEqual(restarts, [1])

    async def test_a_file_it_cant_load(self) -> None:
        done: list[BackupLoadResult | None] = []
        error = BackupError(BackupProblem.NEWER, "This backup was made by a newer AirGrabber.")
        fake = FakeBackup(opened=error)
        dialog = self.open(fake, on_done=done.append)
        await self.settle()
        self.assertEqual(dialog.heading.cget("text"), "Can't load this file")
        self.assertIn("This backup was made by a newer AirGrabber.", texts(dialog))
        self.assertIn(NOTHING_CHANGED, texts(dialog))
        self.assertEqual(set(buttons(dialog)), {"OK"})
        buttons(dialog)["OK"].invoke()
        self.assertEqual(fake.loads, [])
        self.assertEqual(done, [None])

    async def test_cancel_loads_nothing(self) -> None:
        done: list[BackupLoadResult | None] = []
        fake = FakeBackup()
        dialog = self.open(fake, on_done=done.append)
        await self.settle()
        buttons(dialog)["Cancel"].invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual((fake.loads, done), ([], [None]))

    async def test_no_file_picked_opens_nothing(self) -> None:
        with mock.patch.object(backup_dialog, "ask_open_path", return_value=None):
            self.assertIsNone(load_backup(self.root, backup=FakeBackup()))  # type: ignore[arg-type]


class FakeProwlarr:
    async def status(self) -> ProwlarrStatus:
        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        raise AssertionError("not changed here")


class SettingsSectionTests(DialogTestCase):
    def dialog(self, fake: FakeBackup) -> SettingsDialog:
        dialog = SettingsDialog(
            self.root,
            self.settings(),
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.9"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            backup=fake,  # type: ignore[arg-type]
        )
        self.keep(dialog)
        return dialog

    async def test_the_backup_section(self) -> None:
        dialog = self.dialog(FakeBackup())
        items = list(dialog._items)
        self.assertEqual(items[items.index(SECTION_BACKUP) + 1], "Advanced")
        page = dialog.page(SECTION_BACKUP)
        self.assertEqual(texts(page)[0], "Backup")
        self.assertIn(BACKUP_TEXT, texts(page))
        self.assertEqual(
            {SAVE_BACKUP_BUTTON, LOAD_BACKUP_BUTTON} & set(buttons(page)),
            {SAVE_BACKUP_BUTTON, LOAD_BACKUP_BUTTON},
        )

    async def test_save_opens_the_save_dialog(self) -> None:
        dialog = self.dialog(FakeBackup())
        buttons(dialog.page(SECTION_BACKUP))[SAVE_BACKUP_BUTTON].invoke()
        self.assertIsInstance(dialog._backup_dialog, SaveBackupDialog)

    async def test_a_replacing_load_closes_settings(self) -> None:
        fake = FakeBackup()
        dialog = self.dialog(fake)
        with mock.patch.object(
            backup_dialog, "ask_open_path", return_value=self.folder / "b.airgrabber"
        ):
            buttons(dialog.page(SECTION_BACKUP))[LOAD_BACKUP_BUTTON].invoke()
        loading = dialog._backup_dialog
        assert isinstance(loading, LoadBackupDialog)
        await self.settle()
        loading.choose(LoadMode.REPLACE)
        await self.settle()
        self.assertTrue(dialog.winfo_exists())  # until the result is closed
        buttons(loading)["OK"].invoke()
        self.assertFalse(dialog.winfo_exists())  # its form showed the old settings

    async def test_adding_keeps_settings_open(self) -> None:
        dialog = self.dialog(FakeBackup())
        dialog._backup_loaded(result(mode=LoadMode.ADD, replaced=False))
        self.assertTrue(dialog.winfo_exists())
        dialog._backup_loaded(None)
        self.assertTrue(dialog.winfo_exists())


class WelcomeLinkTests(DialogTestCase):
    def welcome(self, settings: SettingsService, **extra: Any) -> WelcomeDialog:
        dialog = WelcomeDialog(
            self.root,
            settings=settings,
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            airdates=mock.MagicMock(),
            library=mock.MagicMock(),
            profiles=FakePeople(),
            **extra,
        )
        self.keep(dialog)
        return dialog

    async def test_no_link_without_the_service(self) -> None:
        dialog = self.welcome(self.settings())
        self.assertNotIn(MOVING_LINK, texts(dialog))

    async def test_a_replacing_load_finishes_the_setup(self) -> None:
        settings = self.settings()
        dialog = self.welcome(settings, backup=FakeBackup())
        self.assertEqual(dialog.backup_link.cget("text"), MOVING_LINK)
        with mock.patch.object(
            backup_dialog, "ask_open_path", return_value=self.folder / "b.airgrabber"
        ):
            dialog._load_backup()
        loading = dialog._backup_dialog
        assert loading is not None
        await self.settle()
        loading.choose(LoadMode.REPLACE)
        await self.settle()
        self.assertTrue(settings.current.setup_done)  # stored before the result shows
        buttons(loading)["OK"].invoke()
        self.assertFalse(dialog.winfo_exists())

    async def test_adding_carries_on_with_the_setup(self) -> None:
        settings = self.settings()
        fake = FakeBackup(loaded=result(mode=LoadMode.ADD, replaced=False, shows_added=3))
        dialog = self.welcome(settings, backup=fake)
        with mock.patch.object(
            backup_dialog, "ask_open_path", return_value=self.folder / "b.airgrabber"
        ):
            dialog._load_backup()
        loading = dialog._backup_dialog
        assert loading is not None
        await self.settle()
        loading.choose(LoadMode.ADD)
        await self.settle()
        buttons(loading)["OK"].invoke()
        self.assertTrue(dialog.winfo_exists())
        self.assertFalse(settings.current.setup_done)


if __name__ == "__main__":
    unittest.main()
