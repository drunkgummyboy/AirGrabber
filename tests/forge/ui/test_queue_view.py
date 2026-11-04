"""The queue (app/forge/ui/queue_view.py), ported from MediaForge's tests/ui/test_queue_view.py and
redesigned (mockups/forge-chosen.html): files dropped and downloads watched in one list of shows
and films, the All / Needs you / Ready filter, bands whose buttons show under the pointer,
Preview / Forge (each kind with its own preset, in one run) / a band's Rename / Undo, uncertain
matches, the preset card (Shows | Films, Custom, Save to preset, Save as preset…, the option
buttons), the toolbar's progress bar, keys, closing during a run, the 1,000-file smoothness check
and the bands' pictures. Plus AirGrabber's parts: Explorer drops, the Activity panel's lines,
Dutch, the Forge look and what destroy leaves behind.

Every test runs on real Forge services (a temporary forge.db, TMDB without a key so names are
read from the files, HTTP on a MockTransport) and a see-through Tk window."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import sys
import time
import tkinter as tk
import unittest
import weakref
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast
from unittest import mock

import customtkinter as ctk
import httpx
from PIL import Image

from app.config import AppPaths
from app.forge.core import fsops
from app.forge.core.executor import RunResult
from app.forge.core.matcher import Resolution
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.media import MatchState, MediaKind, ResolvedMedia, TypeHint
from app.forge.models.preset import Preset, PresetOptions
from app.forge.services import ForgeServices
from app.forge.ui import file_drops
from app.forge.ui.images import BandImages
from app.forge.ui.look import app_look
from app.forge.ui.match_picker import MatchPicker
from app.forge.ui.queue_rows import Show
from app.forge.ui.queue_view import OPTION_LABELS, QueueView, summary_text
from app.forge.ui.widgets.virtual_list import Row
from app.i18n import Language, current_language, set_language
from app.services.images import Finish, ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from app.ui import theme
from app.ui.missing_control import PopupMenu
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_file_drops import post_drop


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def make_files(folder: Path, names: Sequence[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"video")


class FakeImages:
    """ImageCache's `get`: a grey picture of the asked size, finished as asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.finishes: list[Finish | None] = []
        self.keeps: list[bool] = []

    async def get(
        self,
        url: str | None,
        size: tuple[int, int],
        *,
        finish: Finish | None = None,
        keep: bool = True,
    ) -> Image.Image | None:
        assert url is not None
        self.asked.append(url)
        self.finishes.append(finish)
        self.keeps.append(keep)
        picture = Image.new("RGB", size, (90, 90, 90))
        return finish(picture) if finish is not None else picture


class FakeDrops:
    """file_drops' Win32 side: remembers each window's `deliver`, so a test can drop."""

    def __init__(self) -> None:
        self.targets: dict[int, Callable[[list[str]], None]] = {}

    def attach(self, window_id: int, deliver: Callable[[list[str]], None]) -> bool:
        self.targets[window_id] = deliver
        return True

    def detach(self, window_id: int) -> None:
        self.targets.pop(window_id, None)


class QueueCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.attributes("-alpha", 0.0)  # on screen (laid out, mapped), see-through
        self.root.geometry("1200x800+20+20")
        self.addCleanup(self._destroy_root)
        language = current_language()
        self.addCleanup(set_language, language)
        set_language(Language.EN)

    def _destroy_root(self) -> None:
        PopupMenu._open = None
        for child in list(self.root.winfo_children()):
            with contextlib.suppress(tk.TclError):
                child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow
        http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.addAsyncCleanup(http.aclose)
        tmdb = TMDBClient(http, api_key=lambda: "")  # no key: names come from the files
        images = ImageCache(http, self.data_dir / "posters")
        airgrabber = cast(SettingsService, mock.Mock(spec=SettingsService))
        self.services = await ForgeServices.create(
            AppPaths(self.data_dir), http, tmdb, images, airgrabber
        )
        self.addAsyncCleanup(self.services.aclose)
        self.view = await self.make_view()

    async def make_view(self) -> QueueView:
        view = QueueView(self.root, self.services)
        view.pack(fill="both", expand=True)
        self.addCleanup(self._destroy_view, view)
        await pump_until(self.root, lambda: view.loaded)
        await pump(self.root, 0.05)
        return view

    def _destroy_view(self, view: QueueView) -> None:
        if view.winfo_exists():
            view.destroy()

    async def add_and_settle(self, folder: Path) -> None:
        await self.services.queue.add_paths([folder], recursive=True)
        await self.services.queue.wait_idle()
        await pump(self.root, 0.25)

    def preset(self, name: str) -> Preset:
        return next(p for p in self.view.presets if p.name == name)

    async def choose(self, *names: str) -> None:
        for name in names:
            self.view.select_preset(name)
        await self.view.settled()
        self.view.refresh()

    def rows(self, kind: str) -> list[Row]:
        return [r for r in self.view.list.rows if r.kind == kind]

    def select_band(self, title: str) -> None:
        """Click a band: its first line is selected and the card shows that band."""
        band = self.band(title)
        first = next(r.key for r in self.rows("item") if r.band == band.key)
        self.view.list.select(first)

    def item(self, name: str) -> QueueItem:
        return next(i for i in self.services.queue.items if i.path.name.startswith(name))

    def bands(self) -> list[str]:
        return [r.title for r in self.rows("band")]

    def band(self, title: str) -> Row:
        return next(r for r in self.rows("band") if r.title.startswith(title))

    def sections(self) -> list[str]:
        return [r.title for r in self.rows("section")]

    def names(self, folder: Path) -> list[str]:
        return sorted(p.name for p in folder.iterdir())

    # --- watched downloads ---------------------------------------------------------------------

    async def library(self, automatic: str = "on") -> tuple[Path, Path, Path]:
        downloads = self.tmp / "dl"
        series, movies = self.tmp / "lib" / "Series", self.tmp / "lib" / "Movies"
        for folder in (downloads, series, movies):
            folder.mkdir(parents=True, exist_ok=True)
        # The renamer's own looks stay out of these tests: the queue's buttons drive it.
        await self.services.auto.close()
        await self.services.settings.update(
            watch_folders=[str(downloads)],
            series_folder=str(series),
            movies_folder=str(movies),
            automatic=automatic,
        )
        return downloads, series, movies

    async def watched(self, downloads: Path, *names: str) -> list[int]:
        make_files(downloads, names)
        ids = await self.services.queue.add_watched([(downloads / n, downloads) for n in names])
        await self.services.queue.wait_idle()
        self.view.refresh()
        await pump(self.root, 0.1)
        return ids

    def unsure(self, item: QueueItem) -> None:
        item.resolution = Resolution(media=ResolvedMedia.offline(item.parsed, MatchState.UNCERTAIN))
        item.status = ItemStatus.NEEDS_CHOICE


