"""Settings → Phone connection: the box over PhoneService (app/services/phone/service.py).

A switch; the first time, a line that Windows will ask about the network; what's wrong in plain
words; a tick for whether the next phone is a library manager's (on for the first phone, off
afterward); "Connect a phone" with the code to scan (black on white in both looks: a scanner needs
that); and a box listing the connected phones, each with "Disconnect".

Without Plex the page is one line, NEEDS_PLEX_TEXT: no Plex, no phone app. It follows Plex
connecting and disconnecting while it is open.

Everything acts at once (like the Plex box), not on Save. The box follows the service
(`PhoneService.subscribe`) and stops when it goes.

segno is optional (below `# optional` in requirements.txt), so it is imported only in `code_picture`.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Coroutine
from functools import partial
from typing import TYPE_CHECKING, Any

import customtkinter as ctk
from PIL import Image

from app.i18n import N_, t
from app.services.phone.devices import CODE_LIFETIME
from app.services.phone.service import NEEDS_PLEX_TEXT, PACKAGES_MISSING_TEXT, PairingUnavailable
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, DETAIL_SIZE, primary_button, secondary_button
from app.ui.settings_layout import BOX_INNER, SettingsBox, SettingsPage
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.services.phone.service import PhoneService

SWITCH_TEXT = N_("Phone connection")
FIREWALL_TEXT = N_("Windows may ask if AirGrabber may use your network. Choose Allow.")
MANAGER_TEXT = N_("Library manager")
CONNECT_TEXT = N_("Connect a phone")
SCAN_TEXT = N_("Scan this with the Companion app")
CODE_EXPIRED_TEXT = N_("That code has expired, so click Connect a phone for a new one.")
PHONES_TITLE = N_("Connected phones")
NO_PHONES_TEXT = N_("No phones are connected yet.")
DISCONNECT_TEXT = N_("Disconnect")

CODE_SIDE = 240  # logical units: comfortably scanned from a screen
CONNECT_WIDTH = 180
DISCONNECT_WIDTH = 130
NAME_GAP = 12  # between a phone's name and its button
QUIET_MODULES = 4  # the white edge a scanner needs around the code
_BLACK = 0
_WHITE = 255


def code_picture(payload: str, side: int) -> Image.Image:
    """`payload` as a scannable code (greyscale) at most `side` pixels square, each of its dots a
    whole number of pixels, or a scanner would meet blurred edges. ImportError without segno."""
    import segno

    rows = [
        bytes(_BLACK if dark else _WHITE for dark in row)
        for row in segno.make(payload, micro=False).matrix_iter(border=QUIET_MODULES)
    ]
    count = len(rows)
    picture = Image.frombytes("L", (count, count), b"".join(rows))
    scale = max(1, side // count)
    return picture.resize((count * scale, count * scale), Image.Resampling.NEAREST)


class PhoneBox:
    """The Phone connection page's boxes (see the module docstring)."""

    def __init__(
        self,
        page: SettingsPage,
        phone: PhoneService,
        *,
        code_seconds: float = CODE_LIFETIME.total_seconds(),
    ) -> None:
        """`code_seconds`: how long a code stays on screen (tests)."""
        self._phone = phone
        self._code_seconds = code_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        self._code_task: asyncio.Task[None] | None = None
        self._phones_shown = phone.phones()
        self.code_image: ctk.CTkImage | None = None
        # Stays for as long as this window is open: that's when Windows asks.
        self._first_time = not phone.ever_started

        self.box: SettingsBox = page.box()
        body = self.box.body
        self.needs_plex = self.box.text(t(NEEDS_PLEX_TEXT))
        self.box.add(self.needs_plex, gap=0, sticky="w")
        self.switch = ctk.CTkSwitch(
            body,
            text=t(SWITCH_TEXT),
            height=BUTTON_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            progress_color=theme.ACCENT,
            command=self._switched,
        )
        self.box.add(self.switch, gap=0, sticky="w")
        self._firewall = self.box.text(t(FIREWALL_TEXT))
        self.box.add(self._firewall, gap=6, sticky="w")
        self._problem = self._line(body, theme.DANGER)
        self.box.add(self._problem, gap=8, sticky="w")
        self._notice = self._line(body, theme.WARNING)
        self.box.add(self._notice, gap=8, sticky="w")
        self.manager_tick = ctk.CTkCheckBox(
            body,
            text=t(MANAGER_TEXT),
            width=0,
            height=BUTTON_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=6,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
            command=self._tick_changed,
        )
        if not self._phones_shown:
            self.manager_tick.select()
        self.box.add(self.manager_tick, gap=12, sticky="w")
        self.connect_button = primary_button(
            body, t(CONNECT_TEXT), self.show_code, width=CONNECT_WIDTH
        )
        self.box.add(self.connect_button, gap=8, sticky="w")
        self._code_message = self._line(body, theme.TEXT_MUTED)
        self.box.add(self._code_message, gap=8, sticky="w")
        self._code_label = ctk.CTkLabel(body, text="")
        self.box.add(self._code_label, gap=12, sticky="w")
        self._scan_line = self.box.text(t(SCAN_TEXT))
        self.box.add(self._scan_line, gap=6, sticky="w")

        self.phones_box: SettingsBox = page.box(t(PHONES_TITLE))
        self._list = ctk.CTkFrame(self.phones_box.body, fg_color="transparent")
        self.phones_box.add(self._list, gap=8)
        self._list.grid_columnconfigure(0, weight=1)

        self._hide_code()
        self._unsubscribe = phone.subscribe(self.refresh)
        self.listening = True
        # Tk's own bind: CTkFrame.bind would bind its canvas.
        tk.Misc.bind(self.box.frame, "<Destroy>", self._on_destroy, add="+")
        self._fill_list()
        self.refresh()

    def _line(self, master: Any, colour: theme.Color) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            master,
            text="",
            font=theme.font(DETAIL_SIZE),
            text_color=colour,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER,
        )

    # -- following the service -------------------------------------------------------------------

    def refresh(self) -> None:
        """Show the service as it is now."""
        if not self.listening:
            return
        phone = self._phone
        plex = phone.plex_connected
        _show(self.needs_plex, not plex)
        _show(self.phones_box.frame, plex)
        if not plex:
            self._cancel_code()
            self._hide_code()
            for widget in (
                self.switch,
                self._firewall,
                self._problem,
                self._notice,
                self.manager_tick,
                self.connect_button,
                self._code_message,
            ):
                _show(widget, False)
            return
        _show(self.switch, True)
        if phone.on:
            self.switch.select()
        else:
            self.switch.deselect()
        _show(self._firewall, self._first_time)
        _show_text(self._problem, phone.problem)
        _show_text(self._notice, phone.notice)
        _show(self.manager_tick, phone.running)
        _show(self.connect_button, phone.running)
        if not phone.running:
            self._cancel_code()
            self._hide_code()
            _show_text(self._code_message, None)
        phones = phone.phones()
        if phones != self._phones_shown:
            if len(phones) > len(self._phones_shown):  # the code has done its work
                self._cancel_code()
                self._hide_code()
                _show_text(self._code_message, None)
                self.manager_tick.deselect()
            elif not phones:
                self.manager_tick.select()
            self._phones_shown = phones
            self._fill_list()

    def _fill_list(self) -> None:
        for child in self._list.winfo_children():
            child.destroy()
        if not self._phones_shown:
            ctk.CTkLabel(
                self._list,
                text=t(NO_PHONES_TEXT),
                font=theme.font(DETAIL_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            return
        for row, phone in enumerate(self._phones_shown):
            ctk.CTkLabel(
                self._list,
                text=phone.name,
                font=theme.font(DETAIL_SIZE),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=BOX_INNER - DISCONNECT_WIDTH - NAME_GAP,
            ).grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 8, 0))
            secondary_button(
                self._list,
                t(DISCONNECT_TEXT),
                partial(self._disconnect, phone.id),
                width=DISCONNECT_WIDTH,
            ).grid(row=row, column=1, sticky="e", pady=(0 if row == 0 else 8, 0))

    def _disconnect(self, id: str) -> None:
        self._start(self._phone.disconnect(id), "phone-disconnect")

    # -- the switch ------------------------------------------------------------------------------

    def _switched(self) -> None:
        self.switch.configure(state="disabled")
        self._start(self._set_on(bool(self.switch.get())), "phone-switch")

    async def _set_on(self, on: bool) -> None:
        try:
            await self._phone.set_on(on)
        finally:
            if self.listening:
                self.switch.configure(state="normal")
                self.refresh()  # also when saving failed: the switch goes back

    # -- the code --------------------------------------------------------------------------------

    def show_code(self) -> None:
        """A new code on screen, for as long as it's good. Clicking again makes another."""
        self._cancel_code()
        try:
            payload = self._phone.new_pairing_payload(bool(self.manager_tick.get()))
        except PairingUnavailable as exc:
            self._hide_code()
            _show_text(self._code_message, str(exc))
            return
        scaling = ctk.ScalingTracker.get_widget_scaling(self.box.body)
        try:
            picture = code_picture(payload, round(CODE_SIDE * scaling))
        except ImportError:
            self._hide_code()
            _show_text(self._code_message, t(PACKAGES_MISSING_TEXT))
            return
        self.code_image = ctk.CTkImage(
            light_image=picture,
            size=(picture.width / scaling, picture.height / scaling),
        )
        self._code_label.configure(image=self.code_image)
        _show_text(self._code_message, None)
        _show(self._code_label, True)
        _show(self._scan_line, True)
        self._code_task = self._start(self._expire_code(), "phone-code")

    def _tick_changed(self) -> None:
        """A code on screen pairs what the tick said when it was made, so it makes way."""
        if self._code_task is not None and not self._code_task.done():
            self.show_code()

    async def _expire_code(self) -> None:
        await asyncio.sleep(self._code_seconds)  # cancelled when the box goes
        self._hide_code()
        _show_text(self._code_message, t(CODE_EXPIRED_TEXT))

    def _hide_code(self) -> None:
        _show(self._code_label, False)
        _show(self._scan_line, False)

    def _cancel_code(self) -> None:
        if self._code_task is not None:
            self._code_task.cancel()
            self._code_task = None

    # -- tasks -----------------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def settled(self) -> None:
        """Wait for what a click started, not for a code's time on screen (tests)."""
        waiting = self._tasks - ({self._code_task} if self._code_task is not None else set())
        if waiting:
            await asyncio.wait(waiting)

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.box.frame:
            return
        self.listening = False
        self._unsubscribe()
        # Only the code: turning the connection on or off goes on without the window.
        self._cancel_code()


def _show(widget: Any, shown: bool) -> None:
    if shown:
        widget.grid()
    else:
        widget.grid_remove()


def _show_text(label: ctk.CTkLabel, text: str | None) -> None:
    if text:
        label.configure(text=text)
    _show(label, bool(text))
