"""`GET /v1/collection/{tmdb_id}`: the films of the film series ("In deze reeks") a film belongs
to, for a film's page on the phone.

The answer is `{"parts": [{"tmdb_id", "title", "year", "poster_url"}]}` by release date, the film
itself included; `{"parts": []}` for a film in no series, or one TMDB doesn't know. Open to every
phone, a library manager's or a viewer's: it says nothing about anyone's lists. TMDB's key stays
on the PC (a picture's address that could hold a secret isn't sent); with TMDB out of reach, or no
key set, the phone gets one plain sentence.

Nothing here logs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from app.i18n import t
from app.models import Movie
from app.services.phone.coming_up import safe_poster
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.tmdb import TMDBError, TMDBNotFound

_POSTER_SIZE = "w342"
_MAX_ID = 10**9


def _film_id(text: str) -> int | None:
    if not (text.isascii() and text.isdecimal()) or len(text) > 10:
        return None
    number = int(text)
    return number if 0 < number < _MAX_ID else None


def _part(movie: Movie) -> dict[str, Any]:
    return {
        "tmdb_id": movie.id,
        "title": movie.title,
        "year": movie.year,
        "poster_url": safe_poster(movie.poster_url(_POSTER_SIZE)),
    }


class FilmSeries:
    def __init__(self, parts_of: Callable[[int], Awaitable[Iterable[Movie]]]) -> None:
        """`parts_of`: a film's TMDB id → its series' films by release date (in the app
        `DiscoveryService.film_series`, which keeps them a while; TMDB errors propagate)."""
        self._parts_of = parts_of

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("GET", "/v1/collection/{tmdb_id}"): self._parts}

    async def _parts(self, request: Request) -> Reply:
        film = _film_id(request.query.get("tmdb_id", ""))
        if film is None:
            return bad_request()
        try:
            parts = list(await self._parts_of(film))
        except TMDBNotFound:
            parts = []
        except TMDBError:
            return error_reply(
                502,
                "failed",
                t(
                    "AirGrabber couldn't look up the films in this series just now, "
                    "so try again in a moment."
                ),
            )
        return Reply(200, {"parts": [_part(movie) for movie in parts]})
