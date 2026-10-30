"""Quick setup ("Make it yours"): the most important choices on one page, each one click.

Look (Like Windows / Dark / Light, each with a small swatch; applies at once), download quality
(Smaller files = 720p / Recommended = 1080p / Best picture = 2160p, for series and films alike;
the sizes stay), card size (Small = S / Medium = M / Large = L) and language (English /
Nederlands; another language than the running one needs a restart: "Restart now", or it
applies at the next start). What is chosen first is what Settings has now; a quality or card
size Settings has that isn't offered here (4K for films only, XS) is left unchosen, and stays
unless one is clicked.

`QuickSetupPanel` is the page: the welcome's last step (app/ui/welcome_dialog.py) and the small
`QuickSetupDialog` from Settings → Looks → Quick setup. The host saves `quick_changes`.
"""

from __future__ import annotations

import functools
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Generic, TypeVar

import customtkinter as ctk
from PIL import Image, ImageColor, ImageDraw

from app.i18n import N_, Language, current_language, t
from app.models import CardSize, QualityProfile, Settings, ThemeMode
from app.services import SettingsService
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.card_scale import CARD_SIZE_LABEL
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    CONTENT_WIDTH,
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
from app.ui.tasks import async_command

V = TypeVar("V")

# Shown through t().
HEADING = N_("Make it yours")
INTRO = N_("You can change all of this later in Settings.")
LOOK_TITLE = N_("Look")
QUALITY_TITLE = N_("Download quality")
LANGUAGE_TITLE = N_("Language")
RESTART_TEXT = N_("Restart AirGrabber to switch the language")
OPEN_TEXT = N_("Quick setup")  # Settings → Looks: the box and its button

# (value, name, the small line under it) in the order shown.
LOOKS: tuple[tuple[ThemeMode, str], ...] = (
    (ThemeMode.SYSTEM, N_("Like Windows")),
    (ThemeMode.DARK, N_("Dark")),
    (ThemeMode.LIGHT, N_("Light")),
)
QUALITIES: tuple[tuple[QualityProfile, str, str], ...] = (
    (QualityProfile.HD, N_("Smaller files"), N_("720p · saves space")),
    (QualityProfile.FULL_HD, N_("Recommended"), N_("1080p · Full HD")),
    (QualityProfile.UHD, N_("Best picture"), N_("2160p (4K) · big files")),
)
SIZES: tuple[tuple[CardSize, str, str], ...] = (
    (CardSize.S, N_("Small"), N_("more on screen")),
    (CardSize.M, N_("Medium"), ""),
    (CardSize.L, N_("Large"), N_("bigger posters")),
)

# Logical (unscaled) CTk units.
QUESTION_PAD_X = 14
QUESTION_PAD_Y = 10
QUESTION_GAP = 10  # between two questions
CHOICE_WIDTH = 132
CHOICE_GAP = 8
CHOICE_PAD_Y = 6
SWATCH_SIZE = (46, 28)
SWATCH_RADIUS = 6
_RESTART_BUTTON_WIDTH = 150
_SUPERSAMPLE = 4


# -- choices (pure) ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuickChoices:
    """What the page has chosen: None = nothing chosen (Settings keeps its value)."""

    look: ThemeMode
    quality: QualityProfile | None
    size: CardSize | None
    language: Language


def quick_defaults(current: Settings) -> QuickChoices:
    """What the page starts with: Settings' values, a quality only when series and films have
    the same one of the three, a card size only when it is S, M or L."""
    offered_qualities = {quality for quality, _name, _note in QUALITIES}
    quality = current.quality_tv
    same = quality == current.quality_movie and quality in offered_qualities
    size = current.card_size if current.card_size in {s for s, _n, _l in SIZES} else None
    return QuickChoices(
        look=current.theme_mode,
        quality=quality if same else None,
        size=size,
        language=current.language,
    )


