"""Removing the old MediaForge and the double-watching guard (app/forge/old_install.py, F-27);
Settings' notice with "Remove the old MediaForge" (app/forge/ui/import_step.py's RemoveOldBox);
quitting waits for a confirm window that renames (ForgeHost.may_close).

No old install here is real: the "Installed apps" entry is a fake reader's (or a fake `find`),
the uninstaller a fake launcher, and every test refuses the real start
(`old_install._run_uninstaller`); tests/forge/base.py hides the owner's old MediaForge."""

from __future__ import annotations

import asyncio
import ntpath
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest import mock

import customtkinter as ctk
import httpx

from app.config import AppPaths
from app.forge import old_install
from app.forge.migration import OldInstall, find_old_install
from app.forge.models.settings import ForgeSettings
from app.forge.old_install import (
    BRING_FIRST_TEXT,
    GUARD_TEXT,
    NOTICE_MARKER,
    NOTICE_TEXT,
    OUTCOME_TEXTS,
    REMOVE_OLD,
    WAITING_TEXT,
    OldInstallRemover,
    Outcome,
    remover_for,
    split_command,
    startup_notice,
    uninstaller_command,
    uninstaller_script,
)
from app.forge.runtime import ForgeRuntime
from app.forge.services import ForgeServices
from app.forge.ui.import_step import BringOverBox, RemoveOldBox
from app.i18n import t
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_host import HostCase
from tests.forge.ui.test_page import TkCase

LOCATION = r"C:\Users\Alex\AppData\Local\Programs\MediaForge"
SCRIPT = LOCATION + r"\src\mediaforge\uninstall.ps1"
# What MediaForge 1.3's install.ps1 registers.
REGISTERED = (
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + SCRIPT + '"'
)
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def old(uninstall: str = REGISTERED, location: str = LOCATION, data: bool = True) -> OldInstall:
    return OldInstall(
        db_file=Path(r"C:\Users\Alex\AppData\Roaming\MediaForge\mediaforge.db") if data else None,
        install_location=location,
        uninstall_string=uninstall,
        version="1.3.0",
    )


def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("tests must never start the real old uninstaller: use a fake launcher")


def guard_uninstaller(case: Any) -> None:
    case.enterContext(mock.patch.object(old_install, "_run_uninstaller", _refuse))


class FakeRenamer:
    def __init__(self, blocked: bool = True) -> None:
        self.blocked_by_old_install = blocked
        self.reconfigured = 0

    def reconfigure(self) -> None:
        self.reconfigured += 1


class FakeRuntime:
    def __init__(self, services: Any) -> None:
        self.services: Any = services


class UninstallStringTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        guard_uninstaller(self)

    def test_split_command(self) -> None:
        self.assertEqual(
            split_command(r'powershell.exe  -File "C:\a b\c.ps1"'),
            ["powershell.exe", "-File", r"C:\a b\c.ps1"],
        )
        self.assertEqual(split_command('""'), [""])
        with self.assertRaises(ValueError):
            split_command('powershell -File "C:\\a')

    def test_accepts_what_the_old_installer_registers(self) -> None:
        accepted = {
            "as registered": REGISTERED,
            "full path": f'"{POWERSHELL}" -NoProfile -File "{SCRIPT}"',
            "no switches": f'powershell.exe -File "{SCRIPT}"',
            "other case": f'POWERSHELL.EXE -noprofile -executionpolicy BYPASS -file "{SCRIPT.upper()}"',
            "no quotes": r"powershell -File C:\MF\src\mediaforge\uninstall.ps1",
        }
        for name, command in accepted.items():
            with self.subTest(name):
                location = r"C:\MF" if name == "no quotes" else LOCATION
                script = uninstaller_script(old(command, location))
                self.assertIsNotNone(script)
                assert script is not None
                self.assertTrue(script.lower().endswith(r"src\mediaforge\uninstall.ps1"))

    def test_refuses_anything_else(self) -> None:
        refused = {
            "pwsh": f'pwsh.exe -File "{SCRIPT}"',
            "another powershell": rf'"C:\Temp\powershell.exe" -File "{SCRIPT}"',
            "cmd": f'cmd.exe /c "{SCRIPT}"',
            "another script": rf'powershell.exe -File "{LOCATION}\evil.ps1"',
            "another folder": r'powershell.exe -File "C:\Other\src\mediaforge\uninstall.ps1"',
            "a dotted escape": rf'powershell.exe -File "{LOCATION}\src\mediaforge\..\..\x.ps1"',
            "an argument after": REGISTERED + " -Quiet",
            "a command": f'powershell.exe -Command "& \'{SCRIPT}\'"',
            "an unknown switch": f'powershell.exe -EncodedCommand AAAA -File "{SCRIPT}"',
            "a bad policy": f'powershell.exe -ExecutionPolicy Whatever -File "{SCRIPT}"',
            "no -File": f'powershell.exe "{SCRIPT}"',
            "unbalanced": f'powershell.exe -File "{SCRIPT}',
            "empty": "",
        }
        for name, command in refused.items():
            with self.subTest(name):
                self.assertIsNone(uninstaller_script(old(command)))
        for name, location in {"relative": r"Programs\MediaForge", "none": ""}.items():
            with self.subTest(location=name):
                self.assertIsNone(uninstaller_script(old(location=location)))

    def test_the_command_started_is_built_not_taken(self) -> None:
        command = uninstaller_command(SCRIPT, POWERSHELL)
        self.assertEqual(command[0], POWERSHELL)
        self.assertEqual(command[-2:], ["-File", SCRIPT])
        self.assertIn("Hidden", command)
        self.assertNotIn("-Quiet", command)


class RemoverCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        guard_uninstaller(self)
        self.install = self.tmp / "MediaForge"
        self.script = self.install / "src" / "mediaforge" / "uninstall.ps1"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# a fake uninstaller, never run\n", encoding="utf-8")
        registered = (
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
            f'-File "{self.script}"'
        )
        self.entry: OldInstall | None = old(registered, str(self.install))
        self.launched: list[list[str]] = []
        self.uninstall_removes = True  # what the fake uninstaller does (the owner says Yes)
        self.renamer = FakeRenamer()
        self.runtime = FakeRuntime(SimpleNamespace(auto=self.renamer))
        self.heard: list[Outcome | None] = []

    def find(self) -> OldInstall | None:
        return self.entry

    async def launch(self, command: Sequence[str]) -> int:
        self.launched.append(list(command))
        await asyncio.sleep(0)
        if self.uninstall_removes:
            self.entry = None
        return 0

    def remover(self) -> OldInstallRemover:
        remover = OldInstallRemover(
            self.runtime, find=self.find, launch=self.launch, powershell=lambda: POWERSHELL
        )
        remover.subscribe(self.heard.append)
        return remover


