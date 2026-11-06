"""Settings → Looks → Calendar view: a small picture of the view being chosen. Plain canvas shapes in
the theme's colours: "Weeks" is a grid of days with today somewhere in a row, "Rolling week" one
row with today in the middle."""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from app.models import CalendarLayout
from app.ui import theme

WIDTH = 154
HEIGHT = 62
COLUMNS = 7
GAP = 2
TODAY_IN_WEEKS = 4  # Friday: where today happens to be in a row of "Weeks"
WEEK_ROWS = 3


def cells(layout: CalendarLayout, width: int, height: int, gap: int) -> list[tuple[int, int, int, int, bool]]:
    """The days as (x0, y0, x1, y1, is it today), in pixels."""
    rows = 1 if layout is CalendarLayout.ROLLING else WEEK_ROWS
    today_column = COLUMNS // 2 if layout is CalendarLayout.ROLLING else TODAY_IN_WEEKS
    cell_width = (width - gap * (COLUMNS - 1)) / COLUMNS
    cell_height = (height - gap * (rows - 1)) / rows
    out = []
    for row in range(rows):
        for column in range(COLUMNS):
            x0 = round(column * (cell_width + gap))
            y0 = round(row * (cell_height + gap))
            out.append(
                (
                    x0,
                    y0,
                    round(x0 + cell_width),
                    round(y0 + cell_height),
                    row == 0 and column == today_column,
                )
            )
    return out


class CalendarLayoutPicture(ctk.CTkCanvas):
    """`show(layout)` draws that view."""

    def __init__(self, master: Any, layout: CalendarLayout) -> None:
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(master)) or 1.0
        super().__init__(
            master,
            width=round(WIDTH * self._scale),
            height=round(HEIGHT * self._scale),
            highlightthickness=0,
            bd=0,
        )
        self._layout = layout
        self.show(layout)
        theme.on_mode_change(self, self._repaint)

    def show(self, layout: CalendarLayout) -> None:
        self._layout = layout
        self._repaint()

    def _repaint(self) -> None:
        self.delete("all")
        self.configure(background=theme.pick(theme.SURFACE))
        width, height = round(WIDTH * self._scale), round(HEIGHT * self._scale)
        gap = max(1, round(GAP * self._scale))
        for x0, y0, x1, y1, today in cells(self._layout, width, height, gap):
            self.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                fill=theme.pick(theme.ACCENT if today else theme.PLACEHOLDER),
                outline=theme.pick(theme.ACCENT if today else theme.PLACEHOLDER),
            )
