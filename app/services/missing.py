"""Missing episodes of tracked shows, and "Download missing".

Only a show's latest season counts: the highest season with at least one aired regular,
numbered episode (a new season whose first episode hasn't aired yet leaves the previous one the
latest). Its missing episodes are the aired regular numbered ones that are neither downloaded
(the episode or its season pack in the download history) nor marked "I already have this".
"Aired" is `Episode.has_aired`, the rule of the show details window: the airstamp has passed,
or without one the air date is today or earlier; an episode with neither never counts. Specials
(no number, or not of type "regular") and season 0 never count.

An aired episode only counts as missing once MISSING_AFTER (a day) has passed since it aired
(`is_overdue`: its airstamp + MISSING_AFTER; without an airstamp, from the day after its air
date): a copy usually turns up hours after the broadcast. Until then it is "new" (`is_new`):
not counted, not fetched by "Download missing", a blue "New" chip instead of the amber one. The
latest season still follows `has_aired`: a new season's first episode makes it the latest at
once. (The automatic downloads keep their own 2-hour rule, app/services/auto_download.py.)

`missing_episodes` is the rule itself; LibraryRepository.missing_counts / missing_between are
its SQL twins (one query for every tracked show), handed `missing_cutoffs`.

"Download missing" (`MissingDownloadService.download_missing`) fetches a show's missing episodes
one at a time, oldest first, at most MAX_PER_RUN, through the one-click Download
(`QuickDownloadService`, which picks with `pick_best`). Nothing found: on to the next. A failed
grab (no torrent program, unwritable folder, the remote program refusing) ends the run, as it
ends an automatic-download run. One run per show at a time.

Log hygiene: only episode labels and outcomes are logged, never a release or a URL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from app.models import (
    CalendarEntry,
    Episode,
    GrabTarget,
    MediaType,
    MissingProgress,
    MissingRunReport,
)
from app.services.downloads import GrabError
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError
from app.services.search import episode_request

if TYPE_CHECKING:
    from app.services.library import LibraryService

log = logging.getLogger(__name__)

MAX_PER_RUN = 25  # episodes one "Download missing" run fetches at most
REGULAR = "regular"
# An aired episode counts as missing this long after it aired (before that it is "new").
MISSING_AFTER = timedelta(hours=24)
_DAY = timedelta(days=1)

MissingProgressListener = Callable[[MissingProgress], None]


class MissingRunBusy(Exception):
    """A "Download missing" run for this show is already going."""


def counts_as_missing_candidate(episode: Episode) -> bool:
    """A regular, numbered episode of a real season (not a special)."""
    return episode.season > 0 and episode.number is not None and episode.type == REGULAR


def latest_season(episodes: Iterable[Episode], now: datetime) -> int | None:
    """The highest season with an aired regular numbered episode; None when nothing aired.
    `now` is aware; its time zone decides which day "today" is for episodes without an
    airstamp."""
    aired = [
        episode.season
        for episode in episodes
        if counts_as_missing_candidate(episode) and episode.has_aired(now)
    ]
    return max(aired, default=None)


def missing_cutoffs(now: datetime) -> tuple[datetime, date]:
    """The latest airstamp, and for an episode without one the latest air date, that counts
    as missing at `now` (aware; its time zone decides which day "today" is): an airstamp at
    least MISSING_AFTER ago, an air date at least MISSING_AFTER's whole days (rounded up)
    before today. The SQL twins take these two."""
    days = -(-MISSING_AFTER // _DAY)  # a day, rounded up to whole days
    return now - MISSING_AFTER, now.date() - timedelta(days=days)


def is_overdue(episode: Episode, now: datetime) -> bool:
    """Aired long enough ago to count as missing (see MISSING_AFTER)."""
    by, day = missing_cutoffs(now)
    if episode.airstamp is not None:
        return episode.airstamp <= by
    return episode.air_date is not None and episode.air_date <= day


def is_new(episode: Episode, now: datetime) -> bool:
    """Aired, but not long enough ago to count as missing."""
    return episode.has_aired(now) and not is_overdue(episode, now)


def _oldest_first(episode: Episode) -> tuple[date, int]:
    return episode.air_date or date.max, episode.number or 0


def missing_episodes(
    episodes: Iterable[Episode],
    *,
    downloaded: Collection[int],
    have: Collection[int],
    now: datetime,
) -> list[Episode]:
    """The latest season's regular numbered episodes that aired at least MISSING_AFTER ago
    (`is_overdue`) and whose id is in neither `downloaded` nor `have` (marked "I already have
    this"), oldest first."""
    episodes = list(episodes)
    season = latest_season(episodes, now)
    if season is None:
        return []
    return sorted(
        (
            episode
            for episode in episodes
            if episode.season == season
            and counts_as_missing_candidate(episode)
            and is_overdue(episode, now)
            and episode.id not in downloaded
            and episode.id not in have
        ),
        key=_oldest_first,
    )


def new_episodes(
    episodes: Iterable[Episode],
    *,
    downloaded: Collection[int],
    have: Collection[int],
    now: datetime,
) -> list[Episode]:
    """The latest season's regular numbered episodes that aired, but less than MISSING_AFTER
    ago (`is_new`), and whose id is in neither `downloaded` nor `have`, oldest first: the
    ones that become missing once their day has passed."""
    episodes = list(episodes)
    season = latest_season(episodes, now)
    if season is None:
        return []
    return sorted(
        (
            episode
            for episode in episodes
            if episode.season == season
            and counts_as_missing_candidate(episode)
            and is_new(episode, now)
            and episode.id not in downloaded
            and episode.id not in have
        ),
        key=_oldest_first,
    )


def episode_label(entry: CalendarEntry) -> str:
    """"Severance S02E04"."""
    return f"{entry.show.name} {entry.episode.code}"


class MissingDownloadService:
    """Runs "Download missing" for a tracked show (`services.missing`)."""

    def __init__(self, library: LibraryService, quick_download: QuickDownloadService) -> None:
        self._library = library
        self._quick_download = quick_download
        self._running: set[int] = set()  # show ids with a run going

    def is_running(self, show_id: int) -> bool:
        return show_id in self._running

    async def download_missing(
        self,
        show_id: int,
        progress: MissingProgressListener | None = None,
        *,
        limit: int = MAX_PER_RUN,
    ) -> MissingRunReport:
        """Fetch the show's missing episodes (see the module docstring), oldest first, at most
        `limit`. `progress` hears of each episode as it starts and as it ends.

        Raises MissingRunBusy while a run for the show is going. Unexpected errors propagate.
        """
        if show_id in self._running:
            raise MissingRunBusy(show_id)
        self._running.add(show_id)
        try:
            return await self._run(show_id, progress, limit)
        finally:
            self._running.discard(show_id)

    async def _run(
        self, show_id: int, progress: MissingProgressListener | None, limit: int
    ) -> MissingRunReport:
        missing = await self._library.missing_episodes(show_id)
        todo = missing[: max(0, limit)]
        total = len(todo)
        downloaded: list[str] = []
        already: list[str] = []
        not_found: list[str] = []
        error: str | None = None

        def tell(step: MissingProgress) -> None:
            if progress is not None:
                try:
                    progress(step)
                except Exception:
                    log.exception("Download-missing progress listener failed")

        for index, entry in enumerate(todo, start=1):
            show, episode = entry.show, entry.episode
            if episode.number is None:  # never missing (specials don't count); keeps mypy sure
                continue
            label = episode_label(entry)
            step = MissingProgress(show_id=show_id, index=index, total=total, episode=episode)
            tell(step)
            request = episode_request(show, episode.season, episode.number)
            target = GrabTarget(
                media_type=MediaType.TV,
                media_id=show.id,
                season=episode.season,
                episode=episode.number,
            )
            log.info("Download missing %s: searching (%d of %d)", label, index, total)
            try:
                result = await self._quick_download.download(request, target)
            except (GrabError, RemoteError) as exc:
                error = str(exc)  # user-facing text, free of URLs and keys
                log.warning("Download missing %s: the grab failed, stopping: %s", label, error)
                tell(step.model_copy(update={"failed": True}))
                break
            tell(step.model_copy(update={"result": result}))
            if result.already_downloaded:
                log.info("Download missing %s: already downloaded", label)
                already.append(label)
            elif result.grab is None:
                log.info("Download missing %s: nothing suitable yet", label)
                not_found.append(label)
            else:
                log.info("Download missing %s: downloaded (%s)", label, result.quality or "?")
                downloaded.append(label)

        report = MissingRunReport(
            show_id=show_id,
            total=total,
            downloaded=tuple(downloaded),
            already=tuple(already),
            not_found=tuple(not_found),
            left_over=len(missing) - total,
            error=error,
        )
        log.info(
            "Download missing for TVMaze show %d: %d to fetch, %d downloaded, %d already, "
            "%d not found, %d left over%s",
            show_id,
            report.total,
            len(report.downloaded),
            len(report.already),
            len(report.not_found),
            report.left_over,
            ", stopped by a failed grab" if error is not None else "",
        )
        return report
