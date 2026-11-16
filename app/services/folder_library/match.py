"""Matching the folder library's files to TMDB titles and TVMaze shows (docs/folder-library/):
what scan.py read becomes a `PlexScan`, the shape Plex's check stores
(`PlexRepository.replace`), so every have / missing query sees the folders as it sees Plex.

- **A file's TMDB id:** its .nfo's, else its stored row's (`cached`, while the file is unchanged:
  a row once not recognised isn't asked about again until the file changes), else one TMDB
  search (`TMDBClient.find_titles`, title and year) per new title, shared by every file of that
  title in this check, and by a file already matched with the same title and year. TMDB's
  matches are cached per file in `library_files`, never read from MediaForge's forge.db.
- **A title** (a film, or a show by its episodes' show) is a `PlexTitle` keyed
  "folder:movie:<tmdb id>" / "folder:show:<tmdb id>" (`title_key`): TMDB's title, year and
  poster path (`thumb`, "/abc.jpg") and its IMDb / TheTVDB ids (`TMDBClient.title_ids`, one
  request); a show's TVMaze id from `LibraryService.find_show`. A title the last check made from
  TMDB's details (`titles`, by key; it has an IMDb or TheTVDB id) is reused without asking
  again; one made without them is asked about again at every check until TMDB gives them. When
  TMDB can't give the details, a known id still counts, with the name's title.
- **Every file read** ends up in a title or in `unrecognised` (or, TMDB unreachable, neither, as
  below): a file that is neither a film nor an episode with its season and number is listed.
- **Episodes** enter the scan only for shows in the shows cache, as Plex's do (the others are
  counted in `unmatched_shows`; `PlexRepository.replace` would drop them anyway).
- **TMDB unreachable** for a search: those files get no row and aren't called unrecognised, so
  the next check asks again.

Logs give counts only, never a path, a file name or a title.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from app.models import MediaType
from app.models.domain import DomainModel
from app.models.folder_library import LibraryFile
from app.models.plex import LibraryKind, PlexScan, PlexTitle
from app.models.tmdb import TMDBTitle, TMDBTitleIds
from app.services.folder_library.scan import FoundFile
from app.services.library import LibraryService
from app.services.plex import FOLDER_KEY_PREFIX
from app.services.tmdb import TMDBClient, TMDBError

log = logging.getLogger(__name__)

KEY_PREFIX = FOLDER_KEY_PREFIX  # a folder title's key: never one of Plex's (ratingKeys are numbers)
_RANK = {"SD": 1, "720p": 2, "1080p": 3, "4K": 4}  # the best copy's quality is the title's
_MOST_EPISODES = 50  # "S01E01-E99" read wrongly doesn't fill a season

_SearchKey = tuple[LibraryKind, str, int | None]  # kind, folded title, year
_TitleKey = tuple[LibraryKind, int]  # kind, TMDB id


class FolderMatch(DomainModel):
    """One check's matching: the scan to store (`PlexRepository.replace`), each file's row to
    store (`LibraryFileRepository.replace_folder`, per folder) and the files not recognised
    (paths, sorted; never logged)."""

    scan: PlexScan
    rows: tuple[LibraryFile, ...]
    unrecognised: tuple[str, ...]


def title_key(kind: LibraryKind, tmdb_id: int) -> str:
    """A folder title's key in `plex_titles`: "folder:movie:438631", "folder:show:95396"."""
    return f"{KEY_PREFIX}{kind}:{tmdb_id}"


def _library_kind(file: FoundFile) -> LibraryKind | None:
    if file.kind == "movie":
        return "movie"
    return "show" if file.kind == "episode" else None


def _media(kind: LibraryKind) -> MediaType:
    return MediaType.MOVIE if kind == "movie" else MediaType.TV


def _search_key(kind: LibraryKind, file: FoundFile) -> _SearchKey:
    return kind, " ".join(file.title.casefold().split()), file.year


def _unchanged(row: LibraryFile | None, file: FoundFile) -> LibraryFile | None:
    if row is None or (row.size, row.mtime) != (file.size, file.mtime):
        return None
    return row


