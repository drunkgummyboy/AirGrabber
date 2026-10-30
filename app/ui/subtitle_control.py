"""Subtitles asked for by hand (2.0.7): the "CC ▾" button of a downloaded episode row in the show
window, of a downloaded film's info window and the Downloads page's "Get subtitles".

The button shows the subtitles already saved ("CC EN NL ▾", green) or none ("CC ▾", muted); its
menu offers "Get English subtitles" per language chosen in Settings and "Get all my languages"
(`cc_menu`). The main window runs the request (SubtitleService.fetch_now, forced: a request by
hand may try again at once) and keeps its state per title in a SubtitleRuns, so every view of
the title follows it: "Looking for subtitles…", then the new mark (the windows reload when
subtitles are saved) or a plain reason ("Not found yet", "Video not in the folder", "Today's
limit reached") for OUTCOME_MS. Without a key in this version or a finished-downloads folder
(`SubtitleActions.offer` NONE) there is no button: a plain green mark, as before. While
Settings → Subtitles' switch is off (OFF) the button shows greyed, its tip saying where to turn
subtitles on.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import customtkinter as ctk

from app.i18n import N_, t
from app.models import SUBTITLE_LANGUAGES, SubtitleFetch, SubtitleFetchOutcome, SubtitleOffer
from app.ui import theme
from app.ui.download_control import DownloadKey
from app.ui.missing_control import MenuItem, popup_menu
from app.ui.subtitle_settings import CREDIT_TEXT
from app.ui.tooltip import add_tooltip

OUTCOME_MS = 60_000  # a request's outcome stays beside its button this long
FONT_SIZE = 12
HEIGHT = 24
GAP = 8

# Shown through t().
MENU_TIP = N_("Get subtitles")
OFF_TIP = N_("Turn on subtitles in Settings → Subtitles")
GET_ONE_TEXT = N_("Get {language} subtitles")
GET_ALL_TEXT = N_("Get all my languages")
LOOKING_TEXT = N_("Looking for subtitles…")
ALREADY_TEXT = N_("Already there")
NOT_FOUND_TEXT = N_("Not found yet")
NOT_FOUND_TIP = N_("OpenSubtitles has none for this yet. Try again later.")
PARTLY_TEXT = N_("Not found yet: {languages}")
NO_VIDEO_TEXT = N_("Video not in the folder")
NO_VIDEO_TIP = N_(
    "AirGrabber couldn't find the video in the folder where finished downloads end up "
    "(Settings → Subtitles)."
)
DOWNLOADING_TEXT = N_("Still downloading")
NO_GRAB_TEXT = N_("No download found")
QUOTA_TEXT = N_("Today's limit reached")
WAIT_TEXT = N_("Tried recently")
FAILED_TEXT = N_("Couldn't get subtitles")
UNAVAILABLE_TEXT = N_("Choose the downloads folder first")


class RunPhase(StrEnum):
    WORKING = "working"
    DONE = "done"  # with a text to show (a reason, or "Already there")


@dataclass(frozen=True, slots=True)
class SubtitleRun:
    """A title's request: working, or done with `text` (and a longer `tip`) beside its
    button."""

    phase: RunPhase
    text: str = ""
    tip: str = ""
    warn: bool = False  # amber: something to fix or to wait for, not just "none yet"


WORKING = SubtitleRun(RunPhase.WORKING)


def language_name(code: str) -> str:
    """"English" / "Engels" for "en"; an unknown code in capitals."""
    name = SUBTITLE_LANGUAGES.get(code)
    return t(name) if name is not None else code.upper()


def cc_menu(
    languages: Sequence[str], fetch: Callable[[tuple[str, ...]], None]
) -> list[MenuItem]:
    """The CC menu: "Get English subtitles" per language, then "Get all my languages" when
    there is more than one."""
    items: list[MenuItem] = [
        (t(GET_ONE_TEXT, language=language_name(code)), _bind(fetch, (code,)))
        for code in languages
    ]
    if len(languages) > 1:
        items.append((t(GET_ALL_TEXT), _bind(fetch, tuple(languages))))
    return items


def _bind(
    fetch: Callable[[tuple[str, ...]], None], languages: tuple[str, ...]
) -> Callable[[], None]:
    return lambda: fetch(languages)


def cc_text(mark: str) -> str:
    """The button: "CC EN NL ▾" with saved subtitles, "CC ▾" without."""
    return f"{mark or 'CC'} ▾"


def codes_text(languages: Sequence[str]) -> str:
    return " ".join(code.upper() for code in languages)


def fetch_run(result: SubtitleFetch) -> SubtitleRun | None:
    """What a finished request shows beside its button; None when the new mark says it all
    (subtitles saved)."""
    outcome = result.outcome
    if outcome is SubtitleFetchOutcome.SAVED:
        return None
    if outcome is SubtitleFetchOutcome.ALREADY:
        return SubtitleRun(RunPhase.DONE, t(ALREADY_TEXT))
    if outcome is SubtitleFetchOutcome.PARTLY:
        text = t(PARTLY_TEXT, languages=codes_text(result.not_found))
        return SubtitleRun(RunPhase.DONE, text, t(NOT_FOUND_TIP))
    if outcome is SubtitleFetchOutcome.NOT_FOUND:
        return SubtitleRun(RunPhase.DONE, t(NOT_FOUND_TEXT), t(NOT_FOUND_TIP))
    if outcome is SubtitleFetchOutcome.NO_VIDEO:
        return SubtitleRun(RunPhase.DONE, t(NO_VIDEO_TEXT), t(NO_VIDEO_TIP), warn=True)
    if outcome is SubtitleFetchOutcome.DOWNLOADING:
        return SubtitleRun(RunPhase.DONE, t(DOWNLOADING_TEXT))
    if outcome is SubtitleFetchOutcome.NO_GRAB:
        return SubtitleRun(RunPhase.DONE, t(NO_GRAB_TEXT), warn=True)
    if outcome is SubtitleFetchOutcome.QUOTA:
        return SubtitleRun(RunPhase.DONE, t(QUOTA_TEXT), result.message, warn=True)
    if outcome is SubtitleFetchOutcome.WAIT:
        return SubtitleRun(RunPhase.DONE, t(WAIT_TEXT), t(NOT_FOUND_TIP))
    if outcome is SubtitleFetchOutcome.UNAVAILABLE:
        return SubtitleRun(RunPhase.DONE, t(UNAVAILABLE_TEXT), result.message, warn=True)
    return SubtitleRun(RunPhase.DONE, t(FAILED_TEXT), result.message, warn=True)


class RunView(Protocol):
    key: DownloadKey

    def repaint(self) -> None: ...

    def winfo_exists(self) -> Any: ...


class SubtitleRuns:
    """The requests' states per title (owned by the main window, survives rebuilt rows) and
    the views showing them."""

    def __init__(self) -> None:
        self._runs: dict[DownloadKey, SubtitleRun] = {}
        self._views: list[RunView] = []

    def get(self, key: DownloadKey) -> SubtitleRun | None:
        return self._runs.get(key)

    def is_working(self, key: DownloadKey) -> bool:
        run = self._runs.get(key)
        return run is not None and run.phase is RunPhase.WORKING

    def set(self, key: DownloadKey, run: SubtitleRun | None) -> None:
        if run is None:
            self._runs.pop(key, None)
        else:
            self._runs[key] = run
        for view in self._live():
            if view.key == key:
                view.repaint()

    def refresh(self) -> None:
        """Repaint every view (the subtitle settings changed: the buttons may be offered or
        greyed now)."""
        for view in self._live():
            view.repaint()

    def expire(self, key: DownloadKey, run: SubtitleRun) -> None:
        """Drop a finished request's text, unless another request replaced it meanwhile."""
        if self._runs.get(key) is run:
            self.set(key, None)

    def attach(self, view: RunView) -> None:
        self._live().append(view)

    def detach(self, view: RunView) -> None:
        if view in self._views:
            self._views.remove(view)

    def _live(self) -> list[RunView]:
        self._views = [view for view in self._views if _exists(view)]
        return self._views


