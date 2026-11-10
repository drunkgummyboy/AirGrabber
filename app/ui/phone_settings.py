"""Settings → Phone connection: the box over PhoneService (app/services/phone/service.py).

A switch; the first time, a line that Windows will ask about the network; what's wrong in plain
words; "Connect a phone" with the code to scan (black on white in both looks: a scanner needs
that); and a box listing the connected phones. Each phone (the owner's wish, 2026-11-08): its name
with Rename (in place: Enter saves, Escape doesn't), the person it belongs to (a choice while there
is more than one), its own Library manager tick (the first phone pairs as one, later ones don't),
"What's on it…" (app/ui/phone_contents.py) and Disconnect. Under the phones, "Get the phone app…": the
Companion app's download code (app/ui/phone_app.py).

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
from app.models import PairedPhone
from app.services.phone.devices import CODE_LIFETIME
from app.services.phone.service import NEEDS_PLEX_TEXT, PACKAGES_MISSING_TEXT, PairingUnavailable
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, DETAIL_SIZE, primary_button, secondary_button
from app.ui.people import person_name
from app.ui.phone_app import GET_APP_BUTTON, GET_APP_TITLE, PhoneAppDialog
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
RENAME_TEXT = N_("Rename")
SAVE_NAME_TEXT = N_("Save")
CONTENTS_TEXT = N_("What's on it…")

CODE_SIDE = 240  # logical units: comfortably scanned from a screen
CONNECT_WIDTH = 180
DISCONNECT_WIDTH = 130
RENAME_WIDTH = 110
CONTENTS_WIDTH = 150
APP_WIDTH = 190
PERSON_WIDTH = 160
NAME_GAP = 12  # between a phone's name and its buttons
PHONE_GAP = 18  # between two phones
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
        self._people_shown = phone.people
        self.code_image: ctk.CTkImage | None = None
        # What the list shows per phone, by its id (tests reach in here).
        self.name_labels: dict[str, ctk.CTkLabel] = {}
        self.rename_buttons: dict[str, ctk.CTkButton] = {}
        self.manager_ticks: dict[str, ctk.CTkCheckBox] = {}
        self.person_menus: dict[str, ctk.CTkOptionMenu] = {}
        self.contents_buttons: dict[str, ctk.CTkButton] = {}
        self.name_entry: ctk.CTkEntry | None = None  # while a phone is being renamed
        self._renaming: str | None = None
        self._open_contents: Any = None
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
        self.connect_button = primary_button(
            body, t(CONNECT_TEXT), self.show_code, width=CONNECT_WIDTH
        )
        self.box.add(self.connect_button, gap=12, sticky="w")
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
        self.app_dialog: PhoneAppDialog | None = None
        self.app_button = secondary_button(
            self.phones_box.body, t(GET_APP_BUTTON), self.show_app_code, width=APP_WIDTH
        )
        self.phones_box.add(self.app_button, gap=PHONE_GAP, sticky="w")

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
        _show(self.app_button, plex)  # no Plex, no phone app
        if not plex:
            self._cancel_code()
            self._hide_code()
            for widget in (
                self.switch,
                self._firewall,
                self._problem,
                self._notice,
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
        _show(self.connect_button, phone.running)
        if not phone.running:
            self._cancel_code()
            self._hide_code()
            _show_text(self._code_message, None)
        phones = phone.phones()
        people = phone.people
        if phones != self._phones_shown or people != self._people_shown:
            if len(phones) > len(self._phones_shown):  # the code has done its work
                self._cancel_code()
                self._hide_code()
                _show_text(self._code_message, None)
            self._phones_shown = phones
            self._people_shown = people
            self._fill_list()

    def _fill_list(self) -> None:
        for child in self._list.winfo_children():
            child.destroy()
        self.name_labels.clear()
        self.rename_buttons.clear()
        self.manager_ticks.clear()
        self.person_menus.clear()
        self.contents_buttons.clear()
        self.name_entry = None
        self._renaming = None
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
            self._phone_block(phone).grid(
                row=row, column=0, sticky="ew", pady=(0 if row == 0 else PHONE_GAP, 0)
            )

    def _phone_block(self, phone: PairedPhone) -> ctk.CTkFrame:
        """One phone: its name with Rename and Disconnect; under it its person, its role and
        what's on it."""
        block = ctk.CTkFrame(self._list, fg_color="transparent")
        block.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            block,
            text=phone.name,
            font=theme.font(DETAIL_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER - RENAME_WIDTH - DISCONNECT_WIDTH - 2 * NAME_GAP,
        )
        name.grid(row=0, column=0, sticky="w")
        self.name_labels[phone.id] = name
        rename = secondary_button(
            block, t(RENAME_TEXT), partial(self.rename, phone.id), width=RENAME_WIDTH
        )
        rename.grid(row=0, column=1, sticky="e", padx=(NAME_GAP, 0))
        self.rename_buttons[phone.id] = rename
        secondary_button(
            block, t(DISCONNECT_TEXT), partial(self._disconnect, phone.id), width=DISCONNECT_WIDTH
        ).grid(row=0, column=2, sticky="e", padx=(NAME_GAP, 0))

        line = ctk.CTkFrame(block, fg_color="transparent")
        line.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        column = 0
        people = self._people_shown
        if len(people) > 1:
            names = {person_name(person): person.id for person in people}
            menu = ctk.CTkOptionMenu(
                line,
                values=list(names),
                width=PERSON_WIDTH,
                height=BUTTON_HEIGHT - 6,
                font=theme.font(DETAIL_SIZE),
                fg_color=theme.SURFACE,
                button_color=theme.EDGE,
                button_hover_color=theme.ACCENT_HOVER,
                text_color=theme.TEXT,
                dropdown_font=theme.font(DETAIL_SIZE),
                command=partial(self._person_chosen, phone.id, names),
            )
            owner = next((one for one in people if one.id == phone.profile_id), None)
            menu.set(person_name(owner) if owner is not None else "")
            menu.grid(row=0, column=column, padx=(0, 16))
            self.person_menus[phone.id] = menu
            column += 1
        tick = ctk.CTkCheckBox(
            line,
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
            command=partial(self._tick_changed, phone.id),
        )
        if phone.manager:
            tick.select()
        tick.grid(row=0, column=column, padx=(0, 16))
        self.manager_ticks[phone.id] = tick
        contents = secondary_button(
            line, t(CONTENTS_TEXT), partial(self.show_contents, phone.id), width=CONTENTS_WIDTH
        )
        contents.grid(row=0, column=column + 1)
        self.contents_buttons[phone.id] = contents
        return block

    def _disconnect(self, id: str) -> None:
        self._start(self._phone.disconnect(id), "phone-disconnect")

    # -- a phone's name, role and person ---------------------------------------------------------

    def rename(self, id: str) -> None:
        """The name becomes a field to type in, and the button saves it."""
        if self._renaming == id and self.name_entry is not None:
            self._save_name()
            return
        self._stop_renaming()
        label = self.name_labels.get(id)
        phone = self._shown_phone(id)
        if label is None or phone is None:
            return
        entry = ctk.CTkEntry(
            label.master,
            font=theme.font(DETAIL_SIZE),
            height=BUTTON_HEIGHT - 6,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            text_color=theme.TEXT,
        )
        entry.insert(0, phone.name)
        label.grid_remove()
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda _event: self._save_name())
        entry.bind("<Escape>", lambda _event: self._stop_renaming())
        entry.focus_set()
        entry.select_range(0, "end")
        self.name_entry = entry
        self._renaming = id
        self.rename_buttons[id].configure(text=t(SAVE_NAME_TEXT))

    def _save_name(self) -> None:
        id, entry = self._renaming, self.name_entry
        if id is None or entry is None:
            return
        typed = entry.get().strip()
        self._stop_renaming()
        phone = self._shown_phone(id)
        if typed and phone is not None and typed != phone.name:
            self._start(self._phone.change_phone(id, name=typed), "phone-rename")

    def _stop_renaming(self) -> None:
        id, entry = self._renaming, self.name_entry
        self._renaming = None
        self.name_entry = None
        if entry is not None:
            entry.destroy()
        if id is not None:
            if id in self.name_labels:
                self.name_labels[id].grid()
            if id in self.rename_buttons:
                self.rename_buttons[id].configure(text=t(RENAME_TEXT))

    def _shown_phone(self, id: str) -> PairedPhone | None:
        return next((one for one in self._phones_shown if one.id == id), None)

    def _tick_changed(self, id: str) -> None:
        tick = self.manager_ticks.get(id)
        if tick is not None:
            self._start(self._phone.change_phone(id, manager=bool(tick.get())), "phone-role")

    def _person_chosen(self, id: str, names: dict[str, int], chosen: str) -> None:
        if chosen in names:
            self.choose_person(id, names[chosen])

    def choose_person(self, id: str, profile_id: int) -> None:
        self._start(self._phone.change_phone(id, profile_id=profile_id), "phone-person")

    def show_contents(self, id: str) -> None:
        """The phone's menu: what's on it and what was sent to it (one at a time)."""
        from app.ui.phone_contents import PhoneContentsWindow

        self._close_contents()
        phone = self._shown_phone(id)
        if phone is None:
            return
        self._open_contents = PhoneContentsWindow(
            self.box.frame.winfo_toplevel(), phone=phone, sends=self._phone.sends
        )

    def _close_contents(self) -> None:
        window, self._open_contents = self._open_contents, None
        if window is not None and window.winfo_exists():
            window.close()

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

    # -- the phone app ---------------------------------------------------------------------------

    def show_app_code(self) -> None:
        """The Companion app's download code, one window at a time."""
        if self.app_dialog is not None and self.app_dialog.winfo_exists():
            self.app_dialog.lift()
            return
        self.app_dialog = PhoneAppDialog(self.box.frame.winfo_toplevel(), title=t(GET_APP_TITLE))

    # -- the code --------------------------------------------------------------------------------

    def show_code(self) -> None:
        """A new code on screen, for as long as it's good. Clicking again makes another."""
        self._cancel_code()
        try:
            payload = self._phone.new_pairing_payload()
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
        self._close_contents()


def _show(widget: Any, shown: bool) -> None:
    if shown:
        widget.grid()
    else:
        widget.grid_remove()


def _show_text(label: ctk.CTkLabel, text: str | None) -> None:
    if text:
        label.configure(text=text)
    _show(label, bool(text))