class RemoverTests(RemoverCase):
    async def test_runs_the_old_uninstaller_and_lifts_the_guard(self) -> None:
        remover = self.remover()
        self.assertEqual(await remover.remove(), Outcome.REMOVED)
        self.assertEqual(len(self.launched), 1)
        command = self.launched[0]
        self.assertEqual(command[0], POWERSHELL)
        self.assertTrue(ntpath.normcase(command[-1]) == ntpath.normcase(str(self.script)))
        self.assertFalse(self.renamer.blocked_by_old_install)
        self.assertEqual(self.renamer.reconfigured, 1)
        self.assertEqual(self.heard, [None, Outcome.REMOVED])
        self.assertEqual(remover.last, Outcome.REMOVED)

    async def test_the_owner_said_no(self) -> None:
        self.uninstall_removes = False
        remover = self.remover()
        self.assertEqual(await remover.remove(), Outcome.KEPT)
        self.assertTrue(self.renamer.blocked_by_old_install)
        self.assertEqual(self.renamer.reconfigured, 0)
        self.assertEqual(self.heard, [None, Outcome.KEPT])

    async def test_an_unknown_uninstall_string_is_never_started(self) -> None:
        self.entry = old(f'cmd.exe /c "{self.script}"', str(self.install))
        with self.assertLogs("app.forge.old_install", "WARNING"):
            self.assertEqual(await self.remover().remove(), Outcome.REFUSED)
        self.assertEqual(self.launched, [])
        self.assertTrue(self.renamer.blocked_by_old_install)

    async def test_a_missing_script_is_never_started(self) -> None:
        self.script.unlink()
        with self.assertLogs("app.forge.old_install", "WARNING"):
            self.assertEqual(await self.remover().remove(), Outcome.REFUSED)
        self.assertEqual(self.launched, [])

    async def test_a_start_that_fails(self) -> None:
        async def broken(command: Sequence[str]) -> int:
            raise FileNotFoundError("powershell")

        remover = OldInstallRemover(self.runtime, find=self.find, launch=broken)
        with self.assertLogs("app.forge.old_install", "ERROR") as logs:
            self.assertEqual(await remover.remove(), Outcome.FAILED)
        self.assertNotIn(str(self.install), "\n".join(logs.output))
        self.assertTrue(self.renamer.blocked_by_old_install)

    async def test_gone_already(self) -> None:
        self.entry = old(uninstall="", location="")  # only the data is left
        self.assertEqual(await self.remover().remove(), Outcome.REMOVED)
        self.assertEqual(self.launched, [])
        self.assertFalse(self.renamer.blocked_by_old_install)

    async def test_a_second_click_shares_the_first_removal(self) -> None:
        release = asyncio.Event()

        async def slow(command: Sequence[str]) -> int:
            self.launched.append(list(command))
            await release.wait()
            self.entry = None
            return 0

        remover = OldInstallRemover(self.runtime, find=self.find, launch=slow)
        first = asyncio.ensure_future(remover.remove())
        await asyncio.sleep(0.05)
        self.assertTrue(remover.running)
        second = asyncio.ensure_future(remover.remove())
        await asyncio.sleep(0.01)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [Outcome.REMOVED] * 2)
        self.assertEqual(len(self.launched), 1)
        self.assertFalse(remover.running)

    async def test_waiting_can_be_cancelled_without_stopping_the_removal(self) -> None:
        release = asyncio.Event()

        async def slow(command: Sequence[str]) -> int:
            await release.wait()
            self.entry = None
            return 0

        remover = OldInstallRemover(self.runtime, find=self.find, launch=slow)
        waiting = asyncio.ensure_future(remover.remove())
        await asyncio.sleep(0.05)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        release.set()
        await asyncio.sleep(0.05)
        self.assertEqual(remover.last, Outcome.REMOVED)
        self.assertFalse(self.renamer.blocked_by_old_install)

    async def test_forge_not_started_yet(self) -> None:
        self.runtime = FakeRuntime(None)
        self.assertEqual(await self.remover().remove(), Outcome.REMOVED)

    async def test_one_remover_per_runtime(self) -> None:
        self.assertIs(remover_for(self.runtime), remover_for(self.runtime))
        self.assertIsNot(remover_for(self.runtime), remover_for(FakeRuntime(None)))

    async def test_the_real_start_is_refused_in_tests(self) -> None:
        remover = OldInstallRemover(self.runtime, find=self.find, powershell=lambda: POWERSHELL)
        with self.assertLogs("app.forge.old_install", "ERROR"):
            self.assertEqual(await remover.remove(), Outcome.FAILED)

    async def test_the_real_reader_finds_nothing_in_tests(self) -> None:
        self.assertIsNone(find_old_install())


