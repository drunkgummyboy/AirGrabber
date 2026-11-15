"""What the automatic downloads couldn't find (item 23): the given-up episodes and watchlist
films the Downloads page lists under "Couldn't find automatically", and their "Try again"
(the give-up cleared, a download queued at once). A real database; no TVMaze, TMDB or
torrent program (a fake queue)."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from app.models import (
    FIRST_PROFILE_ID,
    DownloadRecord,
    GivenUp,
    MediaType,
    Movie,
    SearchKind,
    SearchRequest,
)
from app.services.auto_download import GIVE_UP_AFTER
from app.services.download_queue import QueuedDownload
from app.services.given_up import GIVEN_UP_MAX, GivenUpService
from app.services.library import LibraryService
from app.services.watchlist import WatchlistService
from tests.test_auto_download import NOW, DatabaseTestCase, episode


class FakeQueue:
    """DownloadQueue's `add`: what was queued."""

    def __init__(self) -> None:
        self.items: list[QueuedDownload] = []

    def add(self, item: QueuedDownload) -> bool:
        self.items.append(item)
        return True


async def film_request(movie_id: int, label: str) -> SearchRequest:
    return SearchRequest(kind=SearchKind.MOVIE, title=label)


DUNE = Movie(id=438631, title="Dune", release_date="2021-09-15")


class GivenUpTests(DatabaseTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.library = LibraryService(self.db.library, self.db.history, mock.Mock())
        self.watchlist = WatchlistService(self.db.watchlist, mock.Mock(), mock.MagicMock())
        self.queue = FakeQueue()
        self.service = GivenUpService(
            self.library, self.watchlist, self.queue, film_request=film_request
        )

    async def give_up(self, episode_id: int, tried: timedelta) -> None:
        """One try `tried` before NOW, then the give-up the automatic downloads make."""
        await self.db.library.record_auto_attempt(episode_id, NOW - tried)
        await self.db.library.auto_give_up(aired_before=NOW - GIVE_UP_AFTER)

    async def give_up_film(self, movie: Movie, tried: timedelta) -> None:
        await self.db.watchlist.add(
            movie, profile_id=FIRST_PROFILE_ID, auto_download=True, when=NOW - timedelta(days=40)
        )
        await self.db.watchlist.record_try(movie.id, NOW - tried, give_up=True)

    async def test_given_up_episodes_are_listed(self) -> None:
        await self.add_show(
            1,
            [
                episode(11, 1, NOW - timedelta(days=6), number=1),
                episode(12, 1, NOW - timedelta(days=5), number=2),
                episode(13, 1, NOW - timedelta(days=5), number=3),  # downloaded since
                episode(14, 1, NOW - timedelta(days=4), number=4),  # never tried: still open
            ],
            name="Severance",
        )
        await self.give_up(11, timedelta(days=3))
        await self.give_up(12, timedelta(days=1))
        await self.give_up(13, timedelta(days=1))
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV, media_id=1, season=1, episode=3, release_title="x"
            )
        )
        await self.give_up_film(DUNE, timedelta(days=2))

        listed = await self.service.listing()
        # Newest give-up (its last try) first, episodes and films together.
        self.assertEqual(
            [item.label for item in listed], ["Severance S01E02", "Dune (2021)", "Severance S01E01"]
        )
        first = listed[0]
        assert first.episode is not None
        self.assertEqual(first.episode.episode.id, 12)
        self.assertEqual(first.tried_at, NOW - timedelta(days=1))
        self.assertEqual(listed[1].movie, DUNE)
        self.assertEqual(GIVEN_UP_MAX, 50)
        self.assertEqual(len(await self.service.listing(limit=2)), 2)

    async def test_a_give_up_from_before_round_one_without_a_try_is_left_out(self) -> None:
        # Before 2026-11-15 an episode missed while the PC was off was given up unsearched
        # (gave_up = 1, tries = 0): it is open again (due), not "couldn't find".
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=6), number=1)])
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO auto_attempts (episode_id, tries, last_try, gave_up) "
                "VALUES (?, 0, ?, 1)",
                (11, (NOW - timedelta(days=2)).isoformat()),
            )
        self.assertEqual(await self.service.listing(), ())
        self.assertEqual(await self.due(), [11])

    async def test_given_up_episodes_of_a_show_switched_off_are_left_out(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=6), number=1)])
        await self.give_up(11, timedelta(days=3))
        self.assertEqual(len(await self.service.listing()), 1)
        await self.library.set_auto_download(1, False, profile_id=FIRST_PROFILE_ID)
        self.assertEqual(await self.service.listing(), ())

    async def test_try_again_clears_the_give_up_and_queues_it(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=6), number=1)], name="Lost")
        await self.give_up(11, timedelta(days=3))
        [item] = await self.service.listing()

        self.assertTrue(await self.service.try_again(item))
        # The give-up is gone: the next automatic run includes it (never tried again).
        self.assertEqual(await self.attempt(11), (0, (NOW - timedelta(days=3)).isoformat(), 0))
        self.assertEqual(await self.due(), [11])
        self.assertEqual(await self.service.listing(), ())
        # And one download starts now, as the calendar's Download makes it.
        [queued] = self.queue.items
        self.assertEqual(queued.label, "Lost S01E01")
        self.assertFalse(queued.again)  # grabbed meanwhile: not fetched a second time
        self.assertEqual(
            (queued.target.media_type, queued.target.media_id, queued.target.season,
             queued.target.episode),
            (MediaType.TV, 1, 1, 1),
        )
        assert isinstance(queued.request, SearchRequest)
        self.assertEqual(queued.request.kind, SearchKind.EPISODE)
        self.assertEqual((queued.request.season, queued.request.episode), (1, 1))

    async def test_try_again_on_a_film_switches_it_back_on(self) -> None:
        await self.give_up_film(DUNE, timedelta(days=2))
        [item] = await self.service.listing()
        self.assertIsInstance(item, GivenUp)

        self.assertTrue(await self.service.try_again(item))
        film = await self.db.watchlist.film(DUNE.id)
        assert film is not None
        self.assertEqual((film.gave_up, film.tries, film.auto_download), (False, 0, True))
        self.assertIsNone(film.first_try)
        self.assertEqual(await self.service.listing(), ())
        [queued] = self.queue.items
        self.assertEqual(queued.label, "Dune (2021)")
        self.assertFalse(queued.again)
        self.assertEqual(queued.target.media_type, MediaType.MOVIE)
        self.assertEqual(queued.target.media_id, DUNE.id)
        assert callable(queued.request)
        request = await queued.request()
        self.assertEqual(request.title, "Dune (2021)")  # made at its turn by its TMDB id
