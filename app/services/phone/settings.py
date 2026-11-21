"""The phone's Settings screen, the PC's side: `GET /v1/settings` and `PATCH /v1/settings`
(docs/companion/2026-11-21-phone-settings-contract.md; `/v1/hello` says `"settings": true`).

`shared` is a few of AirGrabber's own `Settings` fields, by their own names: the subtitle
languages, the two "download automatically" switches, the tray menu's pause and the two
qualities, with every quality the Settings window offers (`quality_choices`, labelled in
English: the phone shows its own words). `phone` is the asking phone itself: its name, the
person whose lists it follows, the people there are, and whether it is a library manager's.

A PATCH sends any part of that. `shared` and `phone.profile_id` only from a library manager's
phone (403 `not_manager`); `phone.name` from any phone, for itself (cleaned up as Settings'
rename does, `PhoneService.change_phone`). All or nothing: every field is checked first (the
Settings model's own validators; switches strictly true / false; a quality one of
QualityProfile's values; subtitle languages a list that keeps at least one code AirGrabber
knows, unknown ones dropped as the model drops them; the pause an ISO time with a time zone or
null; a person who is there), and a bad one is 400 `bad_request` with one plain sentence, with
nothing stored. Then one `SettingsService.update` (the Settings window's own store, so an open
window shows the change at once, without its "Saved · Undo" bar) and the phone's own change.
A pause that has already ended is a resume; a longer one than the tray menu's longest is cut to
that (`longest_pause`). The answer is the whole GET body as it is afterward. Fields the phone
can't change (`quality_choices`, `profiles`, `manager`) may be sent back and change nothing; any
other field is a 400.

Logs: the phone's id and the names of the fields, never a value, a name or a language.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from app.i18n import N_, t
from app.models import PairedPhone, QualityProfile, Settings
from app.models.settings import QUALITY_NAMES
from app.services.auto_pause import longest_pause
from app.services.phone.server import (
    Handler,
    Reply,
    Request,
    bad_request,
    error_reply,
    not_manager,
)
from app.services.profiles import ProfileService
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

# The `shared` fields a phone may change: AirGrabber's own Settings fields.
SHARED_FIELDS = (
    "subtitle_languages",
    "auto_download_new_shows",
    "auto_download_new_films",
    "auto_paused_until",
    "quality_tv",
    "quality_movie",
)
_SWITCHES = frozenset({"auto_download_new_shows", "auto_download_new_films"})
_QUALITIES = frozenset({"quality_tv", "quality_movie"})
_QUALITY_IDS = frozenset(profile.value for profile in QualityProfile)
_SHARED_SHOWN = frozenset({"quality_choices"})  # in the answer only: sent back, it changes nothing
_PHONE_FIELDS = frozenset({"name", "profile_id"})
_PHONE_SHOWN = frozenset({"profiles", "manager"})

QUALITY_TEXT = N_("AirGrabber doesn't offer that quality, so choose another one.")
LANGUAGES_TEXT = N_("Choose at least one subtitle language that AirGrabber offers.")
PAUSE_TEXT = N_(
    "AirGrabber couldn't read that pause time, so try again or update the Companion app."
)
PERSON_TEXT = N_("That person isn't in AirGrabber any more, so choose someone else.")
UNUSABLE_TEXT = N_("AirGrabber couldn't use that setting, so try another one.")
ME_TEXT = N_("Me")  # the first person while they have no name (as the PC shows them)


class ChangePhone(Protocol):
    """`PhoneService.change_phone`: a phone's new name (cleaned up) or person."""

    def __call__(
        self, id: str, *, name: str | None = None, profile_id: int | None = None
    ) -> Awaitable[None]: ...


class _Refused(Exception):
    """A PATCH that can't be done: `reply` says why; `kind` is what the log gets."""

    def __init__(self, reply: Reply, kind: str) -> None:
        super().__init__(kind)
        self.reply = reply
        self.kind = kind


def _bad(kind: str, text: str | None = None) -> _Refused:
    reply = error_reply(400, "bad_request", t(text)) if text is not None else bad_request()
    return _Refused(reply, kind)


def _part(body: dict[str, Any], name: str, known: frozenset[str]) -> dict[str, Any]:
    """`body[name]` (a JSON object, {} when it isn't sent); a field it doesn't know is a 400."""
    part = body.get(name, {})
    if not isinstance(part, dict):
        raise _bad(f"{name} not an object")
    unknown = part.keys() - known
    if unknown:
        raise _bad(f"{len(unknown)} unknown field(s) in {name}")
    return part


