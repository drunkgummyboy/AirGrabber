"""What the automatic downloads couldn't find (item 23): the Downloads page's "Couldn't find
automatically".

`GivenUpService` (`services.given_up`) lists the episodes the automatic downloads gave up on after
trying (`LibraryRepository.auto_given_up`: still followed with the switch on, not grabbed, not
in Plex) and the watchlist films they gave up on (on anyone's list, not downloaded since: a grab
ends a film's search, `WatchlistService.given_up`; one Plex has is left out, as the automatic
downloads skip it), the latest give-up (its last try) first, at most GIVEN_UP_MAX.
No schema of its own: `auto_attempts.gave_up` and the watchlist's `gave_up`.

`try_again` clears the give-up (an episode's tries forgotten, `gave_up = 0, tries = 0`; a film's
switch on again with its tries forgotten), so the next automatic run includes it, and queues
one download of it now in the download queue, as the calendar's / the film's ⬇ makes it (the
queue's listeners, the main window's QueueLink, tell how it went).

Log hygiene: ids and counts only.
"""

from __future__ import annotations

import functools
import logging
from typing import Protocol

from app.models import GivenUp, GrabTarget, MediaType, Movie
from app.services.download_queue import FilmRequest, QueuedDownload
from app.services.library import LibraryService
from app.services.search import episode_request
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)

GIVEN_UP_MAX = 50  # rows "Couldn't find automatically" lists at most


class Queue(Protocol):
    """DownloadQueue's `add`."""

    def add(self, item: QueuedDownload) -> bool: ...


def film_label(movie: Movie) -> str:
    """"Dune (2021)", as a film's one-click download is labelled."""
    return f"{movie.title} ({movie.year})" if movie.year else movie.title


class GivenUpService:
    def __init__(
        self,
        library: LibraryService,
        watchlist: WatchlistService,
        queue: Queue,
        *,
        film_request: FilmRequest,
    ) -> None:
        """`film_request`: a film's search by its TMDB id, made when its turn in the queue
        comes (TMDB's description and details first)."""
        self._library = library
        self._watchlist = watchlist
        self._queue = queue
        self._film_request = film_request

    async def listing(self, limit: int = GIVEN_UP_MAX) -> tuple[GivenUp, ...]:
        """The given-up episodes and films, the latest give-up first, at most `limit`."""
        items = [
            GivenUp(label=f"{entry.show.name} {entry.episode.code}", tried_at=tried, episode=entry)
            for entry, tried in await self._library.given_up(limit)
        ]
        films = await self._watchlist.given_up()
        if films:
            plex = await self._library.plex_films()  # none without Plex
            items += [
                GivenUp(label=film_label(film.movie), tried_at=film.last_try, movie=film.movie)
                for film in films
                if not plex.has(film.movie_id, film.imdb_id)
            ]
        items.sort(key=_newest_first)
        return tuple(items[:limit])

    async def try_again(self, item: GivenUp) -> bool:
        """Clear the give-up (the next automatic run includes it) and queue one download of it
        now; False when the queue didn't take it (it is waiting or running already)."""
        if item.episode is not None:
            entry = item.episode
            show, episode = entry.show, entry.episode
            assert episode.number is not None  # specials aren't looked after (nor given up)
            await self._library.try_again(episode.id)
            queued = QueuedDownload(
                label=item.label,
                target=GrabTarget(
                    media_type=MediaType.TV,
                    media_id=show.id,
                    season=episode.season,
                    episode=episode.number,
                ),
                request=episode_request(show, episode.season, episode.number),
                # A listed title was never grabbed: should one be grabbed meanwhile (by hand,
                # or the automatic run), this one doesn't fetch it a second time.
                again=False,
            )
        else:
            movie = item.movie
            assert movie is not None
            await self._watchlist.try_again(movie.id)
            queued = QueuedDownload(
                label=item.label,
                target=GrabTarget(media_type=MediaType.MOVIE, media_id=movie.id),
                request=functools.partial(self._film_request, movie.id, item.label),
                again=False,
            )
        added = self._queue.add(queued)
        log.info(
            "Couldn't find automatically: %s %d tried again (%s)",
            item.key[0].value, item.key[1], "queued" if added else "in the queue already",
        )
        return added


def _newest_first(item: GivenUp) -> tuple[float, int]:
    tried = item.tried_at.timestamp() if item.tried_at is not None else 0.0
    return (-tried, -item.key[1])
