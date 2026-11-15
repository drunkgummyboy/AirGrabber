"""Duplicates in the library folders: found, the biggest kept, the rest moved aside (undoable)."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from app.forge.core import duplicates as duplicates_module
from app.forge.core.duplicates import (
    QUARANTINE,
    Copy,
    Duplicate,
    episode_key,
    film_key,
    find_duplicates,
    needs_a_place,
    place_problem,
    quarantine_plan,
    quarantine_target,
)
from app.forge.core.executor import Executor
from app.forge.core.undo import UndoService
from app.forge.models.history import RunSource
from tests.forge.core.journal_base import FakePosters, HistoryTestCase


def video(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class KeyTests(unittest.TestCase):
    def test_episodes(self) -> None:
        root = Path("S:/Series")
        self.assertEqual(
            episode_key(root, root / "The Ark" / "Season 02" / "The Ark - S02E05 - Lost.mkv"),
            ("The Ark S02E05", ("the ark", 2, 5, 5)),
        )
        self.assertEqual(
            episode_key(root, root / "Lost" / "Lost.s01e01-e02.720p.mkv"),
            ("Lost S01E01-E02", ("lost", 1, 1, 2)),
        )
        self.assertIsNone(episode_key(root, root / "loose S01E01.mkv"))  # no show folder
        self.assertIsNone(episode_key(root, root / "Lost" / "Lost - Pilot.mkv"))

    def test_films(self) -> None:
        self.assertEqual(film_key(Path("Dune (2021) - 2160p.mkv")), ("Dune (2021)", "dune (2021)"))
        self.assertEqual(film_key(Path("Dune.Part.Two.1080p.WEB-DL.mkv")), ("Dune Part Two", "dune part two"))


class FindTests(HistoryTestCase):
    def library(self) -> tuple[Path, Path]:
        series, movies = self.tmp / "Media" / "Series", self.tmp / "Media" / "Movies"
        video(series / "The Ark" / "Season 01" / "The Ark - S01E01 - Pilot.mkv", 300)
        video(series / "The Ark" / "Season 01" / "The.Ark.S01E01.720p.mkv", 100)
        video(series / "The Ark" / "Season 01" / "The Ark - S01E02 - Two.mkv", 300)
        video(series / "Lost" / "Season 01" / "Lost - S01E01 - Pilot.mkv", 50)  # another show
        video(movies / "Dune (2021)" / "Dune (2021).mkv", 900)
        video(movies / "Dune (2021)" / "Dune (2021) - Trailer.mp4", 20)  # no copy
        video(movies / "Old" / "Dune (2021) 720p.mkv", 400)
        video(movies / "Heat (1995)" / "Heat (1995).mkv", 500)
        video(movies / "Heat (1995)" / "Extras" / "Heat (1995) - Making of.mkv", 10)  # no copy
        return series, movies

    async def test_found_and_moved_aside_then_undone(self) -> None:
        series, movies = self.library()
        found = find_duplicates(series, movies)
        self.assertEqual([d.label for d in found], ["Dune (2021)", "The Ark S01E01"])
        ark = found[1]
        self.assertEqual(ark.keep.path.name, "The Ark - S01E01 - Pilot.mkv")
        self.assertEqual([c.path.name for c in ark.extra], ["The.Ark.S01E01.720p.mkv"])

        plan = quarantine_plan(found, [series, movies])
        executor = Executor(self.history, FakePosters(), None)
        result = await executor.run(
            plan,
            source=RunSource.APP,
            preset_name=None,
            on_progress=lambda _event: None,
            cancel=asyncio.Event(),
            check_files=False,
        )
        self.assertEqual(result.renamed, 2)
        aside = self.tmp / "Media" / QUARANTINE
        self.assertTrue((aside / "Series" / "The Ark" / "Season 01" / "The.Ark.S01E01.720p.mkv").exists())
        self.assertTrue((aside / "Movies" / "Old" / "Dune (2021) 720p.mkv").exists())
        self.assertFalse((series / "The Ark" / "Season 01" / "The.Ark.S01E01.720p.mkv").exists())
        self.assertTrue((movies / "Dune (2021)" / "Dune (2021).mkv").exists())  # kept

        assert result.run_id is not None
        await UndoService(self.history).undo_run(result.run_id)
        self.assertTrue((series / "The Ark" / "Season 01" / "The.Ark.S01E01.720p.mkv").exists())
        self.assertTrue((movies / "Old" / "Dune (2021) 720p.mkv").exists())

    async def test_no_folders(self) -> None:
        self.assertEqual(find_duplicates(None, None), [])
        self.assertEqual(find_duplicates(self.tmp / "nowhere", None), [])

    def test_the_target(self) -> None:
        root = Path("S:/Media/Series")
        self.assertEqual(
            quarantine_target(root / "Lost" / "x.mkv", [root]),
            Path("S:/Media/Duplicates/Series/Lost/x.mkv"),
        )
        self.assertIsNone(quarantine_target(Path("D:/other/x.mkv"), [root]))

    def test_a_share_root_never_gets_its_copies_inside_itself(self) -> None:
        share, drive, beside = Path("//NAS/Movies/"), Path("M:/"), Path("//NAS/Media/Series")
        extra = Copy(share / "Old" / "Dune (2021) 720p.mkv", 400)
        found = [Duplicate("Dune (2021)", Copy(share / "Dune (2021).mkv", 900), (extra,))]
        # No folder beside a share or drive root: one has to be chosen first.
        self.assertTrue(needs_a_place([beside, share]))
        self.assertTrue(needs_a_place([drive]))
        self.assertFalse(needs_a_place([beside]))
        self.assertIsNone(quarantine_target(extra.path, [share]))
        self.assertEqual(quarantine_plan(found, [share]).files, ())
        # Inside either library folder is refused; elsewhere they go there, under the share's name.
        for inner in (share, share / "Duplicates", beside / "Aside"):
            self.assertIsNotNone(place_problem(inner, [beside, share]), inner)
        self.assertIsNone(place_problem(Path("//NAS/Backup"), [beside, share]))
        plan = quarantine_plan(found, [beside, share], place=Path("//NAS/Backup"))
        [moved] = plan.files
        self.assertEqual(moved.target, Path("//NAS/Backup/Movies/Old/Dune (2021) 720p.mkv"))
        self.assertEqual(
            quarantine_target(drive / "Lost" / "x.mkv", [drive], place=Path("D:/Aside")),
            Path("D:/Aside/M/Lost/x.mkv"),
        )

    def test_a_duplicates_folder_is_not_scanned(self) -> None:
        movies = self.tmp / "Movies"
        video(movies / "Dune (2021)" / "Dune (2021).mkv", 900)
        video(movies / QUARANTINE / "Movies" / "Old" / "Dune (2021) 720p.mkv", 400)
        video(movies / "duplicates" / "Dune (2021) 480p.mkv", 100)
        video(movies / "Heat (1995)" / "Duplicates" / "Heat (1995).mkv", 500)  # not at the top
        video(movies / "Heat (1995)" / "Heat (1995) 720p.mkv", 300)
        self.assertEqual([d.label for d in find_duplicates(None, movies)], ["Heat (1995)"])

    def test_a_share_root_and_a_normal_library_folder_together(self) -> None:
        # Review fix 1: the chosen place is only for the share root; the other folder keeps its
        # Duplicates beside it, and its parent can't be the place (its copies would land on
        # their own paths).
        series, movies = Path("D:/Media/Series"), Path("//NAS/Movies/")
        roots = [series, movies]
        episode = Copy(series / "Lost" / "Season 01" / "Lost.S01E01.720p.mkv", 100)
        film = Copy(movies / "Old" / "Dune (2021) 720p.mkv", 400)
        found = [
            Duplicate("Dune (2021)", Copy(movies / "Dune (2021).mkv", 900), (film,)),
            Duplicate("Lost S01E01", Copy(series / "Lost" / "Season 01" / "Pilot.mkv", 300), (episode,)),
        ]
        self.assertTrue(needs_a_place(roots))
        self.assertIsNotNone(place_problem(Path("D:/Media"), roots))  # D:/Media/Series is one
        self.assertIsNotNone(place_problem(Path("//NAS/Movies/Aside"), roots))
        self.assertIsNone(place_problem(Path("E:/Aside"), roots))
        plan = quarantine_plan(found, roots, place=Path("E:/Aside"))
        self.assertEqual(
            {(f.source, f.target) for f in plan.files},
            {
                (film.path, Path("E:/Aside/Movies/Old/Dune (2021) 720p.mkv")),
                (episode.path, Path("D:/Media/Duplicates/Series/Lost/Season 01/Lost.S01E01.720p.mkv")),
            },
        )
        # Even if a place slipped through, a move onto its own path is never planned.
        plan = quarantine_plan(found, roots, place=Path("D:/Media"))
        self.assertTrue(all(f.source != f.target for f in plan.files))

    def test_a_move_onto_itself_is_never_planned(self) -> None:
        extra = Copy(Path("//NAS/Movies/Old/Dune (2021) 720p.mkv"), 400)
        found = [Duplicate("Dune (2021)", Copy(Path("//NAS/Movies/Dune (2021).mkv"), 900), (extra,))]
        with mock.patch.object(duplicates_module, "quarantine_target", lambda *_a: extra.path):
            self.assertEqual(quarantine_plan(found, [Path("//NAS/Movies/")]).files, ())


if __name__ == "__main__":
    unittest.main()
