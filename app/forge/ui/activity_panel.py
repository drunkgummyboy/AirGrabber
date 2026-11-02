"""The Activity panel: what Forge did, opened from the band's "Activity" button over the top right
of the Forge window's pages (it was a strip under the pages, ported from MediaForge's
ui/log_strip.py).

MediaForge's strip polled a logging handler; this panel listens to Forge's `ActivityFeed`
(app/forge/core/activity.py), whose lines are already translated: the feed's earlier lines
first, then each new line (written on idle, in batches). Lines are coloured by level (errors
red, warnings amber, successes green) with `theme.pick`, repainted on a light/dark switch. Its
✕ calls `on_close` (the window hides it).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.forge.core.activity import MAX_LINES, ActivityFeed, ActivityLine
from app.forge.ui.look import forge_look
from app.i18n import t
from app.ui import theme

PANEL_WIDTH = 520  # CTk units
TAG_COLOURS: dict[str, theme.Pair] = {
    "error": theme.DANGER,
    "warning": theme.WARNING,
    "success": theme.SUCCESS,
}


def tag_for(line: ActivityLine) -> str | None:
    """The colour tag a line is shown with (None: the plain text colour)."""
    if line.level >= logging.ERROR:
        return "error"
    if line.level >= logging.WARNING:
        return "warning"
    return "success" if line.success else None


class ActivityPanel(ctk.CTkFrame):
    """What Forge did, newest at the bottom; `feed`'s earlier lines are shown first."""

    def __init__(
        self,
        master: Any,
        feed: ActivityFeed,
        *,
        look: theme.Look | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._look: theme.Look = forge_look() if look is None else look
        super().__init__(
            master,
            width=PANEL_WIDTH,
            fg_color=self._look.SURFACE,
            corner_radius=12,
            border_width=1,
            border_color=self._look.STACK_FAR_EDGE,
        )
        self.on_close: Callable[[], None] = on_close or (lambda: None)
        self._pending: list[ActivityLine] = []
        self._flush_id: str | None = None

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(10, 4))
        self.title_label = ctk.CTkLabel(
            header, text=t("Activity"), font=theme.font(14, bold=True), text_color=theme.TEXT
        )
        self.title_label.pack(side="left")
        self.close_button = ctk.CTkButton(
            header,
            text="✕",
            width=28,
            height=26,
            fg_color="transparent",
            hover_color=self._look.CHIP,
            text_color=theme.TEXT_MUTED,
            font=theme.font(13),
            command=lambda: self.on_close(),
        )
        self.close_button.pack(side="right")
        self._box = ctk.CTkTextbox(
            self,
            fg_color=self._look.SURFACE,
            text_color=theme.TEXT,
            font=theme.font(12),
            wrap="word",
            width=PANEL_WIDTH - 16,
            activate_scrollbars=True,
            scrollbar_button_color=self._look.SCROLLBAR,
            scrollbar_button_hover_color=self._look.EDGE,
        )
        self._box.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        self._paint_tags()
        theme.on_mode_change(self, self._paint_tags)
        self._box.configure(state="disabled")

        self._write(feed.lines())
        self._unsubscribe: Callable[[], None] | None = feed.subscribe(self._heard)

    # --- lines ------------------------------------------------------------------------------------

    def _heard(self, line: ActivityLine) -> None:
        """A new line from the feed (on the loop): written together with its neighbours."""
        self._pending.append(line)
        if self._flush_id is None:
            self._flush_id = self.after_idle(self._flush)

    def _flush(self) -> None:
        self._flush_id = None
        lines, self._pending = self._pending, []
        if self.winfo_exists():
            self._write(lines)

    def _write(self, lines: tuple[ActivityLine, ...] | list[ActivityLine]) -> None:
        if not lines:
            return
        box = self._box
        box.configure(state="normal")
        for line in lines:
            tag = tag_for(line)
            if tag is None:
                box.insert("end", line.text + "\n")
            else:
                box.insert("end", line.text + "\n", tag)
        excess = int(box.index("end-1c").split(".")[0]) - 1 - MAX_LINES
        if excess > 0:
            box.delete("1.0", f"{excess + 1}.0")
        box.see("end")
        box.configure(state="disabled")

    def see_end(self) -> None:
        self._box.see("end")

    def _paint_tags(self) -> None:
        for tag, colour in TAG_COLOURS.items():
            self._box.tag_config(tag, foreground=theme.pick(colour))

    def clear(self) -> None:
        self._box.configure(state="normal")
        self._box.delete("1.0", "end")
        self._box.configure(state="disabled")

    def text(self) -> str:
        return str(self._box.get("1.0", "end"))

    def destroy(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._flush_id is not None:
            self.after_cancel(self._flush_id)
            self._flush_id = None
        super().destroy()
