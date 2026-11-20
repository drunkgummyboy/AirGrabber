"""Settings › Naming (app/forge/ui/settings_naming.py) in the Settings window
(app/ui/settings_dialog.py): the naming and remembered-matches parts of MediaForge's tests/ui/test_settings_view.py
(save / rename / delete a preset, the format sample, forgetting a match, the edited preset
highlighted), plus what the section adds inside AirGrabber: each change stored when it is made (a
name or format on Enter or leaving its field, `typed`; the rest at once; Undo stores what was
before), only the changed settings stored, the presets merged onto what is stored by then, the
Series / Movies presets, the names language, the right-click menu's hook, waiting for Forge, and
Dutch."""

from __future__ import annotations

import asyncio
import builtins
import gc
import tkinter as tk
import weakref
from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge.models.match import RememberedMatch
from app.forge.models.media import MediaKind, TypeHint
from app.forge.models.preset import DEFAULT_PRESETS, Preset, PresetOptions
from app.forge.models.rules import Rule
from app.forge.models.settings import ForgeSettings
from app.forge.ui import settings_naming
from app.forge.ui.settings_naming import (
    FORGET,
    LOAD_FAILED,
    NamingSection,
    PresetDraft,
    PresetsChange,
    code_example,
    last_preset_after,
    match_text,
    merged_presets,
    new_preset_name,
    on_presets_saved,
    preset_problem,
    presets_change,
    sample_lines,
)
from app.forge.ui.look import app_look
from app.forge.ui.settings_pages import FORGE_PARTS, SAVE_FAILED
from app.ui.settings_pages_registry import PAGE_NAMING, PAGE_SORTING, PAGES
from app.ui import theme
from app.ui.settings_dialog import SettingsDialog
from tests.forge.ui.settings_kit import forge_settings
from app.i18n import Language
from app.ui.widgets import install_window_release
from app.ui.settings_layout import NARROW_PAGE_WIDTH
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.ui.subtitle_kit import FakeSubtitleSource
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_settings_library import NOT_READY_TEXT, FakeSettings, labels
from tests.test_settings_dialog import settle

OFFICE = RememberedMatch(
    kind=MediaKind.EPISODE,
    norm_title="the office",
    year=None,
    tmdb_id=1,
    label="The Office (2005)",
)
DUNE = RememberedMatch(
    kind=MediaKind.MOVIE, norm_title="dune", year=2021, tmdb_id=2, label="Dune (2021)"
)


class FakePresets:
    """PresetService's surface the section uses: list, replace_all."""

    def __init__(self, presets: Sequence[Preset] = DEFAULT_PRESETS) -> None:
        self.stored = list(presets)
        self.replaced: list[list[Preset]] = []
        self.fail = False

    async def list(self) -> list[Preset]:
        await asyncio.sleep(0)
        return list(self.stored)

    async def replace_all(self, presets: Sequence[Preset]) -> None:
        await asyncio.sleep(0)
        if self.fail:
            raise OSError(r"C:\Users\Alex\forge.db is locked")
        self.replaced.append(list(presets))
        self.stored = list(presets)

    @property
    def names(self) -> builtins.list[str]:
        return [preset.name for preset in self.stored]


class FakeMatches:
    """MatchService's surface the section uses: list, forget."""

    def __init__(self, matches: Sequence[RememberedMatch] = ()) -> None:
        self.stored = list(matches)
        self.forgotten: list[RememberedMatch] = []

    async def list(self) -> list[RememberedMatch]:
        await asyncio.sleep(0)
        return list(self.stored)

    async def forget(self, match: RememberedMatch) -> None:
        await asyncio.sleep(0)
        self.forgotten.append(match)
        self.stored.remove(match)

    async def remember(self, match: RememberedMatch) -> None:
        await asyncio.sleep(0)
        self.stored.append(match)


class FakeRules:
    """RuleService's surface the section uses: presets_renamed (recorded); and what Sorting
    reads when a test visits it (`current`: AirGrabber's rule only, `airgrabber_rule`,
    `subscribe`)."""

    def __init__(self) -> None:
        self.renamed: list[dict[str, str]] = []
        self.airgrabber_rule = Rule(id=1, airgrabber=True)
        self.current: tuple[Rule, ...] = (self.airgrabber_rule,)

    def subscribe(self, listener: Callable[[tuple[Rule, ...]], None]) -> Callable[[], None]:
        return lambda: None

    async def presets_renamed(self, renamed: Mapping[str, str]) -> None:
        await asyncio.sleep(0)
        self.renamed.append(dict(renamed))


class FakeForge:
    """ForgeRuntime's surface: `services` (None until started), `start()`, `reason`."""

    def __init__(
        self,
        settings: FakeSettings | None,
        presets: FakePresets | None = None,
        matches: FakeMatches | None = None,
        *,
        started: bool = True,
    ) -> None:
        self.parts = (
            None
            if settings is None
            else SimpleNamespace(
                settings=settings,
                presets=presets or FakePresets(),
                matches=matches or FakeMatches(),
                subtitles=FakeSubtitleSource(),
                rules=FakeRules(),
            )
        )
        self.services = self.parts if started else None
        self.reason = ""
        self.gate: asyncio.Future[None] | None = None

    async def start(self) -> Any:
        if self.gate is not None:
            await asyncio.shield(self.gate)
        if self.parts is None:
            self.reason = NOT_READY_TEXT
        self.services = self.parts
        return self.parts


