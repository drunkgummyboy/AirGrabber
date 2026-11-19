"""Settings › Subtitles: the languages (tags), Hearing impaired, the downloads left today and the
credit line OpenSubtitles asks for; and, in one "OpenSubtitles" box on Accounts & keys
(`accounts_page`), the optional account with Sign in and "Create a free account" and the
optional own API key with Check. One part for both pages.

The settings stay AirGrabber's (`Settings`, the same fields), read and written through
`services.airgrabber_settings`, so what was set comes along and backups don't change. Like the
other Forge parts (app/forge/ui/settings_pages.py) each change stores itself through the window's
Committer: a language added or removed, Hearing impaired at once; the account's name and
password together when the keyboard leaves them (or on Enter), refused when only one is filled
in; the own key likewise, after OpenSubtitles was asked about it (a refused key isn't stored).
No language at all is refused too; the refusal shows in its box. Values stored elsewhere (an
Undo) show unless a field is being edited. Sign in and Check ask about what is typed without
storing it. Everything that talks to OpenSubtitles goes through ForgeSubtitles. Without a key in
this version: one quiet line.

AirGrabber ships its own OpenSubtitles API key (a user's own key, when entered, replaces it);
signing in with a free OpenSubtitles account is optional (20 downloads a day instead of 5).

Light at import (the Settings window imports it with `settings_pages`): Forge's models only.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
import webbrowser
from collections.abc import Awaitable, Callable, Iterable, Sequence
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
from app.ui.settings_commit import Committer, Refused, TextCommit, commit_fields
from app.ui.settings_layout import box_inner
from app.ui.tasks import spawn

if TYPE_CHECKING:
    from app.forge.runtime import ForgeRuntime
    from app.forge.ui.settings_pages import ForgeSectionPart, PartSlots
    from app.ui.settings_layout import SettingsBox, SettingsPage

log = logging.getLogger(__name__)

# -- texts ----------------------------------------------------------------------------------------

SIGNUP_URL = "https://www.opensubtitles.com/en/users/sign_up"
CREDIT_URL = "https://www.opensubtitles.com"

GETTING_READY = N_("Getting MediaForge ready…")
# A start that ended without Forge and without a reason (AirGrabber is closing): never "getting
# ready" for ever.
FORGE_UNAVAILABLE = N_("MediaForge isn't available.")
UNAVAILABLE_TEXT = N_("Subtitles aren't available in this version yet")
# Required by OpenSubtitles' terms for free API keys, linking to CREDIT_URL.
CREDIT_TEXT = N_("Subtitles provided by OpenSubtitles.com")
LANGUAGES_TITLE = N_("Languages")
LANGUAGES_TEXT = N_("Looks for subtitles in each of these.")
HEARING_TITLE = N_("Hearing impaired")
HEARING_TEXT = N_("Subtitles that also describe sounds.")
OPENSUBTITLES_TITLE = N_("OpenSubtitles")  # its box on Accounts & keys
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


def account_problem(*, username: str, password: str) -> str | None:
    """Why the OpenSubtitles account can't be stored as it is; None when it is fine."""
    if (username.strip() == "") != (password == ""):
        return t("Fill in both the OpenSubtitles user name and password, or neither")
    return None


