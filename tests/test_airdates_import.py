"""The airdates.tv import panel's decisions and texts (pure helpers), and the panel on a real
(hidden) Tk root: the shows an import missed are named and tried again; the Paste button. The
system clipboard is used briefly by the Paste test."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.models import AirdatesPreview, ImportMatch, ImportResult
from app.ui.airdates_import import (
    AirdatesImportPanel,
    from_note,
    import_label,
    import_notes,
    imported_text,
    importing_text,
    looking_up_text,
    nothing_reached,
    retry_ids,
    review_heading,
    review_notes,
    ticked_by_default,
)

FOUND = ImportMatch(source_name="Severance", show_id=1, show_name="Severance", year=2022)
TRACKED = ImportMatch(
    source_name="Ghosts", show_id=2, show_name="Ghosts", year=2021, already_tracked=True
)
MISSING = ImportMatch(source_name="Nothing Matches")


class TickTests(unittest.TestCase):
    def test_found_shows_start_ticked(self) -> None:
        self.assertTrue(ticked_by_default(FOUND))

    def test_already_followed_and_missing_ones_do_not(self) -> None:
        self.assertFalse(ticked_by_default(TRACKED))
        self.assertFalse(ticked_by_default(MISSING))


class FromNoteTests(unittest.TestCase):
    def note(self, source: str, tvmaze: str) -> str | None:
        return from_note(ImportMatch(source_name=source, show_id=1, show_name=tvmaze))

    def test_same_name_has_no_note(self) -> None:
        self.assertIsNone(self.note("Severance", "Severance"))

    def test_case_and_punctuation_are_ignored(self) -> None:
        self.assertIsNone(
            self.note(
                "Re:Zero − Starting Life in Another World",
                "Re:ZERO -Starting Life in Another World-",
            )
        )
        self.assertIsNone(self.note("Greys Anatomy", "Grey's Anatomy"))
        self.assertIsNone(self.note("shakespeare & hathaway", "Shakespeare & Hathaway"))

    def test_different_name_shows_the_source(self) -> None:
        self.assertEqual(
            self.note("The Traitors (US)", "The Traitors"), ' · from "The Traitors (US)"'
        )
        self.assertEqual(self.note("Monk", "Mr. Monk"), ' · from "Monk"')

    def test_no_note_without_a_match(self) -> None:
        self.assertIsNone(from_note(MISSING))


class TextTests(unittest.TestCase):
    def test_heading_counts_everything_in_the_export(self) -> None:
        preview = AirdatesPreview(
            matches=(FOUND, TRACKED, MISSING), unknown_ids=(7, 8), failed=1
        )
        self.assertEqual(review_heading(preview), "Found 2 of 6 shows")
        single = AirdatesPreview(matches=(FOUND,))
        self.assertEqual(review_heading(single), "Found 1 of 1 show")

    def test_notes(self) -> None:
        other = ImportMatch(source_name="Also Missing")
        preview = AirdatesPreview(
            matches=(FOUND, MISSING, other), unknown_ids=(7, 8), failed=3
        )
        self.assertEqual(
            review_notes(preview),
            [
                "Couldn't find: Nothing Matches, Also Missing",
                "airdates.tv didn't recognise 2 shows",
                "3 couldn't be looked up; try again later",
            ],
        )
        self.assertEqual(
            review_notes(AirdatesPreview(matches=(FOUND,), unknown_ids=(7,))),
            ["airdates.tv didn't recognise 1 show"],
        )
        self.assertEqual(review_notes(AirdatesPreview(matches=(FOUND, TRACKED))), [])

    def test_nothing_reached(self) -> None:
        self.assertTrue(nothing_reached(AirdatesPreview(failed=4)))
        self.assertFalse(nothing_reached(AirdatesPreview(matches=(FOUND,), failed=3)))
        self.assertFalse(nothing_reached(AirdatesPreview(unknown_ids=(1,), failed=3)))
        self.assertFalse(nothing_reached(AirdatesPreview()))

    def test_looking_up_counts_shows_not_lookups(self) -> None:
        # find() reports two lookups per show (airdates.tv, then TVMaze): total = 2 x 37.
        self.assertEqual(looking_up_text(0, 74), "Looking up 1 of 37…")
        self.assertEqual(looking_up_text(23, 74), "Looking up 12 of 37…")
        self.assertEqual(looking_up_text(74, 74), "Looking up 37 of 37…")

    def test_importing(self) -> None:
        self.assertEqual(importing_text(0, 34), "Importing 1 of 34…")
        self.assertEqual(importing_text(2, 34), "Importing 3 of 34…")
        self.assertEqual(importing_text(34, 34), "Importing 34 of 34…")

    def test_import_button_label(self) -> None:
        self.assertEqual(import_label(0), "Import 0 shows")
        self.assertEqual(import_label(1), "Import 1 show")
        self.assertEqual(import_label(34), "Import 34 shows")

    def test_after_import(self) -> None:
        self.assertEqual(imported_text(3), "Imported 3 shows")
        self.assertEqual(imported_text(1), "Imported 1 show")
        self.assertEqual(import_notes(ImportResult(added=("A",))), "")
        names = {"2": "Fargo", "3": "Dark", "4": "Ghosts"}
        self.assertEqual(
            import_notes(ImportResult(added=("A",), failed=("2",)), names),
            "Couldn't import: Fargo — try again later",
        )
        missed = ImportResult(added=("A",), failed=("2", "3"), not_found=("9",))
        self.assertEqual(
            import_notes(missed, names), "Couldn't import: Fargo, Dark, 9 — try again later"
        )
        self.assertEqual(retry_ids(missed), [2, 3, 9])
        self.assertEqual(retry_ids(ImportResult(failed=("Some name",))), [])


class FakeImporter:
    """LibraryService.import_shows: the ids in `failing` fail once, the rest are added."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = set(failing)
        self.calls: list[list[str]] = []

    async def import_shows(self, entries: Any, progress: Any = None) -> ImportResult:
        entries = list(entries)
        self.calls.append(entries)
        failed = tuple(entry for entry in entries if entry in self.failing)
        self.failing -= set(failed)
        added = tuple(f"Show {entry}" for entry in entries if entry not in failed)
        return ImportResult(added=added, failed=failed)


