"""The top band's name button's menu and dialogs (app/services/profiles.py): switch to another
person, "Add a person", "Rename or change colour" (the same dialog for the selected person) and
"Remove a person" (choose who, then confirm). Borderless dialogs in AirGrabber's style
(app/ui/dialog_kit.py), modal over the main window.

The name button itself is drawn on the band (app/ui/spotlight_band.py); MainWindow hands its
clicks to `PeopleActions.open_menu`. Whatever a switch, a removal or a new person changes, the
window hears from ProfileService (reloading the pages, "Now showing …'s list").
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models import Profile, ProfileColour
from app.services.profiles import ProfileError, ProfileService, name_problem, next_colour
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    SMALL_SIZE,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.missing_control import PopupMenu, popup_menu
from app.ui.people import (
    ME,
    ColourSwatches,
    avatar,
    menu_entries,
    menu_measure,
    person_name,
)
from app.ui.tasks import spawn

# Shown through t().
ADD_TITLE = N_("Add a person")
ADD_NOTE = N_("They start with an empty list. Downloads and settings are shared.")
EDIT_TITLE = N_("Rename or change colour")
REMOVE_TITLE = N_("Remove a person")
REMOVE_CHOOSE = N_("Who do you want to remove?")
REMOVE_CONFIRM = N_(
    "Remove {name}? Their followed series and watchlist are removed. Downloads stay."
)
NAME_LABEL = N_("Name")
COLOUR_LABEL = N_("Colour")

WIDTH = 400  # the dialogs' content (CTk units)
FIELD_HEIGHT = 38
ROW_HEIGHT = 44  # a person's row in "Remove a person"
ROW_AVATAR = 28
BUTTON_WIDTH = 120

Save = Callable[[str, ProfileColour], Awaitable[None]]


class _Dialog(ctk.CTkToplevel):
    """A heading, content and right-aligned buttons; Escape and the round ✕ close it (not
    while it is saving)."""

    def __init__(self, master: Any, title: str) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.busy = False
        self._tasks: set[asyncio.Task[None]] = set()
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(self.body, width=WIDTH, height=1, fg_color="transparent").pack()
        self.heading = heading_row(self.body, title, right_room=CLOSE_CLEARANCE)
        self.heading.pack(fill="x")
        self.content = ctk.CTkFrame(self.body, fg_color="transparent")
        self.content.pack(fill="x")
        self.buttons = ctk.CTkFrame(self.body, fg_color="transparent")
        self.buttons.pack(fill="x", pady=(NAV_GAP, 0))
        self.bind("<Escape>", lambda _event: self.close())

    def finish_layout(self, master: Any) -> None:
        self.borderless = make_borderless(
            self, drag_widgets=(self, self.body, self.heading, self.content, self.buttons)
        )
        place_over(self, master)
        activate_later(self)

    def button(
        self, text: str, command: Callable[[], None], *, primary: bool = False
    ) -> ctk.CTkButton:
        """A button at the right of the row (add the rightmost first)."""
        make = primary_button if primary else secondary_button
        button = make(self.buttons, text, command, width=BUTTON_WIDTH)
        button.pack(side="right", padx=(12, 0))
        return button

    def set_buttons(self, state: str) -> None:
        for child in self.buttons.winfo_children():
            if isinstance(child, ctk.CTkButton):
                child.configure(state=state)

    def run(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        if not self.busy and self.winfo_exists():
            self.destroy()

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()


class PersonDialog(_Dialog):
    """"Add a person" (`person` None; the note that they start with an empty list) or "Rename
    or change colour" (the selected `person`): a name (needed, at most NAME_MAX characters,
    nobody else's ignoring case; the first person while still "Me" may leave it empty, to
    change only the colour) and one of the five colours, then Cancel / Add or Save.
    `on_save(name, colour)` does the change; a ProfileError it raises shows under the name."""

    def __init__(
        self,
        master: Any,
        *,
        people: Sequence[Profile],
        on_save: Save,
        person: Profile | None = None,
    ) -> None:
        title = t(ADD_TITLE) if person is None else t(EDIT_TITLE)
        super().__init__(master, title)
        self._people = tuple(people)
        self._person = person
        self._on_save = on_save
        self._required = person is None or person.name is not None
        content = self.content
        if person is None:
            text_label(
                content, t(ADD_NOTE), size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
            ).pack(anchor="w", pady=(LINE_GAP, 0))
        text_label(content, t(NAME_LABEL), size=SMALL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(16, 4)
        )
        self.name = ctk.CTkEntry(
            content,
            height=FIELD_HEIGHT,
            corner_radius=8,
            border_width=1,
            border_color=theme.ACCENT,
            fg_color=theme.SURFACE,
            text_color=theme.TEXT,
            font=theme.font(BODY_SIZE),
            placeholder_text="" if self._required else t(ME),
            placeholder_text_color=theme.TEXT_MUTED,
        )
        self.name.pack(fill="x")
        if person is not None and person.name:
            self.name.insert(0, person.name)
        self.problem = text_label(
            content, "", size=SMALL_SIZE, color=theme.DANGER, wraplength=WIDTH
        )
        self.problem.pack(anchor="w", pady=(4, 0))
        text_label(content, t(COLOUR_LABEL), size=SMALL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(10, 6)
        )
        colour = person.colour if person is not None else next_colour(self._people)
        self.swatches = ColourSwatches(content, selected=colour)
        self.swatches.pack(anchor="w")

        self.save_button = self.button(
            t("Add") if person is None else t("Save"), self.save, primary=True
        )
        self.button(t("Cancel"), self.close)
        self.name.bind("<Return>", lambda _event: self.save())
        self.finish_layout(master)
        self.after(200, self._focus_name)

    def _focus_name(self) -> None:
        if self.winfo_exists():
            self.name.focus_set()
            self.name.icursor("end")

    def save(self) -> None:
        if self.busy:
            return
        name = self.name.get()
        own = self._person.id if self._person is not None else None
        problem = name_problem(name, self._people, own=own, required=self._required)
        if problem is not None:
            self.problem.configure(text=problem)
            return
        self.problem.configure(text="")
        self.busy = True
        self.set_buttons("disabled")
        self.run(self._save(name, self.swatches.selected), "people-save")

    async def _save(self, name: str, colour: ProfileColour) -> None:
        try:
            await self._on_save(name, colour)
        except ProfileError as exc:
            if self.winfo_exists():
                self.busy = False
                self.set_buttons("normal")
                self.problem.configure(text=str(exc))
            return
        except BaseException:
            if self.winfo_exists():
                self.busy = False
                self.set_buttons("normal")
            raise
        self.busy = False
        self.close()


class RemovePersonDialog(_Dialog):
    """"Remove a person": first who (everyone, with their avatar), then the question; Remove
    calls `on_remove(person id)`. The last person can't be removed (the menu offers this only
    with two or more; ProfileService refuses it anyway)."""

    def __init__(
        self,
        master: Any,
        *,
        people: Sequence[Profile],
        on_remove: Callable[[int], Awaitable[None]],
    ) -> None:
        super().__init__(master, t(REMOVE_TITLE))
        self._people = tuple(people)
        self._on_remove = on_remove
        self._images: list[ctk.CTkImage] = []
        self.chosen: Profile | None = None
        self.rows: dict[int, ctk.CTkButton] = {}
        self._choose_step()
        self.finish_layout(master)

    def _clear(self) -> None:
        for child in (*self.content.winfo_children(), *self.buttons.winfo_children()):
            child.destroy()

    def _choose_step(self) -> None:
        self._clear()
        self.chosen = None
        text_label(self.content, t(REMOVE_CHOOSE), size=DETAIL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(LINE_GAP, 10)
        )
        self.rows = {}
        for person in self._people:
            image = avatar(self, person, ROW_AVATAR)
            self._images.append(image)
            row = ctk.CTkButton(
                self.content,
                text=f"  {person_name(person)}",
                image=image,
                compound="left",
                anchor="w",
                height=ROW_HEIGHT,
                corner_radius=8,
                border_width=1,
                border_color=theme.EDGE,
                fg_color=theme.SURFACE,
                hover_color=theme.MENU_HOVER,
                text_color=theme.TEXT,
                font=theme.font(BODY_SIZE),
                command=lambda person=person: self._confirm_step(person),
            )
            row.pack(fill="x", pady=(0, 8))
            self.rows[person.id] = row
        self.button(t("Cancel"), self.close)

    def _confirm_step(self, person: Profile) -> None:
        self._clear()
        self.chosen = person
        self.question = text_label(
            self.content,
            t(REMOVE_CONFIRM, name=person_name(person)),
            size=BODY_SIZE,
            wraplength=WIDTH,
        )
        self.question.pack(anchor="w", pady=(LINE_GAP, 0))
        self.problem = text_label(
            self.content, "", size=SMALL_SIZE, color=theme.DANGER, wraplength=WIDTH
        )
        self.problem.pack(anchor="w", pady=(6, 0))
        self.remove_button = ctk.CTkButton(
            self.buttons,
            text=t("Remove"),
            width=BUTTON_WIDTH,
            height=40,
            corner_radius=8,
            fg_color=theme.DANGER_BUTTON,
            hover_color=theme.DANGER_BUTTON_HOVER,
            text_color=theme.ON_COLOR,
            font=theme.font(15, bold=True),
            command=self.remove,
        )
        self.remove_button.pack(side="right", padx=(12, 0))
        self.button(t("Cancel"), self.close)

    def remove(self) -> None:
        if self.busy or self.chosen is None:
            return
        self.busy = True
        self.set_buttons("disabled")
        self.run(self._remove(self.chosen), "people-remove")

    async def _remove(self, person: Profile) -> None:
        try:
            await self._on_remove(person.id)
        except ProfileError as exc:
            if self.winfo_exists():
                self.busy = False
                self.set_buttons("normal")
                self.problem.configure(text=str(exc))
            return
        except BaseException:
            if self.winfo_exists():
                self.busy = False
                self.set_buttons("normal")
            raise
        self.busy = False
        self.close()


class PeopleActions:
    """What the name button leads to: its menu (`open_menu`), switching, and the three dialogs.
    One dialog at a time."""

    def __init__(self, master: ctk.CTk, profiles: ProfileService) -> None:
        self._master = master
        self._profiles = profiles
        self._images: list[ctk.CTkImage] = []  # the open menu's avatars
        self.dialog: _Dialog | None = None
        self.menu: PopupMenu | None = None

    def open_menu(self, anchor: tk.Misc, under: tuple[int, int, int, int]) -> PopupMenu | None:
        """The menu under the name button (`under`: its box on screen, physical pixels)."""
        profiles = self._profiles
        self._images = []

        def image(person: Profile) -> ctk.CTkImage:
            picture = avatar(anchor, person, 24)
            self._images.append(picture)
            return picture

        entries = menu_entries(
            profiles.people,
            profiles.current_id,
            on_switch=self.switch,
            on_add=self.add,
            on_edit=self.edit,
            on_remove=self.remove,
            image=image,
            measure=menu_measure(),
        )
        self.menu = popup_menu(anchor, entries, under=under)
        return self.menu

    def switch(self, person_id: int) -> None:
        spawn(self._profiles.switch(person_id), name="people-switch")

    def _open(self, make: Callable[[], _Dialog]) -> None:
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            self.dialog.focus_force()
            return
        self.dialog = make()

    def add(self) -> None:
        profiles = self._profiles

        async def save(name: str, colour: ProfileColour) -> None:
            person = await profiles.add(name, colour)
            await profiles.switch(person.id)  # Add switches to the new person

        self._open(lambda: PersonDialog(self._master, people=profiles.people, on_save=save))

    def edit(self) -> None:
        profiles = self._profiles
        person = profiles.current

        async def save(name: str, colour: ProfileColour) -> None:
            await profiles.update(person.id, name=name, colour=colour)

        self._open(
            lambda: PersonDialog(
                self._master, people=profiles.people, on_save=save, person=person
            )
        )

    def remove(self) -> None:
        profiles = self._profiles
        self._open(
            lambda: RemovePersonDialog(
                self._master, people=profiles.people, on_remove=profiles.remove
            )
        )

