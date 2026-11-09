"""A phone's menu ("What's on it…" in Settings → Phone connection; the owner's wish, 2026-11-08).

What the phone last said it holds (copying, ready or failed, with when it said so) and what was sent to it
from the PC and is still waiting to go. A show is one row, folded (the owner, 2026-11-09: "no individual
episodes, but shows and episodes under a folded menu"): its name, how many episodes and their size, and
Remove all / Cancel all; a click on it unfolds its episodes, each with its own button. A film is a row
of its own. Cancel takes back what is waiting; Remove has the phone delete an item the next time it
checks in (until then the row says so, with Cancel). It follows changes while open
(`PhoneSends.subscribe`): a phone checking in updates it, unfolded shows stay unfolded. Borderless in
AirGrabber's style; Escape, the ✕ and Close close it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

import customtkinter as ctk

from app.i18n import N_, number, t, tn
from app.models import PairedPhone, PhoneItem, PhoneOrder
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    FitScroll,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    max_dialog_height,
    place_over,
    secondary_button,
    text_label,
)
from app.ui.tasks import spawn
from app.ui.views.downloads_view import when_text

if TYPE_CHECKING:
    from app.services.phone.sends import PhoneSends

TITLE_TEXT = N_("On {phone}")
HEARD_TEXT = N_("Last heard from this phone: {when}.")
NOT_HEARD_TEXT = N_(
    "Not heard from this phone yet. It checks in when the Companion app is open at home."
)
WAITING_TITLE = N_("Waiting to go")
WAITING_NOTE = N_("The phone copies these from Plex the next time the Companion app is open at home.")
ON_PHONE_TITLE = N_("On the phone")
NOTHING_TEXT = N_("Nothing on this phone yet. Send episodes and films from the Library page.")
COPYING_TEXT = N_("Copying {percent}%")
FAILED_TEXT = N_("Couldn't copy")
REMOVING_TEXT = N_("Being removed")
CANCEL_TEXT = N_("Cancel")
REMOVE_TEXT = N_("Remove")
CANCEL_ALL_TEXT = N_("Cancel all")
REMOVE_ALL_TEXT = N_("Remove all")
COPYING_COUNT_TEXT = N_("{n} copying")
FAILED_COUNT_TEXT = N_("{n} couldn't copy")
FOLDED = "▸"
UNFOLDED = "▾"
EPISODE_INDENT = 26  # an unfolded show's episodes, in from its row
CLOSE_TEXT = N_("Close")
WIDTH = 560  # the text's width (CTk units)
ROW_BUTTON_WIDTH = 100
BUTTON_WIDTH = 120
LIST_ROOM = 160  # CTk units kept for the heading and the Close row when the list scrolls


def episode_code(season: int | None, episode: int | None) -> str:
    """"S04E01", "S04" without a number, "" without a season."""
    if season is None:
        return ""
    if episode is None:
        return f"S{season:02d}"
    return f"S{season:02d}E{episode:02d}"


_CODE = re.compile(r"S\d+E\d+(?:-?E?\d+)*", re.IGNORECASE)


def item_label(item: PhoneItem | PhoneOrder) -> str:
    """"The Boys · S04E01 · Episode title" for an episode, the title for a film. The phone
    keeps no episode titles: what it sends as one is the code ("S04E01", "S01E01-E02"), which
    isn't shown twice."""
    if item.kind == "movie":
        return item.title
    code = episode_code(item.season, item.episode)
    title = item.title
    if code and _CODE.fullmatch(title.strip()):
        title = "" if title.strip().upper().startswith(code) and "-" not in title else title.strip()
        code = "" if title else code
    parts = [item.show, code, title]
    return " · ".join(part for part in parts if part)


def size_text(size: int) -> str:
    """"1.4 GB", "700 MB"; "" without a size."""
    if size <= 0:
        return ""
    if size >= 1024**3:
        return f"{number(size / 1024**3, '.1f')} GB"
    return f"{max(1, round(size / 1024**2))} MB"


def state_text(item: PhoneItem, removing: bool) -> str:
    """What the row says besides the title: being removed, copying, failed, or its size."""
    if removing:
        return t(REMOVING_TEXT)
    if item.state == "copying":
        return t(COPYING_TEXT, percent=item.progress)
    if item.state == "failed":
        return t(FAILED_TEXT)
    return size_text(item.size)


def heard_text(reported_at: datetime | None, now: datetime) -> str:
    if reported_at is None:
        return t(NOT_HEARD_TEXT)
    return t(HEARD_TEXT, when=when_text(reported_at, now))


def _sort_key(item: PhoneItem | PhoneOrder) -> tuple[object, ...]:
    """Films and shows by name, a show's episodes in order."""
    name = (item.show or item.title).casefold()
    return (name, item.season or 0, item.episode or 0, item.title.casefold())


def episode_label(item: PhoneItem | PhoneOrder) -> str:
    """An episode under its show's row: "S04E01", or "S04E01 · Title"."""
    return item_label(item.model_copy(update={"show": ""}))


