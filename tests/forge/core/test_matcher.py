"""The matcher: confidence rules, remembered choices and the Resolver (MediaForge's
tests/core/test_matcher.py, with a fake TMDB)."""

from __future__ import annotations

import unittest

import httpx

from app.forge.clients.tmdb import ForgeTmdb, TmdbBadReply, TmdbError
from app.forge.core.matcher import SKIP_LABEL, Resolver, choose, match_key, normalize_title
from app.forge.db.matches import MatchRepo
from app.forge.models.match import RememberedMatch
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.models.tmdb import TMDBSeason, TMDBSeasonEpisode, TMDBTitle
from app.services.tmdb import TMDBClient
from tests.forge.db.base import ForgeDbTestCase


def res(id_: int, title: str, date: str | None = None, original: str | None = None) -> TMDBTitle:
    return TMDBTitle(
        id=id_,
        title=title,
        date=date,
        original_title=original,
        poster_path=f"/{id_}.jpg",
        backdrop_path=f"/b{id_}.jpg",
    )


class FakeTmdb:
    def __init__(
        self,
        results: list[TMDBTitle] | None = None,
        fail: bool = False,
        *,
        configured: bool = True,
        details_error: TmdbError | None = None,
    ) -> None:
        self.results = results or []
        self.fail = fail
        self.is_configured = configured
        self.details_error = details_error
        self.searches: list[tuple[str, str, int | None]] = []
        self.details_asked: list[int] = []

    def configured(self) -> bool:
        return self.is_configured

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        self.searches.append(("movie", query, year))
        if self.fail:
            raise TmdbError("offline")
        return self.results

    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]:
        self.searches.append(("tv", query, year))
        if self.fail:
            raise TmdbError("offline")
        return self.results

    async def details(self, kind: MediaKind, tmdb_id: int) -> TMDBTitle | None:
        self.details_asked.append(tmdb_id)
        if self.details_error is not None:
            raise self.details_error
        return res(tmdb_id, f"Details {tmdb_id}", "2005-03-24")

    async def season(self, tv_id: int, number: int) -> TMDBSeason | None:
        return TMDBSeason(
            season_number=number,
            poster_path=f"/s{number}.jpg",
            episodes=(TMDBSeasonEpisode(episode_number=2, name="Theseus"),),
        )


def episode(title: str = "VisionQuest", year: int | None = None) -> ParsedMedia:
    return ParsedMedia(
        source_name="x.mkv",
        kind=MediaKind.EPISODE,
        title=title,
        year=year,
        season=1,
        episode=2,
        episode_title="Parsed Name",
    )


def movie(title: str = "Dune", year: int | None = 2021) -> ParsedMedia:
    return ParsedMedia(source_name="x.mkv", kind=MediaKind.MOVIE, title=title, year=year)


