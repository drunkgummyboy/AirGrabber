"""The paired phones and the one-time code that pairs a new one.

A phone's token is shown to it once, when it pairs; only its SHA-256 hash is kept. Never log a
token or a code.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from app.models import PairedPhone
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

CODE_LIFETIME = timedelta(minutes=5)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


class DeviceStore:
    """The paired phones, kept in the settings (`phone_devices`)."""

    def __init__(self, settings: SettingsService) -> None:
        self._settings = settings
        # A change is worked out from the list as it stands, so one at a time: a second one
        # started while the first is being saved would otherwise undo it.
        self._lock = asyncio.Lock()

    async def add(
        self, name: str, profile_id: int, *, manager: bool = False
    ) -> tuple[PairedPhone, str]:
        """Pair a phone. The token comes back only here: it can't be read again later."""
        token = secrets.token_urlsafe(32)
        phone = PairedPhone(
            id=uuid.uuid4().hex,
            name=name,
            token_hash=_token_hash(token),
            profile_id=profile_id,
            paired_at=datetime.now(UTC),
            manager=manager,
        )
        async with self._lock:
            await self._settings.update(
                phone_devices=(*self._settings.current.phone_devices, phone)
            )
        log.info("Paired phone %s", phone.id)
        return phone, token

    def find(self, token: str) -> PairedPhone | None:
        """The phone this token belongs to, or None for an unknown or disconnected one."""
        wanted = _token_hash(token)
        for phone in self._settings.current.phone_devices:
            if _same(phone.token_hash, wanted):
                return phone
        return None

    async def remove(self, id: str) -> None:
        """Disconnect a phone; its token stops working at once, and what was sent to it and
        what it said it holds go with it. An unknown id changes nothing."""
        async with self._lock:
            current = self._settings.current
            await self._settings.update(
                phone_devices=tuple(phone for phone in current.phone_devices if phone.id != id),
                phone_orders=tuple(order for order in current.phone_orders if order.phone_id != id),
                phone_contents=tuple(
                    report for report in current.phone_contents if report.phone_id != id
                ),
            )
        log.info("Removed phone %s", id)

    async def change(
        self,
        id: str,
        *,
        name: str | None = None,
        manager: bool | None = None,
        profile_id: int | None = None,
    ) -> PairedPhone | None:
        """A phone's new name, role or person (None: as it was); None for an unknown phone.
        The server reads the phone at every request, so a new role counts at once."""
        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = name
        if manager is not None:
            changes["manager"] = manager
        if profile_id is not None:
            changes["profile_id"] = profile_id
        async with self._lock:
            phones = self._settings.current.phone_devices
            found = next((phone for phone in phones if phone.id == id), None)
            if found is None:
                return None
            changed = found.model_copy(update=changes)
            if changed != found:
                await self._settings.update(
                    phone_devices=tuple(changed if phone.id == id else phone for phone in phones)
                )
                log.info("Changed phone %s", id)
            return changed


class PairingCode:
    """One code at a time, good for one use within 5 minutes; a new one cancels the old one.

    The code travels in the picture the phone scans and is never typed, so it can be long enough
    that guessing is hopeless. That's why a wrong try leaves it working: nobody on the network
    can spoil a pairing by sending wrong codes.
    """

    def __init__(self) -> None:
        self._code: str | None = None
        self._expires: datetime | None = None

    def new(self, now: datetime) -> str:
        self._code = secrets.token_urlsafe(16)
        self._expires = now + CODE_LIFETIME
        return self._code

    def take(self, code: str, now: datetime) -> bool:
        """Whether `code` is the current code; a good one is used up."""
        if self._code is None or self._expires is None:
            return False
        if now >= self._expires:
            self._code = self._expires = None
            return False
        if not _same(self._code, code):
            return False
        self._code = self._expires = None
        return True