Entry = TypeVar("Entry", PhoneItem, PhoneOrder)


def grouped(entries: list[Entry]) -> list[tuple[str, list[Entry]]]:
    """By name: a show's episodes together under its name, a film alone under "" (any order in;
    each group's episodes in order)."""
    groups: list[tuple[str, list[Entry]]] = []
    shows: dict[str, list[Entry]] = {}
    for entry in sorted(entries, key=_sort_key):
        if entry.kind == "episode" and entry.show:
            key = entry.show.casefold()
            if key not in shows:
                shows[key] = []
                groups.append((entry.show, shows[key]))
            shows[key].append(entry)
        else:
            groups.append(("", [entry]))
    return groups


def show_summary(items: list[PhoneItem], removing: int = 0) -> str:
    """A show's row: "7 episodes · 5.7 GB", what is copying or failed, or that it is going."""
    if items and removing >= len(items):
        return t(REMOVING_TEXT)
    parts = [tn("{n} episode", "{n} episodes", len(items))]
    if size := size_text(sum(item.size for item in items)):
        parts.append(size)
    copying = sum(item.state == "copying" for item in items)
    failed = sum(item.state == "failed" for item in items)
    if copying:
        parts.append(t(COPYING_COUNT_TEXT, n=copying))
    if failed:
        parts.append(t(FAILED_COUNT_TEXT, n=failed))
    return " · ".join(parts)


class PhoneContentsWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        *,
        phone: PairedPhone,
        sends: PhoneSends,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """`now`: for tests."""
        super().__init__(master, fg_color=theme.BG)
        self.title(t(TITLE_TEXT, phone=phone.name))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._phone = phone
        self._sends = sends
        self._now = now
        self._tasks: set[asyncio.Task[None]] = set()
        # Each row's button by what it acts on, for tests: an order's id, an item's key, or a
        # show's "show:<name>" (its Remove all / Cancel all) and "fold:<name>" (its row).
        self.buttons: dict[str, ctk.CTkButton] = {}
        self.rows: list[str] = []  # what each row says, top to bottom (tests)
        self._unfolded: set[str] = set()  # shows unfolded, by section and name

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, t(TITLE_TEXT, phone=phone.name), right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        self.heard = text_label(body, "", size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH)
        self.heard.pack(anchor="w", pady=(LINE_GAP, 0))
        self._scroll = FitScroll(body, follow=True)
        self._scroll.pack(fill="both", expand=True, pady=(LINE_GAP + 8, 0))
        self._list = ctk.CTkFrame(self._scroll, fg_color="transparent")
        self._list.grid(row=0, column=0, sticky="ew")
        self._list.grid_columnconfigure(0, weight=1)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        self.close_button = secondary_button(buttons, t(CLOSE_TEXT), self.close, width=BUTTON_WIDTH)
        self.close_button.pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        self._unsubscribe = sends.subscribe(self.refresh)
        self.refresh()
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        self._scroll.set_limit(round((max_dialog_height(master, scaling) - LIST_ROOM) * scaling))
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    # -- what it shows ---------------------------------------------------------------------------

    def refresh(self) -> None:
        if not self.winfo_exists():
            return
        report = self._sends.contents_of(self._phone.id)
        orders = self._sends.orders_for(self._phone.id)
        self.heard.configure(
            text=heard_text(report.reported_at if report is not None else None, self._now())
        )
        for child in self._list.winfo_children():
            child.destroy()
        self.buttons.clear()
        self.rows.clear()
        row = 0
        waiting = [o for o in orders if o.action == "take"]
        removing = {o.key: o for o in orders if o.action == "remove"}
        items = list(report.items if report is not None else ())
        if waiting:
            row = self._title(row, t(WAITING_TITLE))
            text_label(
                self._list, t(WAITING_NOTE), size=DETAIL_SIZE, color=theme.TEXT_MUTED,
                wraplength=WIDTH,
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 4))
            row += 1
            for show, group in grouped(waiting):
                if show:
                    fold = f"waiting:{show.casefold()}"
                    summary = tn("{n} episode", "{n} episodes", len(group))
                    ids = [order.id for order in group]
                    row = self._show_row(
                        row, show, summary, fold, t(CANCEL_ALL_TEXT),
                        partial(self._cancel_all, ids),
                    )
                    if fold not in self._unfolded:
                        continue
                for order in group:
                    row = self._row(
                        row, episode_label(order) if show else item_label(order), "", order.id,
                        t(CANCEL_TEXT), partial(self._cancel, order.id), indent=bool(show),
                    )
        if items:
            row = self._title(row, t(ON_PHONE_TITLE), first=not waiting)
            for show, items_of in grouped(items):
                if show:
                    fold = f"on:{show.casefold()}"
                    leaving_ids = [removing[i.key].id for i in items_of if i.key in removing]
                    if len(leaving_ids) == len(items_of):
                        text, action = t(CANCEL_TEXT), partial(self._cancel_all, leaving_ids)
                    else:
                        staying = [i.key for i in items_of if i.key not in removing]
                        text, action = t(REMOVE_ALL_TEXT), partial(self._remove_all, staying)
                    row = self._show_row(
                        row, show, show_summary(items_of, len(leaving_ids)), fold, text, action,
                        failed=any(i.state == "failed" for i in items_of),
                    )
                    if fold not in self._unfolded:
                        continue
                for item in items_of:
                    leaving = removing.get(item.key)
                    if leaving is not None:
                        action, text = partial(self._cancel, leaving.id), t(CANCEL_TEXT)
                    else:
                        action, text = partial(self._remove, item.key), t(REMOVE_TEXT)
                    row = self._row(
                        row, episode_label(item) if show else item_label(item),
                        state_text(item, leaving is not None), item.key, text, action,
                        failed=item.state == "failed" and leaving is None, indent=bool(show),
                    )
        if not waiting and not items:
            text_label(
                self._list, t(NOTHING_TEXT), size=BODY_SIZE, color=theme.TEXT_MUTED,
                wraplength=WIDTH,
            ).grid(row=0, column=0, sticky="w")
            self.rows.append(t(NOTHING_TEXT))
        self._scroll.refit()

    def _title(self, row: int, text: str, *, first: bool = True) -> int:
        text_label(self._list, text, size=BODY_SIZE, bold=True, wraplength=WIDTH).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0 if first else 18, 0)
        )
        return row + 1

    def _row(
        self,
        row: int,
        label: str,
        state: str,
        acts_on: str,
        button_text: str,
        action: Callable[[], None],
        *,
        failed: bool = False,
        indent: bool = False,
    ) -> int:
        text_label(
            self._list, label, size=DETAIL_SIZE, color=theme.TEXT_MUTED if indent else theme.TEXT,
            wraplength=WIDTH - ROW_BUTTON_WIDTH - 140 - (EPISODE_INDENT if indent else 0),
        ).grid(row=row, column=0, sticky="w", pady=(6, 0), padx=(EPISODE_INDENT if indent else 0, 0))
        ctk.CTkLabel(
            self._list, text=state, font=theme.font(DETAIL_SIZE),
            text_color=theme.DANGER if failed else theme.TEXT_MUTED,
        ).grid(row=row, column=1, sticky="e", padx=(12, 12), pady=(6, 0))
        button = secondary_button(self._list, button_text, action, width=ROW_BUTTON_WIDTH)
        button.grid(row=row, column=2, sticky="e", pady=(6, 0))
        self.buttons[acts_on] = button
        self.rows.append(f"{'  ' if indent else ''}{label} | {state}".rstrip(" |"))
        return row + 1

    def _show_row(
        self,
        row: int,
        show: str,
        summary: str,
        fold: str,
        button_text: str,
        action: Callable[[], None],
        *,
        failed: bool = False,
    ) -> int:
        """A show's folded row: ▸ and its name (a click unfolds its episodes), the summary, and
        the button for all of them."""
        unfolded = fold in self._unfolded
        toggle = ctk.CTkButton(
            self._list,
            text=f"{UNFOLDED if unfolded else FOLDED}  {show}",
            command=partial(self._toggle, fold),
            anchor="w",
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.TEXT,
            font=theme.font(DETAIL_SIZE, bold=True),
            height=32,
            corner_radius=8,
            border_spacing=4,
        )
        toggle.grid(row=row, column=0, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            self._list, text=summary, font=theme.font(DETAIL_SIZE),
            text_color=theme.DANGER if failed else theme.TEXT_MUTED,
        ).grid(row=row, column=1, sticky="e", padx=(12, 12), pady=(6, 0))
        button = secondary_button(self._list, button_text, action, width=ROW_BUTTON_WIDTH)
        button.grid(row=row, column=2, sticky="e", pady=(6, 0))
        name = fold.partition(":")[2]
        self.buttons[f"fold:{name}"] = toggle
        self.buttons[f"show:{name}"] = button
        self.rows.append(f"{UNFOLDED if unfolded else FOLDED} {show} | {summary}")
        return row + 1

    def _toggle(self, fold: str) -> None:
        self._unfolded.symmetric_difference_update({fold})
        self.refresh()

    # -- what it does ----------------------------------------------------------------------------

    def _cancel(self, order_id: str) -> None:
        self._start(self._sends.cancel(order_id), "phone-cancel")

    def _remove(self, key: str) -> None:
        self._start(self._sends.remove(self._phone.id, key), "phone-remove")

    def _remove_all(self, keys: list[str]) -> None:
        async def remove() -> None:
            for key in keys:
                await self._sends.remove(self._phone.id, key)

        self._start(remove(), "phone-remove-all")

    def _cancel_all(self, order_ids: list[str]) -> None:
        async def cancel() -> None:
            for order_id in order_ids:
                await self._sends.cancel(order_id)

        self._start(cancel(), "phone-cancel-all")

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settled(self) -> None:
        """Wait for what a click started (tests)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks))

    def close(self) -> None:
        self._unsubscribe()
        if self.winfo_exists():
            self.destroy()
