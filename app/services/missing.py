"""Missing episodes of tracked shows: the rules. They apply only while Plex is connected
(app/services/plex.py; owner's decision for 3.1.0): without Plex AirGrabber shows nothing of what
you have or miss, and nothing is missing.

Only a show's latest season counts: the highest season with at least one aired regular,
numbered episode (a new season whose first episode hasn't aired yet leaves the previous one the
latest). Its missing episodes are the aired regular numbered ones that are neither in Plex nor
downloaded by AirGrabber (the episode or its season pack in the download history: a grabbed
episode Plex doesn't have yet is on its way, neither had nor missing).
"Aired" is `Episode.has_aired`, the rule of the show details window: the airstamp has passed,
or without one the air date is today or earlier; an episode with neither never counts. Specials
(no number, or not of type "regular") and season 0 never count.

An aired episode only counts as missing once MISSING_AFTER (a day) has passed since it aired
(`is_overdue`: its airstamp + MISSING_AFTER; without an airstamp, from the day after its air
date): a release usually turns up hours after the broadcast. Until then it is "new" (`is_new`):
not counted, not fetched by "Download missing", a blue "New" chip instead of the amber one. The
latest season still follows `has_aired`: a new season's first episode makes it the latest at
once. (The automatic downloads keep their own 2-hour rule, app/services/auto_download.py.)

`missing_episodes` is the rule itself; LibraryRepository.missing_counts / missing_between are
its SQL twins (one query for every tracked show), handed `missing_cutoffs`.

"Download missing" (My shows, the show window) queues a show's missing episodes, oldest first,
at most MAX_PER_RUN, in the app's download queue (app/services/download_queue.py, through
app/ui/queue_link.py).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import date, datetime, timedelta

from app.models import Episode

MAX_PER_RUN = 25  # episodes one "Download missing" run fetches at most
REGULAR = "regular"
# An aired episode counts as missing this long after it aired (before that it is "new").
MISSING_AFTER = timedelta(hours=24)
_DAY = timedelta(days=1)

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
    in_plex: Collection[int],
    now: datetime,
) -> list[Episode]:
    """The latest season's regular numbered episodes that aired at least MISSING_AFTER ago
    (`is_overdue`) and whose id is in neither `downloaded` nor `in_plex`, oldest first."""
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
            and episode.id not in in_plex
        ),
        key=_oldest_first,
    )


def new_episodes(
    episodes: Iterable[Episode],
    *,
    downloaded: Collection[int],
    in_plex: Collection[int],
    now: datetime,
) -> list[Episode]:
    """The latest season's regular numbered episodes that aired, but less than MISSING_AFTER
    ago (`is_new`), and whose id is in neither `downloaded` nor `in_plex`, oldest first: the
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
            and episode.id not in in_plex
        ),
        key=_oldest_first,
    )