class GuardTests(ForgeAsyncTestCase):
    """The real renamer: Trial waits while the old install is registered, starts once it's gone."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        guard_uninstaller(self)
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        self.addAsyncCleanup(http.aclose)
        self.runtime = ForgeRuntime(
            AppPaths(self.data_dir),
            http,
            TMDBClient(http, api_key=lambda: ""),
            ImageCache(http, self.data_dir / "posters"),
            cast(SettingsService, mock.Mock(spec=SettingsService)),
            parser_ready=lambda: True,
            old_install_blocks=lambda: True,
        )
        self.addAsyncCleanup(self.runtime.aclose)

    async def test_trial_applies_once_the_old_install_is_gone(self) -> None:
        services = await self.runtime.start()
        assert services is not None
        watch, series, movies = (self.tmp / name for name in ("dl", "series", "movies"))
        for folder in (watch, series, movies):
            folder.mkdir()
        await services.settings.update(
            watch_folders=[str(watch)],
            series_folder=str(series),
            movies_folder=str(movies),
            automatic="trial",
        )
        statuses: list[str] = []
        services.auto.on_status = lambda status: statuses.append(status.state)
        self.assertTrue(services.auto.blocked_by_old_install)
        self.assertFalse(services.auto.running)
        self.assertEqual(services.auto.status([]).text, t("Not watching until the old MediaForge is removed"))

        entry: list[OldInstall | None] = [None]

        async def uninstalled(command: Sequence[str]) -> int:
            raise AssertionError("gone already: nothing to start")

        remover = OldInstallRemover(self.runtime, find=lambda: entry[0], launch=uninstalled)
        self.assertEqual(await remover.remove(), Outcome.REMOVED)
        self.assertFalse(services.auto.blocked_by_old_install)
        self.assertTrue(services.auto.running)
        await asyncio.sleep(0.2)
        self.assertIn("trial", statuses)


class NoticeTests(ForgeAsyncTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.paths = AppPaths(self.data_dir)
        self.paths.forge.mkdir(parents=True)
        self.found: OldInstall | None = old()

    def services(self, imported: str = "") -> Any:
        return SimpleNamespace(
            paths=self.paths,
            settings=SimpleNamespace(current=ForgeSettings(imported_from=imported)),
        )

    async def notice(self, services: Any) -> str | None:
        return await startup_notice(services, find=lambda: self.found)

    async def test_once_while_old_data_waits(self) -> None:
        self.assertEqual(await self.notice(self.services()), t(NOTICE_TEXT))
        self.assertTrue((self.paths.forge / NOTICE_MARKER).is_file())
        self.assertIsNone(await self.notice(self.services()))

    async def test_not_when_imported_or_nothing_to_bring(self) -> None:
        self.assertIsNone(await self.notice(self.services("MediaForge 1.3.0, 2026-11-02")))
        self.found = None
        self.assertIsNone(await self.notice(self.services()))
        self.found = old(data=False)
        self.assertIsNone(await self.notice(self.services()))
        self.assertFalse((self.paths.forge / NOTICE_MARKER).exists())

    async def test_a_failed_look_says_nothing(self) -> None:
        def broken() -> OldInstall | None:
            raise OSError("registry")

        with self.assertLogs("app.forge.old_install", "WARNING"):
            self.assertIsNone(await startup_notice(self.services(), find=broken))


IMPORTED = "MediaForge 1.3.0, 2026-11-02"


class FakeForgeSettings:
    def __init__(self, current: ForgeSettings) -> None:
        self.current = current
        self.listeners: list[Any] = []

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def save(self, **changes: Any) -> None:
        self.current = self.current.model_copy(update=changes)
        for listener in list(self.listeners):
            listener(self.current)


class SettingsBoxTests(TkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        guard_uninstaller(self)
        self.page = ctk.CTkFrame(self.root)
        self.page.pack()
        self.foot = ctk.CTkFrame(self.page, fg_color="transparent")
        self.foot.grid()
        self.foot.grid_remove()
        self.entry: OldInstall | None = old(location=str(self.tmp / "MediaForge"))
        script = self.tmp / "MediaForge" / "src" / "mediaforge" / "uninstall.ps1"
        script.parent.mkdir(parents=True)
        script.write_text("# fake\n", encoding="utf-8")
        self.entry = old(f'powershell.exe -File "{script}"', str(self.tmp / "MediaForge"))
        self.release = asyncio.Event()
        self.removes = True
        self.renamer = FakeRenamer()
        self.settings = FakeForgeSettings(ForgeSettings(automatic="trial", imported_from=IMPORTED))
        self.runtime = FakeRuntime(SimpleNamespace(auto=self.renamer, settings=self.settings))
        self.remover = OldInstallRemover(self.runtime, find=self.find, launch=self.launch)

    def find(self) -> OldInstall | None:
        return self.entry

    async def launch(self, command: Sequence[str]) -> int:
        await self.release.wait()
        if self.removes:
            self.entry = None
        return 0

    async def box(self) -> RemoveOldBox:
        box = RemoveOldBox(
            self.foot, cast(Any, self.runtime), remover=self.remover, find_old_install=self.find
        )
        assert box.done is not None
        await box.done
        return box

    async def test_shown_while_the_old_install_is_registered(self) -> None:
        box = await self.box()
        assert box.label is not None and box.button is not None
        self.assertEqual(self.foot.winfo_manager(), "grid")
        self.assertEqual(box.label.cget("text"), t(GUARD_TEXT))
        self.assertEqual(box.label.cget("text_color"), theme.WARNING)  # Trial is held back
        self.assertEqual(box.button.cget("text"), t(REMOVE_OLD))

    async def test_muted_while_off(self) -> None:
        self.settings.current = ForgeSettings(automatic="off", imported_from=IMPORTED)
        box = await self.box()
        assert box.label is not None
        self.assertEqual(box.label.cget("text_color"), theme.TEXT_MUTED)

    async def test_hidden_and_guard_lifted_when_it_is_gone(self) -> None:
        self.entry = old(uninstall="", location="")
        box = await self.box()
        self.assertIsNone(box.row)
        self.assertEqual(self.foot.winfo_manager(), "")
        self.assertFalse(self.renamer.blocked_by_old_install)
        self.assertEqual(self.renamer.reconfigured, 1)

    async def test_remove_then_removed(self) -> None:
        box = await self.box()
        assert box.label is not None and box.button is not None
        box.remove()
        await pump_until(self.root, lambda: box.label is not None and box.label.cget("text") == t(WAITING_TEXT))
        self.assertEqual(box.button.cget("state"), "disabled")
        box.remove()  # a second click does nothing
        self.release.set()
        await pump_until(self.root, lambda: self.remover.last is Outcome.REMOVED)
        await pump(self.root, 0.05)
        self.assertEqual(box.label.cget("text"), t(OUTCOME_TEXTS[Outcome.REMOVED]))
        self.assertEqual(box.label.cget("text_color"), theme.SUCCESS)
        self.assertEqual(box.button.winfo_manager(), "")
        self.assertFalse(self.renamer.blocked_by_old_install)

    async def test_remove_then_kept(self) -> None:
        self.removes = False
        box = await self.box()
        box.remove()
        self.release.set()
        await pump_until(self.root, lambda: self.remover.last is Outcome.KEPT)
        await pump(self.root, 0.05)
        assert box.label is not None and box.button is not None and box.outcome_label is not None
        self.assertEqual(box.label.cget("text"), t(GUARD_TEXT))
        self.assertEqual(box.button.cget("state"), "normal")
        self.assertEqual(box.outcome_label.cget("text"), t(OUTCOME_TEXTS[Outcome.KEPT]))
        self.assertEqual(box.outcome_label.winfo_manager(), "grid")

    async def test_settings_closed_mid_removal_still_lifts_the_guard(self) -> None:
        box = await self.box()
        box.remove()
        await pump(self.root, 0.1)
        self.page.destroy()
        self.release.set()
        await pump_until(self.root, lambda: self.remover.last is Outcome.REMOVED)
        self.assertFalse(self.renamer.blocked_by_old_install)

    async def test_a_reopened_settings_shows_a_removal_under_way(self) -> None:
        first = await self.box()
        first.remove()
        await pump(self.root, 0.1)
        self.page.destroy()
        self.page = ctk.CTkFrame(self.root)
        self.page.pack()
        self.foot = ctk.CTkFrame(self.page, fg_color="transparent")
        box = await self.box()
        assert box.label is not None and box.button is not None
        self.assertEqual(box.label.cget("text"), t(WAITING_TEXT))
        self.assertEqual(box.button.cget("state"), "disabled")
        self.release.set()
        await pump_until(self.root, lambda: self.remover.last is Outcome.REMOVED)

    async def test_no_remove_button_before_the_data_came_over(self) -> None:
        """Removing first could delete data not brought over yet: only the line, no button."""
        self.settings.current = ForgeSettings(automatic="trial")  # nothing imported
        box = await self.box()
        assert box.label is not None and box.button is not None and box.first_label is not None
        self.assertFalse(box.may_remove)
        self.assertEqual(box.label.cget("text"), t(GUARD_TEXT))
        self.assertEqual(box.button.winfo_manager(), "")
        self.assertEqual(box.first_label.winfo_manager(), "grid")
        self.assertEqual(box.first_label.cget("text"), t(BRING_FIRST_TEXT))
        box.remove()  # never starts, even if asked
        await pump(self.root, 0.1)
        self.assertFalse(self.remover.running)
        self.assertIsNone(self.remover.last)

    async def test_an_import_while_settings_shows_offers_the_button(self) -> None:
        self.settings.current = ForgeSettings(automatic="trial")
        box = await self.box()
        assert box.button is not None and box.first_label is not None
        self.settings.save(window_geometry="800x600")  # another change: still no button
        self.assertEqual(box.button.winfo_manager(), "")
        self.settings.save(imported_from=IMPORTED)
        self.assertTrue(box.may_remove)
        self.assertEqual(box.button.winfo_manager(), "grid")
        self.assertEqual(box.first_label.winfo_manager(), "")
        self.assertEqual(self.settings.listeners, [])  # it stopped listening

    async def test_offered_when_the_old_install_has_no_data(self) -> None:
        self.settings.current = ForgeSettings(automatic="trial")  # nothing imported
        assert self.entry is not None
        self.entry = OldInstall(
            db_file=None,
            install_location=self.entry.install_location,
            uninstall_string=self.entry.uninstall_string,
        )
        box = await self.box()
        assert box.button is not None and box.first_label is not None
        self.assertTrue(box.may_remove)
        self.assertEqual(box.button.winfo_manager(), "grid")
        self.assertEqual(box.first_label.winfo_manager(), "")

    async def test_bring_over_sits_above_remove(self) -> None:
        remove = await self.box()
        services = SimpleNamespace(
            paths=None,
            settings=SimpleNamespace(current=ForgeSettings()),
            presets=None,
            history=None,
            matches=None,
            airgrabber_settings=None,
            run_lock=asyncio.Lock(),
        )
        bring = BringOverBox(self.foot, services, find_old_install=self.find)
        assert bring.done is not None
        await bring.done
        self.assertEqual(self.foot.pack_slaves(), [bring.row, remove.row])


class MayCloseTests(HostCase):
    async def test_waits_for_a_confirm_window_that_renames(self) -> None:
        busy = SimpleNamespace(busy=True)
        self.host.confirm_windows.append(cast(Any, busy))
        retried: list[int] = []
        self.assertFalse(self.host.may_close(lambda: retried.append(1)))
        await pump_until(self.root, lambda: retried)
        busy.busy = False
        self.assertTrue(self.host.may_close(lambda: retried.append(1)))
        self.host.confirm_windows.clear()
