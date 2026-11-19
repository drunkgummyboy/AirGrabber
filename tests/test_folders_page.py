"""Settings → Folders, set once (app/ui/folders_page.py; docs/settings/2026-11-19-settings-redesign.md
§4): one list per kind with MediaForge's destination marked in it, "+ Add a folder" (the first of
a kind also the destination), "Sort into this one", removing the destination (cleared, an amber
line), a destination outside its list, the lists read first while they count, the lists usable
while MediaForge isn't ready (the marks wait, or say why), "Finished downloads", and each change
stored at once with Undo. Stand-ins for the folder library and MediaForge's destinations (and once
the real LibraryDestinations over tests/forge/ui's fake runtime); nothing real is read."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.models import Settings
from app.models.folder_library import FolderLibraryStatus
from app.services import SettingsService
from app.ui.settings_commit import Committer, Refused
from app.ui.settings_layout import SettingsPage
from tests.forge.tk import pump_until
from tests.forge.ui.test_settings_library import (
    NOT_READY_TEXT,
    FakeForge,
    FakeRules,
)
from tests.forge.ui.test_settings_library import FakeSettings as FakeForgeSettings
from tests.test_backup_dialog import FakeProwlarr, FakeRepository, FakeShortcut, texts
from tests.test_folder_library_settings import FakeFolders
from tests.test_plex_ui import NOW, TkTestCase

SERIES = r"\\NAS\Video\Series"
OLD_SERIES = r"E:\Old series"
FILMS = r"D:\Films"


class FakeDestinations:
    """LibraryDestinations' face: the folders, started or not, a refusal to give, what was
    stored."""

    def __init__(self, series: str = "", film: str = "", *, ready: bool = True) -> None:
        self.folders = {"series": series, "film": film}
        self._ready = ready
        self.reason = ""
        self.refuse: str | None = None
        self.listeners: list[Any] = []
        self.stored: list[dict[str, str]] = []
        self.starts = 0
        self.gate: asyncio.Event | None = None
        self.closed = False

    @property
    def ready(self) -> bool:
        return self._ready

    def folder(self, kind: str) -> str:
        return self.folders[kind]

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def tell(self) -> None:
        for listener in list(self.listeners):
            listener()

    async def start(self) -> bool:
        self.starts += 1
        if self.gate is not None:
            await self.gate.wait()
        self._ready = True
        self.tell()
        return True

    def close(self) -> None:
        self.closed = True

    def problem(self, folders: Mapping[str, str]) -> str | None:
        return self.refuse

    async def store(self, folders: Mapping[str, str]) -> bool:
        await asyncio.sleep(0)
        if self.refuse is not None:
            raise Refused(self.refuse)
        changes = {k: v for k, v in folders.items() if self.folders[k] != v}
        if not changes:
            return False
        self.stored.append(changes)
        self.folders.update(changes)
        self.tell()
        return True


def service(settings: Settings | None = None) -> SettingsService:
    return SettingsService(FakeRepository(), settings or Settings())  # type: ignore[arg-type]


class FoldersPageCase(TkTestCase):
    async def page(
        self,
        settings: SettingsService,
        *,
        destinations: Any = None,
        folders: Any = None,
        picked: list[str] | None = None,
    ) -> Any:
        from app.ui.folders_page import FoldersPage

        self.frame = ctk.CTkFrame(self.root)
        self.keep(self.frame)
        page = SettingsPage(self.frame, "Folders", "A note")
        page.frame.pack()
        self.committer = Committer()
        answers = list(picked or [])
        folders_page = FoldersPage(
            page,
            settings,
            self.committer,
            folders=folders,
            destinations=destinations,
            pick_folder=lambda _owner: answers.pop(0) if answers else "",
        )
        await folders_page.loaded()
        await self.settle()
        return folders_page

    def row_texts(self, page: Any, kind: str) -> list[list[str]]:
        return [texts(row.frame) for row in page.rows[kind]]

    async def done(self, page: Any, task: Any) -> bool:
        assert task is not None
        stored = bool(await task)
        await page.loaded()
        await self.settle()
        return stored


class FoldersPageTests(FoldersPageCase):
    async def test_one_list_per_kind_with_the_destination_marked(self) -> None:
        from app.ui.folders_page import MARKS, NO_DESTINATION, SORT_INTO_TEXT

        settings = service(Settings(series_folders=(OLD_SERIES, SERIES), film_folders=(FILMS,)))
        page = await self.page(settings, destinations=FakeDestinations(series=SERIES))
        self.assertEqual([row.folder for row in page.rows["series"]], [OLD_SERIES, SERIES])
        self.assertEqual([row.marked for row in page.rows["series"]], [False, True])
        self.assertIn(MARKS["series"], self.row_texts(page, "series")[1])
        sort = page.rows["series"][0].sort
        assert sort is not None
        self.assertEqual(sort.cget("text"), SORT_INTO_TEXT)
        self.assertIsNone(page.rows["series"][1].sort)  # the destination has its mark
        self.assertEqual([row.folder for row in page.rows["film"]], [FILMS])
        note = page.notes["film"]
        assert note is not None
        self.assertEqual(note.cget("text"), NO_DESTINATION["film"])
        self.assertEqual(
            NO_DESTINATION["film"],
            "No Movies folder yet: sorted films stay where they were downloaded.",
        )
        self.assertIsNone(page.notes["series"])
        self.assertEqual(self.committer.can_undo, False)  # opening stores nothing

    async def test_add_first_folder_becomes_the_destination(self) -> None:
        settings = service()
        destinations = FakeDestinations()
        page = await self.page(settings, destinations=destinations, picked=["D:/Films", r"E:\More"])
        self.assertTrue(await self.done(page, page.add("film")))
        self.assertEqual(settings.current.film_folders, (FILMS,))  # Windows separators
        self.assertEqual(destinations.folders["film"], FILMS)
        self.assertEqual([row.marked for row in page.rows["film"]], [True])
        self.assertIsNone(page.notes["film"])
        # The second folder is only listed: the destination stays.
        self.assertTrue(await self.done(page, page.add("film")))
        self.assertEqual(settings.current.film_folders, (FILMS, r"E:\More"))
        self.assertEqual(destinations.stored, [{"film": FILMS}])
        # A folder already listed isn't added twice.
        page._pick_folder = lambda _owner: "d:/films/"
        self.assertIsNone(page.add("film"))

    async def test_undo_of_add_folder(self) -> None:
        settings = service()
        destinations = FakeDestinations()
        page = await self.page(settings, destinations=destinations, picked=[SERIES])
        self.assertTrue(await self.done(page, page.add("series")))
        self.assertTrue(self.committer.can_undo)
        self.assertTrue(await self.committer.run_undo())  # both as they were, in one Undo
        await self.settle()
        self.assertEqual(settings.current.series_folders, ())
        self.assertEqual(destinations.folders["series"], "")
        self.assertEqual(page.rows["series"], [])
        self.assertFalse(self.committer.can_undo)  # an Undo can't be undone

    async def test_sort_into_this_one_moves_the_mark(self) -> None:
        settings = service(Settings(series_folders=(OLD_SERIES, SERIES)))
        destinations = FakeDestinations(series=SERIES)
        page = await self.page(settings, destinations=destinations)
        sort = page.rows["series"][0].sort
        assert sort is not None
        sort.invoke()
        await page.loaded()
        await self.settle()
        self.assertEqual(destinations.stored, [{"series": OLD_SERIES}])
        self.assertEqual(settings.current.series_folders, (OLD_SERIES, SERIES))  # the list stays
        self.assertEqual([row.marked for row in page.rows["series"]], [True, False])
        self.assertTrue(await self.committer.run_undo())
        await self.settle()
        self.assertEqual(destinations.folders["series"], SERIES)
        # A rule on Sorting would be refused: said in red in the box, nothing stored.
        destinations.refuse = "Rule 2: ✕ Can't save: the folder it watches is inside a library folder."
        self.assertFalse(await self.done(page, page.sort_into("series", OLD_SERIES)))
        self.assertEqual(page.boxes["series"].refusal, destinations.refuse)
        self.assertEqual(destinations.folders["series"], SERIES)

    async def test_removing_the_destination_clears_it_and_warns(self) -> None:
        from app.ui.folders_page import NO_DESTINATION

        settings = service(Settings(series_folders=(OLD_SERIES, SERIES)))
        destinations = FakeDestinations(series=SERIES)
        page = await self.page(settings, destinations=destinations)
        remove = page.rows["series"][1].remove
        assert remove is not None
        remove.invoke()
        await page.loaded()
        await self.settle()
        self.assertEqual(settings.current.series_folders, (OLD_SERIES,))
        self.assertEqual(destinations.folders["series"], "")
        note = page.notes["series"]
        assert note is not None
        self.assertEqual(note.cget("text"), NO_DESTINATION["series"])
        self.assertTrue(await self.committer.run_undo())
        await self.settle()
        self.assertEqual(settings.current.series_folders, (OLD_SERIES, SERIES))
        self.assertEqual(destinations.folders["series"], SERIES)
        # Another row's ✕ leaves the destination alone.
        self.assertTrue(await self.done(page, page.remove("series", OLD_SERIES)))
        self.assertEqual(destinations.folders["series"], SERIES)

    async def test_a_destination_outside_its_list_shows_on_its_own(self) -> None:
        settings = service(Settings(series_folders=(OLD_SERIES,)))
        destinations = FakeDestinations(series=SERIES)
        page = await self.page(settings, destinations=destinations)
        self.assertEqual(
            [(row.folder, row.listed, row.marked) for row in page.rows["series"]],
            [(OLD_SERIES, True, False), (SERIES, False, True)],
        )
        self.assertTrue(await self.done(page, page.remove("series", SERIES)))
        self.assertEqual(settings.current.series_folders, (OLD_SERIES,))  # untouched
        self.assertEqual(destinations.folders["series"], "")

    async def test_count_switch_reads_before_turning_on(self) -> None:
        from app.ui.folder_library_settings import FolderLibraryBox, UNREACHABLE_CHANGE_TEXT

        settings = service(Settings(series_folders=(SERIES,)))
        folders = FakeFolders(settings)
        page = await self.page(settings, folders=folders, picked=[FILMS, r"E:\Away"])
        # Off: a list change is stored as it is; nothing is read.
        self.assertTrue(await self.done(page, page.add("film")))
        self.assertEqual(settings.current.film_folders, (FILMS,))
        self.assertEqual(folders.calls, [])
        # The switch reads the page's lists first, then counts.
        frame = ctk.CTkFrame(self.root)
        self.keep(frame)
        count = FolderLibraryBox(SettingsPage(frame, "Folders", ""), folders, settings, self.committer)
        count.switch.toggle()
        await count.loaded()
        await self.settle()
        self.assertEqual(folders.calls, [("on", (FILMS,), (SERIES,))])
        self.assertTrue(settings.current.folder_library_on)
        # On: a change is read first, and refused when it can't be read.
        folders.outcome = "unreachable"
        self.assertFalse(await self.done(page, page.add("film")))
        self.assertEqual(folders.calls[-1], ("on", (FILMS, r"E:\Away"), (SERIES,)))
        self.assertEqual(page.boxes["film"].refusal, UNREACHABLE_CHANGE_TEXT)
        self.assertEqual(settings.current.film_folders, (FILMS,))
        # While they count, the last folder isn't taken out here.
        await settings.update(film_folders=())
        await self.settle()
        self.assertIsNone(page.rows["series"][0].remove)
        self.assertIsNone(page.remove("series", SERIES))

    async def test_no_change_while_reading(self) -> None:
        settings = service(Settings(folder_library_on=True, series_folders=(SERIES,), film_folders=(FILMS,)))
        folders = FakeFolders(settings, FolderLibraryStatus(on=True, films=1, checked_at=NOW))
        folders.gate = asyncio.Event()
        page = await self.page(settings, folders=folders, picked=[OLD_SERIES, r"E:\Other"])
        task = page.add("series")
        await self.settle()
        self.assertTrue(page.busy)
        self.assertEqual(page.add_buttons["film"].cget("state"), "disabled")
        self.assertIsNone(page.add("film"))  # even called: nothing changes
        folders.gate.set()
        self.assertTrue(await self.done(page, task))
        self.assertEqual(settings.current.series_folders, (SERIES, OLD_SERIES))
        self.assertEqual(page.add_buttons["film"].cget("state"), "normal")

    async def test_an_undo_during_switching_off_is_refused(self) -> None:
        # Round 2 A, fix round 1 (review Important 1): a folder added, counting switched off
        # within the bar's time, Undo clicked during the switch-off's clear: counting stays off.
        from app.ui.folder_library_settings import (
            UNDO_WAITS_TEXT,
            FolderLibraryBox,
            FoldersBusy,
        )
        from app.ui.folders_page import FoldersPage

        settings = service(
            Settings(folder_library_on=True, series_folders=(SERIES,), film_folders=(FILMS,))
        )
        gate = asyncio.Event()
        slow = {"off": False}

        class SlowOff(FakeFolders):
            async def switch_off(self) -> None:
                if slow["off"]:
                    await gate.wait()  # the database clear
                await super().switch_off()

        folders = SlowOff(settings, FolderLibraryStatus(on=True, films=1, checked_at=NOW))
        busy = FoldersBusy()
        self.frame = ctk.CTkFrame(self.root)
        self.keep(self.frame)
        self.committer = Committer()
        page = FoldersPage(
            SettingsPage(self.frame, "Folders", ""),
            settings,
            self.committer,
            folders=folders,
            pick_folder=lambda _owner: OLD_SERIES,
            busy=busy,
        )
        count = FolderLibraryBox(
            SettingsPage(self.frame, "Folders", ""), folders, settings, self.committer, busy=busy
        )
        await self.settle()
        self.assertTrue(await self.done(page, page.add("series")))  # its Undo is offered
        self.assertTrue(self.committer.can_undo)
        slow["off"] = True
        switching = count.set_counting(False)
        await self.settle()
        self.assertFalse(await self.committer.run_undo())  # the bar's Undo, clicked now
        self.assertEqual(page.boxes["series"].refusal, UNDO_WAITS_TEXT)
        gate.set()
        assert switching is not None
        self.assertTrue(await switching)
        await page.loaded()
        await count.loaded()
        await self.settle()
        self.assertFalse(settings.current.folder_library_on)  # not switched on again
        self.assertEqual([call[0] for call in folders.calls], ["on", "off"])  # the add's read only
        self.assertEqual(settings.current.series_folders, (SERIES, OLD_SERIES))
        self.assertFalse(busy.busy)
        # Not busy: the switch's own Undo runs, as a change of the folders.
        self.assertTrue(await self.committer.run_undo())
        self.assertTrue(settings.current.folder_library_on)
        self.assertFalse(busy.busy)

    async def test_a_failing_busy_listener_leaves_nothing_busy(self) -> None:
        from app.ui.folder_library_settings import FoldersBusy

        busy = FoldersBusy()
        heard: list[bool] = []

        def broken() -> None:
            raise RuntimeError("gone")

        busy.subscribe(broken)
        busy.subscribe(lambda: heard.append(busy.busy))
        with self.assertLogs("app.ui.folder_library_settings", "ERROR"):
            busy.begin()
        self.assertEqual(heard, [True])  # the others still hear it
        with self.assertLogs("app.ui.folder_library_settings", "ERROR"):
            busy.end()
        self.assertFalse(busy.busy)

    async def test_the_lists_and_the_count_switch_are_busy_together(self) -> None:
        # Round 2, 6: a list edit during switching off would switch counting on again.
        from app.ui.folder_library_settings import FolderLibraryBox, FoldersBusy

        settings = service(
            Settings(folder_library_on=True, series_folders=(SERIES,), film_folders=(FILMS,))
        )
        gate = asyncio.Event()

        class SlowOff(FakeFolders):
            async def switch_off(self) -> None:
                await gate.wait()  # the database clear
                await super().switch_off()

        folders = SlowOff(settings, FolderLibraryStatus(on=True, films=1, checked_at=NOW))
        busy = FoldersBusy()
        from app.ui.folders_page import FoldersPage

        self.frame = ctk.CTkFrame(self.root)
        self.keep(self.frame)
        self.committer = Committer()
        page = FoldersPage(
            SettingsPage(self.frame, "Folders", ""),
            settings,
            self.committer,
            folders=folders,
            pick_folder=lambda _owner: OLD_SERIES,
            busy=busy,
        )
        count = FolderLibraryBox(
            SettingsPage(self.frame, "Folders", ""), folders, settings, self.committer, busy=busy
        )
        await self.settle()
        switching = count.set_counting(False)
        await self.settle()
        self.assertTrue(page.busy)
        self.assertEqual(page.add_buttons["series"].cget("state"), "disabled")
        self.assertIsNone(page.add("series"))  # even called: nothing changes
        self.assertIsNone(page.remove("film", FILMS))
        gate.set()
        assert switching is not None
        self.assertTrue(await switching)
        await page.loaded()
        await self.settle()
        self.assertFalse(settings.current.folder_library_on)
        self.assertEqual(folders.calls, [("off",)])  # never switched on again
        self.assertFalse(page.busy)
        self.assertEqual(page.add_buttons["series"].cget("state"), "normal")
        # And the other way: a list change under way holds the switch.
        folders.gate = asyncio.Event()
        await settings.update(folder_library_on=True)
        folders.set(FolderLibraryStatus(on=True, films=1, checked_at=NOW))
        task = page.add("series")
        await asyncio.sleep(0)  # the change is under way, its read waits
        self.assertTrue(count.busy)
        self.assertIsNone(count.set_counting(False))
        folders.gate.set()
        self.assertTrue(await self.done(page, task))
        await count.loaded()
        self.assertFalse(count.busy)

    async def test_lists_usable_while_forge_is_not_ready(self) -> None:
        from app.ui.folders_page import WAITING_TEXT

        settings = service(Settings(series_folders=(SERIES,)))
        destinations = FakeDestinations(series=SERIES, ready=False)
        destinations.gate = asyncio.Event()
        page = await self.page(settings, destinations=destinations, picked=[FILMS])
        page.page_shown()
        page.page_shown()  # once
        await self.settle()
        self.assertEqual(destinations.starts, 1)
        for kind in ("series", "film"):
            note = page.notes[kind]
            assert note is not None
            self.assertEqual(note.cget("text"), WAITING_TEXT)
        self.assertEqual([(row.marked, row.sort) for row in page.rows["series"]], [(False, None)])
        # The lists work meanwhile; the first film folder isn't made the destination unseen.
        task = page.add("film")
        assert task is not None
        self.assertTrue(await task)  # (`loaded` would wait for MediaForge too)
        await self.settle()
        self.assertEqual(settings.current.film_folders, (FILMS,))
        self.assertEqual(destinations.stored, [])
        destinations.gate.set()
        await page.loaded()
        await self.settle()
        self.assertEqual([row.marked for row in page.rows["series"]], [True])
        self.assertIsNotNone(page.rows["film"][0].sort)  # "Sort into this one" now
        # Forge can't start: why, instead of the marks.
        destinations = FakeDestinations(ready=False)
        destinations.reason = NOT_READY_TEXT
        page = await self.page(service(), destinations=destinations)
        note = page.notes["series"]
        assert note is not None
        self.assertEqual(note.cget("text"), NOT_READY_TEXT)

    async def test_closing_stops_listening(self) -> None:
        settings = service()
        destinations = FakeDestinations()
        folders = FakeFolders(settings)
        await self.page(settings, destinations=destinations, folders=folders)
        self.frame.destroy()
        await self.settle()
        self.assertEqual(destinations.listeners, [])
        self.assertTrue(destinations.closed)
        self.assertEqual(folders.listeners, [])

    # -- carried from the Task 3 review ------------------------------------------------------------

    async def test_a_later_folder_added_while_no_destination_only_joins_the_list(self) -> None:
        # Only a kind's first folder becomes the destination: adding one to count it never
        # changes where sorted files go (the owner chose no Series folder).
        settings = service(Settings(series_folders=(OLD_SERIES,)))
        destinations = FakeDestinations()
        page = await self.page(settings, destinations=destinations, picked=[SERIES])
        self.assertTrue(await self.done(page, page.add("series")))
        self.assertEqual(settings.current.series_folders, (OLD_SERIES, SERIES))
        self.assertEqual(destinations.folders["series"], "")
        self.assertEqual(destinations.stored, [])

    async def test_a_destination_outside_its_list_says_it_isnt_counted(self) -> None:
        from app.ui.folders_page import NOT_COUNTED

        settings = service(Settings(series_folders=(OLD_SERIES,)))
        page = await self.page(settings, destinations=FakeDestinations(series=SERIES))
        listed, alone = self.row_texts(page, "series")
        self.assertNotIn(NOT_COUNTED, listed)
        self.assertIn(NOT_COUNTED, alone)

    async def test_a_failed_destination_keeps_its_own_reason(self) -> None:
        # The list is stored (read while they count), the destination then fails, putting the
        # list back is refused too: the box says why the change failed, not why the put-back did.
        settings = service(
            Settings(series_folders=(OLD_SERIES, SERIES), film_folders=(FILMS,), folder_library_on=True)
        )
        folders = OnceFolders(settings, FolderLibraryStatus(on=True, films=1, shows=1))
        destinations = FailingDestinations(series=SERIES)
        page = await self.page(settings, destinations=destinations, folders=folders)
        with self.assertLogs("app.ui.folders_page", "WARNING") as logs:  # said, not swallowed
            self.assertFalse(await self.done(page, page.remove("series", SERIES)))
        self.assertEqual(
            logs.output, ["WARNING:app.ui.folders_page:Putting the series folder list back failed (Refused)"]
        )  # the kind and the type only: no folder
        self.assertEqual(len(folders.calls), 2)  # the change's read, then the put-back's
        self.assertEqual(page.boxes["series"].refusal, "forge.db couldn't be written")
        self.assertEqual(destinations.folders["series"], SERIES)

    async def test_undo_never_leaves_half_a_change(self) -> None:
        from app.ui.folder_library_settings import UNREACHABLE_CHANGE_TEXT

        settings = service(
            Settings(series_folders=(OLD_SERIES, SERIES), film_folders=(FILMS,), folder_library_on=True)
        )
        folders = FakeFolders(settings, FolderLibraryStatus(on=True, films=1, shows=1))
        destinations = FakeDestinations(series=SERIES)
        page = await self.page(settings, destinations=destinations, folders=folders)
        self.assertTrue(await self.done(page, page.remove("series", SERIES)))
        self.assertEqual(
            (settings.current.series_folders, destinations.folders["series"]), ((OLD_SERIES,), "")
        )
        folders.outcome = "unreachable"  # the list can't go back: the destination stays as left
        self.assertFalse(await self.committer.run_undo())
        await self.settle()
        self.assertEqual(
            (settings.current.series_folders, destinations.folders["series"]), ((OLD_SERIES,), "")
        )
        self.assertEqual(page.boxes["series"].refusal, UNREACHABLE_CHANGE_TEXT)


class OnceFolders(FakeFolders):
    """Reads the folders once; after that none can be reached."""

    async def switch_on(self, films: Any, series: Any) -> Any:
        outcome = await super().switch_on(films, series)
        self.outcome = "unreachable"
        return outcome


class FailingDestinations(FakeDestinations):
    """Stores nothing: forge.db can't be written (its problem() lets the change through)."""

    async def store(self, folders: Mapping[str, str]) -> bool:
        await asyncio.sleep(0)
        raise Refused("forge.db couldn't be written")


