"""Settings › Sorting (app/forge/ui/settings_pages.py) in the Settings window
(app/ui/settings_dialog.py): its part from FORGE_PARTS, opening at the page, the status
box (the renamer's status, live, and the "Watch folders" switch that pauses all rules), the rules list
(look A: each rule's row and notes, ＋ Add a rule, the editor without Delete for AirGrabber's rule
and a new one, a drag, the files no rule fits, a rule's spot opening its editor), the form
(When sorting; the library folders are the Folders page's, tests/test_folders_page.py), each change stored when it is made (through
RuleService's save, as the old Save did; a change that leaves a rule with a refusal is refused in
its box and nothing is stored; Undo stores what was before), waiting for Forge to start and saying
when it can't, and that nothing the Auto-rename page had (it went, the owner's decision) is
lost."""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge.core.auto import Unfit, WatchStatus
from app.forge.core.rules import rules_from_settings
from app.forge.models.ignored import IgnoredPath
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import AirGrabberDownloads, AirGrabberFolder, ForgeSettings
from app.forge.ui.rule_rows import RowLook, row_look
from app.forge.ui.settings_pages import (
    FORGE_PARTS,
    GETTING_READY,
    LIBRARY_POINTER,
    SAVE_FAILED,
    LibrarySection,
    UnfitWindow,
)
from app.i18n import Language
from app.ui import theme
from app.ui.settings_dialog import SettingsDialog
from app.ui.settings_pages_registry import PAGE_FOLDERS, PAGE_NAMING, PAGE_SORTING, PAGES
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until
from tests.forge.ui.settings_kit import airgrabber_settings, forge_settings
from tests.forge.ui.subtitle_kit import FakeSubtitleSource
from tests.test_settings_dialog import find, settle

ROOT = Path(__file__).resolve().parents[3]

READY = ForgeSettings(
    watch_folders=[r"D:\Downloads\tv"],
    series_folder=r"\\nas\video\Series",
    movies_folder=r"\\nas\video\Movies",
)
NOT_READY_TEXT = "MediaForge isn't ready yet (test)."


