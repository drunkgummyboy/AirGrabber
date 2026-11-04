"""People ("accounts"): the family sharing one AirGrabber, each with their own followed series,
film watchlist and calendar. Everything else is shared: the settings, the folders, the torrent
program, the Downloads page and the download history, "I already have this" and the
shows' cache. The automatic downloads look after everyone's lists, whoever is selected
(app/services/auto_download.py).

`ProfileService` (`services.profiles`): the people (`people`), who is selected (`current`,
kept as the `current_profile` setting, so AirGrabber opens as the last one chosen), adding,
renaming / recolouring (`update`), removing (never the last; their followed series and watchlist
go with them, downloads stay) and switching. Listeners (`subscribe`) hear every change: PEOPLE
(someone added, renamed, recoloured or removed) or SWITCHED (another person selected; the
watchlist in memory is theirs by then).

Who is selected is read where a page or window loads and handed on as an id (`profile_id`) to
every per-person call, so work that finishes after a switch still belongs to the person it was
started for.

Log hygiene: people's names are never logged, only their ids.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum

from app.database import ProfileRepository
from app.i18n import t
from app.models import FIRST_PROFILE_ID, Profile, ProfileColour
from app.models.profiles import NAME_MAX
from app.services.settings import SettingsService
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)


class ProfileChange(StrEnum):
    PEOPLE = "people"  # someone was added, renamed, recoloured or removed
    SWITCHED = "switched"  # another person is selected


ProfileListener = Callable[[ProfileChange], None]


class ProfileError(ValueError):
    """A change that can't be made; `str()` says why in plain words."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def clean_name(name: str | None) -> str:
    """A typed name as stored: spaces at the ends dropped, inner runs of spaces made one."""
    return " ".join((name or "").split())


def name_problem(
    name: str | None, people: Iterable[Profile], *, required: bool = True, own: int | None = None
) -> str | None:
    """Why `name` (cleaned) can't be a person's name, else None: empty while `required`, longer
    than NAME_MAX, or another person's (`own`: the person being renamed) ignoring case."""
    text = clean_name(name)
    if not text:
        return t("Type a name") if required else None
    if len(text) > NAME_MAX:
        return t("Use at most {count} characters", count=NAME_MAX)
    folded = text.casefold()
    for person in people:
        if person.id != own and person.name is not None and person.name.casefold() == folded:
            return t("Someone is called {name} already", name=person.name)
    return None


def next_colour(people: Iterable[Profile]) -> ProfileColour:
    """The first swatch nobody has yet (all taken: the first)."""
    taken = {person.colour for person in people}
    return next((colour for colour in ProfileColour if colour not in taken), ProfileColour.BLUE)


