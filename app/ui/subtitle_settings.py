"""Settings' Subtitles pieces: the language tags, the OpenSubtitles account row with Sign in, the
optional own API key with Check, the credit line OpenSubtitles asks for, and the plain-words
texts they show (pure helpers, tested).

The section itself is laid out by SettingsDialog (app/ui/settings_dialog.py); everything that
talks to OpenSubtitles goes through SubtitleService. AirGrabber ships its own OpenSubtitles API
key (a user's own key, when entered, replaces it); signing in with a free OpenSubtitles account
is optional (20 downloads a day instead of 5).
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import SUBTITLE_LANGUAGES, SubtitleAccount, SubtitleQuota
from app.services.opensubtitles import KeyRejected, OpenSubtitlesError
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, BUTTON_RADIUS, DETAIL_SIZE, SMALL_SIZE, text_label
from app.ui.missing_control import popup_menu
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.services import SubtitleService

log = logging.getLogger(__name__)

SIGNUP_URL = "https://www.opensubtitles.com/en/users/sign_up"
CREDIT_URL = "https://www.opensubtitles.com"

SUBTITLES_HEADING = N_("Subtitles")
SUBTITLES_NOTE = N_("Saved next to your videos, so your player shows them.")
UNAVAILABLE_TEXT = N_("Subtitles aren't available in this version yet")
# Required by OpenSubtitles' terms for free API keys, linking to CREDIT_URL.
CREDIT_TEXT = N_("Subtitles provided by OpenSubtitles.com")
SWITCH_TEXT = N_("Get subtitles for my downloads")
FOLDER_LABEL = N_("Where finished downloads end up")
FOLDER_HINT = N_("Your torrent program's download folder")
ACCOUNT_LABEL = N_("OpenSubtitles account")
ACCOUNT_HINT = N_("Optional: 20 subtitles a day instead of 5.")
SIGN_UP_TEXT = N_("Create a free account")
ADD_TEXT = N_("+ Add")
SIGN_IN_TEXT = N_("Sign in")
OWN_KEY_LABEL = N_("Own OpenSubtitles key (optional)")
OWN_KEY_PLACEHOLDER = N_("Leave empty to use AirGrabber's key")
OWN_KEY_HINT = N_("Only needed if you have your own API key from opensubtitles.com")
CHECK_TEXT = N_("Check")
CHECKING_TEXT = N_("Checking the key…")
KEY_ACCEPTED_TEXT = N_("✓ OpenSubtitles accepted the key")
NO_OWN_KEY_TEXT = N_("Empty: AirGrabber's own key is used")
KEY_UNCHECKED_TEXT = N_("Couldn't check the key right now; it is saved anyway")

_TAG_HEIGHT = 30
_SIDE_BUTTON_WIDTH = 120


# -- texts (the constants above are shown through t()) ------------------------------------------


def language_name(code: str) -> str:
    name = SUBTITLE_LANGUAGES.get(code)
    return t(name) if name is not None else code


def addable_languages(chosen: Iterable[str]) -> list[str]:
    """The languages "+ Add" offers: every known one not chosen yet, in menu order."""
    taken = set(chosen)
    return [code for code in SUBTITLE_LANGUAGES if code not in taken]


def subtitle_mark(languages: Iterable[str], order: Sequence[str] = ()) -> str:
    """"CC EN NL": the show window's mark for an episode with saved subtitles ("" without).
    Languages in `order` (the Settings' order) first, then the rest alphabetically."""
    found = set(languages)
    if not found:
        return ""
    ranked = [code for code in order if code in found]
    ranked += sorted(found - set(ranked))
    return "CC " + " ".join(code.upper() for code in ranked)


def quota_line(quota: SubtitleQuota, now: datetime) -> str | None:
    """"18 subtitle downloads left today" when OpenSubtitles said so; None when unknown."""
    if quota.remaining is None:
        return None
    if quota.remaining <= 0:
        if quota.reset_at is not None and quota.reset_at > now:
            return t(
                "No subtitle downloads left today; more after {time}",
                time=f"{quota.reset_at.astimezone():%H:%M}",
            )
        return t("No subtitle downloads left today")
    return tn(
        "{n} subtitle download left today", "{n} subtitle downloads left today", quota.remaining
    )


def account_text(account: SubtitleAccount) -> str:
    """The Sign in result: "✓ Signed in · 20 downloads a day, 18 left today"."""
    parts = (
        [t("{n} downloads a day", n=account.allowed_downloads)]
        if account.allowed_downloads
        else []
    )
    if account.remaining_downloads is not None:
        parts.append(t("{n} left today", n=account.remaining_downloads))
    return t("✓ Signed in") + (" · " + ", ".join(parts) if parts else "")


def subtitle_problem(
    *, enabled: bool, folder: str, languages: Sequence[str], username: str, password: str
) -> str | None:
    """Why Save can't store the subtitle settings as they are; None when they are fine. Nothing
    is required while subtitles are off."""
    if (username.strip() == "") != (password == ""):
        return t("Fill in both the OpenSubtitles user name and password, or neither")
    if not enabled:
        return None
    if not languages:
        return t("Choose at least one subtitle language")
    if not folder.strip():
        return t("Choose where finished downloads end up, for subtitles")
    return None


def own_key_needs_check(typed: str, checked: str | None) -> bool:
    """Whether Save has to check the own key first: one is filled in and it isn't the one
    that was saved or accepted already (`checked`)."""
    key = typed.strip()
    return bool(key) and key != (checked or "").strip()


# -- widgets -----------------------------------------------------------------------------------


def link(master: Any, text: str, url: str, *, size: int = DETAIL_SIZE) -> ctk.CTkLabel:
    """An underlined link label that opens `url` in the browser."""
    label = ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(size, underline=True),
        text_color=theme.LINK,
        cursor="hand2",
        anchor="w",
    )
    label.bind("<Button-1>", lambda _event: webbrowser.open(url))
    return label


