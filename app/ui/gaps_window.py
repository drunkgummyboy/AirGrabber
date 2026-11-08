"""My shows' ⋯ → "🧩 Find gaps in Plex…": the episodes Plex doesn't have in the seasons it has
some of, for every show the person follows (LibraryService.gaps; offered only with Plex).

One row per show: its name, its gaps per season ("S01: E03, E05–E07 · S02: E01"), what is on its
way (grabbed, not in Plex yet), and "⬇ Download {n}"; above them "⬇ Download all {n}". A click
queues the show's gaps that were never grabbed as one batch (the main window's
`on_download`), and the button says it was queued. Borderless in AirGrabber's style, over its
window; Escape and the round ✕ close it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import CalendarEntry, Show
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    CLOSE_CLEARANCE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    secondary_button,
    text_label,
)
from app.ui.scrollable import ScrollableFrame

TITLE = N_("Gaps in Plex")
TEXT = N_("Episodes Plex doesn't have, in the seasons it has some of.")
NONE_TEXT = N_("No gaps: every season Plex has of your shows is complete.")
ALL_TEXT = N_("⬇ Download all {n}")
ONE_TEXT = N_("⬇ Download {n}")
QUEUED_TEXT = N_("✓ Queued")
ON_ITS_WAY = N_("{n} on its way")
ON_THEIR_WAY = N_("{n} on their way")
WIDTH = 560  # the list's width (CTk units)
LIST_HEIGHT = 380
ROW_BUTTON_WIDTH = 130


@dataclass(frozen=True, slots=True)
class ShowGaps:
    """One show's gaps: every one (`entries`, by season and number) and how many of them were
    grabbed already (on their way to Plex)."""

    show: Show
    entries: tuple[CalendarEntry, ...]

    @property
    def wanted(self) -> tuple[CalendarEntry, ...]:
        """The gaps never grabbed: what "⬇ Download" queues."""
        return tuple(entry for entry in self.entries if not entry.is_downloaded)

    @property
    def coming(self) -> int:
        return len(self.entries) - len(self.wanted)


def group_gaps(entries: Iterable[CalendarEntry]) -> list[ShowGaps]:
    """The gaps per show, in the order the shows first come (LibraryService.gaps: by name)."""
    shows: dict[int, tuple[Show, list[CalendarEntry]]] = {}
    for entry in entries:
        shows.setdefault(entry.show.id, (entry.show, []))[1].append(entry)
    return [ShowGaps(show, tuple(found)) for show, found in shows.values()]


def _runs(numbers: Sequence[int]) -> list[str]:
    """[3, 5, 6, 7] -> ["E03", "E05–E07"]."""
    parts: list[str] = []
    start = previous = numbers[0]
    for number in [*numbers[1:], None]:
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(f"E{start:02d}" if start == previous else f"E{start:02d}–E{previous:02d}")
        if number is not None:
            start = previous = number
    return parts


def seasons_text(entries: Iterable[CalendarEntry]) -> str:
    """"S01: E03, E05–E07 · S02: E01"."""
    seasons: dict[int, list[int]] = {}
    for entry in entries:
        if entry.episode.number is not None:
            seasons.setdefault(entry.episode.season, []).append(entry.episode.number)
    return " · ".join(
        f"S{season:02d}: " + ", ".join(_runs(sorted(set(numbers))))
        for season, numbers in sorted(seasons.items())
    )


def coming_text(count: int) -> str:
    return tn(ON_ITS_WAY, ON_THEIR_WAY, count) if count else ""


class GapsWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        gaps: Sequence[ShowGaps],
        *,
        on_download: Callable[[Sequence[CalendarEntry]], None],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(t(TITLE))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._on_download = on_download
        self._gaps = list(gaps)
        self.row_buttons: list[ctk.CTkButton] = []
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, t(TITLE), right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        text = t(TEXT) if self._gaps else t(NONE_TEXT)
        text_label(body, text, size=BODY_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )
        if self._gaps:
            rows = ScrollableFrame(body, fg_color="transparent", width=WIDTH, height=LIST_HEIGHT)
            rows.pack(fill="both", expand=True, pady=(NAV_GAP, 0))
            rows.grid_columnconfigure(0, weight=1)
            for index, show_gaps in enumerate(self._gaps):
                self._row(rows, index, show_gaps)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        wanted = sum(len(show_gaps.wanted) for show_gaps in self._gaps)
        self.all_button: ctk.CTkButton | None = None
        if wanted:
            self.all_button = ctk.CTkButton(
                buttons,
                text=t(ALL_TEXT, n=wanted),
                height=BUTTON_HEIGHT,
                corner_radius=BUTTON_RADIUS,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                text_color=theme.ON_ACCENT,
                font=theme.font(15, bold=True),
                command=self.download_all,
            )
            self.all_button.pack(side="right", padx=(12, 0))
        secondary_button(buttons, t("Close"), self.close, width=120).pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    def _row(self, master: Any, index: int, show_gaps: ShowGaps) -> None:
        row = ctk.CTkFrame(master, fg_color=theme.SURFACE, corner_radius=8)
        row.grid(row=index, column=0, sticky="ew", pady=(0, 6))
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row,
            text=show_gaps.show.name,
            font=theme.font(14, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
        detail = seasons_text(show_gaps.entries)
        coming = coming_text(show_gaps.coming)
        ctk.CTkLabel(
            row,
            text=f"{detail} · {coming}" if coming else detail,
            font=theme.font(12),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=WIDTH - ROW_BUTTON_WIDTH - 60,
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))
        wanted = show_gaps.wanted
        if not wanted:
            return
        button = ctk.CTkButton(
            row,
            text=t(ONE_TEXT, n=len(wanted)),
            width=ROW_BUTTON_WIDTH,
            height=30,
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
        )
        button.configure(command=lambda: self.download(show_gaps, button))
        button.grid(row=0, column=1, rowspan=2, padx=12)
        self.row_buttons.append(button)

    def download(self, show_gaps: ShowGaps, button: ctk.CTkButton | None = None) -> None:
        """Queue one show's gaps that were never grabbed."""
        if show_gaps.wanted:
            self._on_download(show_gaps.wanted)
        if button is not None and button.winfo_exists():
            button.configure(text=t(QUEUED_TEXT), state="disabled")

    def download_all(self) -> None:
        for show_gaps, button in zip(self._wanted_rows(), self.row_buttons, strict=True):
            if button.cget("state") != "disabled":
                self.download(show_gaps, button)
        if self.all_button is not None:
            self.all_button.configure(text=t(QUEUED_TEXT), state="disabled")

    def _wanted_rows(self) -> list[ShowGaps]:
        return [show_gaps for show_gaps in self._gaps if show_gaps.wanted]

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()
