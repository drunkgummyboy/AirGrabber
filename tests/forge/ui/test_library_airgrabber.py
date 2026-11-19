"""AirGrabber's downloads in Settings › Sorting (B-01): AirGrabber's rule's switch is "Use
AirGrabber's downloads" (on for a new setup, off for an imported one, stored on Save only), the box
lists AirGrabber's folders and why each is (not) watched, or what to choose, and a suggestion's
"Add a rule for this folder" adds a rule with AirGrabber's rule's routing."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from typing import Any

import customtkinter as ctk

from app import i18n
from app.forge.core.rules import watched_folders
from app.forge.models.settings import AirGrabberDownloads, AirGrabberFolder, ForgeSettings
from app.forge.ui.settings_pages import LibrarySection
from app.ui.settings_pages_registry import PAGE_SORTING
from app.i18n import Language
from app.ui.settings_dialog import SettingsDialog
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.ui.settings_kit import forge_settings
from tests.test_settings_dialog import settle
from tests.forge.ui.test_settings_library import FakeForge, FakeRules, FakeSettings, labels

SERIES = r"\\nas\video\Series"
MOVIES = r"\\nas\video\Movies"
DONE = AirGrabberFolder(path=r"D:\Torrents", why="qbittorrent", program="qBittorrent")
SUGGESTED = AirGrabberFolder(
    path=r"\\192.168.1.20\video\tv",
    why="shows",
    program="Download Station",
    suggested=True,
    reachable=True,
)
IMPORTED = "MediaForge 1.3.0, 2026-11-02"


def with_airgrabber(
    *folders: AirGrabberFolder, ask: str = "", **changes: Any
) -> ForgeSettings:
    values: dict[str, Any] = {
        "series_folder": SERIES,
        "movies_folder": MOVIES,
        "airgrabber": {"folders": [f.model_dump() for f in folders], "ask": ask},
    }
    values.update(changes)
    return ForgeSettings.model_validate(values)


class _TkCase(ForgeAsyncTestCase):
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

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()


class SettingsSectionTests(_TkCase):
    def ready(
        self, settings: ForgeSettings
    ) -> tuple[SettingsDialog, LibrarySection, FakeSettings]:
        fake = FakeSettings(settings)
        self.rules = FakeRules(fake)
        dialog = forge_settings(
            self.root, FakeForge(fake, rules=self.rules), section=PAGE_SORTING
        )
        part = dialog.parts[PAGE_SORTING]
        assert isinstance(part, LibrarySection)
        return dialog, part, fake

    @staticmethod
    def airgrabbers(part: LibrarySection) -> int:
        """AirGrabber's rule's place in the list."""
        return next(i for i, rule in enumerate(part.rule_drafts) if rule.airgrabber)

    async def test_lists_airgrabbers_folders_and_why(self) -> None:
        _dialog, part, _fake = self.ready(
            with_airgrabber(DONE, SUGGESTED, watch_folders=[r"E:\Mine"])
        )
        self.assertEqual(
            labels(part.airgrabber_list),
            [
                DONE.path,
                "qBittorrent on this PC saves AirGrabber's downloads here",
                SUGGESTED.path,
                "Suggested: Download Station saves AirGrabber's downloads here. This PC can "
                "reach it.",
            ],
        )
        self.assertIn("Mine", labels(part.rules_list))  # the owner's own folder: a rule
        self.assertIn(
            "The folders where AirGrabber's downloads end up, as AirGrabber's settings say. "
            "AirGrabber's rule watches them; a suggested folder is only watched once you add a "
            "rule for it.",
            labels(part.airgrabber_list.master),
        )

    async def test_says_what_to_choose(self) -> None:
        _dialog, part, _fake = self.ready(with_airgrabber(ask="finished"))
        self.assertEqual(
            labels(part.airgrabber_list)[0],
            "AirGrabber doesn't know where its finished downloads end up on this PC. Fill in "
            "“Finished downloads” in Settings › Folders, or add a rule for the folder.",
        )

    async def test_nothing_of_airgrabbers_while_its_rule_is_off(self) -> None:
        _dialog, part, _fake = self.ready(
            with_airgrabber(DONE, ask="finished", use_airgrabber_downloads=False)
        )
        self.assertNotIn("AirGrabber doesn't know", "\n".join(labels(part.airgrabber_list)))
        self.assertEqual(part.rule_rows[self.airgrabbers(part)].switch.get(), 0)

    async def test_on_for_a_new_setup_and_nothing_saved_untouched(self) -> None:
        dialog, part, fake = self.ready(with_airgrabber(DONE, SUGGESTED))
        self.assertEqual(part.rule_rows[self.airgrabbers(part)].switch.get(), 1)
        shown = labels(part.airgrabber_list)
        self.assertEqual(shown[:2], [DONE.path, "qBittorrent on this PC saves AirGrabber's downloads here"])
        await settle()  # opening stores nothing
        self.assertEqual(fake.updates, [])
        self.assertEqual(self.rules.saved, [])
        self.assertIsNone(fake.current.use_airgrabber_downloads)

    async def test_off_for_an_imported_one_until_its_switch_is_on(self) -> None:
        dialog, part, fake = self.ready(with_airgrabber(DONE, imported_from=IMPORTED))
        index = self.airgrabbers(part)
        self.assertEqual(part.rule_rows[index].switch.get(), 0)
        part.rule_rows[index].switch.toggle()  # clicked: stored at once
        await settle()
        self.assertEqual(fake.updates, [{"use_airgrabber_downloads": True}])
        self.assertEqual(watched_folders(self.rules.current, fake.current), [DONE.path])

    async def test_turning_it_off_stores_the_setting(self) -> None:
        dialog, part, fake = self.ready(with_airgrabber(DONE, automatic="on"))
        await part.toggle_rule(self.airgrabbers(part), False)  # a rule: RuleService stores it
        self.assertEqual(fake.updates, [{"use_airgrabber_downloads": False}])
        self.assertEqual(fake.current.automatic, "on")  # the master switch is untouched

    async def test_add_a_rule_for_a_suggestion(self) -> None:
        dialog, part, _fake = self.ready(with_airgrabber(SUGGESTED, watch_folders=[r"E:\Mine"]))
        self.assertTrue(labels(part.airgrabber_list)[1].startswith("Suggested: Download Station"))
        buttons = [b for b in part.airgrabber_list.winfo_children() if isinstance(b, ctk.CTkButton)]
        self.assertEqual([b.cget("text") for b in buttons], ["Add a rule for this folder"])
        buttons[0].invoke()
        await settle()  # stored at once (a new rule: its first look waits)
        self.assertEqual([r.folder for r in part.rule_drafts], [r"E:\Mine", "", SUGGESTED.path])
        airgrabbers = part.rule_drafts[1]
        added = part.rule_drafts[2]
        self.assertEqual(
            added.model_dump(exclude={"folder", "airgrabber", "id", "fresh"}),
            airgrabbers.model_dump(exclude={"folder", "airgrabber", "id", "fresh"}),
        )  # AirGrabber's rule's routing
        self.assertEqual([r.folder for r in self.rules.saved[0]], [r"E:\Mine", "", SUGGESTED.path])

    async def test_airgrabbers_folders_derived_meanwhile_show(self) -> None:
        _dialog, part, fake = self.ready(with_airgrabber())
        self.assertEqual(labels(part.airgrabber_list), [])
        await fake.update(airgrabber=AirGrabberDownloads(ask="finished"))
        self.assertTrue(labels(part.airgrabber_list)[0].startswith("AirGrabber doesn't know"))
        await fake.update(airgrabber=AirGrabberDownloads(folders=[DONE]))
        self.assertEqual(labels(part.airgrabber_list)[0], DONE.path)
        self.assertFalse(part.rules_changed)

    async def test_without_airgrabbers_folders_the_box_ends_under_its_text(self) -> None:
        # An empty CTkFrame keeps its default 200 units: the empty list is taken out.
        _dialog, part, fake = self.ready(with_airgrabber())
        body = part.airgrabber_list.master
        body.update_idletasks()
        self.assertEqual(part.airgrabber_list.winfo_manager(), "")
        empty = body.winfo_reqheight()
        scaling = ctk.ScalingTracker.get_widget_scaling(body)
        self.assertLess(empty, 200 * scaling)  # the heading and its text only
        await fake.update(airgrabber=AirGrabberDownloads(folders=[DONE]))
        body.update_idletasks()
        self.assertEqual(part.airgrabber_list.winfo_manager(), "grid")
        self.assertGreater(body.winfo_reqheight(), empty)
        await fake.update(airgrabber=AirGrabberDownloads())  # gone again: no room kept
        body.update_idletasks()
        self.assertEqual(part.airgrabber_list.winfo_manager(), "")
        self.assertEqual(body.winfo_reqheight(), empty)
