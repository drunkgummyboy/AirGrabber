"""First-run welcome: what AirGrabber does, importing shows from airdates.tv, how downloads
should open, the TMDB key, and Quick setup ("Make it yours": look, download quality, card size,
language; app/ui/quick_setup.py).

MainWindow opens it on the first start of a new library (see `first_run_action`). Choices are
saved as they are made: an airdates.tv import tracks its shows at once; step 3's Next stores
how to download (with the NAS / other computer form when that is the choice; its Test
connection saves the form first); step 4's Next checks a pasted key nobody checked yet, and
stays on the step when TMDB doesn't accept it (Skip for now goes on without a key); step 5's
look applies at once. Finish (and closing the window) marks the setup as done and saves the
TMDB key that checked OK and the Quick setup choices; another language than the running one
offers "Restart now" (it saves the same first), else it applies at the next start.
"""

from __future__ import annotations

import asyncio
import math
import tkinter as tk
from collections.abc import Callable, Coroutine, Mapping
from enum import Enum
from pathlib import Path
from tkinter import filedialog
from typing import Any

import customtkinter as ctk
from pydantic import ValidationError

from app import APP_NAME
from app.i18n import N_, t
from app.models import DownloadMode, QBittorrentState, Settings, TorrentApp
from app.services import DiscoveryService, RemoteDownloads, SettingsService, TorrentAppService
from app.services.qbittorrent_local import set_up_by_airgrabber
from app.ui import theme
from app.ui.airdates_import import (
    SIZING_PREVIEW,
    AirdatesImportPanel,
    PreviewFinder,
    ShowImporter,
)
from app.ui.borderless import make_borderless
from app.ui.branding import load_logo, set_window_icon
from app.ui.quick_setup import HEADING as QUICK_HEADING
from app.ui.quick_setup import INTRO as QUICK_INTRO
from app.ui.quick_setup import QuickSetupPanel
from app.ui.dialog_kit import (
    BLOCK_GAP,
    BODY_SIZE,
    BUTTON_HEIGHT,
    CLOSE_CLEARANCE,
    CONTENT_WIDTH,
    DETAIL_SIZE,
    HEADING_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    FitScroll,
    activate_later,
    heading_row,
    max_dialog_height,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.remote_program_form import RemoteProgramForm, describe_error, result_look
from app.ui.tasks import spawn
from app.ui.text_fit import ELLIPSIS, SLACK, Measure, elide_to_width
from app.ui.tmdb_key_guide import TMDB_HEADING, TMDB_INTRO, KeyAction, TmdbKeyGuide
from app.ui.torrent_app_status import QBITTORRENT, TorrentAppStatus, TorrentStatus

STEP_COUNT = 5
# Shown through t().
AIRDATES_HEADING = N_("Already follow shows on airdates.tv?")
AIRDATES_INTRO = N_("Bring your list over, so your calendar is ready right away.")
AIRDATES_IMPORT_HEADING = N_("Import from airdates.tv")
WELCOME_TEXT = N_(
    "Follow your favourite shows, see what's on each day, and download episodes and movies "
    "with one click."
)

_LOGO_HEIGHT = 96
_CARD_PAD_X = 20
_CARD_PAD_Y = 18
_CARD_GAP = 14
_DOT_SIZE = 20
_DOT_GAP = 14
_CARD_TEXT_WIDTH = CONTENT_WIDTH - 2 * _CARD_PAD_X - _DOT_SIZE - _DOT_GAP
_FOLDER_BUTTON_WIDTH = 150
_FOLDER_TEXT_WIDTH = _CARD_TEXT_WIDTH - _FOLDER_BUTTON_WIDTH - 16
_REMOTE_LABEL_WIDTH = 150
REMOTE_TITLE = N_("Send to a torrent program on my NAS or another computer")
REMOTE_NOTE = N_("For a NAS or home server (Synology, qBittorrent, Transmission)")


# -- decisions (pure, tested) --------------------------------------------------------------------


class FirstRun(Enum):
    NOTHING = "nothing"  # set up already
    WELCOME = "welcome"  # a new library: open the welcome
    MARK_DONE = "mark_done"  # a library from before the welcome: record the setup as done


def first_run_action(setup_done: bool, tracked_shows: int) -> FirstRun:
    if setup_done:
        return FirstRun.NOTHING
    return FirstRun.MARK_DONE if tracked_shows > 0 else FirstRun.WELCOME


def first_run_defaults(
    current: Settings, app: TorrentApp | None, qbittorrent: QBittorrentState | None
) -> dict[str, Any]:
    """What a new install's first start saves before the welcome opens, when qBittorrent is the
    torrent program (install.ps1 installs it when there is none): open downloads in it
    (DownloadMode.APP), and "Where finished downloads end up" = qBittorrent's save folder while
    that is still empty (`qbittorrent`: its settings file; None when unreadable). Nothing with
    another program or none, or with a NAS / other computer already set up."""
    if app is None or app.name != QBITTORRENT:
        return {}
    if current.download_mode is DownloadMode.REMOTE or current.remote_configured:
        return {}
    changes: dict[str, Any] = {}
    if current.download_mode is not DownloadMode.APP:
        changes["download_mode"] = DownloadMode.APP
    if not current.subtitles_folder.strip() and qbittorrent is not None:
        changes["subtitles_folder"] = qbittorrent.save_path
    return changes


class Page(Enum):
    """The welcome's pages; AIRDATES_IMPORT is still step 2 (after "Yes" on AIRDATES)."""

    WELCOME = "welcome"
    AIRDATES = "airdates"  # already follow shows on airdates.tv? yes / no
    AIRDATES_IMPORT = "airdates_import"  # the import panel
    DOWNLOAD = "download"
    TMDB = "tmdb"
    QUICK = "quick"  # "Make it yours"


_STEPS = {
    Page.WELCOME: 1,
    Page.AIRDATES: 2,
    Page.AIRDATES_IMPORT: 2,
    Page.DOWNLOAD: 3,
    Page.TMDB: 4,
    Page.QUICK: 5,
}


def page_step(page: Page) -> int:
    return _STEPS[page]


def step_label(step: int) -> str | None:
    """'Step 2 of 5' at the top right of steps 2 to 5; the welcome itself has none."""
    return t("Step {step} of {count}", step=step, count=STEP_COUNT) if step >= 2 else None


def nav_buttons(page: Page, *, imported: bool = False) -> tuple[str | None, str | None]:
    """(left, right) texts of the bottom row; the welcome step has its button in the content.
    The import panel's right button is "Skip" until an import is done."""
    if page is Page.AIRDATES_IMPORT:
        return t("Back"), t("Next") if imported else t("Skip")
    if page in (Page.AIRDATES, Page.DOWNLOAD):
        return t("Back"), t("Next")
    if page is Page.TMDB:
        return t("Skip for now"), t("Next")
    if page is Page.QUICK:
        return t("Back"), t("Finish")
    return None, None


def next_page(page: Page, *, import_shows: bool) -> Page:
    """Where the right button (or "Get started") leads; `import_shows` = "Yes" on AIRDATES."""
    if page is Page.WELCOME:
        return Page.AIRDATES
    if page is Page.AIRDATES:
        return Page.AIRDATES_IMPORT if import_shows else Page.DOWNLOAD
    if page is Page.AIRDATES_IMPORT:
        return Page.DOWNLOAD
    if page is Page.DOWNLOAD:
        return Page.TMDB
    return Page.QUICK


def previous_page(page: Page, *, import_shows: bool) -> Page:
    """Where Back leads (TMDB's left button is Skip for now, not Back)."""
    if page is Page.QUICK:
        return Page.TMDB
    if page is Page.DOWNLOAD:
        return Page.AIRDATES_IMPORT if import_shows else Page.AIRDATES
    if page is Page.AIRDATES_IMPORT:
        return Page.AIRDATES
    return Page.WELCOME


def download_changes(
    current: Settings,
    mode: DownloadMode,
    folder: Path,
    remote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What step 3's Next saves: the mode, the folder when it was changed, and the NAS / other
    computer form (`remote`, its `remote_*` settings) when that is the choice. "Open in my
    torrent program" while downloads go to the qBittorrent on this PC that AirGrabber set up
    (its Web UI) keeps that: the same program gets them, without a pop-up, with progress."""
    if (
        mode is DownloadMode.APP
        and current.download_mode is DownloadMode.REMOTE
        and set_up_by_airgrabber(current)
    ):
        mode, remote = DownloadMode.REMOTE, None
    changes: dict[str, Any] = {"download_mode": mode}
    if folder != current.download_dir:
        changes["download_dir"] = folder
    if mode is DownloadMode.REMOTE and remote is not None:
        changes.update(remote)
    return changes


def changes_error(current: Settings, changes: Mapping[str, Any]) -> str | None:
    """Why `changes` wouldn't save (e.g. "Address: Use a full address like …"), else None."""
    try:
        Settings.model_validate({**current.model_dump(), **changes})
    except ValidationError as exc:
        return describe_error(exc)
    return None


def elide_path(path: str, max_width: float, measure: Measure) -> str:
    r"""`path` if it fits in `max_width`, else its drive (or UNC share) and as many trailing
    folders as fit, "…" in between: "C:\…\Videos\watch". When not even the last folder fits
    that way, the path elided at its end."""
    if measure(path) <= max_width:
        return path
    sep = "\\" if "\\" in path else "/"
    parts = path.split(sep)
    head_count = 4 if path.startswith(sep * 2) else 1  # "", "", server, share
    head, tail = sep.join(parts[:head_count]), parts[head_count:]
    best = ""
    for count in range(1, len(tail)):  # at least one folder is left out
        candidate = sep.join((head, ELLIPSIS, *tail[-count:]))
        if measure(candidate) > max_width:
            break
        best = candidate
    return best or elide_to_width(path, max_width, measure)


def finish_changes(checked_key: str | None) -> dict[str, Any]:
    """What closing the welcome saves: setup done, plus the TMDB key when one checked OK."""
    changes: dict[str, Any] = {"setup_done": True}
    if checked_key:
        changes["tmdb_api_key"] = checked_key
    return changes


def _page_heading(page: ctk.CTkFrame, title: str, which: Page) -> ctk.CTkFrame:
    """A step's heading with "Step N of 3", clear of the close button."""
    row = heading_row(page, title, step_label(page_step(which)), right_room=CLOSE_CLEARANCE)
    row.pack(fill="x")
    return row


# -- widgets -------------------------------------------------------------------------------------


class _OptionCard(ctk.CTkFrame):
    """A selectable option: radio-style dot, title, muted description and an optional extra row.

    The whole card is clickable; call `bind_select()` once everything inside it exists.
    """

    def __init__(self, master: Any, title: str, description: str | None = None) -> None:
        super().__init__(master, corner_radius=10, cursor="hand2")
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="x", padx=_CARD_PAD_X, pady=_CARD_PAD_Y)
        inner.grid_columnconfigure(1, weight=1)
        self._dot = ctk.CTkFrame(
            inner,
            width=_DOT_SIZE,
            height=_DOT_SIZE,
            corner_radius=_DOT_SIZE // 2,
            fg_color="transparent",
        )
        self._dot.grid(row=0, column=0, sticky="w", padx=(0, _DOT_GAP))
        ctk.CTkLabel(
            inner,
            text=title,
            font=theme.font(17, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        if description is not None:
            text_label(
                inner,
                description,
                size=DETAIL_SIZE,
                color=theme.TEXT_MUTED,
                wraplength=_CARD_TEXT_WIDTH,
            ).grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.extra = ctk.CTkFrame(inner, fg_color="transparent")
        self.set_selected(False)

    def show_extra(self, show: bool) -> None:
        if show:
            self.extra.grid(row=2, column=1, sticky="ew", pady=(12, 0))
        else:
            self.extra.grid_remove()

    def set_selected(self, selected: bool) -> None:
        self.configure(
            border_width=2 if selected else 1,
            border_color=theme.ACCENT if selected else theme.CELL_BORDER,
            fg_color=theme.TODAY_BG if selected else theme.CELL_BG,
        )
        self._dot.configure(
            border_width=6 if selected else 2,
            border_color=theme.ACCENT if selected else theme.EDGE,
        )

    def bind_select(self, command: Callable[[], None]) -> None:
        stack: list[tk.Misc] = [self]
        while stack:
            widget = stack.pop()
            widget.bind("<Button-1>", lambda _event: command(), add="+")
            stack.extend(widget.winfo_children())


class WelcomeDialog(ctk.CTkToplevel):
    """Five steps: welcome; import the shows followed on airdates.tv (a yes / no question, then
    the import panel); open downloads in the torrent program or save them to a folder; the TMDB
    key; Quick setup. Modal over the main window."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        settings: SettingsService,
        torrent_app: TorrentAppService,
        discovery: DiscoveryService,
        airdates: PreviewFinder,
        library: ShowImporter,
        remote: RemoteDownloads | None = None,  # None: one reading `settings` (main_window.py)
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        """`on_restart`: restarts AirGrabber (Quick setup's "Restart now" for another language
        or accent colour; without it they apply at the next start)."""
        super().__init__(master, fg_color=theme.BG)
        self._on_restart = on_restart
        self._skip_key = False  # Skip for now on step 4: Finish saves no key
        self._settings = settings
        self._torrent_app = torrent_app
        self._remote = remote if remote is not None else RemoteDownloads(settings)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._logo_image: ctk.CTkImage | None = None
        self._page = Page.WELCOME
        self._import_shows = True  # step 2's suggestion
        self._imported = False  # an airdates.tv import finished
        self._mode = DownloadMode.APP  # the welcome's suggestion, whatever is stored
        self._folder = settings.current.download_dir
        self._headings: list[ctk.CTkFrame] = []  # the steps' headings (the window drags by them)

        self.title(t("Welcome to {app}", app=APP_NAME))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        # The steps share one cell (the shown one is gridded), which scrolls only when the
        # screen is too small for the tallest step; this frame underneath holds the content
        # width.
        self._steps = FitScroll(body)
        self._steps.grid(row=0, column=0, sticky="nsew")
        steps = self._steps
        ctk.CTkFrame(steps, width=CONTENT_WIDTH, height=1, fg_color="transparent").grid(
            row=0, column=0, sticky="n"
        )
        self._pages = {
            Page.WELCOME: self._build_welcome(steps),
            Page.AIRDATES: self._build_airdates(steps),
            Page.AIRDATES_IMPORT: self._build_airdates_import(steps, airdates, library),
            Page.DOWNLOAD: self._build_download(steps),
            Page.TMDB: self._build_tmdb(steps, discovery),
            Page.QUICK: self._build_quick(steps),
        }
        for page in self._pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        nav = ctk.CTkFrame(body, fg_color="transparent", height=BUTTON_HEIGHT)
        nav.grid(row=1, column=0, sticky="ew", pady=(NAV_GAP, 0))
        nav.pack_propagate(False)  # keeps its height on the welcome step, which has no buttons
        self._left = secondary_button(nav, "", self._on_left)
        self._right = primary_button(nav, "", self._on_right)

        # No Escape; the ✕ (and Alt+F4) call `_close`: done with what was chosen so far.
        self.borderless = make_borderless(self, drag_widgets=(self, body, nav, *self._headings))
        self._keep_tallest_size()
        self._show_page(Page.WELCOME)
        place_over(self, master)
        activate_later(self)
        self._spawn(self._load_logo(), "welcome-logo")

    # -- steps ---------------------------------------------------------------------------------

    def _build_welcome(self, master: Any) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        inner = ctk.CTkFrame(page, fg_color="transparent")
        inner.pack(expand=True)
        self._logo = ctk.CTkLabel(inner, text="", height=_LOGO_HEIGHT)
        self._logo.pack()
        ctk.CTkLabel(
            inner,
            text=t("Welcome to {app}", app=APP_NAME),
            font=theme.font(HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
        ).pack(pady=(BLOCK_GAP, 0))
        ctk.CTkLabel(
            inner,
            text=t(WELCOME_TEXT),
            font=theme.font(BODY_SIZE),
            text_color=theme.TEXT,
            justify="center",
            wraplength=480,
        ).pack(pady=(12, 0))
        primary_button(
            inner, t("Get started"), lambda: self._show_page(Page.AIRDATES), width=180
        ).pack(pady=(NAV_GAP, 0))
        return page

    def _build_airdates(self, master: Any) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        self._headings.append(_page_heading(page, t(AIRDATES_HEADING), Page.AIRDATES))
        text_label(page, t(AIRDATES_INTRO), size=DETAIL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )
        self._yes_card = _OptionCard(page, t("Yes, import my shows"))
        self._yes_card.pack(fill="x", pady=(BLOCK_GAP, 0))
        self._yes_card.bind_select(lambda: self._select_import(True))
        self._no_card = _OptionCard(page, t("No, I'll add shows myself"))
        self._no_card.pack(fill="x", pady=(_CARD_GAP, 0))
        self._no_card.bind_select(lambda: self._select_import(False))
        self._select_import(self._import_shows)
        return page

    def _build_airdates_import(
        self, master: Any, airdates: PreviewFinder, library: ShowImporter
    ) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        heading = _page_heading(page, t(AIRDATES_IMPORT_HEADING), Page.AIRDATES_IMPORT)
        self._headings.append(heading)
        self._airdates_panel = AirdatesImportPanel(
            page, airdates=airdates, library=library, on_done=self._on_imported
        )
        self._airdates_panel.pack(fill="x", pady=(BLOCK_GAP, 0))
        return page

    def _build_download(self, master: Any) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        heading = _page_heading(page, t("How do you want to download?"), Page.DOWNLOAD)
        self._headings.append(heading)
        text_label(
            page,
            t("You can change this later in Settings."),
            size=DETAIL_SIZE,
            color=theme.TEXT_MUTED,
        ).pack(anchor="w", pady=(LINE_GAP, 0))

        # The cards scroll when the NAS / other computer form makes them taller than the step.
        self._download_cards = ctk.CTkScrollableFrame(
            page,
            width=CONTENT_WIDTH,
            fg_color="transparent",
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self._download_cards.pack(fill="both", expand=True, pady=(BLOCK_GAP, 0))
        cards = self._download_cards

        self._app_card = _OptionCard(
            cards,
            t("Open in my torrent program"),
            t("Each download opens in your torrent program by itself. Best for most people."),
        )
        self._app_card.pack(fill="x")
        self._torrent = TorrentAppStatus(
            self._app_card.extra, torrent_app=self._torrent_app, wraplength=_CARD_TEXT_WIDTH
        )
        self._torrent.pack(fill="x")
        self._app_card.show_extra(True)
        self._app_card.bind_select(lambda: self._select_mode(DownloadMode.APP))

        self._remote_card = _OptionCard(cards, t(REMOTE_TITLE), t(REMOTE_NOTE))
        self._remote_card.pack(fill="x", pady=(_CARD_GAP, 0))
        self._remote_form = RemoteProgramForm(
            self._remote_card.extra,
            settings=self._settings.current,
            run_test=self._test_remote,
            label_width=_REMOTE_LABEL_WIDTH,
            field_width=_CARD_TEXT_WIDTH - _REMOTE_LABEL_WIDTH,
        )
        self._remote_form.pack(fill="x")
        self._remote_card.bind_select(lambda: self._select_mode(DownloadMode.REMOTE))

        self._folder_card = _OptionCard(
            cards, t("Save to a folder"), t("For a watch folder, for example on a NAS.")
        )
        self._folder_card.pack(fill="x", pady=(_CARD_GAP, 0))
        folder_row = self._folder_card.extra
        # One line, whatever the path: the dialog keeps the size it had with one line.
        self._folder_label = text_label(folder_row, "", size=DETAIL_SIZE, wraplength=0)
        self._folder_label.pack(side="left")
        self._show_folder()
        secondary_button(
            folder_row, t("Choose folder…"), self._choose_folder, width=_FOLDER_BUTTON_WIDTH
        ).pack(side="right")
        self._folder_card.bind_select(lambda: self._select_mode(DownloadMode.FOLDER))

        self._select_mode(self._mode)
        return page

    def _build_tmdb(self, master: Any, discovery: DiscoveryService) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        self._headings.append(_page_heading(page, t(TMDB_HEADING), Page.TMDB))
        text_label(
            page,
            f"{t(TMDB_INTRO)} {t('You can skip this and do it later.')}",
            size=DETAIL_SIZE,
            color=theme.TEXT_MUTED,
        ).pack(anchor="w", pady=(LINE_GAP, 0))
        self._guide = TmdbKeyGuide(page, discovery=discovery)
        self._guide.pack(fill="x", pady=(BLOCK_GAP, 0))
        return page

    def _build_quick(self, master: Any) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        self._headings.append(_page_heading(page, t(QUICK_HEADING), Page.QUICK))
        text_label(page, t(QUICK_INTRO), size=DETAIL_SIZE, color=theme.TEXT_MUTED).pack(
            anchor="w", pady=(LINE_GAP, 0)
        )
        restart = self._restart_now if self._on_restart is not None else None
        self.quick = QuickSetupPanel(page, current=self._settings.current, on_restart=restart)
        self.quick.pack(fill="x", pady=(16, 0))
        return page

    def _keep_tallest_size(self) -> None:
        """Fix the dialog at its tallest (the downloads step with the torrent program's button and
        the folder row, or the airdates.tv review with every kind of line), so it doesn't change size
        between steps or while a step changes. The download cards' area takes the height of the
        cards with the NAS / other computer form folded; with the form open they scroll. On a
        screen too small for that (`max_dialog_height`) the dialog is as tall as it allows and a
        step taller than its room scrolls, with the scrollbar's room kept beside every step."""
        self._torrent.show(TorrentStatus.SETUP_NEEDED)  # its tallest: the line and a button
        self._remote_card.show_extra(False)
        self._folder_card.show_extra(True)
        self._airdates_panel.show_review(SIZING_PREVIEW)
        self.quick.show_restart(always=True)  # with the language note and Restart now
        self.update_idletasks()
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        cards = math.ceil(self._download_cards.winfo_reqheight() / scaling)
        self._download_cards.configure(height=cards)
        self.update_idletasks()
        steps = self._steps  # every step gridded: as tall as the tallest
        tallest = math.ceil(steps.content_height() / scaling)
        steps.configure(width=math.ceil(steps.winfo_reqwidth() / scaling), height=tallest)
        self.update_idletasks()
        height = math.ceil(self.winfo_reqheight() / scaling)
        room = max_dialog_height(self.master, scaling)
        if height > room:
            steps.configure(height=max(1, tallest - (height - room)))
            steps.reserve_bar()
            self.update_idletasks()
            height = room
        width = steps.width_needed(scaling) + 2 * PAD_X
        self._torrent.show(TorrentStatus.LOOKING)
        self._select_mode(self._mode)
        self._airdates_panel.show_paste()
        self.quick.show_restart()
        self.geometry(f"{width}x{height}")  # CTk scales the size (not the position)

    def _show_page(self, page: Page) -> None:
        self._page = page
        for which, frame in self._pages.items():
            if which is page:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        self._steps.to_top()
        self._steps.refit()
        left, right = nav_buttons(page, imported=self._imported)
        for button, text, side in ((self._left, left, "left"), (self._right, right, "right")):
            if text is None:
                button.pack_forget()
            else:
                button.configure(text=text, state="normal")
                button.pack(side=side)
        if page is Page.DOWNLOAD:
            self._torrent.refresh()  # again each time: a program may have been installed
        elif page is Page.AIRDATES_IMPORT:
            self._airdates_panel.focus_paste()

    def _on_left(self) -> None:
        if self._page is Page.TMDB:
            self._skip_key = True  # Skip for now: on without a key
            self._show_page(Page.QUICK)
        else:
            self._show_page(previous_page(self._page, import_shows=self._import_shows))

    def _on_right(self) -> None:
        if self._page is Page.DOWNLOAD:
            self._spawn(self._save_download_choice(), "welcome-download-choice")
        elif self._page in (Page.AIRDATES, Page.AIRDATES_IMPORT):
            self._show_page(next_page(self._page, import_shows=self._import_shows))
        elif self._page is Page.TMDB:
            self._skip_key = False
            if self._guide.key_action() is KeyAction.CHECK:
                self._spawn(self._check_then_go_on(), "welcome-key-check")
            else:
                self._show_page(Page.QUICK)
        elif self._page is Page.QUICK:
            self._finish()

    # -- step 2: shows from airdates.tv -----------------------------------------------------------

    def _select_import(self, import_shows: bool) -> None:
        self._import_shows = import_shows
        self._yes_card.set_selected(import_shows)
        self._no_card.set_selected(not import_shows)

    def _on_imported(self, _count: int) -> None:
        self._imported = True
        if self._page is Page.AIRDATES_IMPORT:
            self._show_page(Page.AIRDATES_IMPORT)  # Skip becomes Next

    # -- step 3: how to download -----------------------------------------------------------------

    def _select_mode(self, mode: DownloadMode) -> None:
        self._mode = mode
        self._app_card.set_selected(mode is DownloadMode.APP)
        self._remote_card.set_selected(mode is DownloadMode.REMOTE)
        self._remote_card.show_extra(mode is DownloadMode.REMOTE)
        self._folder_card.set_selected(mode is DownloadMode.FOLDER)
        self._folder_card.show_extra(mode is DownloadMode.FOLDER)

    async def _test_remote(self) -> str:
        """The form's Test connection: save its settings, then test the program."""
        await self._settings.update(**self._remote_form.changes())
        return await self._remote.test()

    def _choose_folder(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(parent=self, initialdir=str(self._folder), mustexist=True)
        if chosen:
            self._folder = Path(chosen)
            self._show_folder()

    def _show_folder(self) -> None:
        measure = theme.font(DETAIL_SIZE).measure
        text = elide_path(str(self._folder), _FOLDER_TEXT_WIDTH - SLACK, measure)
        self._folder_label.configure(text=text)

    async def _save_download_choice(self) -> None:
        current = self._settings.current
        changes = download_changes(current, self._mode, self._folder, self._remote_form.changes())
        if (error := changes_error(current, changes)) is not None:
            self._remote_form.show_result(result_look(error=error))  # e.g. a bad address
            return
        self._set_nav_state("disabled")
        saved = self._save(changes)
        await asyncio.wait([saved])  # not `await saved`: cancelling this must not cancel the save
        if not self.winfo_exists():
            return
        self._set_nav_state("normal")
        if saved.cancelled() or saved.exception() is not None:
            return  # logged and reported by the task runner
        self._show_page(Page.TMDB)

    # -- finishing -------------------------------------------------------------------------------

    async def _check_then_go_on(self) -> None:
        """Next with a key nobody checked yet: check it first. When TMDB doesn't accept it (or
        can't be reached) the step stays, showing why; Skip for now still goes on without it."""
        self._right.configure(state="disabled")
        key = await self._guide.check_now()
        if not self.winfo_exists():
            return
        self._right.configure(state="normal")
        if key is not None:
            self._show_page(Page.QUICK)

    def _close(self) -> None:
        """The round ✕ (or Alt+F4): done, with whatever was chosen so far."""
        self._finish()

    def _final_changes(self) -> dict[str, Any]:
        """Setup done, the key that checked OK (unless skipped) and Quick setup's choices."""
        key = None if self._skip_key else self._guide.checked_key
        return {**self.quick.changes(self._settings.current), **finish_changes(key)}

    def _finish(self) -> None:
        self._save(self._final_changes())
        self.destroy()

    def _restart_now(self) -> None:
        """Quick setup's "Restart now": save like Finish, then restart once it is stored."""
        self._set_nav_state("disabled")
        self._spawn(self._save_then_restart(), "welcome-restart")

    async def _save_then_restart(self) -> None:
        saved = self._save(self._final_changes())
        await asyncio.wait([saved])  # not cancelled with the dialog
        if self._on_restart is not None:
            self._on_restart()  # closes this dialog with the main window

    def _save(self, changes: dict[str, Any]) -> asyncio.Task[Settings]:
        """Save `changes`; not cancelled with the dialog. SettingsService applies updates one at
        a time, in order, so step 3's save and Finish's can't undo each other."""
        return spawn(self._settings.update(**changes), name="welcome-save")

    # -- helpers ---------------------------------------------------------------------------------

    def _set_nav_state(self, state: str) -> None:
        self._left.configure(state=state)
        self._right.configure(state=state)

    async def _load_logo(self) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        logo = await asyncio.to_thread(load_logo, round(_LOGO_HEIGHT * scaling))
        if logo is None or not self.winfo_exists():
            return
        width = round(logo.width / scaling)
        self._logo_image = ctk.CTkImage(light_image=logo, dark_image=logo, size=(width, _LOGO_HEIGHT))
        self._logo.configure(image=self._logo_image)

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        super().destroy()