class ChooseTest(unittest.TestCase):
    def test_normalize(self) -> None:
        assert normalize_title("Fast & Furious: 9!") == "fast and furious 9"
        assert normalize_title("  The   Office ") == "the office"

    def test_match_key(self) -> None:
        assert match_key(movie("Dune: Part One", 2021)) == (MediaKind.MOVIE, "dune part one", 2021)

    def test_choose_confident_on_title_and_year(self) -> None:
        state, chosen = choose(
            [res(1, "Dune", "1984-12-14"), res(2, "Dune", "2021-10-22")], "Dune", 2021
        )
        assert (state, chosen and chosen.id) == (MatchState.CONFIDENT, 2)

    def test_choose_confident_on_original_title(self) -> None:
        results = [res(1, "Other", "2019-01-01"), res(2, "Parasite", "2019-05-30", original="기생충")]
        state, chosen = choose(results, "기생충", 2019)
        assert (state, chosen and chosen.id) == (MatchState.CONFIDENT, 2)

    def test_choose_single_result_confident_only_on_its_own_name(self) -> None:
        # The file's name (no year): sure.
        state, chosen = choose([res(5, "Dune", "2021-10-22")], "dune", None)
        assert (state, chosen and chosen.id) == (MatchState.CONFIDENT, 5)
        # Its original title counts too.
        state, chosen = choose([res(6, "Parasite", "2019-05-30", original="기생충")], "기생충", None)
        assert (state, chosen and chosen.id) == (MatchState.CONFIDENT, 6)

    def test_choose_single_result_with_another_name_is_uncertain(self) -> None:
        # A home video or a course in a watched folder: TMDB's only hit is some film.
        state, chosen = choose([res(5, "Something Else")], "Dune", None)
        assert (state, chosen and chosen.id) == (MatchState.UNCERTAIN, 5)
        state, chosen = choose([res(7, "Holiday in Spain", "1960-01-01")], "Holiday 2023", None)
        assert (state, chosen and chosen.id) == (MatchState.UNCERTAIN, 7)

    def test_choose_single_result_with_another_year_is_uncertain(self) -> None:
        state, chosen = choose([res(5, "Dune", "1984-12-14")], "Dune", 2021)
        assert (state, chosen and chosen.id) == (MatchState.UNCERTAIN, 5)
        state, _ = choose([res(5, "Dune")], "Dune", 2021)  # TMDB has no date for it
        assert state is MatchState.UNCERTAIN
        state, _ = choose([res(5, "Dune", "2021-10-22")], "Dune", 2021)
        assert state is MatchState.CONFIDENT

    def test_choose_uncertain_takes_top(self) -> None:
        state, chosen = choose(
            [res(1, "The Office", "2005-03-24"), res(2, "The Office", "2001-07-09")],
            "The Office",
            2010,
        )
        assert (state, chosen and chosen.id) == (MatchState.UNCERTAIN, 1)

    def test_choose_no_year_multiple_is_uncertain(self) -> None:
        state, _ = choose(
            [res(1, "The Office", "2005-03-24"), res(2, "The Office", "2001-07-09")],
            "The Office",
            None,
        )
        assert state is MatchState.UNCERTAIN

    def test_choose_nothing(self) -> None:
        assert choose([], "x", None) == (MatchState.NOT_FOUND, None)


