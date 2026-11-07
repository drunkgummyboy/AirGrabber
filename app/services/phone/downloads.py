"""The phone's downloads: `POST /v1/download` (a tap: AirGrabber chooses, as the PC's button does),
`POST /v1/releases` and `POST /v1/grab` (holding it: choose by hand).

A phone asks only for what is on its own person's lists, and what's out; a film just grabbed has
left every list, but can still be asked about (a tap then says it was downloaded already, and
`again` works). The releases stay here,
under short random `ref`s for RELEASE_TTL_S: their links (`download_url`, `magnet_uri`) may hold a
key or a passkey and never leave the PC. They come best first (`rank_releases`, what `pick_best`
chooses from); what a tap wouldn't take follows, so "Choose one yourself" also leads somewhere when
nothing was good enough. Likely fakes are never offered.

A `ref` is used up by its grab, so a double tap grabs once; a grab that failed can be tried again.
A person since removed gets a plain answer (`person_gone`).

The log gets what a title is and the outcome only, never a release or a link.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeGuard

from app.i18n import t
from app.models import (
    GrabTarget,
    MediaType,
    MovieDetails,
    QuickDownloadResult,
    SearchRequest,
    TorrentRelease,
)
from app.services.downloads import DownloadService, GrabError
from app.services.library import LibraryService
from app.services.phone.coming_up import GrabbedFilms, person_gone
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.picking import is_likely_fake, rank_releases
from app.services.profiles import ProfileService
from app.services.quick_download import SEARCH_TIMEOUT, QuickDownloadService, request_label
from app.services.remote import RemoteError
from app.services.search import TorrentSearchService, episode_request, movie_request
from app.services.settings import SettingsService
from app.services.watchlist import WatchlistService, is_out

log = logging.getLogger(__name__)

RELEASE_TTL_S = 10 * 60.0
_REF_BYTES = 6  # 8 characters


@dataclass(frozen=True, slots=True)
class _Title:
    """What a phone asked about, once found on its person's lists."""

    request: SearchRequest
    target: GrabTarget
    out: bool

    @property
    def label(self) -> str:
        return request_label(self.request)


@dataclass(frozen=True, slots=True)
class _Kept:
    release: TorrentRelease
    target: GrabTarget
    label: str
    until: float