def quick_changes(current: Settings, chosen: QuickChoices) -> dict[str, Any]:
    """The settings to save for `chosen` (only what differs from `current`): a quality goes to
    both `quality_tv` and `quality_movie`; the size ranges are left alone."""
    changes: dict[str, Any] = {}
    if chosen.look != current.theme_mode:
        changes["theme_mode"] = chosen.look
    if chosen.quality is not None:
        if chosen.quality != current.quality_tv:
            changes["quality_tv"] = chosen.quality
        if chosen.quality != current.quality_movie:
            changes["quality_movie"] = chosen.quality
    if chosen.size is not None and chosen.size != current.card_size:
        changes["card_size"] = chosen.size
    if chosen.language != current.language:
        changes["language"] = chosen.language
    return changes


def needs_restart(chosen: Language, running: Language) -> bool:
    return chosen != running


# -- swatches (baked; the light and dark look's edge each) -------------------------------------


def _rgb(color: str) -> tuple[int, int, int]:
    red, green, blue = ImageColor.getrgb(color)[:3]
    return red, green, blue


@lru_cache(maxsize=16)
def swatch_image(look: ThemeMode, size: tuple[int, int], edge: str, radius: int) -> Image.Image:
    """A look's swatch, `size` physical pixels, RGBA: the window background of the light look
    (LIGHT), of the dark look (DARK), or of both, half and half (SYSTEM), in a rounded `edge`
    line."""
    width, height = size
    scale = _SUPERSAMPLE
    big = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    light, dark = _rgb(theme.BG[0]), _rgb(theme.BG[1])
    fill = Image.new("RGB", big.size, dark if look is ThemeMode.DARK else light)
    if look is ThemeMode.SYSTEM:
        fill.paste(dark, (big.width // 2, 0, big.width, big.height))
    mask = Image.new("L", big.size, 0)
    box = (0, 0, big.width - 1, big.height - 1)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius * scale, fill=255)
    big.paste(fill, (0, 0), mask)
    ImageDraw.Draw(big).rounded_rectangle(
        box, radius=radius * scale, outline=(*_rgb(edge), 255), width=scale
    )
    return big.resize(size, Image.Resampling.BOX)


# -- widgets -----------------------------------------------------------------------------------


class _Choice(ctk.CTkFrame):
    """One choice: a bordered box with an optional swatch, its name and a small muted line; a
    click anywhere chooses it."""

    def __init__(
        self,
        master: Any,
        name: str,
        note: str = "",
        *,
        swatch: ctk.CTkImage | None = None,
        command: Callable[[], None],
    ) -> None:
        super().__init__(master, corner_radius=9, width=CHOICE_WIDTH, cursor="hand2")
        self.name = name
        self.selected = False
        widgets: list[Any] = [self]
        if swatch is not None:
            picture = ctk.CTkLabel(self, text="", image=swatch)
            picture.pack(pady=(CHOICE_PAD_Y + 2, 0))
            widgets.append(picture)
        label = ctk.CTkLabel(
            self, text=name, font=theme.font(14), text_color=theme.TEXT, height=22
        )
        label.pack(padx=10, pady=(2 if swatch is not None else CHOICE_PAD_Y, 0))
        widgets.append(label)
        if note:
            small = ctk.CTkLabel(
                self, text=note, font=theme.font(12), text_color=theme.TEXT_MUTED, height=18
            )
            small.pack(padx=10)
            widgets.append(small)
        # Holds the box's width, inside its border (where it can't hide the border line).
        ctk.CTkFrame(self, fg_color="transparent", width=CHOICE_WIDTH - 8, height=1).pack(
            pady=(0, CHOICE_PAD_Y)
        )
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event: command(), add="+")
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.configure(
            border_width=2 if selected else 1,
            border_color=theme.ACCENT if selected else theme.CELL_BORDER,
            fg_color=theme.TODAY_BG if selected else theme.CELL_BG,
        )


