"""The Forge on the phone: `GET /v1/forge`, `GET /v1/forge/{id}/search`,
`POST /v1/forge/{id}/choose`, `POST /v1/forge/{id}/replace` and `POST /v1/forge/{id}/keep`, for a
library manager's phone only (`manager_only`).

Only watched downloads are shown: those the Forge found in the watched folders and that always go
into the library. A file that needs a match, or was held because its match is missing or wrong
(`QueueItem.match_problem`), can be sorted out here (`waiting`); one with another problem is
listed with its reason, to be sorted out on the PC (`elsewhere`). So is an episode with no
episode number: it would get "E00" for a name, which the PC's preview shows and a tap doesn't.

A tap is the owner's choice and Rename in one, in Trial and On alike: `Queue.choose_match`, then
`AutoRenamer.rename`, recorded as the owner's own Rename (`RunSource.APP`: an older version gone
back to reads the same History, and an unknown source would stop it). So it stays in the PC's
History and can be undone there. It also moves the other files `waiting` of that show
(`same_show_count` counts them with the file itself, so it says beforehand how many will move). The
choice reaches hand-dropped files of that title as on the PC, but they aren't moved. Files of that
show held for another reason (one the owner undid was put back on purpose) keep their place and
their reason. A taken name never becomes a numbered copy here, as no preview shows it: the file
is held (`rename(hold_taken=True)`) and listed in `elsewhere`. Of files wanting one name, the
tapped one goes and the others wait; if none of them was tapped, they all wait: nobody chose.

A download held because the film or episode is in the library already (`held_in_library`) is
listed in `elsewhere` with what the PC's band says too (`can_replace`, `kind`, its quality and size,
and the library copy's file name, quality and size: app/forge/core/replace_facts.py). The sizes are
read in the background (a thread under a time limit, one per drive or share at a time): a request
never waits for a NAS, and what isn't read yet is null. "Use this one instead" (`replace`) is the
PC's own: `AutoRenamer.replace` as the owner's run (`RunSource.APP`), the copy there moved aside to
Duplicates, Undo in the PC's History. It runs only where the PC could without asking: a library
folder at a drive or share root needs the place chosen on the PC this session (`aside_place`).
"Keep the one I have" (`keep`) is the PC's "Leave this file alone": `services.ignored`, and the
file leaves the queue.

A file is known by its queue id and `path_key` together: the queue lives in memory, so after a
restart an id can mean another file, and the key then differs ("no longer there", nothing moved).
A key is a hash: it says which file without saying where. One tap at a time, and everything is
looked at again once the wait is over: a second tap, or a run that was going, may have sorted the
file meanwhile. A tap can wait a long time for a running run (`ForgeServices.run_lock`; the phone
allows 120 s, and what it started carries on if the phone gives up).

Forge stays off AirGrabber's start path, so its modules are imported where they are used. The log
gets counts only, never a file's name or path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.i18n import N_, t, tn
from app.models.domain import TMDB_IMAGE_BASE
from app.services.phone.coming_up import person_gone
from app.services.phone.server import (
    Handler,
    Reply,
    Request,
    bad_request,
    error_reply,
    manager_only,
)
from app.services.profiles import ProfileService

if TYPE_CHECKING:  # Forge's types: only typed here
    from app.forge.core.queue import Queue, QueueItem
    from app.forge.core.auto import NamingChoice
    from app.forge.core.replace_facts import ReplaceFacts
    from app.forge.models.media import MediaKind
    from app.forge.runtime import ForgeRuntime
    from app.forge.services import ForgeServices
    from app.models.tmdb import TMDBTitle

log = logging.getLogger(__name__)

MAX_QUERY = 200  # characters of a typed title
MAX_RESULTS = 8  # what a search offers
_POSTER_SIZE = "w342"
_ITEM_ID = re.compile(r"[1-9][0-9]{0,17}")
# The answers of "Use this one instead" and "Keep the one I have" (English on the Companion).
REPLACED = N_(
    "Moved the old copy to the Duplicates folder and put this one in its place. Undo on the PC "
    "puts both back."
)
REPLACED_TO_PLACE = N_(
    "Moved the old copy to the folder chosen on the PC and put this one in its place. Undo on "
    "the PC puts both back."
)
REPLACED_NOTHING = N_("Put this one in the library; there was no old copy to move.")
NOT_REPLACED = N_("Couldn't put this one in its place, so sort it out on the PC.")
NO_ASIDE = N_(
    "Your library folder is the top of a drive or share, so choose on the PC where replaced "
    "copies go first."
)
KEPT = N_("Left this download alone. The one in your library stays.")
NOT_KEPT = N_("Couldn't leave this download alone, so sort it out on the PC.")


def path_key(path: Path) -> str:
    """Which file this is, the way Windows compares paths, without saying where it is."""
    key = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(os.fsencode(key)).hexdigest()[:32]


def _starting() -> Reply:
    return error_reply(
        503, "forge_starting", t("The Forge is still starting, so try again in a moment.")
    )


def _gone() -> Reply:
    return error_reply(410, "no_longer_there", t("Already sorted, or AirGrabber restarted."))


def _tmdb_away() -> Reply:
    return error_reply(
        502, "failed", t("TMDB can't be reached just now, so try again in a moment.")
    )


def _item_id(request: Request) -> int | None:
    text = request.query.get("id", "")
    return int(text) if _ITEM_ID.fullmatch(text) else None


def _numbered(item: QueueItem) -> bool:
    from app.forge.models.media import MediaKind

    parsed = item.parsed
    if parsed.kind is not MediaKind.EPISODE:
        return True
    return parsed.season is not None and parsed.episode is not None


def _waiting(item: QueueItem) -> bool:
    """A watched download the phone can sort out."""
    from app.forge.core.queue import ItemStatus

    if item.watched is None or not _numbered(item):
        return False
    return item.status is ItemStatus.NEEDS_CHOICE or (
        item.status is ItemStatus.HELD and item.match_problem
    )


def _elsewhere(item: QueueItem) -> bool:
    """A watched download to sort out on the PC."""
    from app.forge.core.queue import ItemStatus

    if item.watched is None or _waiting(item):
        return False
    return item.status in (ItemStatus.HELD, ItemStatus.ERROR, ItemStatus.NEEDS_CHOICE)


def _reason(item: QueueItem) -> str:
    if not _numbered(item):
        return t("No season or episode number in the name.")
    return item.message or t("Sort this out on the PC.")


def _waiting_item(services: ForgeServices, item_id: int) -> QueueItem | None:
    item = services.queue.get(item_id)
    return item if item is not None and _waiting(item) else None


def _in_library_item(services: ForgeServices, item_id: int, key: str) -> QueueItem | None:
    """The watched download `item_id` with `path_key` `key`, held as already in the library."""
    from app.forge.core.auto import held_in_library

    item = services.queue.get(item_id)
    if item is None or item.watched is None or not held_in_library(item):
        return None
    return item if path_key(item.path) == key else None


def _can_replace(services: ForgeServices, item: QueueItem) -> bool:
    """Whether a replace from the phone can run (no disk asked): a library folder set, and, at a
    drive or share root, the place for replaced copies chosen on the PC this session."""
    from app.forge.core.duplicates import aside_folder
    from app.forge.core.library import library_folder, library_problems

    settings = services.settings.current
    library = library_folder(item.media.kind, settings)
    if library is None or library_problems(settings):
        return False
    return aside_folder(library, services.aside_place) is not None


def _band_naming(services: ForgeServices, item: QueueItem) -> NamingChoice | None:
    """How the PC's queue band names this download (its chip), if the page has said; else the
    library's preset goes (None)."""
    return services.band_naming.get(item.id)