class NamingSettingsTests(ForgeAsyncTestCase):
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
        self.told: list[PresetsChange] = []

        async def tell(change: PresetsChange) -> None:
            self.told.append(change)

        self.addCleanup(on_presets_saved(tell))

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # the dialog's Tk variables, on the main thread

    def dialog(self, forge: FakeForge) -> SettingsDialog:
        return forge_settings(self.root, forge, section=PAGE_NAMING)

    @staticmethod
    def section(dialog: SettingsDialog) -> NamingSection:
        part = dialog.parts[PAGE_NAMING]
        assert isinstance(part, NamingSection)
        return part

    async def ready(
        self,
        settings: ForgeSettings | None = None,
        presets: Sequence[Preset] = DEFAULT_PRESETS,
        matches: Sequence[RememberedMatch] = (),
    ) -> tuple[SettingsDialog, NamingSection, FakeForge]:
        forge = FakeForge(
            FakeSettings(settings or ForgeSettings()), FakePresets(presets), FakeMatches(matches)
        )
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump_until(self.root, lambda: part.ready)
        return dialog, part, forge

    @staticmethod
    def stored(forge: FakeForge) -> FakePresets:
        assert forge.parts is not None
        return cast(FakePresets, forge.parts.presets)

    @staticmethod
    def settings(forge: FakeForge) -> FakeSettings:
        assert forge.parts is not None
        return cast(FakeSettings, forge.parts.settings)

    @staticmethod
    def remembered(forge: FakeForge) -> FakeMatches:
        assert forge.parts is not None
        return cast(FakeMatches, forge.parts.matches)

    @staticmethod
    def click(part: NamingSection, code: str) -> None:
        next(r for r in part.code_table.rows if r.value == code)._clicked(cast(Any, None))

    @staticmethod
    async def typed(part: NamingSection) -> None:
        """Enter in the name and the format fields (or the keyboard leaving them), stored."""
        part.name_field.commit_now()
        part.format_field.commit_now()
        await settle()

    def select(self, part: NamingSection, name: str) -> None:
        draft = next(d for d in part._drafts if d.preset.name == name)
        part.preset_buttons[part._drafts.index(draft)].invoke()

    # --- the section in the group ----------------------------------------------------------------

    async def test_a_closed_dialog_is_freed(self) -> None:
        # Tk holds a variable trace's callback, out of the garbage collector's sight: the name
        # and format fields' go with the page, so the closed dialog doesn't stay in memory.
        install_window_release(self.root)  # as MainWindow does
        dialog, part, _forge = await self.ready()
        part._name.set("Renamed")  # the traces run
        part._format.set("{t}")
        ref = weakref.ref(dialog)
        dialog.destroy()
        del dialog, part
        await pump(self.root, 0.05)
        gc.collect()
        self.assertIsNone(ref())

    async def test_naming_follows_sorting_in_its_group(self) -> None:
        dialog, _part, _forge = await self.ready()
        self.assertIn(PAGE_NAMING, FORGE_PARTS)
        self.assertEqual(PAGES[PAGE_NAMING].icon, "🏷")
        self.assertEqual(dialog.sections.index(PAGE_NAMING), dialog.sections.index(PAGE_SORTING) + 1)
        self.assertEqual(dialog.section, PAGE_NAMING)
        texts = labels(dialog.page(PAGE_NAMING))
        self.assertEqual(texts[0], "Naming")
        self.assertTrue(texts[1])
        for title in ("Presets", "For new rules", "Names language", "Remembered matches"):
            self.assertIn(title, texts)
        self.assertNotIn("Library presets", texts)

    async def test_shows_the_presets_in_order_and_the_first_one_edited(self) -> None:
        _dialog, part, _forge = await self.ready()
        names = [preset.name for preset in DEFAULT_PRESETS]
        self.assertEqual([b.cget("text") for b in part.preset_buttons], names)
        # The edited preset is highlighted (MediaForge: test_edited_preset_is_highlighted).
        assert part.editing is not None
        self.assertEqual(part.editing.preset.name, "Auto")
        self.assertEqual(part.preset_buttons[0].cget("fg_color"), app_look().SELECTED)
        self.assertEqual(part.preset_buttons[1].cget("fg_color"), "transparent")
        self.assertEqual(part.name_entry.get(), "Auto")
        self.assertEqual(part.format_entry.get(), "{ny}")
        self.assertEqual(part.type_switch.get(), "Auto")
        self.assertEqual(str(part.delete_button.cget("state")), "normal")
        self.select(part, "Series (Folder)")
        self.assertEqual(part.preset_buttons[3].cget("fg_color"), app_look().SELECTED)
        self.assertEqual(part.type_switch.get(), "TV")
        self.assertTrue(part._options["season_poster"].get())
        self.assertFalse(part._options["trailer"].get())

    async def test_shows_the_stored_settings(self) -> None:
        stored = ForgeSettings(series_preset="Series (Flat)", tmdb_language="nl-NL")
        _dialog, part, _forge = await self.ready(stored)
        self.assertEqual(part.choice_menus["series_preset"].get(), "Series (Flat)")
        self.assertEqual(part.choice_menus["movies_preset"].get(), "Movie (Folder)")
        self.assertEqual(part.language_menu.get(), "Dutch")
        self.assertFalse(part.presets_changed())

    # --- the format sample -----------------------------------------------------------------------

    async def test_format_sample_shows_rendered_name(self) -> None:
        _dialog, part, _forge = await self.ready()
        self.select(part, "Series (Flat)")
        part._format.set("{ny} - {s00e00} - {en}")
        self.assertIn("S01E02 - Theseus", part.format_sample.cget("text"))
        self.assertEqual(part.unknown_label.winfo_manager(), "")
        part._format.set("{ny}/Season {s}/{s00e00}")
        self.assertEqual(
            part.format_sample.cget("text"), r"VisionQuest (2026)\Season 01\S01E02.mkv"
        )

    async def test_a_film_preset_samples_the_film_and_auto_both(self) -> None:
        _dialog, part, _forge = await self.ready()
        self.select(part, "Movie (Folder)")
        self.assertEqual(
            part.format_sample.cget("text"), r"Dune Part Two (2024)\Dune Part Two (2024).mkv"
        )
        part.set_type(TypeHint.AUTO)
        self.assertEqual(
            part.format_sample.cget("text").splitlines(),
            [
                r"VisionQuest (2026)\VisionQuest (2026).mkv",
                r"Dune Part Two (2024)\Dune Part Two (2024).mkv",
            ],
        )

    async def test_an_unknown_code_is_flagged(self) -> None:
        _dialog, part, _forge = await self.ready()
        part._format.set("{ny} {codec} {Year}")
        self.assertTrue(part.unknown_label.winfo_manager())
        self.assertEqual(
            part.unknown_label.cget("text"), "⚠ Not a known code: {codec}, {Year}"
        )
        part._format.set("{ny}")
        self.assertEqual(part.unknown_label.winfo_manager(), "")

    # --- the code buttons under the format --------------------------------------------------------

    async def test_the_codes_table_in_order(self) -> None:
        _dialog, part, _forge = await self.ready()
        self.select(part, "Series (Flat)")
        self.assertEqual(part.codes_caption.cget("text"), "Codes: click one to add it")
        self.assertEqual(
            [
                (row.left_label.cget("text"), row.right_label.cget("text"), row.value)
                for row in part.code_table.rows
            ],
            [
                ("ny", "VisionQuest (2026)", "{ny}"),
                ("t", "VisionQuest", "{t}"),
                ("y", "2026", "{y}"),
                ("s00e00", "S01E02", "{s00e00}"),
                ("sxe", "1x02", "{sxe}"),
                ("s", "01", "{s}"),
                ("e", "02", "{e}"),
                ("en", "Theseus", "{en}"),
                ("quality", "1080p H.265", "{quality}"),
                ("/", "starts a new folder", "/"),
                ("-", "a dash", " - "),
            ],
        )

    async def test_the_tables_follow_the_type(self) -> None:
        dialog, part, _forge = await self.ready()
        self.select(part, "Movie (Flat)")
        dialog.update()
        shown = [row for row in part.code_table.rows if row.winfo_manager()]
        self.assertEqual(
            [(row.left_text, row.right_text) for row in shown],
            [
                ("ny", "Dune Part Two (2024)"),
                ("t", "Dune Part Two"),
                ("y", "2024"),
                ("quality", "1080p H.265"),
                ("/", "starts a new folder"),
                ("-", "a dash"),
            ],
        )
        examples = [row.value for row in part.example_table.rows if row.winfo_manager()]
        self.assertEqual(examples, list(settings_naming.MOVIE_EXAMPLES))
        part.set_type(TypeHint.TV)
        examples = [row.value for row in part.example_table.rows if row.winfo_manager()]
        self.assertEqual(examples, list(settings_naming.SHOW_EXAMPLES))
        self.assertEqual(sum(1 for r in part.code_table.rows if r.winfo_manager()), 11)
        self.assertEqual(part.code_table.rows[0].right_text, "VisionQuest (2026)")
        part.set_type(TypeHint.AUTO)
        examples = [row.value for row in part.example_table.rows if row.winfo_manager()]
        self.assertEqual(
            examples, [*settings_naming.MOVIE_EXAMPLES, *settings_naming.SHOW_EXAMPLES]
        )

    async def test_the_examples_results_and_a_click_uses_one(self) -> None:
        _dialog, part, forge = await self.ready()
        self.select(part, "Series (Flat)")
        rows = {row.value: row for row in part.example_table.rows}
        self.assertEqual(rows["{ny}"].right_text, "Dune Part Two (2024).mkv")
        self.assertEqual(rows["{t} {sxe} - {en}"].right_text, "VisionQuest 1x02 - Theseus.mkv")
        self.assertEqual(rows["{t} - {s00e00}"].right_label.cget("text"), "VisionQuest - S01E02.mkv")
        part._format.set("{ny}/typed by hand")
        part.format_entry.icursor(0)
        with mock.patch.object(part.format_entry, "focus_set") as focus_set:
            result = rows["{t} - {s00e00}"]._clicked(cast(Any, None))
        focus_set.assert_called_once_with()
        self.assertEqual(result, "break")
        self.assertEqual(part.format_entry.get(), "{t} - {s00e00}")
        self.assertEqual(part.format_entry.index("insert"), len("{t} - {s00e00}"))
        assert part.editing is not None
        self.assertEqual(part.editing.preset.format, "{t} - {s00e00}")
        self.assertEqual(part.format_sample.cget("text"), "VisionQuest - S01E02.mkv")
        await settle()  # a click on an example is a choice: stored at once
        stored = {preset.name: preset for preset in self.stored(forge).stored}
        self.assertEqual(stored["Series (Flat)"].format, "{t} - {s00e00}")

    async def test_fit_middle_keeps_the_end(self) -> None:
        part_font = theme.font(13)
        text = r"VisionQuest (2026)\Season 01\VisionQuest (2026) - S01E02 - Theseus.mkv"
        self.assertEqual(settings_naming.fit_middle(text, part_font, 10_000), text)
        fitted = settings_naming.fit_middle(text, part_font, 200)
        self.assertIn("…", fitted)
        self.assertTrue(fitted.endswith("Theseus.mkv"))
        self.assertLessEqual(part_font.measure(fitted), 200)

    async def test_every_code_and_example_is_known_to_the_formatter(self) -> None:
        from app.forge.core.formatter import KNOWN_VARIABLES

        self.assertEqual(
            {code.shown for code in settings_naming.FORMAT_CODES} - {"/", "-"},
            set(KNOWN_VARIABLES),
        )
        for fmt in (*settings_naming.MOVIE_EXAMPLES, *settings_naming.SHOW_EXAMPLES):
            self.assertEqual(sample_lines(fmt, TypeHint.AUTO)[1], [], fmt)

    async def test_a_cell_too_long_is_cut_and_its_tip_shows_it_whole(self) -> None:
        # At the narrowest page (a 1366 x 768 screen at 150 %): the longest example doesn't fit.
        with mock.patch(
            "app.ui.settings_dialog.fitting_page_width", return_value=NARROW_PAGE_WIDTH
        ):
            dialog, part, _forge = await self.ready()
        self.select(part, "Series (Flat)")
        part.codes_fold.set_open(True)  # "? Codes": the tables show
        dialog.update()
        row = part.example_table.rows[-1]  # the longest
        shown = row.right_label.cget("text")
        self.assertIn("…", shown)
        self.assertTrue(shown.endswith("S01E02 - Theseus.mkv"))  # the file name stays in sight
        self.assertEqual(row._tip.text, f"{row.left_text}\n{row.right_text}")
        self.assertEqual(part.code_table.rows[0]._tip.text, "")  # fits: no tip
        room = part.format_area.winfo_width()
        for table in (part.code_table, part.example_table):
            self.assertEqual(table.winfo_width(), room)  # nothing pushed the box wider

    async def test_the_format_spans_the_box_below_the_list(self) -> None:
        dialog, part, _forge = await self.ready()
        dialog.update()
        self.assertGreaterEqual(
            part.format_area.winfo_rooty(),
            part.preset_list.winfo_rooty() + part.preset_list.winfo_height(),
        )
        self.assertEqual(part.format_area.winfo_rootx(), part.preset_list.winfo_rootx())
        self.assertEqual(part.format_area.winfo_width(), part.format_entry.winfo_width())

    async def test_a_click_adds_at_the_cursor(self) -> None:
        _dialog, part, forge = await self.ready()
        self.select(part, "Series (Flat)")
        part._format.set("{ny}{en}")
        part.format_entry.icursor(4)
        self.click(part, " - ")
        self.assertEqual(part.format_entry.get(), "{ny} - {en}")
        self.assertEqual(part.format_entry.index("insert"), 7)
        self.click(part, "{s00e00}")
        self.assertEqual(part.format_entry.get(), "{ny} - {s00e00}{en}")
        self.assertEqual(part.format_entry.index("insert"), 15)
        # The preset and its sample follow, as when typing; stored on Save only.
        assert part.editing is not None
        self.assertEqual(part.editing.preset.format, "{ny} - {s00e00}{en}")
        self.assertEqual(
            part.format_sample.cget("text"), "VisionQuest (2026) - S01E02Theseus.mkv"
        )
        self.assertEqual(self.stored(forge).replaced, [])

    async def test_a_click_replaces_the_selection_with_the_cursor_after_it(self) -> None:
        _dialog, part, _forge = await self.ready()
        part._format.set("{ny}/{t}/{e}")
        part.format_entry.icursor(0)  # the cursor elsewhere than the selection's end
        part.format_entry.select_range(5, 8)
        self.click(part, "{s00e00}")
        self.assertEqual(part.format_entry.get(), "{ny}/{s00e00}/{e}")
        self.assertEqual(part.format_entry.index("insert"), 13)
        self.assertFalse(part.format_entry.select_present())
        assert part.editing is not None
        self.assertEqual(part.editing.preset.format, "{ny}/{s00e00}/{e}")

    async def test_the_selection_and_cursor_survive_losing_focus(self) -> None:
        _dialog, part, _forge = await self.ready()
        part._format.set("{ny}{t}")
        part.format_entry.focus_set()
        part.format_entry.select_range(4, 7)
        part.format_entry.icursor(7)
        part.name_entry.focus_set()
        await pump(self.root, 0.02)
        part.insert_code("{en}")
        self.assertEqual(part.format_entry.get(), "{ny}{en}")
        self.assertEqual(part.format_entry.index("insert"), 8)

    async def test_a_click_puts_the_keyboard_in_the_field(self) -> None:
        _dialog, part, _forge = await self.ready()
        with mock.patch.object(part.format_entry, "focus_set") as focus_set:
            result = part.code_table.rows[0]._clicked(cast(Any, None))
        focus_set.assert_called_once_with()
        self.assertEqual(result, "break")  # CTk's click on "all" would take the focus back

    async def test_all_of_a_row_is_clickable(self) -> None:
        _dialog, part, _forge = await self.ready()
        row = part.code_table.rows[0]
        parts = (
            row,
            row._canvas,
            row.left_label._label,
            row.left_label._canvas,
            row.right_label._label,
            row.right_label._canvas,
        )
        for widget in parts:
            bound = tk.Misc.bind(widget)
            for sequence in ("<Button-1>", "<Enter>", "<Leave>"):
                self.assertIn(sequence, bound)
            self.assertEqual(str(widget.tk.call(str(widget), "cget", "-cursor")), "hand2")

    async def test_a_click_without_a_preset_does_nothing(self) -> None:
        _dialog, part, _forge = await self.ready()
        before = part.format_entry.get()
        part._editing = None
        part.insert_code("{t}")
        self.assertEqual(part.format_entry.get(), before)

    async def test_the_row_keeps_its_hover_colour_between_its_parts(self) -> None:
        dialog, part, _forge = await self.ready()
        dialog.update()
        chip = part.code_table.rows[0]
        look = app_look()
        self.assertEqual(chip.cget("fg_color"), look.SURFACE)
        chip._entered(cast(Any, None))
        self.assertEqual(chip.cget("fg_color"), look.CHIP_HOVER)
        x, y = chip.winfo_rootx(), chip.winfo_rooty()
        chip._left(cast(Any, SimpleNamespace(x_root=x, y_root=y)))  # on to the label
        self.assertEqual(chip.cget("fg_color"), look.CHIP_HOVER)
        chip._left(cast(Any, SimpleNamespace(x_root=x - 1, y_root=y)))  # off the row
        self.assertEqual(chip.cget("fg_color"), look.SURFACE)

    async def test_dutch_tables(self) -> None:
        i18n.set_language(Language.NL)
        _dialog, part, _forge = await self.ready()
        self.assertEqual(part.codes_caption.cget("text"), "Codes: klik erop om toe te voegen")
        self.assertEqual(
            part.examples_caption.cget("text"), "Voorbeelden: klik erop om te gebruiken"
        )
        rows = {row.value: row for row in part.code_table.rows}
        self.assertEqual(rows["/"].right_text, "begint een nieuwe map")
        self.assertEqual(rows[" - "].right_text, "een streepje")
    async def test_a_new_preset_is_stored_at_once_and_each_edit_after(self) -> None:
        dialog, part, forge = await self.ready()
        part.new_button.invoke()
        assert part.editing is not None
        self.assertEqual(part.editing.preset.name, "New preset")
        self.assertEqual(part.preset_buttons[-1].cget("fg_color"), app_look().SELECTED)
        await settle()
        self.assertEqual(self.stored(forge).names[-1], "New preset")  # at once
        part._name.set("Mine")
        self.assertEqual(self.stored(forge).names[-1], "New preset")  # typing stores nothing
        self.assertEqual(part.preset_buttons[-1].cget("text"), "Mine")
        await self.typed(part)
        part.set_type(TypeHint.TV)  # at once
        await settle()
        part._format.set("{t} {s00e00}")
        await self.typed(part)
        part.set_option("season_poster", True)  # at once
        await settle()
        mine = Preset(
            name="Mine",
            type=TypeHint.TV,
            format="{t} {s00e00}",
            options=PresetOptions(season_poster=True),
        )
        self.assertEqual(self.stored(forge).stored, [*DEFAULT_PRESETS, mine])
        self.assertEqual(len(self.stored(forge).replaced), 5)  # one store per change
        # The right-click menu is told each time (MediaForge: on_presets_changed).
        self.assertEqual(self.told[0], PresetsChange((*self.stored(forge).names[:-1], "New preset"), {}, ()))
        self.assertEqual(self.told[1].renamed, {"New preset": "Mine"})
        self.assertFalse(part.presets_changed())  # what is stored is what the page shows
        self.assertEqual(self.settings(forge).updates, [])  # no setting changed
        bar = dialog.committer.bar
        assert bar is not None
        self.assertTrue(bar.shown)

    async def test_undo_puts_the_presets_back(self) -> None:
        dialog, part, forge = await self.ready()
        assert forge.parts is not None
        rules = cast(FakeRules, forge.parts.rules)
        self.select(part, "Movie (Flat)")
        part._name.set("Films")
        await self.typed(part)
        self.assertIn("Films", self.stored(forge).names)
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual(self.stored(forge).stored, list(DEFAULT_PRESETS))
        self.assertEqual(rules.renamed, [{"Movie (Flat)": "Films"}, {"Films": "Movie (Flat)"}])
        self.assertEqual(self.told[-1].renamed, {"Films": "Movie (Flat)"})
        assert part.editing is not None
        self.assertEqual(part.editing.preset.name, "Movie (Flat)")  # still edited, as it was
        self.assertEqual(part._name.get(), "Movie (Flat)")

    async def test_undo_keeps_what_was_typed_since_the_change(self) -> None:
        # Round 2, 8 (Task 2 M8): only a field still showing the undone value is refilled.
        dialog, part, forge = await self.ready()
        self.select(part, "Movie (Flat)")
        part._format.set("{n} [{y}]")
        await self.typed(part)  # the change Undo takes back: the format
        self.assertEqual(self.stored(forge).stored[2].format, "{n} [{y}]")
        part._name.set("Films")  # typed since, not stored yet
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual(self.stored(forge).stored, list(DEFAULT_PRESETS))
        self.assertEqual(part._format.get(), "{ny}")  # refilled: it showed the undone value
        self.assertEqual(part._name.get(), "Films")  # kept as typed
        assert part.editing is not None
        self.assertEqual(part.editing.origin, "Movie (Flat)")  # still the one edited
        self.assertTrue(part.name_field.pending)
        await self.typed(part)  # left or Enter: the typed name is stored on the right preset
        self.assertEqual(self.stored(forge).names[2], "Films")
        self.assertEqual(self.stored(forge).stored[2].format, "{ny}")

    async def test_undo_of_a_rename_keeps_a_name_typed_since(self) -> None:
        dialog, part, forge = await self.ready()
        self.select(part, "Movie (Flat)")
        part._name.set("Films")
        await self.typed(part)
        part._name.set("Films 4K")  # typed on, not stored
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual(self.stored(forge).names[2], "Movie (Flat)")
        self.assertEqual(part._name.get(), "Films 4K")
        assert part.editing is not None
        self.assertEqual(part.editing.origin, "Movie (Flat)")
        await self.typed(part)
        self.assertEqual(self.stored(forge).names[2], "Films 4K")

    async def test_a_second_new_preset_gets_a_number(self) -> None:
        _dialog, part, forge = await self.ready()
        part.new_preset()
        part.new_preset()
        self.assertEqual(
            [d.preset.name for d in part._drafts][-2:], ["New preset", "New preset 2"]
        )
        await settle()
        self.assertEqual(self.stored(forge).names[-2:], ["New preset", "New preset 2"])

    async def test_rename_preset(self) -> None:
        _dialog, part, forge = await self.ready()
        self.select(part, "Movie (Flat)")
        part._name.set("Films")
        await self.typed(part)
        names = self.stored(forge).names
        self.assertIn("Films", names)
        self.assertNotIn("Movie (Flat)", names)
        self.assertEqual(names.index("Films"), 2)  # in its place
        self.assertEqual(self.told[0].renamed, {"Movie (Flat)": "Films"})
        assert part.editing is not None
        self.assertEqual(part.editing.preset.name, "Films")  # still edited after storing

    async def test_a_rename_reaches_the_rules(self) -> None:
        _dialog, part, forge = await self.ready()
        assert forge.parts is not None
        rules = cast(FakeRules, forge.parts.rules)
        self.select(part, "Movie (Folder)")
        part._name.set("Films 4K")
        await self.typed(part)
        self.assertEqual(rules.renamed, [{"Movie (Folder)": "Films 4K"}])
        part._format.set("{t}")  # a change without a rename tells the rules nothing
        await self.typed(part)
        self.assertEqual(len(rules.renamed), 1)

    async def test_the_library_choice_follows_a_renamed_preset(self) -> None:
        _dialog, part, forge = await self.ready()
        self.select(part, "Series (Folder)")
        part._name.set("Shows")
        self.assertEqual(part.choice_menus["series_preset"].get(), "Shows")
        self.assertIn("Shows", part.choice_menus["series_preset"].cget("values"))
        await self.typed(part)
        self.assertEqual(self.settings(forge).updates, [{"series_preset": "Shows"}])
        self.assertEqual(self.stored(forge).names[3], "Shows")

    async def test_delete_hands_the_choice_to_the_same_type(self) -> None:
        _dialog, part, forge = await self.ready()
        self.select(part, "Movie (Folder)")
        part.delete_button.invoke()
        assert part.editing is not None
        self.assertEqual(part.editing.preset.name, "Movie (Flat)")  # the next one
        self.assertEqual(part.choice_menus["movies_preset"].get(), "Movie (Flat)")
        await settle()  # at once
        self.assertNotIn("Movie (Folder)", self.stored(forge).names)
        self.assertEqual(self.settings(forge).updates, [{"movies_preset": "Movie (Flat)"}])
        self.assertEqual(self.told[0].removed, ("Movie (Folder)",))

    async def test_the_inbox_preset_follows_a_rename(self) -> None:
        _dialog, part, forge = await self.ready(ForgeSettings(last_preset="Movie (Flat)"))
        self.select(part, "Movie (Flat)")
        part._name.set("Films")
        await self.typed(part)
        self.assertEqual(self.settings(forge).updates, [{"last_preset": "Films"}])

    async def test_the_inbox_preset_stays_when_another_is_renamed(self) -> None:
        _dialog, part, forge = await self.ready(ForgeSettings(last_preset="Auto"))
        self.select(part, "Movie (Flat)")
        part._name.set("Films")
        await self.typed(part)
        self.assertEqual(self.settings(forge).updates, [])
        self.assertEqual(self.settings(forge).current.last_preset, "Auto")

    async def test_a_deleted_inbox_preset_goes_to_the_first_of_its_type(self) -> None:
        _dialog, part, forge = await self.ready(ForgeSettings(last_preset="Series (Flat)"))
        self.select(part, "Series (Flat)")
        part.delete_preset()
        await settle()
        self.assertEqual(self.settings(forge).updates, [{"last_preset": "Series (Folder)"}])

    async def test_a_deleted_inbox_preset_with_none_of_its_type_left_is_cleared(self) -> None:
        presets = [
            Preset(name="Auto", type=TypeHint.AUTO, format="{ny}"),
            Preset(name="Films", type=TypeHint.MOVIE, format="{ny}"),
        ]
        _dialog, part, forge = await self.ready(ForgeSettings(last_preset="Films"), presets)
        self.select(part, "Films")
        part.delete_preset()
        await settle()
        self.assertEqual(self.settings(forge).updates, [{"last_preset": None}])

    async def test_the_last_preset_cannot_be_deleted(self) -> None:
        only = Preset(name="Only", type=TypeHint.AUTO, format="{ny}")
        _dialog, part, forge = await self.ready(presets=[only])
        self.assertEqual(str(part.delete_button.cget("state")), "disabled")
        self.assertIsNone(part.delete_preset())
        self.assertEqual(part.presets, [only])
        await settle()
        self.assertEqual(self.stored(forge).replaced, [])

    async def test_a_preset_needs_a_name_of_its_own(self) -> None:
        _dialog, part, forge = await self.ready()
        part._name.set("  ")
        self.assertEqual(part.name_problem.cget("text"), "Give this preset a name.")
        self.assertTrue(part.name_problem.winfo_manager())
        await self.typed(part)
        self.assertEqual(part._presets_box.refusal, "Give every preset a name.")  # in its box
        self.assertEqual(self.stored(forge).replaced, [])
        self.assertEqual(part._name.get(), "  ")  # what was typed stays
        part._name.set("series (flat)")  # the same name in other letters
        self.assertEqual(part.name_problem.cget("text"), "Another preset is called that already.")
        await self.typed(part)
        self.assertIn("series (flat)", part._presets_box.refusal.lower())
        part._name.set("Plain")
        self.assertEqual(part.name_problem.winfo_manager(), "")
        await self.typed(part)
        self.assertEqual(self.stored(forge).names[0], "Plain")
        self.assertEqual(part._presets_box.refusal, "")

    async def test_a_refused_name_typed_back_loses_its_red_line(self) -> None:
        # Round 2, 2: the name field typed back to the stored name after a refusal.
        dialog, part, forge = await self.ready()
        part._name.set("series (flat)")  # another preset's name
        await self.typed(part)
        self.assertNotEqual(part._presets_box.refusal, "")
        part._name.set("Auto")  # back to what is stored
        await self.typed(part)
        self.assertEqual(part._presets_box.refusal, "")
        self.assertEqual(self.stored(forge).replaced, [])  # nothing was stored
        self.assertFalse(dialog.committer.can_undo)

    async def test_closing_stores_what_was_typed_and_stops_listening(self) -> None:
        dialog, part, forge = await self.ready(matches=[OFFICE])
        part._name.set("Renamed")
        dialog.close()  # ✕ and Escape: a field's text is stored as leaving it would
        await settle()
        self.assertEqual(self.stored(forge).names[0], "Renamed")
        # It stopped listening (the Library section's listener goes with it too).
        self.assertEqual(self.settings(forge).listeners, [])

    async def test_an_unchanged_field_stores_nothing(self) -> None:
        _dialog, part, forge = await self.ready(matches=[OFFICE])
        part._name.set("Auto")  # typed over with the same name
        await self.typed(part)
        self.assertEqual(self.stored(forge).replaced, [])
        self.assertEqual(self.settings(forge).updates, [])
        self.assertEqual(self.told, [])

    async def test_a_preset_changed_elsewhere_meanwhile_is_kept(self) -> None:
        _dialog, part, forge = await self.ready()
        self.select(part, "Auto")
        part._format.set("{t}")
        # Meanwhile (the old MediaForge's import, say) another preset changes and one is added.
        presets = self.stored(forge)
        flat = Preset(name="Series (Flat)", type=TypeHint.TV, format="{t} {s00e00}")
        extra = Preset(name="Extra", type=TypeHint.MOVIE, format="{ny}")
        presets.stored = [p if p.name != flat.name else flat for p in presets.stored] + [extra]
        await self.typed(part)
        stored = {p.name: p for p in presets.stored}
        self.assertEqual(stored["Auto"].format, "{t}")
        self.assertEqual(stored["Series (Flat)"], flat)
        self.assertIn("Extra", stored)
        self.assertEqual([b.cget("text") for b in part.preset_buttons][-1], "Extra")

    async def test_a_failed_store_says_so_in_its_box(self) -> None:
        dialog, part, forge = await self.ready()
        part._name.set("Changed")
        self.stored(forge).fail = True
        with self.assertLogs("app.forge.ui.settings_naming", "ERROR") as logged:
            await self.typed(part)
        self.assertEqual(part._presets_box.refusal, i18n.t(SAVE_FAILED))
        self.assertNotIn("Alex", logged.output[0])  # the type only, never a path
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(self.told, [])
        self.assertTrue(part.presets_changed())  # still to be stored

    async def test_a_failing_listener_does_not_fail_the_store(self) -> None:
        async def broken(change: PresetsChange) -> None:
            raise RuntimeError(r"HKCU\Software\Classes\*\shell\MediaForge")

        self.addCleanup(on_presets_saved(broken))
        _dialog, part, forge = await self.ready()
        part._name.set("Changed")
        with self.assertLogs("app.forge.ui.settings_naming", "ERROR") as logged:
            await self.typed(part)
        self.assertNotIn("HKCU", logged.output[0])
        self.assertEqual(self.stored(forge).names[0], "Changed")
        self.assertEqual(len(self.told), 1)

    async def test_long_names_are_cut_in_the_list(self) -> None:
        long = Preset(name="A very long preset name for my anime series", type="tv", format="{ny}")
        _dialog, part, _forge = await self.ready(presets=[long])
        shown = part.preset_buttons[0].cget("text")
        self.assertTrue(shown.endswith("…"))
        self.assertTrue(long.name.startswith(shown[:-1]))
        self.assertEqual(part.name_entry.get(), long.name)  # the editor has it whole

    # --- the names language ---------------------------------------------------------------------

    async def test_the_names_language_is_stored_at_once(self) -> None:
        dialog, part, forge = await self.ready()
        self.assertEqual(part.language_menu.get(), "English (US)")
        self.assertEqual(len(part.language_menu.cget("values")), len(settings_naming.LANGUAGES))
        part._language_chosen("Dutch")  # picked in the menu
        await settle()
        self.assertEqual(self.settings(forge).updates, [{"tmdb_language": "nl-NL"}])
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual(self.settings(forge).current.tmdb_language, "en-US")
        self.assertEqual(part.language_menu.get(), "English (US)")

    async def test_a_language_not_in_the_list_shows_as_it_is(self) -> None:
        _dialog, part, forge = await self.ready(ForgeSettings(tmdb_language="ja-JP"))
        self.assertEqual(part.language_menu.get(), "ja-JP")
        self.assertIn("ja-JP", part.language_menu.cget("values"))
        await settle()
        self.assertEqual(self.settings(forge).updates, [])

    async def test_a_choice_for_new_rules_is_stored_and_one_made_elsewhere_shows(self) -> None:
        _dialog, part, forge = await self.ready()
        task = part.choose_preset_for("movies_preset", "Movie (Flat)")
        assert task is not None
        await task
        self.assertEqual(self.settings(forge).updates, [{"movies_preset": "Movie (Flat)"}])
        await self.settings(forge).update(tmdb_language="fr-FR", movies_preset="Auto")
        self.assertEqual(part.language_menu.get(), "French")
        self.assertEqual(part.choice_menus["movies_preset"].get(), "Auto")

    # --- remembered matches -----------------------------------------------------------------------

    async def test_matches_can_be_forgotten(self) -> None:
        dialog, part, forge = await self.ready(matches=[OFFICE, DUNE])
        self.assertEqual(part.match_summary.cget("text"), "2 matches remembered")
        window = self.open_matches(part)
        texts = labels(window.match_list)
        self.assertEqual(texts, [match_text(OFFICE), match_text(DUNE)])
        forget = [
            w
            for w in window.match_list.winfo_children()
            if isinstance(w, ctk.CTkButton) and w.cget("text") == i18n.t(FORGET)
        ]
        forget[0].invoke()
        await settle()  # at once
        self.assertEqual(labels(window.match_list), [match_text(DUNE)])
        self.assertEqual(part.match_summary.cget("text"), "1 match remembered")
        self.assertEqual(self.remembered(forge).stored, [DUNE])
        self.assertEqual(self.remembered(forge).forgotten, [OFFICE])
        window.close()
        self.assertTrue(await dialog.committer.run_undo())  # remembered again
        self.assertEqual(self.remembered(forge).stored, [DUNE, OFFICE])
        self.assertEqual(part.match_summary.cget("text"), "2 matches remembered")

    async def test_the_window_opens_once_and_closing_it_keeps_the_form(self) -> None:
        _dialog, part, forge = await self.ready(matches=[OFFICE])
        window = self.open_matches(part)
        part.open_matches()
        self.assertIs(part.matches_window, window)
        window.done_button.invoke()
        self.assertTrue(window.closed)
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self.remembered(forge).forgotten, [])
        again = self.open_matches(part)
        self.assertIsNot(again, window)
        again.close()

    async def test_clear_all_is_stored_at_once(self) -> None:
        _dialog, part, forge = await self.ready(matches=[OFFICE])
        window = self.open_matches(part)
        window.clear_button.invoke()
        window.close()
        await settle()
        self.assertEqual(part.match_summary.cget("text"), "None yet")
        self.assertEqual(self.remembered(forge).stored, [])

    @staticmethod
    def open_matches(part: Any) -> Any:
        part.manage_button.invoke()
        window = part.matches_window
        assert window is not None
        return window

    async def test_clear_all(self) -> None:
        dialog, part, forge = await self.ready(matches=[OFFICE, DUNE])
        window = self.open_matches(part)
        window.clear_button.invoke()
        await settle()
        self.assertEqual(labels(window.match_list), ["None yet"])
        self.assertEqual(str(window.clear_button.cget("state")), "disabled")
        window.close()
        await settle()
        self.assertEqual(str(part.manage_button.cget("state")), "disabled")
        self.assertEqual(self.remembered(forge).stored, [])

    async def test_no_matches_yet(self) -> None:
        _dialog, part, _forge = await self.ready()
        self.assertEqual(part.match_summary.cget("text"), "None yet")
        self.assertEqual(str(part.manage_button.cget("state")), "disabled")

    # --- before Forge has started -----------------------------------------------------------------

    async def test_waits_for_forge_to_start(self) -> None:
        forge = FakeForge(FakeSettings(ForgeSettings()), started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog = self.dialog(forge)
        part = self.section(dialog)
        await pump(self.root, 0.05)
        self.assertFalse(part.ready)
        self.assertIn("Getting MediaForge ready…", labels(dialog.page(PAGE_NAMING)))
        self.assertNotIn("Presets", labels(dialog.page(PAGE_NAMING)))
        self.assertIsNone(part.new_preset())  # nothing to add to yet
        forge.gate.set_result(None)
        await pump_until(self.root, lambda: part.ready)
        texts = labels(dialog.page(PAGE_NAMING))
        self.assertNotIn("Getting MediaForge ready…", texts)
        self.assertIn("Presets", texts)

    async def test_not_ready_says_why(self) -> None:
        dialog = self.dialog(FakeForge(None, started=False))
        part = self.section(dialog)
        await pump_until(self.root, lambda: NOT_READY_TEXT in labels(dialog.page(PAGE_NAMING)))
        self.assertFalse(part.ready)
        self.assertIsNone(part.commit_presets())

    async def test_presets_that_cannot_be_read_say_so(self) -> None:
        forge = FakeForge(FakeSettings(ForgeSettings()))
        assert forge.parts is not None
        forge.parts.presets.list = mock.AsyncMock(side_effect=OSError("database is locked"))
        with self.assertLogs(level="ERROR") as logged:  # the Right-click menu's section too
            dialog = self.dialog(forge)
            part = self.section(dialog)
            await pump_until(
                self.root, lambda: i18n.t(LOAD_FAILED) in labels(dialog.page(PAGE_NAMING))
            )
        self.assertIn("app.forge.ui.settings_naming", [r.name for r in logged.records])
        self.assertFalse(part.ready)

    # --- Dutch ------------------------------------------------------------------------------------

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        dialog, part, _forge = await self.ready(matches=[OFFICE])
        texts = labels(dialog.page(PAGE_NAMING))
        self.assertEqual(texts[0], "Naamgeving")
        self.assertIn("Voorinstellingen", texts)
        self.assertIn("Onthouden keuzes", texts)
        self.assertEqual(part.type_switch.get(), "Auto")
        self.select(part, "Movie (Flat)")
        self.assertEqual(part.type_switch.get(), "Film")
        self.assertEqual(part.language_menu.get(), "Engels (VS)")
        self.assertEqual(part.match_summary.cget("text"), "1 keuze onthouden")
        window = self.open_matches(part)
        self.assertEqual(labels(window.match_list), ["the office → The Office (2005) · serie"])
        window.close()
        part.new_preset()
        assert part.editing is not None
        self.assertEqual(part.editing.preset.name, "Nieuwe voorinstelling")


class NamingRulesTests(ForgeTestCase):
    """The pure part: merging onto what is stored, names, what listeners are told."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    @staticmethod
    def preset(name: str, fmt: str = "{ny}", type_hint: TypeHint = TypeHint.AUTO) -> Preset:
        return Preset(name=name, type=type_hint, format=fmt)

    def test_unchanged_storage_gives_the_form_as_it_shows(self) -> None:
        a, b, c = self.preset("A"), self.preset("B"), self.preset("C")
        drafts = [
            PresetDraft("A", a),
            PresetDraft("C", self.preset("C2", "{t}")),
            PresetDraft(None, self.preset("New")),
        ]
        self.assertEqual(
            merged_presets([a, b, c], [a, b, c], drafts),
            [a, self.preset("C2", "{t}"), self.preset("New")],
        )

    def test_two_presets_can_swap_names(self) -> None:
        a, b = self.preset("A", "{t}"), self.preset("B", "{ny}")
        drafts = [
            PresetDraft("A", a.model_copy(update={"name": "B"})),
            PresetDraft("B", b.model_copy(update={"name": "A"})),
        ]
        merged = merged_presets([a, b], [a, b], drafts)
        self.assertEqual([(p.name, p.format) for p in merged], [("B", "{t}"), ("A", "{ny}")])
        change = presets_change([p.name for p in merged], [a, b], drafts)
        self.assertEqual(change.renamed, {"A": "B", "B": "A"})
        self.assertEqual(change.removed, ())

    def test_a_change_made_elsewhere_stays_unless_edited_here(self) -> None:
        a, b = self.preset("A"), self.preset("B")
        elsewhere = [self.preset("A", "{t}"), self.preset("B", "{t}"), self.preset("Z")]
        drafts = [PresetDraft("A", a), PresetDraft("B", self.preset("B", "{s00e00}"))]
        self.assertEqual(
            merged_presets(elsewhere, [a, b], drafts),
            [self.preset("A", "{t}"), self.preset("B", "{s00e00}"), self.preset("Z")],
        )

    def test_edited_here_but_deleted_elsewhere_comes_back_last(self) -> None:
        a, b = self.preset("A"), self.preset("B")
        drafts = [PresetDraft("A", a), PresetDraft("B", self.preset("B", "{t}"))]
        self.assertEqual(merged_presets([a], [a, b], drafts), [a, self.preset("B", "{t}")])

    def test_a_name_taken_elsewhere_meanwhile_keeps_the_first(self) -> None:
        a = self.preset("A")
        drafts = [PresetDraft("A", a), PresetDraft(None, self.preset("new", "{t}"))]
        merged = merged_presets([a, self.preset("New")], [a], drafts)
        self.assertEqual(merged, [a, self.preset("New")])

    def test_deleted_ones_are_told(self) -> None:
        a, b = self.preset("A"), self.preset("B")
        drafts = [PresetDraft("B", b)]
        merged = merged_presets([a, b], [a, b], drafts)
        self.assertEqual(merged, [b])
        self.assertEqual(presets_change(["B"], [a, b], drafts), PresetsChange(("B",), {}, ("A",)))

    def test_the_inbox_preset_after_a_save(self) -> None:
        a, b = self.preset("A"), self.preset("B", type_hint=TypeHint.TV)
        c = self.preset("C", type_hint=TypeHint.TV)
        swap = PresetsChange(("B", "A"), {"A": "B", "B": "A"}, ())
        self.assertEqual(last_preset_after("A", swap, [a, b], [b, a]), "B")
        gone = PresetsChange(("A", "C"), {}, ("B",))
        self.assertEqual(last_preset_after("B", gone, [a, b, c], [a, c]), "C")
        self.assertIsNone(last_preset_after("B", gone, [a, b], [a]))
        self.assertEqual(last_preset_after("A", gone, [a, b], [a]), "A")
        self.assertIsNone(last_preset_after(None, gone, [a, b], [a]))

    def test_names(self) -> None:
        self.assertIsNone(preset_problem(["Auto", "Films"]))
        self.assertEqual(preset_problem(["Auto", ""]), "Give every preset a name.")
        self.assertEqual(
            preset_problem(["Auto", "AUTO"]),
            "Two presets are called “AUTO”. Give each its own name."
        )
        self.assertEqual(new_preset_name(["Auto"]), "New preset")
        self.assertEqual(new_preset_name(["new preset", "New preset 2"]), "New preset 3")

    def test_samples(self) -> None:
        lines, unknown = sample_lines("{ny}/{x}{x}", TypeHint.TV)
        self.assertEqual(lines, [r"VisionQuest (2026)\{x}{x}.mkv"])
        self.assertEqual(unknown, ["x"])

    def test_code_examples_come_from_the_formatter(self) -> None:
        for code, example in (
            ("{ny}", "VisionQuest (2026)"),
            ("{t}", "VisionQuest"),
            ("{y}", "2026"),
            ("{en}", "Theseus"),
            ("{s00e00}", "S01E02"),
            ("{sxe}", "1x02"),
            ("{s}", "01"),
            ("{e}", "02"),
        ):
            self.assertEqual(code_example(code), example)
            lines, _unknown = sample_lines(code, TypeHint.TV)
            self.assertEqual(lines, [example + settings_naming.SAMPLE_EXTENSION])
        self.assertEqual(code_example("{y}", settings_naming.SAMPLE_MOVIE), "2024")
        self.assertEqual(code_example("/"), "starts a new folder")
        self.assertEqual(code_example(" - "), "a dash")

    def test_match_texts(self) -> None:
        self.assertEqual(match_text(OFFICE), "the office → The Office (2005) · show")
        self.assertEqual(match_text(DUNE), "dune (2021) → Dune (2021) · movie")
        skipped = DUNE.model_copy(update={"tmdb_id": None, "label": "Don't use TMDB"})
        i18n.set_language(Language.NL)
        self.assertEqual(match_text(skipped), "dune (2021) → TMDB niet gebruiken · film")

    def test_a_listener_can_disconnect(self) -> None:
        async def listener(change: PresetsChange) -> None:
            pass

        disconnect = on_presets_saved(listener)
        self.assertIn(listener, settings_naming._listeners)
        disconnect()
        disconnect()  # twice is fine
        self.assertNotIn(listener, settings_naming._listeners)

