"""The phone app's download code (the owner's wish, 2026-11-10): a code a phone's camera opens as
PHONE_APP_URL, where the Companion app's file (an APK, ~100 MB: too big for a file in the repository)
waits on AirGrabber's GitHub page as the `phone-app` release's download; each new version replaces
that file, so the link stays the same.

Shown when the hidden switch turns the phone features on (Settings → Updates, app/ui/settings_dialog.py),
then with the restart it needs; Off says so without a code. Settings → Phone connection's "Get the phone
app…" shows it too. Black on white in both looks (a scanner needs that); without segno the link alone.
Borderless in AirGrabber's style; Escape, the ✕ and Close / Later close it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    link_label,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)

PHONE_APP_URL = (
    "https://github.com/drunkgummyboy/AirGrabber/releases/download/phone-app/AirGrabber-Companion.apk"
)
GET_APP_TITLE = N_("Get the phone app")
GET_APP_BUTTON = N_("Get the phone app…")
SCAN_APP_TEXT = N_(
    "Point your phone's camera at this code to download the Companion app, then open the file to "
    "install it. Your phone may warn that the app isn't from the Play Store: that's expected."
)
LINK_APP_TEXT = N_("Open this link on your phone to download the Companion app:")
LATER_TEXT = N_("Later")
RESTART_TEXT = N_("Restart now")
CLOSE_TEXT = N_("Close")
WIDTH = 440  # the text's width (CTk units)
CODE_SIDE = 220  # logical units
BUTTON_WIDTH = 130


class PhoneAppDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        *,
        title: str,
        note: str | None = None,
        show_code: bool = True,
        on_restart: Callable[[], object] | None = None,
    ) -> None:
        """`note`: a line under the heading. `show_code`: the download code and its link.
        `on_restart`: Later and Restart now instead of Close."""
        super().__init__(master, fg_color=theme.BG)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._on_restart = on_restart
        self.code_image: ctk.CTkImage | None = None
        self.restart_button: ctk.CTkButton | None = None

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, title, right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        if note:
            text_label(body, note, size=BODY_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH).pack(
                anchor="w", pady=(LINE_GAP, 0)
            )
        if show_code:
            self._add_code(body)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(NAV_GAP, 0))
        if on_restart is not None:
            self.restart_button = primary_button(
                buttons, t(RESTART_TEXT), self.restart, width=BUTTON_WIDTH
            )
            self.restart_button.pack(side="right", padx=(12, 0))
            self.close_button = secondary_button(buttons, t(LATER_TEXT), self.close, width=BUTTON_WIDTH)
        else:
            self.close_button = secondary_button(buttons, t(CLOSE_TEXT), self.close, width=BUTTON_WIDTH)
        self.close_button.pack(side="right")
        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading, buttons))
        place_over(self, master)
        activate_later(self)

    def _add_code(self, body: ctk.CTkFrame) -> None:
        from app.ui.phone_settings import code_picture  # phone_settings opens this window

        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        try:
            picture = code_picture(PHONE_APP_URL, round(CODE_SIDE * scaling))
        except ImportError:
            picture = None
        if picture is not None:
            text_label(
                body, t(SCAN_APP_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
            ).pack(anchor="w", pady=(LINE_GAP + 8, 0))
            self.code_image = ctk.CTkImage(
                light_image=picture, size=(picture.width / scaling, picture.height / scaling)
            )
            ctk.CTkLabel(body, text="", image=self.code_image).pack(pady=(LINE_GAP + 4, 0))
        else:
            text_label(
                body, t(LINK_APP_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
            ).pack(anchor="w", pady=(LINE_GAP + 8, 0))
        link = link_label(body, PHONE_APP_URL, PHONE_APP_URL, size=DETAIL_SIZE - 2)
        link.configure(wraplength=WIDTH, justify="left")
        link.pack(anchor="w", pady=(LINE_GAP, 0))

    def restart(self) -> None:
        on_restart = self._on_restart
        self.close()
        if on_restart is not None:
            on_restart()

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()
