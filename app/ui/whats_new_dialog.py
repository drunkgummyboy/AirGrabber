"""The "What's new in AirGrabber 2.0.5" dialog, shown once on the first start after an update
(notes in app/whats_new.py). OK, Enter, Escape and the round ✕ close it.

A release with only small things shows its lines. One with big features shows a card per feature
(its picture, title and sentence) with the lines under "Also new", in a body that scrolls when it
doesn't fit; OK stays in view."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from pathlib import Path

import customtkinter as ctk
from PIL import Image

from app.i18n import N_, t
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import ASSETS_DIR, set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    CLOSE_CLEARANCE,
    CONTENT_WIDTH,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    FitScroll,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    screen_area,
    text_label,
)
from app.ui.tasks import spawn
from app.whats_new import Feature, dialog_title

log = logging.getLogger(__name__)

BULLET = "•"
BULLET_WIDTH = 22  # the bullet's column; the lines wrap beside it
OK_TEXT = N_("OK")
ALSO_NEW = N_("Also new")

PICTURES_DIR = ASSETS_DIR / "whats_new"
# Logical (unscaled) CTk units.
FEATURES_WIDTH = 732  # the cards' and the content's width in a dialog with features
PICTURE_HEIGHT = 244  # 3:1
CARD_RADIUS = 12  # app/assets/whats_new's pictures have this corner at this size
CARD_GAP = 14
CARD_PAD_X = 16
CARD_PAD_Y = 12
CARD_TITLE_SIZE = 16
CARD_TEXT_SIZE = 14
CARD_TEXT_GAP = 2
SECTION_SIZE = 12  # "ALSO NEW", as Settings' group labels
MAX_SCREEN_SHARE = 0.88  # at most this much of the work area's height


def load_picture(path: Path, size: tuple[int, int]) -> Image.Image:
    """The picture at `size` (physical pixels). Pillow work: call through asyncio.to_thread."""
    with Image.open(path) as image:
        return image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)


def _add_lines(master: ctk.CTkFrame, lines: Sequence[str], wraplength: int) -> None:
    """The bullets, in `master`'s grid."""
    master.grid_columnconfigure(1, weight=1)
    for row, line in enumerate(lines):
        pad = (0 if row == 0 else LINE_GAP, 0)
        # Both from the top, so the bullet stays level with a wrapped line's first row.
        ctk.CTkLabel(
            master,
            text=BULLET,
            width=BULLET_WIDTH,
            height=1,  # as tall as its text: even gaps between the lines
            anchor="nw",
            font=theme.font(BODY_SIZE),
            text_color=theme.TEXT_MUTED,
        ).grid(row=row, column=0, sticky="nw", pady=pad)
        text = text_label(master, t(line), wraplength=wraplength)
        text.configure(anchor="nw", height=1)
        text.grid(row=row, column=1, sticky="nw", pady=pad)


class FeatureCard(ctk.CTkFrame):
    """A big feature: its picture without a gap to the card's top and sides, then its title and
    sentence. The picture's room is there from the start, so nothing moves when it lands."""

    def __init__(self, master: ctk.CTkFrame, feature: Feature) -> None:
        super().__init__(master, fg_color=theme.SURFACE, corner_radius=CARD_RADIUS)
        self.feature = feature
        wrap = FEATURES_WIDTH - 2 * CARD_PAD_X
        self._title = text_label(
            self, t(feature.title), size=CARD_TITLE_SIZE, bold=True, wraplength=wrap
        )
        self._title.configure(height=1)
        self._title.pack(anchor="w", padx=CARD_PAD_X, pady=self._title_pad(with_picture=True))
        sentence = text_label(
            self, t(feature.text), size=CARD_TEXT_SIZE, color=theme.TEXT_MUTED, wraplength=wrap
        )
        sentence.configure(height=1)
        sentence.pack(anchor="w", padx=CARD_PAD_X, pady=(CARD_TEXT_GAP, CARD_PAD_Y))
        # Placed when the picture lands. A widget has a square ground over the card's rounded
        # corner: the window's, which is what lies outside that corner, shows through the
        # picture's transparent corners.
        self.picture = ctk.CTkLabel(
            self,
            text="",
            width=FEATURES_WIDTH,
            height=PICTURE_HEIGHT,
            fg_color=theme.BG,
            corner_radius=0,
        )

    @staticmethod
    def _title_pad(*, with_picture: bool) -> tuple[int, int]:
        return (PICTURE_HEIGHT if with_picture else 0) + CARD_PAD_Y, 0

    @property
    def has_picture(self) -> bool:
        return bool(self.picture.winfo_manager())

    def show_picture(self, image: Image.Image) -> None:
        """`image`: rendered at the physical size of FEATURES_WIDTH x PICTURE_HEIGHT."""
        picture = ctk.CTkImage(light_image=image, size=(FEATURES_WIDTH, PICTURE_HEIGHT))
        self.picture.configure(image=picture)
        self.picture.place(x=0, y=0)

    def drop_picture(self) -> None:
        """No picture to show: only its words."""
        self._title.pack_configure(pady=self._title_pad(with_picture=False))


class WhatsNewDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        *,
        version: str,
        lines: Sequence[str],
        features: Sequence[Feature] = (),
    ) -> None:
        """`lines`, `features`: the notes in English (app/whats_new.py); they show through t()."""
        super().__init__(master, fg_color=theme.BG)
        self._load_task: asyncio.Task[None] | None = None
        self.scroll: FitScroll | None = None  # with features: what scrolls
        self.cards: list[FeatureCard] = []
        title = dialog_title(version)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        self.bind("<Return>", lambda _event: self.destroy())
        self.bind("<Escape>", lambda _event: self.destroy())
        if features:
            self._build_with_features(title, lines, features)
        else:
            self._build_lines_only(title, lines)
        place_over(self, master)
        activate_later(self)
        if self.cards:
            self._load_task = spawn(self._load_pictures(), name="whats_new.pictures")

    def _build_lines_only(self, title: str, lines: Sequence[str]) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        # Holds the content width.
        ctk.CTkFrame(body, width=CONTENT_WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, title, right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        notes = ctk.CTkFrame(body, fg_color="transparent")
        notes.pack(fill="x", pady=(LINE_GAP * 2, 0))
        _add_lines(notes, lines, CONTENT_WIDTH - BULLET_WIDTH)
        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", pady=(NAV_GAP, 0))
        primary_button(nav, t(OK_TEXT), self.destroy).pack(side="right")
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, notes))

    def _build_with_features(
        self, title: str, lines: Sequence[str], features: Sequence[Feature]
    ) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=PAD_Y)
        heading = heading_row(body, title, right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x", padx=PAD_X)
        # Its scrollbar has its room in the window's right margin: the cards keep their width
        # and the window its size whether it scrolls or not.
        scroll = self.scroll = FitScroll(body, follow=True)
        scroll.configure(width=FEATURES_WIDTH)
        scroll.reserve_bar()
        scroll.pack(
            fill="both",
            expand=True,
            padx=(PAD_X, PAD_X - scroll.bar_room),
            pady=(LINE_GAP * 2, 0),
        )
        content = ctk.CTkFrame(scroll, fg_color="transparent")
        content.grid(row=0, column=0, sticky="nsew")
        for index, feature in enumerate(features):
            card = FeatureCard(content, feature)
            card.pack(fill="x", pady=(CARD_GAP if index else 0, 0))
            self.cards.append(card)
        drag_widgets = [self, body, heading, scroll, content, *self.cards]
        if lines:
            ctk.CTkLabel(
                content,
                text=t(ALSO_NEW).upper(),
                font=theme.font(SECTION_SIZE, bold=True),
                text_color=theme.TEXT_MUTED,
                anchor="w",
                height=1,
            ).pack(fill="x", pady=(CARD_GAP + LINE_GAP // 2, LINE_GAP))
            notes = ctk.CTkFrame(content, fg_color="transparent")
            notes.pack(fill="x")
            _add_lines(notes, lines, FEATURES_WIDTH - BULLET_WIDTH)
            drag_widgets.append(notes)
        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", padx=PAD_X, pady=(NAV_GAP - LINE_GAP, 0))
        primary_button(nav, t(OK_TEXT), self.destroy).pack(side="right")
        drag_widgets.append(nav)
        self.borderless = make_borderless(self, drag_widgets=drag_widgets)
        self._fit_screen(scroll)

    def _fit_screen(self, scroll: FitScroll) -> None:
        """As tall as its content, at most MAX_SCREEN_SHARE of the work area: then the body
        scrolls between the heading and OK."""
        self.update_idletasks()
        scroll.refit()
        self.update_idletasks()
        around = self.winfo_reqheight() - scroll.content_height()  # heading and OK, physical pixels
        _left, top, _right, bottom = screen_area(self.master)
        scroll.set_limit(math.floor((bottom - top) * MAX_SCREEN_SHARE) - around)
        self.update_idletasks()

    async def _load_pictures(self) -> None:
        scaling = float(ctk.ScalingTracker.get_widget_scaling(self))
        size = round(FEATURES_WIDTH * scaling), round(PICTURE_HEIGHT * scaling)
        for card in self.cards:
            name = card.feature.picture
            try:
                image = await asyncio.to_thread(load_picture, PICTURES_DIR / name, size)
            except Exception as exc:  # a missing or broken file costs the card its picture only
                log.warning("What's new: no picture %s (%s)", name, type(exc).__name__)
                if not self.winfo_exists():
                    return
                card.drop_picture()
                continue
            if not self.winfo_exists():
                return
            card.show_picture(image)

    def destroy(self) -> None:
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None
        super().destroy()