def _whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class PhoneSettings:
    def __init__(
        self,
        settings: SettingsService,
        profiles: ProfileService,
        change_phone: ChangePhone,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """`change_phone`: PhoneService's (a phone's name and person, as Settings changes them).
        `now`: for tests."""
        self._settings = settings
        self._profiles = profiles
        self._change_phone = change_phone
        self._now = now

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("GET", "/v1/settings"): self._read, ("PATCH", "/v1/settings"): self._change}

    # -- the answer ------------------------------------------------------------------------------

    def _answer(self, phone: PairedPhone) -> Reply:
        current = self._settings.current
        return Reply(200, {"shared": self._shared(current), "phone": self._phone(phone)})

    def _shared(self, current: Settings) -> dict[str, Any]:
        until = current.auto_paused_until
        paused = until is not None and until > self._now()
        return {
            "subtitle_languages": list(current.subtitle_languages),
            "auto_download_new_shows": current.auto_download_new_shows,
            "auto_download_new_films": current.auto_download_new_films,
            # Local time with its offset; null when not paused (an ended pause is none).
            "auto_paused_until": (
                until.astimezone().isoformat(timespec="seconds")
                if until is not None and paused
                else None
            ),
            "quality_tv": current.quality_tv.value,
            "quality_movie": current.quality_movie.value,
            # The Settings window's menu, in its order, in English (the key is the English text).
            "quality_choices": [
                {"id": profile.value, "label": label} for profile, label in QUALITY_NAMES.items()
            ],
        }

    def _phone(self, phone: PairedPhone) -> dict[str, Any]:
        return {
            "name": phone.name,
            "profile_id": phone.profile_id,
            "profiles": [
                {"id": person.id, "name": person.name or t(ME_TEXT)}
                for person in self._profiles.people
            ],
            "manager": phone.manager,
        }

    def _as_stored(self, phone: PairedPhone) -> PairedPhone:
        """The phone as the settings hold it now (the request's copy is from before a change)."""
        return next(
            (one for one in self._settings.current.phone_devices if one.id == phone.id), phone
        )

    # -- the routes ------------------------------------------------------------------------------

    async def _read(self, request: Request) -> Reply:
        assert request.phone is not None  # the server lets only paired phones through
        return self._answer(self._as_stored(request.phone))

    async def _change(self, request: Request) -> Reply:
        phone = request.phone
        assert phone is not None  # the server lets only paired phones through
        try:
            unknown = request.body.keys() - {"shared", "phone"}
            if unknown:
                raise _bad(f"{len(unknown)} unknown field(s)")
            shared = _part(request.body, "shared", frozenset(SHARED_FIELDS) | _SHARED_SHOWN)
            own = _part(request.body, "phone", _PHONE_FIELDS | _PHONE_SHOWN)
            wanted = [name for name in SHARED_FIELDS if name in shared]
            if (wanted or "profile_id" in own) and not phone.manager:
                raise _Refused(not_manager(), "not a manager's phone")
            changes = self._checked(shared, wanted)
            name, profile_id = self._own_changes(own)
        except _Refused as refused:
            log.info("A phone's settings change was refused (%s): phone %s", refused.kind, phone.id)
            return refused.reply
        if changes:
            try:
                await self._settings.update(**changes)
            except ValidationError as exc:  # checked above; another change came in between
                log.info(
                    "A phone's settings change didn't validate: phone %s, %d error(s)",
                    phone.id,
                    exc.error_count(),
                )
                return error_reply(400, "bad_request", t(UNUSABLE_TEXT))
        if name is not None or profile_id is not None:
            await self._change_phone(phone.id, name=name, profile_id=profile_id)
        mine = (("name", name), ("profile_id", profile_id))
        fields = sorted([*changes, *(key for key, value in mine if value is not None)])
        if fields:
            log.info("Phone %s changed settings: %s", phone.id, ", ".join(fields))
        return self._answer(self._as_stored(phone))

    # -- checking --------------------------------------------------------------------------------

    def _checked(self, shared: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
        """The `shared` fields as `SettingsService.update` takes them, each checked; then all of
        them together through the Settings model, as `update` will."""
        changes: dict[str, Any] = {}
        for name in wanted:
            value = shared[name]
            if name in _SWITCHES:
                if not isinstance(value, bool):
                    raise _bad(f"{name} not true or false")
                changes[name] = value
            elif name in _QUALITIES:
                if not isinstance(value, str) or value not in _QUALITY_IDS:
                    raise _bad(f"{name} unknown", QUALITY_TEXT)
                changes[name] = QualityProfile(value)
            elif name == "subtitle_languages":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise _bad(f"{name} not a list of codes", LANGUAGES_TEXT)
                changes[name] = value
            else:  # auto_paused_until
                changes[name] = self._pause(value)
        if not changes:
            return changes
        try:
            tried = Settings.model_validate({**self._settings.current.model_dump(), **changes})
        except ValidationError as exc:
            raise _bad(f"{exc.error_count()} invalid field(s)", UNUSABLE_TEXT) from None
        if "subtitle_languages" in changes:
            if not tried.subtitle_languages:  # none AirGrabber knows (the model drops those)
                raise _bad("no known subtitle language", LANGUAGES_TEXT)
            changes["subtitle_languages"] = tried.subtitle_languages
        return changes

    def _pause(self, value: object) -> datetime | None:
        """When the pause ends (UTC), or None to resume: an ISO time with a time zone (one in
        the past resumes; a later one than the tray menu's longest is cut to that), or null."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise _bad("auto_paused_until not a time", PAUSE_TEXT)
        try:
            until = datetime.fromisoformat(value)
        except ValueError:
            raise _bad("auto_paused_until not a time", PAUSE_TEXT) from None
        if until.utcoffset() is None:
            raise _bad("auto_paused_until without a time zone", PAUSE_TEXT)
        now = self._now()
        if until <= now:
            return None
        return min(until, longest_pause(now)).astimezone(UTC)

    def _own_changes(self, own: dict[str, Any]) -> tuple[str | None, int | None]:
        """The phone's new name and person (None: as it is)."""
        name = own.get("name")
        if name is not None and not isinstance(name, str):
            raise _bad("name not a text")
        profile_id = own.get("profile_id")
        if profile_id is not None:
            if not _whole_number(profile_id):
                raise _bad("profile_id not a number")
            if self._profiles.get(profile_id) is None:
                raise _bad("profile_id unknown", PERSON_TEXT)
        return name, profile_id
