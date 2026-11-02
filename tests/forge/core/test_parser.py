"""MediaForge's parser (guessit with MediaForge's options and wrapper rules), unchanged in
behaviour, and that it is optional: without guessit nothing breaks at import."""

from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from app.forge.core import parser
from app.forge.core.parser import human_title, is_video, parse_filename, parser_ready
from app.forge.models.media import MediaKind, TypeHint
from tests.forge.base import ForgeTestCase

M, E = MediaKind.MOVIE, MediaKind.EPISODE

type Case = tuple[str, str, int | None, int | None, int | None, int | None, str | None, MediaKind]

# (filename, title, year, season, episode, episode_end, episode_title, kind)
# Expected values come from guessit 4.4 with MediaForge's options and wrapper rules.
CASES: list[Case] = [
    # --- names from the owner's own history ---
    (
        "VisionQuest.S01E02.Theseus.1080p.DSNP.WEB-DL.ENG.Atmos.ITA.DDP5.1.H264-TBK.mkv",
        "VisionQuest",
        None,
        1,
        2,
        None,
        "Theseus",
        E,
    ),
    (
        "VisionQuest.S01E01.No.Strings.1080p.DSNP.WEB-DL.ENG.Atmos.ITA.DDP5.1.H264-TBK.mkv",
        "VisionQuest",
        None,
        1,
        1,
        None,
        "No Strings",
        E,
    ),
    (
        "Gold Rush S17E03 Diagnosing Rick 1080p AMZN WEB-DL DDP2 0 H 264-NTb[EZTVx.to].mkv",
        "Gold Rush",
        None,
        17,
        3,
        None,
        "Diagnosing Rick",
        E,
    ),
    ("Lanterns (2026) - S01E08 - _.mkv", "Lanterns", 2026, 1, 8, None, None, E),
    (
        "Lanterns.2026.S01E08.1080p.WEB.h264-ETHEL[EZTVx.to].mkv",
        "Lanterns",
        2026,
        1,
        8,
        None,
        None,
        E,
    ),
    # --- guessit quirks the wrapper options fix ---
    (
        "Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.HDR.H.265-FLUX.mkv",
        "Dune Part Two",
        2024,
        None,
        None,
        None,
        None,
        M,
    ),  # 'part' excluded
    (
        "Spider-Man.No.Way.Home.2021.1080p.WEB-DL.mkv",
        "Spider-Man No Way Home",
        2021,
        None,
        None,
        None,
        None,
        M,
    ),  # 'release_group' excluded
    (
        "the.last.of.us.s01e03.long.long.time.1080p.web.h264-glhf.mkv",
        "The Last Of Us",
        None,
        1,
        3,
        None,
        "Long Long Time",
        E,
    ),  # 'country' excluded
    ("Kill.Bill.Vol.1.2003.mkv", "Kill Bill Vol 1", 2003, None, None, None, None, M),
    (
        "Back.to.the.Future.Part.II.1989.mkv",
        "Back To The Future Part II",
        1989,
        None,
        None,
        None,
        None,
        M,
    ),
    # guessit calls this an episode titled "The Final Cut"; no S/E numbers -> movie
    (
        "Blade.Runner.1982.The.Final.Cut.1080p.BluRay.x264.mkv",
        "Blade Runner",
        1982,
        None,
        None,
        None,
        None,
        M,
    ),
    # date-based shows have no S/E numbers: treated as a movie, year taken from the date
    (
        "The.Daily.Show.2024.01.15.Guest.Name.720p.WEB.h264.mkv",
        "The Daily Show",
        2024,
        None,
        None,
        None,
        None,
        M,
    ),
    # --- episode numbering styles ---
    (
        "Show.Name.1x02.Episode.Title.HDTV.x264-LOL.avi",
        "Show Name",
        None,
        1,
        2,
        None,
        "Episode Title",
        E,
    ),
    ("Show.Name.S01E01-E02.720p.WEB.mkv", "Show Name", None, 1, 1, 2, None, E),
    ("Show.Name.S01E01E02.720p.WEB.mkv", "Show Name", None, 1, 1, 2, None, E),
    ("Anime Show - E05 [1080p].mkv", "Anime Show", None, 1, 5, None, None, E),
    ("Anime Show Ep 12.mkv", "Anime Show", None, 1, 12, None, None, E),
    (
        "Friends.S10E17-E18.The.Last.One.720p.BluRay.x264.mkv",
        "Friends",
        None,
        10,
        17,
        18,
        "The Last One",
        E,
    ),
    ("Seinfeld.9x23.The.Finale.DVDRip.XviD.avi", "Seinfeld", None, 9, 23, None, "The Finale", E),
    (
        "The Simpsons - 35x01 - Homer's Crossing.mkv",
        "The Simpsons",
        None,
        35,
        1,
        None,
        "Homer's Crossing",
        E,
    ),
    (
        "Attack on Titan - S04E28 - The Dawn of Humanity [1080p].mkv",
        "Attack On Titan",
        None,
        4,
        28,
        None,
        "The Dawn Of Humanity",
        E,
    ),
    # --- years inside titles ---
    (
        "2001.A.Space.Odyssey.1968.1080p.BluRay.x264.mkv",
        "2001 A Space Odyssey",
        1968,
        None,
        None,
        None,
        None,
        M,
    ),
    ("1917.2019.2160p.UHD.BluRay.x265-TERMiNAL.mkv", "1917", 2019, None, None, None, None, M),
    (
        "Blade.Runner.2049.2017.1080p.BluRay.x264.mkv",
        "Blade Runner 2049",
        2017,
        None,
        None,
        None,
        None,
        M,
    ),
    ("Doctor.Who.2005.S01E01.Rose.1080p.mkv", "Doctor Who", 2005, 1, 1, None, "Rose", E),
    (
        "Shogun.2024.S01E10.A.Dream.of.a.Dream.1080p.DSNP.WEB-DL.mkv",
        "Shogun",
        2024,
        1,
        10,
        None,
        "A Dream Of A Dream",
        E,
    ),
    (
        "One.Piece.2023.S01E01.Romance.Dawn.1080p.NF.WEB-DL.mkv",
        "One Piece",
        2023,
        1,
        1,
        None,
        "Romance Dawn",
        E,
    ),
    # --- brackets and site tags ---
    (
        "Inception (2010) [1080p] [BluRay] [5.1] [YTS.MX].mp4",
        "Inception",
        2010,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "Barbie (2023) [2160p] [4K] [WEB] [5.1] [YTS.MX].mp4",
        "Barbie",
        2023,
        None,
        None,
        None,
        None,
        M,
    ),
    ("Fast & Furious 9 (2021).mkv", "Fast & Furious 9", 2021, None, None, None, None, M),
    # --- release tags: editions, repacks, codecs, audio ---
    (
        "The.Shawshank.Redemption.1994.REPACK.1080p.BluRay.x264-AMIABLE.mkv",
        "The Shawshank Redemption",
        1994,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "Oppenheimer.2023.PROPER.2160p.UHD.BluRay.x265.10bit.HDR.DTS-HD.MA.5.1-SWTYBLZ.mkv",
        "Oppenheimer",
        2023,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "Gladiator.2000.EXTENDED.REMASTERED.1080p.BluRay.x264.DTS-FGT.mkv",
        "Gladiator",
        2000,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "Interstellar.2014.IMAX.1080p.WEB-DL.DDP5.1.H264.mkv",
        "Interstellar",
        2014,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "The.Godfather.1972.Criterion.1080p.BluRay.AV1.Opus.mkv",
        "The Godfather",
        1972,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "Parasite.2019.KOREAN.1080p.BluRay.H264.AAC-RARBG.mp4",
        "Parasite",
        2019,
        None,
        None,
        None,
        None,
        M,
    ),
    ("Amelie.2001.FRENCH.720p.BluRay.x264.mkv", "Amelie", 2001, None, None, None, None, M),
    ("Joker.2019.HDRip.XviD.AC3-EVO.avi", "Joker", 2019, None, None, None, None, M),
    (
        "Avatar.The.Way.of.Water.2022.HEVC.2160p.WEB.mkv",
        "Avatar The Way Of Water",
        2022,
        None,
        None,
        None,
        None,
        M,
    ),
    (
        "The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.2001.EXTENDED.1080p.BluRay.x264.mkv",
        "The Lord Of The Rings The Fellowship Of The Ring",
        2001,
        None,
        None,
        None,
        None,
        M,
    ),
    ("Alien.1979.Directors.Cut.mov", "Alien", 1979, None, None, None, None, M),
    ("Ghostbusters.1984.720p.BluRay.x264.wmv", "Ghostbusters", 1984, None, None, None, None, M),
    (
        "Severance.S02E10.Cold.Harbor.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX.mkv",
        "Severance",
        None,
        2,
        10,
        None,
        "Cold Harbor",
        E,
    ),
    (
        "Stranger.Things.S04E09.Chapter.Nine.The.Piggyback.2160p.NF.WEB-DL.DDP5.1.Atmos.DV.HEVC.mkv",
        "Stranger Things",
        None,
        4,
        9,
        None,
        "Chapter Nine The Piggyback",
        E,
    ),
    (
        "Fallout.S01E01.The.End.2160p.AMZN.WEB-DL.DDP5.1.Atmos.HDR10Plus.H.265-FLUX.mkv",
        "Fallout",
        None,
        1,
        1,
        None,
        "The End",
        E,
    ),
    (
        "House.of.the.Dragon.S02E01.1080p.HEVC.x265-MeGusta.mkv",
        "House Of The Dragon",
        None,
        2,
        1,
        None,
        None,
        E,
    ),
    ("Dark.S01E01.Secrets.1080p.NF.WEB-DL.GERMAN.mkv", "Dark", None, 1, 1, None, "Secrets", E),
    # --- capitalisation: ALL-CAPS and mixed case stay as they are ---
    ("NCIS.S21E05.720p.HDTV.x264-SYNCOPY.mkv", "NCIS", None, 21, 5, None, None, E),
    ("WALL-E.2008.1080p.BluRay.x264.mkv", "WALL-E", 2008, None, None, None, None, M),
    ("Se7en.1995.1080p.BluRay.x264.mkv", "Se7en", 1995, None, None, None, None, M),
    # --- separators and numbers ---
    ("Top_Gun_Maverick_2022_1080p.mkv", "Top Gun Maverick", 2022, None, None, None, None, M),
    (
        "Mad Max Fury Road 2015 1080p BluRay x264.mkv",
        "Mad Max Fury Road",
        2015,
        None,
        None,
        None,
        None,
        M,
    ),
    ("Toy.Story.4.2019.1080p.BluRay.x264.mkv", "Toy Story 4", 2019, None, None, None, None, M),
    ("Rocky.II.1979.1080p.BluRay.x264.mkv", "Rocky II", 1979, None, None, None, None, M),
    (
        "Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.x264.mkv",
        "Breaking Bad",
        None,
        5,
        14,
        None,
        "Ozymandias",
        E,
    ),
    (
        "Game.of.Thrones.S08E03.The.Long.Night.1080p.AMZN.WEB-DL.DDP5.1.H.264-GoT.mkv",
        "Game Of Thrones",
        None,
        8,
        3,
        None,
        "The Long Night",
        E,
    ),
    (
        "The.Mandalorian.S03E08.Chapter.24.The.Return.1080p.DSNP.WEB-DL.mkv",
        "The Mandalorian",
        None,
        3,
        8,
        None,
        "Chapter 24 The Return",
        E,
    ),
    (
        "Planet.Earth.II.S01E01.Islands.2160p.UHD.BluRay.x265.mkv",
        "Planet Earth II",
        None,
        1,
        1,
        None,
        "Islands",
        E,
    ),
    (
        "The.Boys.S04E01.Department.of.Dirty.Tricks.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX.mkv",
        "The Boys",
        None,
        4,
        1,
        None,
        "Department Of Dirty Tricks",
        E,
    ),
    ("Bluey.S03E01.Faceytalk.1080p.DSNP.WEB-DL.mkv", "Bluey", None, 3, 1, None, "Faceytalk", E),
    ("Twin.Peaks.S03E08.Gotta.Light.1080p.mkv", "Twin Peaks", None, 3, 8, None, "Gotta Light", E),
    (
        "Black.Mirror.S06E01.Joan.Is.Awful.1080p.NF.WEB-DL.mkv",
        "Black Mirror",
        None,
        6,
        1,
        None,
        "Joan Is Awful",
        E,
    ),
]


