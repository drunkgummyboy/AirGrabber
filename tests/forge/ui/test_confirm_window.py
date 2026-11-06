"""The right-click confirm window (app/forge/ui/confirm_window.py), ported from MediaForge's
tests/ui/test_confirm_window.py: new names listed, unticked files left alone, a right-click run
and its undo, cancel, "Open in MediaForge", many paths through the batcher into one window, one
run for two clicks, hundreds of files stay quick, tick boxes, "Change match…". Plus: uncertain
matches say so, the run lock, a preset switch, never transient, it shows while AirGrabber's window
is hidden, and the texts in Dutch. On real Forge services (temporary forge.db, no TMDB key: names
come from the file names)."""

from __future__ import annotations

import asyncio
import gc
import time
import tkinter as tk
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from unittest import mock

import customtkinter as ctk
import httpx

from app.config import AppPaths
from app.forge.core.batcher import ConfirmBatcher
from app.forge.core.queue import ItemStatus
from app.forge.models.history import RunSource
from app.forge.models.media import MediaKind
from app.forge.services import ForgeServices
from app.forge.ui.confirm_window import ConfirmWindow
from app.forge.ui.match_picker import MatchPicker
from app.i18n import Language, current_language, set_language
from app.models.tmdb import TMDBTitle
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import SPEED_ROOM, press, pump, pump_until


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def make_files(folder: Path, names: Sequence[str]) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / n for n in names]
    for p in paths:
        p.write_bytes(b"video")
    return paths


class ConfirmCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()  # AirGrabber in the tray
        self.addCleanup(self._destroy_root)
        language = current_language()
        self.addCleanup(set_language, language)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow
        http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.addAsyncCleanup(http.aclose)
        self.services = await ForgeServices.create(
            AppPaths(self.data_dir),
            http,
            TMDBClient(http, api_key=lambda: ""),
            ImageCache(http, self.data_dir / "posters"),
            cast(SettingsService, mock.Mock(spec=SettingsService)),
        )
        self.addAsyncCleanup(self.services.aclose)

    async def open_window(
        self, paths: list[Path], preset: str | None = "Movie (Flat)"
    ) -> ConfirmWindow:
        win = ConfirmWindow(self.root, self.services, paths, preset)
        self.addCleanup(self._destroy_window, win)
        await win.loaded()
        await pump(self.root, 0.2)
        return win

    def _destroy_window(self, win: ConfirmWindow) -> None:
        try:
            if win.winfo_exists():
                win.destroy()
        except tk.TclError:
            pass

    def names(self, folder: Path) -> list[str]:
        return sorted(p.name for p in folder.iterdir())


class ListTests(ConfirmCase):
    async def test_lists_files_with_new_names(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv", "Heat.1995.1080p.mkv"])
        win = await self.open_window(files)
        self.assertEqual(win.heading.cget("text"), "Rename 2 files?")
        self.assertEqual(sorted(win.new_names()), ["Dune (2021).mkv", "Heat (1995).mkv"])
        self.assertEqual(win.selected_label.cget("text"), "2 of 2 selected")
        self.assertEqual(win.rename_button.cget("text"), "Rename 2 files")
        rows = [r for r in win.list.rows if r.kind == "item"]
        # The new name over the old one.
        self.assertEqual(
            sorted((r.title, r.subtitle) for r in rows),
            [("Dune (2021).mkv", "Dune.2021.mkv"), ("Heat (1995).mkv", "Heat.1995.1080p.mkv")],
        )
        self.assertTrue(all(r.checked for r in rows))

    async def test_one_file_reads_singular(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        self.assertEqual(win.heading.cget("text"), "Rename 1 file?")
        self.assertEqual(win.rename_button.cget("text"), "Rename 1 file")

    async def test_an_unknown_preset_falls_back_to_the_last_used_one(self) -> None:
        await self.services.settings.update(last_preset="Movie (Folder)")
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files, preset="Gone")
        assert win.preset is not None
        self.assertEqual(win.preset.name, "Movie (Folder)")
        self.assertEqual(win.preset_menu.get(), "Movie (Folder)")

    async def test_switching_the_preset_changes_the_new_names(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        win.select_preset("Movie (Folder)")
        await win.queue.wait_idle()
        await pump(self.root, 0.2)
        self.assertEqual(win.new_names(), ["Dune (2021).mkv"])
        targets = list(win._targets.values())
        self.assertEqual(targets[0].parent.name, "Dune (2021)")

    async def test_an_uncertain_match_says_so(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv", "Heat.1995.mkv"])
        win = await self.open_window(files)
        first = win.queue.items[0]
        first.status = ItemStatus.NEEDS_CHOICE
        win.refresh()
        row = next(r for r in win.list.rows if r.key == first.id)
        self.assertIn("TMDB isn't sure, using Dune (2021)", row.subtitle)
        self.assertIn("Dune.2021.mkv", row.subtitle)
        self.assertEqual(win.uncertain_ids(), [first.id])

    async def test_texts_in_dutch(self) -> None:
        set_language(Language.NL)
        files = make_files(self.tmp / "m", ["Dune.2021.mkv", "Heat.1995.mkv"])
        win = await self.open_window(files)
        self.assertEqual(win.heading.cget("text"), "2 bestanden hernoemen?")
        self.assertEqual(win.selected_label.cget("text"), "2 van 2 geselecteerd")
        self.assertEqual(win.rename_button.cget("text"), "2 bestanden hernoemen")
        self.assertEqual(win.open_button.cget("text"), "Openen in MediaForge")


class ActionTests(ConfirmCase):
    async def test_unticked_files_untouched(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv", "Heat.1995.1080p.mkv"])
        win = await self.open_window(files)
        win.set_ticked(files[1], False)
        self.assertEqual(win.rename_button.cget("text"), "Rename 1 file")
        await win.rename()
        self.assertEqual(self.names(self.tmp / "m"), ["Dune (2021).mkv", "Heat.1995.1080p.mkv"])

    async def test_rename_creates_right_click_run(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        await win.rename()
        [run] = await self.services.history.list_runs()
        self.assertIs(run.source, RunSource.RIGHT_CLICK)
        self.assertEqual(run.preset_name, "Movie (Flat)")
        self.assertIn(MediaKind.MOVIE, run.kinds)
        self.assertIn("1 renamed", win.result_label.cget("text"))
        self.assertTrue(win.undo_button.winfo_manager())
        self.assertTrue(win.close_button.winfo_manager())
        self.assertFalse(win.rename_button.winfo_manager())

    async def test_rename_hands_each_files_media_to_the_move_records(self) -> None:
        await self.services.settings.update(movies_folder=str(self.tmp / "m"))
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        [item] = win.queue.items
        executor = self.services.executor
        with mock.patch.object(executor, "run", wraps=executor.run) as run:
            await win.rename()
        self.assertEqual(run.call_args.kwargs["media"], {item.id: item.media})
        [move] = await self.services.moves.moves_from(self.tmp / "m")
        self.assertEqual((move.target.name, move.kind), ("Dune (2021).mkv", MediaKind.MOVIE))

    async def test_a_preset_that_moves_shows_it_and_moves_and_undo_brings_back(self) -> None:
        movies = self.tmp / "lib" / "Movies"
        movies.mkdir(parents=True)
        await self.services.settings.update(movies_folder=str(movies))
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files, "Movie (Folder)")
        self.assertIn("library folder", win.hint.cget("text"))
        [item] = win.queue.items
        target = movies / "Dune (2021)" / "Dune (2021).mkv"
        self.assertEqual(win._targets[item.id], target)
        await win.rename()
        self.assertTrue(target.is_file())
        self.assertEqual(self.names(self.tmp / "m"), [])
        await win.undo()
        self.assertEqual(self.names(self.tmp / "m"), ["Dune.2021.mkv"])
        self.assertFalse(target.exists())

    async def test_a_preset_that_moves_without_its_folder_leaves_the_file_alone(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files, "Movie (Folder)")
        await win.rename()
        self.assertEqual(self.names(self.tmp / "m"), ["Dune.2021.mkv"])
        self.assertIn("library folder", win.result_label.cget("text"))
        self.assertEqual(await self.services.history.list_runs(), [])
        self.assertEqual(win.rename_button.cget("state"), "normal")

    async def test_a_preset_that_does_not_move_keeps_the_old_hint(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        self.assertEqual(win.hint.cget("text"), "Nothing changes until you click Rename.")

    async def test_undo_button_reverts(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        await win.rename()
        await win.undo()
        self.assertEqual(self.names(self.tmp / "m"), ["Dune.2021.mkv"])
        self.assertIn("put back", win.result_label.cget("text").lower())
        self.assertFalse(win.undo_button.winfo_manager())

    async def test_undo_reports_what_was_left_with_its_reason_translated(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        await win.rename()
        (self.tmp / "m" / "Dune (2021).mkv").rename(self.tmp / "m" / "Elsewhere.mkv")
        set_language(Language.NL)
        await win.undo()
        text = win.result_label.cget("text")
        self.assertIn("1 ongemoeid gelaten", text)
        self.assertNotIn("file was", text)  # the journal's English note, shown in Dutch

    async def test_nothing_happens_on_cancel(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        closed: list[ConfirmWindow] = []
        win = await self.open_window(files)
        win.on_closed = closed.append
        win.cancel()
        self.assertFalse(win.winfo_exists())
        self.assertEqual(closed, [win])
        self.assertEqual(self.names(self.tmp / "m"), ["Dune.2021.mkv"])
        self.assertEqual(await self.services.history.list_runs(), [])

    async def test_escape_cancels(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        press(win, "<Escape>")
        await pump(self.root, 0.05)
        self.assertFalse(win.winfo_exists())

    async def test_open_in_forge_hands_the_paths_over_and_closes(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv", "Heat.1995.mkv"])
        handed: list[tuple[list[Path], str | None]] = []
        closed: list[ConfirmWindow] = []
        win = await self.open_window(files)
        win.on_open_in_forge = lambda paths, preset: handed.append((paths, preset))
        win.on_closed = closed.append
        win.open_in_forge()
        self.assertEqual(handed, [(files, "Movie (Flat)")])
        self.assertEqual(closed, [win])
        self.assertFalse(win.winfo_exists())
        self.assertEqual(self.services.queue.items, [])  # the Inbox's queue is the host's job

    async def test_rename_twice_at_once_runs_once(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        await asyncio.gather(win.rename(), win.rename())
        await win.rename()  # and not again once done
        self.assertEqual(len(await self.services.history.list_runs()), 1)
        self.assertNotIn("failed", win.result_label.cget("text"))

    async def test_rename_waits_for_the_run_lock(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        async with self.services.run_lock:  # the renamer's run
            task = asyncio.create_task(win.rename())
            await asyncio.sleep(0.2)
            self.assertEqual(self.names(self.tmp / "m"), ["Dune.2021.mkv"])
            win.cancel()  # busy: ignored
            self.assertTrue(win.winfo_exists())
        await task
        self.assertEqual(self.names(self.tmp / "m"), ["Dune (2021).mkv"])

    async def test_clicking_a_tick_box_unticks_that_file(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv", "Heat.1995.mkv"])
        win = await self.open_window(files)
        win.deiconify()
        await pump(self.root, 0.2)
        first, second = (item.id for item in win.queue.items)
        x, y = win.list.check_point(second), win.list.row_middle(second)
        win.list.canvas.event_generate("<Button-1>", x=x, y=y)
        await pump(self.root, 0.05)
        self.assertEqual(win.chosen_ids(), [first])
        self.assertEqual(win.selected_label.cget("text"), "1 of 2 selected")

    async def test_nothing_ticked_disables_rename(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        win.set_ticked(files[0], False)
        self.assertEqual(win.rename_button.cget("state"), "disabled")

    async def test_change_match_needs_a_chosen_row(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        self.assertEqual(win.change_button.cget("state"), "disabled")
        win.list.select(win.queue.items[0].id)
        self.assertEqual(win.change_button.cget("state"), "normal")

    async def test_change_match_applies_the_picked_title(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        item = win.queue.items[0]
        win.list.select(item.id)
        picked = TMDBTitle(id=438631, title="Dune", date="2021-09-15")
        chosen: list[tuple[int, TMDBTitle | None]] = []

        async def ask(*args: object, **kwargs: object) -> TMDBTitle:
            return picked

        async def choose(item_id: int, title: TMDBTitle | None) -> None:
            chosen.append((item_id, title))

        with (
            mock.patch.object(MatchPicker, "ask", ask),
            mock.patch.object(win.queue, "choose_match", choose),
        ):
            await win.change_selected()
        self.assertEqual(chosen, [(item.id, picked)])

    async def test_a_cancelled_pick_changes_nothing(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        win.list.select(win.queue.items[0].id)
        choose = mock.AsyncMock()

        async def ask(*args: object, **kwargs: object) -> str:
            return "cancel"

        with (
            mock.patch.object(MatchPicker, "ask", ask),
            mock.patch.object(win.queue, "choose_match", choose),
        ):
            await win.change_selected()
        choose.assert_not_called()


class WindowTests(ConfirmCase):
    async def test_never_transient_and_shows_while_airgrabber_is_hidden(self) -> None:
        files = make_files(self.tmp / "m", ["Dune.2021.mkv"])
        win = await self.open_window(files)
        await pump(self.root, 0.3)
        self.assertEqual(str(win.transient()), "")
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertTrue(win.winfo_viewable())
        self.assertEqual(win.grab_current(), None)  # other windows stay usable

    async def test_batcher_to_window_one_window_for_many_paths(self) -> None:
        files = make_files(self.tmp / "m", [f"Movie.{n}.20{n:02d}.mkv" for n in range(12)])
        windows: list[ConfirmWindow] = []

        def open_confirm(preset: str | None, paths: list[Path]) -> None:
            win = ConfirmWindow(self.root, self.services, paths, preset)
            self.addCleanup(self._destroy_window, win)
            windows.append(win)

        batcher = ConfirmBatcher(open_confirm)
        for f in files:
            batcher.add([str(f)], "Movie (Flat)", now=1.0)
        batcher.add([str(files[0])], "Movie (Flat)", now=1.0)  # the same path again
        batcher.flush_due(now=10**9)
        [win] = windows
        await win.loaded()
        self.assertEqual(len(win.new_names()), 12)

    async def test_three_hundred_files_redraw_without_freezing(self) -> None:
        # MediaForge's review C1: one set of widgets per file froze the window for seconds.
        files = make_files(self.tmp / "m", [f"Movie.{n}.20{n % 25:02d}.mkv" for n in range(300)])
        win = await self.open_window(files)
        self.assertEqual(len(win.new_names()), 300)
        start = time.perf_counter()
        win.refresh()
        self.root.update()
        self.assertLess(time.perf_counter() - start, 0.25 * SPEED_ROOM)

    async def test_closing_while_files_are_read_leaves_nothing_running(self) -> None:
        files = make_files(self.tmp / "m", [f"Movie.{n}.20{n:02d}.mkv" for n in range(30)])
        win = ConfirmWindow(self.root, self.services, files, "Movie (Flat)")
        win.cancel()
        await pump(self.root, 0.2)
        self.assertFalse(win.winfo_exists())
        self.assertEqual(win.queue.items, [])
        await pump_until(self.root, lambda: not win._tasks)


if __name__ == "__main__":
    import unittest

    unittest.main()