def _replace_entry(item: QueueItem, facts: ReplaceFacts | None, can: bool) -> dict[str, Any]:
    """What `elsewhere` adds for a download already in the library: no path, names only."""
    from app.forge.core.replace_facts import screen_quality

    return {
        "id": item.id,
        "path_key": path_key(item.path),
        "can_replace": can,
        "kind": item.media.kind.value,
        "size_bytes": facts.download_size if facts is not None else None,
        "quality": screen_quality(item.parsed.quality),
        "replaces": {
            "file_name": facts.library_name if facts is not None else None,
            "size_bytes": facts.library_size if facts is not None else None,
            "quality": facts.library_quality if facts is not None else None,
        },
    }


def _candidate(title: TMDBTitle, kind: MediaKind) -> dict[str, Any]:
    return {
        "tmdb_id": title.id,
        "kind": kind.value,
        "title": title.title,
        "year": title.year,
        "poster_url": _poster(title.poster_path),
    }


def _poster(path: str | None) -> str | None:
    return f"{TMDB_IMAGE_BASE}{_POSTER_SIZE}{path}" if path else None


def _guess(item: QueueItem) -> dict[str, Any] | None:
    media = item.media
    if media.tmdb_id is None:
        return None
    return {
        "tmdb_id": media.tmdb_id,
        "kind": media.kind.value,
        "title": media.title,
        "year": media.year,
        "poster_url": _poster(media.poster_path),
    }