async def _search(
    tmdb: TMDBClient, kind: LibraryKind, file: FoundFile
) -> tuple[bool, TMDBTitle | None]:
    """(asked, TMDB's best hit); asked False when TMDB couldn't be asked."""
    try:
        found = await tmdb.find_titles(_media(kind), file.title, year=file.year)
    except TMDBError:
        return False, None
    return True, found[0] if found else None


async def _details(tmdb: TMDBClient, kind: LibraryKind, tmdb_id: int) -> TMDBTitleIds | None:
    try:
        return await tmdb.title_ids(_media(kind), tmdb_id)
    except TMDBError:
        return None


def _episodes(files: Sequence[FoundFile]) -> set[tuple[int, int]]:
    """Every (season, episode) the files hold ("S01E02-E03" holds two)."""
    found: set[tuple[int, int]] = set()
    for file in files:
        if file.season is None or file.episode is None or file.season < 0 or file.episode < 0:
            continue
        last = file.episode_end if file.episode_end is not None else file.episode
        if not file.episode <= last < file.episode + _MOST_EPISODES:
            last = file.episode
        found.update((file.season, number) for number in range(file.episode, last + 1))
    return found


def _resolution(files: Sequence[FoundFile]) -> str | None:
    known = [file.quality for file in files if file.quality in _RANK]
    return max(known, key=lambda quality: _RANK[quality or ""]) if known else None


def _placeable(file: FoundFile) -> bool:
    """A file that can join a title: a film, or an episode with its season and number. Every
    other file read is listed as not recognised (none just disappears)."""
    if file.kind == "movie":
        return True
    return file.kind == "episode" and file.season is not None and file.episode is not None


def _complete(title: PlexTitle | None) -> bool:
    """A last check's title made from TMDB's details (they gave it its IMDb / TheTVDB id): reused
    as it is. One made without them (TMDB offline, no key) is asked about again."""
    return title is not None and bool(title.imdb_id or title.tvdb_id)


def _fallback_name(files: Sequence[FoundFile]) -> tuple[str, int | None]:
    title = next((file.title for file in files if file.title.strip()), "")
    year = next((file.year for file in files if file.year is not None), None)
    return title, year


