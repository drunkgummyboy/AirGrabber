"""A person as TMDB knows them (`TMDBClient.person`, `search_people`): the phone's actor page
(app/services/phone/person.py). Validated from TMDB's JSON in app/models/tmdb.py."""

from __future__ import annotations

from datetime import date

from app.models.domain import TMDB_IMAGE_BASE, DiscoverShow, DomainModel, Movie


class PersonMatch(DomainModel):
    """One answer of TMDB's /search/person."""

    id: int  # TMDB's person id
    name: str


class PersonCredit(DomainModel):
    """One part a person played (TMDB's combined_credits' cast): a film or a series."""

    title: Movie | DiscoverShow
    character: str = ""  # "" when TMDB names none

    @property
    def popularity(self) -> float:
        return self.title.popularity


class Person(DomainModel):
    """TMDB's /person/{id}, with the cast parts of its combined_credits in TMDB's order."""

    id: int
    name: str
    biography: str = ""  # in the client's language; "" when TMDB has none
    birthday: date | None = None
    deathday: date | None = None
    place_of_birth: str | None = None
    known_for: str | None = None  # TMDB's known_for_department ("Acting")
    profile_path: str | None = None
    credits: tuple[PersonCredit, ...] = ()

    def photo_url(self, size: str = "w185") -> str | None:
        return f"{TMDB_IMAGE_BASE}{size}{self.profile_path}" if self.profile_path else None