def _whole_number(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _gone() -> Reply:
    return error_reply(
        410,
        "no_longer_there",
        t("That isn't on your list any more, so refresh the list."),
    )


def _not_out(label: str) -> str:
    return t("{label} isn't out yet, so try again once it is.", label=label)


def _grab_failed(exc: GrabError | RemoteError) -> Reply:
    """Both carry a plain sentence for the user (and never a link)."""
    return error_reply(502, "failed", str(exc))


class PhoneDownloads:
    def __init__(
        self,
        library: LibraryService,
        watchlist: WatchlistService,
        search: TorrentSearchService,
        downloads: DownloadService,
        quick: QuickDownloadService,
        settings: SettingsService,
        profiles: ProfileService,
        films: GrabbedFilms,
        *,
        timeout: float = SEARCH_TIMEOUT,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`timeout`: seconds `/v1/releases` waits for the sources. `now`, `clock`: for tests."""
        self._library = library
        self._watchlist = watchlist
        self._search = search
        self._downloads = downloads
        self._quick = quick
        self._settings = settings
        self._profiles = profiles
        self._grabbed_films = films
        self._timeout = timeout
        self._now = now
        self._clock = clock
        self._kept: dict[str, _Kept] = {}

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {
            ("POST", "/v1/download"): self._download,
            ("POST", "/v1/releases"): self._releases,
            ("POST", "/v1/grab"): self._grab,
        }

    # -- a tap -----------------------------------------------------------------------------------

    async def _download(self, request: Request) -> Reply:
        title = await self._title(request)
        if isinstance(title, Reply):
            return title
        again = request.body.get("again", False)
        if not isinstance(again, bool):
            return bad_request()
        if not title.out:
            return _outcome("nothing_found", _not_out(title.label))
        try:
            result = await self._quick.download(title.request, title.target, again=again)
        except (GrabError, RemoteError) as exc:
            return _grab_failed(exc)
        return _tap_reply(title.label, result)

    # -- choosing by hand ------------------------------------------------------------------------

    async def _releases(self, request: Request) -> Reply:
        title = await self._title(request)
        if isinstance(title, Reply):
            return title
        if not title.out:
            return Reply(200, {"releases": [], "message": _not_out(title.label)})
        found = self._in_order(await self._collect(title.request), title.request)
        self._let_go_of_old_ones()
        until = self._clock() + RELEASE_TTL_S
        items = []
        for release in found:
            ref = secrets.token_urlsafe(_REF_BYTES)
            self._kept[ref] = _Kept(release, title.target, title.label, until)
            items.append(
                {
                    "ref": ref,
                    "title": release.title,
                    "source": release.source,
                    "size_bytes": release.size_bytes,
                    "seeders": release.seeders,
                }
            )
        log.info("Phone: %d downloads to choose from for %s", len(items), title.label)
        if not items:
            message = t("No downloads found for {label}, so try again later.", label=title.label)
            return Reply(200, {"releases": [], "message": message})
        return Reply(200, {"releases": items})

    async def _grab(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        ref = request.body.get("ref")
        if not isinstance(ref, str):
            return bad_request()
        self._let_go_of_old_ones()
        kept = self._kept.pop(ref, None)  # before the first wait: a double tap grabs once
        if kept is None:
            return error_reply(
                410,
                "no_longer_there",
                t("That download isn't available any more, so look for downloads again."),
            )
        try:
            grab = await self._downloads.grab(kept.release, kept.target)
        except (GrabError, RemoteError) as exc:
            self._kept[ref] = kept
            return _grab_failed(exc)
        log.info("Phone: chosen download for %s: %s", kept.label, grab.outcome.value)
        return _outcome("grabbed", f"{kept.label} · {grab.message}")

    def _in_order(
        self, releases: tuple[TorrentRelease, ...], request: SearchRequest
    ) -> list[TorrentRelease]:
        ranked = rank_releases(releases, request, self._settings.current)
        taken = {id(release) for release in ranked}
        return ranked + [
            release
            for release in releases
            if id(release) not in taken and not is_likely_fake(release, request)
        ]

    async def _collect(self, request: SearchRequest) -> tuple[TorrentRelease, ...]:
        """What has arrived when the sources are done, or at the time limit."""
        releases: tuple[TorrentRelease, ...] = ()
        deadline = asyncio.timeout(self._timeout)
        try:
            async with deadline:
                async with contextlib.aclosing(self._search.search(request)) as updates:
                    async for update in updates:
                        releases = update.releases
        except TimeoutError:
            if not deadline.expired():
                raise
        return releases

    def _let_go_of_old_ones(self) -> None:
        now = self._clock()
        for ref, kept in list(self._kept.items()):
            if kept.until <= now:
                del self._kept[ref]

    # -- what was asked about --------------------------------------------------------------------

    async def _title(self, request: Request) -> _Title | Reply:
        """The title `kind`, `id`, `season` and `episode` mean, on the phone's person's lists."""
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        body = request.body
        kind, id = body.get("kind"), body.get("id")
        season, number = body.get("season"), body.get("episode")
        if kind == "movie" and _whole_number(id):
            return await self._film(id, request.phone.profile_id)
        if (
            kind == "episode"
            and _whole_number(id)
            and _whole_number(season)
            and _whole_number(number)
        ):
            return await self._episode(id, season, number, request.phone.profile_id)
        return bad_request()

    async def _episode(self, show_id: int, season: int, number: int, person: int) -> _Title | Reply:
        followed = await self._library.tracked(profile_id=person)
        show = next((entry.show for entry in followed if entry.show.id == show_id), None)
        if show is None:
            return _gone()
        episodes = await self._library.episodes(show_id)
        episode = next(
            (one for one in episodes if one.season == season and one.number == number), None
        )
        if episode is None:
            return _gone()
        return _Title(
            episode_request(show, season, number),
            GrabTarget(media_type=MediaType.TV, media_id=show_id, season=season, episode=number),
            episode.has_aired(self._now()),
        )

    async def _film(self, movie_id: int, person: int) -> _Title | Reply:
        entries = await self._watchlist.by_release_of(person)
        entry = next((one for one in entries if one.movie_id == movie_id), None)
        target = GrabTarget(media_type=MediaType.MOVIE, media_id=movie_id)
        if entry is None:
            return await self._grabbed_film(target, person)
        self._grabbed_films.note(entry, person)
        movie = entry.movie
        return _Title(
            movie_request(movie, MovieDetails(id=movie.id, imdb_id=entry.imdb_id)),
            target,
            is_out(entry, self._now().date()),
        )

    async def _grabbed_film(self, target: GrabTarget, person: int) -> _Title | Reply:
        """A film off the lists: grabbed (a grab takes it off), or just taken off by someone."""
        movie_id = target.media_id
        if not self._grabbed_films.concerns(movie_id, person):
            return _gone()
        on_the_page = movie_id in self._grabbed_films.rows()
        if not on_the_page and not await self._downloads.has_grabbed(target):
            return _gone()
        movie = self._grabbed_films.movie(movie_id)
        if movie is None:  # grabbed long ago: it has left Coming up as well
            return _gone()
        seen = self._grabbed_films.seen(movie_id)
        imdb_id = seen.imdb_id if seen is not None else None
        return _Title(
            movie_request(movie, MovieDetails(id=movie_id, imdb_id=imdb_id)), target, True
        )


def _outcome(outcome: str, message: str) -> Reply:
    return Reply(200, {"outcome": outcome, "message": message})


def _tap_reply(label: str, result: QuickDownloadResult) -> Reply:
    if result.already_downloaded:
        return _outcome("already", t("{label} was already downloaded", label=label))
    if result.grab is None:
        return _outcome(
            "nothing_found",
            t("No good download found for {label}. Choose one yourself.", label=label),
        )
    return _outcome("grabbed", f"{label} · {result.grab.message}")
