"""The phone's actor page: `GET /v1/person?name=<name>[&tmdb_id=<id>]`
(docs/companion/2026-11-18-person-progress-design.md).

The phone has a Plex role's name and sometimes a TMDB person id. With an id, TMDB's
`/person/{id}` (its combined_credits appended, one call); not found there, or without one, by
name: TMDB's `/search/person`, the first answer whose name is the one asked for once accents and
case are folded (search.py's `normalize_text`), else TMDB's first, then `/person/{id}`. An empty
`tmdb_id=` is no id. No TMDB key: 409 `no_tmdb_key`; a key TMDB refuses: 409 `tmdb_key_refused`;
TMDB out of reach: 502 `failed`; each with `/v1/find`'s sentence. The biography comes in the
client's language (English), as find's titles and overviews do.

The credits: the person's cast parts (not crew), films and series, each once (the first
character kept); not series of TMDB's Talk, News or Reality genres, not parts as themself (the
character starts with "Self", "Himself", "Herself" or "Themselves" as a whole word: "Self - Host",
"Himself (voice)"; "Future Self" and "Selfridge" are parts), not one without a title, not one TMDB
flags adult (dropped when the credits are read, app/models/tmdb.py). TMDB's most
popular first, at most PERSON_CREDITS. Each is exactly a `/v1/find` result (find.py's
`items_of`, which also remembers it as found, so a download, a follow or an episode list takes it
as any search result) plus its `character` and `popularity`.

The log gets counts only: never the name, a TMDB id, a title or a character.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.i18n import t
from app.models import DiscoverShow, Movie
from app.models.person import Person, PersonCredit, PersonMatch
from app.services.discovery import DiscoveryService
from app.services.phone.coming_up import person_gone, safe_poster
from app.services.phone.find import (
    PhoneFind,
    couldnt_search,
    key_refused_message,
    no_key_message,
    tmdb_trouble,
)
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.profiles import ProfileService
from app.services.search import normalize_text
from app.services.tmdb import TMDBError, TMDBNotFound

log = logging.getLogger(__name__)

PERSON_CREDITS = 40
LONGEST_NAME = 100
_ID_DIGITS = 10
_PHOTO_SIZE = "w342"
# TMDB's TV genres that are no part played: Talk, News, Reality.
_NOT_A_PART = frozenset({10767, 10763, 10764})
# At the start of the folded character, a whole word ("Self - Host", "Himself (voice)"); English
# only, as TMDB writes these, and not "Themself", which TMDB hardly uses. A part merely named
# after one ("Future Self") is a part.
_THEMSELF = re.compile(r"(?:self|himself|herself|themselves)\b")


def _plays_themself(character: str) -> bool:
    return _THEMSELF.match(normalize_text(character)) is not None


def _counts(credit: PersonCredit) -> bool:
    """A part the actor page lists."""
    title = credit.title
    name = title.title if isinstance(title, Movie) else title.name
    if not name.strip() or _plays_themself(credit.character):
        return False
    return not (isinstance(title, DiscoverShow) and _NOT_A_PART.intersection(title.genre_ids))


def credits_of(person: Person) -> list[PersonCredit]:
    """The parts to list: each title once (its first part), the most popular first."""
    seen: set[tuple[str, int]] = set()
    kept: list[PersonCredit] = []
    for credit in person.credits:
        key = ("movie" if isinstance(credit.title, Movie) else "series", credit.title.id)
        if key in seen or not _counts(credit):
            continue
        seen.add(key)
        kept.append(credit)
    kept.sort(key=lambda credit: credit.popularity, reverse=True)
    return kept[:PERSON_CREDITS]


def pick(found: list[PersonMatch], name: str) -> PersonMatch | None:
    """The answer named exactly `name` (folded), else TMDB's first; None without any."""
    wanted = normalize_text(name)
    named = (one for one in found if normalize_text(one.name) == wanted)
    return next(named, found[0] if found else None)


def _not_found(name: str) -> Reply:
    return error_reply(404, "not_found", t("AirGrabber couldn't find {name} on TMDB.", name=name))


def _no_key() -> Reply:
    return error_reply(409, "no_tmdb_key", no_key_message())


def _not_asked(failure: TMDBError) -> Reply:
    trouble = tmdb_trouble([failure], route="Phone person")
    if trouble == "no_key":
        return _no_key()
    if trouble == "key_refused":
        return error_reply(409, "tmdb_key_refused", key_refused_message())
    return couldnt_search()


class PhonePerson:
    def __init__(
        self, find: PhoneFind, discovery: DiscoveryService, profiles: ProfileService
    ) -> None:
        self._find = find
        self._discovery = discovery
        self._profiles = profiles

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("GET", "/v1/person"): self._person}

    async def _person(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        name = request.query.get("name", "").strip()
        raw_id = request.query.get("tmdb_id") or None  # an empty one is none
        if not 1 <= len(name) <= LONGEST_NAME:
            return bad_request()
        if raw_id is not None and not (
            raw_id.isascii() and raw_id.isdigit() and len(raw_id) <= _ID_DIGITS
        ):
            return bad_request()
        if not self._discovery.has_tmdb_key:
            return _no_key()
        try:
            person = await self._look_up(name, int(raw_id) if raw_id is not None else None)
        except TMDBError as exc:
            return _not_asked(exc)
        if person is None:
            log.info("Phone person: not on TMDB")
            return _not_found(name)
        credits = credits_of(person)
        films = [credit.title for credit in credits if isinstance(credit.title, Movie)]
        shows = [credit.title for credit in credits if isinstance(credit.title, DiscoverShow)]
        film_items, series_items = await self._find.items_of(
            films, shows, request.phone.profile_id
        )
        items = {(item["kind"], item["id"]): item for item in (*film_items, *series_items)}
        log.info(
            "Phone person: %d credits listed (%d films, %d series) of %d",
            len(credits), len(films), len(shows), len(person.credits),
        )
        return Reply(
            200,
            {
                "id": person.id,
                "name": person.name,
                "photo_url": safe_poster(person.photo_url(_PHOTO_SIZE)),
                "biography": person.biography,
                "birthday": person.birthday.isoformat() if person.birthday else None,
                "deathday": person.deathday.isoformat() if person.deathday else None,
                "place_of_birth": person.place_of_birth,
                "known_for": person.known_for,
                "credits": [_credit_item(credit, items) for credit in credits],
            },
        )

    async def _look_up(self, name: str, tmdb_id: int | None) -> Person | None:
        """By TMDB id; not found there (or without one), by name. None: TMDB knows no one."""
        if tmdb_id is not None:
            try:
                return await self._discovery.person(tmdb_id)
            except TMDBNotFound:
                log.info("Phone person: the id isn't on TMDB, so by name")
        match = pick(await self._discovery.search_people(name), name)
        if match is None:
            return None
        try:
            return await self._discovery.person(match.id)
        except TMDBNotFound:
            return None


def _credit_item(
    credit: PersonCredit, items: dict[tuple[str, int], dict[str, Any]]
) -> dict[str, Any]:
    kind = "movie" if isinstance(credit.title, Movie) else "series"
    return {
        **items[(kind, credit.title.id)],
        "character": credit.character,
        "popularity": credit.popularity,
    }
