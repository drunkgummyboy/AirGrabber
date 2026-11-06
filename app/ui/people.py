"""People in the UI (app/services/profiles.py): a person's name ("Me" for the first one while
they have none), initial and colour, their avatar (a disc of their colour with the initial in
white), the row of colour swatches the dialogs and the welcome choose with, and the top band's
name button's menu (`menu_entries`; the dialogs are app/ui/people_dialogs.py)."""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from app.i18n import N_, t
from app.models import Profile, ProfileColour
from app.ui import theme
from app.ui.borderless import disc_image
from app.ui.missing_control import MENU_FONT_SIZE, MENU_SEPARATOR, MenuEntry
from app.ui.text_fit import elide_to_width

# Shown through t().
ME = N_("Me")  # the first person while they have no name
ADD_TEXT = N_("＋ Add a person…")
EDIT_TEXT = N_("✎ Rename or change colour…")
REMOVE_TEXT = N_("🗑 Remove a person…")
SWITCHED_TEXT = N_("Now showing {name}'s list")

MENU_NAME_WIDTH = 150  # a name there is cut short beyond this (room for the ✓)
SWATCH = 26  # a colour swatch's disc
SWATCH_RING = 2  # the ring around the chosen one
SWATCH_GAP = 10
_SWATCH_BOX = SWATCH + 2 * (SWATCH_RING + 2)
_FONT_FILE = Path(ctk.__file__).parent / "assets" / "fonts" / "Roboto" / "Roboto-Medium.ttf"
RGB = tuple[int, int, int]


def person_name(person: Profile) -> str:
    """The name to show: their own, or "Me" for the first person while they have none."""
    return person.name or t(ME)


def person_initial(person: Profile) -> str:
    """The letter on their avatar: the first of the shown name, upper case."""
    return person_name(person).strip()[:1].upper() or "?"


def switched_text(person: Profile) -> str:
    """The status line after a switch: "Now showing Sanne's list"."""
    return t(SWITCHED_TEXT, name=person_name(person))


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(_FONT_FILE), size)
    except OSError:
        return ImageFont.load_default(size)


def avatar_image(initial: str, fill: RGB, diameter: int) -> Image.Image:
    """A `fill` disc `diameter` physical pixels wide with `initial` in white in its middle."""
    disc = disc_image(max(1, diameter), fill)
    draw = ImageDraw.Draw(disc)
    middle = diameter / 2
    draw.text(
        (middle, middle),
        initial,
        fill=theme.ON_COLOR,
        font=_font(max(6, round(diameter * 0.5))),
        anchor="mm",
    )
    return disc


def _rgb(master: Any, colour: str) -> RGB:
    red, green, blue = master.winfo_rgb(colour)
    return red >> 8, green >> 8, blue >> 8


def avatar(master: Any, person: Profile, size: int) -> ctk.CTkImage:
    """`person`'s avatar `size` units wide, baked at `master`'s scaling for light and dark."""
    scaling: float = ctk.ScalingTracker.get_widget_scaling(master)
    side = max(1, round(size * scaling))
    pair = theme.person_colour(person.colour)
    initial = person_initial(person)
    light, dark = (
        avatar_image(initial, _rgb(master, theme.pick(pair, dark=mode)), side)
        for mode in (False, True)
    )
    return ctk.CTkImage(light_image=light, dark_image=dark, size=(size, size))


def menu_entries(
    people: Sequence[Profile],
    current_id: int,
    *,
    on_switch: Callable[[int], None],
    on_add: Callable[[], None],
    on_edit: Callable[[], None],
    on_remove: Callable[[], None],
    image: Callable[[Profile], Any] | None = None,
    measure: Callable[[str], float] | None = None,
) -> list[MenuEntry]:
    """The name button's menu: everyone (their avatar from `image`, a ✓ on the selected one;
    choosing one switches to them), a line, then Add a person…, Rename or change colour… and,
    with two or more people, Remove a person…. `measure`: cuts long names short."""
    entries: list[MenuEntry] = []
    for person in people:
        name = person_name(person)
        if measure is not None:
            name = elide_to_width(name, MENU_NAME_WIDTH, measure)
        entries.append(
            MenuEntry(
                f" {name}",
                functools.partial(on_switch, person.id),
                image=image(person) if image is not None else None,
                checked=person.id == current_id,
            )
        )
    entries.append(MENU_SEPARATOR)
    entries.append(MenuEntry(t(ADD_TEXT), on_add))
    entries.append(MenuEntry(t(EDIT_TEXT), on_edit))
    if len(people) >= 2:
        entries.append(MenuEntry(t(REMOVE_TEXT), on_remove))
    return entries


def menu_measure() -> Callable[[str], float]:
    """How wide a menu item's text is (CTk units)."""
    font = theme.font(MENU_FONT_SIZE)

    def measure(text: str) -> float:
        return float(font.measure(text))

    return measure


class ColourSwatches(ctk.CTkFrame):
    """The five person colours in a row (theme.PERSON_*), the chosen one ringed; a click
    chooses (`on_change`)."""

    def __init__(
        self,
        master: Any,
        *,
        selected: ProfileColour,
        on_change: Callable[[ProfileColour], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._selected = selected
        self._on_change = on_change
        self._rings: dict[ProfileColour, ctk.CTkFrame] = {}
        self.buttons: dict[ProfileColour, ctk.CTkButton] = {}
        for index, colour in enumerate(ProfileColour):
            ring = ctk.CTkFrame(
                self,
                width=_SWATCH_BOX,
                height=_SWATCH_BOX,
                corner_radius=_SWATCH_BOX // 2,
                border_width=0,
                border_color=theme.TEXT,
                fg_color="transparent",
            )
            ring.grid(row=0, column=index, padx=(0 if index == 0 else SWATCH_GAP - 4, 0))
            ring.grid_propagate(False)
            pair = theme.person_colour(colour)
            button = ctk.CTkButton(
                ring,
                text="",
                width=SWATCH,
                height=SWATCH,
                corner_radius=SWATCH // 2,
                fg_color=pair,
                hover_color=pair,
                border_width=0,
                command=lambda colour=colour: self.select(colour, notify=True),
            )
            button.place(relx=0.5, rely=0.5, anchor="center")
            self._rings[colour] = ring
            self.buttons[colour] = button
        self._paint()

    @property
    def selected(self) -> ProfileColour:
        return self._selected

    def select(self, colour: ProfileColour, *, notify: bool = False) -> None:
        self._selected = colour
        self._paint()
        if notify and self._on_change is not None:
            self._on_change(colour)

    def _paint(self) -> None:
        for colour, ring in self._rings.items():
            chosen = colour is self._selected
            ring.configure(border_width=SWATCH_RING if chosen else 0, border_color=theme.TEXT)
