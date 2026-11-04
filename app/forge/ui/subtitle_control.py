"""Subtitles asked for by hand: CC ▾ on the queue's done bands and on History's rows.

The button is drawn by the rows themselves (app/forge/ui/widgets/virtual_list.py: a band's or a
line's `actions`, greyed through `greyed` with the reason as a tip), so no widget is added per
row; this holds what it does. Its menu offers "Get English subtitles" per language chosen in
Settings › Subtitles and "Get all my languages" with two or more (`cc_menu`). A view keeps one
SubtitleDesk: it runs a request (ForgeSubtitles.fetch for each of a row's videos) and remembers
its state per row, which stays through the list's rebuilds: "Looking for subtitles…", then what
happened in plain words for OUTCOME_MS (`fetch_run`). Without a key in this version or a language
(`SubtitleDesk.look` None) no button shows. Today's allowance used up greys it, with the reason.

No Tk here: the view hands in how to spawn, wait and redraw.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Hashable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.forge.core.subtitles import combine_fetches, quota_text
from app.forge.models.subtitles import (
    SubtitleFetch,
    SubtitleFetchOutcome,
    SubtitleIdentity,
    SubtitleQuota,
)
from app.i18n import N_, t
from app.models import SUBTITLE_LANGUAGES

OUTCOME_MS = 60_000  # a request's outcome stays on its row this long
CC_ACTION = "cc"  # the rows' action id
CC_LABEL = "CC ▾"  # the button's text: a name, the same in every language

# Shown through t().
GET_ONE_TEXT = N_("Get {language} subtitles")
GET_ALL_TEXT = N_("Get all my languages")
LOOKING_TEXT = N_("Looking for subtitles…")
FOUND_TEXT = N_("Found: {languages}")
ALREADY_TEXT = N_("Already there")
NOT_FOUND_TEXT = N_("Not found yet")
PARTLY_TEXT = N_("Not found yet: {languages}")
NO_VIDEO_TEXT = N_("The video is no longer there")
QUOTA_TEXT = N_("Today's limit reached")
FAILED_TEXT = N_("Couldn't get subtitles")

MenuItems = list[tuple[str, Callable[[], None]]]


class RunPhase(StrEnum):
    WORKING = "working"
    DONE = "done"  # with a text to show


@dataclass(frozen=True, slots=True)
class SubtitleRun:
    """A row's request: working, or done with `text`."""

    phase: RunPhase
    text: str = ""


WORKING = SubtitleRun(RunPhase.WORKING)


def language_name(code: str) -> str:
    """"English" / "Engels" for "en"; an unknown code in capitals."""
    name = SUBTITLE_LANGUAGES.get(code)
    return t(name) if name is not None else code.upper()


def cc_menu(languages: Sequence[str], fetch: Callable[[tuple[str, ...]], None]) -> MenuItems:
    """The CC menu: "Get English subtitles" per language, then "Get all my languages" when
    there is more than one."""
    items: MenuItems = [
        (t(GET_ONE_TEXT, language=language_name(code)), _bind(fetch, (code,)))
        for code in languages
    ]
    if len(languages) > 1:
        items.append((t(GET_ALL_TEXT), _bind(fetch, tuple(languages))))
    return items


def _bind(
    fetch: Callable[[tuple[str, ...]], None], languages: tuple[str, ...]
) -> Callable[[], None]:
    return lambda: fetch(languages)


def codes_text(languages: Sequence[str]) -> str:
    return " ".join(code.upper() for code in languages)


def fetch_run(result: SubtitleFetch) -> SubtitleRun:
    """What a finished request says on its row."""
    outcome = result.outcome
    if outcome is SubtitleFetchOutcome.SAVED:
        return SubtitleRun(RunPhase.DONE, t(FOUND_TEXT, languages=codes_text(result.saved)))
    if outcome is SubtitleFetchOutcome.ALREADY:
        return SubtitleRun(RunPhase.DONE, t(ALREADY_TEXT))
    if outcome is SubtitleFetchOutcome.PARTLY:
        return SubtitleRun(RunPhase.DONE, t(PARTLY_TEXT, languages=codes_text(result.not_found)))
    if outcome is SubtitleFetchOutcome.NOT_FOUND:
        return SubtitleRun(RunPhase.DONE, t(NOT_FOUND_TEXT))
    if outcome is SubtitleFetchOutcome.NO_VIDEO:
        return SubtitleRun(RunPhase.DONE, t(NO_VIDEO_TEXT))
    if outcome is SubtitleFetchOutcome.QUOTA:
        return SubtitleRun(RunPhase.DONE, t(QUOTA_TEXT))
    return SubtitleRun(RunPhase.DONE, result.message or t(FAILED_TEXT))


