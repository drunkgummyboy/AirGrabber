"""Settings → Plex → "Connect to Plex…": the small window of Plex's PIN sign-in
(app/services/plex.py). It asks plex.tv for a code, shows it big, offers "Open plex.tv/link"
(the browser) and waits while the user enters the code there ("⟳ Waiting for you to enter the
code…"). Once plex.tv hands over the token the window closes by itself and `on_connected` runs
(Settings then lets the server be picked and the first check run). A code that expired offers
"Get a new code"; plex.tv not answering says so and offers the same. Cancel, Escape and the
round ✕ stop the waiting and close it.

Borderless in AirGrabber's style (app/ui/dialog_kit.py, centred like mockups/plex.html), modal
over the window that opened it, which gets the grab back when it closes. Its tasks are cancelled
when it goes. The code is the only thing about the sign-in it shows; never the token.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.i18n import N_, t
from app.models.plex import PlexPin
from app.services.plex import LINK_URL, PlexError
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    HEADING_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    place_over,
    secondary_button,
)
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.services.plex import PlexService

log = logging.getLogger(__name__)

WIDTH = 420  # the text's width (CTk units)
CODE_WIDTH = 240
CODE_HEIGHT = 72
CODE_SIZE = 40
CODE_FONT = "Consolas"
BUTTON_WIDTH = 200

TITLE_TEXT = N_("Connect to Plex")
INTRO_TEXT = N_("Go to plex.tv/link, sign in if asked, and enter this code:")
OPEN_TEXT = N_("Open plex.tv/link")
GETTING_TEXT = N_("Getting a code from Plex…")
WAITING_TEXT = N_("⟳ Waiting for you to enter the code…")
EXPIRED_TEXT = N_("This code has expired.")
NEW_CODE_TEXT = N_("Get a new code")
NO_ANSWER_TEXT = N_("Plex didn't answer. Check the internet connection and try again.")
NO_CODE = "····"  # the code box before a code is there

State = Literal["getting", "waiting", "expired", "failed"]


class PlexConnectWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        *,
        plex: PlexService,
        on_connected: Callable[[], None] | None = None,
        open_link: Callable[[str], object] = webbrowser.open,
    ) -> None:
        """`on_connected`: once the token arrived and the window closed. `open_link`: opens
        plex.tv/link (tests replace it)."""
        super().__init__(master, fg_color=theme.BG)
        self.title(t(TITLE_TEXT))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._plex = plex
        self._on_connected = on_connected
        self._open_link = open_link
        self._tasks: set[asyncio.Task[None]] = set()
        # Not `state`: Tk's state() (iconic, withdrawn…) is asked by CTk's scaling tracker.
        self.step: State = "getting"
        self.pin: PlexPin | None = None
        self.connected = False

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()  # the width
        self.heading = ctk.CTkLabel(
            body,
            text=t(TITLE_TEXT),
            font=theme.font(HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
        )
        self.heading.pack(padx=CLOSE_CLEARANCE)
        self.intro = ctk.CTkLabel(
            body,
            text=t(INTRO_TEXT),
            font=theme.font(BODY_SIZE),
            text_color=theme.TEXT_MUTED,
            wraplength=WIDTH,
            justify="center",
        )
        self.intro.pack(pady=(LINE_GAP, 0))
        self.code = ctk.CTkLabel(
            body,
            text=NO_CODE,
            width=CODE_WIDTH,
            height=CODE_HEIGHT,
            corner_radius=10,
            fg_color=theme.SURFACE,
            text_color=theme.TEXT,
            font=ctk.CTkFont(family=CODE_FONT, size=CODE_SIZE, weight="bold"),
        )
        self.code.pack(pady=(16, 0))
        self.open_button = ctk.CTkButton(
            body,
            text=t(OPEN_TEXT),
            width=BUTTON_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            fg_color=theme.PLEX,
            hover_color=theme.PLEX_HOVER,
            text_color=theme.ON_PLEX,
            font=theme.font(15, bold=True),
            command=self.open_link,
        )
        self.open_button.pack(pady=(16, 0))
        self.status = ctk.CTkLabel(
            body,
            text=t(GETTING_TEXT),
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
            wraplength=WIDTH,
            justify="center",
        )
        self.status.pack(pady=(12, 0))
        self.buttons = ctk.CTkFrame(body, fg_color="transparent")
        self.buttons.pack(pady=(NAV_GAP - 8, 0))
        self.new_code_button = secondary_button(
            self.buttons, t(NEW_CODE_TEXT), self.new_code, width=BUTTON_WIDTH
        )
        self.cancel_button = secondary_button(self.buttons, t("Cancel"), self.close, width=120)
        self.cancel_button.pack(side="left")

        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(
            self, drag_widgets=(self, body, self.heading, self.intro, self.status, self.buttons)
        )
        place_over(self, master)
        activate_later(self)
        self.new_code()

    # -- states ------------------------------------------------------------------------------

    def _show(self, state: State) -> None:
        self.step = state
        texts: dict[State, str] = {
            "getting": t(GETTING_TEXT),
            "waiting": t(WAITING_TEXT),
            "expired": t(EXPIRED_TEXT),
            "failed": t(NO_ANSWER_TEXT),
        }
        self.status.configure(
            text=texts[state],
            text_color=theme.WARNING if state in ("expired", "failed") else theme.TEXT_MUTED,
        )
        offer_new = state in ("expired", "failed")
        self.open_button.configure(state="normal" if state == "waiting" else "disabled")
        if offer_new:
            self.cancel_button.pack_forget()
            self.new_code_button.pack(side="left", padx=(0, 12))
            self.cancel_button.pack(side="left")
            if state == "expired":
                self.code.configure(text_color=theme.TEXT_FAINT)
        else:
            self.new_code_button.pack_forget()
            self.code.configure(text_color=theme.TEXT)

    def new_code(self) -> None:
        """Ask plex.tv for a (new) code and wait for it to be entered."""
        for task in list(self._tasks):
            task.cancel()
        self.pin = None
        self.code.configure(text=NO_CODE)
        self._show("getting")
        self._run(self._sign_in(), "PlexConnectWindow.sign_in")

    async def _sign_in(self) -> None:
        try:
            pin = await self._plex.new_pin()
        except PlexError as exc:
            log.info("Plex: no sign-in code (%s)", type(exc).__name__)
            if self.winfo_exists():
                self._show("failed")
            return
        if not self.winfo_exists():
            return
        self.pin = pin
        self.code.configure(text=pin.code)
        self._show("waiting")
        if await self._plex.wait_for_pin(pin):
            self.connected = True
            self.finish()
            if self._on_connected is not None:
                self._on_connected()
        elif self.winfo_exists():
            self._show("expired")

    def open_link(self) -> None:
        self._open_link(LINK_URL)

    # -- closing -----------------------------------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        """Cancel: stop waiting and close."""
        self.finish()

    def finish(self) -> None:
        """Close, giving the window under it the grab back."""
        master = self.master
        if self.winfo_exists():
            self.destroy()
        try:  # Settings, modal again
            if master.winfo_exists():
                master.grab_set()
                master.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass

    def destroy(self) -> None:
        current = asyncio.current_task() if _loop_running() else None
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        self._tasks.clear()
        super().destroy()


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
