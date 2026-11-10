"""Send to phone: episodes and films put on a phone from the PC, and what each phone holds.

The PC keeps orders (`Settings.phone_orders`): `take` an item along (copy it from Plex, as the phone's own
"Take along" does) or `remove` it. A phone says what it holds with `PUT /v1/on-phone` (everything on it,
copying, ready or failed) and gets the orders still open for it in the answer. An order is done when a
report shows it: a `take` whose key is among the items (in any state), a `remove` whose key isn't. So an
answer that got lost, or a phone that asks twice, does no harm. The last report is kept per phone
(`Settings.phone_contents`) for the phone's menu in Settings.

A 4K film can go phone-sized (`phone_sized` in the order, the owner's wish, 2026-11-10): this PC starts
making the smaller copy at once (copies.py), so it is ready when the phone comes home, and the phone
copies that one from here instead of the film from Plex. Cancelling the order before the phone took it
drops the copy; a copy still wanted when AirGrabber starts again is started again (`resume_copies`).

Open to every paired phone (a library manager's or not), behind the "no Plex, no phone app" rule. Both
lists are this PC's own (never in a backup); disconnecting a phone drops its share (`DeviceStore.remove`).

The log gets ids and counts, never a title.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from app.models import PairedPhone, PhoneContents, PhoneItem, PhoneOrder, Settings
from app.models.plex import PlexEpisode, PlexTitle
from app.services.phone.server import Handler, Reply, Request, bad_request
from app.services.settings import SettingsService

if TYPE_CHECKING:
    from app.services.phone.copies import PhoneCopies

log = logging.getLogger(__name__)

MAX_ITEMS = 300  # what one phone may say it holds; MAX_BODY fits this many
MAX_TEXT = 200  # characters of a title or a show's name
MAX_KEY = 20  # characters of a Plex ratingKey
MAX_NUMBER = 10_000  # a season or an episode
MAX_SIZE = 10**13  # bytes: 10 TB
_KINDS = ("movie", "episode")
_STATES = ("copying", "ready", "failed")


def _whole_number(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _key(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= MAX_KEY and value.isascii() and value.isalnum()


def _text(value: object, *, needed: bool) -> str | None:
    """One line of at most MAX_TEXT characters; "" for a missing one that isn't needed."""
    if value is None and not needed:
        return ""
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(c if c.isprintable() else " " for c in value).split())[:MAX_TEXT]
    if needed and not cleaned:
        return None
    return cleaned


def _number(value: object) -> int | None | Literal[False]:
    """A season's or an episode's number, None for none, False for nonsense."""
    if value is None:
        return None
    if _whole_number(value) and 0 <= value <= MAX_NUMBER:
        return value
    return False


def read_item(data: object) -> PhoneItem | None:
    """One item of a report, or None when it makes no sense."""
    if not isinstance(data, dict):
        return None
    key, kind, state = data.get("key"), data.get("kind"), data.get("state")
    title = _text(data.get("title"), needed=True)
    show = _text(data.get("show"), needed=False)
    season, episode = _number(data.get("season")), _number(data.get("episode"))
    progress, size = data.get("progress", 0), data.get("size", 0)
    if (
        not _key(key)
        or kind not in _KINDS
        or state not in _STATES
        or title is None
        or show is None
        or season is False
        or episode is False
        or not _whole_number(progress)
        or not 0 <= progress <= 100
        or not _whole_number(size)
        or not 0 <= size <= MAX_SIZE
    ):
        return None
    return PhoneItem(
        key=key,
        kind=kind,
        title=title,
        show=show if kind == "episode" else "",
        season=season if kind == "episode" else None,
        episode=episode if kind == "episode" else None,
        state=state,
        progress=progress,
        size=size,
    )


def order_json(order: PhoneOrder) -> dict[str, Any]:
    return {
        "id": order.id,
        "key": order.key,
        "kind": order.kind,
        "title": order.title,
        "show": order.show,
        "season": order.season,
        "episode": order.episode,
        "phone_sized": order.phone_sized,
    }


def still_open(orders: Sequence[PhoneOrder], phone_id: str, held: set[str]) -> tuple[PhoneOrder, ...]:
    """`orders` without this phone's that its report of `held` keys shows done."""
    return tuple(
        order
        for order in orders
        if order.phone_id != phone_id
        or (order.action == "take" and order.key not in held)
        or (order.action == "remove" and order.key in held)
    )


