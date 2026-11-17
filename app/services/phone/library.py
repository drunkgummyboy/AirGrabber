"""The folder library on the phone (docs/folder-library/2026-11-16-companion-contract.md): what a
Companion that knows the folder library asks while the PC's own folders are on: instead of Plex
when they are the only library, and for its separate Downloaded section beside Plex (the two
shelves, `"folders": true` in hello).

- `GET /v1/library?kind=movie|show`: the films or shows, by title (a title with no file left out).
- `GET /v1/library/<title key>`: a title plus TMDB's details (empty when TMDB can't be asked).
- `GET /v1/library/<show key>/episodes`: a show's episode files, by season and number, the
  specials (season 0) last, each named as TMDB names it in the app's language ("" otherwise).
- `GET /v1/library/<video key>/file`: the best copy that is there now (to copy to the phone),
  `Range: bytes=N-` from byte N; none to be reached: 404 `no_longer_there`.
- `GET /v1/library/<video key>/subtitles` and `…/subtitles/<id>` (`?as=vtt`: an SRT as WebVTT):
  the subtitle files next to that copy (sidecars.py), in the order the folder gave them.
- `POST /v1/library/<video key>/play-link`: a play link (play_links.py) to that copy and its SRT
  and VTT subtitles (each under the id `subtitles` gives it), on the address the phone reached
  the PC on, for streaming on the phone's own player or a TV.
- `PUT /v1/library/<video key>/place` (`{"position_ms", "duration_ms"}`), `PUT …/watched`
  (`{"watched"}`) and `DELETE …/place` (off Carry on): 204, the phone's person's place and mark
  through playing.py (its 90 % and 10 s rules). A title key there is 400; a key that names no
  video with a file, 404 `no_longer_there`.

Title keys are "folder:movie:<tmdb>" / "folder:show:<tmdb>" (`plex_titles`' keys); a video's is
playing.py's item ("movie:<tmdb>", "episode:<tmdb>:<season>:<episode>"), so a place survives a
file MediaForge renamed or moved. A double episode file is one video under its first episode,
with `episode_end` set. Each copy is the best one stored (playing.py's `ranked`). Every place and
watched mark is the phone's own person's (`PairedPhone.profile_id`), never the PC's.

The lists come from the database and TMDB. Only `file`, the subtitles and play links touch the
disk (often a NAS's share, which may be asleep): every stat, listing and read in a worker thread
under READ_TIMEOUT_S (part 1's), so the window never waits for a share; one that doesn't answer
in time is 404 `no_longer_there`. They answer whenever the folders are on (`folders_on`), with or
without Plex; with the folders off they answer 409 `plex_library` (the phone refreshes hello; the
phone connection's gate answers first without any library). The log gets counts and the kind
of a failure only, never a title, a key, a path, a link or an address.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Iterable, Mapping
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, TypeGuard, TypeVar
from urllib.parse import quote

from app.database import LibraryFileRepository, WatchPlaceRepository
from app.i18n import N_, t
from app.models.domain import TMDB_IMAGE_BASE, TitleExtras, TitleInfo
from app.models.folder_library import FileKind, LibraryFile, WatchPlace
from app.models.plex import LibraryKind, PlexTitle, PlexVideo
from app.services.discovery import DiscoveryService
from app.services.folder_library.match import title_key
from app.services.folder_library.playing import (
    FolderPlaying,
    canonical_item,
    episode_item,
    film_item,
    parse_item,
    ranked,
)
from app.services.folder_library.scan import READ_TIMEOUT_S
from app.services.folder_library.sidecars import CONTENT_TYPES, VTT_TYPE, Sidecar, sidecars
from app.services.library_source import folders_on
from app.services.phone.coming_up import safe_poster
from app.services.phone.play_links import MAX_SUBTITLE_BYTES as MAX_SUBTITLE_BYTES
from app.services.phone.play_links import PLAY_PORT, PlayLinks, play_url_host, subtitle_bytes
from app.services.phone.play_links import file_size as _size_of
from app.services.phone.play_links import video_type as _video_type
from app.services.phone.server import (
    AnyReply,
    DataReply,
    FileReply,
    Reply,
    Request,
    Route,
    bad_request,
    error_reply,
    range_start,
)
from app.services.phone.suggestions import split_genres
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient, TMDBError

log = logging.getLogger(__name__)

PLEX_LIBRARY_TEXT = N_("AirGrabber's library is Plex now, so pull down to refresh.")
GONE_TEXT = N_("That isn't in your folders any more, so refresh the list.")
CAST_SIZE = 15
_POSTER_SIZE = "w342"
_ENGLISH = "en-US"  # the genres: the phone joins them by their English names
_TITLE_KEY = re.compile(r"folder:(movie|show):([0-9]{1,12})")
_FILE_KIND: Mapping[str, FileKind] = {"movie": "movie", "show": "episode"}
_STREAMED_FORMATS = ("srt", "vtt")  # the subtitles a play link carries, as VTT
_SUBTITLE_ID = re.compile(r"0|[1-9][0-9]{0,5}")
_MOST_MS = 10**10  # a place's or length's milliseconds: over 115 days, more than any video
_DONE = Reply(204, {})

T = TypeVar("T")
_Copies = dict[tuple[int, int], list[LibraryFile]]  # an episode's copies by (season, episode)


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


def video_json(
    item: str, file: LibraryFile, place: WatchPlace | None, title: str
) -> dict[str, Any]:
    """A video as the phone gets it: `file`, the copy it would be sent, at the person's `place`
    (none: from the start, its length not known)."""
    episode = file.kind == "episode"
    return {
        "key": item,
        "season": file.season if episode else None,
        "episode": file.episode if episode else None,
        "episode_end": file.episode_end if episode else None,
        "title": title,
        "quality": file.quality,
        "size": file.size,
        "ext": PureWindowsPath(file.path).suffix.lstrip(".").lower(),
        "duration_ms": place.duration_ms if place is not None else None,
        "position_ms": place.position_ms if place is not None else 0,
        "watched": place.watched if place is not None else False,
        "last_watched_at": _iso(place.updated_at) if place is not None else None,
    }


def _gone() -> Reply:
    return error_reply(404, "no_longer_there", t(GONE_TEXT))


def _grouped(files: Iterable[LibraryFile]) -> dict[tuple[FileKind, int], list[LibraryFile]]:
    """The recognised files by (kind, TMDB id)."""
    found: dict[tuple[FileKind, int], list[LibraryFile]] = {}
    for file in files:
        if file.recognised and file.kind is not None and file.tmdb_id is not None:
            found.setdefault((file.kind, file.tmdb_id), []).append(file)
    return found


def _episode_copies(files: Iterable[LibraryFile]) -> _Copies:
    copies: _Copies = {}
    for file in files:
        if file.season is not None and file.episode is not None:
            copies.setdefault((file.season, file.episode), []).append(file)
    return copies


def _episode_order(episode: tuple[int, int]) -> tuple[bool, int, int]:
    """By season and number, the specials (season 0) last."""
    season, number = episode
    return season == 0, season, number


def _no_details() -> dict[str, Any]:
    return {
        "overview": "",
        "genres": [],
        "rating": None,
        "backdrop_url": None,
        "runtime_min": None,
        "trailer_url": None,
        "cast": [],
    }


def _details(extras: TitleExtras | None, english: TitleInfo | None) -> dict[str, Any]:
    details = _no_details()
    if extras is not None:
        info = extras.info
        details |= {
            "overview": info.overview,
            "rating": info.vote_average if info.vote_count else None,
            "backdrop_url": safe_poster(info.backdrop_url()),
            "runtime_min": info.runtime,
            "trailer_url": extras.trailer.watch_url if extras.trailer is not None else None,
            "cast": [
                {
                    "name": member.name,
                    "character": member.character,
                    "photo_url": safe_poster(member.photo_url()),
                }
                for member in extras.cast[:CAST_SIZE]
            ],
        }
    if english is not None:
        details["genres"] = split_genres(english.genres)
    return details


def _told(result: T | BaseException) -> T | None:
    """What a TMDB call gathered gave; None when TMDB couldn't say (anything else is raised)."""
    if isinstance(result, BaseException):
        if not isinstance(result, TMDBError):
            raise result
        log.info("Phone library: no details from TMDB: %s", type(result).__name__)
        return None
    return result


