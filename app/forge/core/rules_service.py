"""The rules for watched folders as the renamer and Settings see them (`services.rules`).

A wrapper over `app.forge.db.rules.RuleRepo` that holds the rules in memory for synchronous reads
(`current`, `get`, `airgrabber_rule`, `watched_folders`, `destination_folders`), so `app/forge/ui`
and AirGrabber's side never see the database package.

- `load()` at Forge's start: the one-time carry-over first (`carry_rules`: today's watched folders,
  presets and Off / Manual / On become rules, each fresh, so nothing already there moves unseen;
  `ForgeSettings.rules_carried`; a failure is logged by its type and tried again at the next
  start), then AirGrabber's rule is made sure of: exactly one, added last when missing.
- AirGrabber's rule's on / off *is* `ForgeSettings.use_airgrabber_downloads` (with its "not chosen
  yet" meaning: `airgrabber_on`): `current` shows the setting, never the stored column, and a
  `replace_all` that switches it stores the setting.
- `replace_all` works out each rule's fresh flag against the stored one: a new rule, one whose
  folder or subfolders changed, or one switched back on is fresh (its first look queues the
  files already there as Manual: nothing moves unseen); otherwise it keeps the stored flag.
  `mark_fresh` / `first_look_done` set and clear it (the renamer).
- `save` (Settings' Save): the settings and the rules as one change (`generation` None
  meanwhile), inside the renamer's `hold_looks` (only ever held for a look's short commits: a
  Save waits milliseconds, never for a listing, TMDB or a move), and first every rule that will watch what a look
  skipped before marked fresh (`rules.uncovered`: a destination or library folder that stopped
  being one under its folder, a folder newly watched such as AirGrabber's from "overlap").
- `presets_renamed` (Settings › Naming's Save) has the rules follow renamed presets (one
  transaction, swaps included).
- One change at a time (a lock): a fresh flag set while Settings saves is never written over.
- `generation`: changes with every stored change (a fresh mark included, a first look's clearing
  not), None while one is being stored; the renamer commits a look's take-in and moves only
  while it is as that look found it (app/forge/core/auto.py's `_still_as_read`), so neither
  waits for the other.
- `subscribe` hears `replace_all`, `reload` and `presets_renamed`, not the fresh flags. A listener
  that fails is logged, never raised.

Logs give ids and counts only: never a folder or a rule's name (it may hold a title).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from app.forge.core.rules import destinations, rules_from_settings, uncovered, watched_folders
from app.forge.db.rules import RuleRepo
from app.forge.models.rules import Rule
from app.forge.models.settings import ForgeSettings

log = logging.getLogger(__name__)

RulesListener = Callable[[tuple[Rule, ...]], None]


class RulesSettings(Protocol):
    """What the rules need of ForgeSettingsService."""

    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...


async def carry_rules(repo: RuleRepo, settings: ForgeSettings) -> bool:
    """Once: today's setup into the rules table (only while it is empty), every rule fresh: its
    first look queues what is already there as Manual once (a carried rule may watch more than
    before: AirGrabber's folder that held one of the owner's, a setup that waited for its library
    folders), what arrives after it goes as its mode says. True when the caller should mark it
    done (`rules_carried=True`), False when it was done before."""
    if settings.rules_carried:
        return False
    if not await repo.list():
        carried = [rule.model_copy(update={"fresh": True}) for rule in rules_from_settings(settings)]
        await repo.replace_all(carried)
        log.info("Carried %d watched folder(s) over into rules", len(settings.watch_folders))
    return True


class RuleService:
    def __init__(self, repo: RuleRepo, settings: RulesSettings) -> None:
        self._repo = repo
        self._settings = settings
        self._stored: tuple[Rule, ...] = ()  # as stored (AirGrabber's `on` column unread)
        self._listeners: list[RulesListener] = []
        # One change at a time: a fresh flag set while Settings saves is never written over.
        self._lock = asyncio.Lock()
        # `generation`: how many changes were stored; how many are being stored now.
        self._changes = 0
        self._storing = 0
        # The renamer's `hold_looks` (`hold_looks_with`): no look commits during a Save.
        self._hold_looks: Callable[[], AbstractAsyncContextManager[None]] | None = None

    def hold_looks_with(self, hold: Callable[[], AbstractAsyncContextManager[None]]) -> None:
        """`save` stores inside `hold` (the renamer's `hold_looks`): a look holds it only for
        its two short commits (a few DB writes), so a Save waits milliseconds at most, never for
        a listing, TMDB or a move."""
        self._hold_looks = hold

    @property
    def generation(self) -> int | None:
        """Changes with every stored change of the rules (a Save with its settings, a fresh
        mark, `replace_all`, an import's `reload`, presets renamed; not a first look's
        `first_look_done`); None while one is being stored. The renamer commits what a look
        decided from the rules only while it is as the look found it."""
        return None if self._storing else self._changes

    @contextlib.asynccontextmanager
    async def _changing(self) -> AsyncIterator[None]:
        self._storing += 1
        self._changes += 1
        try:
            yield
        finally:
            self._storing -= 1
            self._changes += 1

    # -- reading ------------------------------------------------------------------------------

    @property
    def current(self) -> tuple[Rule, ...]:
        """The rules in their order; AirGrabber's rule's `on` is the setting's."""
        on = self._settings.current.airgrabber_on
        return tuple(
            rule.model_copy(update={"on": on}) if rule.airgrabber and rule.on != on else rule
            for rule in self._stored
        )

    def get(self, rule_id: int) -> Rule | None:
        return next((rule for rule in self.current if rule.id == rule_id), None)

    @property
    def airgrabber_rule(self) -> Rule:
        """AirGrabber's rule (before `load`: the one the carry-over would make)."""
        found = next((rule for rule in self.current if rule.airgrabber), None)
        if found is not None:
            return found
        made = rules_from_settings(self._settings.current)[-1]
        return made.model_copy(update={"on": self._settings.current.airgrabber_on})

    def watched_folders(self) -> list[str]:
        """The distinct folders the rules watch now, in order, as given (AirGrabber's in use
        included): what AirGrabber's Downloads page reads Forge's moves from, the queue's
        roots and the ignored entries kept at the start."""
        return watched_folders(self.current, self._settings.current)

    def destination_folders(self) -> list[str]:
        """The folders files are moved to now: the library folders and the rules' own folder
        destinations (`rules.destinations`), as text."""
        return [str(folder) for folder in destinations(self.current, self._settings.current)]

    def subscribe(self, listener: RulesListener) -> Callable[[], None]:
        """Call `listener` with the rules after every change of them (not after a fresh flag's).
        Returns the function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _changed(self) -> None:
        rules = self.current
        for listener in list(self._listeners):
            try:
                listener(rules)
            except Exception:
                log.exception("A listener to changed rules failed")

    # -- loading ------------------------------------------------------------------------------

    async def load(self) -> None:
        """At Forge's start: the one-time carry-over, then the rules read (AirGrabber's made
        sure of)."""
        async with self._lock:
            try:
                if await carry_rules(self._repo, self._settings.current):
                    await self._settings.update(rules_carried=True)
            except Exception as exc:  # noqa: BLE001 - forge.db: tried again at the next start
                log.warning(
                    "Couldn't carry the watched folders over into rules (%s)", type(exc).__name__
                )
            await self._read()

    async def reload(
        self, *, before_telling: Callable[[], Awaitable[object]] | None = None
    ) -> None:
        """Read the rules again (forge.db was replaced), and tell. `before_telling` is awaited
        once they are read (the import stores its settings there: the settings' listeners then
        see the new rules, and these listeners the new settings)."""
        async with self._changing():  # the rules and the import's settings: one change
            async with self._lock:
                await self._read()
            if before_telling is not None:
                await before_telling()
            self._changed()

    async def _read(self) -> None:
        rules = await self._repo.list()
        if not any(rule.airgrabber for rule in rules):
            # Missing (never the carry-over's doing): added last, fresh like any new rule.
            made = rules_from_settings(self._settings.current)[-1]
            rules = await self._repo.replace_all([*rules, made.model_copy(update={"fresh": True})])
            log.info("Added AirGrabber's rule (it was missing)")
        self._stored = tuple(rules)

    # -- changing -----------------------------------------------------------------------------

    async def replace_all(self, rules: Sequence[Rule]) -> tuple[Rule, ...]:
        """Store `rules` as the whole list (Settings' Save), each rule's fresh flag worked out
        against the stored one; AirGrabber's rule, when left out, stays (last). Its on / off goes
        to `use_airgrabber_downloads` when it differs from the setting. Returns `current`."""
        async with self._lock, self._changing():
            before = {rule.id: rule for rule in self.current}
            wanted = list(rules)
            if not any(rule.airgrabber for rule in wanted):  # as shown: `on` is the setting's
                wanted.extend(rule for rule in before.values() if rule.airgrabber)
            prepared = [rule.model_copy(update={"fresh": _fresh(rule, before)}) for rule in wanted]
            self._stored = tuple(await self._repo.replace_all(prepared))
            airgrabber = next((rule for rule in prepared if rule.airgrabber), None)
            if airgrabber is not None and airgrabber.on != self._settings.current.airgrabber_on:
                await self._settings.update(use_airgrabber_downloads=airgrabber.on)
            log.info("Saved %d rule(s)", len(self._stored))
        self._changed()
        return self.current

    async def save(
        self, rules: Sequence[Rule] | None, changes: Mapping[str, Any]
    ) -> tuple[Rule, ...]:
        """Settings' Save: every rule that will watch what a look skipped before (`uncovered`: a
        destination or library folder gone from under its folder, a folder newly watched) is
        marked fresh first, then the settings `changes` are stored, then `rules` as the whole
        list (`replace_all`; None: the rules stay). One change for the renamer (the
        renamer's generation, `generation`, is None meanwhile: a look that read the rules or the
        settings before or during it commits nothing it decided from them), stored inside the
        renamer's `hold_looks` so it never lands inside a look's commit (which is only a few DB
        writes long: a Save never waits for a listing, TMDB or a move). A failure halfway leaves
        the marks (the safe side: those rules' files wait once). Returns `current`."""
        hold = self._hold_looks() if self._hold_looks is not None else contextlib.nullcontext()
        async with hold, self._changing():
            before_rules, before = self.current, self._settings.current
            after = (
                ForgeSettings.model_validate({**before.model_dump(), **changes})
                if changes
                else before
            )
            wanted = list(rules) if rules is not None else list(before_rules)
            if rules is None or not any(rule.airgrabber for rule in wanted):
                wanted = [rule for rule in wanted if not rule.airgrabber] + [
                    rule.model_copy(update={"on": after.airgrabber_on})
                    for rule in before_rules
                    if rule.airgrabber
                ]
            exposed = uncovered(before_rules, before, wanted, after)
            if exposed:
                await self._set_fresh(exposed, True)
                log.info("%d rule(s) watch more now: their files wait once", len(exposed))
            if changes:
                await self._settings.update(**changes)
            if rules is not None:
                await self.replace_all(rules)
        return self.current

    async def mark_fresh(self, rule_id: int) -> None:
        """The rule's next look queues the files already in its folder as Manual."""
        await self._set_fresh([rule_id], True)

    async def first_look_done(
        self, rule_ids: Collection[int], *, generation: int | None = None
    ) -> None:
        """These rules had their first look: from now on their files are done as their mode says.
        `generation`: the one the look noted; when the rules changed since (a Save, a fresh mark),
        nothing is cleared (the look decided from rules that aren't there any more)."""
        await self._set_fresh(rule_ids, False, generation=generation)

    async def _set_fresh(
        self, rule_ids: Collection[int], fresh: bool, *, generation: int | None = None
    ) -> None:
        ids = set(rule_ids)
        if not ids:
            return
        # A fresh mark is a change for the renamer (a look that read the rules before it
        # commits nothing); a first look's clearing is the look's own commit.
        changing = self._changing() if fresh else contextlib.nullcontext()
        async with self._lock, changing:
            if generation is not None and self.generation != generation:
                log.info("The rules changed during a look: %d fresh flag(s) kept", len(ids))
                return
            await self._repo.set_fresh(ids, fresh)
            self._stored = tuple(
                rule.model_copy(update={"fresh": fresh}) if rule.id in ids else rule
                for rule in self._stored
            )

    async def presets_renamed(self, renamed: Mapping[str, str]) -> None:
        """The rules follow presets renamed in Settings › Naming (old name → new name; two
        presets that swapped names swap in the rules too), in one transaction; then tell."""
        if not any(old != new for old, new in renamed.items()):
            return
        async with self._lock, self._changing():
            changed = await self._repo.rename_presets(renamed)
            log.info("Rules followed %d renamed preset(s) (%d rule(s))", len(renamed), changed)
            await self._read()
        self._changed()


def _fresh(rule: Rule, before: Mapping[int, Rule]) -> bool:
    """New, moved (folder or subfolders) or switched back on: fresh; else as it was."""
    old = before.get(rule.id) if rule.id else None
    if old is None:
        return True
    if rule.folder != old.folder or rule.subfolders != old.subfolders:
        return True
    if rule.on and not old.on:
        return True
    return old.fresh
