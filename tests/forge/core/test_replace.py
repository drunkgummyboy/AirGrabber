"""MediaForge's "Use this one instead" (round 2, item 26): a watched download held as
"Already in the library" takes the library copy's place. The copy there moves aside first, to the
Duplicates folder beside the library folder (app/forge/core/duplicates.py's rule), with its
subtitles; then the download is renamed in as usual. One run, so Undo (a card's or History's)
puts both back. Nothing is deleted.

On the renamer's real parts (tests/forge/core/test_auto_renamer.py's RenamerTestCase): a temporary
forge.db, the real queue, executor and undo, a fake TMDB, temporary folders only."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import sqlite3
import threading
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from app.forge.core import fsops
from app.forge.core.auto import ALREADY_THERE, SAME_NAME, held_in_library, replace_runs
from app.forge.core.matcher import Resolver
from app.forge.core.queue import Queue
from app.forge.core.duplicates import aside_folder
from app.forge.core.undo import UndoService
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.history import OpKind, RunSource
from app.forge.models.preset import DEFAULT_PRESETS
from app.i18n import t
from tests.forge.core.test_auto_decide import MOVIES, SETTINGS, movie
from tests.forge.core.test_auto_renamer import Clock, RenamerTestCase

FILM = "Dune (2021)"


class ReplaceCase(RenamerTestCase):
    def library_copy(self, name: str = f"{FILM}.mkv", data: bytes = b"old copy") -> Path:
        """The copy already in the Movies folder, with an English subtitle beside it."""
        folder = self.folders.movies / FILM
        folder.mkdir(parents=True, exist_ok=True)
        old = folder / name
        old.write_bytes(data)
        (folder / f"{Path(name).stem}.en.srt").write_bytes(b"old subtitle")
        return old

    @property
    def aside(self) -> Path:
        return self.folders.movies.parent / "Duplicates" / "Movies" / FILM

    async def held(self, release: str = "Dune.2021.2160p.WEB-DL.mkv") -> QueueItem:
        """A finished download Auto-rename held because the film is in the library already."""
        await self.use()
        self.folders.download(release)
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        [item] = [i for i in self.queue.items if i.path.name == release]
        assert item.status is ItemStatus.HELD and item.message == t(ALREADY_THERE)
        assert held_in_library(item)
        return item

    async def replace(self, *items: QueueItem) -> int:
        auto = self.renamer(Clock())
        result = await auto.replace([i.id for i in items], source=RunSource.APP)
        assert result.run_id is not None
        return result.run_id


class ReplaceTest(ReplaceCase):
    async def test_replace_moves_the_old_copy_aside_then_renames(self) -> None:
        old = self.library_copy()
        item = await self.held()
        run_id = await self.replace(item)

        self.assertEqual(old.read_bytes(), b"video")  # the download, in the library copy's place
        self.assertEqual((self.aside / f"{FILM}.mkv").read_bytes(), b"old copy")
        self.assertEqual((self.aside / f"{FILM}.en.srt").read_bytes(), b"old subtitle")
        self.assertFalse((old.parent / f"{FILM}.en.srt").exists())  # never the new one's
        self.assertIs(item.status, ItemStatus.DONE)
        self.assertEqual(item.run_id, run_id)
        renames = [o for o in await self.history.operations(run_id) if o.kind is OpKind.RENAME]
        self.assertEqual(  # aside first, its subtitle along, then the download
            [(Path(o.src or "").name, Path(o.dst or "").parent.name) for o in renames],
            [(f"{FILM}.mkv", FILM), (f"{FILM}.en.srt", FILM), (item.parsed.source_name, FILM)],
        )
        self.assertEqual(Path(renames[0].dst or "").parent, self.aside)

    async def test_another_kind_of_video_is_replaced_too(self) -> None:
        old = self.library_copy(f"{FILM}.mp4")
        item = await self.held()
        await self.replace(item)
        self.assertEqual((old.parent / f"{FILM}.mkv").read_bytes(), b"video")
        self.assertFalse(old.exists())
        self.assertEqual((self.aside / f"{FILM}.mp4").read_bytes(), b"old copy")
        self.assertEqual((self.aside / f"{FILM}.en.srt").read_bytes(), b"old subtitle")

    async def test_an_earlier_copy_aside_is_never_overwritten(self) -> None:
        self.library_copy()
        self.aside.mkdir(parents=True)
        (self.aside / f"{FILM}.mkv").write_bytes(b"replaced before")
        item = await self.held()
        await self.replace(item)
        self.assertEqual((self.aside / f"{FILM}.mkv").read_bytes(), b"replaced before")
        self.assertEqual((self.aside / f"{FILM} (1).mkv").read_bytes(), b"old copy")
        self.assertEqual((self.aside / f"{FILM} (1).en.srt").read_bytes(), b"old subtitle")

    async def test_only_held_in_the_library_is_replaced(self) -> None:
        await self.use(automatic="off")
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        [item_id] = await self.queue.add_watched([(file, self.folders.downloads)])
        await self.queue.wait_idle()
        auto = self.renamer(Clock())
        result = await auto.replace([item_id], source=RunSource.APP)
        self.assertEqual(result.renamed, 0)
        self.assertTrue(file.exists())  # a ready download goes through its band's Rename

    async def test_undo_of_a_replace_puts_both_back(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        run_id = await self.replace(item)
        [new] = [
            o
            for o in await self.history.operations(run_id)
            if o.kind is OpKind.RENAME and o.dst == str(old)
        ]
        result = await self.undo.undo_card(run_id, [new.id])  # the queue card's Undo
        self.assertEqual(result.skipped, [])
        self.assertEqual(download.read_bytes(), b"video")
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertEqual((old.parent / f"{FILM}.en.srt").read_bytes(), b"old subtitle")
        self.assertFalse((self.folders.movies.parent / "Duplicates").exists())

    async def test_historys_undo_of_a_replace_puts_both_back(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        run_id = await self.replace(item)
        await self.undo.undo_run(run_id)
        self.assertEqual(download.read_bytes(), b"video")
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertEqual((old.parent / f"{FILM}.en.srt").read_bytes(), b"old subtitle")
        self.assertFalse((self.folders.movies.parent / "Duplicates").exists())

    async def test_historys_tick_on_the_download_puts_the_old_copy_back_too(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        run_id = await self.replace(item)
        [new] = [
            o
            for o in await self.history.operations(run_id)
            if o.kind is OpKind.RENAME and o.dst == str(old)
        ]
        await self.undo.undo_operations([new.id])  # History's "Undo selected", that row only
        self.assertEqual(download.read_bytes(), b"video")
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertEqual((old.parent / f"{FILM}.en.srt").read_bytes(), b"old subtitle")

    async def test_a_cards_undo_takes_its_own_old_copy_only(self) -> None:
        dune = self.library_copy()
        heat_folder = self.folders.movies / "Heat (2021)"
        heat_folder.mkdir()
        heat = heat_folder / "Heat (2021).mkv"
        heat.write_bytes(b"old heat")
        await self.use()
        for release in ("Dune.2021.2160p.WEB-DL.mkv", "Heat.2021.1080p.mkv"):
            self.folders.download(release)
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        items = sorted(self.queue.items, key=lambda i: i.path.name)
        assert all(held_in_library(i) for i in items)
        run_id = await self.replace(*items)
        ops = await self.history.operations(run_id)
        [new_dune] = [o for o in ops if o.kind is OpKind.RENAME and o.dst == str(dune)]
        await self.undo.undo_card(run_id, [new_dune.id])
        self.assertEqual(dune.read_bytes(), b"old copy")
        self.assertEqual(heat.read_bytes(), b"video")  # Heat stays replaced
        heat_aside = self.folders.movies.parent / "Duplicates" / "Movies" / "Heat (2021)"
        self.assertEqual((heat_aside / "Heat (2021).mkv").read_bytes(), b"old heat")
        self.assertFalse(self.aside.exists())

    async def test_replace_never_deletes(self) -> None:
        old = self.library_copy()
        item = await self.held()
        removed: list[Path] = []
        real_remove = fsops.remove_file

        def remove(path: Path) -> None:
            removed.append(path)
            real_remove(path)

        with (
            mock.patch.object(fsops, "remove_file", remove),
            mock.patch.object(os, "remove", side_effect=AssertionError("deleted")),
            mock.patch.object(os, "unlink", side_effect=AssertionError("deleted")),
        ):
            await self.replace(item)
        self.assertEqual(removed, [])
        self.assertEqual(old.read_bytes(), b"video")
        self.assertEqual((self.aside / f"{FILM}.mkv").read_bytes(), b"old copy")
        self.assertEqual((self.aside / f"{FILM}.en.srt").read_bytes(), b"old subtitle")

    async def test_a_copy_that_cant_move_aside_keeps_the_download_where_it_is(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        real_move = fsops.move_file

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src == old:
                raise PermissionError(13, "Access is denied")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        said: list[str] = []
        with mock.patch.object(fsops, "move_file", move):
            auto = self.renamer(Clock())
            auto._say = lambda text, **_kw: said.append(text)
            self.parts.executor._say = auto._say  # type: ignore[attr-defined]
            result = await auto.replace([item.id], source=RunSource.APP)
        self.assertEqual((result.renamed, result.failed, result.replaced), (0, 1, 0))
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertEqual(download.read_bytes(), b"video")
        text = f"Couldn't move the copy in the library aside: {FILM}.mkv (Access is denied)"
        self.assertIn(text, said)
        self.assertEqual(item.message, text)
        self.assertTrue(held_in_library(item))  # Replace is still offered

    def assert_put_back(self, old: Path, download: Path) -> None:
        """The library copy and its subtitle back where they were, the download where it was,
        no Duplicates folder left, nothing lost."""
        self.assertEqual(old.read_bytes(), b"old copy")
        self.assertEqual((old.parent / f"{FILM}.en.srt").read_bytes(), b"old subtitle")
        self.assertEqual(download.read_bytes(), b"video")
        self.assertFalse((self.folders.movies.parent / "Duplicates").exists())

    async def test_a_rename_failing_after_the_move_aside_puts_the_copy_back(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        real_move = fsops.move_file

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src == download:
                raise PermissionError(13, "Access is denied")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(fsops, "move_file", move):
            auto = self.renamer(Clock())
            result = await auto.replace([item.id], source=RunSource.APP)
        self.assertEqual((result.renamed, result.failed, result.replaced), (0, 1, 0))
        self.assert_put_back(old, download)
        self.assertTrue(held_in_library(item))
        [run] = await self.history.list_runs()  # journaled: the moves aside, undone
        ops = await self.history.operations(run.id)
        aside = [o for o in ops if o.kind is OpKind.RENAME and o.src == str(old)]
        self.assertEqual([o.undoable for o in aside], [False])

    async def test_stop_during_the_downloads_copy_puts_the_copy_back(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        real_move = fsops.move_file

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src == download:
                raise fsops.CopyCancelled(errno.ECANCELED, "stopped")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(fsops, "move_file", move):
            auto = self.renamer(Clock())
            result = await auto.replace([item.id], source=RunSource.APP)
        self.assertTrue(result.cancelled)
        self.assertEqual((result.renamed, result.replaced), (0, 0))
        self.assert_put_back(old, download)

    async def test_closing_during_the_downloads_rename_puts_the_copy_back(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        real_move = fsops.move_file
        started = threading.Event()
        release = threading.Event()

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src == download:
                started.set()
                release.wait(5)
                raise PermissionError(13, "Access is denied")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(fsops, "move_file", move):
            auto = self.renamer(Clock())
            task = asyncio.ensure_future(auto.replace([item.id], source=RunSource.APP))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()  # the window closes while the download's move is under way
            await asyncio.sleep(0.05)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_put_back(old, download)

    async def test_a_non_os_error_after_the_move_aside_puts_the_copy_back(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        history = self.parts.executor._history  # type: ignore[attr-defined]
        real_record = history.record_pending

        async def record(run_id: int, kind: OpKind, src: Path, dst: Path) -> int:
            if src == download:
                raise sqlite3.OperationalError("database is locked")
            return cast(int, await real_record(run_id, kind, src, dst))

        with mock.patch.object(history, "record_pending", record):
            auto = self.renamer(Clock())
            with self.assertRaises(sqlite3.OperationalError):
                await auto.replace([item.id], source=RunSource.APP)
        self.assert_put_back(old, download)

    async def test_a_put_back_that_fails_says_so_in_activity(self) -> None:
        old = self.library_copy()
        item = await self.held()
        download = item.path
        aside = self.aside / f"{FILM}.mkv"
        real_move = fsops.move_file

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src in (download, aside):  # the rename fails, and so does putting the copy back
                raise PermissionError(13, "Access is denied")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        said: list[tuple[str, object]] = []
        with mock.patch.object(fsops, "move_file", move):
            auto = self.renamer(Clock())
            self.parts.executor._say = (  # type: ignore[attr-defined]
                lambda text, **kw: said.append((text, kw.get("level")))
            )
            await auto.replace([item.id], source=RunSource.APP)
        self.assertEqual(aside.read_bytes(), b"old copy")  # still aside: History can undo it
        self.assertFalse(old.exists())
        text = f"Couldn't put the copy back in the library: {FILM}.mkv. History can undo it."
        self.assertIn((text, logging.WARNING), said)

    async def test_a_put_back_that_raises_says_so_in_activity(self) -> None:
        self.library_copy()
        item = await self.held()
        download = item.path
        real_move = fsops.move_file

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src == download:
                raise PermissionError(13, "Access is denied")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        said: list[tuple[str, object]] = []
        with (
            mock.patch.object(fsops, "move_file", move),
            mock.patch.object(UndoService, "undo_operations", side_effect=RuntimeError("boom")),
        ):
            auto = self.renamer(Clock())
            self.parts.executor._say = (  # type: ignore[attr-defined]
                lambda text, **kw: said.append((text, kw.get("level")))
            )
            await auto.replace([item.id], source=RunSource.APP)
        text = f"Couldn't put the copy back in the library: {FILM}.mkv. History can undo it."
        self.assertIn((text, logging.WARNING), said)
        self.assertFalse(any("\\" in t_ or "/" in t_ for t_, _ in said if "History" in t_))

    async def test_the_second_copy_failing_puts_the_first_back(self) -> None:
        old = self.library_copy()
        other = old.with_suffix(".mp4")
        other.write_bytes(b"old mp4")
        item = await self.held()
        download = item.path
        real_move = fsops.move_file

        def move(src: Path, dst: Path, **kwargs: object) -> None:
            if src == other:
                raise PermissionError(13, "Access is denied")
            real_move(src, dst, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(fsops, "move_file", move):
            auto = self.renamer(Clock())
            result = await auto.replace([item.id], source=RunSource.APP)
        self.assertEqual((result.renamed, result.failed, result.replaced), (0, 1, 0))
        self.assertEqual(other.read_bytes(), b"old mp4")
        self.assert_put_back(old, download)

    async def test_two_downloads_of_one_film_replace_once(self) -> None:
        old = self.library_copy()
        await self.use()
        for release in ("Dune.2021.2160p.WEB-DL.mkv", "Dune.2021.1080p.BluRay.mkv"):
            self.folders.download(release)
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        items = sorted(self.queue.items, key=lambda i: i.path.name)
        for item in items:  # a look holds them for each other; say both are in the library
            self.queue.hold(item.id, t(ALREADY_THERE), in_library=True)
        await self.replace(*items)
        videos = sorted(p.name for p in old.parent.iterdir() if p.suffix == ".mkv")
        self.assertEqual(videos, [old.name])  # no "(1)" copy in the library
        self.assertEqual([i.status for i in items].count(ItemStatus.DONE), 1)
        [waiting] = [i for i in items if i.status is ItemStatus.HELD]
        self.assertEqual(waiting.message, t(SAME_NAME))
        self.assertTrue(waiting.path.exists())

    async def test_nothing_there_to_replace_says_so_and_renames(self) -> None:
        self.library_copy()
        item = await self.held()
        shutil.rmtree(self.folders.movies / FILM)  # gone by hand before the click
        said: list[str] = []
        auto = self.renamer(Clock())
        auto._say = lambda text, **_kw: said.append(text)
        result = await auto.replace([item.id], source=RunSource.APP)
        self.assertEqual((result.renamed, result.replaced), (1, 0))
        self.assertIn("Nothing in the library to replace for 1 file: renamed as usual.", said)
        self.assertEqual((self.folders.movies / FILM / f"{FILM}.mkv").read_bytes(), b"video")


class ReplacePlanTest(unittest.TestCase):
    def test_a_share_root_library_needs_a_place(self) -> None:
        root = Path("//NAS/Movies/")
        settings = SETTINGS.model_copy(update={"movies_folder": str(root)})
        wanted = root / FILM / f"{FILM}.mkv"
        [run] = replace_runs([movie()], settings, DEFAULT_PRESETS, lambda p: p == wanted)
        self.assertEqual(run.plan.files, ())  # never inside the library itself
        self.assertEqual(run.held, {1: t(ALREADY_THERE)})
        place = Path(r"D:\Old copies")
        [run] = replace_runs(
            [movie()], settings, DEFAULT_PRESETS, lambda p: p == wanted, place=place
        )
        [aside] = run.plan.aside
        self.assertEqual(aside.source, wanted)
        self.assertEqual(aside.target, place / "Movies" / FILM / f"{FILM}.mkv")
        self.assertEqual(run.plan.files[0].target, wanted)

    def test_a_place_inside_the_library_is_refused(self) -> None:
        root = Path("//NAS/Movies/")
        settings = SETTINGS.model_copy(update={"movies_folder": str(root)})
        wanted = root / FILM / f"{FILM}.mkv"
        [run] = replace_runs(
            [movie()], settings, DEFAULT_PRESETS, lambda p: p == wanted, place=root / "Old"
        )
        self.assertEqual((run.plan.files, run.plan.aside), ((), ()))
        self.assertEqual(run.held, {1: t(ALREADY_THERE)})

    def test_a_folder_with_a_parent_ignores_the_place(self) -> None:
        wanted = MOVIES / FILM / f"{FILM}.mkv"
        [run] = replace_runs(
            [movie()], SETTINGS, DEFAULT_PRESETS, lambda p: p == wanted, place=Path(r"D:\Old")
        )
        [aside] = run.plan.aside
        self.assertEqual(aside.target, MOVIES.parent / "Duplicates" / "Movies" / FILM / wanted.name)

    def test_nothing_there_any_more_is_a_plain_rename(self) -> None:
        [run] = replace_runs([movie()], SETTINGS, DEFAULT_PRESETS, lambda p: False)
        self.assertEqual(run.plan.aside, ())
        self.assertEqual(run.plan.files[0].target, MOVIES / FILM / f"{FILM}.mkv")

    def test_the_folder_the_question_names(self) -> None:
        self.assertEqual(aside_folder(MOVIES, None), MOVIES.parent / "Duplicates")
        self.assertIsNone(aside_folder(Path("//NAS/Movies/"), None))
        place = Path(r"D:\Old copies")
        self.assertEqual(aside_folder(Path("//NAS/Movies/"), place), place)

    def test_held_in_library_is_the_holds_flag(self) -> None:
        queue = Queue(cast(Resolver, None), lambda _event: None)
        item = movie()
        queue.items.append(item)
        self.assertFalse(held_in_library(item))
        queue.hold(item.id, t(ALREADY_THERE))  # the words alone don't make it
        self.assertFalse(held_in_library(item))
        queue.hold(item.id, t(ALREADY_THERE), in_library=True)
        self.assertTrue(held_in_library(item))
        queue.hold(item.id, t("Not found at TMDB."))  # every hold says it anew
        self.assertFalse(held_in_library(item))
        item.status, item.in_library = ItemStatus.READY, True
        self.assertFalse(held_in_library(item))

    def test_a_library_run_says_which_holds_are_in_the_library(self) -> None:
        from app.forge.core.auto import library_runs

        wanted = MOVIES / FILM / f"{FILM}.mkv"
        [run] = library_runs([movie()], SETTINGS, DEFAULT_PRESETS, lambda p: p == wanted)
        self.assertEqual(run.in_library, frozenset({1}))
