"""📱 Send to phone, from a Library card (the owner's wish, 2026-11-08: from the Windows application).

First the person, then, if they have more than one phone, which phone (owner). The person selected in
AirGrabber comes first when they have a phone. A film goes as it is; a show lists its episodes as the
Plex server has them now, by season, to tick episodes or whole seasons. What is on the chosen phone or on
its way there already shows ticked and greyed, and isn't sent twice. Send hands it to
`PhoneSends` and closes; the phone copies it from Plex the next time the Companion app is open at home.
A 4K film, on a PC whose card makes phone-sized copies, gets the Phone-sized tick (on to start with):
then this PC makes the ~5 GB 1080p copy at once and the phone copies that one from here.
Borderless in AirGrabber's style; Escape, the ✕ and Cancel close it without sending.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import PairedPhone, Profile
from app.models.plex import PlexEpisode, PlexTitle
from app.services.plex import PlexError
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    BUTTON_HEIGHT,
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
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.people import person_name
from app.ui.phone_contents import episode_code
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.services.phone.sends import PhoneSends

TITLE_TEXT = N_("Send to phone")
TO_TEXT = N_("To")
ONE_PHONE_TEXT = N_("To {phone} ({person})")
HOW_TEXT = N_("The phone copies it from Plex the next time the Companion app is open at home.")
THERE_TEXT = N_("Already on this phone or on its way.")
PHONE_SIZED_TEXT = N_("Phone-sized: about 5 GB in 1080p instead of the whole 4K film")
PHONE_SIZED_HOW_TEXT = N_(
    "This PC makes the smaller copy now, and the phone copies it from here the next time the "
    "Companion app is open at home."
)
LOADING_TEXT = N_("Reading the episodes from Plex…")
PLEX_AWAY_TEXT = N_("Plex didn't answer, so try again in a moment.")
NO_EPISODES_TEXT = N_("Plex has no episodes of this show.")
SEASON_TEXT = N_("Season {n}")
SPECIALS_TEXT = N_("Specials")
ON_PHONE_TEXT = N_("on the phone")
CHOSEN_ONE = N_("{n} episode chosen")
CHOSEN_MORE = N_("{n} episodes chosen")
SEND_TEXT = N_("Send")
SENT_FILM_TEXT = N_("Sent {title} to {phone}.")
SENT_ONE = N_("Sent {n} episode to {phone}.")
SENT_MORE = N_("Sent {n} episodes to {phone}.")
WIDTH = 520  # the text's width (CTk units)
MENU_WIDTH = 200
BUTTON_WIDTH = 120
LIST_ROOM = 300  # CTk units kept for everything around the episode list


def phones_by_person(
    phones: Sequence[PairedPhone], people: Sequence[Profile]
) -> dict[int, list[PairedPhone]]:
    """The people who have a phone, in AirGrabber's order, with their phones."""
    found: dict[int, list[PairedPhone]] = {}
    for person in people:
        theirs = [phone for phone in phones if phone.profile_id == person.id]
        if theirs:
            found[person.id] = theirs
    return found


def season_text(season: int | None) -> str:
    if not season:
        return t(SPECIALS_TEXT)
    return t(SEASON_TEXT, n=season)


def episode_text(episode: PlexEpisode) -> str:
    """"S02E03 · Title"."""
    code = episode_code(episode.season, episode.episode)
    return " · ".join(part for part in (code, episode.title) if part)


class SendToPhoneDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        *,
        title: PlexTitle,
        phones: Sequence[PairedPhone],
        people: Sequence[Profile],
        current_person: int,
        sends: PhoneSends,
        episodes: Callable[[PlexTitle], Awaitable[list[PlexEpisode]]],
        on_sent: Callable[[str], None],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(t(TITLE_TEXT))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._item = title
        self._sends = sends
        self._read_episodes = episodes
        self._on_sent = on_sent
        self._owners = phones_by_person(phones, people)
        self._people = {person.id: person for person in people if person.id in self._owners}
        self._person = current_person if current_person in self._owners else next(iter(self._owners))
        self._phone: PairedPhone = self._owners[self._person][0]
        self._episodes: list[PlexEpisode] = []
        self._task: asyncio.Task[None] | None = None
        self.ticks: dict[str, ctk.CTkCheckBox] = {}  # by an episode's key
        self.season_ticks: dict[int | None, ctk.CTkCheckBox] = {}
        self.person_menu: ctk.CTkOptionMenu | None = None
        self.phone_menu: ctk.CTkOptionMenu | None = None
        self.phone_sized: ctk.CTkCheckBox | None = None  # a 4K film's Phone-sized tick

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, t(TITLE_TEXT), right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        name = f"{title.title} ({title.year})" if title.year else title.title
        text_label(body, name, size=BODY_SIZE, bold=True, wraplength=WIDTH).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )
        self._to_row = ctk.CTkFrame(body, fg_color="transparent")
        self._to_row.pack(fill="x", pady=(LINE_GAP + 8, 0))
        self.to_label = text_label(self._to_row, "", size=DETAIL_SIZE, wraplength=WIDTH)
        self._build_to_row()
        self.note = text_label(body, "", size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH)
        self.note.pack(anchor="w", pady=(LINE_GAP + 4, 0))
        if sends.phone_sized_possible(title):
            self.phone_sized = ctk.CTkCheckBox(
                body,
                text=t(PHONE_SIZED_TEXT),
                width=0,
                height=28,
                checkbox_width=20,
                checkbox_height=20,
                corner_radius=5,
                font=theme.font(DETAIL_SIZE),
                text_color=theme.TEXT,
                text_color_disabled=theme.TEXT_MUTED,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                border_color=theme.EDGE,
                command=self._show_film,
            )
            self.phone_sized.select()
            self.phone_sized.pack(anchor="w", pady=(LINE_GAP + 4, 0))
        self._scroll = FitScroll(body, follow=True)
        self._list = ctk.CTkFrame(self._scroll, fg_color="transparent")
        self._list.grid(row=0, column=0, sticky="ew")
        if title.kind == "show":
            self._scroll.pack(fill="both", expand=True, pady=(LINE_GAP, 0))
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        self.chosen = ctk.CTkLabel(
            buttons, text="", font=theme.font(DETAIL_SIZE), text_color=theme.TEXT_MUTED
        )
        self.chosen.pack(side="left")
        self.send_button = primary_button(buttons, t(SEND_TEXT), self.send, width=BUTTON_WIDTH)
        self.send_button.pack(side="right", padx=(12, 0))
        self.cancel_button = secondary_button(buttons, t("Cancel"), self.close, width=BUTTON_WIDTH)
        self.cancel_button.pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        self._scroll.set_limit(round(max(120, max_dialog_height(master, scaling) - LIST_ROOM) * scaling))
        if title.kind == "show":
            self.note.configure(text=t(LOADING_TEXT))
            self.send_button.configure(state="disabled")
            self._task = spawn(self._load(), name="send-to-phone-episodes")
        else:
            self._show_film()
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    # -- whom it goes to -------------------------------------------------------------------------

    def _menu(self, values: list[str], command: Callable[[str], None]) -> ctk.CTkOptionMenu:
        return ctk.CTkOptionMenu(
            self._to_row,
            values=values,
            width=MENU_WIDTH,
            height=BUTTON_HEIGHT - 6,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.ACCENT_HOVER,
            text_color=theme.TEXT,
            dropdown_font=theme.font(DETAIL_SIZE),
            command=command,
        )

    def _build_to_row(self) -> None:
        for child in self._to_row.winfo_children():
            if child is not self.to_label:
                child.destroy()
        self.person_menu = self.phone_menu = None
        theirs = self._owners[self._person]
        if len(self._owners) == 1 and len(theirs) == 1:
            person = self._people[self._person]
            self.to_label.configure(
                text=t(ONE_PHONE_TEXT, phone=self._phone.name, person=person_name(person))
            )
            self.to_label.pack(side="left")
            return
        self.to_label.configure(text=t(TO_TEXT))
        self.to_label.pack(side="left", padx=(0, 12))
        if len(self._owners) > 1:
            names = {person_name(self._people[id]): id for id in self._owners}
            self.person_menu = self._menu(
                list(names), lambda chosen: self.choose_person(names[chosen])
            )
            self.person_menu.set(person_name(self._people[self._person]))
            self.person_menu.pack(side="left", padx=(0, 12))
        if len(theirs) > 1:
            phones = {phone.name: phone for phone in theirs}
            if len(phones) < len(theirs):  # two of a name: tell them apart
                phones = {f"{phone.name} ({n})": phone for n, phone in enumerate(theirs, 1)}
            self.phone_menu = self._menu(
                list(phones), lambda chosen: self.choose_phone(phones[chosen])
            )
            self.phone_menu.set(next(name for name, one in phones.items() if one == self._phone))
            self.phone_menu.pack(side="left")
        elif len(self._owners) > 1:
            ctk.CTkLabel(
                self._to_row, text=self._phone.name, font=theme.font(DETAIL_SIZE),
                text_color=theme.TEXT_MUTED,
            ).pack(side="left")

    def choose_person(self, person_id: int) -> None:
        if person_id not in self._owners:
            return
        self._person = person_id
        self._phone = self._owners[person_id][0]
        self._build_to_row()
        self._phone_changed()

    def choose_phone(self, phone: PairedPhone) -> None:
        self._phone = phone
        self._phone_changed()

    def _phone_changed(self) -> None:
        if self._item.kind == "show":
            if self._episodes:
                self._fill_episodes()
        else:
            self._show_film()

    # -- what goes ---------------------------------------------------------------------------------

    def _show_film(self) -> None:
        there = self._item.key in self._sends.taken_keys(self._phone.id)
        how = PHONE_SIZED_HOW_TEXT if self._wants_phone_sized() else HOW_TEXT
        self.note.configure(text=t(THERE_TEXT if there else how))
        self.send_button.configure(state="disabled" if there else "normal")
        if self.phone_sized is not None:
            self.phone_sized.configure(state="disabled" if there else "normal")

    def _wants_phone_sized(self) -> bool:
        return self.phone_sized is not None and bool(self.phone_sized.get())

    async def _load(self) -> None:
        try:
            episodes = await self._read_episodes(self._item)
        except PlexError:
            if self.winfo_exists():
                self.note.configure(text=t(PLEX_AWAY_TEXT))
            return
        if not self.winfo_exists():
            return
        self._episodes = episodes
        if not episodes:
            self.note.configure(text=t(NO_EPISODES_TEXT))
            return
        self.note.configure(text=t(HOW_TEXT))
        self._fill_episodes()

    def _fill_episodes(self) -> None:
        """The episodes by season, each season with a tick for all of it."""
        for child in self._list.winfo_children():
            child.destroy()
        self.ticks.clear()
        self.season_ticks.clear()
        taken = self._sends.taken_keys(self._phone.id)
        row = 0
        seasons: dict[int | None, list[PlexEpisode]] = {}
        for episode in self._episodes:
            seasons.setdefault(episode.season, []).append(episode)
        for season, episodes in seasons.items():
            tick = self._tick(
                season_text(season), bold=True,
                command=partial(self._season_ticked, season),
            )
            tick.grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 12, 2))
            self.season_ticks[season] = tick
            row += 1
            for episode in episodes:
                there = episode.key in taken
                text = episode_text(episode)
                if there:
                    text = f"{text}  ({t(ON_PHONE_TEXT)})"
                tick = self._tick(text, command=self._episode_ticked)
                if there:
                    tick.select()
                    tick.configure(state="disabled")
                tick.grid(row=row, column=0, sticky="w", padx=(28, 0), pady=1)
                self.ticks[episode.key] = tick
                row += 1
            self._sync_season(season)
        self._scroll.refit()
        self._count()

    def _tick(self, text: str, *, bold: bool = False, command: Callable[[], None]) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            self._list,
            text=text,
            width=0,
            height=28,
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            font=theme.font(DETAIL_SIZE, bold=bold),
            text_color=theme.TEXT,
            text_color_disabled=theme.TEXT_MUTED,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
            command=command,
        )

    def _of_season(self, season: int | None) -> list[PlexEpisode]:
        return [episode for episode in self._episodes if episode.season == season]

    def _season_ticked(self, season: int | None) -> None:
        on = bool(self.season_ticks[season].get())
        for episode in self._of_season(season):
            tick = self.ticks[episode.key]
            if tick.cget("state") == "disabled":
                continue
            if on:
                tick.select()
            else:
                tick.deselect()
        self._count()

    def _episode_ticked(self) -> None:
        for season in self.season_ticks:
            self._sync_season(season)
        self._count()

    def _sync_season(self, season: int | None) -> None:
        tick = self.season_ticks[season]
        if all(self.ticks[episode.key].get() for episode in self._of_season(season)):
            tick.select()
        else:
            tick.deselect()

    def chosen_episodes(self) -> list[PlexEpisode]:
        """The ticked episodes that aren't on the phone or on their way already."""
        return [
            episode
            for episode in self._episodes
            if self.ticks[episode.key].get() and self.ticks[episode.key].cget("state") != "disabled"
        ]

    def _count(self) -> None:
        count = len(self.chosen_episodes())
        self.chosen.configure(text=tn(CHOSEN_ONE, CHOSEN_MORE, count) if count else "")
        self.send_button.configure(state="normal" if count else "disabled")

    # -- sending ---------------------------------------------------------------------------------

    def send(self) -> None:
        if str(self.send_button.cget("state")) == "disabled":
            return
        self.send_button.configure(state="disabled")
        self._task = spawn(self._send(), name="send-to-phone")

    async def _send(self) -> None:
        phone = self._phone
        if self._item.kind == "show":
            count = await self._sends.send_episodes(phone, self._item, self.chosen_episodes())
            text = tn(SENT_ONE, SENT_MORE, count, phone=phone.name)
        else:
            await self._sends.send_film(phone, self._item, phone_sized=self._wants_phone_sized())
            text = t(SENT_FILM_TEXT, title=self._item.title, phone=phone.name)
        self.close()
        self._on_sent(text)

    async def settled(self) -> None:
        """Wait for the episodes or a send (tests)."""
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    def close(self) -> None:
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if self.winfo_exists():
            self.destroy()
