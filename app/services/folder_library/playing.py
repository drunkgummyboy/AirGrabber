"""▶ Play from the folder library (docs/folder-library/, "Playing and per-person state"): the
same title and video shapes Plex gives playback (`PlexTitle`, `PlexVideo`), made from what the
last check stored, and each person's own place in a video.

- **Titles** are the folder titles in their own table (`folder_titles`, `db.folders`; key
  "folder:…", match.py's `title_key`), beside Plex's or without Plex.
- **A video** is a film's or an episode's file from `library_files`: `file` is the local path
  (handed to the player as it is; never logged) and `key` is `item_key`, the title's identity
  ("movie:<tmdb>", "episode:<tmdb>:<season>:<episode>"), so a file MediaForge renamed or moved
  keeps its place. A double episode ("S01E03-E04") is one video, under its first episode.
- **Two copies** of a film or an episode: the one Plex's rule picks (plex.py's `version_rank`:
  the taller picture by the quality its name says, then the bigger file). `copies` gives them
  all in that order, so ▶ Play's `locate` plays the best one still there.
- **Places** are per person (`watch_places`; the PC's person is ProfileService.current_id, a
  phone's its own): `view_offset_ms` / `duration_ms` / `watched` of a video come from the
  person's row. A place under MIN_PLACE_MS isn't kept (nothing to resume, as for a file from
  Explorer); one at WATCHED_SHARE of the length or past it marks the video watched. Continue
  watching is the person's videos with a place, the newest first. A phone also marks a video
  unwatched (back to the start) and takes one off Carry on (its place to 0, watched kept).

Nothing here touches a file on disk. Logs give counts only, never a path, a file name or a title.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol

from app.database import FolderRepository, LibraryFileRepository, WatchPlaceRepository
from app.i18n import N_
from app.models import Settings
from app.models.folder_library import FileKind, LibraryFile, WatchPlace
from app.models.plex import PlexContinue, PlexTitle, PlexVideo
from app.services.library_source import folders_on
from app.services.local_files import MIN_PLACE_MS
from app.services.plex import CONTINUE_SIZE, FOLDER_KEY_PREFIX, episode_order, version_rank

log = logging.getLogger(__name__)

__all__ = [
    "MIN_PLACE_MS",
    "NOT_IN_FOLDERS_TEXT",
    "WATCHED_SHARE",
    "FolderPlaying",
    "from_folders",
    "is_folder_video",
    "item_key",
    "logged_key",
]

WATCHED_SHARE = 0.9  # watched from here, as Plex's mark (app/ui/media_player.py uses this one)
# The status bar when no copy of a folder library title is there from this PC.
NOT_IN_FOLDERS_TEXT = N_("AirGrabber can't find {name} in your folders right now.")
_HEIGHTS = {"4K": 2160, "1080p": 1080, "720p": 720, "SD": 480}  # the quality a name says
_CONTINUE_READ = 50  # places read for Continue watching (some may name what is gone)
_MAX_DIGITS = 12  # of an item key's numbers (a phone's key): more can't be stored as one


class SettingsSource(Protocol):
    """SettingsService."""

    @property
    def current(self) -> Settings: ...


def from_folders(title: PlexTitle) -> bool:
    """A title of the folder library (not one of Plex's)."""
    return title.key.startswith(FOLDER_KEY_PREFIX)


def is_folder_video(video: PlexVideo) -> bool:
    """A video of the folder library (its key an item key, `parse_item`), not one of Plex's: it
    plays from its own file and its place goes to `watch_places`, never to Plex."""
    return parse_item(video.key) is not None


def logged_key(key: str) -> str:
    """A video's or title's key as a log line may hold it: a Plex ratingKey (digits) as it is,
    any other only as the text before its first `:` ("episode", "folder"): a folder key's TMDB
    id would name the title (as app/services/phone/copies.py's `_logged`)."""
    return key if key.isascii() and key.isdigit() else key.split(":", 1)[0]


def film_item(tmdb_id: int) -> str:
    return f"movie:{tmdb_id}"


def episode_item(tmdb_id: int, season: int, episode: int) -> str:
    return f"episode:{tmdb_id}:{season}:{episode}"


def parse_item(item: str) -> tuple[FileKind, int, int | None, int | None] | None:
    """(kind, TMDB id, season, episode) of an item key; None for anything else (a Plex key, or
    numbers no TMDB id or season could be: other digits than 0-9, or over _MAX_DIGITS)."""
    parts = item.split(":")
    if not all(
        part.isascii() and part.isdigit() and len(part) <= _MAX_DIGITS for part in parts[1:]
    ):
        return None
    if parts[0] == "movie" and len(parts) == 2:
        return "movie", int(parts[1]), None, None
    if parts[0] == "episode" and len(parts) == 4:
        return "episode", int(parts[1]), int(parts[2]), int(parts[3])
    return None


def canonical_item(item: str) -> str | None:
    """An item key as the library stores it ("movie:0603" is "movie:603"); None for a key that
    is no folder library video's (`parse_item`)."""
    parsed = parse_item(item)
    if parsed is None:
        return None
    kind, tmdb_id, season, episode = parsed
    if kind == "movie" or season is None or episode is None:
        return film_item(tmdb_id)
    return episode_item(tmdb_id, season, episode)


def item_key(video: PlexVideo) -> str:
    """The `watch_places` item of a folder library video (its `key`: "movie:<tmdb>",
    "episode:<tmdb>:<season>:<episode>"). ValueError for a video that isn't one (Plex's)."""
    return _checked(video.key)


def copy_rank(file: LibraryFile) -> tuple[int, ...]:
    """A copy's place in ▶ Play's order (plex.py's `version_rank`: one file each)."""
    return version_rank(_HEIGHTS.get(file.quality or "", 0), 1, file.size)


def ranked(files: Iterable[LibraryFile]) -> list[LibraryFile]:
    """Best first; of equal copies, the first by path."""
    return sorted(files, key=copy_rank, reverse=True)


class FolderPlaying:
    """The folder library's side of ▶ Play (see the module docstring)."""

    def __init__(
        self,
        settings: SettingsSource,
        files: LibraryFileRepository,
        places: WatchPlaceRepository,
        titles: FolderRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._files = files
        self._places = places
        self._titles = titles
        self._now = now

    @property
    def on(self) -> bool:
        """The folders count now (switched on with a folder set, with or without Plex)."""
        return folders_on(self._settings.current)

    async def titles(self) -> list[PlexTitle]:
        """The folder library's films and shows, as the last check stored them, by title."""
        return await self._titles.titles()

    async def watched_items(self, *, profile_id: int) -> set[str]:
        """The item keys the person has watched here ("movie:<tmdb>", "episode:<tmdb>:<s>:<e>"),
        whether or not their files are still in the folders (one moved into Plex's folder by
        hand keeps its mark: ▶ Play's next episode goes on after it)."""
        places = await self._places.places(profile_id)
        return {item for item, place in places.items() if place.watched}

    async def checked_at(self) -> datetime | None:
        """When the folders' last check was stored (None: never)."""
        return await self._titles.checked_at()

    async def film_video(self, title: PlexTitle, *, profile_id: int) -> PlexVideo | None:
        """A film's video: its best copy, at the person's place; None when it has no file (or
        isn't a folder library film)."""
        if title.kind != "movie" or title.tmdb_id is None or not from_folders(title):
            return None
        files = ranked(await self._files.of_title("movie", title.tmdb_id))
        if not files:
            return None
        item = film_item(title.tmdb_id)
        return _video(item, files[0], await self._places.get(profile_id, item), title.title)

    async def show_videos(self, title: PlexTitle, *, profile_id: int) -> list[PlexVideo]:
        """A show's episodes, each its best copy at the person's place, by season and number
        (the specials, season 0, last)."""
        if title.kind != "show" or title.tmdb_id is None or not from_folders(title):
            return []
        tmdb_id = title.tmdb_id
        copies: dict[tuple[int, int], list[LibraryFile]] = {}
        for file in await self._files.of_title("episode", tmdb_id):
            if file.season is not None and file.episode is not None:
                copies.setdefault((file.season, file.episode), []).append(file)
        places = await self._places.places(profile_id)
        videos = []
        for (season, number), files in copies.items():
            item = episode_item(tmdb_id, season, number)
            videos.append(_video(item, ranked(files)[0], places.get(item)))
        videos.sort(key=episode_order)
        log.info("Folder library: %d episode file(s) of a show", len(videos))
        return videos

    async def copies(self, video: PlexVideo) -> list[str]:
        """Every file of `video`'s film or episode, best first (`locate` plays the first one
        still there); none for a video that isn't the folder library's."""
        return await self.copies_of(video.key)

    async def copies_of(self, item: str) -> list[str]:
        """Every file of the film or episode with this item key, best first; none for any other
        key (the phone's `file` sends the first one still there)."""
        parsed = parse_item(item)
        if parsed is None:
            return []
        kind, tmdb_id, season, number = parsed
        files = [
            file
            for file in await self._files.of_title(kind, tmdb_id)
            if kind == "movie" or (file.season, file.episode) == (season, number)
        ]
        return [file.path for file in ranked(files)]

    async def continue_watching(self, *, profile_id: int) -> list[PlexContinue]:
        """The person's videos with a place to go on from, the newest first, at most
        CONTINUE_SIZE; one whose title or file is gone from the library is left out."""
        places = await self._places.recent(profile_id, _CONTINUE_READ)
        if not places:
            return []
        titles = {title.key: title for title in await self.titles()}
        found: list[PlexContinue] = []
        for place in places:
            chosen = await self._continue_item(place, titles)
            if chosen is not None:
                found.append(chosen)
            if len(found) == CONTINUE_SIZE:
                break
        log.info("Folder library: %d to continue watching", len(found))
        return found

    async def _continue_item(
        self, place: WatchPlace, titles: dict[str, PlexTitle]
    ) -> PlexContinue | None:
        parsed = parse_item(place.item)
        if parsed is None:
            return None
        kind, tmdb_id, season, number = parsed
        title = titles.get(f"{FOLDER_KEY_PREFIX}{'movie' if kind == 'movie' else 'show'}:{tmdb_id}")
        if title is None:
            return None
        files = [
            file
            for file in await self._files.of_title(kind, tmdb_id)
            if kind == "movie" or (file.season, file.episode) == (season, number)
        ]
        if not files:
            return None
        name = title.title if kind == "movie" else ""
        return PlexContinue(title=title, video=_video(place.item, ranked(files)[0], place, name))

    async def report(
        self, video: PlexVideo, position_ms: int, duration_ms: int | None, *, profile_id: int
    ) -> None:
        """Where the person is in `video` (AirGrabber's player: on a new state, a seek, every
        few seconds and when it stops). At WATCHED_SHARE of its length or past it: watched,
        nothing to resume; under MIN_PLACE_MS: no place (nothing stored for a video without
        one)."""
        item = item_key(video)
        before = await self._places.get(profile_id, item)
        length = duration_ms if duration_ms and duration_ms > 0 else None
        if length is None and before is not None:
            length = before.duration_ms
        watched = before.watched if before is not None else False
        position = max(0, position_ms)
        if length and position >= length * WATCHED_SHARE:
            watched, position = True, 0
        elif position < MIN_PLACE_MS:
            if before is None:
                return
            position = 0
        await self._places.put(
            WatchPlace(
                profile_id=profile_id,
                item=item,
                position_ms=position,
                duration_ms=length,
                watched=watched,
                updated_at=self._now(),
            )
        )

    async def mark_watched(self, video: PlexVideo, *, profile_id: int) -> None:
        """The person watched `video` (the player's 90 %, or on to the next episode)."""
        item = item_key(video)
        before = await self._places.get(profile_id, item)
        await self._places.put(
            WatchPlace(
                profile_id=profile_id,
                item=item,
                position_ms=0,
                duration_ms=before.duration_ms if before is not None else None,
                watched=True,
                updated_at=self._now(),
            )
        )

    async def mark_unwatched(self, item: str, *, profile_id: int) -> None:
        """The person marked the video with this item key unwatched: back to the start, its
        length kept; nothing stored for one without a row (it is unwatched already). ValueError
        for a key that isn't a folder library video's."""
        before = await self._places.get(profile_id, _checked(item))
        if before is None:
            return
        changes = {"watched": False, "position_ms": 0, "updated_at": self._now()}
        await self._places.put(before.model_copy(update=changes))

    async def clear_place(self, item: str, *, profile_id: int) -> None:
        """Off Carry on (Continue watching): the person's place in the video with this item key
        back to the start, watched and when it last changed kept (taking it off isn't watching
        it); nothing for one without a row. ValueError for a key that isn't a video's."""
        before = await self._places.get(profile_id, _checked(item))
        if before is None or before.position_ms == 0:
            return
        await self._places.put(before.model_copy(update={"position_ms": 0}))


def _checked(item: str) -> str:
    if parse_item(item) is None:
        raise ValueError("Not a folder library video")
    return item


def _video(item: str, file: LibraryFile, place: WatchPlace | None, title: str = "") -> PlexVideo:
    """A file as ▶ Play's video, at `place` (none: from the start). No Plex markers to ask for."""
    return PlexVideo(
        key=item,
        file=file.path,
        season=file.season if file.kind == "episode" else None,
        episode=file.episode if file.kind == "episode" else None,
        title=title,
        watched=place.watched if place is not None else False,
        view_offset_ms=place.position_ms if place is not None else 0,
        duration_ms=(place.duration_ms or 0) if place is not None else 0,
        markers_known=True,
    )
