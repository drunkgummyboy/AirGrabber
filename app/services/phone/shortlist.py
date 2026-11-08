"""Tonight's shortlist: what someone set aside with a star, shared by every phone at home.

`GET /v1/shortlist`, `PUT /v1/shortlist/{key}`, `DELETE /v1/shortlist/{key}`, `DELETE /v1/shortlist`:
each answers with the list as it now stands and its `version`, which moves only when something
changed. A phone that retries does no harm: adding what's there (the first one stays) and taking
off what isn't change nothing.

It lives in `Settings.phone_shortlist` (this PC's own, never in a backup), so it survives a restart;
one lock, because a change is worked out from the list as it stands. Open to every phone. Who added
it is for the PC to say, not the phone. A picture is kept only if it can't hold a secret: a plain
web address or a Plex path without a query, never a token. A `suggestion_id` is kept only if that
was offered (`Suggestions.offered`), else the row just can't be downloaded from: the id is what a
later tap goes by. The other fields are only for showing; nothing is ever searched for by them.

Nothing here logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeGuard
from urllib.parse import urlsplit

from app.i18n import t
from app.models import ShortItem
from app.services.phone.coming_up import person_gone, safe_poster
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.phone.suggestions import Suggestions
from app.services.profiles import ProfileService
from app.services.settings import SettingsService

MAX_ITEMS = 50  # "3 or 4" in practice; this only keeps the settings a sensible size
MAX_TEXT = 500  # characters of a key, a title, a picture's address or a Plex key
_YEARS = range(1800, 2201)


def _whole_number(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= MAX_TEXT


def _safe_picture(url: str) -> str | None:
    """A plain web address or a path on the Plex server, or nothing."""
    if len(url) > MAX_TEXT:
        return None
    try:
        if safe_poster(url):
            return url
        parts = urlsplit(url)
    except ValueError:
        return None
    a_path = url.startswith("/") and not (parts.netloc or parts.query or parts.fragment)
    return url if a_path else None


class Shortlist:
    def __init__(
        self,
        settings: SettingsService,
        profiles: ProfileService,
        suggestions: Suggestions,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """`now`: for tests."""
        self._settings = settings
        self._profiles = profiles
        self._suggestions = suggestions
        self._now = now
        self._lock = asyncio.Lock()

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {
            ("GET", "/v1/shortlist"): self._read,
            ("PUT", "/v1/shortlist/{key}"): self._add,
            ("DELETE", "/v1/shortlist/{key}"): self._take_off,
            ("DELETE", "/v1/shortlist"): self._clear,
        }

    def _answer(self) -> Reply:
        current = self._settings.current
        return Reply(
            200,
            {
                "version": current.phone_shortlist_version,
                "items": [item.model_dump(mode="json") for item in current.phone_shortlist],
            },
        )

    async def _save(self, items: tuple[ShortItem, ...]) -> None:
        """Call with the lock held, and only for a real change."""
        await self._settings.update(
            phone_shortlist=items,
            phone_shortlist_version=self._settings.current.phone_shortlist_version + 1,
        )

    async def _read(self, request: Request) -> Reply:
        return person_gone(request.phone, self._profiles) or self._answer()

    async def _add(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        item = self._item_from(request)
        if item is None:
            return bad_request()
        assert request.phone is not None
        if item.suggestion_id is not None and not await self._suggestions.offered(
            item.kind, item.suggestion_id, request.phone.profile_id
        ):
            item = item.model_copy(update={"suggestion_id": None})
        async with self._lock:
            items = self._settings.current.phone_shortlist
            if not any(row.key == item.key for row in items):
                if len(items) >= MAX_ITEMS:
                    return error_reply(
                        400, "bad_request", t("The shortlist is full, so clear it first.")
                    )
                await self._save((*items, item))
        return self._answer()

    async def _take_off(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        key = request.query["key"]
        async with self._lock:
            items = self._settings.current.phone_shortlist
            kept = tuple(row for row in items if row.key != key)
            if len(kept) != len(items):
                await self._save(kept)
        return self._answer()

    async def _clear(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        async with self._lock:
            if self._settings.current.phone_shortlist:
                await self._save(())
        return self._answer()

    def _item_from(self, request: Request) -> ShortItem | None:
        """The row the phone asks for, or None when it makes no sense."""
        assert request.phone is not None  # the server lets only paired phones through
        body = request.body
        key = request.query["key"]
        kind, title = body.get("kind"), body.get("title")
        year, suggestion_id = body.get("year"), body.get("suggestion_id")
        picture, plex_key = body.get("poster_url"), body.get("plex_key")
        if (
            not _text(key)
            or kind not in ("movie", "series")
            or not _text(title)
            or not (year is None or (_whole_number(year) and year in _YEARS))
            or not (suggestion_id is None or _whole_number(suggestion_id))
            or not (picture is None or isinstance(picture, str))
            or not (plex_key is None or _text(plex_key))
        ):
            return None
        person = self._profiles.get(request.phone.profile_id)
        return ShortItem(
            key=key,
            kind=kind,
            title=title,
            year=year,
            poster_url=_safe_picture(picture) if picture is not None else None,
            plex_key=plex_key,
            suggestion_id=suggestion_id,
            added_by=(person.name if person is not None else None) or request.phone.name,
            added_at=self._now(),
        )