def own_key_needs_check(typed: str, checked: str | None) -> bool:
    """Whether storing the own key needs a check first: one is filled in and it isn't the one
    that was stored or accepted already (`checked`)."""
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

    def subscribe(self, listener: Callable[[Settings], None]) -> Callable[[], None]: ...


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
    others). `on_change(languages)` after each add or remove (Settings stores them)."""

    def __init__(
        self,
        master: Any,
        languages: Sequence[str],
        look: theme.Look,
        on_change: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._look = look
        self._languages = list(languages)
        self._on_change = on_change
        self._add: ctk.CTkButton | None = None
        self._paint()

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self._languages)

    def add(self, code: str) -> None:
        if code in SUBTITLE_LANGUAGES and code not in self._languages:
            self._languages.append(code)
            self._paint()
            self._changed()

    def remove(self, code: str) -> None:
        if code in self._languages:
            self._languages.remove(code)
            self._paint()
            self._changed()

    def show(self, languages: Sequence[str]) -> None:
        """Show these (stored elsewhere, or a refused change undone)."""
        if list(languages) != self._languages and self.winfo_exists():
            self._languages = list(languages)
            self._paint()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self.languages)

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
    typed without storing it (Settings stores the two fields as they change)."""

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
    result line. Check (and storing it, through `verify`) asks OpenSubtitles about the typed key
    without storing it; refusals show in plain words, never with the key."""

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
        # The stored key counts as checked: storing doesn't ask about it again.
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

    async def verify(self, key: str | None = None, *, force: bool = False) -> str | None:
        """Check `key` (None: the typed one) unless it was stored or accepted already (`force`:
        check anyway). Returns why it must not be stored (OpenSubtitles refused it), else None;
        a check that couldn't reach OpenSubtitles doesn't stop storing it. The row may be gone
        (Settings closed while a typed key is stored): the check runs all the same."""
        key = self.value if key is None else key.strip()
        if not key or not (force or own_key_needs_check(key, self._checked)):
            return None
        if self._alive():
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
        if self._alive():
            self._button.configure(state="normal")
            self.show_result(text, color)
        return problem

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:  # the whole application is gone
            return False

    def destroy(self) -> None:
        if self._task is not None:
            self._task.cancel()
        super().destroy()


# -- the section ----------------------------------------------------------------------------------


class SubtitlesSection:
    """The Subtitles page's contents, and the OpenSubtitles box on `accounts_page` (Settings'
    Accounts & keys; this page without one); its boxes are made once Forge has started."""

    def __init__(
        self,
        page: SettingsPage,
        forge: _Starter,
        *,
        accounts_page: SettingsPage | None = None,
        committer: Committer | None = None,
    ) -> None:
        self._page = page
        self._accounts_page = accounts_page
        self._committer = committer if committer is not None else Committer()
        self._unsubscribe: Callable[[], None] | None = None
        self.account_field: TextCommit | None = None
        self.account_box: SettingsBox | None = None
        self._key_lock = asyncio.Lock()  # one own-key store at a time (M3)
        self._key_latest: str | None = None  # the own key typed last
        self.key_field: TextCommit | None = None
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

        # Shown until Forge has started (or why it can't), here and on Accounts & keys.
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")
        self._accounts_waiting: SettingsBox | None = None
        self.accounts_waiting_label: ctk.CTkLabel | None = None
        if accounts_page is not None:
            self._accounts_waiting = accounts_page.box()
            self.accounts_waiting_label = self._accounts_waiting.text(t(GETTING_READY))
            self._accounts_waiting.add(self.accounts_waiting_label, gap=0, sticky="w")
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
            self._say(self._forge.reason or t(FORGE_UNAVAILABLE))
            return
        self._ready(services)

    def _say(self, text: str) -> None:
        """Why the boxes don't show (in the waiting boxes)."""
        for label in (self.waiting_label, self.accounts_waiting_label):
            if label is not None:
                label.configure(text=text)

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is not self._page.frame:
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _ready(self, parts: _Parts) -> None:
        if not parts.subtitles.available:
            self._say(t(UNAVAILABLE_TEXT))
            return
        self._parts = parts
        page, look = self._page, self._look
        current = parts.airgrabber_settings.current
        first_gap = self.waiting.frame.grid_info().get("pady")
        self.waiting.frame.grid_remove()

        languages = page.box(t(LANGUAGES_TITLE), t(LANGUAGES_TEXT))
        if first_gap is not None:  # the first box sits where the waiting box sat
            languages.frame.grid_configure(pady=first_gap)
        self._languages_box = languages
        self.languages = LanguageTags(
            languages.body, current.subtitle_languages, look, on_change=self._languages_changed
        )
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
            command=lambda _name: self._commit(
                hearing, subtitles_hearing_impaired=self._hearing_value()
            ),
        )
        self.hearing.set(t(HEARING_NAMES[current.subtitles_hearing_impaired]))
        hearing.add(self.hearing, sticky="w")

        accounts = self._accounts_page
        if accounts is None:
            self._account_box(page, parts, current)

        foot: SettingsBox = page.box(search_title=t(CREDIT_TEXT))
        self.quota_label = foot.text("")
        self._quota_row = foot.take_row()
        foot.add(link(foot.body, t(CREDIT_TEXT), CREDIT_URL, size=SMALL_SIZE), gap=0, sticky="w")

        if accounts is not None and self._accounts_waiting is not None:
            gap = self._accounts_waiting.frame.grid_info().get("pady")
            self._accounts_waiting.frame.grid_remove()
            box = self._account_box(accounts, parts, current)
            if gap is not None:  # where the waiting box sat
                box.frame.grid_configure(pady=gap)
        self._show_quota()
        self._unsubscribe = parts.airgrabber_settings.subscribe(self._stored_elsewhere)

    def _account_box(self, page: SettingsPage, parts: _Parts, current: Settings) -> SettingsBox:
        """The OpenSubtitles box: the account (with "Create a free account") and the own key."""
        look = self._look
        account = page.box(t(OPENSUBTITLES_TITLE))
        self.account_box = account
        hint = ctk.CTkFrame(account.body, fg_color="transparent")
        account.add(hint, gap=2, sticky="w")
        # It wraps beside the link (which keeps its line).
        sign_up_width = int(theme.font(_TEXT_SIZE, underline=True).measure(t(SIGN_UP_TEXT)))
        text_label(
            hint,
            t(ACCOUNT_HINT),
            size=_TEXT_SIZE,
            color=theme.TEXT_MUTED,
            wraplength=box_inner() - sign_up_width - 12,
        ).pack(side="left")
        link(hint, t(SIGN_UP_TEXT), SIGNUP_URL, size=_TEXT_SIZE).pack(side="left", padx=(4, 0))
        self.account = AccountRow(
            account.body,
            subtitles=parts.subtitles,
            username=current.opensubtitles_username,
            password=current.opensubtitles_password.get_secret_value(),
            look=look,
            on_signed_in=lambda _account: self._show_quota(),
            field_width=box_inner(),
        )
        account.add(self.account)
        account.subheading(t(OWN_KEY_LABEL))
        self.own_key = OwnKeyRow(
            account.body,
            subtitles=parts.subtitles,
            key=current.opensubtitles_own_key.get_secret_value(),
            look=look,
            field_width=box_inner(),
        )
        account.add(self.own_key, gap=6)
        settings = parts.airgrabber_settings
        # The name and the password are one change: moving between them stores nothing yet.
        self.account_field = self._committer.watch(
            TextCommit(
                [self.account.username, self.account.password],
                stored=lambda: [
                    settings.current.opensubtitles_username,
                    settings.current.opensubtitles_password.get_secret_value(),
                ],
                commit=lambda texts: self._commit_account(account, texts),
                strip=False,
            )
        )
        self.key_field = self._committer.watch(
            TextCommit(
                [self.own_key.entry],
                stored=lambda: [settings.current.opensubtitles_own_key.get_secret_value()],
                commit=lambda _texts: self._commit_own_key(account),
            )
        )
        return account

    def _show_quota(self) -> None:
        if self.quota_label is None or self._parts is None:
            return
        text = quota_line(self._parts.subtitles.quota, datetime.now(UTC))
        if text:
            self.quota_label.configure(text=text)
            self.quota_label.grid(row=self._quota_row, column=0, sticky="w", pady=(0, 8))
        else:
            self.quota_label.grid_remove()

    # --- storing ----------------------------------------------------------------------------------

    def _hearing_value(self) -> HearingImpaired:
        assert self.hearing is not None
        return self._hearing_names.get(self.hearing.get(), HearingImpaired.AVOID)

    def _commit(
        self,
        box: SettingsBox,
        *,
        check: Callable[[], Awaitable[None]] | None = None,
        verify: Callable[[], Awaitable[None]] | None = None,
        **fields: Any,
    ) -> asyncio.Task[bool] | None:
        """Store `fields` of AirGrabber's settings as one change (`verify`, then `check`
        first: `commit_fields`)."""
        parts = self._parts
        if parts is None:
            return None
        return spawn(
            commit_fields(
                self._committer,
                parts.airgrabber_settings,
                box,
                check=check,
                verify=verify,
                **fields,
            ),
            name="forge-subtitles-commit",
        )

    def _languages_changed(self, languages: tuple[str, ...]) -> None:
        spawn(self._store_languages(languages), name="forge-subtitles-languages")

    async def _store_languages(self, languages: tuple[str, ...]) -> bool:
        async def check() -> None:
            if not languages:
                raise Refused(t("Choose at least one subtitle language"))

        task = self._commit(self._languages_box, check=check, subtitle_languages=languages)
        stored = await task if task is not None else False
        if not stored and self._parts is not None and self.languages is not None:
            self.languages.show(self._parts.airgrabber_settings.current.subtitle_languages)
        return stored

    def _commit_account(self, box: SettingsBox, texts: list[str]) -> asyncio.Task[bool] | None:
        username, password = texts[0].strip(), texts[1]

        async def check() -> None:
            problem = account_problem(username=username, password=password)
            if problem is not None:
                raise Refused(problem)

        return self._commit(
            box, check=check, opensubtitles_username=username, opensubtitles_password=password
        )

    def _commit_own_key(self, box: SettingsBox) -> asyncio.Task[bool] | None:
        """The own key as typed (read now: the window may be closing), checked, then stored."""
        own_key = self.own_key
        if own_key is None or self._parts is None:
            return None
        key = own_key.value
        self._key_latest = key
        return spawn(self._store_own_key(box, own_key, key), name="forge-own-key")

    async def _store_own_key(self, box: SettingsBox, own_key: OwnKeyRow, key: str) -> bool:
        """One key at a time; a key typed after this one while its check was out wins (this
        one is dropped, never stored over it). OpenSubtitles' check runs before the settings
        are taken for the store: other settings changes don't wait for it."""
        async with self._key_lock:
            if key != self._key_latest:
                return False

            async def check() -> None:
                if key != self._key_latest:  # a newer key was typed during the check: dropped
                    raise Refused("")  # (nothing said, no red line taken: the newer one is next)

            async def verify() -> None:
                problem = await own_key.verify(key)  # does OpenSubtitles take it?
                await check()
                if problem is not None:
                    raise Refused(f"{t(OWN_KEY_LABEL)}: {problem}")

            task = self._commit(box, check=check, verify=verify, opensubtitles_own_key=key)
            return await task if task is not None else False

    def _stored_elsewhere(self, current: Settings) -> None:
        """Stored here, elsewhere or by an Undo: each control shows it, unless a field is being
        edited."""
        if self.languages is None or not self.languages.winfo_exists():
            return
        self.languages.show(current.subtitle_languages)
        if self.hearing is not None:
            name = t(HEARING_NAMES[current.subtitles_hearing_impaired])
            if self.hearing.get() != name:
                self.hearing.set(name)
        if self.account_field is not None:
            self.account_field.show(
                [current.opensubtitles_username, current.opensubtitles_password.get_secret_value()]
            )
        if self.key_field is not None:
            self.key_field.show([current.opensubtitles_own_key.get_secret_value()])


def build_subtitles(
    slots: PartSlots, forge: ForgeRuntime, committer: Committer
) -> ForgeSectionPart:
    """The Subtitles page's part, with the OpenSubtitles box on Accounts & keys."""
    from app.ui.settings_pages_registry import PAGE_ACCOUNTS, PAGE_SUBTITLES

    return SubtitlesSection(
        slots[PAGE_SUBTITLES],
        forge,
        accounts_page=slots.get(PAGE_ACCOUNTS),
        committer=committer,
    )