class ResolverTest(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.matches = MatchRepo(self.db)

    async def remember_office(self) -> None:
        await self.matches.remember(
            RememberedMatch(
                kind=MediaKind.EPISODE, norm_title="the office", year=None, tmdb_id=2316, label="US"
            )
        )

    async def test_resolve_confident_movie(self) -> None:
        tmdb = FakeTmdb([res(2, "Dune", "2021-10-22")])
        resolution = await Resolver(tmdb, self.matches).resolve(movie())
        m = resolution.media
        assert (m.title, m.year, m.tmdb_id, m.poster_path, m.match_state) == (
            "Dune",
            2021,
            2,
            "/2.jpg",
            MatchState.CONFIDENT,
        )
        assert tmdb.searches == [("movie", "Dune", 2021)]

    async def test_resolve_remembered_wins(self) -> None:
        await self.remember_office()
        tmdb = FakeTmdb([res(99, "Wrong Office")])
        resolution = await Resolver(tmdb, self.matches).resolve(episode("The Office"))
        assert resolution.media.match_state is MatchState.REMEMBERED
        assert resolution.media.tmdb_id == 2316
        assert tmdb.searches == []

    async def test_remembered_match_with_a_bad_reply_falls_back_to_a_search(self) -> None:
        # MediaForge's client returned None for a malformed reply; AirGrabber's raises
        # TmdbBadReply. Either way the file is looked up by its name instead.
        await self.remember_office()
        tmdb = FakeTmdb(
            [res(99, "The Office", "2005-03-24")],
            details_error=TmdbBadReply("TMDB /tv/2316: unexpected payload"),
        )
        resolution = await Resolver(tmdb, self.matches).resolve(episode("The Office"))
        assert tmdb.details_asked == [2316]
        assert tmdb.searches == [("tv", "The Office", None)]
        assert resolution.media.match_state is MatchState.CONFIDENT
        assert resolution.media.tmdb_id == 99

    async def test_remembered_match_offline_stays_offline(self) -> None:
        # A network error (not a bad reply) doesn't search: TMDB can't be reached.
        await self.remember_office()
        tmdb = FakeTmdb([res(99, "The Office")], details_error=TmdbError("request failed"))
        resolution = await Resolver(tmdb, self.matches).resolve(episode("The Office"))
        assert tmdb.searches == []
        assert resolution.media.match_state is MatchState.OFFLINE
        assert resolution.media.title == "The Office"

    async def test_resolve_remembered_skip_gives_skipped_offline_values(self) -> None:
        await self.matches.remember(
            RememberedMatch(
                kind=MediaKind.MOVIE, norm_title="dune", year=2021, tmdb_id=None, label="skip"
            )
        )
        resolution = await Resolver(FakeTmdb([res(2, "Dune")]), self.matches).resolve(movie())
        assert resolution.media.match_state is MatchState.SKIPPED
        assert resolution.media.title == "Dune" and resolution.media.tmdb_id is None

    async def test_resolve_fetches_episode_title_and_season_poster(self) -> None:
        tmdb = FakeTmdb([res(7, "VisionQuest", "2026-01-01")])
        m = (await Resolver(tmdb, self.matches).resolve(episode())).media
        assert (m.title, m.year, m.episode_title, m.season_poster_path) == (
            "VisionQuest",
            2026,
            "Theseus",
            "/s1.jpg",
        )
        assert tmdb.searches == [("tv", "VisionQuest", None)]

    async def test_resolve_offline_without_client(self) -> None:
        m = (await Resolver(None, self.matches).resolve(episode())).media
        assert m.match_state is MatchState.OFFLINE
        assert (m.title, m.episode_title) == ("VisionQuest", "Parsed Name")

    async def test_not_configured_is_offline_and_asks_again_per_lookup(self) -> None:
        # ForgeTmdb follows AirGrabber's key: no client swap, configured() per lookup.
        tmdb = FakeTmdb([res(2, "Dune", "2021-10-22")], configured=False)
        resolver = Resolver(tmdb, self.matches)
        assert (await resolver.resolve(movie())).media.match_state is MatchState.OFFLINE
        assert tmdb.searches == []
        tmdb.is_configured = True
        assert (await resolver.resolve(movie())).media.match_state is MatchState.CONFIDENT

    async def test_set_client_switches_offline_to_online(self) -> None:
        resolver = Resolver(None, self.matches)
        assert (await resolver.resolve(movie())).media.match_state is MatchState.OFFLINE
        resolver.set_client(FakeTmdb([res(2, "Dune", "2021-10-22")]))
        assert (await resolver.resolve(movie())).media.match_state is MatchState.CONFIDENT

    async def test_resolve_network_error_is_offline_not_crash(self) -> None:
        m = (await Resolver(FakeTmdb(fail=True), self.matches).resolve(movie())).media
        assert m.match_state is MatchState.OFFLINE

    async def test_not_found(self) -> None:
        resolution = await Resolver(FakeTmdb([]), self.matches).resolve(movie())
        assert resolution.media.match_state is MatchState.NOT_FOUND
        assert resolution.candidates == ()

    async def test_uncertain_keeps_candidates(self) -> None:
        results = [res(i, "The Office", f"{2000 + i}-01-01") for i in range(10)]
        resolution = await Resolver(FakeTmdb(results), self.matches).resolve(
            episode("The Office", 2020)
        )
        assert resolution.media.match_state is MatchState.UNCERTAIN
        assert len(resolution.candidates) == 8

    async def test_apply_choice_remembers(self) -> None:
        resolver = Resolver(FakeTmdb([]), self.matches)
        chosen = res(2316, "The Office", "2005-03-24")
        m = await resolver.apply_choice(episode("The Office"), chosen)
        assert (m.title, m.year, m.tmdb_id, m.match_state) == (
            "The Office",
            2005,
            2316,
            MatchState.REMEMBERED,
        )
        stored = await self.matches.get(MediaKind.EPISODE, "the office", None)
        assert stored is not None and stored.tmdb_id == 2316
        assert stored.label == "The Office (2005)"

    async def test_apply_choice_skip_remembers_none(self) -> None:
        resolver = Resolver(FakeTmdb([]), self.matches)
        m = await resolver.apply_choice(movie(), None)
        assert m.match_state is MatchState.SKIPPED
        stored = await self.matches.get(MediaKind.MOVIE, "dune", 2021)
        assert stored is not None and stored.tmdb_id is None
        assert stored.label == SKIP_LABEL == "Don't use TMDB"  # stored in English

    async def test_backdrop_comes_with_the_match(self) -> None:
        r = Resolver(FakeTmdb([res(7, "Dune", "2021-09-15")]), self.matches)
        resolution = await r.resolve(movie("Dune", 2021))
        assert resolution.media.backdrop_path == "/b7.jpg"

    async def test_remembered_match_keeps_its_backdrop(self) -> None:
        r = Resolver(FakeTmdb(), self.matches)
        chosen = res(42, "Heat", "1995-12-15")
        media = await r.apply_choice(movie("Heat", 1995), chosen)
        assert media.backdrop_path == "/b42.jpg"
        again = await r.resolve(movie("Heat", 1995))  # through TMDB's details for the remembered id
        assert again.media.backdrop_path == "/b42.jpg"

    async def test_a_match_records_the_original_title_and_whether_tmdb_knows_the_episode(
        self,
    ) -> None:
        # Renaming on its own (1.3) checks both before moving a file by itself.
        tmdb = FakeTmdb([res(7, "Money Heist", "2017-05-02", original="La Casa de Papel")])
        found = (await Resolver(tmdb, self.matches).resolve(episode("La Casa de Papel"))).media
        assert found.original_title == "La Casa de Papel"
        assert found.episode_known  # the fake season has episode 2
        parsed = episode().model_copy(update={"episode": 9, "episode_title": "From The Name"})
        missing = (await Resolver(tmdb, self.matches).resolve(parsed)).media
        assert not missing.episode_known
        assert missing.episode_title == "From The Name"  # the name's own title is kept, as before
        assert not ResolvedMedia.offline(parsed).episode_known


class ResolverOnForgeTmdbTest(ForgeDbTestCase):
    """The bad-reply fallback through the real ForgeTmdb and TMDBClient (MockTransport)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.matches = MatchRepo(self.db)
        self.details_reply: httpx.Response | Exception = httpx.Response(200, json={"oops": 1})
        self.paths: list[str] = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.reply))
        self.addAsyncCleanup(http.aclose)
        client = TMDBClient(http, lambda: "abcdef0123456789secret", retry_delay=0)
        self.resolver = Resolver(ForgeTmdb(client), self.matches)
        await self.matches.remember(
            RememberedMatch(
                kind=MediaKind.MOVIE, norm_title="heat", year=1995, tmdb_id=949, label="Heat"
            )
        )

    def reply(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/3/movie/949":
            if isinstance(self.details_reply, Exception):
                raise self.details_reply
            return self.details_reply
        if request.url.path == "/3/search/movie":
            return httpx.Response(
                200, json={"results": [{"id": 949, "title": "Heat", "release_date": "1995-12-15"}]}
            )
        return httpx.Response(404, json={})

    async def test_malformed_details_fall_back_to_a_search(self) -> None:
        for body in (httpx.Response(200, json={"oops": 1}), httpx.Response(200, text="<html>")):
            with self.subTest(body=body.text[:10]):
                self.paths.clear()
                self.details_reply = body
                with self.assertLogs("app.forge.core.matcher", "WARNING"):
                    media = (await self.resolver.resolve(movie("Heat", 1995))).media
                assert "/3/search/movie" in self.paths
                assert (media.match_state, media.tmdb_id) == (MatchState.CONFIDENT, 949)

    async def test_unreachable_tmdb_does_not_search(self) -> None:
        self.details_reply = httpx.ConnectError("no route")
        media = (await self.resolver.resolve(movie("Heat", 1995))).media
        assert "/3/search/movie" not in self.paths
        assert media.match_state is MatchState.OFFLINE