class PhoneLibrary:
    def __init__(
        self,
        settings: SettingsService,
        playing: FolderPlaying,
        files: LibraryFileRepository,
        places: WatchPlaceRepository,
        discovery: DiscoveryService,
        tmdb: TMDBClient,
        *,
        play_links: PlayLinks | None = None,
    ) -> None:
        """`play_links`: the link server's links (one, shared with the phone connection, which
        runs it)."""
        self._settings = settings
        self._playing = playing
        self._files = files
        self._places = places
        self._discovery = discovery
        self._tmdb = tmdb
        self._play_links = play_links if play_links is not None else PlayLinks()

    def routes(self) -> dict[tuple[str, str], Route]:
        return {
            ("GET", "/v1/library"): self._while_folders_on(self._list),
            ("GET", "/v1/library/{key}"): self._while_folders_on(self._title_details),
            ("GET", "/v1/library/{key}/episodes"): self._while_folders_on(self._episodes),
            ("GET", "/v1/library/{key}/file"): self._while_folders_on(self._file),
            ("GET", "/v1/library/{key}/subtitles"): self._while_folders_on(self._subtitles),
            ("GET", "/v1/library/{key}/subtitles/{id}"): self._while_folders_on(self._subtitle),
            ("POST", "/v1/library/{key}/play-link"): self._while_folders_on(self._play_link),
            ("PUT", "/v1/library/{key}/place"): self._while_folders_on(self._place),
            ("PUT", "/v1/library/{key}/watched"): self._while_folders_on(self._watched),
            ("DELETE", "/v1/library/{key}/place"): self._while_folders_on(self._off_carry_on),
        }

    def _while_folders_on(self, handler: Route) -> Route:
        """`handler` while the folders are on (beside Plex too); off, 409 `plex_library`: the
        phone refreshes hello (with no library at all the phone connection's gate has answered
        `no_library` / `no_plex` before this, so off here means Plex is the library)."""

        async def folders(request: Request) -> AnyReply:
            if not folders_on(self._settings.current):
                return error_reply(409, "plex_library", t(PLEX_LIBRARY_TEXT))
            return await handler(request)

        return folders

    # -- the routes ------------------------------------------------------------------------------

    async def _list(self, request: Request) -> Reply:
        kind = request.query.get("kind", "")
        file_kind = _FILE_KIND.get(kind)
        if file_kind is None:
            return bad_request()
        groups = _grouped((await self._files.all()).values())
        places = await self._places.places(_person(request))
        items = [
            self._title_json(title, groups[(file_kind, title.tmdb_id)], places)
            for title in await self._playing.titles()
            if title.kind == kind
            and title.tmdb_id is not None
            and (file_kind, title.tmdb_id) in groups
        ]
        items.sort(key=lambda item: (str(item["title"]).casefold(), str(item["key"])))
        log.info("Phone library: %d title(s)", len(items))
        return Reply(200, {"items": items})

    async def _title_details(self, request: Request) -> Reply:
        found = await self._title(request.query.get("key", ""))
        if found is None:
            return _gone()
        title, files = found
        places = await self._places.places(_person(request))
        about = self._title_json(title, files, places)
        assert title.tmdb_id is not None  # _title finds only these
        extras: Awaitable[TitleExtras]
        english: Awaitable[TitleInfo]
        if title.kind == "movie":
            extras = self._discovery.movie_extras(title.tmdb_id)
            english = self._tmdb.movie_info(title.tmdb_id, language=_ENGLISH)
        else:
            extras = self._discovery.tv_extras(title.tmdb_id)
            english = self._tmdb.tv_info(title.tmdb_id, language=_ENGLISH)
        got_extras, got_english = await asyncio.gather(extras, english, return_exceptions=True)
        return Reply(200, about | _details(_told(got_extras), _told(got_english)))

    async def _episodes(self, request: Request) -> Reply:
        found = await self._title(request.query.get("key", ""))
        if found is None or found[0].kind != "show":
            return _gone()
        title, files = found
        assert title.tmdb_id is not None  # _title finds only these
        tmdb_id = title.tmdb_id
        copies = _episode_copies(files)
        places = await self._places.places(_person(request))
        names = await self._episode_names(tmdb_id, {season for season, _ in copies})
        episodes = []
        for season, number in sorted(copies, key=_episode_order):
            item = episode_item(tmdb_id, season, number)
            best = ranked(copies[(season, number)])[0]
            name = names.get((season, number), "")
            episodes.append(video_json(item, best, places.get(item), name))
        log.info("Phone library: %d episode(s)", len(episodes))
        return Reply(200, {"episodes": episodes})

    async def _file(self, request: Request) -> Reply | FileReply:
        found = await self._reachable_copy(request.query.get("key", ""))
        if found is None:
            return _gone()
        path, size = found
        start = range_start(request.headers, size)
        if start is False:
            return Reply(416, bad_request().body)
        if start is None:
            return FileReply(path, _video_type(path), size)
        return FileReply(path, _video_type(path), size, start=start, partial=True)

    async def _subtitles(self, request: Request) -> Reply:
        found = await self._subtitles_of(request.query.get("key", ""))
        if found is None:
            return _gone()
        return Reply(
            200,
            {
                "subtitles": [
                    {
                        "id": str(number),
                        "language": one.language,
                        "forced": one.forced,
                        "format": one.format,
                    }
                    for number, one in enumerate(found)
                ]
            },
        )

    async def _subtitle(self, request: Request) -> Reply | DataReply:
        as_vtt = "as" in request.query
        if as_vtt and request.query["as"] != "vtt":
            return bad_request()
        found = await self._subtitles_of(request.query.get("key", ""))
        wanted = request.query.get("id", "")
        if found is None or not _SUBTITLE_ID.fullmatch(wanted) or int(wanted) >= len(found):
            return _gone()
        sidecar = found[int(wanted)]
        if as_vtt and sidecar.format not in ("srt", "vtt"):
            return bad_request()  # only an SRT converts (a VTT is one already)
        convert = as_vtt and sidecar.format == "srt"
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                data = await asyncio.to_thread(_subtitle_bytes, sidecar.path, convert=convert)
        except (OSError, TimeoutError, ValueError) as exc:
            log.info("Phone library: a subtitle file can't be read: %s", type(exc).__name__)
            return _gone()
        if data is None:
            log.info("Phone library: a subtitle file is too big to send")
            return bad_request()
        return DataReply(VTT_TYPE if convert else CONTENT_TYPES[sidecar.format], data)

    async def _play_link(self, request: Request) -> Reply:
        host = play_url_host(request.local_address)
        if host is None:
            return bad_request()  # the phone server always knows it: can't happen through it
        found = await self._reachable_copy(request.query.get("key", ""))
        if found is None:
            return _gone()
        path, _size = found
        found_subtitles = await self._sidecars_of(path)
        # All of them, so a subtitle's place in the link is its id in `subtitles`; the link
        # server sends only an SRT or VTT.
        token, expires = self._play_links.make(path, [one.path for one in found_subtitles])
        base = f"http://{host}:{PLAY_PORT}/p/{token}"
        log.info("Phone library: a play link made")
        return Reply(
            200,
            {
                "url": f"{base}/video{quote(path.suffix.lower())}",
                "subtitles": [
                    {
                        "id": str(number),
                        "language": one.language,
                        "forced": one.forced,
                        "url": f"{base}/sub/{number}.vtt",
                    }
                    for number, one in enumerate(found_subtitles)
                    if one.format in _STREAMED_FORMATS
                ],
                "expires_at": expires.isoformat(),
            },
        )

    async def _place(self, request: Request) -> Reply:
        """Where the phone's person is in the video (every ~10 s while playing, and once when
        it stops): not logged, there are too many."""
        position = request.body.get("position_ms")
        duration = request.body.get("duration_ms")
        if (
            "duration_ms" not in request.body
            or not _milliseconds(position)
            or not (duration is None or _milliseconds(duration))
        ):
            return bad_request()
        item = await self._video_item(request)
        if isinstance(item, Reply):
            return item
        video = PlexVideo(key=item, file="")
        await self._playing.report(video, position, duration, profile_id=_person(request))
        return _DONE

    async def _watched(self, request: Request) -> Reply:
        watched = request.body.get("watched")
        if not isinstance(watched, bool):
            return bad_request()
        item = await self._video_item(request)
        if isinstance(item, Reply):
            return item
        person = _person(request)
        if watched:
            await self._playing.mark_watched(PlexVideo(key=item, file=""), profile_id=person)
        else:
            await self._playing.mark_unwatched(item, profile_id=person)
        log.info("Phone library: a video marked %s", "watched" if watched else "unwatched")
        return _DONE

    async def _off_carry_on(self, request: Request) -> Reply:
        item = await self._video_item(request)
        if isinstance(item, Reply):
            return item
        await self._playing.clear_place(item, profile_id=_person(request))
        log.info("Phone library: a video taken off Carry on")
        return _DONE

    # -- for the other routes (file, subtitles, play links, places) ------------------------------

    async def _video_item(self, request: Request) -> str | Reply:
        """The request's video key as the library stores it, for its place and mark: a title key
        is 400; a key that names no film or episode with a file in the library, 404
        `no_longer_there`."""
        key = request.query.get("key", "")
        if _TITLE_KEY.fullmatch(key):
            return bad_request()
        item = canonical_item(key)  # "movie:0603" is "movie:603", the key the list gives
        if item is None or not await self._playing.copies_of(item):  # a Plex key, or any other
            return _gone()
        return item

    async def _phone_video(self, key: str, profile_id: int) -> dict[str, Any] | None:
        """The video with this key (a film's or an episode's) as the phone gets it, its best
        stored copy at the person's place; None when it isn't one or has no file. A film is named
        as its title, an episode "" (its name is TMDB's, asked for by `episodes`)."""
        parsed = parse_item(key)
        if parsed is None:
            return None
        kind, tmdb_id, season, number = parsed
        files = [
            file
            for file in await self._files.of_title(kind, tmdb_id)
            if kind == "movie" or (file.season, file.episode) == (season, number)
        ]
        if not files:
            return None
        name = ""
        if kind == "movie":
            item = film_item(tmdb_id)
            wanted = title_key("movie", tmdb_id)
            name = next(
                (title.title for title in await self._playing.titles() if title.key == wanted), ""
            )
        else:
            assert season is not None and number is not None  # parse_item gives both
            item = episode_item(tmdb_id, season, number)
        place = await self._places.get(profile_id, item)
        return video_json(item, ranked(files)[0], place, name)

    async def _reachable_copy(self, item: str) -> tuple[Path, int] | None:
        """The best copy of this video (`item`: its key) that is there now, with its size; None
        when it has none, or none answers (`reachable_copy`)."""
        return await reachable_copy(await self._playing.copies_of(item))

    async def _sidecars_of(self, path: Path) -> list[Sidecar]:
        """The subtitle files next to the video at `path`, in the order its folder lists them;
        none when the folder can't be listed within READ_TIMEOUT_S."""
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                names = await asyncio.to_thread(_names_in, path.parent)
        except (OSError, TimeoutError, ValueError) as exc:
            log.info("Phone library: a video's folder can't be listed: %s", type(exc).__name__)
            return []
        return sidecars(path, names)

    async def _subtitles_of(self, item: str) -> list[Sidecar] | None:
        """The subtitle files next to the copy `file` would send; None when there is none."""
        found = await self._reachable_copy(item)
        return await self._sidecars_of(found[0]) if found is not None else None

    # -- helpers ---------------------------------------------------------------------------------

    async def _title(self, key: str) -> tuple[PlexTitle, list[LibraryFile]] | None:
        """The folder title with this key and its files; None for any other key, or a title
        without a file."""
        matched = _TITLE_KEY.fullmatch(key)
        if matched is None:
            return None
        kind: LibraryKind = "movie" if matched[1] == "movie" else "show"
        tmdb_id = int(matched[2])
        wanted = title_key(kind, tmdb_id)
        title = next((one for one in await self._playing.titles() if one.key == wanted), None)
        if title is None or title.kind != kind or title.tmdb_id != tmdb_id:
            return None
        files = await self._files.of_title(_FILE_KIND[kind], tmdb_id)
        return (title, files) if files else None

    def _title_json(
        self, title: PlexTitle, files: list[LibraryFile], places: Mapping[str, WatchPlace]
    ) -> dict[str, Any]:
        """A title as the phone gets it: a film with its best copy's video, a show with its
        episode files counted and the person's watched ones."""
        assert title.tmdb_id is not None
        about: dict[str, Any] = {
            "key": title.key,
            "kind": title.kind,
            "title": title.title,
            "year": title.year,
            "poster_url": safe_poster(f"{TMDB_IMAGE_BASE}{_POSTER_SIZE}{title.thumb}")
            if title.thumb
            else None,
            "added_at": _iso(title.added_at),
            "tmdb_id": title.tmdb_id,
            "quality": None,
            "video": None,
            "seasons": None,
            "episodes": None,
            "watched_episodes": None,
            "last_watched_at": None,
        }
        if title.kind == "movie":
            best = ranked(files)[0]
            item = film_item(title.tmdb_id)
            place = places.get(item)
            return about | {
                "quality": best.quality,
                "video": video_json(item, best, place, title.title),
                "last_watched_at": _iso(place.updated_at) if place is not None else None,
            }
        copies = _episode_copies(files)
        mine = [
            place
            for season, number in copies
            if (place := places.get(episode_item(title.tmdb_id, season, number))) is not None
        ]
        newest = max((place.updated_at for place in mine), default=None)
        return about | {
            "seasons": len({season for season, _ in copies}),
            "episodes": len(copies),
            "watched_episodes": sum(place.watched for place in mine),
            "last_watched_at": _iso(newest),
        }

    async def _episode_names(
        self, tv_id: int, seasons: Iterable[int]
    ) -> dict[tuple[int, int], str]:
        """TMDB's episode names in the app's language, one request per season; a season TMDB
        doesn't know, or can't be asked about, gives none."""
        wanted = sorted(seasons)
        results = await asyncio.gather(
            *(self._tmdb.tv_season(tv_id, season) for season in wanted), return_exceptions=True
        )
        names: dict[tuple[int, int], str] = {}
        for season, result in zip(wanted, results, strict=True):
            found = _told(result)
            if found is None:
                continue
            for episode in found.episodes:
                names[(season, episode.episode_number)] = episode.name
        return names