class ParserTableTest(ForgeTestCase):
    def test_table_has_at_least_60_names(self) -> None:
        self.assertGreaterEqual(len(CASES), 60)

    def test_parse_real_world_names(self) -> None:
        for name, title, year, season, episode, episode_end, episode_title, kind in CASES:
            with self.subTest(name=name[:40]):
                p = parse_filename(name)
                self.assertEqual(
                    (p.title, p.year, p.season, p.episode, p.episode_end, p.episode_title, p.kind),
                    (title, year, season, episode, episode_end, episode_title, kind),
                )
                self.assertEqual(p.source_name, name)


class ParserRulesTest(ForgeTestCase):
    def test_hint_movie_forces_movie_kind(self) -> None:
        self.assertIs(parse_filename("Show.S01E02.mkv", TypeHint.MOVIE).kind, MediaKind.MOVIE)

    def test_hint_tv_forces_episode_kind_even_without_numbers(self) -> None:
        p = parse_filename("Movie Title.mkv", TypeHint.TV)
        assert p.kind is MediaKind.EPISODE
        assert p.season is None and p.episode is None

    def test_tags_collected(self) -> None:
        p = parse_filename("The.Matrix.1999.REMASTERED.1080p.BluRay.x264.TrueHD.7.1.Atmos-FGT.mkv")
        assert {"1080p", "Remastered"} <= set(p.tags)

    def test_empty_title_falls_back_to_stem(self) -> None:
        assert parse_filename("1080p.mkv").title != ""

    def test_human_title_rules(self) -> None:
        assert human_title("the last of us") == "The Last Of Us"
        assert human_title("VisionQuest NCIS mIxEd") == "VisionQuest NCIS mIxEd"
        assert human_title("homer's  crossing") == "Homer's Crossing"

    def test_is_video_case_insensitive(self) -> None:
        assert is_video(Path("a.MKV"))
        assert is_video(Path("b.m4v"))
        assert not is_video(Path("a.srt"))


