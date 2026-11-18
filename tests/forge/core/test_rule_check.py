"""The rule editor's live check (app/forge/core/rule_check.py): the folder read in a thread under
the look's limit (None past it or when it can't be read), and why each file does or doesn't go
to the rule (fits; a rule above; a folder inside, the kind, the quality, with the rule that
takes it instead)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

from app.forge.core import rule_check
from app.forge.core.rule_check import CheckGroup, explain, peek
from app.forge.models.media import MediaKind, ParsedMedia
from app.forge.models.rules import Rule
from app.forge.models.settings import ForgeSettings
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase

D = r"D:\Downloads"
SETTINGS = ForgeSettings()
MOVIE, EPISODE = MediaKind.MOVIE, MediaKind.EPISODE


def film(name: str, folder: str = D) -> tuple[Path, ParsedMedia]:
    return Path(folder) / name, ParsedMedia(source_name=name, kind=MOVIE, title="A Film")


def episode(name: str, folder: str = D) -> tuple[Path, ParsedMedia]:
    parsed = ParsedMedia(source_name=name, kind=EPISODE, title="A Show", season=1, episode=1)
    return Path(folder) / name, parsed


class PeekTests(ForgeAsyncTestCase):
    async def test_peek_gives_up_after_the_limit(self) -> None:
        def slow(*_args: object) -> list[Path]:
            time.sleep(0.5)
            return []

        with mock.patch.object(rule_check, "candidates", side_effect=slow):
            started = time.monotonic()
            self.assertIsNone(await peek(self.tmp, None, timeout_s=0.05))
            self.assertLess(time.monotonic() - started, 0.4)

    async def test_peek_of_a_missing_folder_is_none(self) -> None:
        self.assertIsNone(await peek(self.tmp / "gone", None))

    async def test_peek_gives_the_videos_with_their_names_read(self) -> None:
        (self.tmp / "Dune.2021.2160p.mkv").write_bytes(b"x")
        (self.tmp / "notes.txt").write_bytes(b"x")
        read: list[list[str]] = []

        async def parse(names: list[str]) -> list[ParsedMedia]:
            read.append(list(names))
            return [ParsedMedia(source_name=n, kind=MOVIE, title="Dune") for n in names]

        with mock.patch.object(rule_check, "parse_names", parse):
            found = await peek(self.tmp, None)
        assert found is not None
        self.assertEqual([path.name for path, _parsed in found], ["Dune.2021.2160p.mkv"])
        self.assertEqual(read, [["Dune.2021.2160p.mkv"]])

    async def test_an_empty_folder_is_an_empty_list(self) -> None:
        self.assertEqual(await peek(self.tmp, None), [])


class ExplainTests(ForgeTestCase):
    def test_explain_matches_the_mockup(self) -> None:
        rules = [
            Rule(folder=D, take="films", quality="4k"),
            Rule(folder=D, take="films"),
            Rule(folder=D, take="shows"),
        ]
        files = [
            film("Dune.2021.2160p.mkv"),
            film("Alien.1979.2160p.mkv"),
            film("Heat.1995.1080p.mkv"),
            film("Up.2009.1080p.mkv"),
            film("Big.1988.1080p.mkv"),
            *(episode(f"Show.S01E0{n}.mkv") for n in range(1, 5)),
        ]
        self.assertEqual(
            explain(0, rules, SETTINGS, Path(D), files),
            [
                CheckGroup("fits", MOVIE, 2, None),
                CheckGroup("a_show", EPISODE, 4, 3),
                CheckGroup("not_4k", MOVIE, 3, 2),
            ],
        )

    def test_a_rule_above_takes_them_first(self) -> None:
        rules = [Rule(folder=D), Rule(folder=D, take="films")]
        self.assertEqual(
            explain(1, rules, SETTINGS, Path(D), [film("Heat.mkv"), episode("Show.S01E01.mkv")]),
            [CheckGroup("taken_above", EPISODE, 1, 1), CheckGroup("taken_above", MOVIE, 1, 1)],
        )

    def test_the_rule_counts_as_on_and_a_miss_may_go_to_no_rule(self) -> None:
        rules = [Rule(folder=D, take="films", on=False)]
        self.assertEqual(
            explain(0, rules, SETTINGS, Path(D), [film("Heat.mkv"), episode("S.S01E01.mkv")]),
            [CheckGroup("fits", MOVIE, 1, None), CheckGroup("a_show", EPISODE, 1, None)],
        )

    def test_a_file_in_a_folder_inside_for_a_top_folder_only_rule(self) -> None:
        rules = [Rule(folder=D, subfolders=False), Rule(folder=D)]
        inside = film("Heat.mkv", D + r"\Heat (1995)")
        self.assertEqual(
            explain(0, rules, SETTINGS, Path(D), [inside, film("Up.mkv")]),
            [CheckGroup("fits", MOVIE, 1, None), CheckGroup("in_subfolder", MOVIE, 1, 2)],
        )

    def test_not_in_4k_rule_leaves_the_4k_ones_and_films_only_the_shows(self) -> None:
        rules = [Rule(folder=D, take="shows", quality="not_4k")]
        files = [episode("S.S01E01.2160p.mkv"), episode("S.S01E02.mkv"), film("Up.mkv")]
        self.assertEqual(
            explain(0, rules, SETTINGS, Path(D), files),
            [
                CheckGroup("fits", EPISODE, 1, None),
                CheckGroup("a_film", MOVIE, 1, None),
                CheckGroup("is_4k", EPISODE, 1, None),
            ],
        )

    def test_a_destinations_files_are_never_counted(self) -> None:
        # Another rule moves films into D\Sorted, and the Movies folder is in D: a look never
        # takes what is there, so the check doesn't count it either.
        from app.forge.models.rules import RuleTarget

        sorted_ = D + r"\Sorted"
        mover = Rule(
            folder=r"E:\Other",
            take="films",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=sorted_),
        )
        settings = ForgeSettings(movies_folder=D + r"\Movies")
        rules = [Rule(folder=D, take="shows"), mover]
        files = [film("Heat.mkv", sorted_), film("Up.mkv", D + r"\Movies"), episode("S.S01E01.mkv")]
        self.assertEqual(
            explain(0, rules, settings, Path(D), files), [CheckGroup("fits", EPISODE, 1, None)]
        )

    def test_a_new_rule_last(self) -> None:
        rules = [Rule(folder=D, take="shows"), Rule(folder=D)]
        self.assertEqual(
            explain(1, rules, SETTINGS, Path(D), [episode("S.S01E01.mkv"), film("Up.mkv")]),
            [CheckGroup("fits", MOVIE, 1, None), CheckGroup("taken_above", EPISODE, 1, 1)],
        )
