"""Settings → Plex, under the Plex box: what AirGrabber does without Plex and with it, one line per
thing, a ✓ or a dash in each of the two columns (the owner's wish, 2026-11-08).

Only what really depends on Plex is a dash on the left: what you have and miss (the ✓ and
"missing" cues, the calendar's "✓ Downloaded"), skipping what's already in the library, the
Library page, gaps in seasons, and Plex scanning the folders MediaForge moves files into. The phone
app's lines show only where the phone connection is offered (`phone`): no text tells others
about it.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.settings_layout import BOX_INNER, TEXT_SIZE, SettingsBox, SettingsPage

TITLE_TEXT = N_("With and without Plex")
WITHOUT_TEXT = N_("Without Plex")
WITH_TEXT = N_("With Plex")
PHONE_HEADING = N_("Phone app")
YES = "✓"
NO = "—"
MARK_WIDTH = 104  # each of the two columns


class Line(NamedTuple):
    text: str  # N_: translated where it is shown
    without: bool  # there without Plex too


AIRGRABBER_LINES = (
    Line(N_("Search, download and follow shows and films"), True),
    Line(N_("Rename and sort files with MediaForge"), True),
    Line(N_("Never download the same release twice"), True),
    Line(N_("Skip what's already in your library"), False),
    Line(N_("See which episodes you have and which are missing"), False),
    Line(N_("The Library page with your posters"), False),
    Line(N_("Find the gaps in your seasons"), False),
    Line(N_("Plex picks up new files as soon as MediaForge moves them"), False),
)
PHONE_LINES = (
    Line(N_("Use the phone app"), False),
    Line(N_("Browse your library on the phone"), False),
    Line(N_("Play on your TVs"), False),
    Line(N_("Take films and episodes along"), False),
    Line(N_("Keep track of what you've watched"), False),
)


def compare_lines(*, phone: bool) -> tuple[tuple[str, tuple[Line, ...]], ...]:
    """The table's parts: (heading, lines); AirGrabber's has no heading, the phone app's shows only
    where the phone connection is offered."""
    parts: tuple[tuple[str, tuple[Line, ...]], ...] = (("", AIRGRABBER_LINES),)
    if phone:
        parts += ((PHONE_HEADING, PHONE_LINES),)
    return parts


class PlexCompareBox:
    """The comparison as a box of its own on the Plex page."""

    def __init__(self, page: SettingsPage, *, phone: bool) -> None:
        self.box: SettingsBox = page.box(t(TITLE_TEXT))
        self.table = ctk.CTkFrame(self.box.body, fg_color="transparent")
        self.box.add(self.table, gap=10)
        self.table.grid_columnconfigure(0, weight=1)
        for column in (1, 2):
            self.table.grid_columnconfigure(column, minsize=MARK_WIDTH)
        row = 0
        for column, text in ((1, WITHOUT_TEXT), (2, WITH_TEXT)):
            self._label(t(text), bold=True, colour=theme.TEXT).grid(row=row, column=column, padx=8)
        self.rows: list[tuple[str, str, str]] = []  # what each line shows: text, left, right
        for heading, lines in compare_lines(phone=phone):
            if heading:
                row += 1
                self._label(t(heading), bold=True, colour=theme.TEXT).grid(
                    row=row, column=0, sticky="w", pady=(12, 0)
                )
            for line in lines:
                row += 1
                text = t(line.text)
                wrap = BOX_INNER - 2 * MARK_WIDTH - 8
                self._label(text, colour=theme.TEXT, wrap=wrap).grid(
                    row=row, column=0, sticky="w", pady=(6, 0)
                )
                marks = (YES if line.without else NO, YES)
                for column, mark in zip((1, 2), marks, strict=True):
                    colour = theme.SUCCESS if mark == YES else theme.TEXT_MUTED
                    self._label(mark, bold=mark == YES, colour=colour).grid(
                        row=row, column=column, pady=(6, 0)
                    )
                self.rows.append((text, *marks))

    def _label(
        self, text: str, *, colour: theme.Color, bold: bool = False, wrap: int = 0
    ) -> ctk.CTkLabel:
        label: Any = ctk.CTkLabel(
            self.table,
            text=text,
            font=theme.font(TEXT_SIZE, bold=bold),
            text_color=colour,
            anchor="w",
            justify="left",
            wraplength=wrap,
        )
        return label