class OptionalGuessitTest(ForgeTestCase):
    """guessit is an optional package: AirGrabber must run without it."""

    def setUp(self) -> None:
        super().setUp()
        parser_ready.cache_clear()
        self.addCleanup(parser_ready.cache_clear)

    def _without_guessit(self) -> None:
        real_import = builtins.__import__

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "guessit" or name.startswith("guessit."):
                raise ImportError("No module named 'guessit'")
            return real_import(name, *args, **kwargs)

        def refuse_module(name: str, package: str | None = None) -> Any:
            if name == "guessit" or name.startswith("guessit."):
                raise ImportError("No module named 'guessit'")
            return real_import_module(name, package)

        real_import_module = importlib.import_module
        self.enterContext(mock.patch.object(builtins, "__import__", refuse))
        self.enterContext(mock.patch.object(importlib, "import_module", refuse_module))

    def test_ready_when_guessit_imports(self) -> None:
        self.assertTrue(parser_ready())

    def test_not_ready_and_nothing_raises_at_import_without_guessit(self) -> None:
        self._without_guessit()
        with self.assertLogs("app.forge.core.parser", "WARNING"):
            self.assertFalse(parser_ready())
        self.assertFalse(parser_ready())  # cached: asked once
        importlib.reload(parser)  # the module itself never needs guessit
        self.addCleanup(importlib.reload, parser)
        with self.assertRaises(ImportError):
            parser.parse_filename("Dune.2021.mkv")

    # F-33: prepare_parser (ForgeRuntime's start) pauses between its pieces, once per run.

    def _prepare(self) -> bool:
        async def pause() -> None:
            self.pauses += 1

        return asyncio.run(parser.prepare_parser(pause))

    def _unprepared(self) -> None:
        self.enterContext(mock.patch.object(parser, "_prepared", False))
        # The module's own check: a test above reloads the module, which makes a new one.
        parser.parser_ready.cache_clear()
        self.addCleanup(parser.parser_ready.cache_clear)
        self.pauses = 0

    def test_prepare_pauses_between_pieces_and_only_the_first_time(self) -> None:
        self._unprepared()
        self.assertTrue(self._prepare())
        first = self.pauses
        self.assertGreaterEqual(first, 10)
        self.assertTrue(self._prepare())
        self.assertEqual(self.pauses, first)  # prepared once per run

    def test_prepare_without_guessit_answers_false_and_never_raises(self) -> None:
        self._unprepared()
        self._without_guessit()
        with self.assertLogs("app.forge.core.parser", "WARNING"):
            self.assertFalse(self._prepare())
        self.assertGreater(self.pauses, 0)
        self.assertFalse(self._prepare())