class FakeFinder:
    async def find(self, export_text: str, progress: Any = None) -> AirdatesPreview:
        raise AssertionError("not used")


class PanelTests(unittest.IsolatedAsyncioTestCase):
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
        # Tcl's own cancel: after_cancel() would also delete the callbacks' commands, which
        # the widgets that scheduled them (CTkTextbox) delete again when destroyed.
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.root.destroy()
        gc.collect()

    async def until(self, check: Callable[[], bool]) -> None:
        for _ in range(200):
            if check():
                return
            await asyncio.sleep(0.01)
            self.root.update()
        self.fail("never happened")

    def panel(self, library: FakeImporter) -> AirdatesImportPanel:
        done: list[int] = []
        panel = AirdatesImportPanel(
            self.root, airdates=FakeFinder(), library=library, on_done=done.append
        )
        panel.pack()
        self.done = done
        return panel

    async def test_shows_that_failed_are_named_and_tried_again(self) -> None:
        library = FakeImporter({"3"})
        panel = self.panel(library)
        fargo = ImportMatch(source_name="Fargo", show_id=3, show_name="Fargo", year=2014)
        panel.show_review(AirdatesPreview(matches=(FOUND, fargo)))
        panel._import_button.invoke()
        await self.until(lambda: bool(self.done))
        self.assertEqual(panel._review_status.cget("text"), "Imported 1 show")
        detail = panel._review_detail.cget("text")
        self.assertEqual(" ".join(detail.split()), "Couldn't import: Fargo — try again later")
        self.assertTrue(panel._retry_button.winfo_manager())
        self.assertFalse(panel._import_button.winfo_manager())
        panel._retry_button.invoke()
        await self.until(lambda: len(self.done) == 2)
        self.assertEqual(library.calls, [["1", "3"], ["3"]])  # just the one that failed
        self.assertEqual(panel._review_status.cget("text"), "Imported 2 shows")
        self.assertFalse(panel._review_detail.winfo_manager())
        self.assertFalse(panel._retry_button.winfo_manager())
        self.assertEqual(self.done, [1, 2])

    async def test_the_paste_button(self) -> None:
        panel = self.panel(FakeImporter(set()))
        self.root.clipboard_clear()
        self.root.clipboard_append("6174=#ffcc00;")
        panel._paste_button.invoke()
        self.assertEqual(panel._text.get("1.0", "end").strip(), "6174=#ffcc00;")
        self.root.clipboard_clear()
        panel._text.delete("1.0", "end")
        panel._paste_button.invoke()
        self.assertEqual(panel._text.get("1.0", "end").strip(), "")
        self.assertEqual(
            panel._paste_status.cget("text"),
            "Nothing to paste yet: copy the text on airdates.tv first.",
        )


if __name__ == "__main__":
    unittest.main()
