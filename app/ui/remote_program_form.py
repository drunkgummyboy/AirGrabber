"""The form for a torrent program on a NAS or another computer: program, address, user name,
password, destination folder, certificate check, and "Test connection" with its result line.
Shared by Settings → General ("When I download") and the first-run welcome.

The form only edits; its host stores `changes()` (Settings' Save, the welcome's Next) and hands
it `run_test`, which saves first and then tests, as the other test buttons do.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import customtkinter as ctk
from pydantic import ValidationError

from app.models import RemoteKind, Settings
from app.models.settings import remote_ready
from app.services import RemoteError
from app.ui import theme
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    DETAIL_SIZE,
    SMALL_SIZE,
    primary_button,
    text_label,
)
from app.ui.tasks import spawn

# The program menu, in its order.
PROGRAM_NAMES: dict[RemoteKind, str] = {
    RemoteKind.SYNOLOGY: "Synology Download Station",
    RemoteKind.QBITTORRENT: "qBittorrent",
    RemoteKind.TRANSMISSION: "Transmission",
}
ADDRESS_HINTS: dict[RemoteKind, str] = {  # each program's usual port
    RemoteKind.SYNOLOGY: "http://192.168.1.x:5000",
    RemoteKind.QBITTORRENT: "http://192.168.1.x:8080",
    RemoteKind.TRANSMISSION: "http://192.168.1.x:9091",
}
DESTINATION_HINTS: dict[RemoteKind, str] = {
    RemoteKind.SYNOLOGY: "video/tv — empty: Download Station's default",
    RemoteKind.QBITTORRENT: "/downloads/tv — empty: qBittorrent's default",
    RemoteKind.TRANSMISSION: "/downloads/tv — empty: Transmission's default",
}
# The form's labels, also for validation errors (Settings uses them too).
FIELD_LABELS: dict[str, str] = {
    "remote_kind": "Program",
    "remote_url": "Address",
    "remote_username": "User name",
    "remote_password": "Password",
    "remote_destination": "Destination folder",
    "remote_verify_tls": "Verify the certificate (https)",
}
TESTING_TEXT = "Saving and testing…"
TEST_BUTTON_WIDTH = 160

_ROW_GAP = 10
_LABEL_GAP = 12  # between the label and the field column
_FIELD_ROWS = ("Program", "Address", "User name", "Password", "Destination folder (optional)")

# Saves the form, then tests the connection: e.g. "qBittorrent v5.0.2". Raises RemoteError, or
# ValidationError when the form doesn't validate.
TestRun = Callable[[], Awaitable[str]]


def program_name(kind: RemoteKind) -> str:
    return PROGRAM_NAMES[kind]


def kind_from_name(name: str) -> RemoteKind:
    for kind, shown in PROGRAM_NAMES.items():
        if shown == name:
            return kind
    raise ValueError(f"unknown program name: {name!r}")


def remote_changes(
    kind: RemoteKind,
    url: str,
    username: str,
    password: str,
    verify_tls: bool,
    destination: str,
) -> dict[str, Any]:
    """The settings the form stores."""
    return {
        "remote_kind": kind,
        "remote_url": url,
        "remote_username": username,
        "remote_password": password,
        "remote_verify_tls": verify_tls,
        "remote_destination": destination,
    }


def folder_note(kind: RemoteKind, ready: bool) -> str | None:
    """The muted line under "Save .torrent files to a folder" when a remote program is set."""
    return f"Magnet-only releases are sent to your {kind.program}." if ready else None


def describe_error(error: ValidationError, labels: dict[str, str] = FIELD_LABELS) -> str:
    """'Address: Use a full address like …', one line per problem."""
    lines = []
    for item in error.errors():
        label = labels.get(str(item["loc"][0])) if item["loc"] else None
        message = item["msg"].removeprefix("Value error, ")
        lines.append(f"{label}: {message}" if label else message)
    return "\n".join(lines)


class ResultLook(NamedTuple):
    text: str
    color: str


def result_look(description: str | None = None, error: str | None = None) -> ResultLook:
    """The result line: running (neither given), passed (`description`) or failed (`error`)."""
    if error is not None:
        return ResultLook(f"✗ {error}", theme.DANGER)
    if description is not None:
        return ResultLook(f"✓ {description}", theme.SUCCESS)
    return ResultLook(TESTING_TEXT, theme.TEXT_MUTED)


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole application is gone
        return False


class RemoteProgramForm(ctk.CTkFrame):
    """Label / field rows (`label_width` + `field_width`, logical units) for the `remote_*`
    settings, started from `settings`."""

    def __init__(
        self,
        master: Any,
        *,
        settings: Settings,
        run_test: TestRun,
        label_width: int,
        field_width: int,
        describe: Callable[[ValidationError], str] = describe_error,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._run_test = run_test
        self._describe = describe
        self._test_task: asyncio.Task[None] | None = None
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        # Plain Tk grid options are not DPI-scaled by CTk.
        self.grid_columnconfigure(0, minsize=round(label_width * scaling))
        self.grid_columnconfigure(1, weight=1)
        for row, label in enumerate(_FIELD_ROWS):
            ctk.CTkLabel(
                self,
                text=label,
                height=BUTTON_HEIGHT,
                font=theme.font(DETAIL_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
                justify="left",
                wraplength=label_width - _LABEL_GAP,
            ).grid(row=row, column=0, sticky="nw", padx=(0, _LABEL_GAP), pady=self._pady(row))

        kind = settings.remote_kind
        self._program = ctk.CTkOptionMenu(
            self,
            values=list(PROGRAM_NAMES.values()),
            command=self._program_changed,
            width=field_width,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            dropdown_font=theme.font(DETAIL_SIZE),
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.ACCENT_HOVER,
        )
        self._program.set(program_name(kind))
        self._address = self._entry(1, settings.remote_url, placeholder=ADDRESS_HINTS[kind])
        self._username = self._entry(2, settings.remote_username)
        self._password = self._entry(3, settings.remote_password.get_secret_value(), show="•")
        self._destination = self._entry(
            4, settings.remote_destination, placeholder=DESTINATION_HINTS[kind]
        )
        self._program.grid(row=0, column=1, sticky="w", pady=self._pady(0))

        self._verify = ctk.CTkCheckBox(
            self,
            text=FIELD_LABELS["remote_verify_tls"],
            width=0,
            height=BUTTON_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=6,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
        )
        if settings.remote_verify_tls:
            self._verify.select()
        self._verify.grid(row=5, column=1, sticky="w", pady=self._pady(5))

        self._test_button = primary_button(
            self, "Test connection", self._start_test, width=TEST_BUTTON_WIDTH
        )
        self._test_button.grid(row=6, column=1, sticky="w", pady=self._pady(6))
        self._result = text_label(self, "", size=SMALL_SIZE, wraplength=field_width)
        self._result.grid(row=7, column=1, sticky="w", pady=(6, 0))

    # -- values --------------------------------------------------------------------------------

    @property
    def kind(self) -> RemoteKind:
        return kind_from_name(self._program.get())

    def changes(self) -> dict[str, Any]:
        """The `remote_*` settings as the form shows them."""
        return remote_changes(
            self.kind,
            self._address.get(),
            self._username.get(),
            self._password.get(),
            bool(self._verify.get()),
            self._destination.get(),
        )

    @property
    def ready(self) -> bool:
        """The form has what the program needs to be reached (an address, and for Download
        Station a user name and password)."""
        return remote_ready(
            self.kind, self._address.get(), self._username.get(), self._password.get()
        )

    def show_result(self, look: ResultLook) -> None:
        self._result.configure(text=look.text, text_color=look.color)

    # -- test ----------------------------------------------------------------------------------

    def _start_test(self) -> None:
        if self._test_task is not None and not self._test_task.done():
            return
        self.show_result(result_look())
        self._test_button.configure(state="disabled")
        self._test_task = spawn(self._test(), name="remote-program-test")

    async def _test(self) -> None:
        look = ResultLook("", theme.TEXT_MUTED)  # anything else: reported by the task runner
        try:
            look = result_look(description=await self._run_test())
        except ValidationError as exc:
            look = result_look(error=self._describe(exc))
        except RemoteError as exc:
            look = result_look(error=str(exc))
        finally:
            if _alive(self):
                self.show_result(look)
                self._test_button.configure(state="normal")

    # -- widgets -------------------------------------------------------------------------------

    def _program_changed(self, name: str) -> None:
        kind = kind_from_name(name)
        self._address.configure(placeholder_text=ADDRESS_HINTS[kind])
        self._destination.configure(placeholder_text=DESTINATION_HINTS[kind])

    def _entry(
        self, row: int, value: str, *, placeholder: str | None = None, show: str = ""
    ) -> ctk.CTkEntry:
        """No textvariable: CTkEntry hides placeholders on entries bound to one; `get()` returns
        "" while the placeholder shows."""
        entry = ctk.CTkEntry(
            self,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            placeholder_text=placeholder,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            show=show,
        )
        if value:
            entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="ew", pady=self._pady(row))
        return entry

    @staticmethod
    def _pady(row: int) -> tuple[int, int]:
        return (0 if row == 0 else _ROW_GAP, 0)

    def destroy(self) -> None:
        if self._test_task is not None:
            self._test_task.cancel()
        super().destroy()