class LanguageTags(ctk.CTkFrame):
    """The chosen languages as removable tags ("English ×") and "+ Add" (a menu of the
    others). `languages` is what Save stores."""

    def __init__(self, master: Any, languages: Sequence[str]) -> None:
        super().__init__(master, fg_color="transparent")
        self._languages = list(languages)
        self._add: ctk.CTkButton | None = None
        self._paint()

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self._languages)

    def add(self, code: str) -> None:
        if code in SUBTITLE_LANGUAGES and code not in self._languages:
            self._languages.append(code)
            self._paint()

    def remove(self, code: str) -> None:
        if code in self._languages:
            self._languages.remove(code)
            self._paint()

    def _paint(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        for code in self._languages:
            ctk.CTkButton(
                self,
                text=f"{language_name(code)}  ×",
                height=_TAG_HEIGHT,
                width=0,
                corner_radius=_TAG_HEIGHT // 2,
                font=theme.font(SMALL_SIZE + 1),
                fg_color=theme.SURFACE,
                hover_color=theme.CHIP_HOVER,
                border_width=1,
                border_color=theme.EDGE,
                text_color=theme.TEXT,
                command=lambda c=code: self.remove(c),
            ).pack(side="left", padx=(0, 6), pady=2)
        self._add = ctk.CTkButton(
            self,
            text=t(ADD_TEXT),
            height=_TAG_HEIGHT,
            width=0,
            corner_radius=_TAG_HEIGHT // 2,
            font=theme.font(SMALL_SIZE + 1, bold=True),
            fg_color="transparent",
            hover_color=theme.CHIP_HOVER,
            text_color=theme.LINK,
            command=self._open_menu,
        )
        self._add.pack(side="left", pady=2)

    def _open_menu(self) -> None:
        if self._add is None:
            return
        items = [
            (language_name(code), lambda c=code: self.add(c))
            for code in addable_languages(self._languages)
        ]
        popup_menu(self._add, items)


class AccountRow(ctk.CTkFrame):
    """User name, password (masked) and Sign in with its result line. Sign in checks what is
    typed without saving it; Save stores it."""

    def __init__(
        self,
        master: Any,
        *,
        subtitles: SubtitleService,
        username: str,
        password: str,
        on_signed_in: Callable[[SubtitleAccount], None] | None = None,
        field_width: int,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._subtitles = subtitles
        self._on_signed_in = on_signed_in
        self._task: asyncio.Task[None] | None = None
        self.username = self._field(t("User name"), username, pady=0)
        self.password = self._field(t("Password"), password, pady=8, show="•")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._button = ctk.CTkButton(
            row,
            text=t(SIGN_IN_TEXT),
            width=_SIDE_BUTTON_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            font=theme.font(15, bold=True),
            command=self.sign_in,
        )
        self._button.pack(side="left", anchor="n")
        self._result = text_label(
            row, "", size=SMALL_SIZE, wraplength=field_width - _SIDE_BUTTON_WIDTH - 12
        )
        self._result.pack(side="left", padx=(12, 0), fill="x", expand=True)

    def _field(self, placeholder: str, value: str, *, pady: int, **kwargs: Any) -> ctk.CTkEntry:
        entry = ctk.CTkEntry(
            self,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            placeholder_text=placeholder,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            **kwargs,
        )
        if value:
            entry.insert(0, value)
        entry.pack(fill="x", pady=(pady, 0))
        return entry

    @property
    def values(self) -> tuple[str, str]:
        """(user name, password) as typed; "" for an empty field."""
        return self.username.get().strip(), self.password.get()

    @property
    def result_text(self) -> str:
        return str(self._result.cget("text"))

    def show_result(self, text: str, color: theme.Color) -> None:
        self._result.configure(text=text, text_color=color)

    def sign_in(self) -> None:
        if self._task is not None and not self._task.done():
            return
        username, password = self.values
        if not username or not password:
            self.show_result(
                t("Fill in your OpenSubtitles user name and password"), theme.DANGER
            )
            return
        self.show_result(t("Signing in…"), theme.TEXT_MUTED)
        self._button.configure(state="disabled")
        self._task = spawn(self._run(username, password), name="opensubtitles-sign-in")

    async def _run(self, username: str, password: str) -> None:
        try:
            account = await self._subtitles.sign_in(username, password)
        except OpenSubtitlesError as exc:  # plain words, no secrets
            if self.winfo_exists():
                self._button.configure(state="normal")
                self.show_result(f"✗ {exc}", theme.DANGER)
            return
        if not self.winfo_exists():
            return
        self._button.configure(state="normal")
        self.show_result(account_text(account), theme.SUCCESS)
        if self._on_signed_in is not None:
            self._on_signed_in(account)

    def destroy(self) -> None:
        if self._task is not None:
            self._task.cancel()
        super().destroy()


class OwnKeyRow(ctk.CTkFrame):
    """The optional own OpenSubtitles API key: a masked entry, a muted hint, and Check with its
    result line. Check (and Save, through `verify`) asks OpenSubtitles about the typed key
    without saving it; refusals show in plain words, never with the key."""

    def __init__(
        self, master: Any, *, subtitles: SubtitleService, key: str, field_width: int
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._subtitles = subtitles
        self._task: asyncio.Task[None] | None = None
        # The saved key counts as checked: Save doesn't ask about it again.
        self._checked: str | None = key.strip() or None
        self.entry = ctk.CTkEntry(
            self,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            placeholder_text=t(OWN_KEY_PLACEHOLDER),
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            show="•",
        )
        if key:
            self.entry.insert(0, key)
        self.entry.pack(fill="x")
        text_label(
            self, t(OWN_KEY_HINT), size=SMALL_SIZE, color=theme.TEXT_MUTED, wraplength=field_width
        ).pack(anchor="w", pady=(6, 0))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self._button = ctk.CTkButton(
            row,
            text=t(CHECK_TEXT),
            width=_SIDE_BUTTON_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            font=theme.font(15, bold=True),
            command=self.check,
        )
        self._button.pack(side="left", anchor="n")
        self._result = text_label(
            row, "", size=SMALL_SIZE, wraplength=field_width - _SIDE_BUTTON_WIDTH - 12
        )
        self._result.pack(side="left", padx=(12, 0), fill="x", expand=True)

    @property
    def value(self) -> str:
        """The key as typed, trimmed; "" when empty (the placeholder reads as "")."""
        return str(self.entry.get()).strip()

    @property
    def needs_check(self) -> bool:
        return own_key_needs_check(self.value, self._checked)

    @property
    def result_text(self) -> str:
        return str(self._result.cget("text"))

    def show_result(self, text: str, color: theme.Color) -> None:
        self._result.configure(text=text, text_color=color)

    def check(self) -> None:
        """The Check button: ask OpenSubtitles about the typed key."""
        if self._task is not None and not self._task.done():
            return
        if not self.value:
            self.show_result(t(NO_OWN_KEY_TEXT), theme.TEXT_MUTED)
            return
        self._task = spawn(self._check(), name="opensubtitles-check-key")

    async def _check(self) -> None:
        await self.verify(force=True)

    async def verify(self, *, force: bool = False) -> str | None:
        """Check the typed key unless it was saved or accepted already (`force`: check anyway).
        Returns why Save must not store it (OpenSubtitles refused it), else None; a check that
        couldn't reach OpenSubtitles doesn't stop Save."""
        key = self.value
        if not key or not (force or self.needs_check):
            return None
        self.show_result(t(CHECKING_TEXT), theme.TEXT_MUTED)
        self._button.configure(state="disabled")
        problem: str | None = None
        try:
            await self._subtitles.check_key(key)
        except KeyRejected as exc:  # plain words, never the key
            problem = str(exc)
            text, color = f"✗ {exc}", theme.DANGER
        except OpenSubtitlesError as exc:  # unreachable: the key may well be fine
            log.info("Checking the own OpenSubtitles key: %s", type(exc).__name__)
            text, color = t(KEY_UNCHECKED_TEXT), theme.TEXT_MUTED
        else:
            self._checked = key
            text, color = t(KEY_ACCEPTED_TEXT), theme.SUCCESS
        if self.winfo_exists():
            self._button.configure(state="normal")
            self.show_result(text, color)
        return problem

    def destroy(self) -> None:
        if self._task is not None:
            self._task.cancel()
        super().destroy()
