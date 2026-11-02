"""A download counts as finished only when it has stopped changing and nothing holds it open."""

from __future__ import annotations

import os
from pathlib import Path

from app.forge.core.watch import QUIET_S, Watcher, candidates, is_sample
from tests.forge.base import ForgeTestCase


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def video(path: Path, size: int = 10, age_s: float = 3600, clock: Clock | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    when = (clock.now if clock else 0) - age_s
    os.utime(path, (when, when))
    return path


def watcher(clock: Clock, locked: set[Path] | None = None) -> Watcher:
    return Watcher(now=clock, can_open=lambda p: p not in (locked or set()))


class WatchTest(ForgeTestCase):
    def test_a_new_file_waits_for_a_second_look(self) -> None:
        clock = Clock(1_000_000)
        file = video(self.tmp / "ep.mkv", clock=clock)
        w = watcher(clock)
        assert w.finished([file]) == []  # first seen: not yet
        clock.now += 300
        assert w.finished([file]) == [file]

    def test_a_growing_file_is_not_finished(self) -> None:
        clock = Clock(1_000_000)
        file = video(self.tmp / "ep.mkv", size=10, clock=clock)
        w = watcher(clock)
        w.finished([file])
        clock.now += 300
        video(file, size=20, clock=clock)  # more arrived since the last look
        assert w.finished([file]) == []
        clock.now += 300
        assert w.finished([file]) == [file]

    def test_a_file_touched_recently_is_not_finished(self) -> None:
        clock = Clock(1_000_000)
        file = video(self.tmp / "ep.mkv", age_s=300, clock=clock)  # written 5 minutes ago
        w = watcher(clock)
        w.finished([file])
        assert w.finished([file]) == []  # the same twice, but not quiet for 10 minutes
        clock.now += QUIET_S
        assert w.finished([file]) == [file]

    def test_a_locked_file_is_not_finished(self) -> None:
        clock = Clock(1_000_000)
        file = video(self.tmp / "ep.mkv", clock=clock)
        locked = {file}
        w = watcher(clock, locked)
        w.finished([file])
        clock.now += 300
        assert w.finished([file]) == []
        locked.clear()
        assert w.finished([file]) == [file]

    def test_temporary_and_sample_files_are_never_candidates(self) -> None:
        for name in (
            "ep.mkv.part",
            "ep2.!qB",
            "Show.S01E01/Sample/show-sample.mkv",
            "Show.S01E01/sample.mkv",
            "@eaDir/x.mkv",
            "#recycle/x.mkv",
            ".hidden/x.mkv",
            "~x.mkv",
            ".x.mkv",
            "notes.txt",
        ):
            video(self.tmp / name)
        video(self.tmp / "busy.mkv")
        video(self.tmp / "busy.mkv.aria2")  # the downloader is still writing busy.mkv
        keep = video(self.tmp / "Show.S01E01" / "show.s01e01.mkv")
        assert candidates(self.tmp) == [keep]
        assert is_sample(Path("Show.S01E01-SAMPLE.mkv")) and not is_sample(Path("Samples.Of.Life.mkv"))

    def test_files_in_subfolders_count(self) -> None:
        a = video(self.tmp / "A" / "B" / "movie.2020.mkv")
        b = video(self.tmp / "top.mp4")
        assert sorted(candidates(self.tmp)) == sorted([a, b])

    def test_a_vanished_file_is_forgotten(self) -> None:
        clock = Clock(1_000_000)
        file = video(self.tmp / "ep.mkv", clock=clock)
        w = watcher(clock)
        w.finished([file])
        file.unlink()
        clock.now += 300
        assert w.finished([file]) == []  # listed, then gone before its size was read: skipped
        assert w.finished([]) == []
        video(file, clock=clock)
        clock.now += 300
        assert w.finished([file]) == []  # back again: starts over, waits for a second look

    def test_an_unreadable_root_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            candidates(self.tmp / "not-there")

    def test_exclusive_open_sees_a_file_in_use(self) -> None:
        from app.forge.core.watch import exclusive_open

        file = video(self.tmp / "ep.mkv")
        assert exclusive_open(file)
        with open(file, "rb"):
            assert not exclusive_open(file)  # a downloader (or anything) still has it open
        assert not exclusive_open(self.tmp / "gone.mkv")

    def test_a_folder_still_downloading_holds_back_its_finished_files(self) -> None:
        pack = self.tmp / "Show.S01.1080p"
        video(pack / "Show.S01E01.mkv")
        video(pack / "Show.S01E02.mkv.!qB")  # the rest of the pack is still coming in
        done = video(self.tmp / "Other.S01" / "Other.S01E01.mkv")
        assert candidates(self.tmp) == [done]

    def test_extras_are_never_candidates(self) -> None:
        from app.forge.core.watch import is_extra

        for name in (
            "Dune.2021/Dune.2021.1080p-trailer.mkv",
            "Dune.2021/Extras/Making Of.mkv",
            "Dune.2021/Featurettes/Sandworms.mkv",
            "Show.S01/Show.S01.Behind.the.Scenes.mkv",
            "Show.S01/Deleted Scenes.mkv",
        ):
            video(self.tmp / name)
        movie = video(self.tmp / "Dune.2021" / "Dune.2021.1080p.mkv")
        assert candidates(self.tmp) == [movie]
        assert is_extra(Path("Dune.Trailer.mkv")) and not is_extra(Path("Trailer.Park.Boys.S01E01.mkv"))

    def test_a_preallocated_file_is_not_finished(self) -> None:
        from app.forge.core.watch import looks_filled

        clock = Clock(1_000_000)
        empty = self.tmp / "stalled.mkv"
        empty.write_bytes(bytes(2_000_000))  # full size, but nothing written yet: all zeros
        os.utime(empty, (clock.now - 3600, clock.now - 3600))
        real = video(self.tmp / "real.mkv", size=2_000_000, clock=clock)
        assert not looks_filled(empty) and looks_filled(real)
        w = Watcher(now=clock, can_open=lambda p: True)
        w.finished([empty, real])
        clock.now += 300
        assert w.finished([empty, real]) == [real]