class PhoneSends:
    def __init__(
        self,
        settings: SettingsService,
        *,
        copies: PhoneCopies | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """`copies`: the phone-sized copies (None: none). `now`: for tests."""
        self._settings = settings
        self._copies = copies
        self._now = now
        # A change is worked out from the lists as they stand, so one at a time.
        self._lock = asyncio.Lock()

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("PUT", "/v1/on-phone"): self._report}

    # -- what the PC shows ---------------------------------------------------------------------

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` after a change to what was sent or what a phone holds. Returns what
        unsubscribes it."""
        last = (self._settings.current.phone_orders, self._settings.current.phone_contents)

        def changed(current: Settings) -> None:
            nonlocal last
            now = (current.phone_orders, current.phone_contents)
            if now != last:
                last = now
                listener()

        return self._settings.subscribe(changed)

    def orders_for(self, phone_id: str) -> tuple[PhoneOrder, ...]:
        """What was sent to this phone (or is to be removed from it) and isn't done yet."""
        return tuple(o for o in self._settings.current.phone_orders if o.phone_id == phone_id)

    def contents_of(self, phone_id: str) -> PhoneContents | None:
        """This phone's last report; None before its first."""
        return next(
            (c for c in self._settings.current.phone_contents if c.phone_id == phone_id), None
        )

    def taken_keys(self, phone_id: str) -> frozenset[str]:
        """What is on this phone or on its way there: not to be sent again."""
        report = self.contents_of(phone_id)
        held = {item.key for item in report.items} if report is not None else set()
        for order in self.orders_for(phone_id):
            if order.action == "take":
                held.add(order.key)
            else:
                held.discard(order.key)
        return frozenset(held)

    def phone_sized_possible(self, film: PlexTitle) -> bool:
        """Whether this film can go phone-sized: a 4K film, and a PC whose card makes copies."""
        return (
            film.kind == "movie"
            and film.resolution == "4K"
            and self._copies is not None
            and self._copies.available
        )

    def copy_state(self, order: PhoneOrder) -> tuple[str, int, str | None] | None:
        """A phone-sized order's copy on this PC: its state ("waiting", "fetching",
        "converting", "ready", "failed"), progress and, failed, why. None: not phone-sized, or no
        copy (yet)."""
        if not order.phone_sized or self._copies is None:
            return None
        return self._copies.state_of(order.key)

    # -- what the PC does ----------------------------------------------------------------------

    async def send_film(self, phone: PairedPhone, film: PlexTitle, *, phone_sized: bool = False) -> int:
        """Put a film from the Library page on `phone`: 1, or 0 when it is there already.
        `phone_sized`: as this PC's phone-sized copy, started here at once."""
        added = await self._send(
            phone.id,
            [{"key": film.key, "kind": "movie", "title": film.title, "phone_sized": phone_sized}],
        )
        if added and phone_sized and self._copies is not None and any(
            o.phone_id == phone.id and o.key == film.key and o.action == "take" and o.phone_sized
            for o in self._settings.current.phone_orders
        ):
            self._copies.make(film.key)
        return added

    def resume_copies(self) -> None:
        """AirGrabber started again: the phone-sized copies still waiting for a phone, made
        (a finished one is there already)."""
        if self._copies is None:
            return
        for key in {o.key for o in self._settings.current.phone_orders if o.phone_sized}:
            self._copies.make(key)

    async def _drop_unwanted(self, keys: set[str]) -> None:
        """Copies of these films no open order wants any more: gone."""
        if self._copies is None:
            return
        wanted = {o.key for o in self._settings.current.phone_orders if o.phone_sized}
        for key in keys - wanted:
            await self._copies.drop(key)

    async def send_episodes(
        self, phone: PairedPhone, show: PlexTitle, episodes: Sequence[PlexEpisode]
    ) -> int:
        """Put these episodes of `show` on `phone`: how many weren't there or on their way."""
        return await self._send(
            phone.id,
            [
                {
                    "key": one.key,
                    "kind": "episode",
                    "title": one.title,
                    "show": show.title,
                    "season": one.season,
                    "episode": one.episode,
                }
                for one in episodes
            ],
        )

    async def _send(self, phone_id: str, wanted: list[dict[str, Any]]) -> int:
        async with self._lock:
            current = self._settings.current
            if not any(phone.id == phone_id for phone in current.phone_devices):
                return 0
            report = self.contents_of(phone_id)
            held = {item.key for item in report.items} if report is not None else set()
            orders = list(current.phone_orders)
            mine = [order for order in orders if order.phone_id == phone_id]
            taking = {order.key for order in mine if order.action == "take"}
            removing = {order.key: order for order in mine if order.action == "remove"}
            added = 0
            for fields in wanted:
                key = fields["key"]
                if key in removing:  # on its way off the phone: it stays after all
                    orders.remove(removing.pop(key))
                    added += 1
                    continue
                if key in held or key in taking:
                    continue
                orders.append(
                    PhoneOrder(
                        id=uuid.uuid4().hex,
                        phone_id=phone_id,
                        action="take",
                        added_at=self._now(),
                        **fields,
                    )
                )
                taking.add(key)
                added += 1
            if added:
                await self._settings.update(phone_orders=tuple(orders))
        log.info("Sent %d item(s) to phone %s", added, phone_id)
        return added

    async def cancel(self, order_id: str) -> None:
        """Take back an order the phone hasn't carried out yet (and its phone-sized copy). An
        unknown one changes nothing."""
        async with self._lock:
            orders = self._settings.current.phone_orders
            kept = tuple(order for order in orders if order.id != order_id)
            if len(kept) != len(orders):
                await self._settings.update(phone_orders=kept)
            dropped = {o.key for o in orders if o.id == order_id and o.phone_sized}
            await self._drop_unwanted(dropped)

    async def remove(self, phone_id: str, key: str) -> None:
        """Have this item taken off the phone the next time it checks in; one still waiting to
        go there is simply not sent."""
        async with self._lock:
            current = self._settings.current
            orders = list(current.phone_orders)
            waiting = [
                order
                for order in orders
                if order.phone_id == phone_id and order.key == key and order.action == "take"
            ]
            for order in waiting:
                orders.remove(order)
            report = self.contents_of(phone_id)
            item = next((one for one in report.items if one.key == key), None) if report else None
            asked = any(
                order.phone_id == phone_id and order.key == key and order.action == "remove"
                for order in orders
            )
            if item is not None and not asked:
                orders.append(
                    PhoneOrder(
                        id=uuid.uuid4().hex,
                        phone_id=phone_id,
                        action="remove",
                        key=key,
                        kind=item.kind,
                        title=item.title,
                        show=item.show,
                        season=item.season,
                        episode=item.episode,
                        added_at=self._now(),
                    )
                )
            if tuple(orders) != current.phone_orders:
                await self._settings.update(phone_orders=tuple(orders))
            await self._drop_unwanted({o.key for o in waiting if o.phone_sized})

    # -- the phone's side ----------------------------------------------------------------------

    async def _report(self, request: Request) -> Reply:
        phone = request.phone
        assert phone is not None  # the server lets only paired phones through
        raw = request.body.get("items")
        if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
            return bad_request()
        items: list[PhoneItem] = []
        seen: set[str] = set()
        for data in raw:
            item = read_item(data)
            if item is None:
                return bad_request()
            if item.key not in seen:  # the first one counts
                seen.add(item.key)
                items.append(item)
        async with self._lock:
            current = self._settings.current
            if not any(one.id == phone.id for one in current.phone_devices):
                return Reply(200, {"take": [], "remove": []})  # disconnected meanwhile
            report = PhoneContents(phone_id=phone.id, items=tuple(items), reported_at=self._now())
            orders = still_open(current.phone_orders, phone.id, seen)
            await self._settings.update(
                phone_orders=orders,
                phone_contents=(
                    *(c for c in current.phone_contents if c.phone_id != phone.id),
                    report,
                ),
            )
        mine = [order for order in orders if order.phone_id == phone.id]
        log.debug("Phone %s holds %d item(s), %d order(s) open", phone.id, len(items), len(mine))
        return Reply(
            200,
            {
                "take": [order_json(o) for o in mine if o.action == "take"],
                "remove": [order_json(o) for o in mine if o.action == "remove"],
            },
        )
