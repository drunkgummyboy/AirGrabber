"""The Auto-rename page (app/forge/ui/library_page.py): renaming on its own, set up right there as
a guided page: what it does, the watching light, Off / Trial / On with a line each (On from Off
becomes Trial first, as in MediaForge's Settings › Library; Trial and On refused while a step is
missing, saying why), step 1 the folders to watch (Add folder…, Remove), step 2 the library
folders (Browse), step 3 the presets, each saved at once with Settings' safety, and "More in
Settings…"."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import customtkinter as ctk

from app import i18n
from app.forge.core.auto import WatchStatus
from app.forge.models.preset import DEFAULT_PRESETS, Preset
from app.forge.models.settings import ForgeSettings
from app.forge.ui.library_page import LibraryPage, light_colour, save_problem
from app.i18n import Language
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until
from tests.test_accent import contrast

READY = ForgeSettings(
    watch_folders=[r"D:\Downloads\tv", r"\\nas\video\incoming"],
    series_folder=r"\\nas\video\Series",
    movies_folder=r"\\nas\video\Movies",
)


class FakeSettings:
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
        self.current = self.current.model_copy(update=changes)
        for listener in list(self.listeners):
            listener(self.current)
        return self.current

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class FakeRenamer:
    def __init__(self, settings: FakeSettings) -> None:
        self.settings = settings

    def status(self, unreachable: Sequence[Path]) -> WatchStatus:
        mode = self.settings.current.automatic
        if mode == "off":
            return WatchStatus("off", "Not watching")
        return WatchStatus("trial" if mode == "trial" else "watching", f"{mode} status")


class FakePresets:
    def __init__(self, presets: Sequence[Preset] = DEFAULT_PRESETS) -> None:
        self.presets = list(presets)

    async def list(self) -> list[Preset]:
        await asyncio.sleep(0)
        return list(self.presets)


def texts(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found.extend(texts(child))
    return found


def buttons(widget: tk.Misc) -> dict[str, list[ctk.CTkButton]]:
    found: dict[str, list[ctk.CTkButton]] = {}
    stack: list[tk.Misc] = [widget]
    while stack:
        each = stack.pop()
        if isinstance(each, ctk.CTkButton):
            found.setdefault(str(each.cget("text")), []).append(each)
        stack.extend(each.winfo_children())
    return found


class LibraryPageTests(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.more_clicks = 0
        self.chosen: list[str] = []  # what the folder chooser answers next

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def page(self, settings: ForgeSettings = READY) -> tuple[LibraryPage, FakeSettings]:
        fake = FakeSettings(settings)

        def more() -> None:
            self.more_clicks += 1

        def ask(title: str, start: str) -> str:
            return self.chosen.pop(0) if self.chosen else ""

        page = LibraryPage(
            self.root, fake, FakeRenamer(fake), more, presets=FakePresets(), ask_folder=ask
        )
        page.pack(fill="both", expand=True)
        return page, fake

    def marks(self, page: LibraryPage) -> list[str]:
        return [str(m.cget("text")) for m in (page.watch_mark, page.library_mark, page.naming_mark)]

    # --- what it shows ------------------------------------------------------------------------

    async def test_says_what_it_does_and_what_each_mode_means(self) -> None:
        page, _ = self.page()
        shown = texts(page)
        self.assertIn(
            "MediaForge can rename finished downloads by itself and move them into your Series "
            "and Movies folders.",
            shown,
        )
        self.assertEqual(
            [str(label.cget("text")).split(":")[0] for label in page.mode_lines.values()],
            ["Off", "Trial", "On"],
        )
        self.assertEqual(page.mode_lines["off"].cget("text_color"), theme.TEXT)  # the one chosen
        self.assertEqual(page.mode_lines["on"].cget("text_color"), theme.TEXT_MUTED)
        for title in ("Watch these folders", "Put renamed files here", "How files are named"):
            self.assertIn(title, shown)
        self.assertEqual(self.marks(page), ["✓", "✓", "✓"])  # every step done

    async def test_shows_the_folders_in_use(self) -> None:
        page, _ = self.page()
        shown = texts(page)
        for folder in (*READY.watch_folders, READY.series_folder, READY.movies_folder):
            self.assertIn(folder, shown)
        self.assertEqual(page.mode.get(), "Off")
        self.assertEqual(str(page.mode.cget("state")), "normal")
        self.assertEqual(str(page.problems_label.cget("text")), "")

    async def test_nothing_chosen_says_plainly_what_trial_and_on_need(self) -> None:
        page, fake = self.page(ForgeSettings())
        shown = texts(page)
        self.assertIn("None yet", shown)
        self.assertEqual(shown.count("Not chosen yet"), 2)
        self.assertEqual(
            str(page.problems_label.cget("text")),
            "Trial and On need these first:\n"
            "•  Choose a download folder to watch.\n"
            "•  Choose the Series library folder.\n"
            "•  Choose the Movies library folder.",
        )
        self.assertEqual(self.marks(page), ["1", "2", "✓"])
        await page.set_mode("trial")  # refused: nothing saved
        self.assertEqual(fake.updates, [])
        self.assertEqual(page.mode.get(), "Off")

    async def test_watching_light(self) -> None:
        page, _ = self.page()
        self.assertEqual(page.state, "off")
        self.assertEqual(str(page.status_label.cget("text")), "Not watching")
        for state, colour in (
            ("watching", theme.SUCCESS),
            ("trial", theme.WARNING),
            ("unreachable", theme.DANGER),
            ("setup", theme.TEXT_MUTED),
            ("off", theme.TEXT_MUTED),
        ):
            with self.subTest(state=state):
                page.set_watch_status(WatchStatus(state, f"{state} words"))  # type: ignore[arg-type]
                self.assertEqual(page.light.cget("text_color"), colour)
                self.assertEqual(light_colour(state), colour)  # type: ignore[arg-type]
                self.assertEqual(str(page.status_label.cget("text")), f"{state} words")

    # --- the switch -------------------------------------------------------------------------------

    async def test_on_from_off_is_trial_first(self) -> None:
        page, fake = self.page()
        await page.set_mode("on")
        self.assertEqual(fake.updates, [{"automatic": "trial"}])
        self.assertEqual(page.mode.get(), "Trial")
        self.assertIn("Trial first", str(page.note_label.cget("text")))
        self.assertEqual(page.state, "trial")
        self.assertEqual(page.mode_lines["trial"].cget("text_color"), theme.TEXT)
        await page.set_mode("on")
        self.assertEqual(fake.current.automatic, "on")
        self.assertEqual(page.mode.get(), "On")
        self.assertEqual(str(page.note_label.cget("text")), "")
        self.assertEqual(page.state, "watching")

    async def test_click_saves(self) -> None:
        page, fake = self.page()
        page._mode_clicked("Trial")  # pyright: ignore[reportPrivateUsage]
        await pump_until(self.root, lambda: fake.current.automatic == "trial")
        await pump(self.root, 0.05)
        self.assertEqual(str(page.mode.cget("state")), "normal")
        page._mode_clicked("Off")  # pyright: ignore[reportPrivateUsage]
        await pump_until(self.root, lambda: fake.current.automatic == "off")
        self.assertEqual(page.state, "off")

    async def test_failed_save_shows_what_is_in_use(self) -> None:
        page, fake = self.page()
        fake.fail = True
        page.mode.set("Trial")
        with self.assertRaises(OSError):
            await page.set_mode("trial")
        self.assertEqual(page.mode.get(), "Off")
        self.assertEqual(str(page.mode.cget("state")), "normal")

    async def test_switch_text_readable(self) -> None:
        """The Off / Trial / On labels, selected or not, at least 4.5:1 in both looks (the selected
        part's hover is a passing state and not checked)."""
        page, _ = self.page()
        text = page.mode.cget("text_color")
        for dark in (False, True):
            for fill in ("selected_color", "unselected_color", "unselected_hover_color"):
                with self.subTest(dark=dark, fill=fill):
                    ratio = contrast(
                        theme.pick(text, dark=dark), theme.pick(page.mode.cget(fill), dark=dark)
                    )
                    self.assertGreaterEqual(ratio, 4.5)

    # --- step 1: the folders to watch ---------------------------------------------------------------

    async def test_add_and_remove_a_folder_saves_at_once(self) -> None:
        page, fake = self.page(ForgeSettings())
        self.chosen = [r"E:\Torrents\done"]
        self.assertTrue(await page.add_watch_folder())
        self.assertEqual(fake.updates, [{"watch_folders": [r"E:\Torrents\done"]}])
        self.assertIn(r"E:\Torrents\done", texts(page.watch_list))
        self.assertEqual(self.marks(page)[0], "✓")
        self.chosen = [r"e:\torrents\DONE\\"]  # the same folder again: nothing
        self.assertFalse(await page.add_watch_folder())
        self.assertFalse(await page.add_watch_folder())  # the chooser closed: nothing
        self.assertEqual(len(fake.updates), 1)
        [remove] = buttons(page.watch_list)["Remove"]
        remove.invoke()
        await pump_until(self.root, lambda: fake.current.watch_folders == [])
        self.assertIn("None yet", texts(page.watch_list))

    async def test_a_watched_folder_inside_a_library_folder_is_never_saved(self) -> None:
        page, fake = self.page(READY)
        self.chosen = [READY.series_folder + r"\incoming"]
        self.assertFalse(await page.add_watch_folder())
        self.assertEqual(fake.updates, [])
        self.assertEqual(
            str(page.watch_problem.cget("text")),
            "A watched folder can't be inside a library folder, or the other way round.",
        )
        self.assertTrue(await page.remove_watch_folder(READY.watch_folders[0]))
        self.assertEqual(str(page.watch_problem.cget("text")), "")  # a good change clears it

    async def test_while_trial_or_on_a_change_that_leaves_a_step_missing_is_refused(self) -> None:
        page, fake = self.page(READY.model_copy(update={"watch_folders": [r"D:\in"]}))
        await page.set_mode("trial")
        self.assertFalse(await page.remove_watch_folder(r"D:\in"))
        self.assertEqual(fake.current.watch_folders, [r"D:\in"])
        self.assertEqual(
            str(page.watch_problem.cget("text")), "Choose a download folder to watch."
        )
        await page.set_mode("off")  # back to Off: then it may go
        self.assertTrue(await page.remove_watch_folder(r"D:\in"))

    async def test_a_new_folder_while_on_is_trial_first(self) -> None:
        page, fake = self.page(READY.model_copy(update={"automatic": "on"}))
        self.chosen = [r"F:\More"]
        self.assertTrue(await page.add_watch_folder())
        self.assertEqual(fake.current.automatic, "trial")  # nothing moves from it unseen
        self.assertIn("Trial first", str(page.note_label.cget("text")))

    # --- step 2: the library folders ---------------------------------------------------------------

    async def test_browse_saves_a_library_folder(self) -> None:
        page, fake = self.page(ForgeSettings(watch_folders=[r"D:\in"]))
        self.chosen = [r"M:\Films"]
        self.assertTrue(await page.choose_library("movies"))
        self.assertEqual(fake.current.movies_folder, r"M:\Films")
        self.assertEqual(page.folder_labels["movies"].cget("text"), r"M:\Films")
        self.assertEqual(self.marks(page)[1], "2")  # Series still missing
        self.chosen = [r"S:\Series"]
        page.browse_buttons["series"].invoke()
        await pump_until(self.root, lambda: fake.current.series_folder == r"S:\Series")
        self.assertEqual(self.marks(page)[1], "✓")
        self.chosen = [r"D:\in\library"]  # inside the watched folder: never
        self.assertFalse(await page.choose_library("series"))
        self.assertEqual(fake.current.series_folder, r"S:\Series")
        self.assertTrue(str(page.library_problem.cget("text")))

    # --- step 3: the presets -----------------------------------------------------------------------

    async def test_each_kind_picks_its_preset(self) -> None:
        page, fake = self.page()
        await pump_until(self.root, lambda: bool(page.presets))
        self.assertEqual(
            page.preset_menus["series"].cget("values"), ["Auto", "Series (Folder)", "Series (Flat)"]
        )
        self.assertEqual(
            page.preset_menus["movies"].cget("values"), ["Auto", "Movie (Folder)", "Movie (Flat)"]
        )
        self.assertEqual(page.preset_menus["series"].get(), "Series (Folder)")
        self.assertTrue(await page.set_preset("movies", "Movie (Flat)"))
        self.assertEqual(fake.current.movies_preset, "Movie (Flat)")
        self.assertEqual(page.preset_menus["movies"].get(), "Movie (Flat)")

    async def test_a_preset_that_is_gone_still_shows_what_is_stored(self) -> None:
        page, _ = self.page(READY.model_copy(update={"series_preset": "Mine"}))
        await pump_until(self.root, lambda: bool(page.presets))
        self.assertEqual(page.preset_menus["series"].get(), "Mine")
        self.assertEqual(page.preset_menus["series"].cget("values")[0], "Mine")

    # --- the rest ------------------------------------------------------------------------------------

    async def test_changes_from_settings_show(self) -> None:
        page, fake = self.page(ForgeSettings())
        await fake.update(**READY.model_dump(include={"watch_folders", "series_folder"}))
        shown = texts(page)
        self.assertIn(READY.watch_folders[0], shown)
        self.assertEqual(shown.count("Not chosen yet"), 1)
        await fake.update(movies_folder=READY.movies_folder, automatic="on")
        self.assertEqual(page.mode.get(), "On")
        self.assertEqual(page.state, "watching")
        self.assertEqual(str(page.problems_label.cget("text")), "")

    async def test_an_unrelated_save_keeps_the_last_status(self) -> None:
        page, fake = self.page(READY.model_copy(update={"automatic": "on"}))
        page.set_watch_status(WatchStatus("unreachable", "Can't reach"))
        await fake.update(movies_folder="M:/Films")
        self.assertEqual(page.state, "unreachable")
        self.assertEqual(str(page.status_label.cget("text")), "Can't reach")
        await fake.update(watch_folders=READY.watch_folders[:1])
        self.assertEqual(page.state, "watching")

    async def test_a_failed_save_says_so(self) -> None:
        page, fake = self.page(ForgeSettings())
        fake.fail = True
        self.chosen = [r"E:\in"]
        with self.assertLogs("app.forge.ui.library_page", "ERROR") as logs:
            self.assertFalse(await page.add_watch_folder())
        self.assertNotIn("E:", "\n".join(logs.output))  # no folder in the log
        self.assertEqual(
            str(page.watch_problem.cget("text")),
            "That couldn't be saved. The log has the details.",
        )

    async def test_save_problem_is_settings_rule(self) -> None:
        self.assertIsNone(save_problem(ForgeSettings()))  # Off: missing steps may wait
        self.assertEqual(
            save_problem(ForgeSettings(automatic="trial")), "Choose a download folder to watch."
        )
        nested = ForgeSettings(watch_folders=[r"D:\a", r"D:\a\b"])
        self.assertIsNotNone(save_problem(nested))

    async def test_more_in_settings_is_a_small_link(self) -> None:
        page, _ = self.page()
        self.assertEqual(str(page.more_button.cget("text")), "More in Settings…")
        self.assertEqual(page.more_button.cget("fg_color"), "transparent")
        page.more_button.invoke()
        self.assertEqual(self.more_clicks, 1)

    async def test_destroy_unsubscribes(self) -> None:
        page, fake = self.page()
        page.destroy()
        self.assertEqual(fake.listeners, [])
        await fake.update(automatic="trial")  # nobody listens: no error

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        page, _ = self.page(ForgeSettings())
        self.assertEqual(list(page.mode.cget("values")), ["Uit", "Proef", "Aan"])
        self.assertEqual(page.mode.get(), "Uit")
        shown = texts(page)
        self.assertIn("Nog geen", shown)
        self.assertIn("Deze mappen bewaken", shown)
        self.assertTrue(str(page.problems_label.cget("text")).startswith("Voor Proef en Aan"))
        self.assertEqual(str(page.more_button.cget("text")), "Meer bij Instellingen…")
