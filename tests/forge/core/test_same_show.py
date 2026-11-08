"""Files of one show that name it differently ("The Boys - S05E07" beside "The Boys (2019) -
S01E01"): the unsure one follows the sure ones; and an episode's name always says which episode
it is, whatever the rule (the built-in "Auto" preset is "{ny}")."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.forge.core.formatter import episode_rule, name_file, render
from app.forge.core.matcher import Resolution
from app.forge.core.queue import ItemStatus
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.models.tmdb import TMDBTitle
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.core.test_queue import FakeResolver, make_queue, touch

BOYS_2019 = TMDBTitle(id=76479, title="The Boys", date="2019-07-25")
BOYS_OTHER = TMDBTitle(id=11111, title="The Boys", date="1993-01-01")


def episode(title: str = "The Boys", year: int | None = 2019, name: str = "Cherry") -> ResolvedMedia:
    parsed = ParsedMedia(source_name="x.mkv", kind=MediaKind.EPISODE, title=title, year=year, season=1, episode=2)
    return ResolvedMedia(
        parsed=parsed, title=title, year=year, episode_title=name, match_state=MatchState.CONFIDENT
    )


class RuleTests(unittest.TestCase):
    def test_an_episode_rule_says_which_episode(self) -> None:
        self.assertEqual(episode_rule("{ny}"), "{ny} - {s00e00} - {en}")
        self.assertEqual(episode_rule("{ny} - {en}"), "{ny} - {en} - {s00e00}")
        self.assertEqual(episode_rule("{ny}/{ny} - {s00e00} - {en}"), "{ny}/{ny} - {s00e00} - {en}")
        self.assertEqual(episode_rule("{ny} {sxe}"), "{ny} {sxe}")
        self.assertEqual(episode_rule("{ny}/Season {s}/{e} {en}"), "{ny}/Season {s}/{e} {en}")
        # A code only in a folder doesn't tell the files apart.
        self.assertEqual(episode_rule("{ny} {s00e00}/{ny}"), "{ny} {s00e00}/{ny} - {s00e00} - {en}")

    def test_the_auto_preset_names_episodes_apart(self) -> None:
        self.assertEqual(
            name_file("{ny}", episode(), ".mkv").parts, ("The Boys (2019) - S01E02 - Cherry.mkv",)
        )
        self.assertEqual(render("{ny}", episode(), ".mkv").parts, ("The Boys (2019).mkv",))
        film = ResolvedMedia.offline(
            ParsedMedia(source_name="d.mkv", kind=MediaKind.MOVIE, title="Dune", year=2021)
        )
        self.assertEqual(name_file("{ny}", film, ".mkv").parts, ("Dune (2021).mkv",))


class SureResolver(FakeResolver):
    """Files whose names say the year are sure (The Boys 2019); the others are unsure, with
    both shows called The Boys as TMDB's suggestions."""

    async def resolve(self, parsed: ParsedMedia) -> Resolution:
        if parsed.year == 2019:
            media = ResolvedMedia.offline(parsed, MatchState.CONFIDENT).model_copy(
                update={"tmdb_id": BOYS_2019.id, "year": 2019}
            )
            return Resolution(media=media, candidates=(BOYS_2019,))
        unsure = ResolvedMedia.offline(parsed, MatchState.UNCERTAIN)
        return Resolution(media=unsure, candidates=(BOYS_OTHER, BOYS_2019))


class SiblingTests(ForgeAsyncTestCase):
    async def add(self, *names: str, resolver: FakeResolver | None = None) -> list[ItemStatus]:
        queue, _events = make_queue(resolver or SureResolver())
        for name in names:
            touch(self.tmp / "in" / name)
        await queue.add_paths([self.tmp / "in"], recursive=True)
        await queue.wait_idle()
        self.queue = queue
        return [item.status for item in sorted(queue.items, key=lambda i: i.path.name)]

    async def test_the_unsure_file_follows_the_sure_ones(self) -> None:
        statuses = await self.add(
            "The Boys (2019) - S01E01 - The Name of the Game.mkv",
            "The Boys - S05E07 - The Frenchman.mkv",
        )
        self.assertEqual(statuses, [ItemStatus.READY, ItemStatus.READY])
        unsure = next(i for i in self.queue.items if "S05E07" in i.path.name)
        self.assertEqual(unsure.media.tmdb_id, BOYS_2019.id)
        self.assertEqual(unsure.media.year, 2019)  # in the same band now

    async def test_alone_it_stays_a_question(self) -> None:
        statuses = await self.add("The Boys - S05E07 - The Frenchman.mkv")
        self.assertEqual(statuses, [ItemStatus.NEEDS_CHOICE])

    async def test_not_when_the_sure_ones_disagree(self) -> None:
        class TwoShows(SureResolver):
            async def resolve(self, parsed: ParsedMedia) -> Resolution:
                if parsed.year == 1993:
                    media = ResolvedMedia.offline(parsed, MatchState.CONFIDENT).model_copy(
                        update={"tmdb_id": BOYS_OTHER.id}
                    )
                    return Resolution(media=media, candidates=(BOYS_OTHER,))
                return await super().resolve(parsed)

        statuses = await self.add(
            "The Boys (2019) - S01E01 - A.mkv",
            "The Boys (1993) - S01E01 - B.mkv",
            "The Boys - S05E07 - C.mkv",
            resolver=TwoShows(),
        )
        self.assertEqual(statuses.count(ItemStatus.NEEDS_CHOICE), 1)


if __name__ == "__main__":
    unittest.main()