class ProfileService:
    """The people and who is selected (see the module docstring). `load` once at startup
    (Services.create), before anything reads `current`."""

    def __init__(
        self,
        repository: ProfileRepository,
        settings: SettingsService,
        watchlist: WatchlistService,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`watchlist`: kept in memory for the selected person; loaded for whoever is switched
        to before the listeners hear it. `clock`: the current UTC instant (tests)."""
        self._repository = repository
        self._settings = settings
        self._watchlist = watchlist
        self._clock = clock
        self._people: tuple[Profile, ...] = ()
        self._listeners: list[ProfileListener] = []
        self._lock = asyncio.Lock()  # one change at a time: two quick switches stay in order

    # -- listeners -------------------------------------------------------------------------------

    def subscribe(self, listener: ProfileListener) -> Callable[[], None]:
        """Call `listener` after every change (PEOPLE / SWITCHED). Returns a function that
        unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, change: ProfileChange) -> None:
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:
                log.exception("Profile listener %r failed", listener)

    # -- reading ---------------------------------------------------------------------------------

    @property
    def people(self) -> tuple[Profile, ...]:
        """Everyone, oldest first."""
        return self._people

    @property
    def current(self) -> Profile:
        """The selected person (the first one when the setting names nobody known)."""
        wanted = self._settings.current.current_profile
        for person in self._people:
            if person.id == wanted:
                return person
        if self._people:
            return self._people[0]
        # Before `load`: the person the database always has.
        return Profile(id=FIRST_PROFILE_ID, created_at=datetime(1970, 1, 1, tzinfo=UTC))

    @property
    def current_id(self) -> int:
        return self.current.id

    def get(self, profile_id: int) -> Profile | None:
        return next((person for person in self._people if person.id == profile_id), None)

    async def load(self) -> None:
        """Read the people; a selection naming nobody (a removed person, a copied database)
        becomes the first person; the watchlist is loaded for the selected one."""
        async with self._lock:
            await self._read()

    async def reload(self) -> None:
        """Read everything again (a backup was loaded: people may have come or gone, their
        lists changed) and tell the listeners: PEOPLE, and SWITCHED when the selected person
        went (the first one is selected then)."""
        before = self.current_id
        async with self._lock:
            await self._read()
        self._notify(ProfileChange.PEOPLE)
        if self.current_id != before:
            self._notify(ProfileChange.SWITCHED)

    async def _read(self) -> None:
        self._people = tuple(await self._repository.all())
        selected = self.current.id
        if selected != self._settings.current.current_profile:
            log.info("The selected person is gone: person %d is selected", selected)
            await self._settings.update(current_profile=selected)
        await self._watchlist.load(selected)

    # -- changes ---------------------------------------------------------------------------------

    async def add(self, name: str, colour: ProfileColour) -> Profile:
        """Add a person (an empty list, nothing selected yet). Raises ProfileError for a name
        that can't be one (`name_problem`)."""
        async with self._lock:
            if (problem := name_problem(name, self._people)) is not None:
                raise ProfileError(problem)
            person = await self._repository.add(clean_name(name), colour, self._clock())
            self._people = tuple(await self._repository.all())
        log.info("Added person %d", person.id)
        self._notify(ProfileChange.PEOPLE)
        return person

    async def update(self, profile_id: int, *, name: str | None, colour: ProfileColour) -> Profile:
        """A person's new name and colour. An empty name (None) leaves them unnamed ("Me"),
        which only one person can be. Raises ProfileError for a name that can't be theirs or
        a person that is gone."""
        async with self._lock:
            person = self.get(profile_id)
            if person is None:
                raise ProfileError(t("That person isn't there any more"))
            text = clean_name(name) or None
            if text is None:
                if any(other.id != profile_id and other.name is None for other in self._people):
                    raise ProfileError(t("Type a name"))
            elif (problem := name_problem(text, self._people, own=profile_id)) is not None:
                raise ProfileError(problem)
            if text != person.name or colour is not person.colour:
                await self._repository.update(profile_id, name=text, colour=colour)
                self._people = tuple(await self._repository.all())
                log.info("Changed person %d's name or colour", profile_id)
                changed = True
            else:
                changed = False
            updated = self.get(profile_id) or person
        if changed:
            self._notify(ProfileChange.PEOPLE)
        return updated

    async def remove(self, profile_id: int) -> None:
        """Remove a person with their followed series and watchlist (downloads stay). The
        selected person's removal first switches to the first other one. Raises ProfileError
        for the last person."""
        switched = False
        async with self._lock:
            if self.get(profile_id) is None:
                return
            if len(self._people) <= 1:
                raise ProfileError(t("The last person can't be removed"))
            if profile_id == self.current_id:
                other = next(person for person in self._people if person.id != profile_id)
                await self._select(other.id)
                switched = True
            await self._repository.remove(profile_id)
            self._people = tuple(await self._repository.all())
        log.info("Removed person %d", profile_id)
        self._notify(ProfileChange.PEOPLE)
        if switched:
            self._notify(ProfileChange.SWITCHED)

    async def switch(self, profile_id: int) -> None:
        """Select another person: saved (AirGrabber opens as them next time), their watchlist
        read, then the listeners hear SWITCHED. Selecting the selected person does nothing;
        raises ProfileError for a person that is gone."""
        async with self._lock:
            if self.get(profile_id) is None:
                raise ProfileError(t("That person isn't there any more"))
            if profile_id == self.current_id:
                return
            await self._select(profile_id)
        log.info("Switched to person %d", profile_id)
        self._notify(ProfileChange.SWITCHED)

    async def _select(self, profile_id: int) -> None:
        await self._settings.update(current_profile=profile_id)
        await self._watchlist.load(profile_id)