def _hold_what_didnt_run(queue: Queue, ids: list[int]) -> None:
    """A file chosen but not moved reads as ready and would drop off the phone's lists unseen."""
    from app.forge.core.queue import ItemStatus

    for item_id in ids:
        item = queue.get(item_id)
        if item is not None and item.status is ItemStatus.READY:
            queue.hold(item_id, t("Couldn't move it."))


def _outcome(moved: int, total: int, reasons: list[str]) -> str:
    """What a tap did. A reason is Forge's own sentence; only the first one is said: the list
    has each file's."""
    reason = reasons[0].rstrip(".") + "." if reasons else ""
    if moved and not reasons:
        return tn("Moved {n} file into the library.", "Moved {n} files into the library.", moved)
    if moved:
        return t(
            "Moved {moved} of {total} files. {reason} Sort out the rest on the PC.",
            moved=moved,
            total=total,
            reason=reason,
        )
    if reasons:
        return t("{reason} Sort this out on the PC.", reason=reason)
    return t("Nothing needed moving.")


class PhoneForge:
    def __init__(self, runtime: ForgeRuntime, profiles: ProfileService) -> None:
        self._runtime = runtime
        self._profiles = profiles
        self._one_tap = asyncio.Lock()
        # "Use this one instead"'s facts per item: (its name in the library, them; None while
        # read), and the reads going on in the background.
        self._facts: dict[int, tuple[Path, ReplaceFacts | None]] = {}
        self._reads: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """The app is closing: the background reads stop waiting (their threads end alone)."""
        reads = list(self._reads)
        for task in reads:
            task.cancel()
        await asyncio.gather(*reads, return_exceptions=True)

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {
            ("GET", "/v1/forge"): manager_only(self._list),
            ("GET", "/v1/forge/{id}/search"): manager_only(self._search),
            ("POST", "/v1/forge/{id}/choose"): manager_only(self._choose),
            ("POST", "/v1/forge/{id}/replace"): manager_only(self._replace),
            ("POST", "/v1/forge/{id}/keep"): manager_only(self._keep),
        }

    async def _facts_now(self, services: ForgeServices, held: list[QueueItem]) -> dict[int, Any]:
        """The facts known now of these downloads already in the library (None: not yet). One
        not read for its name in the library starts reading in the background; nothing waits."""
        from app.forge.core.auto import library_naming
        from app.forge.core.formatter import name_file

        present = {item.id for item in services.queue.items}
        for gone in [item_id for item_id in self._facts if item_id not in present]:
            del self._facts[gone]
        if not held:
            return {}
        settings, presets = services.settings.current, await services.presets.list()
        known: dict[int, Any] = {}
        for item in held:
            rule, library = library_naming(item, settings, presets)
            choice = _band_naming(services, item)
            if choice is not None:  # the band's chip on the PC: the same library copy
                rule = choice.rule
            target = library.joinpath(*name_file(rule, item.media, item.path.suffix).parts)
            cached = self._facts.get(item.id)
            if cached is not None and cached[0] == target:
                known[item.id] = cached[1]
                continue
            self._facts[item.id] = (target, None)
            known[item.id] = None
            task = asyncio.create_task(self._read_facts(item, target))
            self._reads.add(task)
            task.add_done_callback(self._reads.discard)
        return known

    async def _read_facts(self, item: QueueItem, target: Path) -> None:
        from app.forge.core.replace_facts import replace_facts

        try:
            facts = await replace_facts(item, target)
        except Exception as exc:  # noqa: BLE001 - a fact not read stays null; the type only
            log.warning("Reading a replace's sizes for the phone failed (%s)", type(exc).__name__)
            return
        cached = self._facts.get(item.id)
        if cached is not None and cached[0] == target:
            self._facts[item.id] = (target, facts)

    async def _list(self, request: Request) -> Reply:
        from app.forge.core.matcher import match_key

        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        services = self._runtime.services
        if services is None:
            body: dict[str, Any] = {"state": "starting", "waiting": [], "elsewhere": []}
            if reason := self._runtime.reason:  # it won't get any further by itself
                body["message"] = reason
            return Reply(200, body)
        from app.forge.core.auto import held_in_library

        items = services.queue.items
        waiting = [i for i in items if _waiting(i)]
        per_show = Counter(match_key(i.parsed) for i in waiting)
        elsewhere = [i for i in items if _elsewhere(i)]
        facts = await self._facts_now(services, [i for i in elsewhere if held_in_library(i)])
        return Reply(
            200,
            {
                "state": "ready",
                "waiting": [
                    {
                        "id": item.id,
                        "path_key": path_key(item.path),
                        "file_name": item.path.name,
                        "guess": _guess(item),
                        "candidates": [
                            _candidate(c, item.parsed.kind)
                            for c in (item.resolution.candidates if item.resolution else ())
                        ],
                        "same_show_count": per_show[match_key(item.parsed)],
                    }
                    for item in waiting
                ],
                "elsewhere": [
                    {
                        "file_name": i.path.name,
                        "reason": _reason(i),
                        **(
                            _replace_entry(i, facts[i.id], _can_replace(services, i))
                            if i.id in facts
                            else {}
                        ),
                    }
                    for i in elsewhere
                ],
            },
        )

    async def _search(self, request: Request) -> Reply:
        from app.forge.clients.tmdb import TmdbError
        from app.forge.models.media import MediaKind

        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        services = self._runtime.services
        if services is None:
            return _starting()
        item_id = _item_id(request)
        query = request.query.get("q", "").strip()
        if item_id is None or not query or len(query) > MAX_QUERY:
            return bad_request()
        item = _waiting_item(services, item_id)
        if item is None:
            return _gone()
        kind = item.parsed.kind
        try:
            if kind is MediaKind.EPISODE:
                found = await services.tmdb.search_tv(query, None)
            else:
                found = await services.tmdb.search_movie(query, None)
        except TmdbError as exc:
            log.warning("TMDB search from the phone failed (%s)", type(exc).__name__)
            return _tmdb_away()
        return Reply(200, {"candidates": [_candidate(c, kind) for c in found[:MAX_RESULTS]]})

    async def _choose(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        services = self._runtime.services
        if services is None:
            return _starting()
        item_id = _item_id(request)
        tmdb_id = request.body.get("tmdb_id")
        kind = request.body.get("kind")
        key = request.body.get("path_key")
        if (
            item_id is None
            or not isinstance(tmdb_id, int)
            or isinstance(tmdb_id, bool)
            or tmdb_id <= 0
            or not isinstance(kind, str)
            or not isinstance(key, str)
        ):
            return bad_request()
        async with self._one_tap:
            return await self._choose_and_move(services, item_id, tmdb_id, kind, key)

    def _held_request(
        self, request: Request
    ) -> tuple[ForgeServices, int, str] | Reply:
        """A replace's or keep's request: the Forge, the item id and its `path_key`, or the
        reply that says why not."""
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        services = self._runtime.services
        if services is None:
            return _starting()
        item_id = _item_id(request)
        key = request.body.get("path_key")
        if item_id is None or not isinstance(key, str):
            return bad_request()
        return services, item_id, key

    async def _replace(self, request: Request) -> Reply:
        asked = self._held_request(request)
        if isinstance(asked, Reply):
            return asked
        async with self._one_tap:
            return await self._replace_now(*asked)

    async def _replace_now(self, services: ForgeServices, item_id: int, key: str) -> Reply:
        """"Use this one instead", as the PC's band does it (without its question: the phone
        asked)."""
        from app.forge.core.duplicates import aside_folder, is_root
        from app.forge.core.library import library_folder, library_problems
        from app.forge.core.queue import ItemStatus
        from app.forge.models.history import RunSource

        item = _in_library_item(services, item_id, key)
        if item is None:
            return _gone()
        settings = services.settings.current
        problems = library_problems(settings)
        if problems:
            return error_reply(409, "failed", problems[0])
        library = library_folder(item.media.kind, settings)
        place = services.aside_place
        if library is None or aside_folder(library, place) is None:
            return error_reply(409, "failed", t(NO_ASIDE))
        choice = _band_naming(services, item)
        result = await services.auto.replace(
            [item.id],
            source=RunSource.APP,
            place=place,
            naming={item.id: choice} if choice is not None else None,
        )
        if result.renamed:  # what moved counts: with remove_after_forge it left the queue at once
            log.info(
                "The phone's replace moved %d file(s), %d copy(ies) aside",
                result.renamed,
                result.replaced,
            )
            if not result.replaced:
                message = t(REPLACED_NOTHING)
            else:
                message = t(REPLACED_TO_PLACE) if is_root(library) else t(REPLACED)
            return Reply(200, {"moved": result.renamed, "message": message})
        after = services.queue.get(item.id)
        if after is None or after.status in (ItemStatus.DONE, ItemStatus.SKIPPED):
            log.info("The phone's replace found it sorted already")  # the PC's own, meanwhile
            return _gone()
        log.info("The phone's replace didn't run (%d failed)", result.failed)
        return error_reply(409, "failed", t(NOT_REPLACED))

    async def _keep(self, request: Request) -> Reply:
        asked = self._held_request(request)
        if isinstance(asked, Reply):
            return asked
        async with self._one_tap:
            return await self._keep_now(*asked)

    async def _keep_now(self, services: ForgeServices, item_id: int, key: str) -> Reply:
        """"Keep the one I have": this download left alone, as the PC's "Leave this file alone"
        (Auto-rename never picks it up again; Settings › Auto-rename lists it)."""
        item = _in_library_item(services, item_id, key)
        if item is None:
            return _gone()
        try:
            await services.ignored.ignore(item.path, folder=False)
        except Exception as exc:  # noqa: BLE001 - the database said no; the type only
            log.warning("Leaving a download alone from the phone failed (%s)", type(exc).__name__)
            return error_reply(409, "failed", t(NOT_KEPT))
        services.queue.remove_many([item.id])
        self._facts.pop(item.id, None)
        log.info("The phone left 1 download alone")
        return Reply(200, {"message": t(KEPT)})

    async def _choose_and_move(
        self, services: ForgeServices, item_id: int, tmdb_id: int, kind: str, key: str
    ) -> Reply:
        from app.forge.clients.tmdb import TmdbError
        from app.forge.core.library import library_problems
        from app.forge.core.matcher import match_key
        from app.forge.core.queue import ItemStatus
        from app.forge.models.history import RunSource

        def the_file() -> QueueItem | None:
            item = _waiting_item(services, item_id)
            return item if item is not None and path_key(item.path) == key else None

        item = the_file()
        if item is None:
            return _gone()
        if kind != item.parsed.kind.value:
            return bad_request()  # a film's name on an episode, or the other way round
        problems = library_problems(services.settings.current)
        if problems:
            return error_reply(409, "failed", problems[0])
        known = item.resolution.candidates if item.resolution else ()
        title = next((c for c in known if c.id == tmdb_id), None)
        if title is None:  # it came from a search
            try:
                title = await services.tmdb.details(item.parsed.kind, tmdb_id)
            except TmdbError as exc:
                log.warning("TMDB lookup from the phone failed (%s)", type(exc).__name__)
                return _tmdb_away()
            if title is None:
                return error_reply(
                    404, "no_results", t("TMDB doesn't know that title, so search for it again.")
                )

        queue = services.queue
        # Not during a run: choose_match marks files ready, also one that run has just finished.
        async with services.run_lock:
            item = the_file()
            if item is None:
                return _gone()
            show = match_key(item.parsed)
            same_show = [i for i in queue.items if match_key(i.parsed) == show]
            ids = [i.id for i in same_show if _waiting(i)]
            staying = [(i.id, _reason(i), i.in_library) for i in same_show if _elsewhere(i)]
            await queue.choose_match(item.id, title)
            for other_id, reason, in_library in staying:  # choose_match made them ready too
                queue.hold(other_id, reason, in_library=in_library)  # Replace stays offered
        try:
            result = await services.auto.rename(
                ids, source=RunSource.APP, hold_taken=True, preferred=item.id
            )
        finally:
            _hold_what_didnt_run(queue, ids)
        moved = result.renamed
        reasons = []
        for other_id in ids:
            other = queue.get(other_id)
            if other is not None and other.status is ItemStatus.HELD:
                reasons.append(_reason(other))
        log.info("The phone's choice moved %d of %d file(s)", moved, len(ids))
        return Reply(200, {"moved": moved, "message": _outcome(moved, len(ids), reasons)})
