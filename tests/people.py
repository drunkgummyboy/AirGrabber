"""A stand-in for ProfileService (app/services/profiles.py) where a test needs the people but no
database: the welcome's people step, dialogs. Like the real one it refuses a name someone has."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import Profile, ProfileColour
from app.services.profiles import ProfileError, clean_name, name_problem

CREATED = datetime(2026, 11, 1, tzinfo=UTC)


class FakePeople:
    """The people in memory: person 1 ("Me", blue) to begin with; `calls` records the changes."""

    def __init__(self, *people: Profile) -> None:
        self._people = list(people) or [Profile(id=1, created_at=CREATED)]
        self.current_id = self._people[0].id
        self.calls: list[tuple[str, int | str | None]] = []

    @property
    def people(self) -> tuple[Profile, ...]:
        return tuple(self._people)

    @property
    def current(self) -> Profile:
        return next(person for person in self._people if person.id == self.current_id)

    async def add(self, name: str, colour: ProfileColour) -> Profile:
        if (problem := name_problem(name, self._people)) is not None:
            raise ProfileError(problem)
        person = Profile(
            id=max(p.id for p in self._people) + 1,
            name=clean_name(name),
            colour=colour,
            created_at=CREATED,
        )
        self._people.append(person)
        self.calls.append(("add", person.name))
        return person

    async def update(self, profile_id: int, *, name: str | None, colour: ProfileColour) -> Profile:
        text = clean_name(name) or None
        if text is not None and (problem := name_problem(text, self._people, own=profile_id)):
            raise ProfileError(problem)
        index = next(i for i, person in enumerate(self._people) if person.id == profile_id)
        person = self._people[index].model_copy(update={"name": text, "colour": colour})
        self._people[index] = person
        self.calls.append(("update", profile_id))
        return person

    async def remove(self, profile_id: int) -> None:
        self._people = [person for person in self._people if person.id != profile_id]
        self.calls.append(("remove", profile_id))

    async def switch(self, profile_id: int) -> None:
        self.current_id = profile_id
        self.calls.append(("switch", profile_id))