class _Question(ctk.CTkFrame, Generic[V]):
    """A titled box of choices; `on_choose(value)` when one is clicked."""

    def __init__(
        self,
        master: Any,
        title: str,
        options: Sequence[tuple[V, str, str, ctk.CTkImage | None]],
        chosen: V | None,
        on_choose: Callable[[V], None],
    ) -> None:
        super().__init__(
            master, fg_color=theme.TAB_BG, border_width=1, border_color=theme.PANEL_EDGE,
            corner_radius=10,
        )
        self._on_choose = on_choose
        ctk.CTkLabel(
            self, text=title, font=theme.font(15, bold=True), text_color=theme.TEXT, anchor="w"
        ).pack(fill="x", padx=QUESTION_PAD_X, pady=(QUESTION_PAD_Y, 6))
        self.row = ctk.CTkFrame(self, fg_color="transparent")
        self.row.pack(fill="x", padx=QUESTION_PAD_X, pady=(0, QUESTION_PAD_Y))
        self.choices: dict[V, _Choice] = {}
        for index, (value, name, note, swatch) in enumerate(options):
            choice = _Choice(
                self.row, name, note, swatch=swatch, command=functools.partial(self.choose, value)
            )
            choice.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else CHOICE_GAP, 0))
            self.choices[value] = choice
        self.value: V | None = None
        self._mark(chosen)

    def choose(self, value: V) -> None:
        self._mark(value)
        self._on_choose(value)

    def _mark(self, value: V | None) -> None:
        self.value = value
        for option, choice in self.choices.items():
            choice.set_selected(option == value)


