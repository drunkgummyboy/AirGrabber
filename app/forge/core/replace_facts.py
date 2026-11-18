"""What the owner weighs before "Use this one instead" (a download held as already in the library,
`auto.held_in_library`): the copy in the library and this download, each by its picture quality
(2160p, 1080p, …) and size (`ReplaceFacts`). The queue's band shows them after its question
("In your library: 1080p · 4.2 GB — this download: 2160p · 18.6 GB", `facts_text`); the phone's
MediaForge tab may ask the same (`replace_facts`).

The library copy is the file `auto.library_copies` finds under the name the download would get.
Its size and the download's are read in a worker thread under READ_TIMEOUT_S (a NAS asleep: the
sizes are left out, never a window that waits). A thread past the limit can't be stopped: until it
ends, no new read starts on that drive or share (`busy`, keyed by `place_of`, as auto.py's
`_busy` per watched folder), so a NAS that never answers costs one blocked thread at most. The download's quality is what its name said when it
was parsed (`item.parsed.quality`); the library copy's name is parsed once (app/forge/core/parser.py,
on the loop: guessit is CPU work, and in a thread it would fight the window for the GIL). What
isn't known is left out. Logs never name a path or a file.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.forge.core import fsops
from app.forge.core.auto import library_copies
from app.forge.core.parser import parse_filename, parser_ready
from app.forge.core.queue import QueueItem
from app.i18n import N_, number, t

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 8.0  # reading both sizes at most; past it they are left out
_GB, _MB = 1024**3, 1024**2
# Reads past the limit whose thread still goes, by the drive or share of the library copy
# (`place_of`): one hung NAS, one thread.
_BUSY: dict[str, asyncio.Future[Any]] = {}
_SCREEN = re.compile(r"\b(\d{3,4}[pi]|[48]K)\b", re.IGNORECASE)

FACTS_BOTH = N_("In your library: {library} — this download: {download}")
FACTS_LIBRARY = N_("In your library: {library}")
FACTS_DOWNLOAD = N_("This download: {download}")


@dataclass(frozen=True, slots=True)
class ReplaceFacts:
    """The library's copy and this download, as far as known (None: not known). The library
    copy's file name is for the phone's own use: shown, never logged."""

    library_name: str | None = None
    library_size: int | None = None
    library_quality: str | None = None
    download_size: int | None = None
    download_quality: str | None = None


def screen_quality(quality: str | None) -> str | None:
    """The picture's height of a parsed quality ("2160p" of "2160p H.265"), else None."""
    found = _SCREEN.search(quality or "")
    if found is None:
        return None
    text = found.group(1)
    return text.upper() if text[-1] in "kK" else text.lower()


def name_quality(name: str) -> str | None:
    """The quality a file name says ("1080p"), read with the parser; None without guessit or
    when the name says none."""
    if not parser_ready():
        return None
    try:
        return screen_quality(parse_filename(name).quality)
    except Exception as exc:  # noqa: BLE001 - a name guessit chokes on just has no quality
        log.info("Couldn't read a quality from a file name (%s)", type(exc).__name__)
        return None


def _size(path: Path) -> int | None:
    try:
        return os.stat(fsops.long_path(path)).st_size
    except OSError:
        return None


def read_sizes(
    download: Path,
    target: Path,
    *,
    exists: Callable[[Path], bool] = fsops.exists,
    size_of: Callable[[Path], int | None] = _size,
) -> ReplaceFacts:
    """The disk part (blocking: a worker thread): the library's copy under `target`'s name, its
    size and the download's. No qualities yet."""
    copies = library_copies(target, exists)
    library = copies[0] if copies else None
    return ReplaceFacts(
        library_name=library.name if library is not None else None,
        library_size=size_of(library) if library is not None else None,
        download_size=size_of(download),
    )


def place_of(target: Path) -> str:
    """Where a library copy sits, as the guard knows it: its drive or share (normcased); a
    relative name, its folder."""
    return os.path.normcase(target.anchor or str(target.parent))


def _retrieve(step: asyncio.Future[Any]) -> None:
    """An abandoned read's end: its error (if any) is read, so nothing logs it as never read."""
    if not step.cancelled():
        step.exception()


async def replace_facts(
    item: QueueItem,
    target: Path,
    *,
    timeout_s: float = READ_TIMEOUT_S,
    read: Callable[[Path, Path], ReplaceFacts] = read_sizes,
    quality_of: Callable[[str], str | None] = name_quality,
    busy: dict[str, asyncio.Future[Any]] | None = None,
) -> ReplaceFacts:
    """The facts of a download held as already in the library, `target` being the name it would
    get there. The sizes come through a thread under `timeout_s`; past it, on a disk error, or
    while an earlier read on that drive or share still hangs (`busy`, the module's own by
    default), they are left out."""
    busy = _BUSY if busy is None else busy
    key = place_of(target)
    facts = ReplaceFacts()
    earlier = busy.get(key)
    if earlier is not None and earlier.done():
        del busy[key]
        earlier = None
    if earlier is not None:
        log.info("An earlier size read on that drive or share still hangs; sizes left out")
    else:
        step = asyncio.ensure_future(asyncio.to_thread(read, item.path, target))
        try:
            facts = await asyncio.wait_for(asyncio.shield(step), timeout_s)
        except (TimeoutError, OSError) as exc:
            log.info("Couldn't read the sizes for a replace in time (%s)", type(exc).__name__)
        finally:
            if not step.done():  # past the limit (or this read was cancelled): it goes on alone
                busy[key] = step
                step.add_done_callback(_retrieve)
    library_quality = quality_of(facts.library_name) if facts.library_name else None
    return dataclasses.replace(
        facts,
        library_quality=library_quality,
        download_quality=screen_quality(item.parsed.quality),
    )


def size_text(size: int) -> str:
    """"4.2 GB", "18.6 GB", "700 MB" (Dutch separators while Dutch is on)."""
    if size >= _GB:
        return f"{number(size / _GB, '.1f')} GB"
    return f"{number(max(1, round(size / _MB)))} MB"


def _side(quality: str | None, size: int | None) -> str:
    return " · ".join(part for part in (quality, size_text(size) if size else None) if part)


def facts_text(facts: ReplaceFacts) -> str:
    """"In your library: 1080p · 4.2 GB — this download: 2160p · 18.6 GB", translated; a side
    that isn't known is left out, and "" when neither is."""
    library = _side(facts.library_quality, facts.library_size)
    download = _side(facts.download_quality, facts.download_size)
    if library and download:
        return t(FACTS_BOTH, library=library, download=download)
    if library:
        return t(FACTS_LIBRARY, library=library)
    if download:
        return t(FACTS_DOWNLOAD, download=download)
    return ""