def _exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


@dataclass(frozen=True, slots=True)
class SubtitleActions:
    """What the CC buttons do (the main window provides them)."""

    offer: Callable[[], SubtitleOffer]  # SubtitleService.offer: none, greyed (off) or on
    languages: Callable[[], tuple[str, ...]]  # Settings' languages, in order
    fetch: Callable[[DownloadKey, tuple[str, ...]], None]  # start a request
    runs: SubtitleRuns

    def menu(self, key: DownloadKey) -> list[MenuItem]:
        return cc_menu(self.languages(), lambda languages: self.fetch(key, languages))


class CCControl(ctk.CTkFrame):
    """A title's subtitles beside its name: the request's state, then the "CC ▾" button (greyed
    while subtitles are turned off; without `actions` or while subtitles can't be asked for at
    all, the plain green "CC EN NL" mark). `on_change` hears when its width may have changed
    (the title is fitted again)."""

    def __init__(
        self,
        master: Any,
        *,
        key: DownloadKey,
        actions: SubtitleActions | None,
        on_change: Callable[[], None] | None = None,
        height: int = HEIGHT,
    ) -> None:
        super().__init__(master, fg_color="transparent", width=1, height=height)
        self.key = key
        self._actions = actions
        self._on_change = on_change
        self._mark = ""
        self._shown = False
        self._painted: tuple[Any, ...] | None = None
        font = theme.font(FONT_SIZE, bold=True)
        self.state_label = ctk.CTkLabel(
            self, text="", height=height, font=theme.font(FONT_SIZE), text_color=theme.TEXT_MUTED
        )
        self._state_tip = add_tooltip(self.state_label, "")
        self.mark_label = ctk.CTkLabel(
            self, text="", height=height, font=font, text_color=theme.SUCCESS
        )
        add_tooltip(self.mark_label, t(CREDIT_TEXT))  # the credit OpenSubtitles asks for
        self.button = ctk.CTkButton(
            self,
            text="",
            width=1,
            height=height,
            corner_radius=5,
            font=font,
            fg_color="transparent",
            hover_color=theme.CHIP_HOVER,
            border_width=1,
            border_color=theme.EDGE,
            text_color=theme.TEXT_MUTED,
            text_color_disabled=theme.TEXT_FAINT,
            command=self.open_menu,
        )
        self._button_tip = add_tooltip(self.button, t(MENU_TIP))
        if actions is not None:
            actions.runs.attach(self)

    @property
    def mark(self) -> str:
        """The saved subtitles' mark ("CC EN NL"), "" for none."""
        return self._mark

    @property
    def has_content(self) -> bool:
        """Something shows: the CC ▾ button, or a plain mark."""
        return self._shown and (self.offered or bool(self._mark))

    @property
    def offered(self) -> bool:
        """The CC ▾ button shows (rather than a plain mark), greyed or not."""
        return self._offer() is not SubtitleOffer.NONE

    def _offer(self) -> SubtitleOffer:
        return self._actions.offer() if self._actions is not None else SubtitleOffer.NONE

    def show(self, mark: str, shown: bool = True) -> None:
        """Show `mark` ("CC EN NL", "" for none); `shown` False hides it all (a row that isn't
        downloaded)."""
        self._mark, self._shown = mark, shown
        self.repaint()

    def repaint(self) -> None:
        run = self._actions.runs.get(self.key) if self._actions is not None else None
        offer = self._offer()
        offered = self._shown and offer is not SubtitleOffer.NONE
        look = (self._mark, self._shown, offer, run)
        if look == self._painted:
            return
        self._painted = look
        for widget in (self.state_label, self.mark_label, self.button):
            widget.pack_forget()
        if not self._shown:
            self._changed()
            return
        if not offered:
            if self._mark:
                self.mark_label.configure(text=self._mark)
                self.mark_label.pack(side="right")
            self._changed()
            return
        working = run is not None and run.phase is RunPhase.WORKING
        off = offer is SubtitleOffer.OFF
        green = bool(self._mark) and not off
        self.button.configure(
            text=cc_text(self._mark),
            text_color=theme.SUCCESS if green else theme.TEXT_MUTED,
            border_color=theme.SUCCESS if green else theme.EDGE,
            state="disabled" if working or off else "normal",
        )
        self._button_tip.set_text(t(OFF_TIP) if off else "" if working else t(MENU_TIP))
        self.button.pack(side="right")
        if run is not None and not off:
            text = t(LOOKING_TEXT) if working else run.text
            self.state_label.configure(
                text=text, text_color=theme.WARNING if run.warn else theme.TEXT_MUTED
            )
            self._state_tip.set_text(run.tip)
            self.state_label.pack(side="right", padx=(0, GAP))
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def open_menu(self) -> None:
        actions = self._actions
        if actions is None or actions.runs.is_working(self.key):
            return
        if actions.offer() is not SubtitleOffer.ON:
            return
        popup_menu(self.button, actions.menu(self.key))

    def destroy(self) -> None:
        if self._actions is not None:
            self._actions.runs.detach(self)
        super().destroy()