async def reachable_copy(copies: Iterable[str]) -> tuple[Path, int] | None:
    """The first of these copies (best first) that is there now, with its size; None when there
    are none, or none answers. Every copy is stat'ed at once, each in a worker thread, and all
    within READ_TIMEOUT_S: the best one that answered in time wins. Also the phone-sized copies'
    way to a folder film's own file (`PhoneCopies`' `local_source`, built in app/services)."""
    paths = [Path(one) for one in copies]
    if not paths:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READ_TIMEOUT_S
    looks = [asyncio.ensure_future(asyncio.to_thread(_size_of, path)) for path in paths]
    try:
        for path, look in zip(paths, looks, strict=True):
            if not look.done():
                await asyncio.wait((look,), timeout=max(0.0, deadline - loop.time()))
            size = _size_found(look)
            if size is not None:
                return path, size
    finally:
        for look in looks:  # a stat past its limit ends on its own: its outcome isn't wanted
            look.add_done_callback(_retrieve)
    log.info("Phone library: none of a video's %d copies can be reached", len(paths))
    return None


def _person(request: Request) -> int:
    assert request.phone is not None  # the server lets only paired phones through
    return request.phone.profile_id


def _milliseconds(value: object) -> TypeGuard[int]:
    """A place or a length the phone sent: a whole number from 0 to _MOST_MS (not a bool)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MOST_MS


# -- the disk: each in a worker thread ----------------------------------------------------------


def _names_in(folder: Path) -> list[str]:
    return os.listdir(folder)


def _subtitle_bytes(path: Path, *, convert: bool) -> bytes | None:
    """The subtitle file's bytes (`convert`: an SRT's as WebVTT, UTF-8); None when it's over
    MAX_SUBTITLE_BYTES."""
    return subtitle_bytes(path, convert=convert, most=MAX_SUBTITLE_BYTES)


def _size_found(look: asyncio.Future[int | None]) -> int | None:
    """What a stat that has ended found; None when it hasn't ended, or couldn't reach the file."""
    if not look.done() or look.cancelled():
        return None
    failed = look.exception()
    if failed is None:
        return look.result()
    if isinstance(failed, (OSError, ValueError)):
        log.info("Phone library: a video's copy can't be reached: %s", type(failed).__name__)
        return None
    raise failed


def _retrieve(look: asyncio.Future[int | None]) -> None:
    if not look.cancelled():
        look.exception()
