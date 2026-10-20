"""Getting a TMDB API key, step by step, with a Check of the pasted key.

TmdbKeyGuide is the reusable part (step 3 of the first-run welcome); TmdbKeyDialog shows it on
its own, for the "How do I get a free key?" link in Settings → Movie & show info.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
import webbrowser
from collections.abc import Callable
from enum import Enum
from typing import Any

import customtkinter as ctk

from app.services import DiscoveryService, SettingsService
from app.services.tmdb import TMDBError
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

log = logging.getLogger(__name__)

SIGNUP_URL = "https://www.themoviedb.org/signup"
API_KEY_URL = "https://www.themoviedb.org/settings/api"

TMDB_HEADING = "Movies and Discover"
TMDB_INTRO = (
    "These pages need a free key from The Movie Database. It takes about two minutes."
)

_NUMBER_SIZE = 18
_ROW_GAP = 14
_SIDE_BUTTON_WIDTH = 150
_TEXT_WIDTH = CONTENT_WIDTH - 40 - _SIDE_BUTTON_WIDTH - 16  # beside the number and a button


class KeyCheck(Enum):
    CHECKING = "checking"
    OK = "ok"
    REJECTED = "rejected"  # TMDB answered 401 (or the text can't be a key)
    UNREACHABLE = "unreachable"  # no answer, or an unexpected one


def key_check_message(result: KeyCheck) -> tuple[str, str]:
    """(text, colour) of the result line under the key entry."""
    if result is KeyCheck.OK:
        return "✓ The key works", theme.SUCCESS
    if result is KeyCheck.REJECTED:
        return (
            'That key didn\'t work. Check that you copied the "API Key" (not the long token).',
            theme.DANGER,
        )
    if result is KeyCheck.UNREACHABLE:
        return (
            "Couldn't reach The Movie Database. Check your internet connection and try again.",
            theme.DANGER,
        )
    return "Checking…", theme.TEXT_MUTED


class KeyAction(Enum):
    """What Finish / Save does with the key entry."""

    NONE = "none"  # the entry is empty: nothing to save
    SAVE = "save"  # the entry's key already checked OK: save it
    CHECK = "check"  # typed or pasted but not checked OK (yet): check it, save it if it works


def key_action(entry: str, checked_key: str | None) -> KeyAction:
    key = entry.strip()
    if not key:
        return KeyAction.NONE
    return KeyAction.SAVE if key == checked_key else KeyAction.CHECK


class TmdbKeyGuide(ctk.CTkFrame):
    """Three numbered steps (sign up, ask for a key, paste it) and a key entry with Check.

    `checked_key` is the entry's key once TMDB accepted it, None otherwise (editing the entry
    forgets the result). `on_checked` is called with it after every check and edit that changes
    it. Saving the key is up to the owner: `key_action()` says what its Finish / Save should do,
    and `check_now()` checks a key nobody checked yet.
    """

    def __init__(
        self,
        master: Any,
        *,
        discovery: DiscoveryService,
        on_checked: Callable[[str | None], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._discovery = discovery
        self._on_checked = on_checked
        self._checked_key: str | None = None
        self._check_task: asyncio.Task[None] | None = None
        self._key = tk.StringVar(master=self)

        self.grid_columnconfigure(1, weight=1)
        self._step_row(0, "Create a free account", None, ("Open sign-up", SIGNUP_URL))
        self._step_row(
            1,
            "Ask for an API key",
            'Choose "Developer"; the app details can be anything',
            ("Open API page", API_KEY_URL),
        )
        self._step_row(2, 'Copy the "API Key" and paste it below', None, None)

        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._key,
            height=40,
            corner_radius=8,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            font=theme.font(DETAIL_SIZE),
        )
        self._entry.grid(row=3, column=1, sticky="ew", pady=(16, 0))
        self._entry.bind("<Return>", lambda _event: self._check())
        self._check_button = primary_button(
            self, "Check", self._check, width=_SIDE_BUTTON_WIDTH
        )
        self._check_button.grid(row=3, column=2, sticky="e", padx=(16, 0), pady=(16, 0))
        self._result = text_label(self, "", size=DETAIL_SIZE, wraplength=CONTENT_WIDTH - 40)
        self._result.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))
        self._key.trace_add("write", self._on_key_edited)

    @property
    def checked_key(self) -> str | None:
        return self._checked_key

    def focus_entry(self) -> None:
        self._entry.focus_set()

    def key_action(self) -> KeyAction:
        return key_action(self._key.get(), self._checked_key)

    async def check_now(self) -> str | None:
        """Check the entry's key now (or wait for the check already running), showing
        "Checking…" and then the result. Returns the key when TMDB accepted it, else None."""
        if self.key_action() is not KeyAction.SAVE:
            task = self._start_check()
            if task is not None:
                await asyncio.wait([task])  # not `await task`: a cancelled caller leaves it be
        return self._checked_key

    def _step_row(
        self, row: int, title: str, hint: str | None, button: tuple[str, str] | None
    ) -> None:
        pady = (0 if row == 0 else _ROW_GAP, 0)
        ctk.CTkLabel(
            self,
            text=str(row + 1),
            width=28,
            anchor="w",
            font=theme.font(_NUMBER_SIZE, bold=True),
            text_color=theme.TEXT_MUTED,
        ).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=pady)
        texts = ctk.CTkFrame(self, fg_color="transparent")
        texts.grid(row=row, column=1, sticky="w", pady=pady)
        text_label(texts, title, size=BODY_SIZE, wraplength=_TEXT_WIDTH).pack(anchor="w")
        if hint:
            text_label(
                texts, hint, size=SMALL_SIZE, color=theme.TEXT_MUTED, wraplength=_TEXT_WIDTH
            ).pack(anchor="w")
        if button is not None:
            label, url = button
            secondary_button(
                self, label, lambda: _open(url), width=_SIDE_BUTTON_WIDTH
            ).grid(row=row, column=2, sticky="e", padx=(16, 0), pady=pady)

    # -- checking ------------------------------------------------------------------------------

    def _check(self) -> None:
        self._start_check()

    def _start_check(self) -> asyncio.Task[None] | None:
        """Check the entry's key; returns the check (the one already running, if any), or None
        when the entry is empty."""
        key = self._key.get().strip()
        if not key:
            self._entry.focus_set()
            return None
        if self._check_task is not None and not self._check_task.done():
            return self._check_task
        self._show(KeyCheck.CHECKING)
        self._check_button.configure(state="disabled")
        self._check_task = spawn(self._run_check(key), name="tmdb-key-check")
        return self._check_task

    async def _run_check(self, key: str) -> None:
        try:
            result = KeyCheck.OK if await self._discovery.check_tmdb_key(key) else KeyCheck.REJECTED
        except TMDBError as exc:  # its message holds neither the key nor the URL
            log.info("TMDB key check: %s", exc)
            result = KeyCheck.UNREACHABLE
        if not self.winfo_exists():
            return
        self._check_button.configure(state="normal")
        self._show(result)
        self._set_checked(key if result is KeyCheck.OK else None)

    def _on_key_edited(self, *_args: object) -> None:
        """A changed key needs a new check: forget the last result, stop a check in flight."""
        if self._checked_key is not None and self._key.get().strip() == self._checked_key:
            return  # e.g. a space typed after the key
        if self._check_task is not None and not self._check_task.done():
            self._check_task.cancel()
            self._check_button.configure(state="normal")
        self._result.configure(text="")
        if self._checked_key is not None:
            self._set_checked(None)

    def _set_checked(self, key: str | None) -> None:
        self._checked_key = key
        if self._on_checked is not None:
            self._on_checked(key)

    def _show(self, result: KeyCheck) -> None:
        text, color = key_check_message(result)
        self._result.configure(text=text, text_color=color)

    def destroy(self) -> None:
        if self._check_task is not None:
            self._check_task.cancel()
        super().destroy()


class TmdbKeyDialog(ctk.CTkToplevel):
    """The TMDB key guide on its own (opened from Settings). Save checks a key that wasn't
    checked yet; a key TMDB accepts is stored through SettingsService and handed to `on_saved`,
    so an open Settings form can show it. Otherwise the dialog stays open, showing why.
    """

    def __init__(
        self,
        master: ctk.CTk | ctk.CTkToplevel,
        *,
        settings: SettingsService,
        discovery: DiscoveryService,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._settings = settings
        self._on_saved = on_saved
        self._save_task: asyncio.Task[None] | None = None
        # Settings is modal too: give it its grab back when this dialog closes.
        self._previous_grab = _grabbing_window(master)

        self.title("TMDB API key")
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        # Holds the content width.
        ctk.CTkFrame(body, width=CONTENT_WIDTH, height=1, fg_color="transparent").pack()
        heading_row(body, TMDB_HEADING).pack(fill="x")
        text_label(body, TMDB_INTRO, size=DETAIL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )
        self._guide = TmdbKeyGuide(body, discovery=discovery)
        self._guide.pack(fill="x", pady=(BLOCK_GAP, 0))

        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", pady=(NAV_GAP, 0))
        secondary_button(nav, "Cancel", self.destroy).pack(side="left")
        self._save_button = primary_button(nav, "Save", self._save)
        self._save_button.pack(side="right")

        self.bind("<Escape>", lambda _event: self.destroy())
        place_over(self, master)
        activate_later(self)
        self.after(200, self._guide.focus_entry)

    def _save(self) -> None:
        action = self._guide.key_action()
        key = self._guide.checked_key
        if action is KeyAction.NONE:
            self._guide.focus_entry()  # nothing to save yet
        elif action is KeyAction.SAVE and key is not None:
            self._store(key)
        elif self._save_task is None or self._save_task.done():
            self._save_task = spawn(self._check_then_save(), name="tmdb-key-dialog-save")

    async def _check_then_save(self) -> None:
        self._save_button.configure(state="disabled")
        key = await self._guide.check_now()
        if not self.winfo_exists():
            return
        self._save_button.configure(state="normal")
        if key is not None:
            self._store(key)

    def _store(self, key: str) -> None:
        if self._on_saved is not None:
            self._on_saved(key)
        # Not tied to this dialog: the save finishes after it has closed.
        spawn(self._settings.update(tmdb_api_key=key), name="tmdb-key-save")
        self.destroy()

    def destroy(self) -> None:
        if self._save_task is not None:
            self._save_task.cancel()
        previous, self._previous_grab = self._previous_grab, None
        super().destroy()
        if previous is not None:
            try:
                if previous.winfo_exists():
                    previous.grab_set()
            except tk.TclError:
                pass  # the application is shutting down


def _open(url: str) -> None:
    webbrowser.open(url)


def _grabbing_window(widget: tk.Misc) -> tk.Misc | None:
    """The window holding the application's grab, if any."""
    try:
        current = widget.grab_current()  # type: ignore[no-untyped-call]
    except (KeyError, tk.TclError):
        return None
    return current if isinstance(current, tk.Misc) else None
