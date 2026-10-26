"""Adding FlareSolverr, step by step: the dialog behind Settings → Advanced's "How to add
FlareSolverr" link (Prowlarr panel).

Instructions only: AirGrabber never downloads, starts or configures FlareSolverr. It links to the
official download page, checks whether FlareSolverr runs on this PC (FlareSolverrService) and can
open Prowlarr.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
import webbrowser
from collections.abc import Callable
from enum import Enum
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.services.flaresolverr import FlareSolverrService
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BLOCK_GAP,
    BODY_SIZE,
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
from app.ui.tasks import spawn

DOWNLOAD_URL = "https://github.com/FlareSolverr/FlareSolverr/releases/latest"

# Shown through t().
GUIDE_TITLE = N_("Add FlareSolverr")
PANEL_TIP = N_(
    "Some sites ask for a robot check first. FlareSolverr, a free helper, can let Prowlarr "
    "reach them."
)
PANEL_LINK = N_("How to add FlareSolverr")
STEP_DOWNLOAD = N_(
    "Download FlareSolverr for Windows: open its download page and get "
    "flaresolverr_windows_x64.zip."
)
STEP_START = N_(
    "Unzip it, for example to your Documents, and double-click flaresolverr.exe. A black window "
    "opens: leave it open while you use AirGrabber."
)
STEP_START_HINT = N_(
    'Windows may warn you because it\'s a downloaded program. Choose "More info" and then '
    '"Run anyway" only if you trust it.'
)
STEP_CHECK = N_("Check that it's running:")
STEP_PROXY = N_(
    "In Prowlarr, go to Settings → Indexers, click + under Indexer Proxies and choose "
    "FlareSolverr. Type flaresolverr under Tags and http://localhost:8191/ under Host, then "
    "Test and Save."
)
STEP_TAG = N_(
    "Then open each site that shows a robot-check error in Prowlarr, add the tag flaresolverr, "
    "and save."
)

_NUMBER_SIZE = 18
_NUMBER_ROOM = 40  # the number column
_ROW_GAP = 16
_SIDE_BUTTON_WIDTH = 210
_SIDE_GAP = 16
_TEXT_WIDTH = CONTENT_WIDTH - _NUMBER_ROOM  # a step without a button
_SIDE_TEXT_WIDTH = _TEXT_WIDTH - _SIDE_BUTTON_WIDTH - _SIDE_GAP  # beside a button


class RunCheck(Enum):
    CHECKING = "checking"
    RUNNING = "running"
    NOT_RUNNING = "not_running"


def run_check_message(result: RunCheck) -> tuple[str, theme.Color]:
    """(text, colour) of the result line under step 3."""
    if result is RunCheck.RUNNING:
        return t("✓ FlareSolverr is running"), theme.SUCCESS
    if result is RunCheck.NOT_RUNNING:
        return t("FlareSolverr isn't running on this PC yet."), theme.WARNING
    return t("Checking…"), theme.TEXT_MUTED


class FlareSolverrDialog(ctk.CTkToplevel):
    """Five numbered steps with "Open the download page", "Check" and (when `open_prowlarr` is
    given) "Open Prowlarr"."""

    def __init__(
        self,
        master: ctk.CTk | ctk.CTkToplevel,
        *,
        flaresolverr: FlareSolverrService,
        open_prowlarr: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._flaresolverr = flaresolverr
        self._check_task: asyncio.Task[None] | None = None
        # Settings is modal too: give it its grab back when this dialog closes.
        self._previous_grab = _grabbing_window(master)

        self.title(t(GUIDE_TITLE))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        # Holds the content width.
        ctk.CTkFrame(body, width=CONTENT_WIDTH, height=1, fg_color="transparent").pack()
        heading_row(body, t(GUIDE_TITLE)).pack(fill="x")
        text_label(body, t(PANEL_TIP), size=DETAIL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )

        steps = ctk.CTkFrame(body, fg_color="transparent")
        steps.pack(fill="x", pady=(BLOCK_GAP, 0))
        steps.grid_columnconfigure(1, weight=1)
        self._steps = steps
        self._step(0, t(STEP_DOWNLOAD), button=(t("Open the download page"), _open_download))
        self._step(1, t(STEP_START), hint=t(STEP_START_HINT))
        self._check_button = self._step(2, t(STEP_CHECK), button=(t("Check"), self._check))
        self._result = text_label(steps, "", size=DETAIL_SIZE, wraplength=_TEXT_WIDTH)
        self._result.grid(row=3, column=1, columnspan=2, sticky="w", pady=(LINE_GAP, 0))
        self._step(
            4,
            t(STEP_PROXY),
            button=None if open_prowlarr is None else (t("Open Prowlarr"), open_prowlarr),
        )
        self._step(5, t(STEP_TAG))

        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", pady=(NAV_GAP, 0))
        primary_button(nav, t("Close"), self.destroy).pack(side="right")

        self.bind("<Escape>", lambda _event: self.destroy())
        place_over(self, master)
        activate_later(self)

    def _step(
        self,
        row: int,
        text: str,
        *,
        hint: str | None = None,
        button: tuple[str, Callable[[], None]] | None = None,
    ) -> ctk.CTkButton | None:
        """Grid row `row` of the steps (the result line takes row 3, so step 4 is row 4)."""
        number = row + 1 if row < 3 else row
        pady = (0 if row == 0 else _ROW_GAP, 0)
        ctk.CTkLabel(
            self._steps,
            text=str(number),
            width=28,
            anchor="nw",
            font=theme.font(_NUMBER_SIZE, bold=True),
            text_color=theme.TEXT_MUTED,
        ).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=pady)
        width = _TEXT_WIDTH if button is None else _SIDE_TEXT_WIDTH
        texts = ctk.CTkFrame(self._steps, fg_color="transparent")
        texts.grid(row=row, column=1, columnspan=1 if button else 2, sticky="nw", pady=pady)
        text_label(texts, text, size=BODY_SIZE, wraplength=width).pack(anchor="w")
        if hint:
            text_label(
                texts, hint, size=SMALL_SIZE, color=theme.TEXT_MUTED, wraplength=width
            ).pack(anchor="w", pady=(4, 0))
        if button is None:
            return None
        label, command = button
        make = primary_button if row == 2 else secondary_button
        widget = make(self._steps, label, command, width=_SIDE_BUTTON_WIDTH)
        widget.grid(row=row, column=2, sticky="ne", padx=(_SIDE_GAP, 0), pady=pady)
        return widget

    # -- checking ------------------------------------------------------------------------------

    def _check(self) -> None:
        if self._check_task is not None and not self._check_task.done():
            return
        self._show(RunCheck.CHECKING)
        if self._check_button is not None:
            self._check_button.configure(state="disabled")
        self._check_task = spawn(self._run_check(), name="flaresolverr-check")

    async def _run_check(self) -> None:
        try:
            running = await self._flaresolverr.is_running()
        finally:
            if self.winfo_exists() and self._check_button is not None:
                self._check_button.configure(state="normal")
        if self.winfo_exists():
            self._show(RunCheck.RUNNING if running else RunCheck.NOT_RUNNING)

    def _show(self, result: RunCheck) -> None:
        text, color = run_check_message(result)
        self._result.configure(text=text, text_color=color)

    def destroy(self) -> None:
        if self._check_task is not None:
            self._check_task.cancel()
        previous, self._previous_grab = self._previous_grab, None
        super().destroy()
        if previous is not None:
            try:
                if previous.winfo_exists():
                    previous.grab_set()
            except tk.TclError:
                pass  # the application is shutting down


def _open_download() -> None:
    webbrowser.open(DOWNLOAD_URL)


def _grabbing_window(widget: Any) -> tk.Misc | None:
    """The window holding the application's grab, if any."""
    try:
        current = widget.grab_current()
    except (KeyError, tk.TclError):
        return None
    return current if isinstance(current, tk.Misc) else None