async def match_files(
    files: Sequence[FoundFile],
    *,
    tmdb: TMDBClient,
    library: LibraryService,
    cached: Mapping[str, LibraryFile],
    titles: Mapping[str, PlexTitle] | None = None,
) -> FolderMatch:
    """Match every file read (all folders') to its TMDB title: the scan, the rows to store and
    the files not recognised. `cached`: the stored rows by path; `titles`: the last check's
    folder titles by key (`PlexRepository.titles`), reused without asking TMDB / TVMaze."""
    known_titles = titles or {}
    ids: dict[str, int | None] = {}  # path → TMDB id (None: not recognised); absent: not asked
    by_key: dict[_SearchKey, int] = {}  # what this check already knows by title and year
    pending: dict[_SearchKey, list[FoundFile]] = {}
    for file in files:
        kind = _library_kind(file)
        row = _unchanged(cached.get(file.path), file)
        if not _placeable(file):
            ids[file.path] = None  # not a film, nor an episode with its number: listed
        elif file.tmdb_id is not None:
            ids[file.path] = file.tmdb_id
        elif row is not None:
            ids[file.path] = row.tmdb_id if row.recognised else None
        elif kind is None or not file.title.strip():
            ids[file.path] = None
        else:
            pending.setdefault(_search_key(kind, file), []).append(file)
            continue
        found = ids[file.path]
        if found is not None and kind is not None and file.title.strip():
            by_key.setdefault(_search_key(kind, file), found)

    hits: dict[_TitleKey, TMDBTitle] = {}
    to_search = {key: group for key, group in pending.items() if key not in by_key}
    for key, group in pending.items():
        if key in by_key:
            ids.update((file.path, by_key[key]) for file in group)
    answers = await asyncio.gather(
        *(_search(tmdb, key[0], group[0]) for key, group in to_search.items())
    )
    failed = 0
    for (key, group), (asked, hit) in zip(to_search.items(), answers, strict=True):
        if not asked:
            failed += len(group)  # no row: asked again at the next check
            continue
        if hit is not None:
            hits.setdefault((key[0], hit.id), hit)
        ids.update((file.path, hit.id if hit is not None else None) for file in group)

    grouped: dict[_TitleKey, list[FoundFile]] = defaultdict(list)
    for file in files:
        kind = _library_kind(file)
        tmdb_id = ids.get(file.path)
        if kind is not None and tmdb_id is not None:
            grouped[(kind, tmdb_id)].append(file)

    new = [key for key in grouped if not _complete(known_titles.get(title_key(*key)))]
    details = dict(
        zip(new, await asyncio.gather(*(_details(tmdb, *key) for key in new)), strict=True)
    )
    made: list[PlexTitle] = []
    for (kind, tmdb_id), group in grouped.items():
        prior = known_titles.get(title_key(kind, tmdb_id))
        about = details.get((kind, tmdb_id))
        if prior is not None and (_complete(prior) or about is None):
            base = prior  # complete, or TMDB still gave no details: as the last check made it
        else:
            hit = hits.get((kind, tmdb_id))
            name, year = _fallback_name(group)
            source: TMDBTitle | None = about if about is not None else hit
            base = PlexTitle(
                kind=kind,
                key=title_key(kind, tmdb_id),
                title=source.title if source is not None and source.title else name,
                year=source.year if source is not None and source.year else year,
                thumb=source.poster_path if source is not None else None,
                imdb_id=about.imdb if about is not None else None,
                tvdb_id=about.tvdb if about is not None and kind == "show" else None,
                tmdb_id=tmdb_id,
            )
        show_id = base.show_id
        if kind == "show" and show_id is None:
            show = await library.find_show(
                tvdb_id=base.tvdb_id, imdb_id=base.imdb_id, name=base.title
            )
            show_id = show.id if show is not None else None
        held = _episodes(group) if kind == "show" else set()
        made.append(
            base.model_copy(
                update={
                    "added_at": datetime.fromtimestamp(min(file.mtime for file in group), UTC),
                    "resolution": _resolution(group),
                    "seasons": len({season for season, _ in held}) or None,
                    "episodes": len(held) or None,
                    "show_id": show_id,
                }
            )
        )
    made.sort(key=lambda title: (title.kind, title.title.casefold(), title.key))

    shows = [title for title in made if title.kind == "show"]
    in_cache = set(
        await library.cached_shows(title.show_id for title in shows if title.show_id is not None)
    )
    episodes: set[tuple[int, int, int]] = set()
    for title in shows:
        if title.show_id in in_cache and title.tmdb_id is not None:
            group = grouped[("show", title.tmdb_id)]
            episodes.update((title.show_id, season, number) for season, number in _episodes(group))
    films = frozenset((title.tmdb_id, title.imdb_id) for title in made if title.kind == "movie")

    rows = tuple(
        LibraryFile(
            path=file.path,
            size=file.size,
            mtime=file.mtime,
            kind=file.kind,
            tmdb_id=ids[file.path],
            season=file.season,
            episode=file.episode,
            episode_end=file.episode_end,
            quality=file.quality,
            recognised=ids[file.path] is not None,
        )
        for file in files
        if file.path in ids
    )
    unrecognised = tuple(sorted(path for path, tmdb_id in ids.items() if tmdb_id is None))
    by_path = {file.path: file for file in files}
    scan = PlexScan(
        episodes=frozenset(episodes),
        films=films,
        shows=len(shows),
        unmatched_shows=sum(1 for title in shows if title.show_id not in in_cache),
        unmatched_films=sum(1 for path in unrecognised if by_path[path].kind == "movie"),
        titles=tuple(made),
    )
    log.info(
        "Matched the folder library: %d titles, %d files not recognised, %d TMDB searches, "
        "%d details",
        len(made), len(unrecognised), len(to_search), len(new),
    )
    if failed:
        log.info("TMDB couldn't be asked about %d files: asked again at the next check", failed)
    return FolderMatch(scan=scan, rows=rows, unrecognised=unrecognised)