class FakeSettings:
    """ForgeSettingsService's surface: current, update (validated, listeners told), subscribe."""

    def __init__(self, current: ForgeSettings) -> None:
        self.current = current
        self.listeners: list[Callable[[ForgeSettings], None]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail = False

    async def update(self, **changes: Any) -> ForgeSettings:
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("disk full")
        self.updates.append(changes)
        self.current = ForgeSettings.model_validate({**self.current.model_dump(), **changes})
        for listener in list(self.listeners):
            listener(self.current)
        return self.current

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class FakeRules:
    """RuleService's surface for the section: current (AirGrabber's `on` is the setting's),
    airgrabber_rule, replace_all (recorded; new rules get ids; AirGrabber's switch stored as the
    setting), save (the settings, then replace_all), subscribe."""

    def __init__(self, settings: FakeSettings, rules: Sequence[Rule] | None = None) -> None:
        self.settings = settings
        made = list(rules) if rules is not None else rules_from_settings(settings.current)
        self.stored = [r.model_copy(update={"id": r.id or i}) for i, r in enumerate(made, 1)]
        self.saved: list[list[Rule]] = []
        self.saves: list[tuple[list[Rule] | None, dict[str, Any]]] = []
        self.renamed: list[dict[str, str]] = []
        self.listeners: list[Callable[[tuple[Rule, ...]], None]] = []

    @property
    def current(self) -> tuple[Rule, ...]:
        on = self.settings.current.airgrabber_on
        return tuple(r.model_copy(update={"on": on}) if r.airgrabber else r for r in self.stored)

    @property
    def airgrabber_rule(self) -> Rule:
        return next(r for r in self.current if r.airgrabber)

    async def replace_all(self, rules: Sequence[Rule]) -> tuple[Rule, ...]:
        await asyncio.sleep(0)
        self.saved.append(list(rules))
        next_id = max((r.id for r in self.stored), default=0) + 1
        stored: list[Rule] = []
        for rule in rules:
            if not rule.id:
                rule, next_id = rule.model_copy(update={"id": next_id}), next_id + 1
            stored.append(rule)
        self.stored = stored
        airgrabber = next((r for r in stored if r.airgrabber), None)
        if airgrabber is not None and airgrabber.on != self.settings.current.airgrabber_on:
            await self.settings.update(use_airgrabber_downloads=airgrabber.on)
        self.tell()
        return self.current

    async def save(
        self, rules: Sequence[Rule] | None, changes: dict[str, Any]
    ) -> tuple[Rule, ...]:
        """Settings' Save: the settings changes, then the rules (None: unchanged)."""
        self.saves.append((None if rules is None else list(rules), dict(changes)))
        if changes:
            await self.settings.update(**changes)
        if rules is not None:
            return await self.replace_all(rules)
        return self.current

    def subscribe(self, listener: Callable[[tuple[Rule, ...]], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def presets_renamed(self, renamed: dict[str, str]) -> None:
        """Settings › Naming's Save: the rules follow renamed presets."""
        await asyncio.sleep(0)
        self.renamed.append(dict(renamed))

    def set(self, rules: Sequence[Rule]) -> None:
        """Stored elsewhere meanwhile (an import)."""
        self.stored = list(rules)
        self.tell()

    def tell(self) -> None:
        for listener in list(self.listeners):
            listener(self.current)


class FakeIgnored:
    """IgnoreService's surface for the section: entries, subscribe, stop_ignoring, snapshot."""

    def __init__(self, *entries: IgnoredPath) -> None:
        self.entries: tuple[IgnoredPath, ...] = entries
        self.listeners: list[Callable[[tuple[IgnoredPath, ...]], None]] = []
        self.stopped: list[Path] = []
        self.list = object()  # what `snapshot` hands the live check

    def subscribe(
        self, listener: Callable[[tuple[IgnoredPath, ...]], None]
    ) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def set(self, *entries: IgnoredPath) -> None:
        """Ignored (or stopped) elsewhere meanwhile."""
        self.entries = entries
        for listener in list(self.listeners):
            listener(entries)

    async def stop_ignoring(self, path: Path) -> bool:
        await asyncio.sleep(0)
        self.stopped.append(path)
        self.set(*(e for e in self.entries if e.path != path))
        return True

    async def ignore(self, path: Path, *, folder: bool) -> IgnoredPath:
        await asyncio.sleep(0)
        entry = ignored_entry(str(path), folder=folder)
        self.set(*self.entries, entry)
        return entry

    def snapshot(self) -> object:
        return self.list


class FakeRenamer:
    """AutoRenamer's surface for the section: the status line, and what the last look found
    (`unfit`, `unreachable_folders`)."""

    blocked_by_old_install = False

    def __init__(self, settings: FakeSettings) -> None:
        self.settings = settings
        self.last_status: WatchStatus | None = None
        self.listeners: list[Callable[[WatchStatus], None]] = []
        self.unfit_found: list[Unfit] = []
        self.unreachable: list[str] = []

    def status(self, unreachable: Sequence[Path]) -> WatchStatus:
        mode = self.settings.current.automatic
        if mode == "off":
            return WatchStatus("off", "Not watching")
        return WatchStatus("trial" if mode == "trial" else "watching", f"{mode} status")

    def watch_status(self, listener: Callable[[WatchStatus], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def tell(self, status: WatchStatus) -> None:
        self.last_status = status
        for listener in list(self.listeners):
            listener(status)

    def unfit(self) -> list[Unfit]:
        return list(self.unfit_found)

    def unreachable_folders(self) -> list[str]:
        return list(self.unreachable)


FakeAuto = FakeRenamer  # the brief's name for it


def ignored_entry(path: str, folder: bool = False) -> IgnoredPath:
    return IgnoredPath.model_validate({"path": path, "folder": folder, "added_at": 0})


class FakeForge:
    """ForgeRuntime's surface: `services` (None until started), `start()`, `reason`."""

    def __init__(
        self,
        settings: FakeSettings | None,
        *,
        started: bool = True,
        ignored: FakeIgnored | None = None,
        renamer: FakeRenamer | None = None,
        rules: FakeRules | None = None,
    ) -> None:
        # The other MediaForge sections (Naming, Right-click menu) read the presets and matches.
        self._parts = (
            None
            if settings is None
            else SimpleNamespace(
                settings=settings,
                auto=renamer if renamer is not None else FakeRenamer(settings),
                rules=rules if rules is not None else FakeRules(settings),
                presets=SimpleNamespace(list=mock.AsyncMock(return_value=list(DEFAULT_PRESETS))),
                matches=SimpleNamespace(list=mock.AsyncMock(return_value=[])),
                ignored=ignored if ignored is not None else FakeIgnored(),
                subtitles=FakeSubtitleSource(),
                airgrabber_settings=airgrabber_settings(),
            )
        )
        self.services = self._parts if started else None
        self.reason = ""
        self.gate: asyncio.Future[None] | None = None
        self.starts = 0

    async def start(self) -> Any:
        self.starts += 1
        if self.gate is not None:
            await asyncio.shield(self.gate)  # as ForgeRuntime: a cancelled caller doesn't stop it
        if self._parts is None:
            self.reason = NOT_READY_TEXT
        self.services = self._parts
        return self._parts


def labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel) and child.winfo_manager():
            found.append(str(child.cget("text")))
        if child.winfo_manager():
            found += labels(child)
    return found


class EditorRecorder:
    """Stands in for the rule editor: what it was opened with."""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []

    def __call__(self, master: Any, **options: Any) -> Any:
        self.opened.append(options)
        return SimpleNamespace(closed=True)  # gone at once: the next one may open


class LibrarySettingsCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.chosen: list[str] = []  # what the fake folder picker answers, in turn
        self.asked: list[tuple[str, str]] = []
        self.editor = EditorRecorder()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # the dialog's Tk variables, on the main thread

    def dialog(
        self, forge: FakeForge, *, section: str | None = PAGE_SORTING
    ) -> SettingsDialog:
        return forge_settings(self.root, forge, section=section)

    def section(self, dialog: SettingsDialog) -> LibrarySection:
        part = dialog.parts[PAGE_SORTING]
        assert isinstance(part, LibrarySection)
        part.ask_folder = self._ask
        part.open_editor = self.editor
        return part

    def _ask(self, title: str, start: str) -> str:
        self.asked.append((title, start))
        return self.chosen.pop(0) if self.chosen else ""

    def ready(
        self,
        settings: ForgeSettings = READY,
        *,
        rules: Sequence[Rule] | None = None,
        presets: Any = None,
    ) -> tuple[SettingsDialog, LibrarySection, FakeSettings]:
        """The section on `settings` and `rules`; `presets`: the presets' `list` (an AsyncMock)."""
        fake = FakeSettings(settings)
        forge = FakeForge(fake, rules=FakeRules(fake, rules))
        if presets is not None:
            assert forge.services is not None
            forge.services.presets = SimpleNamespace(list=presets)
        self.forge = forge
        dialog = self.dialog(forge)
        return dialog, self.section(dialog), fake

    async def presets_read(self, part: LibrarySection) -> None:
        """Until the section has read the presets (the editor opens at once from then on)."""
        await pump_until(self.root, lambda: part._presets is not None)

    @property
    def rules(self) -> FakeRules:
        assert self.forge.services is not None
        rules: FakeRules = self.forge.services.rules
        return rules

    @property
    def renamer(self) -> FakeRenamer:
        assert self.forge.services is not None
        renamer: FakeRenamer = self.forge.services.auto
        return renamer


class LibrarySettingsTests(LibrarySettingsCase):
    # --- the sections and this one ---------------------------------------------------------------

    async def test_the_sidebar_lists_forges_pages_and_opens_at_sorting(self) -> None:
        dialog, _part, _fake = self.ready()
        for name in FORGE_PARTS:
            self.assertIn(name, dialog.sections)
        # Your files' label, then an icon and a name per page: Folders, then Sorting.
        shown = labels(dialog._sidebar)
        group = shown.index("YOUR FILES")
        self.assertEqual(shown[group + 2 : group + 5], [PAGE_FOLDERS, "⇅", PAGE_SORTING])
        self.assertEqual(dialog.section, PAGE_SORTING)
        self.assertTrue(dialog._items[PAGE_SORTING].selected)
        texts = labels(dialog.page(PAGE_SORTING))
        self.assertEqual(texts[0], "Sorting")
        self.assertEqual(texts[1], t_en(PAGES[PAGE_SORTING].note))
        dialog.show_section(PAGE_NAMING)
        self.assertEqual(dialog.section, PAGE_NAMING)
        dialog.show_section(PAGE_SORTING)  # an open window asked for the page again
        self.assertEqual(dialog.section, PAGE_SORTING)

    async def test_shows_the_stored_settings_and_rules(self) -> None:
        dialog, part, _fake = self.ready()
        texts = labels(dialog.page(PAGE_SORTING))
        for text in ("tv", "AirGrabber's downloads"):
            self.assertIn(text, texts)
        for text in (READY.series_folder, READY.movies_folder):
            self.assertNotIn(text, texts)  # marked on the Folders page
        self.assertNotIn(t_en(GETTING_READY), texts)
        self.assertEqual(part.watching.get(), 0)  # stored "off"
        self.assertEqual(len(part.rule_rows), 2)
        self.assertEqual(part.rule_drafts, list(self.rules.current))
        self.assertEqual(part.draft()["clean_downloads"], True)
        self.assertFalse(part.rules_changed)

    async def test_nothing_chosen_yet(self) -> None:
        _dialog, part, _fake = self.ready(ForgeSettings())
        airgrabbers = part.rule_rows[-1]
        self.assertIn("→ your Series folder / your Movies folder", labels(airgrabbers.frame))
        self.assertEqual(
            [str(note.cget("text")) for note in airgrabbers.notes],
            [
                "⚠ Choose your Series folder under Folders.",
                "⚠ Choose your Movies folder under Folders.",
            ],
        )

    # --- each change stores itself --------------------------------------------------------------

    async def test_each_change_stores_itself_and_only_itself(self) -> None:
        dialog, part, fake = self.ready()
        find(dialog.page(PAGE_SORTING), part._needs).toggle()  # a tick: at once
        await settle()
        # Through RuleService's save, as the old Save: the settings and the rules in one step.
        self.assertEqual(self.rules.saves, [(None, {"notify_needs": False})])
        self.assertEqual(fake.updates, [{"notify_needs": False}])
        self.assertEqual(fake.current.automatic, "off")  # watching stays paused
        self.assertEqual(self.rules.saved, [])  # the rules weren't touched
        bar = dialog.committer.bar
        assert bar is not None
        self.assertTrue(bar.shown)

    async def test_a_library_folder_marked_on_folders_shows_in_the_notes(self) -> None:
        _dialog, part, fake = self.ready(ForgeSettings())
        notes = [str(n.cget("text")) for n in part.rule_rows[-1].notes]
        self.assertIn("⚠ Choose your Series folder under Folders.", notes)
        await fake.update(series_folder=r"F:\Series")  # the Folders page's "Sort into this one"
        notes = [str(n.cget("text")) for n in part.rule_rows[-1].notes]
        self.assertNotIn("⚠ Choose your Series folder under Folders.", notes)
        self.assertEqual(part.draft().get("series_folder"), None)  # not this page's

    async def test_remove_after_forge_is_off_until_ticked(self) -> None:
        dialog, part, fake = self.ready()
        self.assertFalse(fake.current.remove_after_forge)
        find(dialog.page(PAGE_SORTING), part._remove).toggle()
        await settle()
        self.assertEqual(fake.updates, [{"remove_after_forge": True}])

    async def test_closing_stops_listening(self) -> None:
        dialog, _part, fake = self.ready()
        dialog.destroy()  # ✕ and Escape close the dialog
        await pump(self.root, 0.1)
        self.assertEqual(fake.updates, [])
        self.assertEqual(self.rules.saved, [])
        self.assertEqual(fake.listeners, [])  # it stopped listening
        self.assertEqual(self.rules.listeners, [])
        self.assertEqual(self.renamer.listeners, [])

    async def test_closing_while_a_change_is_stored_is_harmless(self) -> None:
        dialog, part, fake = self.ready()
        task = part.toggle_rule(0, False)
        dialog.destroy()
        await task
        await pump(self.root, 0.05)
        self.assertEqual(len(self.rules.saved), 1)  # it was stored; nothing of the page touched

    async def test_opening_stores_nothing(self) -> None:
        _dialog, _part, fake = self.ready()
        await settle()
        self.assertEqual(fake.updates, [])
        self.assertEqual(self.rules.saved, [])

    async def test_a_failed_store_says_so_in_its_box(self) -> None:
        dialog, part, fake = self.ready()
        fake.fail = True
        with self.assertLogs("app.forge.ui.settings_pages", "ERROR") as logged:
            find(dialog.page(PAGE_SORTING), part._summary).toggle()
            await settle()
        self.assertEqual(part._options_box.refusal, t_en(SAVE_FAILED))
        self.assertEqual(logged.records[0].getMessage().count("disk full"), 0)  # the type only
        self.assertTrue(part._summary.get())  # the tick shows what is stored again
        self.assertTrue(dialog.winfo_exists())

    # --- stored elsewhere meanwhile --------------------------------------------------------------

    async def test_a_change_stored_elsewhere_shows(self) -> None:
        _dialog, part, fake = self.ready()
        await fake.update(automatic="trial", clean_downloads=False, kept_back=["x"])
        self.assertEqual(part.watching.get(), 1)
        self.assertEqual(part.draft()["clean_downloads"], False)
        self.assertEqual(part._clean.get(), False)

    async def test_rules_stored_elsewhere_show(self) -> None:
        _dialog, part, _fake = self.ready()
        imported = [Rule(id=5, folder=r"E:\Imported"), *self.rules.stored[1:]]
        self.rules.set(imported)
        self.assertEqual([r.folder for r in part.rule_drafts], [r"E:\Imported", ""])
        self.assertIn("Imported", labels(part.rules_list))
        await part.toggle_rule(0, False)  # stored at once: the next import shows too
        self.rules.set([Rule(id=6, folder=r"F:\Other"), *self.rules.stored[1:]])
        self.assertEqual(part.rule_drafts[0].folder, r"F:\Other")

    # --- before Forge has started ----------------------------------------------------------------

    async def test_waits_for_forge_to_start(self) -> None:
        fake = FakeSettings(READY)
        forge = FakeForge(fake, started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump(self.root, 0.05)
        self.assertEqual(forge.starts, 1)  # the page showing asks once
        self.assertFalse(part.ready)
        self.assertIn("Getting MediaForge ready…", labels(dialog.page(PAGE_SORTING)))
        forge.gate.set_result(None)
        await pump_until(self.root, lambda: part.ready)
        self.assertNotIn("Getting MediaForge ready…", labels(dialog.page(PAGE_SORTING)))

    async def test_not_ready_says_why(self) -> None:
        forge = FakeForge(None, started=False)  # start() gives no services: NOT_READY
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump_until(self.root, lambda: NOT_READY_TEXT in labels(dialog.page(PAGE_SORTING)))
        self.assertFalse(part.ready)
        self.assertIsNone(part.refusal_of(None))

    async def test_closing_while_waiting_stops_waiting(self) -> None:
        forge = FakeForge(FakeSettings(READY), started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump(self.root, 0.05)
        task = part._task
        assert task is not None
        dialog.destroy()
        await pump(self.root, 0.05)
        self.assertTrue(task.cancelled())
        self.assertFalse(forge.gate.cancelled())  # Forge's own start goes on (it is shielded)

    # --- Dutch, the foot ------------------------------------------------------------------------

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        dialog, part, _fake = self.ready()
        texts = labels(dialog.page(PAGE_SORTING))
        self.assertEqual(texts[0], "Sorteren")
        self.assertIn("Regels", texts)
        self.assertIn("Downloads van AirGrabber", texts)
        self.assertIn("Bij het sorteren", texts)
        self.assertEqual(part.watching.cget("text"), "Mappen bewaken")
        self.assertEqual(part.add_rule_button.cget("text"), "＋ Regel toevoegen")
        self.assertIn("JE BESTANDEN", labels(dialog._sidebar))

    async def test_the_foot_is_hidden_until_used(self) -> None:
        dialog, part, _fake = self.ready()
        self.assertEqual(part.foot.winfo_manager(), "")
        ctk.CTkLabel(part.foot, text="Remove the old MediaForge").pack()
        part.foot.grid()  # F-27 shows it under the boxes
        self.assertEqual(labels(dialog.page(PAGE_SORTING))[-1], "Remove the old MediaForge")


class StatusBoxTests(LibrarySettingsCase):
    """The first box: the live status and the "Watch folders" switch."""

    async def test_it_comes_first_and_the_rest_follows_in_order(self) -> None:
        dialog, part, _fake = self.ready()
        texts = labels(dialog.page(PAGE_SORTING))
        order = [
            texts.index(str(part.status_label.cget("text"))),
            texts.index("Rules"),
            texts.index("When sorting"),
            texts.index("Ignored"),
            texts.index("AirGrabber's downloads", texts.index("Ignored")),  # the box, not the row
        ]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("Library folders", texts)  # gone: the Folders page's lists mark them
        self.assertEqual(part.watching.master, part.status_label.master)
        self.assertEqual(part.watching.cget("text"), "Watch folders")

    async def test_the_status_is_live(self) -> None:
        dialog, part, _fake = self.ready()
        renamer = self.renamer
        self.assertEqual(part.status_label.cget("text"), "Not watching")
        self.assertEqual(tuple(part.light.cget("text_color")), theme.TEXT_MUTED)
        renamer.tell(WatchStatus("unreachable", "Can't reach the NAS"))
        self.assertEqual(part.status_label.cget("text"), "Can't reach the NAS")
        self.assertEqual(tuple(part.light.cget("text_color")), theme.DANGER)
        renamer.tell(WatchStatus("trial", "Manual: watching 1 folder"))
        self.assertEqual(tuple(part.light.cget("text_color")), theme.WARNING)
        renamer.tell(WatchStatus("watching", "Watching 1 folder"))
        self.assertEqual(tuple(part.light.cget("text_color")), theme.SUCCESS)
        dialog.destroy()
        await pump(self.root, 0.05)
        self.assertEqual(renamer.listeners, [])  # it stopped listening

    async def test_it_shows_the_last_status_the_renamer_gave(self) -> None:
        fake = FakeSettings(READY)
        renamer = FakeRenamer(fake)
        renamer.last_status = WatchStatus("unreachable", "Can't reach the NAS")
        dialog = self.dialog(FakeForge(fake, renamer=renamer))
        self.assertEqual(self.section(dialog).status_label.cget("text"), "Can't reach the NAS")

    async def test_the_switch_pauses_and_watches_again_at_once(self) -> None:
        _dialog, part, fake = self.ready()
        part.watching.toggle()  # clicked: from a stored Off, On (each rule's mode decides)
        await settle()
        self.assertEqual(part.draft()["automatic"], "on")
        self.assertEqual(fake.updates, [{"automatic": "on"}])
        self.assertEqual(self.rules.saves, [(None, {"automatic": "on"})])

    async def test_a_stored_watching_value_is_kept(self) -> None:
        _dialog, part, fake = self.ready(READY.model_copy(update={"automatic": "trial"}))
        self.assertEqual(part.watching.get(), 1)
        await part.set_watching(False)
        self.assertEqual(fake.current.automatic, "off")
        self.assertEqual(part.watching.get(), 0)
        await part.set_watching(True)
        self.assertEqual(fake.current.automatic, "trial")  # back as it was, not On


class RulesListTests(LibrarySettingsCase):
    """The rules box (look A): rows, notes, changes kept until Save, the editor's ways in."""

    S = READY
    FILMS_4K = Rule(
        id=1,
        name="4K films",
        folder=r"D:\Downloads",
        take="films",
        quality="4k",
        films=RuleTarget(preset="Movie (Folder)", to="folder", folder=r"\\NAS\Movies 4K"),
        mode="auto",
    )
    COURSES = Rule(
        id=3,
        folder=r"D:\Courses",
        subfolders=False,
        stays=True,
        mode="manual",
        on=False,
        shows=RuleTarget(preset="Series (Folder)"),
        films=RuleTarget(preset="Movie (Folder)"),
    )

    def three(self) -> list[Rule]:
        airgrabber = rules_from_settings(self.S)[-1].model_copy(update={"id": 2, "mode": "auto"})
        return [self.FILMS_4K, airgrabber, self.COURSES]

    async def test_rows_read_as_look_a(self) -> None:
        rules = self.three()
        ag, courses = rules[1], rules[2]
        self.assertEqual(
            row_look(rules[0], self.S),
            RowLook(
                False, "Downloads", ("Films", "4K"), "Movie (Folder)", r"\\NAS\Movies 4K",
                "Automatic", "4K films", False,
            ),
        )
        self.assertEqual(
            row_look(ag, self.S).destination, r"\\nas\video\Series / \\nas\video\Movies"
        )
        self.assertTrue(row_look(ag, self.S).locked)
        self.assertEqual(row_look(courses, self.S).chips, ("Anything", "Top folder only"))
        self.assertEqual(row_look(courses, self.S).destination, "sorted in this folder")
        # The widgets.
        _dialog, part, _fake = self.ready(rules=rules)
        first, second, third = part.rule_rows
        self.assertEqual(first.grip.cget("text"), "⠿")
        self.assertEqual(second.grip.cget("text"), "🔒")
        self.assertEqual(
            [str(label.cget("text")) for label in first.labels],
            ["Downloads", "Films", "4K", "Movie (Folder)", r"→ \\NAS\Movies 4K", "Automatic"],
        )
        self.assertEqual(tuple(first.labels[-1].cget("text_color")), theme.SUCCESS)
        self.assertEqual(first.switch.get(), 1)
        self.assertEqual(third.switch.get(), 0)  # off: greyed
        self.assertEqual(tuple(third.labels[0].cget("text_color")), theme.TEXT_MUTED)
        self.assertEqual(first.edit.cget("text"), "✎")
        self.assertEqual(tuple(first.edit.cget("text_color")), theme.BUTTON_TEXT)
        manual = third.labels[-1]
        self.assertEqual(manual.cget("text"), "Manual")

    async def test_a_manual_rule_shows_its_pill_in_amber(self) -> None:
        manual = self.FILMS_4K.model_copy(update={"mode": "manual"})
        _dialog, part, _fake = self.ready(rules=[manual, self.three()[1]])
        self.assertEqual(tuple(part.rule_rows[0].labels[-1].cget("text_color")), theme.WARNING)

    async def test_each_rule_change_stores_itself(self) -> None:
        stored = READY.model_copy(update={"automatic": "trial"})
        _dialog, part, fake = self.ready(stored, rules=self.three())
        await part.toggle_rule(2, True)
        self.assertEqual([r.on for r in self.rules.saved[-1]], [True, True, True])
        await part.move_rule(2, 0)
        self.assertEqual([r.id for r in self.rules.saved[-1]], [3, 1, 2])
        await part.set_watching(False)
        self.assertEqual(fake.updates, [{"automatic": "off"}])
        self.assertFalse(part.rules_changed)  # what is stored is what the page shows
        self.assertEqual(part.rule_drafts, list(self.rules.current))
        self.assertEqual([r.id for r in part.rule_drafts], [3, 1, 2])
        self.assertTrue(part.rule_drafts[0].on)

    async def test_undo_stores_the_rules_from_before(self) -> None:
        dialog, part, _fake = self.ready(rules=self.three())
        await part.move_rule(2, 0)
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual([r.id for r in self.rules.saves[-1][0] or []], [1, 2, 3])
        self.assertEqual([r.id for r in part.rule_drafts], [1, 2, 3])

    async def test_a_dragged_row_moves(self) -> None:
        _dialog, part, _fake = self.ready(rules=self.three())
        part._drag_start(2, SimpleNamespace())  # type: ignore[arg-type]
        part._drag_motion(SimpleNamespace(y_root=-1000))  # type: ignore[arg-type]
        part._drag_end(SimpleNamespace(y_root=-1000))  # type: ignore[arg-type]  # above the first
        await settle()
        self.assertEqual([r.id for r in part.rule_drafts], [3, 1, 2])
        self.assertEqual([r.id for r in self.rules.saved[-1]], [3, 1, 2])

    async def test_a_refused_change_is_said_in_the_rules_box_and_stores_nothing(self) -> None:
        inside = Rule(
            id=0,
            folder=r"E:\Films\in",
            take="films",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=r"E:\Films"),
        )
        _dialog, part, fake = self.ready(rules=[self.three()[1]])
        self.assertFalse(await part.rule_saved(1, inside))  # as the editor's Save (new, last)
        self.assertEqual(self.rules.saved, [])
        self.assertEqual(fake.updates, [])
        self.assertTrue(part._rules_box.refusal.startswith("Rule 2: ✕ Can't save: "))
        self.assertEqual(len(part.rule_drafts), 1)  # the list shows what is stored again
        # The editor's own Save hears it and stays open.
        refused = part._edited(1, inside, True, inside)
        assert refused is not None
        self.assertTrue(refused.startswith("Rule 2: ✕ Can't save: "))
        await settle()
        self.assertEqual(self.rules.saved, [])

    async def test_airgrabbers_rule_has_no_delete_and_a_lock(self) -> None:
        _dialog, part, _fake = self.ready(rules=self.three())
        await self.presets_read(part)
        part.edit_rule(1)
        part.edit_rule(0)
        part.add_rule()
        ag, own, new = self.editor.opened
        self.assertTrue(ag["rule"].airgrabber)
        self.assertIsNone(ag["on_delete"])
        self.assertIsNotNone(own["on_delete"])
        self.assertIsNone(new["on_delete"])
        self.assertEqual(new["index"], 3)
        self.assertEqual(new["rule"].shows.preset, READY.series_preset)
        self.assertEqual(new["rule"].films.preset, READY.movies_preset)
        self.assertEqual(own["rules"], part.rule_drafts)
        await part.delete_rule(1)  # never AirGrabber's
        self.assertEqual(len(part.rule_drafts), 3)
        self.assertEqual(self.rules.saved, [])
        own["on_delete"]()
        await settle()
        self.assertEqual([r.id for r in part.rule_drafts], [2, 3])
        self.assertIsNone(new["on_save"](Rule(folder=r"F:\New")))  # taken: the editor closes
        await settle()
        self.assertEqual(part.rule_drafts[-1].folder, r"F:\New")
        self.assertEqual(len(self.rules.saved), 2)  # each stored as it was made

    async def test_the_editors_check_reads_without_the_ignored(self) -> None:
        ignored = FakeIgnored()
        fake = FakeSettings(READY)
        self.forge = FakeForge(fake, ignored=ignored)
        dialog = self.dialog(self.forge)
        part = self.section(dialog)
        await self.presets_read(part)
        part.edit_rule(0)
        peek = self.editor.opened[0]["peek"]
        with mock.patch("app.forge.core.rule_check.peek", mock.AsyncMock(return_value=[])) as read:
            self.assertEqual(await peek(Path(r"D:\Downloads\tv")), [])
        read.assert_awaited_once_with(Path(r"D:\Downloads\tv"), ignored.list)

    async def test_a_suggestion_adds_a_rule_with_airgrabbers_routing(self) -> None:
        suggestion = AirGrabberFolder(
            path=r"\\nas\video\tv", why="shows", program="Download Station", suggested=True
        )
        stored = READY.model_copy(
            update={"airgrabber": AirGrabberDownloads(folders=[suggestion])}
        )
        airgrabber = rules_from_settings(stored)[-1].model_copy(
            update={"id": 2, "mode": "auto", "take": "shows"}
        )
        _dialog, part, _fake = self.ready(stored, rules=[airgrabber])
        buttons = [b for b in part.airgrabber_list.winfo_children() if isinstance(b, ctk.CTkButton)]
        self.assertEqual([b.cget("text") for b in buttons], ["Add a rule for this folder"])
        task = part.add_rule_for(r"\\nas\video\tv")
        assert task is not None
        await task
        added = self.rules.saved[-1][-1]  # stored as a new rule: its first look waits
        self.assertEqual(added.folder, r"\\nas\video\tv")
        self.assertFalse(added.airgrabber)
        self.assertEqual(added.id, 0)
        self.assertEqual(part.rule_drafts[-1].folder, r"\\nas\video\tv")
        self.assertEqual(
            (added.shows, added.films, added.mode, added.take),
            (airgrabber.shows, airgrabber.films, "auto", "shows"),
        )
        self.assertIsNone(part.add_rule_for(r"\\NAS\video\tv"))  # a rule watches it now
        self.assertEqual(len(part.rule_drafts), 2)
        buttons = [b for b in part.airgrabber_list.winfo_children() if isinstance(b, ctk.CTkButton)]
        self.assertEqual(buttons, [])

    async def test_fit_no_rule_line_and_show_them(self) -> None:
        _dialog, part, _fake = self.ready()
        self.assertEqual(part.unfit_label.master.winfo_manager(), "")  # nothing: no line
        downloads = Path(r"D:\Downloads")
        self.renamer.unfit_found = [
            Unfit(downloads, (downloads / "Holiday.Clip.mp4", downloads / "Lecture 3.mkv"))
        ]
        self.renamer.tell(WatchStatus("watching", "Watching 1 folder"))
        self.assertEqual(
            part.unfit_label.cget("text"), "2 files in Downloads fit no rule and are left alone."
        )
        self.assertEqual(part.unfit_label.master.winfo_manager(), "grid")
        self.assertEqual(part.show_them.cget("text"), "Show them")
        part.show_them.invoke()
        window = part.unfit_window
        assert isinstance(window, UnfitWindow)
        self.assertEqual(window.title(), "Files no rule fits")
        self.assertEqual(labels(window.names), ["Holiday.Clip.mp4", "Lecture 3.mkv"])
        self.assertEqual(window.close_button.cget("text"), "Close")
        window.close_button.invoke()
        self.assertTrue(window.closed)

    async def test_unreachable_destination_warns_on_its_row(self) -> None:
        _dialog, part, _fake = self.ready(rules=self.three())
        self.assertEqual(part.rule_rows[0].notes, [])
        self.renamer.unreachable = [r"\\NAS\Movies 4K"]
        self.renamer.tell(WatchStatus("unreachable", r"Can't reach \\NAS\Movies 4K"))
        note = part.rule_rows[0].notes[0]
        self.assertEqual(
            note.cget("text"),
            r"⚠ Can't reach \\NAS\Movies 4K right now. Films wait in the queue until it's back.",
        )
        self.assertEqual(tuple(note.cget("text_color")), theme.WARNING)

    async def test_a_rule_spot_opens_its_editor(self) -> None:
        seven = self.FILMS_4K.model_copy(update={"id": 7})
        dialog, part, _fake = self.ready(rules=[seven, self.three()[1]])
        await self.presets_read(part)
        self.assertIs(part.spots["rule:7"], part.rule_rows[0].frame)
        part.spot_shown("rule:7")
        self.assertEqual(self.editor.opened[-1]["rule"], seven)
        part.spot_shown("library-folders")  # not a rule's: nothing opens
        self.assertEqual(len(self.editor.opened), 1)
        dialog.go_to_spot(PAGE_SORTING, "rule:7")  # Settings scrolls there, then opens it
        await pump_until(self.root, lambda: len(self.editor.opened) == 2)
        self.assertEqual(self.editor.opened[-1]["rule"], seven)


class EditorPresetsTests(LibrarySettingsCase):
    """The rule editor opens only with the presets read: never with an empty list."""

    MINE = [*DEFAULT_PRESETS, DEFAULT_PRESETS[0].model_copy(update={"name": "Mine"})]

    async def test_the_editor_gets_the_stored_presets(self) -> None:
        _dialog, part, _fake = self.ready(presets=mock.AsyncMock(return_value=list(self.MINE)))
        await self.presets_read(part)
        part.edit_rule(0)
        part.add_rule()
        self.assertEqual([o["presets"] for o in self.editor.opened], [self.MINE, self.MINE])

    async def test_it_waits_for_a_read_under_way(self) -> None:
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def slow() -> list[Any]:
            await gate
            return list(self.MINE)

        reads = mock.AsyncMock(side_effect=slow)
        _dialog, part, _fake = self.ready(presets=reads)
        part.edit_rule(0)  # the read hasn't ended: nothing opens yet
        part.spot_shown("rule:1")  # the queue's label meanwhile: still one editor to come
        await pump(self.root, 0.05)
        self.assertEqual(self.editor.opened, [])
        gate.set_result(None)
        await pump_until(self.root, lambda: self.editor.opened)
        await pump(self.root, 0.05)
        self.assertEqual(len(self.editor.opened), 1)
        self.assertEqual(self.editor.opened[0]["presets"], self.MINE)
        self.assertEqual(self.editor.opened[0]["index"], 0)
        self.assertEqual(reads.await_count, 1)  # waited for, not read twice

    async def test_a_new_rule_is_new_from_the_click_whatever_happens_meanwhile(self) -> None:
        # Decided when ＋ Add a rule is clicked: rules arriving during the presets' read (an
        # import) never turn its Save into an edit of a rule that isn't there (dropped).
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def slow() -> list[Any]:
            await gate
            return list(self.MINE)

        _dialog, part, _fake = self.ready(presets=mock.AsyncMock(side_effect=slow))
        part.add_rule()
        await pump(self.root, 0.05)
        self.rules.set([Rule(id=8, folder=r"E:\Imported"), *self.rules.stored])  # longer now
        gate.set_result(None)
        await pump_until(self.root, lambda: self.editor.opened)
        self.editor.opened[-1]["on_save"](Rule(folder=r"E:\Mine"))
        await settle()
        self.assertEqual(part.rule_drafts[-1].folder, r"E:\Mine")

    async def test_a_failed_read_is_tried_again_and_else_says_so(self) -> None:
        reads = mock.AsyncMock(side_effect=OSError("disk gone"))
        with self.assertLogs("app.forge.ui.settings_pages", "WARNING"):
            _dialog, part, _fake = self.ready(presets=reads)
            await pump_until(self.root, lambda: reads.await_count == 1)
            part.add_rule()
            await pump_until(self.root, lambda: part.presets_problem.winfo_manager())
        self.assertEqual(reads.await_count, 2)  # tried again before giving up
        self.assertEqual(self.editor.opened, [])
        self.assertEqual(
            part.presets_problem.cget("text"), "Couldn't read your presets. Try again in a moment."
        )
        self.assertEqual(tuple(part.presets_problem.cget("text_color")), theme.WARNING)
        reads.side_effect = None
        reads.return_value = list(self.MINE)
        part.add_rule()  # a moment later: read now, and it opens
        await pump_until(self.root, lambda: self.editor.opened)
        self.assertEqual(self.editor.opened[0]["presets"], self.MINE)
        self.assertEqual(part.presets_problem.winfo_manager(), "")

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        reads = mock.AsyncMock(side_effect=OSError("disk gone"))
        with self.assertLogs("app.forge.ui.settings_pages", "WARNING"):
            _dialog, part, _fake = self.ready(presets=reads)
            part.edit_rule(0)
            await pump_until(self.root, lambda: part.presets_problem.winfo_manager())
        self.assertEqual(
            part.presets_problem.cget("text"),
            "Je voorinstellingen konden niet worden gelezen. Probeer het zo nog eens.",
        )


class EditorPlaceTests(LibrarySettingsCase):
    """The editor's Save and Delete reach the rule it opened, even if the list changed meanwhile."""

    async def test_save_and_delete_follow_the_rule_by_id(self) -> None:
        one, airgrabber = Rule(id=1, folder=r"E:\One"), rules_from_settings(READY)[-1]
        two = Rule(id=2, folder=r"E:\Two")
        _dialog, part, _fake = self.ready(rules=[one, two, airgrabber.model_copy(update={"id": 3})])
        await self.presets_read(part)
        part.edit_rule(1)  # "Two"
        opened = self.editor.opened[-1]
        self.rules.set([self.rules.stored[1], self.rules.stored[2], self.rules.stored[0]])  # moved
        self.assertEqual([r.id for r in part.rule_drafts], [2, 3, 1])
        opened["on_save"](two.model_copy(update={"name": "Twee"}))
        await settle()
        self.assertEqual([r.name for r in part.rule_drafts], ["Twee", "", ""])
        opened["on_delete"]()
        await settle()
        self.assertEqual([r.id for r in part.rule_drafts], [3, 1])

    async def test_a_rule_gone_meanwhile_is_left_alone(self) -> None:
        _dialog, part, _fake = self.ready()
        await self.presets_read(part)
        part.edit_rule(0)
        opened = self.editor.opened[-1]
        self.rules.set(self.rules.stored[1:])  # removed elsewhere (an import)
        before = list(part.rule_drafts)
        opened["on_save"](Rule(id=1, folder=r"E:\Back"))
        opened["on_delete"]()
        await settle()
        self.assertEqual(part.rule_drafts, before)
        self.assertEqual(self.rules.saved, [])


class NothingLostTests(LibrarySettingsCase):
    """Everything the old Auto-rename page offered has its place in Settings."""

    def test_the_page_and_the_window_are_gone(self) -> None:
        for name in ("library_page", "window", "taskbar"):
            self.assertIsNone(importlib.util.find_spec(f"app.forge.ui.{name}"), name)

    async def test_what_it_does_is_the_pages_note(self) -> None:
        dialog, _part, _fake = self.ready()
        self.assertIn("renamed and moved into your folders", labels(dialog.page(PAGE_SORTING))[1])

    async def test_the_folders_and_their_buttons(self) -> None:
        dialog, part, _fake = self.ready()
        dialog.show_section(PAGE_FOLDERS)  # the library folders, marked in its lists
        folders = dialog.folders_page
        assert folders is not None
        await pump_until(self.root, lambda: folders.destination("series") == READY.series_folder)
        texts = labels(dialog.page(PAGE_FOLDERS))
        for folder in (READY.series_folder, READY.movies_folder):
            self.assertIn(folder, texts)
        self.assertEqual(part.add_rule_button.cget("text"), "＋ Add a rule")

    async def test_new_rules_start_with_namings_presets_and_it_says_so(self) -> None:
        dialog, part, _fake = self.ready()
        self.assertIn(t_en(LIBRARY_POINTER), labels(part._rules_box.frame))
        dialog.show_section(PAGE_NAMING)
        naming = dialog.parts[PAGE_NAMING]
        await pump_until(self.root, lambda: getattr(naming, "ready", False))
        draft = naming.draft()  # type: ignore[attr-defined]
        self.assertEqual(draft["series_preset"], READY.series_preset)
        self.assertEqual(draft["movies_preset"], READY.movies_preset)


class IgnoredBoxTests(LibrarySettingsCase):
    """The "Ignored" box under the rules: what Sorting leaves alone, each with "Stop ignoring"
    (at once; Undo ignores it again); "Nothing ignored." without a gap when empty."""

    COURSE = r"D:\Downloads\tv\Python Course"
    CLIP = r"D:\Downloads\tv\Holiday.Clip.mp4"
    CLIP_NAME = "Holiday.Clip.mp4"

    def ignored_ready(
        self, ignored: FakeIgnored
    ) -> tuple[SettingsDialog, LibrarySection, FakeSettings]:
        fake = FakeSettings(READY)
        self.forge = FakeForge(fake, ignored=ignored)
        dialog = self.dialog(self.forge)
        return dialog, self.section(dialog), fake

    async def test_empty_says_nothing_ignored_without_a_gap(self) -> None:
        _dialog, part, _fake = self.ignored_ready(FakeIgnored())
        self.assertEqual(labels(part.ignored_list), ["Nothing ignored."])
        box = part.ignored_list.master
        box.update_idletasks()
        scaling = ctk.ScalingTracker.get_widget_scaling(box)
        # The heading, its line and "Nothing ignored." only (an empty CTkFrame would keep 200).
        self.assertLess(box.winfo_reqheight(), 140 * scaling)

    async def test_the_box_follows_the_rules(self) -> None:
        dialog, _part, _fake = self.ignored_ready(FakeIgnored())
        texts = labels(dialog.page(PAGE_SORTING))
        rules = texts.index("Rules")
        ignored = texts.index("Ignored")
        self.assertLess(rules, ignored)
        self.assertLess(ignored, texts.index("AirGrabber's downloads", ignored))  # the box
        self.assertEqual(texts[ignored + 1], "Files and folders Sorting leaves alone.")

    async def test_lists_entries_and_stop_ignoring_stores_at_once(self) -> None:
        source = FakeIgnored(ignored_entry(self.COURSE, folder=True), ignored_entry(self.CLIP))
        dialog, part, fake = self.ignored_ready(source)
        self.assertEqual(labels(part.ignored_list), ["📁  Python Course", self.CLIP_NAME])
        buttons = [w for w in part.ignored_list.winfo_children() if isinstance(w, ctk.CTkButton)]
        self.assertEqual([b.cget("text") for b in buttons], ["Stop ignoring", "Stop ignoring"])
        buttons[0].invoke()
        self.assertEqual(labels(part.ignored_list), [self.CLIP_NAME])  # gone at once
        await settle()
        self.assertEqual(source.stopped, [Path(self.COURSE)])
        self.assertEqual(fake.updates, [])  # no setting changed
        self.assertEqual(part.unignored, [])
        self.assertEqual(labels(part.ignored_list), [self.CLIP_NAME])
        self.assertTrue(await dialog.committer.run_undo())  # ignored again
        self.assertEqual(
            sorted(labels(part.ignored_list)), sorted(["📁  Python Course", self.CLIP_NAME])
        )

    async def test_the_whole_path_is_the_tooltip(self) -> None:
        source = FakeIgnored(ignored_entry(self.COURSE, folder=True), ignored_entry(self.CLIP))
        with mock.patch("app.forge.ui.settings_pages.add_tooltip") as tip:
            _dialog, part, _fake = self.ignored_ready(source)
        ignored_tips = [
            call.args[1]
            for call in tip.call_args_list
            if call.args[0].master is part.ignored_list
        ]
        self.assertEqual(ignored_tips, [self.COURSE, self.CLIP])

    async def test_closing_stops_listening(self) -> None:
        source = FakeIgnored(ignored_entry(self.CLIP))
        dialog, part, _fake = self.ignored_ready(source)
        task = part.stop_ignoring(Path(self.CLIP))
        self.assertEqual(labels(part.ignored_list), ["Nothing ignored."])
        dialog.destroy()
        assert task is not None
        await task  # stored, nothing of the closed page touched
        await pump(self.root, 0.05)
        self.assertEqual(source.stopped, [Path(self.CLIP)])
        self.assertEqual(source.listeners, [])  # it stopped listening

    async def test_ignored_meanwhile_shows(self) -> None:
        source = FakeIgnored()
        _dialog, part, _fake = self.ignored_ready(source)
        source.set(ignored_entry(self.CLIP))
        self.assertEqual(labels(part.ignored_list), [self.CLIP_NAME])

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        dialog, part, _fake = self.ignored_ready(FakeIgnored(ignored_entry(self.CLIP)))
        texts = labels(dialog.page(PAGE_SORTING))
        self.assertIn("Genegeerd", texts)
        self.assertIn("Bestanden en mappen die Sorteren met rust laat.", texts)
        buttons = [w for w in part.ignored_list.winfo_children() if isinstance(w, ctk.CTkButton)]
        self.assertEqual(buttons[0].cget("text"), "Niet meer negeren")
        part.stop_ignoring(Path(self.CLIP))
        self.assertEqual(labels(part.ignored_list), ["Niets genegeerd."])


class ImportCostTests(ForgeAsyncTestCase):
    def test_airgrabbers_settings_load_none_of_forges_settings(self) -> None:
        """Settings is imported with the main window at start: its MediaForge pages are
        imported when it opens, so none of Forge's settings pages, core, forge.db or guessit come
        along (INTEGRATION.md 3.6)."""
        code = (
            "import sys\n"
            "import app.ui.settings_dialog\n"
            "bad = [m for m in sys.modules if m == 'guessit' or m.startswith('app.forge.db')\n"
            "       or m.startswith('app.forge.ui.settings')\n"
            "       or m in ('app.forge.core.auto', 'app.forge.services')]\n"
            "print(bad)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")

    def test_the_section_loads_neither_the_renamer_nor_the_editor(self) -> None:
        """Importing the Settings pages leaves the renamer and the rule editor unloaded (the
        editor is imported when it opens)."""
        code = (
            "import sys\n"
            "import app.forge.ui.settings_pages\n"
            "bad = [m for m in sys.modules if m == 'guessit' or m.startswith('app.forge.db')\n"
            "       or m in ('app.forge.core.auto', 'app.forge.ui.rule_editor',\n"
            "                'app.forge.core.rule_check', 'app.forge.services')]\n"
            "print(bad)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")


def t_en(text: str) -> str:
    return i18n.t(text)
