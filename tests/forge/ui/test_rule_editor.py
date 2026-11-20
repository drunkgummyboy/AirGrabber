"""The rule editor (app/forge/ui/rule_editor.py): it builds the rule it shows (only the changed
ticks kept), "Anything" with a move has two destinations, AirGrabber's rule has no folder choice
and no Delete, a preset gone says so, the live check says what fits and when it can't reach the
folder (an answer after a newer look is dropped), and Cancel changes nothing."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from pathlib import Path

import customtkinter as ctk

from app import i18n
from app.forge.core.rules import PRESET_GONE
from app.forge.models.media import MediaKind, ParsedMedia
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.forge.ui.rule_editor import LOOKING, NO_DOWNLOADS, UNREACHABLE, WIDTH, RuleEditor
from app.i18n import Language, t
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until

D = r"D:\Downloads"
MOVIE, EPISODE = MediaKind.MOVIE, MediaKind.EPISODE
Files = list[tuple[Path, ParsedMedia]] | None


class EditorCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.saved: list[Rule] = []
        self.deleted: list[bool] = []
        self.asked: list[tuple[str, str]] = []
        self.answer = ""
        self.peeked: list[Path] = []
        self.files: Files = []

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def peek(self, folder: Path) -> Files:
        self.peeked.append(folder)
        return self.files

    def ask_folder(self, title: str, start: str) -> str:
        self.asked.append((title, start))
        return self.answer

    def editor(
        self,
        rule: Rule,
        *,
        index: int = 0,
        rules: list[Rule] | None = None,
        airgrabber_folders: tuple[str, ...] = (),
        deletable: bool = True,
    ) -> RuleEditor:
        rules = [rule] if rules is None else rules
        editor = RuleEditor(
            self.root,
            rule=rule,
            index=index,
            rules=rules,
            settings=ForgeSettings(),
            presets=DEFAULT_PRESETS,
            airgrabber_folders=airgrabber_folders,
            peek=self.peek,
            ask_folder=self.ask_folder,
            on_save=self.saved.append,
            on_delete=(lambda: self.deleted.append(True)) if deletable else None,
        )
        def close() -> None:
            if editor.winfo_exists():
                editor.destroy()

        self.addCleanup(close)
        return editor

    def lines(self, editor: RuleEditor) -> list[str]:
        return [str(label.cget("text")) for label in editor.check_lines]


class BuildTests(EditorCase):
    async def test_the_editor_builds_the_rule_it_shows(self) -> None:
        editor = self.editor(Rule(folder=D))
        self.assertEqual(editor.title(), "Edit rule")
        editor.take.set(t("Films"))
        editor.quality.set(t("4K only"))
        self.answer = r"E:\Big"
        editor.destination_buttons[MOVIE].invoke()
        editor.mode.set("auto")
        editor.ticks[MOVIE, "trailer"].set(False)
        self.assertEqual(
            editor.draft(),
            Rule(
                folder=D,
                take="films",
                quality="4k",
                films=RuleTarget(
                    preset="Movie (Folder)",
                    extras={"trailer": False},
                    to="folder",
                    folder=r"E:\Big",
                ),
                mode="auto",
            ),
        )
        self.assertEqual(editor.destination_menus[MOVIE].get(), r"E:\Big")
        self.assertEqual(self.asked, [("Choose the folder to move them to", "")])
        draft = editor.draft()
        editor.save()
        self.assertEqual(self.saved, [draft])
        self.assertTrue(editor.closed)
        self.assertFalse(editor.winfo_exists())

    async def test_anything_with_a_move_has_two_destinations(self) -> None:
        editor = self.editor(Rule(folder=D))
        await pump(self.root, 0.05)
        self.assertEqual(set(editor.destination_menus), {EPISODE, MOVIE})
        self.assertEqual(editor.destination_menus[EPISODE].get(), "Your Series folder")
        self.assertEqual(editor.destination_menus[MOVIE].get(), "Your Movies folder")
        for kind in (EPISODE, MOVIE):
            self.assertTrue(editor.destination_menus[kind].winfo_ismapped())
            self.assertTrue(editor.preset_menus[kind].winfo_ismapped())
        self.assertEqual(editor.preset_menus[EPISODE].get(), "Series (Folder)")
        editor.take.set(t("Shows"))
        await pump(self.root, 0.05)
        self.assertTrue(editor.destination_menus[EPISODE].winfo_ismapped())
        self.assertFalse(editor.destination_menus[MOVIE].winfo_ismapped())
        self.assertFalse(editor.preset_menus[MOVIE].winfo_ismapped())

    async def test_ticks_follow_the_preset_and_only_changes_are_kept(self) -> None:
        editor = self.editor(Rule(folder=D, take="films"))
        self.assertTrue(editor.ticks[MOVIE, "trailer"].get())  # Movie (Folder) has it
        self.assertFalse(editor.ticks[MOVIE, "nfo"].get())
        editor.ticks[MOVIE, "nfo"].set(True)
        self.assertEqual(editor.draft().films.extras, {"nfo": True})
        editor.preset_menus[MOVIE].set("Movie (Flat)")  # no extras of its own
        self.assertFalse(editor.ticks[MOVIE, "trailer"].get())
        self.assertTrue(editor.ticks[MOVIE, "nfo"].get())  # this rule's change stays
        self.assertEqual(
            editor.draft().films, RuleTarget(preset="Movie (Flat)", extras={"nfo": True})
        )
        self.assertEqual(
            {key for kind, key in editor.ticks if kind is MOVIE},
            {"subtitles", "movie_poster", "backdrop", "trailer", "quality_in_name", "nfo"},
        )

    async def test_sort_inside_the_folder_and_a_name(self) -> None:
        editor = self.editor(Rule(folder=D, take="shows"))
        editor.then.set("stays")
        editor.subfolders.set(False)
        editor.name_entry.insert(0, " Courses ")
        draft = editor.draft()
        self.assertTrue(draft.stays)
        self.assertFalse(draft.subfolders)
        self.assertEqual(draft.name, "Courses")
        await pump(self.root, 0.05)
        self.assertFalse(editor.destination_menus[EPISODE].winfo_ismapped())
        self.assertIn(r"Downloads\VisionQuest", str(editor.sort_note.cget("text")))

    async def test_the_name_hint_and_placeholder_follow_the_choices(self) -> None:
        editor = self.editor(Rule(folder=D))
        editor.take.set(t("Films"))
        self.assertEqual(editor.name_entry.cget("placeholder_text"), "Films from Downloads")
        self.assertIn("“Rule: Films from Downloads”", str(editor.name_hint.cget("text")))

    async def test_a_new_rule_chooses_its_folder(self) -> None:
        rules = [Rule(folder=D)]
        editor = self.editor(Rule(), index=1, rules=rules, deletable=False)
        self.assertEqual(editor.title(), "New rule")
        self.assertIsNone(editor.delete_button)
        assert editor.browse_button is not None
        self.answer = r"F:\Courses"
        editor.browse_button.invoke()
        self.assertEqual(self.asked[-1], ("Choose the folder this rule watches", ""))
        self.assertEqual(editor.draft().folder, r"F:\Courses")
        self.assertEqual(editor.folder_label.cget("text"), r"F:\Courses")

    async def test_airgrabbers_rule_has_no_folder_choice_and_no_delete(self) -> None:
        rule = Rule(airgrabber=True)
        editor = self.editor(
            rule, airgrabber_folders=(r"D:\AirGrabber", r"E:\Seeds"), deletable=False
        )
        self.assertIsNone(editor.browse_button)
        self.assertIsNone(editor.delete_button)
        self.assertEqual(editor.folder_label.cget("text"), "The folders AirGrabber downloads to")
        self.assertEqual(editor.draft().folder, "")
        self.assertTrue(editor.draft().airgrabber)
        await pump_until(self.root, lambda: len(self.peeked) == 2)
        self.assertEqual(self.peeked, [Path(r"D:\AirGrabber"), Path(r"E:\Seeds")])

    async def test_a_preset_gone_says_so_under_its_menu(self) -> None:
        rule = Rule(folder=D, take="films", films=RuleTarget(preset="Old one"))
        editor = self.editor(rule)
        await pump(self.root, 0.05)
        self.assertEqual(editor.preset_menus[MOVIE].get(), "Old one")
        gone = [
            label
            for label in editor._gone.values()
            if label.winfo_ismapped() and label.cget("text") == t(PRESET_GONE)
        ]
        self.assertEqual(len(gone), 1)
        self.assertEqual(editor.draft().films.preset, "Old one")

    async def test_each_kinds_menu_lists_the_presets_that_fit_it(self) -> None:
        editor = self.editor(Rule(folder=D))
        self.assertEqual(
            list(editor.preset_menus[EPISODE].cget("values")),
            ["Auto", "Series (Folder)", "Series (Flat)"],
        )
        self.assertEqual(
            list(editor.preset_menus[MOVIE].cget("values")),
            ["Auto", "Movie (Folder)", "Movie (Flat)"],
        )

    async def test_the_rules_own_preset_stays_in_its_menu(self) -> None:
        rule = Rule(
            folder=D,
            shows=RuleTarget(preset="Movie (Flat)"),  # made for films
            films=RuleTarget(preset="Old one"),  # gone
        )
        editor = self.editor(rule)
        self.assertEqual(
            list(editor.preset_menus[EPISODE].cget("values")),
            ["Auto", "Series (Folder)", "Series (Flat)", "Movie (Flat)"],
        )
        self.assertEqual(
            list(editor.preset_menus[MOVIE].cget("values")),
            ["Auto", "Movie (Folder)", "Movie (Flat)", "Old one"],
        )
        self.assertEqual(editor.preset_menus[EPISODE].get(), "Movie (Flat)")
        self.assertEqual(editor.draft(), rule)

    async def test_a_gone_preset_keeps_the_rules_extras(self) -> None:
        films = RuleTarget(preset="Old one", extras={"trailer": False, "nfo": True})
        editor = self.editor(Rule(folder=D, take="films", films=films))
        self.assertEqual(editor.draft().films, films)
        editor.preset_menus[MOVIE].set("Movie (Flat)")  # chosen again: compared with it
        self.assertEqual(
            editor.draft().films, RuleTarget(preset="Movie (Flat)", extras={"nfo": True})
        )

    async def test_a_new_rule_without_a_folder_has_a_name_that_reads(self) -> None:
        editor = self.editor(Rule(take="films"), index=1, rules=[Rule(folder=D)], deletable=False)
        self.assertEqual(editor.name_entry.cget("placeholder_text"), "Films")
        self.assertIn("“Rule: Films”", str(editor.name_hint.cget("text")))
        self.assertFalse(editor.check_frame.winfo_manager())  # no folder: nothing to check
        self.answer = r"F:\Courses"
        assert editor.browse_button is not None
        editor.browse_button.invoke()
        self.assertEqual(editor.name_entry.cget("placeholder_text"), "Films from Courses")

    async def test_delete_hands_it_over_and_closes(self) -> None:
        editor = self.editor(Rule(folder=D))
        assert editor.delete_button is not None
        editor.delete_button.invoke()
        self.assertEqual(self.deleted, [True])
        self.assertEqual(self.saved, [])
        self.assertTrue(editor.closed)

    async def test_cancel_changes_nothing(self) -> None:
        editor = self.editor(Rule(folder=D))
        editor.take.set(t("Films"))
        editor.cancel()
        self.assertEqual(self.saved, [])
        self.assertEqual(self.deleted, [])
        self.assertFalse(editor.winfo_exists())


class LiveCheckTests(EditorCase):
    def film(self, name: str) -> tuple[Path, ParsedMedia]:
        return Path(D) / name, ParsedMedia(source_name=name, kind=MOVIE, title="A Film")

    async def test_it_can_be_made_wider_and_its_texts_follow(self) -> None:
        # Wider than it was (the owner's wish, 2026-11-20), and dragged wider still.
        self.files = None
        editor = self.editor(Rule(folder=D))
        await pump_until(self.root, lambda: self.lines(editor) == [t(UNREACHABLE)])
        self.assertEqual(editor.resizable(), (True, True))
        editor.deiconify()
        editor.update()
        scale = float(ctk.ScalingTracker.get_widget_scaling(editor))
        first = round(editor.winfo_width() / scale)
        self.assertGreaterEqual(first, WIDTH)
        self.assertAlmostEqual(editor.wm_minsize()[0] / scale, first, delta=1)
        line = editor.check_lines[0]
        notes_wrap, line_wrap = (int(w.cget("wraplength")) for w in (editor.notes_label, line))
        editor.geometry(f"{first + 150}x{round(editor.winfo_height() / scale)}")
        editor.update()
        await pump(self.root, 0.15)
        self.assertEqual(int(editor.notes_label.cget("wraplength")), notes_wrap + 150)
        self.assertEqual(int(line.cget("wraplength")), line_wrap + 150)
        # The check's lines drawn again: as wide as the window is now.
        editor.refresh_check()
        await pump_until(
            self.root,
            lambda: bool(editor.check_lines)
            and editor.check_lines[0] is not line
            and int(editor.check_lines[0].cget("wraplength")) == line_wrap + 150,
        )

    async def test_the_live_check_says_when_it_cant_reach(self) -> None:
        self.files = None
        editor = self.editor(Rule(folder=D))
        await pump_until(self.root, lambda: self.lines(editor) == [t(UNREACHABLE)])

    async def test_an_empty_folder_says_so(self) -> None:
        editor = self.editor(Rule(folder=D))
        await pump_until(self.root, lambda: self.lines(editor) == [t(NO_DOWNLOADS)])

    async def test_what_fits_and_why_the_rest_doesnt(self) -> None:
        self.files = [self.film("Dune.2160p.mkv"), self.film("Heat.1080p.mkv")]
        rules = [Rule(folder=D, take="films", quality="4k"), Rule(folder=D, take="films")]
        editor = self.editor(rules[0], rules=rules)
        await pump_until(
            self.root,
            lambda: self.lines(editor)
            == ["1 film fits this rule.", "1 film doesn't: it's not 4K. Rule 2 takes it."],
        )

    async def test_a_change_looks_again_and_an_older_answer_is_dropped(self) -> None:
        release = asyncio.Event()
        answers: list[Files] = [[self.film("Old.mkv")], []]

        async def slow_then_fast(folder: Path) -> Files:
            self.peeked.append(folder)
            answer = answers.pop(0)
            if answer:
                await release.wait()  # the first look is slow
            return answer

        self.peek = slow_then_fast  # type: ignore[method-assign]
        editor = self.editor(Rule(folder=D))
        await pump_until(self.root, lambda: len(self.peeked) == 1)  # the slow look is on
        self.assertEqual(self.lines(editor), [t(LOOKING)])
        self.answer = r"F:\Other"
        assert editor.browse_button is not None
        editor.browse_button.invoke()  # another folder: read again
        await pump_until(self.root, lambda: self.lines(editor) == [t(NO_DOWNLOADS)], within=3)
        release.set()
        await pump(self.root, 0.1)
        self.assertEqual(self.lines(editor), [t(NO_DOWNLOADS)])
        self.assertEqual(self.peeked, [Path(D), Path(r"F:\Other")])

    async def test_take_and_quality_explain_what_was_read_without_reading_again(self) -> None:
        self.files = [self.film("Dune.2160p.mkv"), self.film("Heat.1080p.mkv")]
        editor = self.editor(Rule(folder=D, take="films"))
        await pump_until(self.root, lambda: self.lines(editor) == ["2 films fit this rule."])
        editor.quality.set(t("4K only"))
        await pump_until(
            self.root,
            lambda: self.lines(editor)
            == ["1 film fits this rule.", "1 film doesn't: it's not 4K. No rule takes it."],
        )
        editor.take.set(t("Shows"))
        await pump_until(
            self.root,
            lambda: self.lines(editor) == ["2 films don't: they're films. No rule takes them."],
        )
        self.assertEqual(self.peeked, [Path(D)])
        editor.refresh_check()  # asked for: read again
        await pump_until(self.root, lambda: len(self.peeked) == 2)

    async def test_an_answer_after_the_window_closed_is_dropped(self) -> None:
        release = asyncio.Event()

        async def slow(folder: Path) -> Files:
            await release.wait()
            return [self.film("Dune.mkv")]

        self.peek = slow  # type: ignore[method-assign]
        editor = self.editor(Rule(folder=D))
        await pump(self.root, 0.05)
        # A check that outlives the window (closing cancels its own task: this one isn't).
        late = asyncio.create_task(editor._check(editor._check_generation, [Path(D)]))
        await pump(self.root, 0.05)
        editor.cancel()
        release.set()
        await asyncio.wait_for(late, 2)  # ends quietly, touching no destroyed widget
        self.assertIsNone(late.exception())
        self.assertFalse(editor.winfo_exists())