class WithMediaForgeTests(FoldersPageCase):
    """The real LibraryDestinations over tests/forge/ui's fake runtime, and the window."""

    def dialog(self, forge: Any, settings: SettingsService) -> Any:
        from app.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.1.0", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            folder_library=FakeFolders(settings),  # type: ignore[arg-type]
            forge=forge,
        )
        self.keep(dialog)
        return dialog

    async def test_destinations_store_through_the_rules(self) -> None:
        from app.forge.ui.settings_pages import LibraryDestinations

        fake = FakeForgeSettings(ForgeSettings(series_folder=SERIES))
        rules = FakeRules(fake, [Rule(folder=r"D:\Downloads", shows=RuleTarget(preset="Series (Folder)"))])
        destinations = LibraryDestinations(FakeForge(fake, rules=rules))
        self.assertTrue(await destinations.start())
        heard: list[int] = []
        destinations.subscribe(lambda: heard.append(1))
        self.assertTrue(await destinations.store({"film": FILMS}))
        self.assertEqual(rules.saves, [(None, {"movies_folder": FILMS})])  # as Sorting stores
        self.assertEqual(destinations.folder("film"), FILMS)
        self.assertTrue(heard)
        self.assertFalse(await destinations.store({"film": FILMS}))  # so already
        heard.clear()
        await fake.update(series_folder=OLD_SERIES)  # stored elsewhere (an import): heard
        self.assertTrue(heard)
        # A destination around a watched folder would make its rule refused.
        problem = destinations.problem({"series": "D:\\"})
        assert problem is not None
        self.assertTrue(problem.startswith("Rule 1: ✕ Can't save"))
        with self.assertRaises(Refused):
            await destinations.store({"series": "D:\\"})
        self.assertEqual(fake.current.series_folder, OLD_SERIES)  # nothing stored
        destinations.close()

    async def test_a_start_ending_without_a_reason_says_it_isnt_available(self) -> None:
        # Final review Minor 4: never "Getting MediaForge ready…" for ever.
        from app.forge.ui.settings_pages import LibraryDestinations

        forge = FakeForge(None)
        quiet_start = forge.start

        async def start_without_reason() -> Any:
            await quiet_start()
            forge.reason = ""  # AirGrabber closing: the start ends with no reason
            return None

        forge.start = start_without_reason  # type: ignore[method-assign]
        destinations = LibraryDestinations(forge)
        self.assertEqual(destinations.reason, "")  # getting ready
        self.assertFalse(await destinations.start())
        self.assertEqual(destinations.reason, "MediaForge isn't available.")
        forge.reason = NOT_READY_TEXT  # a reason is said as it is
        self.assertEqual(destinations.reason, NOT_READY_TEXT)
        destinations.close()

    async def test_the_window_starts_forge_for_the_marks(self) -> None:
        from app.ui.folders_page import WAITING_TEXT
        from app.ui.settings_pages_registry import FOLDERS_SPOT, PAGE_FOLDERS

        forge = FakeForge(FakeForgeSettings(ForgeSettings(series_folder=SERIES)), started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        settings = service(Settings(series_folders=(SERIES,)))
        dialog = self.dialog(forge, settings)
        self.assertEqual(forge.starts, 0)  # not before the page shows
        dialog.go_to_spot(PAGE_FOLDERS, FOLDERS_SPOT)  # MediaForge's "choose folders"
        await self.settle()
        self.assertEqual(dialog.section, PAGE_FOLDERS)
        self.assertEqual(forge.starts, 1)
        page = dialog.folders_page
        self.assertIn(WAITING_TEXT, texts(page.boxes["series"].frame))
        forge.gate.set_result(None)
        await pump_until(self.root, lambda: bool(page.rows["series"]) and page.rows["series"][0].marked)
        self.assertNotIn(WAITING_TEXT, texts(page.boxes["series"].frame))
        self.assertIs(page.spots[FOLDERS_SPOT], page.boxes["series"].frame)
        # MediaForge that can't start says why.
        dialog = self.dialog(FakeForge(None, started=False), service())
        dialog.show_section(PAGE_FOLDERS)
        await pump_until(
            self.root, lambda: NOT_READY_TEXT in texts(dialog.folders_page.boxes["film"].frame)
        )

    async def test_closing_the_window_during_a_change_stores_all_of_it(self) -> None:
        # The window goes while a change's first half (the list) is being stored: nothing
        # raises, and the change still lands whole (the list and the destination), not half.
        from app.ui.settings_pages_registry import PAGE_FOLDERS
        from tests.test_settings_commit import SlowRepository

        repository = SlowRepository()
        settings = SettingsService(repository, Settings())  # type: ignore[arg-type]
        stored = FakeForgeSettings(ForgeSettings(series_folder=""))
        dialog = self.dialog(FakeForge(stored), settings)
        dialog.show_section(PAGE_FOLDERS)
        page = dialog.folders_page
        await pump_until(self.root, lambda: page.destination("series") == "")
        page._pick_folder = lambda _owner: SERIES
        task = page.add("series")  # the first folder: the destination too
        assert task is not None
        await asyncio.sleep(0.05)  # the list's store waits
        self.assertEqual(settings.current.series_folders, ())
        dialog.close()
        self.assertFalse(dialog.winfo_exists())
        repository.go.set()
        self.assertTrue(await task)
        await self.settle()
        self.assertEqual(settings.current.series_folders, (SERIES,))
        self.assertEqual(stored.current.series_folder, SERIES)

    async def test_finished_downloads_folder_commits(self) -> None:
        from app.ui.settings_dialog import FOLDER_HINT, FOLDER_LABEL
        from app.ui.settings_pages_registry import PAGE_FOLDERS

        self.assertEqual(FOLDER_LABEL, "Finished downloads")
        settings = service(Settings(subtitles_folder=r"D:\Downloads\complete"))
        dialog = self.dialog(None, settings)
        dialog.show_section(PAGE_FOLDERS)
        shown = texts(dialog.page(PAGE_FOLDERS))
        order = [shown.index(text) for text in ("Series", "Movies", FOLDER_LABEL, "Count what's in them")]
        self.assertEqual(order, sorted(order))
        self.assertIn(FOLDER_HINT, shown)
        dialog._finished_folder.set(r"E:\Done")
        dialog._finished_field.commit_now()  # Enter, or leaving the field
        await self.settle()
        self.assertEqual(settings.current.subtitles_folder, r"E:\Done")
        self.assertTrue(dialog.committer.can_undo)
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual(settings.current.subtitles_folder, r"D:\Downloads\complete")
