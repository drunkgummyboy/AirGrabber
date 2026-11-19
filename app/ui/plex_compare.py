"""Settings → Plex & library, under its boxes: "What does a library add?", what AirGrabber does
without a library and with one (Plex, or your own folders: docs/folder-library/), one line per
thing, a ✓ or a dash in each of the two columns (the owner's wish, 2026-11-08; both sources since
the folder library). The table is folded, closed at first ("▸ Show" beside the box's name; spec
§2).

Only what really needs a library is a dash on the left: what you have and miss (the ✓ and
"missing" cues, the calendar's "✓ Downloaded"), skipping what's already in the library, the
Library page, gaps in seasons, and new files counting soon after MediaForge moves them (Plex
scans the folder; the folder library reads it again). The phone app's lines show only where the
phone connection is offered (`phone`): no text tells others about it; their heading says the
phone app needs a library.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.settings_layout import TEXT_SIZE, Fold, SettingsBox, SettingsPage, box_inner
from app.ui.settings_search import add_words

TITLE_TEXT = N_("What does a library add?")
OLD_TITLE = N_("With and without a library")  # what search still finds it by
WITHOUT_TEXT = N_("Without a library")
WITH_TEXT = N_("With Plex or your folders")
PHONE_HEADING = N_("Phone app (needs a library)")
YES = "✓"
NO = "—"
MARK_WIDTH = 130  # each of the two columns (their headings wrap)


class Line(NamedTuple):
    text: str  # N_: translated where it is shown
    without: bool  # there without a library too


AIRGRABBER_LINES = (
    Line(N_("Search, download and follow shows and films"), True),
    Line(N_("Rename and sort files with MediaForge"), True),
    Line(N_("Never download the same release twice"), True),
    Line(N_("Skip what's already in your library"), False),
    Line(N_("See which episodes you have and which are missing"), False),
    Line(N_("The Library page with your posters"), False),
    Line(N_("Find the gaps in your seasons"), False),
    Line(N_("New files count soon after MediaForge moves them"), False),
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
        add_words(self.box.frame, OLD_TITLE)
        self.table = ctk.CTkFrame(self.box.body, fg_color="transparent")
        self.box.add(self.table, gap=10)
        self.fold = Fold(self.box.body, self.table)
        self.fold.button.grid(row=0, column=0, sticky="e")  # beside the box's name
        self.table.grid_columnconfigure(0, weight=1)
        for column in (1, 2):
            self.table.grid_columnconfigure(column, minsize=MARK_WIDTH)
        row = 0
        for column, text in ((1, WITHOUT_TEXT), (2, WITH_TEXT)):
            heading = self._label(t(text), bold=True, colour=theme.TEXT, wrap=MARK_WIDTH - 8)
            heading.configure(anchor="center", justify="center")
            heading.grid(row=row, column=column, padx=4, sticky="s")
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
                wrap = box_inner() - 2 * MARK_WIDTH - 8
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
