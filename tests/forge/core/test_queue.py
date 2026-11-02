"""The queue: items, background lookups, chunked parsing on the loop, watched items (MediaForge's
tests/core/test_queue.py, with a fake resolver)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from app.forge.core import queue as queue_module
from app.forge.core.matcher import Resolution
from app.forge.core.queue import PARSE_CHUNK, ItemStatus, Queue, QueueEvent, scan_paths
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia, TypeHint
from app.models.tmdb import TMDBTitle
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase


class FakeResolver:
    def __init__(self, state: Callable[[ParsedMedia], MatchState] | None = None) -> None:
        self.state = state or (lambda p: MatchState.CONFIDENT)
        self.in_flight = 0
        self.max_in_flight = 0
        self.resolved: list[ParsedMedia] = []

    async def resolve(self, parsed: ParsedMedia) -> Resolution:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        self.resolved.append(parsed)
        return Resolution(media=ResolvedMedia.offline(parsed, self.state(parsed)))

    async def apply_choice(
        self, parsed: ParsedMedia, chosen: TMDBTitle | None, *, remember: bool = True
    ) -> ResolvedMedia:
        if chosen is None:
            return ResolvedMedia.offline(parsed, MatchState.SKIPPED)
        return ResolvedMedia(
            parsed=parsed,
            title=chosen.title,
            year=chosen.year,
            episode_title=None,
            match_state=MatchState.REMEMBERED,
            tmdb_id=chosen.id,
        )


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"v")
    return path


def make_queue(resolver: FakeResolver | None = None) -> tuple[Queue, list[QueueEvent]]:
    events: list[QueueEvent] = []
    return Queue(resolver or FakeResolver(), events.append), events


class ScanPathsTest(ForgeTestCase):
    def test_scan_paths_sorted_and_tagged(self) -> None:
        touch(self.tmp / "b.mkv")
        touch(self.tmp / "A.mkv")
        f = touch(self.tmp / "c.avi")
        found = scan_paths([self.tmp, f], recursive=True).files
        assert found[:2] == [(self.tmp / "A.mkv", self.tmp), (self.tmp / "b.mkv", self.tmp)]
        assert (f, None) in found


class QueueTest(ForgeAsyncTestCase):
    def spy_to_thread(self) -> list[Any]:
        seen: list[Any] = []
        real = asyncio.to_thread

        async def spy(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            seen.append(func)
            return await real(func, *args, **kwargs)

        self.enterContext(mock.patch.object(asyncio, "to_thread", spy))
        return seen

    async def test_add_folder_recursive_and_flat(self) -> None:
        touch(self.tmp / "a.mkv")
        touch(self.tmp / "sub" / "b.mp4")
        q, _ = make_queue()
        assert await q.add_paths([self.tmp], recursive=False) == 1
        q.clear()
        assert await q.add_paths([self.tmp], recursive=True) == 2
        assert {i.path.name for i in q.items} == {"a.mkv", "b.mp4"}
        assert all(i.dropped_folder == self.tmp and i.root == self.tmp for i in q.items)

    async def test_single_file_root_is_its_folder(self) -> None:
        f = touch(self.tmp / "dl" / "a.mkv")
        q, _ = make_queue()
        await q.add_paths([f], recursive=True)
        [item] = q.items
        assert item.dropped_folder is None and item.root == self.tmp / "dl"

    async def test_non_videos_ignored(self) -> None:
        touch(self.tmp / "a.srt")
        touch(self.tmp / "b.nfo")
        q, _ = make_queue()
        assert await q.add_paths([self.tmp, self.tmp / "a.srt"], recursive=True) == 0

    async def test_same_file_added_twice_appears_once_and_folder_root_wins(self) -> None:
        f = touch(self.tmp / "show" / "a.mkv")
        q, events = make_queue()
        await q.add_paths([f], recursive=True)
        await q.add_paths([f], recursive=True)
        assert len(q.items) == 1
        assert await q.add_paths([self.tmp / "show"], recursive=True) == 0
        assert len(q.items) == 1
        assert q.items[0].dropped_folder == self.tmp / "show"
        assert any(e.kind == "updated" for e in events)

    async def test_lookups_set_ready_or_needs_choice(self) -> None:
        touch(self.tmp / "Dune.2021.mkv")
        touch(self.tmp / "The.Office.S02E01.mkv")
        resolver = FakeResolver(
            lambda p: MatchState.UNCERTAIN if p.kind is MediaKind.EPISODE else MatchState.CONFIDENT
        )
        q, _ = make_queue(resolver)
        await q.add_paths([self.tmp], recursive=True)
        assert all(i.status is ItemStatus.LOOKING_UP for i in q.items)
        await q.wait_idle()
        status = {i.path.name: i.status for i in q.items}
        assert status == {
            "Dune.2021.mkv": ItemStatus.READY,
            "The.Office.S02E01.mkv": ItemStatus.NEEDS_CHOICE,
        }
        assert [i.path.name for i in q.uncertain()] == ["The.Office.S02E01.mkv"]

    async def test_media_before_lookup_is_offline_parse(self) -> None:
        touch(self.tmp / "Dune.2021.mkv")
        q, _ = make_queue()
        await q.add_paths([self.tmp], recursive=True)
        assert q.items[0].media.title == "Dune" and q.items[0].media.year == 2021

    async def test_choose_match_applies_to_same_key(self) -> None:
        touch(self.tmp / "The.Office.S02E01.mkv")
        touch(self.tmp / "The.Office.S02E02.mkv")
        q, _ = make_queue(FakeResolver(lambda p: MatchState.UNCERTAIN))
        await q.add_paths([self.tmp], recursive=True)
        await q.wait_idle()
        chosen = TMDBTitle(id=2316, title="The Office", date="2005-03-24")
        await q.choose_match(q.items[0].id, chosen)
        assert all(i.status is ItemStatus.READY for i in q.items)
        assert all(i.media.tmdb_id == 2316 for i in q.items)

    async def test_set_hint_reparses_all(self) -> None:
        touch(self.tmp / "Show.S01E02.mkv")
        q, _ = make_queue()
        await q.add_paths([self.tmp], recursive=True)
        await q.wait_idle()
        assert q.items[0].parsed.kind is MediaKind.EPISODE
        await q.set_hint(TypeHint.MOVIE)
        await q.wait_idle()
        assert q.hint is TypeHint.MOVIE
        [item] = q.items  # a fresh name: mypy keeps the narrowed kind of q.items[0]
        assert item.parsed.kind is MediaKind.MOVIE
        assert q.items[0].status is ItemStatus.READY

    async def test_events_emitted(self) -> None:
        touch(self.tmp / "a.mkv")
        q, events = make_queue()
        await q.add_paths([self.tmp], recursive=True)
        await q.wait_idle()
        assert [e.kind for e in events][:1] == ["added"]
        assert any(e.kind == "updated" and e.ids == (q.items[0].id,) for e in events)

    async def test_remove_and_clear(self) -> None:
        touch(self.tmp / "a.mkv")
        touch(self.tmp / "b.mkv")
        q, events = make_queue()
        await q.add_paths([self.tmp], recursive=True)
        first = q.items[0].id
        q.remove(first)
        assert [i.id for i in q.items] != [first] and len(q.items) == 1
        assert events[-1] == QueueEvent(kind="removed", ids=(first,))
        q.clear()
        assert q.items == [] and events[-1].kind == "cleared"
        await q.wait_idle()  # cancelled lookups don't hang

    async def test_rescan_after_toggle(self) -> None:
        folder = self.tmp / "drop"
        touch(folder / "top.mkv")
        touch(folder / "sub" / "deep.mkv")
        loose = touch(self.tmp / "drop-loose" / "loose.mkv")
        q, _ = make_queue()
        await q.add_paths([folder, loose], recursive=False)
        assert {i.path.name for i in q.items} == {"top.mkv", "loose.mkv"}
        await q.rescan(recursive=True)
        assert {i.path.name for i in q.items} == {"top.mkv", "deep.mkv", "loose.mkv"}

    async def test_lookup_concurrency_limited(self) -> None:
        for n in range(20):
            touch(self.tmp / f"Movie.{n}.2000.mkv")
        resolver = FakeResolver()
        q, _ = make_queue(resolver)
        await q.add_paths([self.tmp], recursive=True)
        await q.wait_idle()
        assert 1 < resolver.max_in_flight <= 6

    async def test_scan_runs_off_loop(self) -> None:
        touch(self.tmp / "a.mkv")
        seen = self.spy_to_thread()
        q, _ = make_queue()
        await q.add_paths([self.tmp], recursive=True)
        assert scan_paths in seen

    async def test_parsing_stays_on_the_loop_and_yields(self) -> None:
        # CPU-heavy parsing in a worker thread starves the Tk thread of the GIL, so names are
        # parsed on the event loop in small chunks instead.
        assert PARSE_CHUNK == 8
        for n in range(120):
            touch(self.tmp / f"Movie.{n}.2000.mkv")
        offloaded = self.spy_to_thread()
        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0)

        q, _ = make_queue()
        ticking = asyncio.create_task(ticker())
        await q.add_paths([self.tmp], recursive=True)
        before_parse = ticks
        stop.set()
        await ticking
        assert offloaded == [scan_paths]
        assert before_parse > 5

    async def test_a_timer_runs_between_pieces_of_parsing(self) -> None:
        # F-33: Tk's pump is a timer on the loop. After a piece of parsing it must get its turn
        # before the next piece (with sleep(0) two pieces of 8 names ran back to back), and a
        # piece stops after PARSE_PIECE_S of work. Each name here takes 12 ms.
        def slow_parse(name: str, hint: TypeHint = TypeHint.AUTO) -> ParsedMedia:
            end = time.perf_counter() + 0.012
            while time.perf_counter() < end:
                pass
            return ParsedMedia(source_name=name, kind=MediaKind.MOVIE, title=name)

        names = [f"Movie.{n}.2000.mkv" for n in range(40)]
        gaps: list[float] = []
        stop = asyncio.Event()

        async def pump() -> None:  # as async-tkinter-loop pumps Tk: a 10 ms timer
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        self.enterContext(mock.patch.object(queue_module, "parse_filename", slow_parse))
        pumping = asyncio.create_task(pump())
        await asyncio.sleep(0.02)
        parsed = await queue_module._parse_all(names, TypeHint.AUTO)
        stop.set()
        await pumping
        assert [p.source_name for p in parsed] == names
        # A piece is at most PARSE_PIECE_S plus one name (~42 ms), plus the timer's own wait;
        # two pieces of 8 names back to back would be ~190 ms.
        assert max(gaps) < 0.1, [round(g * 1000) for g in gaps]

    async def test_plan_inputs(self) -> None:
        touch(self.tmp / "Dune.2021.mkv")
        q, _ = make_queue()
        await q.add_paths([self.tmp], recursive=True)
        await q.wait_idle()
        [pi] = q.plan_inputs()
        assert (pi.source, pi.root, pi.dropped_folder, pi.media.title) == (
            self.tmp / "Dune.2021.mkv",
            self.tmp,
            self.tmp,
            "Dune",
        )
        assert q.plan_inputs(ids=[]) == []

    async def test_two_drops_at_once_add_each_file_once(self) -> None:
        # Two overlapping add_paths calls (parsing takes a while) doubled the queue once.
        for n in range(40):
            touch(self.tmp / f"Movie.{n}.2000.mkv")
        q, _ = make_queue()
        await asyncio.gather(q.add_paths([self.tmp], True), q.add_paths([self.tmp], True))
        assert len(q.items) == 40

    async def test_clear_during_add_discards_it(self) -> None:
        for n in range(5):
            touch(self.tmp / f"Movie.{n}.2000.mkv")
        gate = asyncio.Event()
        real_parse = queue_module._parse_all

        async def held_parse(names: Any, hint: Any) -> Any:
            await gate.wait()
            return await real_parse(names, hint)

        self.enterContext(mock.patch.object(queue_module, "_parse_all", held_parse))
        q, _ = make_queue()
        task = asyncio.create_task(q.add_paths([self.tmp], True))
        await asyncio.sleep(0.2)  # scanned, now parsing
        q.clear()
        gate.set()
        await task
        await q.wait_idle()
        assert q.items == []

    async def test_finished_items_stay_finished(self) -> None:
        # Changing the preset's type or picking a match must not turn renamed files back into
        # "to do", and their new path must be remembered.
        touch(self.tmp / "Show.S01E01.mkv")
        touch(self.tmp / "Show.S01E02.mkv")
        q, _ = make_queue()
        await q.add_paths([self.tmp], True)
        await q.wait_idle()
        first, second = q.items
        renamed = self.tmp / "Show - S01E01.mkv"
        q.mark_finished(first.id, renamed)
        await q.set_hint(TypeHint.MOVIE)
        await q.wait_idle()
        assert first.status is ItemStatus.DONE and first.path == renamed
        assert second.status is ItemStatus.READY and second.parsed.kind is MediaKind.MOVIE
        await q.choose_match(second.id, TMDBTitle(id=1, title="Show"))
        assert first.status is ItemStatus.DONE
        assert [i.id for i in q.pending()] == [second.id]

    # --- watched downloads (MediaForge 1.3) ---------------------------------------------------

    async def test_watched_files_are_parsed_without_the_presets_hint(self) -> None:
        q, _ = make_queue()
        await q.set_hint(TypeHint.TV)  # the window's preset is a Series one
        file = touch(self.tmp / "Dune.2021.mkv")
        [item_id] = await q.add_watched([(file, self.tmp)])
        await q.wait_idle()
        item = q.get(item_id)
        assert item is not None and item.watched == self.tmp
        assert item.parsed.kind is MediaKind.MOVIE
        assert item.root == self.tmp  # where a dropped file would be renamed; unused here

    async def test_set_hint_leaves_watched_files_alone(self) -> None:
        q, _ = make_queue()
        [watched] = await q.add_watched([(touch(self.tmp / "w" / "Dune.2021.mkv"), self.tmp / "w")])
        await q.add_paths([touch(self.tmp / "d" / "Heat.1995.mkv")], recursive=False)
        await q.wait_idle()
        await q.set_hint(TypeHint.TV)
        await q.wait_idle()
        kinds = {i.path.name: i.parsed.kind for i in q.items}
        assert kinds == {"Dune.2021.mkv": MediaKind.MOVIE, "Heat.1995.mkv": MediaKind.EPISODE}
        assert q.get(watched) is not None

    async def test_a_watched_file_is_added_once(self) -> None:
        q, _ = make_queue()
        file = touch(self.tmp / "Show.S01E01.mkv")
        assert len(await q.add_watched([(file, self.tmp)])) == 1
        assert await q.add_watched([(file, self.tmp)]) == []
        await q.add_paths([file], recursive=False)  # dropped on the window as well
        assert len(q.items) == 1

    async def test_hold_keeps_a_reason(self) -> None:
        q, events = make_queue()
        [item_id] = await q.add_watched([(touch(self.tmp / "Dune.mkv"), self.tmp)])
        await q.wait_idle()
        q.hold(item_id, "No year in the name: is it a movie?")
        item = q.get(item_id)
        assert item is not None and item.status is ItemStatus.HELD
        assert item.message == "No year in the name: is it a movie?"
        assert events[-1] == QueueEvent(kind="updated", ids=(item_id,))
        assert item in q.pending()  # not renamed yet

    async def test_lookup_again_clears_the_message_and_looks_up(self) -> None:
        resolver = FakeResolver()
        q, _ = make_queue(resolver)
        [item_id] = await q.add_watched([(touch(self.tmp / "Dune.2021.mkv"), self.tmp)])
        await q.wait_idle()
        q.hold(item_id, "held")
        q.lookup_again([item_id, 999])
        item = q.get(item_id)
        assert item is not None and item.message == "" and item.status is ItemStatus.LOOKING_UP
        await q.wait_idle()
        again = q.get(item_id)  # a fresh name: mypy keeps item.status narrowed to LOOKING_UP
        assert again is not None and again.status is ItemStatus.READY and len(resolver.resolved) == 2

    async def test_purge_finished_drops_yesterdays_done(self) -> None:
        q, events = make_queue()
        ids = await q.add_watched(
            [(touch(self.tmp / f"S01E0{n}.mkv"), self.tmp) for n in (1, 2, 3)]
        )
        await q.wait_idle()
        q.mark_finished(ids[0], self.tmp / "x1.mkv")
        q.mark_finished(ids[1], self.tmp / "x2.mkv")
        old, new = (q.get(i) for i in ids[:2])
        assert old is not None and new is not None
        old.finished_at = 1000.0
        new.finished_at = 5000.0
        assert q.purge_finished(before=2000.0) == 1
        assert [i.id for i in q.items] == ids[1:]  # yesterday's done went; today's and pending stay
        assert events[-1] == QueueEvent(kind="removed", ids=(ids[0],))

    async def test_forget_watched_drops_files_that_went_away(self) -> None:
        q, _ = make_queue()
        a, b = touch(self.tmp / "a.mkv"), touch(self.tmp / "b.mkv")
        ids = await q.add_watched([(a, self.tmp), (b, self.tmp)])
        dropped = await q.add_paths([touch(self.tmp / "other" / "c.mkv")], recursive=False)
        await q.wait_idle()
        assert dropped == 1
        present = {queue_module._key(a)}
        assert q.forget_watched(present) == 1
        assert {i.path.name for i in q.items} == {"a.mkv", "c.mkv"}  # dropped files aren't watched
        assert q.get(ids[0]) is not None

    async def test_mark_finished_keeps_the_run(self) -> None:
        q, _ = make_queue()
        [item_id] = await q.add_watched([(touch(self.tmp / "a.mkv"), self.tmp)])
        await q.wait_idle()
        q.mark_finished(item_id, self.tmp / "b.mkv", run_id=7)
        item = q.get(item_id)
        assert item is not None and item.run_id == 7 and item.status is ItemStatus.DONE

    async def test_mark_finished_without_a_new_path_is_skipped(self) -> None:
        q, _ = make_queue()
        [item_id] = await q.add_watched([(touch(self.tmp / "a.mkv"), self.tmp)])
        await q.wait_idle()
        q.mark_finished(item_id, None)
        item = q.get(item_id)
        assert item is not None and item.status is ItemStatus.SKIPPED
        assert item.finished_at is not None and q.pending() == []

    async def test_rescan_keeps_watched_files(self) -> None:
        q, _ = make_queue()
        await q.add_watched([(touch(self.tmp / "w" / "a.mkv"), self.tmp / "w")])
        touch(self.tmp / "d" / "b.mkv")
        await q.add_paths([self.tmp / "d"], recursive=True)
        await q.wait_idle()
        await q.rescan(recursive=True)
        await q.wait_idle()
        assert sorted(i.path.name for i in q.items) == ["a.mkv", "b.mkv"]
        assert [i.watched for i in q.items if i.path.name == "a.mkv"] == [self.tmp / "w"]