# F-33: prepare_parser in a fresh interpreter (guessit not imported yet): each piece's own work
# (this thread's CPU time, so other programs busy on the machine don't count) is recorded at
# every pause. The whole preparation at once is ~0.4 s of work or more; the pieces ~0.1 s at most.
_PREPARE_PROBE = """
import asyncio, json, sys, time
from app.forge.core import parser

pieces = []
mark = [time.thread_time()]

async def pause():
    pieces.append(time.thread_time() - mark[0])
    await asyncio.sleep(0)
    mark[0] = time.thread_time()

async def main():
    before = "guessit" in sys.modules
    ready = await parser.prepare_parser(pause)
    pieces.append(time.thread_time() - mark[0])  # the last piece: guessit's rules
    again = await parser.prepare_parser(pause)
    return before, ready, again

before, ready, again = asyncio.run(main())
start = time.thread_time()
title = parser.parse_filename("Dune.Part.Two.2024.2160p.WEB-DL.mkv").title
print(json.dumps({"before": before, "ready": ready, "again": again, "pieces": pieces,
                  "next_parse": time.thread_time() - start, "title": title}))
"""
PIECE_LIMIT_S = 0.25  # each piece's own CPU time; all at once it is well over this


class PrepareParserTest(ForgeTestCase):
    def test_guessit_is_imported_and_warmed_up_in_small_pieces(self) -> None:
        done = subprocess.run(
            [sys.executable, "-c", _PREPARE_PROBE],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertFalse(result["before"])  # the parser module alone doesn't import guessit
        self.assertTrue(result["ready"])
        self.assertTrue(result["again"])
        pieces: list[float] = result["pieces"]
        self.assertGreaterEqual(len(pieces), 10)
        self.assertLess(max(pieces), PIECE_LIMIT_S, pieces)
        self.assertGreater(sum(pieces), max(pieces) * 2)  # the work really is spread out
        self.assertLess(result["next_parse"], 0.1)  # the rules were built: a parse is cheap now
        self.assertEqual(result["title"], "Dune Part Two")