class SubtitleRuns:
    """The requests' states per row (survives the list's rebuilds)."""

    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self._runs: dict[Hashable, SubtitleRun] = {}
        self._on_change = on_change

    def get(self, key: Hashable) -> SubtitleRun | None:
        return self._runs.get(key)

    def is_working(self, key: Hashable) -> bool:
        run = self._runs.get(key)
        return run is not None and run.phase is RunPhase.WORKING

    def set(self, key: Hashable, run: SubtitleRun | None) -> None:
        if run is None:
            self._runs.pop(key, None)
        else:
            self._runs[key] = run
        self._on_change()

    def expire(self, key: Hashable, run: SubtitleRun) -> None:
        """Drop a finished request's text, unless another request replaced it meanwhile."""
        if self._runs.get(key) is run:
            self.set(key, None)


@dataclass(frozen=True, slots=True)
class CcLook:
    """How a row's CC ▾ shows now: greyed (`tip`: why, "" while its request runs) or not, and
    what its request says (`note`, "" for nothing)."""

    greyed: bool = False
    tip: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class VideoTarget:
    """One video of a row: where it is, who it is, and the run that renamed it (None: none)."""

    video: Path
    identity: SubtitleIdentity
    run_id: int | None = None


class SubtitleSource(Protocol):
    """What the desk needs of ForgeSubtitles (app/forge/core/subtitles.py)."""

    @property
    def available(self) -> bool: ...

    @property
    def languages(self) -> tuple[str, ...]: ...

    @property
    def used_up(self) -> bool: ...

    @property
    def quota(self) -> SubtitleQuota: ...

    async def fetch(
        self,
        video: Path,
        identity: SubtitleIdentity,
        languages: Sequence[str] | None = None,
        *,
        run_id: int | None = None,
    ) -> SubtitleFetch: ...


class SubtitleDesk:
    """A view's CC ▾: what each row's button shows, its menu and its requests."""

    def __init__(
        self,
        source: SubtitleSource,
        *,
        spawn: Callable[[Coroutine[Any, Any, None]], object],
        after: Callable[[int, Callable[[], None]], object],
        on_change: Callable[[], None],
        on_done: Callable[[Hashable], None] = lambda _key: None,
    ) -> None:
        """`spawn`: starts a coroutine as one of the view's tasks; `after`: Tk's after; `on_change`:
        draw the rows again; `on_done`: a request finished (the view may read the journal again)."""
        self._source = source
        self._spawn = spawn
        self._after = after
        self._on_done = on_done
        self.runs = SubtitleRuns(on_change)

    def look(self, key: Hashable, *, gone: str = "") -> CcLook | None:
        """How `key`'s CC ▾ shows; None when it doesn't (no key in this version, no language).
        `gone`: why this row's video can't get any (its video is no longer there)."""
        source = self._source
        if not source.available or not source.languages:
            return None
        run = self.runs.get(key)
        if run is not None and run.phase is RunPhase.WORKING:
            return CcLook(greyed=True, note=t(LOOKING_TEXT))
        note = run.text if run is not None else ""
        if gone:
            return CcLook(greyed=True, tip=gone, note=note)
        if source.used_up:
            return CcLook(greyed=True, tip=quota_text(source.quota.reset_at), note=note)
        return CcLook(note=note)

    def menu(
        self, key: Hashable, targets: Callable[[], Awaitable[Sequence[VideoTarget]]]
    ) -> MenuItems:
        """The items of `key`'s menu; `targets` gives the videos when one is chosen."""
        return cc_menu(
            self._source.languages, lambda languages: self.fetch(key, targets, languages)
        )

    def fetch(
        self,
        key: Hashable,
        targets: Callable[[], Awaitable[Sequence[VideoTarget]]],
        languages: Sequence[str],
    ) -> None:
        """One request for a row's videos, one video at a time; today's allowance used up or a
        problem ends it."""
        runs = self.runs
        if runs.is_working(key) or not languages:
            return
        runs.set(key, WORKING)
        self._spawn(self._run(key, targets, languages))

    async def _run(
        self,
        key: Hashable,
        targets: Callable[[], Awaitable[Sequence[VideoTarget]]],
        languages: Sequence[str],
    ) -> None:
        runs = self.runs
        results: list[SubtitleFetch] = []
        try:
            for target in await targets():
                found = await self._source.fetch(
                    target.video, target.identity, languages, run_id=target.run_id
                )
                results.append(found)
                if found.outcome in (SubtitleFetchOutcome.QUOTA, SubtitleFetchOutcome.PROBLEM):
                    break
        except BaseException:
            runs.set(key, None)
            raise  # cancelled, or logged and reported by the task runner
        result = (
            combine_fetches(results)
            if results
            else SubtitleFetch(outcome=SubtitleFetchOutcome.NO_VIDEO)
        )
        state = fetch_run(result)
        runs.set(key, state)
        self._after(OUTCOME_MS, lambda: runs.expire(key, state))
        self._on_done(key)
