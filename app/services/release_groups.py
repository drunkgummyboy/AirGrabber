"""Release groups AirGrabber learns from the download history, and the ones the user blocks.

Learned (`learned_groups`): a group whose grabs the user kept. A grab is kept when nothing
fetched the same episode, season pack or film again later (a second grab means the first was
wrong, fake or stuck). A group with TRUST_MIN kept grabs, and at most one grab fetched again per
TRUST_PER_REDO kept ones, is trusted: the picker ranks it right after the user's own preferred
groups (`picking.group_rank`). A blocked group is never learned.

Blocked (`block_group`): "Wrong or fake?" in a download's ⋯ menu. The group goes on
`blocked_groups` (Settings › Advanced), off the preferred and learned ones, and its releases are
never shown or picked again (`search.passes_settings`).

Nothing here logs a release title.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from app.models import DownloadRecord, MediaType
from app.services.search import release_group
from app.services.settings import SettingsService

TRUST_MIN = 3  # kept grabs before a group is trusted
TRUST_PER_REDO = 3  # kept grabs a group needs for every one fetched again

_Item = tuple[MediaType, int, int | None, int | None]


def _item(record: DownloadRecord) -> _Item:
    """What a grab fetched: a film, an episode or a season pack (episode None)."""
    return record.media_type, record.media_id, record.season, record.episode


def learned_groups(grabs: Iterable[DownloadRecord], blocked: Iterable[str] = ()) -> tuple[str, ...]:
    """The trusted groups of `grabs` (any order), in alphabetical order, each spelled as its
    newest grab has it; never a `blocked` one (ignoring case)."""
    newest_first = sorted(grabs, key=lambda record: record.downloaded_at, reverse=True)
    fetched_later: set[_Item] = set()
    kept: Counter[str] = Counter()
    redone: Counter[str] = Counter()
    names: dict[str, str] = {}
    for record in newest_first:
        item = _item(record)
        group = release_group(record.release_title)
        if group is not None:
            key = group.casefold()
            names.setdefault(key, group)
            if item in fetched_later:
                redone[key] += 1
            else:
                kept[key] += 1
        fetched_later.add(item)
    skipped = {name.casefold() for name in blocked}
    trusted = [
        names[key]
        for key, count in kept.items()
        if count >= TRUST_MIN and count >= TRUST_PER_REDO * redone[key] and key not in skipped
    ]
    return tuple(sorted(trusted, key=str.casefold))


def _without(names: Iterable[str], group: str) -> tuple[str, ...]:
    return tuple(name for name in names if name.casefold() != group.casefold())


async def block_group(settings: SettingsService, group: str) -> None:
    """Never show or pick `group`'s releases again: on `blocked_groups` (once), off the preferred
    and learned groups."""
    current = settings.current
    blocked = current.blocked_groups
    if group.casefold() not in {name.casefold() for name in blocked}:
        blocked = (*blocked, group)
    await settings.update(
        blocked_groups=blocked,
        preferred_groups=_without(current.preferred_groups, group),
        learned_groups=_without(current.learned_groups, group),
    )