class QueueTest(QueueCase):
    async def test_empty_state_shown_first(self) -> None:
        self.assertTrue(self.view.empty_state.winfo_ismapped())
        self.assertFalse(self.view.list.winfo_ismapped())
        self.assertEqual(self.view.empty_title.cget("text"), "Drop video files or folders here")
        self.assertEqual(self.view.forge_button.cget("text"), "⚒ Forge")
        self.assertEqual(self.view.example, "Add files to see how they'll be named.")
        self.assertEqual(
            [b.cget("text") for b in self.view.filter_buttons.values()],
            ["All 0", "Needs you 0", "Ready 0"],
        )

    async def test_lists_added_files(self) -> None:
        make_files(self.tmp / "in", [f"Show.Name.S01E0{i}.mkv" for i in range(1, 6)])
        await self.add_and_settle(self.tmp / "in")
        self.assertEqual(len(self.rows("item")), 5)
        self.assertTrue(self.rows("band"))  # show bands, not folder headings
        self.assertTrue(self.view.list.winfo_ismapped())
        self.assertFalse(self.view.empty_state.winfo_ismapped())
        self.assertEqual(self.view.forge_button.cget("text"), "⚒ Forge 5 files")
        self.assertEqual(self.view.filter_buttons["all"].cget("text"), "All 5")
        self.assertEqual(self.view.filter_buttons["ready"].cget("text"), "Ready 5")
        # The first band is selected; the card names it (there is no Shows | Films switch).
        self.assertEqual(self.view.for_label.cget("text"), "For: Show Name · 5 episodes")
        self.assertFalse(hasattr(self.view, "kind_switch"))

    async def test_shows_and_films_are_one_list(self) -> None:
        make_files(
            self.tmp / "in",
            [
                "Ted.Lasso.S04E03.1080p.WEB.mkv",
                "Ted.Lasso.S04E05.1080p.WEB.mkv",
                "Severance.S03E02.2160p.WEB-DL.mkv",
                "Dune.2021.mkv",
            ],
        )
        await self.choose("Series (Folder)")
        await self.add_and_settle(self.tmp / "in")
        self.assertEqual(self.bands(), ["Dune", "Severance", "Ted Lasso"])
        ted = self.band("Ted Lasso")
        self.assertEqual(ted.subtitle, "Season 4 · 2 episodes")
        self.assertTrue(ted.dest.startswith("→ Ted Lasso"))
        rows = self.view.list.rows
        after = rows[rows.index(ted) + 1 : rows.index(ted) + 3]
        self.assertEqual([r.code for r in after], ["S04E03", "S04E05"])
        self.assertEqual(after[0].faint, "Ted.Lasso.S04E03.1080p.WEB.mkv")
        self.assertEqual(ted.actions[1:], (("ignore", "Ignore"), ("rename", "Rename")))
        self.select_band("Dune")  # the card only: the list stays as it is
        await pump(self.root, 0.1)
        self.assertEqual(self.bands(), ["Dune", "Severance", "Ted Lasso"])
        self.assertEqual(self.view.for_label.cget("text"), "For: Dune (2021)")

    async def test_the_filter_shows_needs_you_and_ready(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "in")
        dune = next(i for i in self.services.queue.items if i.path.name.startswith("Dune"))
        self.unsure(dune)
        self.view.refresh()
        self.assertEqual(self.bands(), ["Dune (2021)", "Heat"])  # needs you first
        self.assertEqual(self.view.filter_buttons["needs"].cget("text"), "Needs you 1")
        self.view.set_filter("needs")
        self.assertEqual(self.bands(), ["Dune (2021)"])
        self.view.set_filter("ready")
        self.assertEqual(self.bands(), ["Heat"])
        look = app_look()
        self.assertEqual(tuple(self.view.filter_buttons["ready"].cget("fg_color")), look.ROW_SELECTED)
        self.assertEqual(tuple(self.view.filter_buttons["all"].cget("fg_color")), look.SURFACE)
        self.view.set_filter("needs")
        self.view.services.queue.remove(dune.id)
        self.view.refresh()
        await pump(self.root, 0.1)
        self.assertTrue(self.view.empty_state.winfo_ismapped())
        self.assertEqual(self.view.empty_title.cget("text"), "Nothing needs you")

    async def test_band_buttons_show_under_the_pointer_and_always_when_it_needs_you(
        self,
    ) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "in")
        dune = next(i for i in self.services.queue.items if i.path.name.startswith("Dune"))
        self.unsure(dune)
        self.view.refresh()
        await pump(self.root, 0.1)
        vlist = self.view.list
        self.assertTrue(vlist.hover_actions)
        needs, heat = self.band("Dune"), self.band("Heat")
        vlist.action_point(needs.key, "choose")  # shown without the pointer
        with self.assertRaises(KeyError):
            vlist.action_point(heat.key, "rename")
        vlist.hover_band(heat.key)
        vlist.action_point(heat.key, "rename")
        line = next(r for r in self.rows("item") if r.band == heat.key)
        y = vlist.row_middle(line.key)
        vlist.hover_band(None)
        vlist.canvas.event_generate("<Motion>", x=200, y=y)  # over one of its lines
        await pump(self.root, 0.05)
        vlist.action_point(heat.key, "rename")
        canvas = vlist.canvas
        texts = [
            str(canvas.tk.call(str(canvas), "itemcget", i, "-text"))
            for i in canvas.find_withtag("band-state")
        ]
        self.assertEqual(texts, ["Needs you", "Ready"])

    async def test_custom_rule_renames_the_titles_of_its_kind(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Ted.Lasso.S04E03.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.select_band("Dune")
        self.view._custom_clicked()  # Custom: the rule's box can be typed in
        self.assertEqual(self.view.choice.custom, self.view.choice.preset.format)  # type: ignore[union-attr]
        self.assertEqual(str(self.view.rule_text.cget("state")), "normal")
        box = self.view.rule_text
        box.delete("1.0", "end")
        box.insert("1.0", "{t} [{ny}]")
        await pump(self.root, 0.4)
        self.assertEqual(self.view.choice.custom, "{t} [{ny}]")
        [dune] = [r for r in self.rows("item") if r.badge == "Dune"]
        self.assertEqual(dune.title, "Dune [Dune (2021)].mkv")
        self.assertEqual(self.view.example, "→ Dune [Dune (2021)].mkv")
        ted = next(r for r in self.rows("item") if r.code)  # shows keep their own rule
        self.assertEqual(self.view.choice_for(self.item("Ted")).custom, None)
        self.assertTrue(ted.title)
        codes = self.view.rule_text._textbox.tag_ranges("code")
        self.assertEqual(len(codes), 4)  # {t} and {ny} coloured
        self.assertTrue(self.view.changed_note.winfo_ismapped())
        self.view._custom_clicked()  # off again: back to the preset's rule
        self.assertIsNone(self.view.choice.custom)
        self.assertEqual(str(self.view.rule_text.cget("state")), "disabled")

    async def test_unknown_code_flagged(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.view.set_custom("{ny} {oops}")  # the only band is selected
        await pump(self.root, 0.3)
        self.assertIn("not a known code: {oops}", self.view.example)

    async def test_forge_renames_each_kind_with_its_own_preset_in_one_run(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Ted.Lasso.S04E03.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)", "Series (Flat)")
        dune = self.view.choice_for(self.item("Dune"))
        self.assertEqual(dune.preset, self.preset("Movie (Flat)"))
        await self.view.forge()
        await pump(self.root, 0.2)
        self.assertEqual(
            self.names(self.tmp / "in"),
            ["Dune (2021).mkv", "Ted Lasso - S04E03 - Episode 03.mkv"],
        )
        self.assertTrue(all(i.status is ItemStatus.DONE for i in self.services.queue.items))
        [run] = await self.services.history.list_runs()
        self.assertEqual(run.kinds, {MediaKind.MOVIE, MediaKind.EPISODE})
        self.assertEqual(run.preset_name, "Series (Flat) + Movie (Flat)")
        self.assertEqual(self.view.forge_button.cget("text"), "⚒ Forge")
        last = self.services.activity.lines()[-1]
        self.assertEqual(last.text, "Forged: 2 renamed, 0 already right, 0 failed")
        self.assertTrue(last.success)

    async def test_forge_hands_each_files_media_to_the_move_records(self) -> None:
        await self.services.settings.update(movies_folder=str(self.tmp / "in"))
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        [item] = self.services.queue.items
        media = item.media
        executor = self.services.executor
        with mock.patch.object(executor, "run", wraps=executor.run) as run:
            await self.view.forge()
        self.assertEqual(run.call_args.kwargs["media"], {item.id: media})
        self.assertEqual(run.call_args.kwargs["preset_name"], "Movie (Flat)")
        [move] = await self.services.moves.moves_from(self.tmp / "in")
        self.assertEqual((move.target.name, move.kind), ("Dune (2021).mkv", MediaKind.MOVIE))

    async def test_preview_writes_the_plan_to_the_activity_panel_and_touches_nothing(
        self,
    ) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Ted.Lasso.S04E03.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)", "Series (Flat)")
        before = len(self.services.activity.lines())
        await self.view.preview()
        texts = [line.text for line in self.services.activity.lines()[before:]]
        self.assertEqual(texts[0], "Preview (nothing has been changed):")
        self.assertTrue(any(text.startswith("Rename: Dune.2021.mkv → ") for text in texts))
        self.assertTrue(any("Ted Lasso - S04E03" in text for text in texts))
        self.assertEqual(self.names(self.tmp / "in"), ["Dune.2021.mkv", "Ted.Lasso.S04E03.mkv"])
        self.assertEqual(await self.services.history.list_runs(), [])

    async def test_uncertain_items_prompt_before_forge(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        [item] = self.services.queue.items
        self.unsure(item)
        asked: list[tuple[str, str, Sequence[str]]] = []

        async def answer_cancel(title: str, message: str, choices: Sequence[str]) -> str | None:
            asked.append((title, message, choices))
            return "Cancel"

        self.view.ask = answer_cancel
        await self.view.forge()
        self.assertTrue((self.tmp / "in" / "Dune.2021.mkv").exists())
        [(title, message, choices)] = asked
        self.assertEqual(title, "Some matches are uncertain")
        self.assertIn("about 1 file.", message)
        self.assertEqual(list(choices), ["Review them", "Use top results", "Cancel"])

        async def answer_top(title: str, message: str, choices: Sequence[str]) -> str | None:
            return "Use top results"

        self.view.ask = answer_top
        await self.view.forge()
        self.assertTrue((self.tmp / "in" / "Dune (2021).mkv").exists())

    async def test_review_them_opens_the_picker_for_each(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        for item in self.services.queue.items:
            self.unsure(item)
        picked: list[int] = []

        async def skip_tmdb(master: object, item: QueueItem, services: object, **_: object) -> None:
            picked.append(item.id)
            return None  # "Skip TMDB for this file"

        async def review(title: str, message: str, choices: Sequence[str]) -> str | None:
            return "Review them"

        self.view.ask = review
        with mock.patch.object(MatchPicker, "ask", skip_tmdb):
            await self.view.forge()
        self.assertEqual(sorted(picked), sorted(i.id for i in self.services.queue.items))
        self.assertEqual(self.names(self.tmp / "in"), ["Dune (2021).mkv", "Heat (1995).mkv"])

    async def test_shortcuts_ignored_while_typing(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        started: list[str] = []

        async def fake() -> None:
            started.append("preview")

        self.view.preview = fake  # type: ignore[method-assign]
        typing = self.view.rule_text._textbox  # the Tk Text inside the rule's box
        with mock.patch.object(self.view, "focus_get", return_value=typing):
            self.assertFalse(self.view.shortcut("preview"))
        with mock.patch.object(self.view, "focus_get", return_value=self.view.list.canvas):
            self.assertTrue(self.view.shortcut("preview"))
        await pump(self.root, 0.05)
        self.assertEqual(started, ["preview"])

    async def test_shortcuts_only_while_the_queue_shows(self) -> None:
        self.view.pack_forget()  # another page of the window is shown
        await pump(self.root, 0.05)
        self.assertFalse(self.view.shortcut("preview"))

    async def test_up_down_moves_selection(self) -> None:
        make_files(self.tmp / "in", ["A.2001.mkv", "B.2002.mkv", "C.2003.mkv"])
        await self.add_and_settle(self.tmp / "in")
        keys = self.view.list.item_keys()
        self.assertEqual(self.view.list.selected, keys[0])  # the first band is selected already
        self.view.move_selection(1)
        self.assertEqual(self.view.list.selected, keys[1])
        self.view.move_selection(-1)
        self.assertEqual(self.view.list.selected, keys[0])
        self.view.move_selection(-1)
        self.assertEqual(self.view.list.selected, keys[0])  # stays at the top

    async def test_the_example_follows_the_selected_file(self) -> None:
        make_files(self.tmp / "in", ["A.2001.mkv", "B.2002.mkv", "Show.S01E02.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)", "Series (Flat)")
        self.view.list.select(self.item("B").id)
        self.assertEqual(self.view.example, "→ B (2002).mkv")
        self.select_band("Show")
        self.assertEqual(self.view.example, "→ Show - S01E02 - Episode 02.mkv")

    async def test_responsive_with_1000_items(self) -> None:
        # MediaForge's smoothness check: no turn of the window takes 0.1 s while 1,000 files
        # arrive. Measured as the turn's own work (this thread's CPU time), so other programs
        # busy on the machine (tests running side by side) don't count against it, and with the
        # garbage collector paused: a collection's pause is as long as everything the whole test
        # run holds, not this turn's work.
        make_files(
            self.tmp / "big", [f"Show.S{1 + i // 100:02d}E{i % 100:02d}.mkv" for i in range(1000)]
        )
        queue = self.services.queue
        gc.collect()
        gc.disable()
        self.addCleanup(gc.enable)
        task = self.view.spawn(queue.add_paths([self.tmp / "big"], recursive=True))
        slowest = 0.0
        deadline = time.monotonic() + 60
        while (not task.done() or queue._tasks) and time.monotonic() < deadline:
            start = time.thread_time()
            self.root.update()
            slowest = max(slowest, time.thread_time() - start)
            await pump(self.root, 0.01)
        gc.enable()
        await pump_until(self.root, lambda: len(self.view.list.item_keys()) == 1000, within=10)
        self.assertLess(slowest, 0.1)

    async def test_a_destroyed_view_is_freed(self) -> None:
        view = QueueView(self.root, self.services)
        view.pack(fill="both", expand=True)
        await pump_until(self.root, lambda: view.loaded)
        view.set_custom("{t}")
        await pump(self.root, 0.05)
        ref = weakref.ref(view)
        view.destroy()
        del view
        await pump(self.root, 0.05)
        gc.collect()
        self.assertIsNone(ref())

    async def test_shortcut_actions(self) -> None:
        calls: list[str] = []

        async def preview() -> None:
            calls.append("preview")

        async def forge(only: Sequence[int] | None = None) -> None:
            calls.append("forge")

        async def undo() -> None:
            calls.append("undo")

        self.view.preview = preview  # type: ignore[method-assign]
        self.view.forge = forge  # type: ignore[method-assign]
        self.view.undo_latest = undo  # type: ignore[method-assign]
        with mock.patch.object(self.view, "focus_get", return_value=self.view.list.canvas):
            for action in ("preview", "forge", "undo"):
                self.assertTrue(self.view.shortcut(action))
        await pump(self.root, 0.05)
        self.assertEqual(calls, ["preview", "forge", "undo"])

    async def test_keys_are_bound_on_the_window_and_let_go_on_destroy(self) -> None:
        for sequence in ("<Return>", "<Control-Return>", "<Control-z>", "<Up>", "<Down>"):
            self.assertTrue(self.root.bind(sequence), sequence)
        self.view.destroy()
        for sequence in ("<Return>", "<Control-Return>", "<Control-z>", "<Up>", "<Down>"):
            self.assertFalse(self.root.bind(sequence).strip(), sequence)

    async def test_forge_twice_at_once_runs_once(self) -> None:
        # MediaForge's review I1: a second click (or Ctrl+Enter) while the first run is starting.
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        for item in self.services.queue.items:  # a question, so both clicks get past the start
            self.unsure(item)

        async def slow_answer(title: str, message: str, choices: Sequence[str]) -> str | None:
            await asyncio.sleep(0.1)
            return "Use top results"

        self.view.ask = slow_answer
        await asyncio.gather(self.view.forge(), self.view.forge())
        runs = await self.services.history.list_runs()
        self.assertEqual([r.file_count for r in runs], [2])
        lines = self.services.activity.lines()
        errors = [line.text for line in lines if line.level >= logging.ERROR]
        self.assertEqual(errors, [])  # a second run would fail on every file
        self.assertFalse(self.view.busy)

    async def test_renamed_files_keep_their_new_path(self) -> None:
        # MediaForge's review I2: after Forge, another preset must not make files "to do" again.
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        await self.view.forge()
        await self.choose("Movie (Folder)")
        [item] = self.services.queue.items
        self.assertEqual(item.status, ItemStatus.DONE)
        self.assertEqual(item.path, self.tmp / "in" / "Dune (2021).mkv")
        self.assertEqual(self.view.pending_items(), [])

    async def test_close_during_forge_waits_for_the_run_with_the_progress_shown(self) -> None:
        # MediaForge's review I4: closing mid-run stops after the current file, records it, then
        # closes.
        make_files(self.tmp / "in", ["A.2001.mkv", "B.2002.mkv", "C.2003.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        real_move = fsops.move_file
        moving: list[Path] = []

        def slow_move(src: Path, dst: Path) -> None:
            moving.append(src)
            time.sleep(0.3)
            real_move(src, dst)

        self.enterContext(mock.patch.object(fsops, "move_file", slow_move))
        closed: list[int] = []
        task = self.view.spawn(self.view.forge())
        await pump_until(self.root, lambda: bool(moving))  # mid-run: a file is being moved
        await pump(self.root, 0.05)
        self.assertTrue(self.view.running)
        self.assertTrue(self.view.cancel_button.winfo_ismapped())
        self.assertTrue(self.view.progress.winfo_ismapped())
        self.assertFalse(self.view.forge_button.winfo_ismapped())  # the bar is in its place
        self.assertFalse(self.view.clear_button.winfo_ismapped())
        self.assertTrue(self.view.progress.text.startswith("Renaming 1 of 3 · "), self.view.progress.text)
        self.assertFalse(self.view.request_close(lambda: closed.append(1)))
        await task
        await pump(self.root, 0.1)
        self.assertEqual(closed, [1])
        [run] = await self.services.history.list_runs()
        ops = await self.services.history.operations(run.id)
        self.assertTrue(ops and all(o.state.value == "done" for o in ops))
        self.assertTrue(1 <= run.file_count < 3)
        self.assertFalse(self.view.cancel_button.winfo_ismapped())
        self.assertTrue(self.view.forge_button.winfo_ismapped())
        self.assertIn("cancelled", self.services.activity.lines()[-1].text)

    async def test_request_close_while_idle_closes_at_once(self) -> None:
        self.assertTrue(self.view.request_close(lambda: None))

    async def test_band_rename_renames_only_that_band(self) -> None:
        make_files(
            self.tmp / "in",
            ["Ted.Lasso.S04E03.1080p.WEB.mkv", "Severance.S03E02.2160p.WEB-DL.mkv"],
        )
        # The films' preset (Movie (Folder): a trailer) has no say: no film is renamed, so
        # nothing asks for the trailer tools.
        await self.choose("Series (Flat)")
        await self.add_and_settle(self.tmp / "in")
        self.view.list.on_action(self.band("Severance").key, "rename")
        queue = self.services.queue
        await pump_until(self.root, lambda: not self.view.busy and len(queue.pending()) == 1)
        names = self.names(self.tmp / "in")
        self.assertIn("Ted.Lasso.S04E03.1080p.WEB.mkv", names)
        self.assertNotIn("Severance.S03E02.2160p.WEB-DL.mkv", names)

    async def test_band_choose_opens_the_picker_for_the_unsure_item(self) -> None:
        asked: list[int] = []

        async def fake_ask(master: object, item: QueueItem, services: object, **_: object) -> str:
            asked.append(item.id)
            return "cancel"

        self.enterContext(mock.patch.object(MatchPicker, "ask", fake_ask))
        make_files(self.tmp / "in", ["Shrinking.S03E01.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        [item] = self.services.queue.items
        item.status = ItemStatus.NEEDS_CHOICE
        self.view.refresh()
        [band] = self.rows("band")
        self.assertEqual((band.title, band.state), ("Shrinking", "needs"))
        self.assertEqual(band.actions[1:3], (("ignore", "Ignore"), ("choose", "Choose…")))
        self.view.list.on_action(band.key, "choose")
        await pump(self.root, 0.1)
        self.assertEqual(asked, [item.id])

    async def test_the_queue_fits_the_minimum_window(self) -> None:
        # MediaForge's review focus 5: the toolbar's and the card's buttons stay on screen.
        self.root.geometry("1000x640")
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await pump(self.root, 0.3)
        right = self.view.winfo_rootx() + self.view.winfo_width()
        view = self.view
        for widget in (
            view.forge_button,
            view.preview_button,
            view.clear_button,
            view.preset_menu,
            view.custom_button,
            view.save_as_button,
            *view.option_buttons.values(),
        ):
            if widget is view.option_buttons["Season posters"] and view.kind == "movies":
                continue
            self.assertTrue(widget.winfo_ismapped(), widget)
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, widget)

    async def test_the_option_line_wraps_rather_than_overflow(self) -> None:
        self.root.geometry("1000x640")
        await pump(self.root, 0.2)
        self.view.option_buttons["Trailer"].invoke()  # "Changed for this item" too
        await pump(self.root, 0.3)
        line = self.view.option_line
        right = line.winfo_rootx() + line.winfo_width()
        note = self.view.changed_note
        self.assertTrue(note.winfo_ismapped())
        self.assertLessEqual(note.winfo_rootx() + note.winfo_width(), right + 1)
        self.root.geometry("1600x800")
        await pump(self.root, 0.3)
        tops = {b.winfo_rooty() for b in self.view.option_buttons.values() if b.winfo_ismapped()}
        self.assertEqual(len(tops), 1, "one line when there is room")

    async def test_episode_code_shows_whatever_the_naming_rule(self) -> None:
        # Found in a real-window screenshot: with "{ny}" (Auto) the lines lost their S17E03.
        make_files(self.tmp / "in", ["Gold Rush S17E03 Diagnosing Rick 1080p AMZN WEB-DL.mkv"])
        await self.choose("Auto")
        await self.add_and_settle(self.tmp / "in")
        [line] = self.rows("item")
        self.assertEqual(line.code, "S17E03")

    async def test_done_today_stays_at_the_end_faded(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Heat.1995.mkv", "Ran.1985.mkv"])
        await self.add_and_settle(self.tmp / "in")
        queue = self.services.queue
        old, new, waiting = queue.items
        queue.mark_finished(old.id, old.path)
        queue.mark_finished(new.id, new.path)
        old.finished_at = time.time() - 2 * 24 * 3600
        self.view.refresh()
        await pump(self.root, 0.1)
        self.assertEqual(self.sections(), ["Done today"])
        self.assertEqual(self.bands(), ["Ran", "Heat"])
        self.assertTrue(self.band("Heat").faded)
        self.assertEqual(self.band("Heat").actions[1:], (("cc", "CC ▾"), ("undo", "Undo")))
        self.view.set_filter("ready")
        self.assertEqual(self.bands(), ["Ran"])

    async def test_forged_files_stay_in_the_queue_by_default(self) -> None:
        make_files(self.tmp / "in", ["Heat.1995.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        await self.view.forge()
        await pump(self.root, 0.2)
        self.assertEqual(len(self.services.queue.items), 1)

    async def test_the_setting_takes_forged_files_out_of_the_queue(self) -> None:
        await self.services.settings.update(remove_after_forge=True)
        make_files(self.tmp / "in", ["Heat.1995.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        await self.view.forge()
        await pump(self.root, 0.2)
        self.assertTrue((self.tmp / "in" / "Heat (1995).mkv").exists())
        self.assertEqual(self.services.queue.items, [])
        self.assertEqual(self.rows("band"), [])
        # History still has it, with its Undo.
        [run] = await self.services.history.list_runs()
        self.assertEqual(run.file_count, 1)

    async def test_undo_on_a_done_band_puts_the_file_back(self) -> None:
        make_files(self.tmp / "in", ["Heat.1995.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        await self.view.forge()
        await pump(self.root, 0.2)
        self.assertTrue((self.tmp / "in" / "Heat (1995).mkv").exists())
        [band] = self.rows("band")
        self.view.list.on_action(band.key, "undo")
        await pump_until(self.root, (self.tmp / "in" / "Heat.1995.1080p.mkv").exists)
        await pump(self.root, 0.2)
        self.assertEqual(self.services.queue.items, [])
        self.assertEqual(self.services.activity.lines()[-1].text, "Put back 1 file")

    async def test_undo_latest_asks_then_puts_the_run_back(self) -> None:
        make_files(self.tmp / "in", ["Heat.1995.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        await self.view.forge()
        asked: list[str] = []

        async def undo(title: str, message: str, choices: Sequence[str]) -> str | None:
            asked.append(message)
            return choices[0]

        self.view.ask = undo
        await self.view.undo_latest()
        self.assertEqual(self.names(self.tmp / "in"), ["Heat.1995.1080p.mkv"])
        self.assertEqual(
            asked,
            [
                "This puts back 1 renamed file and removes 0 downloaded posters, trailers or subtitles "
                "from that run."
            ],
        )
        self.assertEqual(
            self.services.activity.lines()[-1].text, "Undo finished: 1 change reversed"
        )
        await self.view.undo_latest()  # nothing left: no question
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.services.activity.lines()[-1].text, "Nothing to undo.")

    async def test_forge_waits_for_an_automatic_run(self) -> None:
        make_files(self.tmp / "in", ["Heat.1995.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        lock = self.services.run_lock
        await lock.acquire()  # an automatic run is busy
        try:
            task = asyncio.get_running_loop().create_task(self.view.forge())
            await pump(self.root, 0.3)
            self.assertTrue((self.tmp / "in" / "Heat.1995.1080p.mkv").exists())
        finally:
            lock.release()
        await task
        self.assertTrue((self.tmp / "in" / "Heat (1995).mkv").exists())

    async def test_trailers_ask_for_their_tools_first(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Folder)")  # posters and a trailer
        self.assertTrue(self.view.option_buttons["Trailer"].value)
        asked: list[str] = []

        async def skip(title: str, message: str, choices: Sequence[str]) -> str | None:
            asked.append(title)
            return "Skip trailers"

        self.view.ask = skip
        await self.view.forge()
        self.assertEqual(asked, ["Download trailer tools?"])
        self.assertTrue((self.tmp / "in" / "Dune (2021)" / "Dune (2021).mkv").exists())
        self.assertEqual(self.names(self.tmp / "in" / "Dune (2021)"), ["Dune (2021).mkv"])

    async def test_option_buttons_follow_the_selected_band_and_their_clicks(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Show.S01E02.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Folder)", "Series (Folder)")
        view = self.view
        self.select_band("Dune")
        await pump(self.root, 0.1)
        on = {name for name, b in view.option_buttons.items() if b.value}
        self.assertEqual(on, {"Poster", "Trailer"})
        self.assertFalse(view.option_buttons["Season posters"].winfo_ismapped())  # shows only
        self.assertEqual(
            view.option_keys(),
            {
                "Poster": "movie_poster",
                "Trailer": "trailer",
                "Subtitles": "subtitles",
                "Clean junk": "clean_junk",
                "Empty folders": "prune_empty",
            },
        )
        self.assertFalse(view.changed_note.winfo_ismapped())
        view.option_buttons["Trailer"].invoke()  # off for this item
        await pump(self.root, 0.1)
        self.assertFalse(view.choice_for(self.item("Dune")).options.trailer)
        self.assertTrue(view.changed_note.winfo_ismapped())  # "Changed for this item"
        self.select_band("Show")
        await pump(self.root, 0.1)
        on = {name for name, b in view.option_buttons.items() if b.value}
        self.assertEqual(on, {"Poster", "Season posters"})
        self.assertTrue(view.option_buttons["Season posters"].winfo_ismapped())
        self.assertFalse(view.changed_note.winfo_ismapped())  # the shows' preset is unchanged
        self.assertEqual(view.fmt, "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {en}")

    async def test_save_to_preset_stores_the_items_changes(self) -> None:
        await self.choose("Movie (Folder)")
        view = self.view  # an empty queue: the card is the films' default
        view.option_buttons["Trailer"].invoke()
        view.set_custom("{t}")
        await view.save_to_preset()
        stored = next(p for p in await self.services.presets.list() if p.name == "Movie (Folder)")
        self.assertEqual(stored.format, "{t}")
        self.assertEqual(stored.options, PresetOptions(movie_poster=True))
        self.assertFalse(view.choice.changed)
        self.assertIsNone(view.choice.custom)
        await pump(self.root, 0.1)
        self.assertFalse(view.changed_note.winfo_ismapped())
        self.assertEqual(self.services.activity.lines()[-1].text, "Saved to the preset Movie (Folder)")

    async def test_save_as_preset_makes_one_for_the_cards_kind(self) -> None:
        view = self.view  # an empty queue: the films' default
        view.set_custom("{t} ({y})")
        asked: list[str] = []

        async def name(title: str, message: str, ok: str) -> str | None:
            asked.append(title)
            return "Films, short"

        view.ask_name = name
        await view.save_as_preset()
        self.assertEqual(asked, ["Save as preset"])
        stored = next(p for p in await self.services.presets.list() if p.name == "Films, short")
        self.assertEqual((stored.type.value, stored.format), ("movie", "{t} ({y})"))
        self.assertEqual(view.choice.preset, stored)
        self.assertEqual(view.preset_menu.get(), "Films, short")
        self.assertNotIn("Films, short", [p.name for p in view.presets_for("series")])
        await pump_until(
            self.root, lambda: self.services.settings.current.last_film_preset == "Films, short"
        )

    async def test_save_as_an_existing_name_asks_first(self) -> None:
        view = self.view

        async def name(title: str, message: str, ok: str) -> str | None:
            return "Auto"

        questions: list[str] = []

        async def no(title: str, message: str, choices: Sequence[str]) -> str | None:
            questions.append(message)
            return "Cancel"

        view.ask_name, view.ask = name, no
        view.set_custom("{t}")
        await view.save_as_preset()
        self.assertEqual(questions, ["There is a preset called Auto already. Replace it?"])
        auto = next(p for p in await self.services.presets.list() if p.name == "Auto")
        self.assertEqual(auto.format, "{ny}")

    async def test_the_chosen_presets_are_remembered_per_kind(self) -> None:
        await self.choose("Series (Flat)", "Movie (Flat)")
        await pump_until(
            self.root,
            lambda: (
                self.services.settings.current.last_show_preset,
                self.services.settings.current.last_film_preset,
            )
            == ("Series (Flat)", "Movie (Flat)"),
        )
        second = await self.make_view()
        self.assertEqual(second.preset_menu.get(), "Movie (Flat)")  # "For: new files"
        self.assertEqual(second.for_label.cget("text"), "For: new files")
        self.assertEqual(second._kind_default("series").preset, self.preset("Series (Flat)"))
        make_files(self.tmp / "in", ["Show.S01E02.mkv"])
        await self.add_and_settle(self.tmp / "in")
        second.refresh()
        self.assertEqual(second.fmt, "{ny} - {s00e00} - {en}")  # a new show starts with it

    async def test_the_menu_offers_the_presets_for_the_bands_kind(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Show.S01E02.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.select_band("Show")
        self.assertEqual(
            self.view.preset_menu.cget("values"), ["Auto", "Series (Folder)", "Series (Flat)"]
        )
        self.select_band("Dune")
        self.assertEqual(
            self.view.preset_menu.cget("values"), ["Auto", "Movie (Folder)", "Movie (Flat)"]
        )
        self.view._preset_chosen("Movie (Flat)")
        dune = self.view.choice_for(self.item("Dune"))
        self.assertEqual(dune.preset, self.preset("Movie (Flat)"))
        self.view.select_preset("Auto")  # a right-click request's without files: every band
        self.assertEqual(self.view.choice_for(self.item("Show")).preset, self.preset("Auto"))
        self.assertEqual(self.view.choice_for(self.item("Dune")).preset, self.preset("Auto"))

    async def test_scan_subfolders_is_in_the_folder_menu_saved_and_scans_again(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        make_files(self.tmp / "in" / "extra", ["Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.assertEqual(len(self.services.queue.items), 2)
        self.view.folder_button.invoke()
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels, ("Choose a folder…", "☑  Scan subfolders"))
        menu.buttons[1].invoke()  # off
        await pump_until(self.root, lambda: len(self.services.queue.items) == 1)
        self.assertFalse(self.services.settings.current.scan_subfolders)
        self.view.folder_button.invoke()
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels[1], "☐  Scan subfolders")
        menu.close()

    async def test_clear_queue_empties_it(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.view.clear_button.invoke()
        await pump(self.root, 0.2)
        self.assertEqual(self.services.queue.items, [])
        self.assertTrue(self.view.empty_state.winfo_ismapped())


class PicturesTest(QueueCase):
    async def show_with_pictures(self) -> FakeImages:
        """One show band whose match has a poster and a backdrop; returns the fake cache."""
        fake = FakeImages()
        self.view.images = BandImages(cast(ImageCache, fake), self.view.list)
        make_files(self.tmp / "in", ["Ted.Lasso.S04E03.1080p.WEB.mkv"])
        await self.add_and_settle(self.tmp / "in")
        for item in self.services.queue.items:
            media = item.media.model_copy(
                update={"poster_path": "/p.jpg", "backdrop_path": "/b.jpg"}
            )
            item.resolution = Resolution(media=media)
        self.view.refresh()
        await pump(self.root, 0.3)
        return fake

    def band_pictures(self) -> int:
        canvas = self.view.list.canvas
        return len(canvas.find_withtag("band-image")) + len(canvas.find_withtag("card-image"))

    async def test_a_band_gets_its_poster_and_its_card_a_picture(self) -> None:
        fake = await self.show_with_pictures()
        self.assertEqual(self.band_pictures(), 2)
        self.assertEqual(len(fake.asked), 2)
        self.assertTrue(any(url.endswith("/p.jpg") for url in fake.asked))

    async def test_band_pictures_survive_a_resize(self) -> None:
        # MediaForge's review: a wider window left only the poster; the backdrop never came back.
        await self.show_with_pictures()
        self.assertEqual(self.band_pictures(), 2)
        self.root.geometry("1500x800")
        await pump(self.root, 0.4)
        self.assertEqual(self.band_pictures(), 2)

    async def test_pictures_come_back_after_the_cache_drops_them(self) -> None:
        # MediaForge's review: a picture pushed out of the cache was never fetched again.
        fake = await self.show_with_pictures()
        self.view.images._photos.clear()
        self.view.list.refresh_images()
        await pump(self.root, 0.3)
        self.assertEqual(self.band_pictures(), 2)
        self.assertEqual(len(fake.asked), 4)  # each picture fetched once more, not over and over

    async def test_the_sizes_asked_are_the_bands_at_physical_size(self) -> None:
        await self.show_with_pictures()
        images = self.view.images
        [band] = self.rows("band")
        poster = self.view._images_for(band)
        self.assertIsNotNone(poster)  # the backdrop lies behind the whole card
        shape = self.view.list.card_shape(1)
        picture = self.view._card_picture(band, shape, theme.pick(self.view.look.SURFACE))
        assert picture is not None and poster is not None
        self.assertEqual((picture.width(), picture.height()), (shape.width, shape.height))
        scale = self.view.list.scale
        self.assertLessEqual(abs(poster.width() - round(58 * scale)), 1)  # the design's poster
        self.assertLessEqual(abs(poster.height() - round(86 * scale)), 1)
        self.assertEqual(len(images._photos), 2)

    async def test_a_done_band_gets_a_faded_poster_and_a_quieter_picture(self) -> None:
        fake = await self.show_with_pictures()
        [item] = self.services.queue.items
        self.services.queue.mark_finished(item.id, item.path)
        self.view.refresh()
        await pump(self.root, 0.3)
        self.assertEqual(self.band_pictures(), 2)
        self.assertEqual(
            {type(finish).__name__ for finish in fake.finishes[-2:]}, {"Fade", "CardBake"}
        )


class WatchedTest(QueueCase):
    async def test_watched_files_show_where_they_will_go(self) -> None:
        downloads, _, _ = await self.library()
        await self.watched(downloads, "VisionQuest.S01E02.Theseus.mkv")
        [band] = self.rows("band")
        self.assertIn("→ Series › VisionQuest › VisionQuest - Season 01", band.dest)
        self.assertEqual(band.state_text, "Ready")

    async def test_held_files_need_you_with_their_reason(self) -> None:
        downloads, _, _ = await self.library()
        [item_id] = await self.watched(downloads, "Dune.mkv")
        self.services.queue.hold(item_id, "No year in the name: is it a movie?")
        self.view.refresh()
        await pump(self.root, 0.1)
        [band] = self.rows("band")
        self.assertEqual(band.state_text, "Needs you")
        self.assertTrue(band.subtitle.startswith("No year in the name: is it a movie?"))
        self.assertEqual(band.actions[-1], ("rename", "Rename"))

    async def test_trial_files_would_rename(self) -> None:
        downloads, _, _ = await self.library(automatic="trial")
        await self.watched(downloads, "Dune.2021.mkv")
        [band] = self.rows("band")
        self.assertEqual((band.state, band.state_text), ("would", "Would rename"))
        self.assertEqual(self.view.filter_buttons["all"].cget("text"), "All 1")
        self.assertEqual(self.view.filter_buttons["ready"].cget("text"), "Ready 0")

    async def test_needs_counts_only_what_needs_you(self) -> None:
        counts: list[int] = []
        self.view.on_counts = counts.append
        downloads, _, _ = await self.library()
        make_files(self.tmp / "in", ["Ted.Lasso.S04E03.mkv"])
        await self.add_and_settle(self.tmp / "in")  # a dropped file: ready
        ready, held = await self.watched(
            downloads, "Ted.Lasso.S04E04.mkv", "Ted.Lasso.S04E05.mkv"
        )
        self.services.queue.hold(held, "TMDB doesn't know this episode.")
        self.view.refresh()
        await pump(self.root, 0.1)
        self.assertEqual(counts[-1], 1)  # the held one only
        self.assertIsNotNone(self.services.queue.get(ready))

    async def test_rename_on_a_watched_band_moves_it_to_the_library(self) -> None:
        downloads, _, movies = await self.library()
        [item_id] = await self.watched(downloads, "Dune.2021.mkv")
        [band] = self.rows("band")
        self.view.list.on_action(band.key, "rename")
        target = movies / "Dune (2021)" / "Dune (2021).mkv"
        item = self.services.queue.get(item_id)
        assert item is not None
        # The file lands a moment before the queue hears it is done: wait for both.
        await pump_until(self.root, lambda: target.exists() and item.status is ItemStatus.DONE)
        self.assertFalse((downloads / "Dune.2021.mkv").exists())

    async def test_forge_leaves_held_downloads_alone(self) -> None:
        downloads, _, movies = await self.library()
        [held] = await self.watched(downloads, "Dune.mkv")
        self.services.queue.hold(held, "No year in the name: is it a movie?")
        make_files(self.tmp / "in", ["Heat.1995.1080p.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await self.choose("Movie (Flat)")
        self.assertEqual(self.view.forge_button.cget("text"), "⚒ Forge 1 file")
        await self.view.forge()
        await pump(self.root, 0.2)
        self.assertTrue((self.tmp / "in" / "Heat (1995).mkv").exists())
        self.assertTrue((downloads / "Dune.mkv").exists())
        self.assertEqual(list(movies.iterdir()), [])

    async def test_cancel_reaches_a_download_move(self) -> None:
        downloads, _, _ = await self.library()
        await self.watched(downloads, "Dune.2021.mkv")
        seen: list[object] = []
        real = self.services.auto.rename

        async def spy(ids: Sequence[int], **kwargs: Any) -> RunResult:
            seen.append(kwargs.get("cancel"))
            return await real(ids, **kwargs)

        self.enterContext(mock.patch.object(self.services.auto, "rename", spy))
        await self.view.forge()
        self.assertTrue(seen)
        self.assertIs(seen[0], self.view._cancel)

    async def test_a_done_band_undo_keeps_a_download_back(self) -> None:
        downloads, _, movies = await self.library()
        await self.watched(downloads, "Dune.2021.mkv")
        self.view.list.on_action(self.rows("band")[0].key, "rename")
        await pump_until(self.root, (movies / "Dune (2021)" / "Dune (2021).mkv").exists)
        await pump(self.root, 0.2)
        self.view.list.on_action(self.rows("band")[0].key, "undo")
        await pump_until(self.root, (downloads / "Dune.2021.mkv").exists)
        await pump(self.root, 0.2)
        kept = self.services.settings.current.kept_back
        self.assertEqual(kept, [os.path.normcase(str(downloads / "Dune.2021.mkv"))])


class DropsTest(QueueCase):
    async def test_files_dropped_on_the_queue_are_added(self) -> None:
        fake = FakeDrops()
        self.addCleanup(file_drops.set_native, file_drops.set_native(fake))
        self.view.destroy()
        view = await self.make_view()
        self.assertTrue(view.drops)
        deliver = fake.targets[view.winfo_id()]
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        make_files(self.tmp / "folder", ["Heat.1995.mkv", "notes.txt"])
        deliver([str(self.tmp / "in" / "Dune.2021.mkv"), str(self.tmp / "folder")])
        await pump_until(self.root, lambda: len(self.services.queue.items) == 2)
        self.assertEqual(
            sorted(i.path.name for i in self.services.queue.items),
            ["Dune.2021.mkv", "Heat.1995.mkv"],
        )
        await pump(self.root, 0.1)
        self.assertEqual(
            self.services.activity.lines()[-1].text, "Added 2 files to the queue"
        )
        view.destroy()
        self.assertEqual(fake.targets, {})  # the window's own procedure is back

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    async def test_a_real_drop_on_the_queue_reaches_it(self) -> None:
        # The real Win32 side on the queue's own window, with a drop posted as Explorer posts it.
        real = file_drops.windows_native()
        if real is None:
            self.skipTest("Win32 file drops unavailable")
        self.addCleanup(file_drops.set_native, file_drops.set_native(real))
        self.view.destroy()
        view = await self.make_view()
        self.assertTrue(view.drops)
        make_files(self.tmp / "in", ["Amélie.2001.mkv"])
        make_files(self.tmp / "folder", ["Ted.Lasso.S04E03.mkv"])
        post_drop(
            view.winfo_id(), [str(self.tmp / "in" / "Amélie.2001.mkv"), str(self.tmp / "folder")]
        )
        await pump_until(self.root, lambda: len(self.services.queue.items) == 2)
        self.assertEqual(
            sorted(i.path.name for i in self.services.queue.items),
            ["Amélie.2001.mkv", "Ted.Lasso.S04E03.mkv"],
        )

    async def test_without_drops_the_empty_queue_says_to_add_files(self) -> None:
        self.addCleanup(file_drops.set_native, file_drops.set_native(None))
        self.view.destroy()
        view = await self.make_view()
        self.assertFalse(view.drops)
        self.assertEqual(view.empty_title.cget("text"), "Add video files or folders")
        self.assertTrue(view.empty_hint.cget("text").startswith("with the buttons above."))


class LookAndLanguageTest(QueueCase):
    async def test_dutch(self) -> None:
        set_language(Language.NL)
        self.view.destroy()
        view = await self.make_view()
        self.assertEqual(view.empty_title.cget("text"), "Sleep videobestanden of mappen hierheen")
        self.assertEqual(view.option_buttons["Poster"].text, "Poster")
        self.assertEqual(view.option_buttons["Empty folders"].text, "Lege mappen")
        self.assertEqual(view.preview_button.cget("text"), "Voorbeeld")
        self.assertEqual(view.clear_button.cget("text"), "✕ Wachtrij leegmaken")
        self.assertEqual(view.custom_button.cget("text"), "✎ Aangepast")
        self.assertEqual(view.save_as_button.cget("text"), "Opslaan als voorinstelling…")
        self.assertEqual(view.folder_button.cget("text"), "+ Map ▾")
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.assertEqual(view.forge_button.cget("text"), "⚒ Forge 2 bestanden")
        self.assertEqual(
            [b.cget("text") for b in view.filter_buttons.values()],
            ["Alles 2", "Wacht op jou 0", "Klaar 2"],
        )
        self.assertEqual(view.for_label.cget("text"), "Voor: Dune (2021)")
        self.assertEqual(
            summary_text(RunResult(renamed=2, posters=1, cancelled=True)),
            "Klaar: 2 hernoemd, 0 al goed, 0 mislukt · 1 poster · geannuleerd",
        )

    async def test_summary_texts(self) -> None:
        self.assertEqual(
            summary_text(RunResult(renamed=3, skipped=1, failed=1, posters=2, trailers=1)),
            "Forged: 3 renamed, 1 already right, 1 failed · 2 posters, 1 trailer",
        )

    async def test_option_labels_are_the_presets_options(self) -> None:
        self.assertEqual(set(OPTION_LABELS), set(PresetOptions.model_fields))
        keys = {
            key
            for spec in self.view._option_specs.values()
            for key in (spec.series, spec.movies)
            if key
        }
        self.assertEqual(keys, set(PresetOptions.model_fields))  # every option has a button

    async def test_the_queue_wears_the_look(self) -> None:
        look = app_look()
        self.assertEqual(tuple(self.view.cget("fg_color")), look.BG)
        self.assertEqual(tuple(self.view.forge_button.cget("fg_color")), look.ACCENT)
        self.assertEqual(tuple(self.view.forge_button.cget("hover_color")), look.ACCENT_HOVER)
        self.assertEqual(tuple(self.view.files_button.cget("border_color")), look.ACCENT)
        self.assertEqual(tuple(self.view.card.cget("fg_color")), look.STACK_FAR)

    async def test_destroy_cancels_its_tasks(self) -> None:
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(60)

        task = self.view.spawn(forever())
        await started.wait()
        self.view.destroy()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.services.queue_events.emit(mock.Mock())  # no longer listening: nothing breaks


class SmoothRefreshTest(QueueCase):
    """F-33: with 1,000 files and their lookups landing for a minute, a refresh is small, comes
    at a measured pace, is split over two turns of the window and redraws only what changed."""

    def big_folder(self) -> Path:
        folder = self.tmp / "big"
        make_files(folder, [f"Show{i % 60}.S01E{i // 60 + 1:02d}.mkv" for i in range(600)])
        make_files(folder, [f"Film{i}.{1990 + i % 30}.mkv" for i in range(400)])
        return folder

    async def test_a_burst_of_queue_events_brings_one_refresh_paced_by_its_cost(self) -> None:
        delays: list[int] = []
        self.view._cancel_refresh()  # anything scheduled at the start
        with mock.patch.object(self.view, "_schedule_refresh", side_effect=delays.append):
            self.view._refresh_cost = 0.001
            self.view._on_queue_event(mock.Mock())
        self.assertEqual(delays, [80])  # a cheap refresh: one burst, one refresh soon after
        delays.clear()
        self.view._refreshed_at = time.monotonic()
        self.view._refresh_cost = 0.05  # 50 ms: refreshes take at most a quarter of the time
        with mock.patch.object(self.view, "_schedule_refresh", side_effect=delays.append):
            self.view._on_queue_event(mock.Mock())
        self.assertEqual(len(delays), 1)
        self.assertGreater(delays[0], 150)
        self.assertLessEqual(delays[0], 200)
        self.view._schedule_refresh(delays[0])
        with mock.patch.object(self.view, "_schedule_refresh", side_effect=delays.append):
            for _ in range(20):
                self.view._on_queue_event(mock.Mock())  # covered by the one coming
        self.assertEqual(len(delays), 1)

    async def test_a_scheduled_refresh_is_built_now_and_shown_in_a_later_turn(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.services.queue.add_paths([self.tmp / "in"], recursive=True)
        await self.services.queue.wait_idle()
        self.view._cancel_refresh()
        self.view._refresh_in_turns()
        self.assertEqual(self.rows("band"), [])  # built, not shown yet
        self.assertIsNotNone(self.view._show_id)
        await pump(self.root, 0.05)
        self.assertEqual(self.bands(), ["Dune"])
        self.assertIsNone(self.view._show_id)

    async def test_a_refresh_with_nothing_new_leaves_the_widgets_alone(self) -> None:
        counts: list[int] = []
        self.view.on_counts = counts.append
        make_files(self.tmp / "in", ["Dune.2021.mkv", "Ted.Lasso.S04E03.mkv"])
        await self.add_and_settle(self.tmp / "in")
        self.view.refresh()
        heard = len(counts)
        watched = (
            self.view.forge_button,
            self.view.example_label,
            self.view.for_label,
            self.view.preset_menu,
            *self.view.filter_buttons.values(),
        )
        with contextlib.ExitStack() as stack:
            configures = [
                stack.enter_context(mock.patch.object(widget, "configure")) for widget in watched
            ]
            redraw = stack.enter_context(mock.patch.object(self.view.list, "_redraw"))
            self.view.refresh()
        for configure in configures:
            configure.assert_not_called()
        redraw.assert_not_called()
        self.assertEqual(len(counts), heard)
        later: list[int] = []
        self.view.on_counts = later.append  # the window
        self.view.refresh()
        self.assertEqual(later, [counts[-1]])  # a new listener hears the count as it is

    async def test_each_part_of_a_refresh_with_1000_files_is_well_under_100_ms(self) -> None:
        # The part's own work (this thread's CPU time), the garbage collector paused, as
        # test_responsive_with_1000_items measures it. Shows and films are one list now: all
        # 1,000 are built every time.
        await self.add_and_settle(self.big_folder())
        await pump_until(self.root, lambda: len(self.view.list.item_keys()) == 1000, within=10)
        gc.collect()
        gc.disable()
        self.addCleanup(gc.enable)
        shows: tuple[Show, ...] = ("all", "ready")
        for show in shows:
            self.view.show = show
            start = time.thread_time()
            built = self.view._build()
            done = time.thread_time()
            self.view.list.set_rows([])  # so the rows are drawn again
            self.view._show(built, time.perf_counter())
            shown = time.thread_time()
            with self.subTest(show=show):
                self.assertEqual(len([r for r in built.rows if r.kind == "item"]), 1000)
                self.assertLess(done - start, 0.1)
                self.assertLess(shown - done, 0.1)


class IgnoreTest(QueueCase):
    """The band's Ignore: this file, or the download's whole folder right under the watched
    folder; the rows leave the queue and Auto-rename leaves them alone from then on."""

    async def course(self) -> tuple[Path, list[int], list[int]]:
        downloads, _, _ = await self.library()
        lessons = ["Python Course/01 Getting Started.mp4", "Python Course/Part 2/02 Loops.mp4"]
        for name in lessons:
            (downloads / name).parent.mkdir(parents=True, exist_ok=True)
        course = await self.watched(downloads, *lessons)
        clip = await self.watched(downloads, "Holiday.Clip.2023.mp4")
        return downloads, course, clip

    def band_of(self, item_id: int) -> int:
        return next(key for key, ids in self.view._bands.items() if item_id in ids)

    async def test_every_band_not_done_has_ignore(self) -> None:
        _downloads, course, clip = await self.course()
        for item_id in (*course, *clip):
            self.unsure(cast(QueueItem, self.services.queue.get(item_id)))
        self.view.refresh()
        await pump(self.root, 0.1)
        for band in self.rows("band"):
            with self.subTest(band=band.title):
                self.assertEqual(band.actions[1], ("ignore", "Ignore"))

    async def test_a_click_on_ignore_opens_its_menu_under_the_button(self) -> None:
        _downloads, _course, [clip] = await self.course()
        self.unsure(cast(QueueItem, self.services.queue.get(clip)))  # Choose… sits right of it
        self.view.refresh()
        band = self.band_of(clip)
        vlist = self.view.list
        vlist.hover_band(band)
        await pump(self.root, 0.05)
        self.view.focus_force()  # the keys elsewhere: the click must not pull them back
        await pump(self.root, 0.05)
        x, y = vlist.action_point(band, "ignore")
        vlist.canvas.event_generate("<Button-1>", x=x, y=y)
        await pump(self.root, 0.3)  # past the menu's check of where the focus went
        menu = PopupMenu._open
        assert menu is not None
        self.assertTrue(menu.is_open)
        self.assertEqual(menu.labels, ("Ignore this file",))
        box = vlist.action_box(band, "ignore")
        assert box is not None
        left, top, _width, height = vlist.screen_box(box)
        window = menu._top
        self.assertEqual(window.winfo_rooty(), top + height + 2)  # under the button
        # Left edges lined up (the band's chip stays in sight), moved left only as far as the
        # window's right edge asks.
        right = self.root.winfo_rootx() + self.root.winfo_width()
        self.assertLessEqual(left + window.winfo_width(), right)  # here: room enough
        self.assertEqual(window.winfo_rootx(), min(left, right - window.winfo_width()))
        menu.close()

    async def test_a_file_directly_in_the_watched_folder_has_no_folder_choice(self) -> None:
        _downloads, _course, [clip] = await self.course()
        labels = [label for label, _ in self.view.ignore_choices([clip])]
        self.assertEqual(labels, ["Ignore this file"])

    async def test_ignore_this_file(self) -> None:
        downloads, course, [clip] = await self.course()
        self.view.list.on_action(self.band_of(clip), "ignore")  # the menu under the button
        await pump(self.root, 0.05)
        self.assertIsNotNone(PopupMenu._open)
        assert PopupMenu._open is not None
        self.assertEqual(PopupMenu._open.labels, ("Ignore this file",))
        PopupMenu._open.close()
        [(_label, ignore)] = self.view.ignore_choices([clip])
        ignore()
        await pump_until(self.root, lambda: self.services.queue.get(clip) is None)
        [entry] = self.services.ignored.entries
        self.assertEqual((entry.path, entry.folder), (downloads / "Holiday.Clip.2023.mp4", False))
        self.assertTrue(all(self.services.queue.get(i) is not None for i in course))
        lines = [line.text for line in self.services.activity.lines()]
        self.assertIn("Ignored: Holiday.Clip.2023.mp4. MediaForge won't pick it up again.", lines)

    async def test_ignore_this_whole_folder(self) -> None:
        downloads, course, [clip] = await self.course()
        labels = [label for label, _ in self.view.ignore_choices(course[1:])]
        self.assertEqual(labels, ["Ignore this file", "Ignore this whole folder"])
        _file, (_label, whole) = self.view.ignore_choices(course[1:])
        whole()
        await pump_until(self.root, lambda: all(self.services.queue.get(i) is None for i in course))
        [entry] = self.services.ignored.entries
        self.assertEqual((entry.path, entry.folder), (downloads / "Python Course", True))
        self.assertIsNotNone(self.services.queue.get(clip))  # the rest stays
        lines = [line.text for line in self.services.activity.lines()]
        self.assertIn("Ignored: Python Course. MediaForge won't pick it up again.", lines)

    async def test_a_dropped_file_can_be_ignored_but_not_its_folder(self) -> None:
        make_files(self.tmp / "in" / "Sub", ["Holiday.Clip.mp4"])
        await self.add_and_settle(self.tmp / "in")
        [item] = self.services.queue.items
        labels = [label for label, _ in self.view.ignore_choices([item.id])]
        self.assertEqual(labels, ["Ignore this file"])  # not under a watched folder

    async def test_nothing_while_a_run_goes_or_for_done_files(self) -> None:
        _downloads, _course, [clip] = await self.course()
        self.view.running = True
        self.assertEqual(self.view.ignore_choices([clip]), [])
        self.view.running = False
        item = self.services.queue.get(clip)
        assert item is not None
        item.status = ItemStatus.DONE
        self.assertEqual(self.view.ignore_choices([clip]), [])

    async def test_dutch(self) -> None:
        set_language(Language.NL)
        _downloads, course, _clip = await self.course()
        labels = [label for label, _ in self.view.ignore_choices(course[1:])]
        self.assertEqual(labels, ["Dit bestand negeren", "Deze hele map negeren"])
        self.assertEqual(
            [label for label, _ in self.view.ignore_choices(course)][0],
            "Deze 2 bestanden negeren",
        )


class PresetPerBandTest(QueueCase):
    """A preset per band (the Shows | Films switch is gone): each film and each show has its own
    Choice; the card follows the selected band; the chip sets a band's preset; "Use for all ▾"
    copies it where it fits; Forge and the library use each band's own."""

    async def films_and_show(self) -> None:
        make_files(
            self.tmp / "in",
            ["Dune.2021.mkv", "Heat.1995.mkv", "Show.S01E01.mkv", "Show.S01E02.mkv"],
        )
        await self.add_and_settle(self.tmp / "in")

    def line(self, name: str) -> Row:
        item = self.item(name)
        return next(r for r in self.rows("item") if r.key == item.id)

    def preset_name(self, name: str) -> str:
        preset = self.view.choice_for(self.item(name)).preset
        return preset.name if preset is not None else ""

    async def test_two_films_with_their_own_presets(self) -> None:
        await self.films_and_show()
        self.view.set_band_preset([self.item("Dune").id], self.preset("Movie (Flat)"))
        self.view.set_band_preset([self.item("Heat").id], self.preset("Movie (Folder)"))
        self.view.refresh()
        self.assertEqual(self.line("Dune").title, "Dune (2021).mkv")
        self.assertEqual(self.band("Dune").chip, "Movie (Flat) ▾")
        self.assertEqual(self.band("Heat").chip, "Movie (Folder) ▾")
        parts = self.view._plan_parts([self.item("Dune"), self.item("Heat")])
        self.assertEqual(sorted(rule for _inputs, rule, _options in parts), ["{ny}", "{ny}/{ny}"])
        self.view.ask = self._skip_trailers
        await self.view.forge(only=[self.item("Dune").id, self.item("Heat").id])
        await pump(self.root, 0.2)
        names = self.names(self.tmp / "in")
        self.assertIn("Dune (2021).mkv", names)  # flat
        self.assertTrue((self.tmp / "in" / "Heat (1995)" / "Heat (1995).mkv").exists())

    async def _skip_trailers(self, title: str, message: str, choices: Sequence[str]) -> str | None:
        return "Skip trailers"

    async def test_a_shows_episodes_follow_the_show(self) -> None:
        await self.films_and_show()
        self.select_band("Show")
        self.view._preset_chosen("Series (Flat)")
        self.view.refresh()
        for name in ("Show.S01E01", "Show.S01E02"):
            self.assertEqual(self.preset_name(name), "Series (Flat)")
        make_files(self.tmp / "more", ["Show.S01E03.mkv"])  # a new episode joins its show
        await self.add_and_settle(self.tmp / "more")
        self.view.refresh()
        self.assertEqual(self.preset_name("Show.S01E03"), "Series (Flat)")
        self.assertEqual(self.band("Show").subtitle, "Season 1 · 3 episodes")
        self.assertEqual(self.preset_name("Dune"), "Movie (Folder)")

    async def test_the_card_follows_the_selection(self) -> None:
        await self.films_and_show()
        self.view.set_band_preset([self.item("Heat").id], self.preset("Movie (Flat)"))
        self.select_band("Dune")
        self.assertEqual(self.view.for_label.cget("text"), "For: Dune (2021)")
        self.assertEqual(self.view.preset_menu.get(), "Movie (Folder)")
        self.select_band("Heat")
        self.assertEqual(self.view.for_label.cget("text"), "For: Heat (1995)")
        self.assertEqual(self.view.preset_menu.get(), "Movie (Flat)")
        self.select_band("Show")
        self.assertEqual(self.view.for_label.cget("text"), "For: Show · 2 episodes")
        self.assertIn("Series (Folder)", self.view.preset_menu.cget("values"))
        self.view.list.select(None)  # nothing selected: the first band is, at the next refresh
        self.view.refresh()
        self.assertIsNotNone(self.view.list.selected)

    async def test_the_chip_menu_sets_a_bands_preset(self) -> None:
        await self.films_and_show()
        band = self.band("Heat")
        ids = self.view._bands[band.key]
        labels = [label for label, _ in self.view.chip_choices(ids)]
        self.assertEqual(labels, ["Auto", "Movie (Folder)", "Movie (Flat)"])  # the film presets
        self.view.focus_force()  # the keys elsewhere: the click must not pull them back
        await pump(self.root, 0.05)
        x, y = self.view.list.action_point(band.key, "preset")
        self.view.list.canvas.event_generate("<Button-1>", x=x, y=y)  # the menu at the chip
        await pump(self.root, 0.3)  # past the menu's check of where the focus went
        assert PopupMenu._open is not None
        self.assertTrue(PopupMenu._open.is_open)
        self.assertEqual(PopupMenu._open.labels, tuple(labels))
        PopupMenu._open.close()
        dict(self.view.chip_choices(ids))["Movie (Flat)"]()
        self.view.refresh()
        self.assertEqual(self.band("Heat").chip, "Movie (Flat) ▾")
        self.assertEqual(self.view.for_label.cget("text"), "For: Heat (1995)")  # selected
        self.view.set_custom("{t}")
        self.view.refresh()
        self.assertEqual(self.band("Heat").chip, "✎ Custom")
        await pump_until(
            self.root, lambda: self.services.settings.current.last_film_preset == "Movie (Flat)"
        )

    async def test_use_for_all_films_copies_to_films_only(self) -> None:
        await self.films_and_show()
        self.select_band("Dune")
        self.view._preset_chosen("Movie (Flat)")
        self.view.option_buttons["Trailer"].invoke()
        entries = self.view.use_for_all_entries()
        self.assertEqual(
            [(e.label, e.action is not None) for e in entries],
            [("All films (2)", True), ("All shows (1) · doesn't fit", False)],
        )
        assert entries[0].action is not None
        entries[0].action()
        self.view.refresh()
        heat = self.view.choice_for(self.item("Heat"))
        self.assertEqual((heat.preset, heat.options.trailer), (self.preset("Movie (Flat)"), True))
        self.assertIsNot(heat, self.view.choice_for(self.item("Dune")))  # a copy of its own
        self.assertEqual(self.preset_name("Show"), "Series (Folder)")
        self.assertEqual(self.services.activity.lines()[-1].text, "Used Movie (Flat) for 2 films")

    async def test_use_for_all_shows_and_everything(self) -> None:
        await self.films_and_show()
        self.select_band("Show")
        self.view._preset_chosen("Series (Flat)")
        entries = self.view.use_for_all_entries()
        self.assertEqual(
            [(e.label, e.action is not None) for e in entries],
            [("All films (2) · doesn't fit", False), ("All shows (1)", True)],
        )
        self.view._preset_chosen("Auto")  # fits both: everything is offered
        entries = self.view.use_for_all_entries()
        self.assertEqual(
            [e.label for e in entries],
            ["All films (2)", "All shows (1)", "Everything in the queue (3)"],
        )
        assert entries[-1].action is not None
        entries[-1].action()
        for name in ("Dune", "Heat", "Show.S01E01"):
            self.assertEqual(self.preset_name(name), "Auto")
        self.assertEqual(self.services.activity.lines()[-1].text, "Used Auto for 3 items")
        self.select_band("Dune")
        self.view._preset_chosen("Movie (Flat)")
        self.assertEqual(len(self.view.use_for_all_entries()), 2)  # a film preset: no everything
        self.view.set_custom("{t}")  # a Custom rule names anything
        self.assertEqual(len(self.view.use_for_all_entries()), 3)

    async def test_use_for_all_is_hidden_with_one_band(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        await pump(self.root, 0.05)
        self.assertFalse(self.view.use_all_link.winfo_ismapped())
        make_files(self.tmp / "more", ["Heat.1995.mkv"])
        await self.add_and_settle(self.tmp / "more")
        self.view.refresh()
        await pump(self.root, 0.05)
        self.assertTrue(self.view.use_all_link.winfo_ismapped())
        self.assertEqual(self.view.use_all_link.cget("text"), "Use for all ▾")

    async def test_presets_read_again_keep_a_bands_preset_by_name_or_fall_back(self) -> None:
        await self.films_and_show()
        mine = Preset(name="Mine", type=TypeHint.MOVIE, format="{t}", options=PresetOptions())
        await self.services.presets.save(mine)
        await self.view._load_presets()
        self.view.set_band_preset([self.item("Dune").id], self.preset("Mine"))
        self.view.set_band_preset([self.item("Heat").id], self.preset("Movie (Flat)"))
        flat = self.preset("Movie (Flat)")
        await self.services.presets.save(flat.model_copy(update={"format": "{t} [{y}]"}))
        await self.services.presets.delete("Mine")
        await self.view._load_presets()
        heat = self.view.choice_for(self.item("Heat"))
        self.assertEqual((self.preset_name("Heat"), heat.rule), ("Movie (Flat)", "{t} [{y}]"))
        # Its preset is gone: its default again (the film preset chosen last, Heat's).
        self.assertEqual(self.preset_name("Dune"), "Movie (Flat)")

    async def test_a_right_click_requests_preset_names_the_files_it_added(self) -> None:
        make_files(self.tmp / "in", ["Dune.2021.mkv"])
        await self.add_and_settle(self.tmp / "in")
        make_files(self.tmp / "request", ["Heat.1995.mkv", "Show.S01E01.mkv"])
        await self.view.add([self.tmp / "request"], preset="Movie (Flat)")
        await self.view.settled()
        self.view.refresh()
        self.assertEqual(self.preset_name("Heat"), "Movie (Flat)")
        self.assertEqual(self.preset_name("Dune"), "Movie (Folder)")
        self.assertEqual(self.preset_name("Show"), "Series (Folder)")
        self.view.select_preset("Movie (Flat)")  # without files: every film band
        self.assertEqual(self.preset_name("Dune"), "Movie (Flat)")
        self.assertEqual(self.preset_name("Show"), "Series (Folder)")

    async def test_a_watched_download_goes_to_the_library_with_its_bands_preset(self) -> None:
        downloads, _, movies = await self.library()
        [item_id] = await self.watched(downloads, "Dune.2021.mkv")
        self.assertEqual(self.band("Dune").chip, "Movie (Folder) ▾")  # the library's at first
        self.view.set_band_preset([item_id], self.preset("Movie (Flat)"))
        self.view.refresh()
        self.assertIn("→ Movies", self.band("Dune").dest)
        self.view.list.on_action(self.band("Dune").key, "rename")
        target = movies / "Dune (2021).mkv"  # flat, in the Movies library
        item = self.services.queue.get(item_id)
        assert item is not None
        await pump_until(self.root, lambda: target.exists() and item.status is ItemStatus.DONE)
        [run] = await self.services.history.list_runs()
        self.assertEqual(run.preset_name, "Movie (Flat)")

    async def test_dutch(self) -> None:
        set_language(Language.NL)
        self.view.destroy()
        self.view = await self.make_view()
        self.assertEqual(self.view.for_label.cget("text"), "Voor: nieuwe bestanden")
        await self.films_and_show()
        self.select_band("Show")
        self.assertEqual(self.view.for_label.cget("text"), "Voor: Show · 2 afleveringen")
        self.assertEqual(self.view.use_all_link.cget("text"), "Gebruiken voor alles ▾")
        self.view._preset_chosen("Auto")
        self.assertEqual(
            [e.label for e in self.view.use_for_all_entries()],
            ["Alle films (2)", "Alle series (1)", "Alles in de wachtrij (3)"],
        )
        self.view._preset_chosen("Series (Flat)")
        self.assertEqual(self.view.use_for_all_entries()[0].label, "Alle films (2) · past niet")