class QuickSetupPanel(ctk.CTkFrame):
    """The Quick setup page under its own heading (see the module docstring). The look applies
    as soon as it is clicked (`revert_look` takes it back). `on_restart`: the "Restart now"
    button beside the language note (its host saves first); without it only the note shows."""

    def __init__(
        self,
        master: Any,
        *,
        current: Settings,
        on_restart: Callable[[], None] | None = None,
        width: int = CONTENT_WIDTH,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._chosen = quick_defaults(current)
        self._swatches: list[ctk.CTkImage] = []

        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        physical = (round(SWATCH_SIZE[0] * scaling), round(SWATCH_SIZE[1] * scaling))
        radius = round(SWATCH_RADIUS * scaling)
        looks: list[tuple[ThemeMode, str, str, ctk.CTkImage | None]] = []
        for look, name in LOOKS:
            image = ctk.CTkImage(
                light_image=swatch_image(look, physical, theme.EDGE[0], radius),
                dark_image=swatch_image(look, physical, theme.EDGE[1], radius),
                size=SWATCH_SIZE,
            )
            self._swatches.append(image)
            looks.append((look, t(name), "", image))
        self.look = _Question(self, t(LOOK_TITLE), looks, self._chosen.look, self._look_chosen)
        self.quality = _Question(
            self,
            t(QUALITY_TITLE),
            [(quality, t(name), t(note), None) for quality, name, note in QUALITIES],
            self._chosen.quality,
            lambda value: self._set_choice(quality=value),
        )
        self.card_size = _Question(
            self,
            t(CARD_SIZE_LABEL),
            [(size, t(name), t(note) if note else "", None) for size, name, note in SIZES],
            self._chosen.size,
            lambda value: self._set_choice(size=value),
        )
        self.language = _Question(
            self,
            t(LANGUAGE_TITLE),
            [(language, language.native_name, "", None) for language in Language],
            self._chosen.language,
            self._language_chosen,
        )
        questions: tuple[ctk.CTkFrame, ...] = (self.look, self.quality, self.card_size, self.language)
        for index, question in enumerate(questions):
            question.pack(fill="x", pady=(0 if index == 0 else QUESTION_GAP, 0))

        self.restart_row = ctk.CTkFrame(self, fg_color="transparent")
        text_label(
            self.restart_row,
            t(RESTART_TEXT),
            size=SMALL_SIZE,
            color=theme.WARNING,
            wraplength=width - (_RESTART_BUTTON_WIDTH + 12 if on_restart is not None else 0),
        ).pack(side="left", fill="x", expand=True)
        self.restart_button: ctk.CTkButton | None = None
        if on_restart is not None:
            self.restart_button = primary_button(
                self.restart_row, t("Restart now"), on_restart, width=_RESTART_BUTTON_WIDTH
            )
            self.restart_button.pack(side="right", padx=(12, 0))
        self.show_restart()

    @property
    def chosen(self) -> QuickChoices:
        return self._chosen

    def changes(self, current: Settings) -> dict[str, Any]:
        return quick_changes(current, self._chosen)

    def revert_look(self, look: ThemeMode) -> None:
        """Show `look` again (the page was closed without saving)."""
        if self._chosen.look != look:
            theme.apply_appearance(look)

    def _set_choice(self, **fields: Any) -> None:
        self._chosen = replace(self._chosen, **fields)

    def _look_chosen(self, look: ThemeMode) -> None:
        changed = look != self._chosen.look
        self._set_choice(look=look)
        if changed:
            theme.apply_appearance(look)

    def _language_chosen(self, language: Language) -> None:
        self._set_choice(language=language)
        self.show_restart()

    def show_restart(self, *, always: bool = False) -> None:
        """The language note (and Restart now) while another language than the running one is
        chosen; `always`: also now (the host measures its tallest size)."""
        if always or needs_restart(self._chosen.language, current_language()):
            self.restart_row.pack(fill="x", pady=(QUESTION_GAP, 0))
        else:
            self.restart_row.pack_forget()


class QuickSetupDialog(ctk.CTkToplevel):
    """Quick setup on its own, from Settings → Looks (modal over Settings). Save stores the
    choices and calls `on_saved`; Cancel, Esc and the window's X take the look back."""

    def __init__(
        self,
        master: Any,
        *,
        settings: SettingsService,
        on_saved: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        """`on_restart`: after saving, restarts AirGrabber (the language row's Restart now)."""
        super().__init__(master, fg_color=theme.BG)
        self._settings = settings
        self._on_saved = on_saved
        self._on_restart = on_restart
        self._saved_look = settings.current.theme_mode
        self.title(t(OPEN_TEXT))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        heading_row(body, t(HEADING)).pack(fill="x")
        text_label(body, t(INTRO), size=DETAIL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(LINE_GAP, 16)
        )
        restart = async_command(self._save_and_restart) if on_restart is not None else None
        self.panel = QuickSetupPanel(body, current=settings.current, on_restart=restart)
        self.panel.pack(fill="x")
        buttons = ctk.CTkFrame(body, fg_color="transparent", height=BUTTON_HEIGHT)
        buttons.pack(fill="x", pady=(NAV_GAP - 8, 0))
        secondary_button(buttons, t("Cancel"), self.cancel).pack(side="right")
        self.save_button = primary_button(buttons, t("Save"), async_command(self._save_clicked))
        self.save_button.pack(side="right", padx=(0, 12))

        self.bind("<Escape>", lambda _event: self.cancel())
        place_over(self, master)
        activate_later(self)

    async def save(self) -> bool:
        """Store the choices; False (the dialog stays) when that failed."""
        self.save_button.configure(state="disabled")
        changes = self.panel.changes(self._settings.current)
        try:
            if changes:
                await self._settings.update(**changes)
        finally:
            if self.winfo_exists():
                self.save_button.configure(state="normal")
        if self._on_saved is not None:
            self._on_saved()
        self._close()
        return True

    async def _save_clicked(self) -> None:
        await self.save()

    async def _save_and_restart(self) -> None:
        if await self.save() and self._on_restart is not None:
            self._on_restart()

    def cancel(self) -> None:
        self.panel.revert_look(self._saved_look)
        self._close()

    def _close(self) -> None:
        master = self.master
        if self.winfo_exists():
            self.destroy()
        try:  # Settings, modal again
            if master.winfo_exists():
                master.grab_set()
                master.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass
