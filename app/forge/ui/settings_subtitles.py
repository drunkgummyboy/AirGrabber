"""Settings › Subtitles, in MediaForge's group: the languages (tags), Hearing impaired, the optional
OpenSubtitles account with Sign in and "Create a free account", the optional own API key with
Check, the downloads left today, and the credit line OpenSubtitles asks for.

The settings stay AirGrabber's (`Settings`, the same fields), read and written through
`services.airgrabber_settings`, so what was set comes along and backups don't change. Like the
other Forge sections (app/forge/ui/settings_pages.py) its changes wait for Settings' Save:
`problem()` refuses an account half filled in or no language at all; `save()` first asks
OpenSubtitles about a new own key. A refused key means nothing of this page is stored, and the
reason shows. Sign in and Check ask about what is typed without storing it. Everything that talks
to OpenSubtitles goes through ForgeSubtitles. Without a key in this version: one quiet line.

AirGrabber ships its own OpenSubtitles API key (a user's own key, when entered, replaces it);
signing in with a free OpenSubtitles account is optional (20 downloads a day instead of 5).

Light at import (the Settings window imports it with `settings_pages`): Forge's models only.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
import webbrowser
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import customtkinter as ctk

from app.forge.models.subtitles import (
    KeyRejected,
    OpenSubtitlesError,
    SubtitleAccount,
    SubtitleQuota,
)
from app.forge.ui.look import app_look
from app.i18n import N_, t, tn
from app.models import SUBTITLE_LANGUAGES, HearingImpaired, Settings
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, BUTTON_RADIUS, DETAIL_SIZE, SMALL_SIZE, text_label
from app.ui.missing_control import popup_menu
from app.ui.settings_layout import BOX_INNER
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.forge.runtime import ForgeRuntime
    from app.forge.ui.settings_pages import ForgeSectionPart
    from app.ui.settings_layout import SettingsBox, SettingsPage

log = logging.getLogger(__name__)

# -- its place in Settings (FORGE_SECTIONS in app/forge/ui/settings_pages.py) ------------------

SECTION_SUBTITLES = N_("Subtitles")
SUBTITLES_ICON = "💬"
SUBTITLES_NOTE = N_("Found on OpenSubtitles.com and saved next to your videos.")

# -- texts ----------------------------------------------------------------------------------------

SIGNUP_URL = "https://www.opensubtitles.com/en/users/sign_up"
CREDIT_URL = "https://www.opensubtitles.com"

GETTING_READY = N_("Getting MediaForge ready…")
UNAVAILABLE_TEXT = N_("Subtitles aren't available in this version yet")
SAVE_FAILED = N_("The subtitle settings couldn't be saved. The log has the details.")
# Required by OpenSubtitles' terms for free API keys, linking to CREDIT_URL.
CREDIT_TEXT = N_("Subtitles provided by OpenSubtitles.com")
LANGUAGES_TITLE = N_("Languages")
LANGUAGES_TEXT = N_("Looks for subtitles in each of these.")
HEARING_TITLE = N_("Hearing impaired")
HEARING_TEXT = N_("Subtitles that also describe sounds.")
ACCOUNT_LABEL = N_("OpenSubtitles account")
ACCOUNT_HINT = N_("Optional: 20 a day instead of 5.")
SIGN_UP_TEXT = N_("Create a free account")
ADD_TEXT = N_("+ Add")
SIGN_IN_TEXT = N_("Sign in")
OWN_KEY_LABEL = N_("Own API key")
OWN_KEY_PLACEHOLDER = N_("Leave empty to use AirGrabber's key")
OWN_KEY_HINT = N_("Optional. Only needed if you have your own API key from opensubtitles.com")
CHECK_TEXT = N_("Check")
CHECKING_TEXT = N_("Checking the key…")
KEY_ACCEPTED_TEXT = N_("✓ OpenSubtitles accepted the key")
NO_OWN_KEY_TEXT = N_("Empty: AirGrabber's own key is used")
KEY_UNCHECKED_TEXT = N_("Couldn't check the key right now; it is saved anyway")

HEARING_NAMES: dict[HearingImpaired, str] = {
    HearingImpaired.AVOID: N_("Avoid"),
    HearingImpaired.PREFER: N_("Prefer"),
    HearingImpaired.EITHER: N_("Either"),
}

_TAG_HEIGHT = 30
_SIDE_BUTTON_WIDTH = 120
_MENU_WIDTH = 160
_TEXT_SIZE = SMALL_SIZE + 1  # as the boxes' explanations


# -- texts (the constants above are shown through t()) ------------------------------------------


def language_name(code: str) -> str:
    name = SUBTITLE_LANGUAGES.get(code)
    return t(name) if name is not None else code


def addable_languages(chosen: Iterable[str]) -> list[str]:
    """The languages "+ Add" offers: every known one not chosen yet, in menu order."""
    taken = set(chosen)
    return [code for code in SUBTITLE_LANGUAGES if code not in taken]


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


def subtitle_problem(*, languages: Sequence[str], username: str, password: str) -> str | None:
    """Why Save can't store the subtitle settings as they are; None when they are fine."""
    if (username.strip() == "") != (password == ""):
        return t("Fill in both the OpenSubtitles user name and password, or neither")
    if not languages:
        return t("Choose at least one subtitle language")
    return None


def own_key_needs_check(typed: str, checked: str | None) -> bool:
    """Whether Save has to check the own key first: one is filled in and it isn't the one
    that was saved or accepted already (`checked`)."""
    key = typed.strip()
    return bool(key) and key != (checked or "").strip()


# -- what the widgets need ------------------------------------------------------------------------


class SubtitleChecks(Protocol):
    """What the page's rows ask of ForgeSubtitles."""

    async def sign_in(self, username: str, password: str) -> SubtitleAccount: ...

    async def check_key(self, key: str) -> None: ...


class SubtitleParts(SubtitleChecks, Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def quota(self) -> SubtitleQuota: ...


class SettingsSource(Protocol):
    """AirGrabber's SettingsService, as this page uses it."""

    @property
    def current(self) -> Settings: ...

    async def update(self, **changes: Any) -> Settings: ...


class _Parts(Protocol):
    @property
    def subtitles(self) -> SubtitleParts: ...

    @property
    def airgrabber_settings(self) -> SettingsSource: ...


class _Starter(Protocol):
    @property
    def reason(self) -> str: ...

    @property
    def services(self) -> _Parts | None: ...

    async def start(self) -> _Parts | None: ...


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

    def __init__(self, master: Any, languages: Sequence[str], look: theme.Look) -> None:
        super().__init__(master, fg_color="transparent")
        self._look = look
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
        look = self._look
        for code in self._languages:
            ctk.CTkButton(
                self,
                text=f"{language_name(code)}  ×",
                height=_TAG_HEIGHT,
                width=0,
                corner_radius=_TAG_HEIGHT // 2,
                font=theme.font(SMALL_SIZE + 1),
                fg_color=look.SURFACE,
                hover_color=look.CHIP_HOVER,
                border_width=1,
                border_color=look.EDGE,
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
            hover_color=look.CHIP_HOVER,
            text_color=look.LINK,
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
        popup_menu(self._add, items, look=self._look)


class AccountRow(ctk.CTkFrame):
    """User name, password (masked) and Sign in with its result line. Sign in checks what is
    typed without saving it; Save stores it."""

    def __init__(
        self,
        master: Any,
        *,
        subtitles: SubtitleChecks,
        username: str,
        password: str,
        look: theme.Look,
        on_signed_in: Callable[[SubtitleAccount], None] | None = None,
        field_width: int,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._subtitles = subtitles
        self._look = look
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
            fg_color=look.ACCENT,
            hover_color=look.ACCENT_HOVER,
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
            fg_color=self._look.SURFACE,
            border_color=self._look.EDGE,
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
        self,
        master: Any,
        *,
        subtitles: SubtitleChecks,
        key: str,
        look: theme.Look,
        field_width: int,
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
            fg_color=look.SURFACE,
            border_color=look.EDGE,
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
            fg_color=look.ACCENT,
            hover_color=look.ACCENT_HOVER,
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


# -- the section ----------------------------------------------------------------------------------


class SubtitlesSection:
    """The Subtitles page's contents; its boxes are made once Forge has started."""

    def __init__(self, page: SettingsPage, forge: _Starter) -> None:
        self._page = page
        self._forge = forge
        self._look = app_look()
        self._parts: _Parts | None = None
        self._task: asyncio.Task[None] | None = None
        self.languages: LanguageTags | None = None
        self.hearing: ctk.CTkOptionMenu | None = None
        self.account: AccountRow | None = None
        self.own_key: OwnKeyRow | None = None
        self.quota_label: ctk.CTkLabel | None = None
        self._hearing_names = {t(name): value for value, name in HEARING_NAMES.items()}

        # Shown until Forge has started (or why it can't).
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")
        tk.Misc.bind(page.frame, "<Destroy>", self._destroyed, "+")
        services = forge.services
        if services is not None:
            self._ready(services)
        else:
            self._task = spawn(self._wait(), name="forge-subtitles-start")

    @property
    def ready(self) -> bool:
        """Whether the form shows (Forge has started and this version has a key)."""
        return self.languages is not None

    async def _wait(self) -> None:
        services = await self._forge.start()
        if not self.waiting_label.winfo_exists():
            return
        if services is None:
            # NOT_READY (guessit missing) or FAILED: say why; nothing here can be changed.
            self.waiting_label.configure(text=self._forge.reason or t(GETTING_READY))
            return
        self._ready(services)

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is self._page.frame and self._task is not None:
            self._task.cancel()
            self._task = None

    def _ready(self, parts: _Parts) -> None:
        if not parts.subtitles.available:
            self.waiting_label.configure(text=t(UNAVAILABLE_TEXT))
            return
        self._parts = parts
        page, look = self._page, self._look
        current = parts.airgrabber_settings.current
        first_gap = self.waiting.frame.grid_info().get("pady")
        self.waiting.frame.grid_remove()

        languages = page.box(t(LANGUAGES_TITLE), t(LANGUAGES_TEXT))
        if first_gap is not None:  # the first box sits where the waiting box sat
            languages.frame.grid_configure(pady=first_gap)
        self.languages = LanguageTags(languages.body, current.subtitle_languages, look)
        languages.add(self.languages, sticky="w")

        hearing = page.box(t(HEARING_TITLE), t(HEARING_TEXT))
        self.hearing = ctk.CTkOptionMenu(
            hearing.body,
            values=list(self._hearing_names),
            width=_MENU_WIDTH,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            dropdown_font=theme.font(DETAIL_SIZE),
            fg_color=look.SURFACE,
            button_color=look.EDGE,
            button_hover_color=look.HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
        )
        self.hearing.set(t(HEARING_NAMES[current.subtitles_hearing_impaired]))
        hearing.add(self.hearing, sticky="w")

        account = page.box(t(ACCOUNT_LABEL))
        hint = ctk.CTkFrame(account.body, fg_color="transparent")
        account.add(hint, gap=2, sticky="w")
        text_label(hint, t(ACCOUNT_HINT), size=_TEXT_SIZE, color=theme.TEXT_MUTED).pack(side="left")
        link(hint, t(SIGN_UP_TEXT), SIGNUP_URL, size=_TEXT_SIZE).pack(side="left", padx=(4, 0))
        self.account = AccountRow(
            account.body,
            subtitles=parts.subtitles,
            username=current.opensubtitles_username,
            password=current.opensubtitles_password.get_secret_value(),
            look=look,
            on_signed_in=lambda _account: self._show_quota(),
            field_width=BOX_INNER,
        )
        account.add(self.account)

        own_key = page.box(t(OWN_KEY_LABEL))
        self.own_key = OwnKeyRow(
            own_key.body,
            subtitles=parts.subtitles,
            key=current.opensubtitles_own_key.get_secret_value(),
            look=look,
            field_width=BOX_INNER,
        )
        own_key.add(self.own_key)

        foot: SettingsBox = page.box()
        self.quota_label = foot.text("")
        self._quota_row = foot.take_row()
        foot.add(link(foot.body, t(CREDIT_TEXT), CREDIT_URL, size=SMALL_SIZE), gap=0, sticky="w")
        self._show_quota()

    def _show_quota(self) -> None:
        if self.quota_label is None or self._parts is None:
            return
        text = quota_line(self._parts.subtitles.quota, datetime.now(UTC))
        if text:
            self.quota_label.configure(text=text)
            self.quota_label.grid(row=self._quota_row, column=0, sticky="w", pady=(0, 8))
        else:
            self.quota_label.grid_remove()

    # --- Save -------------------------------------------------------------------------------------

    def _hearing_value(self) -> HearingImpaired:
        assert self.hearing is not None
        return self._hearing_names.get(self.hearing.get(), HearingImpaired.AVOID)

    def changes(self) -> dict[str, Any]:
        """The fields changed in the form (what Save stores)."""
        parts = self._parts
        if parts is None or self.languages is None or self.account is None or self.own_key is None:
            return {}
        current = parts.airgrabber_settings.current
        username, password = self.account.values
        form: dict[str, Any] = {
            "subtitle_languages": self.languages.languages,
            "subtitles_hearing_impaired": self._hearing_value(),
            "opensubtitles_username": username,
            "opensubtitles_password": password,
            "opensubtitles_own_key": self.own_key.value,
        }
        stored: dict[str, Any] = {
            "subtitle_languages": current.subtitle_languages,
            "subtitles_hearing_impaired": current.subtitles_hearing_impaired,
            "opensubtitles_username": current.opensubtitles_username,
            "opensubtitles_password": current.opensubtitles_password.get_secret_value(),
            "opensubtitles_own_key": current.opensubtitles_own_key.get_secret_value(),
        }
        return {name: value for name, value in form.items() if value != stored[name]}

    def problem(self) -> str | None:
        changes = self.changes()
        if not changes or self.languages is None or self.account is None:
            return None  # unchanged settings never stop Save
        username, password = self.account.values
        return subtitle_problem(
            languages=self.languages.languages, username=username, password=password
        )

    async def save(self) -> str | None:
        changes = self.changes()
        if not changes or self._parts is None or self.own_key is None:
            return None
        if self.own_key.needs_check:
            problem = await self.own_key.verify()  # a new own key: does OpenSubtitles take it?
            if problem is not None:
                return f"{t(OWN_KEY_LABEL)}: {problem}"
        try:
            await self._parts.airgrabber_settings.update(**changes)
        except Exception as exc:  # a bad value, or the database couldn't be written
            log.error("The subtitle settings couldn't be saved (%s)", type(exc).__name__)
            return t(SAVE_FAILED)
        return None


def build_subtitles(page: SettingsPage, forge: ForgeRuntime) -> ForgeSectionPart:
    return SubtitlesSection(page, forge)

