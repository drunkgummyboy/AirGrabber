"""The root window: header (mode switch, search, settings), page area and status bar."""

from __future__ import annotations

import asyncio
import logging
import time
import tkinter as tk
from collections.abc import Awaitable, Callable
from enum import StrEnum
from types import TracebackType
from typing import NamedTuple

import customtkinter as ctk
from async_tkinter_loop.mixins import AsyncCTk

from app import APP_NAME, __version__
from app.models import (
    AutoDownloaded,
    AutoError,
    AutoEvent,
    AutoGaveUp,
    AutoMovieDownloaded,
    AutoMovieGaveUp,
    CalendarEntry,
    GrabTarget,
    MediaType,
    MissingProgress,
    Movie,
    MovieDetails,
    ProwlarrSetupResult,
    QuickDownloadResult,
    Settings,
    Show,
    SubtitleEvent,
    SubtitleProblem,
    SubtitlesSaved,
    SyncProgress,
    UpdateInfo,
)
from app.services import Services, UpdateError
from app.services.missing import MissingRunBusy
from app.services.auto_download import GIVE_UP_AFTER
from app.services.watchlist import GIVE_UP_AFTER as MOVIE_GIVE_UP_AFTER
from app.services.downloads import GrabError
from app.services.synology import DownloadStationError
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.branding import load_logo, set_window_icon
from app.ui.card_scale import CardScale
from app.ui.download_control import (
    DownloadActions,
    DownloadKey,
    DownloadStates,
    Phase,
    RunState,
    already_downloaded_text,
    download_status_text,
    episode_key,
    movie_key,
    nothing_found_text,
)
from app.ui.missing_control import (
    OUTCOME_MS,
    MissingActions,
    MissingRuns,
    MissingRunState,
    have_status_text,
    outcome_status_text,
)
from app.ui.indexer_dialog import (
    IndexerDialog,
    IndexerTarget,
    MovieTarget,
    TvTarget,
    movie_title,
)
from app.ui.search import SearchResultsWindow, SearchSuggestions
from app.ui.settings_dialog import SettingsDialog
from app.ui.show_details import ShowWindows
from app.ui.subtitle_settings import subtitle_mark
from app.ui.tasks import on_task_error, spawn
from app.ui.tooltip import add_tooltip
from app.ui.tray import TrayHost
from app.ui.views import BaseView, CalendarView, DiscoverView, MovieView, TrackedView
from app.ui.welcome_dialog import FirstRun, WelcomeDialog, first_run_action

log = logging.getLogger(__name__)

_LOGO_HEIGHT = 100  # header logo, as in 1.x
_UPDATE_CHECK_DELAY_S = 5  # let the pages load first
_DOWNLOAD_STATUS_MS = 10_000  # how long a one-click download's result stays in the status bar
_DETAILS_WAIT_S = 5.0  # a one-click movie download waits this long for TMDB details (IMDb id)
_UPDATE_STATUS_MS = 10_000  # how long "… is ready" / "Updated to …" stay in the status bar
# The update badges' hover tips.
UPDATE_TIP = "Open Settings to update"
RESTART_TIP = "Installs the downloaded update and restarts AirGrabber"
# Settings no page shows (connections, how downloads are handed over, the first-run marker,
# updates): changing only these (as the automatic Prowlarr setup, the welcome and every start
# do) leaves the pages as they are.
_OFF_PAGE_SETTINGS = frozenset(
    {
        "prowlarr_url",
        "prowlarr_api_key",
        "prowlarr_seeded_url",
        "remote_kind",
        "remote_url",
        "remote_username",
        "remote_password",
        "remote_verify_tls",
        "remote_destination",
        "download_mode",
        "auto_download_new_shows",
        "setup_done",
        "auto_update",
        "last_run_version",
        "keep_in_tray",
        "tray_notice_shown",
        "start_with_windows",
        "subtitles_enabled",
        "subtitle_languages",
        "subtitles_folder",
        "opensubtitles_username",
        "opensubtitles_password",
        "opensubtitles_own_key",
    }
)


def settings_affect_pages(previous: Settings, updated: Settings) -> bool:
    """Whether a settings change touches anything a page shows (see _OFF_PAGE_SETTINGS)."""
    return any(
        getattr(previous, name) != getattr(updated, name)
        for name in Settings.model_fields
        if name not in _OFF_PAGE_SETTINGS
    )


def subtitle_status_text(problem: SubtitleProblem) -> str:
    """The status-bar line for a subtitle problem (already plain words, no URLs or secrets)."""
    return f"⚠ Subtitles: {problem.message}"


def prowlarr_setup_message(result: ProwlarrSetupResult) -> str | None:
    """The status-bar text for what the automatic Prowlarr setup did; None when it did nothing."""
    added = ", ".join(result.added)
    if result.connected and added:
        return f"Connected to Prowlarr on this PC · added {added} to your sites"
    if result.connected:
        return "Connected to Prowlarr on this PC"
    if added:
        return f"Prowlarr: added {added} to your sites"
    return None


def update_notice(last_run_version: str, running: str) -> str | None:
    """The status-bar text after an update: the version that ran last differs from this one."""
    if last_run_version and last_run_version != running:
        return f"Updated to v{running}"
    return None


_SUBTITLE_STATUS_MS = 15_000  # how long a subtitle problem stays in the status bar
_AUTO_STATUS_MS = 10_000  # how long an automatic download's line stays in the status bar
AUTO_ERROR_INTERVAL_S = 3600.0  # at most one "Automatic download didn't work" per hour


class AutoNotice(NamedTuple):
    """How an automatic-download event is told: a tray notification (title, body) and the
    status-bar line (`status`), for 10 s. Without a tray icon, only the status bar."""

    title: str
    body: str
    status: str


def auto_notice(event: AutoEvent) -> AutoNotice:
    if isinstance(event, AutoDownloaded):
        body = f"{event.quality} · picked automatically" if event.quality else "Picked automatically"
        return AutoNotice(
            title=f"{event.label} is downloading",
            body=body,
            status=f"{event.label} · downloading (picked automatically)",
        )
    if isinstance(event, AutoGaveUp):
        title = f"Couldn't find {event.label}"
        body = (
            f"No good download turned up in {GIVE_UP_AFTER.days} days. "
            "Choose one yourself from the calendar."
        )
        return AutoNotice(title=title, body=body, status=f"{title}. {body}")
    if isinstance(event, AutoMovieDownloaded):
        how = "picked automatically from your watchlist"
        return AutoNotice(
            title=f"Downloaded {event.title}",
            body=f"{event.quality} · {how}" if event.quality else how.capitalize(),
            status=f"Downloaded {event.title} ({how})",
        )
    if isinstance(event, AutoMovieGaveUp):
        title = f"Couldn't find {event.title}"
        body = (
            f"No good download turned up in {MOVIE_GIVE_UP_AFTER.days} days. "
            "Choose one yourself from your watchlist on the Movies page."
        )
        return AutoNotice(title=title, body=body, status=f"{title}. {body}")
    title = "Automatic download didn't work"
    return AutoNotice(title=title, body=event.message, status=f"{title}: {event.message}")


class AutoNoticeThrottle:
    """Lets every event through except AutoError, which passes at most once per `interval_s`
    (a broken setup fails every run). `clock`: monotonic seconds (tests move it)."""

    def __init__(
        self,
        interval_s: float = AUTO_ERROR_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._last_error: float | None = None

    def allow(self, event: AutoEvent) -> bool:
        if not isinstance(event, AutoError):
            return True
        now = self._clock()
        if self._last_error is not None and now - self._last_error < self._interval_s:
            return False
        self._last_error = now
        return True


class Mode(StrEnum):
    SERIES = "Series"
    MOVIES = "Movies"


class MainWindow(AsyncCTk, ctk.CTk):
    """Owns layout and navigation only; every page fetches its own data through services."""

    def __init__(
        self,
        services: Services,
        *,
        start_hidden: bool = False,
        show_requests: Callable[[], bool] | None = None,
    ) -> None:
        theme.apply_appearance()
        super().__init__(fg_color=theme.BG)
        self._services = services
        self._mode: Mode | None = None
        self._idle_status = ""
        self._status_job: str | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._results_window: SearchResultsWindow | None = None
        self._indexer_dialog: IndexerDialog | None = None
        self._logo_image: ctk.CTkImage | None = None
        self._update_info: UpdateInfo | None = None
        self._update_task: asyncio.Task[None] | None = None
        self.restart_requested = False  # read by main.py once the window has closed
        # One-click downloads: their state per title, shared by every card that shows it.
        self._download_states = DownloadStates()
        self._download_tasks: set[asyncio.Task[None]] = set()
        self._episode_actions: DownloadActions[CalendarEntry] = DownloadActions(
            download=self._download_episode,
            choose=self._search_episode,
            states=self._download_states,
        )
        self._movie_actions: DownloadActions[Movie] = DownloadActions(
            download=self._download_movie, choose=self._grab_movie, states=self._download_states
        )
        # "Download missing": its run state per show, shared by every button that shows it.
        self._missing_runs = MissingRuns()
        self._missing_actions = MissingActions(
            download=self._download_missing,
            mark_have=self._mark_have,
            unmark_have=self._unmark_have,
            runs=self._missing_runs,
        )
        # The show details windows (one per show), opened from the show cards.
        self._show_windows = ShowWindows(
            self,
            library=services.library,
            discovery=services.discovery,
            images=services.images,
            status=self.set_status,
            episode_actions=self._episode_actions,
            downloads_changed=services.downloads.subscribe,
            missing_actions=self._missing_actions,
            subtitle_marks=self._subtitle_marks,
            subtitles_changed=self._on_subtitles_saved,
        )

        self.title(f"{APP_NAME} — Track your shows. Grab your movies.")
        set_window_icon(self)
        self._place(1650, 900)
        self.minsize(1220, 640)  # narrowest width at which the centred header still fits
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_pages()
        self._build_status_bar()

        on_task_error(self._report_task_error)
        self._settings_seen = services.settings.current
        self._unsubscribe_settings = services.settings.subscribe(self._on_settings_changed)
        self._unsubscribe_library = services.library.subscribe(self._on_library_changed)
        # A grab flags episodes ("✓ Grabbed") and films as downloaded.
        self._unsubscribe_downloads = services.downloads.subscribe(self._reload_pages)
        # The automatic downloads: a tray notification (or the status bar) per event.
        self._auto_throttle = AutoNoticeThrottle()
        self._unsubscribe_auto = services.auto_download.subscribe(self._on_auto_event)
        # Subtitles: problems in the status bar (saved ones only update the show windows).
        self._unsubscribe_subtitles = services.subtitles.subscribe(self._on_subtitle_event)

        # Closing the window keeps AirGrabber running in the tray; main.py stops the icon at exit.
        self.tray = TrayHost(
            self,
            settings=services.settings,
            on_check_now=self._check_now,
            show_requests=show_requests,  # a second start of AirGrabber asks for the window
        )
        self.protocol("WM_DELETE_WINDOW", self.tray.close_window)
        if start_hidden:
            self.withdraw()  # main.py --tray: only the tray icon shows

    def async_mainloop(self, event_loop: asyncio.AbstractEventLoop | None = None) -> None:
        loop = event_loop or asyncio.new_event_loop()
        # Not after_idle(): CTk pumps Tk events (update()) before the asyncio loop is running,
        # and nothing may spawn coroutines until it is.
        loop.call_soon(self._start)
        super().async_mainloop(event_loop=loop)

    def destroy(self) -> None:
        self._suggestions.destroy()
        self._show_windows.close()
        if self._results_window is not None and self._results_window.winfo_exists():
            self._results_window.destroy()
        self._results_window = None
        if self._indexer_dialog is not None and self._indexer_dialog.winfo_exists():
            self._indexer_dialog.destroy()
        self._indexer_dialog = None
        for task in list(self._download_tasks):
            task.cancel()
        self._unsubscribe_settings()
        self._unsubscribe_library()
        self._unsubscribe_downloads()
        self._unsubscribe_auto()
        self._unsubscribe_subtitles()
        # async-tkinter-loop keeps draining Tk events after destroy(); cancel CTk's pending timers
        # (DPI checks, redraws) so they don't fire into a dead interpreter.
        for job in self.tk.splitlist(self.tk.call("after", "info")):
            self.after_cancel(job)
        super().destroy()

    # -- layout ------------------------------------------------------------------------------

    def _place(self, width: int, height: int) -> None:
        """Centre a `width`×`height` (unscaled) window on the primary screen.

        CTk multiplies width/height by the DPI factor but not x/y, and on Windows
        winfo_screenwidth() reports unscaled units — so measure with wm_maxsize() (physical px).
        """
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        screen_w, screen_h = self.wm_maxsize()
        width = min(width, int(screen_w / scaling) - 40)
        height = min(height, int(screen_h / scaling) - 40)
        x = (screen_w - round(width * scaling)) // 2
        y = (screen_h - round(height * scaling)) // 2
        self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 5))
        header.grid_columnconfigure((0, 2), weight=1, uniform="header")

        nav = ctk.CTkFrame(header, fg_color="transparent")
        nav.grid(row=0, column=1)
        self._mode_buttons = {
            mode: ctk.CTkButton(
                nav,
                text=mode.value,
                width=90,
                height=30,
                corner_radius=6,
                fg_color="transparent",
                hover_color=theme.SURFACE,
                font=theme.font(14, bold=True),
                command=lambda m=mode: self._select_mode(m),
            )
            for mode in Mode
        }
        self._mode_buttons[Mode.SERIES].grid(row=0, column=0, padx=15)
        # The name stands in until the logo has loaded; the row is logo-tall from the start.
        self._logo = ctk.CTkLabel(
            nav,
            text=APP_NAME,
            height=_LOGO_HEIGHT,
            font=theme.font(24, bold=True),
            text_color=theme.ACCENT,
        )
        self._logo.grid(row=0, column=1, padx=15)
        self._mode_buttons[Mode.MOVIES].grid(row=0, column=2, padx=15)

        tools = ctk.CTkFrame(header, fg_color="transparent")
        tools.grid(row=0, column=2, sticky="e")
        self._search = ctk.CTkEntry(
            tools,
            placeholder_text="Search shows & movies…",
            placeholder_text_color=theme.TEXT_MUTED,
            width=260,
            height=40,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
        )
        self._search.pack(side="left", padx=(0, 5))
        self._search.bind("<Return>", lambda _event: self._on_search())
        self._suggestions = SearchSuggestions(
            self._search,
            root=self,
            library=self._services.library,
            discovery=self._services.discovery,
            on_open=self._open_search_results,
        )
        search_button = ctk.CTkButton(
            tools,
            text="🔍",
            width=40,
            height=40,
            corner_radius=10,
            fg_color=theme.SURFACE,
            hover_color=theme.ACCENT_HOVER,
            border_width=1,
            border_color=theme.EDGE,
            font=theme.font(18),
            command=self._on_search,
        )
        search_button.pack(side="left", padx=(0, 15))
        add_tooltip(search_button, "Search")
        settings_button = ctk.CTkButton(
            tools,
            text="⚙",
            width=40,
            height=40,
            corner_radius=10,
            fg_color="transparent",
            hover_color=theme.SURFACE,
            font=theme.font(26),
            command=self._open_settings,
        )
        settings_button.pack(side="left")
        add_tooltip(settings_button, "Settings")

    def _build_pages(self) -> None:
        library, settings = self._services.library, self._services.settings
        images, discovery = self._services.images, self._services.discovery
        self._series_tabs = ctk.CTkTabview(
            self,
            corner_radius=15,
            border_width=1,
            fg_color=theme.TAB_BG,
            border_color=theme.EDGE,
            segmented_button_fg_color=theme.SURFACE,
            segmented_button_selected_color=theme.ACCENT,
            segmented_button_selected_hover_color=theme.ACCENT_HOVER,
            segmented_button_unselected_color=theme.SURFACE,
            segmented_button_font=theme.font(16, bold=True),
            command=self._on_tab_changed,
        )
        tabs = self._series_tabs
        self._series_views: dict[str, BaseView] = {
            "Calendar": CalendarView(
                tabs.add("Calendar"),
                library=library,
                settings=settings,
                images=images,
                status=self.set_status,
                episode_actions=self._episode_actions,
            ),
            "Discover": DiscoverView(
                tabs.add("Discover"),
                discovery=discovery,
                images=images,
                status=self.set_status,
                on_open_settings=self._open_settings,
                on_open_show=self._show_windows.open_discover,
                card_scale=self._card_scale,
            ),
            "Tracked": TrackedView(
                tabs.add("Tracked"),
                library=library,
                airdates=self._services.airdates,
                images=images,
                status=self.set_status,
                on_open_show=self._show_windows.open_show,
                missing_actions=self._missing_actions,
                card_scale=self._card_scale,
            ),
        }
        for view in self._series_views.values():
            view.pack(fill="both", expand=True)

        self._movie_view = MovieView(
            self,
            discovery=discovery,
            images=images,
            status=self.set_status,
            on_open_settings=self._open_settings,
            movie_actions=self._movie_actions,
            watchlist=self._services.watchlist,
            card_scale=self._card_scale,
        )

    def _card_scale(self) -> CardScale:
        """The cards' size as Settings has it now; the pages read it at every load."""
        return CardScale.of(self._services.settings.current.card_size)

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=15, pady=(0, 6))
        bar.grid_columnconfigure(0, weight=1)
        self._status = ctk.CTkLabel(bar, text="", font=theme.font(12), text_color=theme.TEXT_MUTED)
        self._status.grid(row=0, column=0)
        self._version_label = ctk.CTkLabel(
            bar, text=f"v{__version__}", font=theme.font(11), text_color=theme.EDGE
        )
        self._version_label.grid(row=0, column=1, sticky="e")
        self._version_tip = add_tooltip(self._version_label, "")  # the update badge's

    # -- navigation --------------------------------------------------------------------------

    def _start(self) -> None:
        self._select_mode(Mode.SERIES)
        spawn(self._show_library_summary(), name="library-summary")
        spawn(
            self._services.library.run_periodic_sync(progress=self._on_sync_progress),
            name="library-sync",
        )
        # Its events arrive in _on_auto_event.
        spawn(self._services.auto_download.run_periodic(), name="auto-download")
        # Its problems arrive in _on_subtitle_event.
        spawn(self._services.subtitles.run_periodic(), name="subtitles")
        spawn(self._services.images.prune(), name="poster-prune")
        spawn(self._load_logo(), name="logo")
        spawn(self._note_version(), name="version-notice")
        spawn(self._services.updates.register_install(), name="installed-apps-entry")
        spawn(self._check_for_update(), name="update-check")
        spawn(self._set_up_prowlarr(), name="prowlarr-setup")
        spawn(self._first_run(), name="first-run")
        self.tray.start()

    def _check_now(self) -> None:
        """The tray's "Check for new episodes now": refresh the library, then auto-download."""

        async def check() -> None:
            await self._services.library.sync(progress=self._on_sync_progress)
            await self._services.auto_download.run_once()

        spawn(check(), name="check-now")

    def _on_auto_event(self, event: AutoEvent) -> None:
        """Tell the user what the automatic downloads did: a tray notification, plus the status
        bar for a download (an episode or a watchlist film); only the status bar when there is
        no tray icon."""
        if not self._auto_throttle.allow(event):
            log.info("Automatic-download error not shown again within the hour")
            return
        notice = auto_notice(event)
        tray_running = self.tray.running
        if tray_running:
            self.tray.notify(notice.title, notice.body)
        if not tray_running or isinstance(event, (AutoDownloaded, AutoMovieDownloaded)):
            self._report(notice.status, _AUTO_STATUS_MS)

    def _on_subtitle_event(self, event: SubtitleEvent) -> None:
        """A subtitle problem (bad key, wrong password, folder out of reach, downloads used
        up) shows in the status bar, in plain words."""
        if isinstance(event, SubtitleProblem):
            self._report(subtitle_status_text(event), _SUBTITLE_STATUS_MS)

    def _on_subtitles_saved(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe `listener` to "subtitles were saved" (the show windows' CC marks)."""

        def on_event(event: SubtitleEvent) -> None:
            if isinstance(event, SubtitlesSaved):
                listener()

        return self._services.subtitles.subscribe(on_event)

    async def _subtitle_marks(self, show_id: int) -> dict[tuple[int, int], str]:
        """Per (season, episode), the show window's "CC EN NL" mark."""
        found = await self._services.subtitles.show_languages(show_id)
        order = self._services.settings.current.subtitle_languages
        return {key: subtitle_mark(languages, order) for key, languages in found.items()}

    async def _load_logo(self) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        logo = await asyncio.to_thread(load_logo, round(_LOGO_HEIGHT * scaling))
        if logo is None or not self.winfo_exists():
            return
        width = round(logo.width / scaling)
        self._logo_image = ctk.CTkImage(light_image=logo, dark_image=logo, size=(width, _LOGO_HEIGHT))
        self._logo.configure(image=self._logo_image, text="")

    def _select_mode(self, mode: Mode) -> None:
        if mode is self._mode:
            return
        self._mode = mode
        for button_mode, button in self._mode_buttons.items():
            button.configure(fg_color=theme.ACCENT if button_mode is mode else "transparent")
        if mode is Mode.SERIES:
            self._movie_view.grid_remove()
            self._series_tabs.grid(row=1, column=0, sticky="nsew", padx=15, pady=5)
        else:
            self._series_tabs.grid_remove()
            self._movie_view.grid(row=1, column=0, sticky="nsew", padx=15, pady=5)
        self._visible_view().refresh_if_stale()

    def _on_tab_changed(self) -> None:
        self._visible_view().refresh_if_stale()

    def _visible_view(self) -> BaseView:
        if self._mode is Mode.MOVIES:
            return self._movie_view
        return self._series_views[self._series_tabs.get()]

    def _on_settings_changed(self, updated: Settings) -> None:
        previous, self._settings_seen = self._settings_seen, updated
        if settings_affect_pages(previous, updated):
            self._reload_pages()

    def _reload_pages(self) -> None:
        """Settings or library changed: every page is outdated; reload the one on screen now."""
        for view in (*self._series_views.values(), self._movie_view):
            view.invalidate()
        self._visible_view().refresh()

    def _on_search(self) -> None:
        self._suggestions.hide()
        query = self._search.get().strip()
        self._search.delete(0, "end")
        if query:
            self._open_search_results(query)

    def _open_search_results(self, query: str) -> None:
        """One results window at a time: a new search replaces the previous window."""
        self._suggestions.hide()
        if self._results_window is not None and self._results_window.winfo_exists():
            self._results_window.destroy()
        self._results_window = SearchResultsWindow(
            self,
            library=self._services.library,
            images=self._services.images,
            discovery=self._services.discovery,
            status=self.set_status,
            query=query,
            movie_actions=self._movie_actions,
            watchlist=self._services.watchlist,
            on_open_settings=self._open_settings,
            on_open_show=self._show_windows.open_show,
            card_scale=self._card_scale(),
        )

    def _search_episode(self, entry: CalendarEntry) -> None:
        # A special (no episode number) opens on its season's pack.
        episode = entry.episode
        self._open_indexer_dialog(TvTarget(entry.show, episode.season, episode.number))

    def _grab_movie(self, movie: Movie) -> None:
        self._open_indexer_dialog(MovieTarget(movie))

    # -- one-click downloads -------------------------------------------------------------------

    def _download_episode(self, entry: CalendarEntry) -> None:
        show, episode = entry.show, entry.episode
        if episode.number is None:  # a special: nothing to download by (its card says Search)
            self._search_episode(entry)
            return
        season, number = episode.season, episode.number
        request = self._services.search.episode_request(show, season, number)
        target = GrabTarget(
            media_type=MediaType.TV, media_id=show.id, season=season, episode=number
        )
        self._start_download(
            episode_key(show.id, season, number),
            f"{show.name} {episode.code}",
            lambda: self._services.quick_download.download(request, target),
        )

    def _download_movie(self, movie: Movie) -> None:
        async def download() -> QuickDownloadResult:
            details = await self._movie_details(movie)
            request = self._services.search.movie_request(movie, details)
            target = GrabTarget(media_type=MediaType.MOVIE, media_id=movie.id)
            return await self._services.quick_download.download(request, target)

        self._start_download(movie_key(movie.id), movie_title(movie), download)

    async def _movie_details(self, movie: Movie) -> MovieDetails | None:
        """The film's TMDB details (for its IMDb id), as the search window waits for them."""
        try:
            async with asyncio.timeout(_DETAILS_WAIT_S):
                return await self._services.discovery.movie_details(movie.id)
        except TMDBError as exc:
            log.info("No TMDB details for %s (TMDB id %d): %s", movie.title, movie.id, exc)
        except TimeoutError:
            log.info("No TMDB details for %s (TMDB id %d) in time", movie.title, movie.id)
        return None

    def _start_download(
        self,
        key: DownloadKey,
        label: str,
        download: Callable[[], Awaitable[QuickDownloadResult]],
    ) -> None:
        """Run a one-click download for `key`, unless one is running already."""
        if self._download_states.is_working(key):
            return
        self._download_states.set(key, RunState(Phase.WORKING))
        task = spawn(self._run_download(key, label, download), name="quick-download")
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _run_download(
        self,
        key: DownloadKey,
        label: str,
        download: Callable[[], Awaitable[QuickDownloadResult]],
    ) -> None:
        # The cards may be gone or rebuilt by now: DownloadStates repaints whichever exist.
        try:
            result = await download()
        except (GrabError, DownloadStationError) as exc:
            self._download_states.set(key, None)
            self._report(str(exc), _DOWNLOAD_STATUS_MS)
            return
        except BaseException:
            self._download_states.set(key, None)
            raise  # cancelled, or logged and reported by the task runner
        if result.already_downloaded:  # e.g. grabbed meanwhile from another window
            self._download_states.set(key, RunState(Phase.DONE))
            self._report(already_downloaded_text(label), _DOWNLOAD_STATUS_MS)
            return
        if result.grab is None:
            self._download_states.set(key, RunState(Phase.NOTHING))
            self._report(nothing_found_text(label), _DOWNLOAD_STATUS_MS)
            return
        self._download_states.set(key, RunState(Phase.DONE, result.grab.outcome))
        self._report(download_status_text(label, result), _DOWNLOAD_STATUS_MS)

    # -- missing episodes ----------------------------------------------------------------------

    def _download_missing(self, show: Show) -> None:
        """Start "Download missing" for a tracked show, unless its run is going already."""
        runs = self._missing_runs
        if runs.is_working(show.id) or self._services.missing.is_running(show.id):
            return
        runs.set(show.id, MissingRunState())
        task = spawn(self._run_missing(show), name="download-missing")
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _run_missing(self, show: Show) -> None:
        # The buttons may be gone or rebuilt by now: MissingRuns repaints whichever exist.
        runs = self._missing_runs

        def on_progress(step: MissingProgress) -> None:
            runs.set(show.id, MissingRunState(index=step.index, total=step.total))
            self._missing_step(show, step)

        try:
            report = await self._services.missing.download_missing(show.id, on_progress)
        except MissingRunBusy:
            return  # that run's state is the one shown
        except BaseException:
            runs.set(show.id, None)
            raise  # cancelled, or logged and reported by the task runner
        state = MissingRunState(index=report.total, total=report.total, report=report)
        runs.set(show.id, state)
        self._report(outcome_status_text(show.name, report), _DOWNLOAD_STATUS_MS)
        self.after(OUTCOME_MS, lambda: runs.expire(show.id, state))

    def _missing_step(self, show: Show, step: MissingProgress) -> None:
        """The episode's own Download row follows the run (show details window)."""
        episode, result, states = step.episode, step.result, self._download_states
        key = episode_key(show.id, episode.season, episode.number)
        if step.failed:
            states.set(key, None)
        elif result is None:
            states.set(key, RunState(Phase.WORKING))
        elif result.already_downloaded:
            current = states.get(key)
            if current is None or current.phase is not Phase.DONE:  # keep "✓ Saved" and co
                states.set(key, RunState(Phase.DONE))
        elif result.grab is None:
            states.set(key, RunState(Phase.NOTHING))
        else:
            states.set(key, RunState(Phase.DONE, result.grab.outcome))

    def _mark_have(self, show: Show, episode_ids: tuple[int, ...]) -> None:
        """ "I already have this": the episodes no longer count as missing."""
        spawn(self._set_have(show, episode_ids, have=True), name="mark-have")

    def _unmark_have(self, show: Show, episode_ids: tuple[int, ...]) -> None:
        """ "Mark as missing again"."""
        spawn(self._set_have(show, episode_ids, have=False), name="unmark-have")

    async def _set_have(self, show: Show, episode_ids: tuple[int, ...], *, have: bool) -> None:
        library = self._services.library
        if have:
            await library.mark_have(episode_ids)
        else:
            await library.unmark_have(episode_ids)
        # The library's listeners reload the pages and the show windows.
        self._report(have_status_text(show.name, len(episode_ids), have=have), 6000)

    def _report(self, text: str, clear_after_ms: int) -> None:
        try:
            self.set_status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # window already destroyed

    def _open_indexer_dialog(self, target: IndexerTarget) -> None:
        """One indexer dialog at a time: a new one replaces the previous dialog."""
        if self._indexer_dialog is not None and self._indexer_dialog.winfo_exists():
            self._indexer_dialog.destroy()
        services = self._services
        self._indexer_dialog = IndexerDialog(
            self,
            target=target,
            search=services.search,
            downloads=services.downloads,
            library=services.library,
            discovery=services.discovery,
            images=services.images,
            settings=services.settings,
            status=self.set_status,
            on_reopen=self._open_indexer_dialog,
        )

    def _on_library_changed(self) -> None:
        self._reload_pages()
        # Also replaces a lingering "Syncing library…" message (unless a timed one is showing).
        spawn(self._show_library_summary(), name="library-summary")

    def _open_settings(self) -> None:
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            self._settings_dialog.lift()
            self._settings_dialog.focus_force()
            return
        self._settings_dialog = SettingsDialog(
            self,
            self._services.settings,
            search=self._services.search,
            station=self._services.station,
            updates=self._services.updates,
            torrent_app=self._services.torrent_app,
            discovery=self._services.discovery,
            prowlarr=self._services.prowlarr_setup,
            update_info=self._update_info,
            on_update=self._start_update,
            subtitles=self._services.subtitles,
        )

    async def _set_up_prowlarr(self) -> None:
        message = prowlarr_setup_message(await self._services.prowlarr_setup.run())
        if message and self.winfo_exists():
            self.set_status(message, clear_after_ms=10_000)

    async def _first_run(self) -> None:
        """The welcome for a new library; a library from before it just records it as done."""
        settings = self._services.settings
        if settings.current.setup_done:
            return
        stats = await self._services.library.stats()
        action = first_run_action(settings.current.setup_done, stats.tracked_shows)
        if action is FirstRun.MARK_DONE:
            await settings.update(setup_done=True)
        elif action is FirstRun.WELCOME and self.winfo_exists():
            # A key saved there reloads the TMDB pages through _on_settings_changed.
            WelcomeDialog(
                self,
                settings=settings,
                torrent_app=self._services.torrent_app,
                discovery=self._services.discovery,
                airdates=self._services.airdates,
                library=self._services.library,
                remote=self._services.remote,
            )

    # -- updates -----------------------------------------------------------------------------

    async def _check_for_update(self) -> None:
        await asyncio.sleep(_UPDATE_CHECK_DELAY_S)
        try:
            info = await self._services.updates.check()
        except UpdateError as exc:
            log.info("Update check: %s", exc)
            return
        self._update_info = info
        if not info.available or not self.winfo_exists():
            return
        if not info.installable:
            self._version_label.configure(text=f"v{__version__} · v{info.latest} on GitHub")
        elif self._services.settings.current.auto_update:
            await self._stage_update(info)
        else:
            self._show_update_badge(
                f"⬆ Update to v{info.latest}", UPDATE_TIP, self._open_settings
            )

    async def _stage_update(self, info: UpdateInfo) -> None:
        """Download the update in the background; main.py installs it when AirGrabber closes."""
        try:
            version = await self._services.updates.stage()
        except UpdateError as exc:
            log.warning("Downloading the update failed: %s", exc)
            if self.winfo_exists():  # Settings can still install it
                self._show_update_badge(
                f"⬆ Update to v{info.latest}", UPDATE_TIP, self._open_settings
            )
            return
        if not self.winfo_exists() or self._update_task is not None:
            return  # closing, or Settings' "Update to …" is installing it right now
        self._show_update_badge("⬆ Restart to update", RESTART_TIP, self.restart)
        self.set_status(
            f"AirGrabber {version} is ready. It installs when you close AirGrabber.",
            clear_after_ms=_UPDATE_STATUS_MS,
        )

    def _show_update_badge(self, text: str, tip: str, on_click: Callable[[], None]) -> None:
        self._version_label.configure(
            text=text,
            text_color=theme.SECTION_NEW,
            font=theme.font(11, underline=True),
            cursor="hand2",
        )
        self._version_label.bind("<Button-1>", lambda _event: on_click())
        self._version_tip.set_text(tip)

    async def _note_version(self) -> None:
        """Show "Updated to v…" on the first start after an update; then remember this version."""
        settings = self._services.settings
        notice = update_notice(settings.current.last_run_version, __version__)
        if notice is not None:
            self.set_status(notice, clear_after_ms=_UPDATE_STATUS_MS)
        await settings.update(last_run_version=__version__)

    def _start_update(self) -> None:
        if self._update_task is not None and not self._update_task.done():
            return
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            self._settings_dialog.destroy()
        self._settings_dialog = None
        self._update_task = spawn(self._run_update(), name="update")

    async def _run_update(self) -> None:
        try:
            version = await self._services.updates.install(progress=self.set_status)
        except UpdateError as exc:
            log.warning("Update failed: %s", exc)
            self.set_status(f"⚠ Update failed: {exc}", clear_after_ms=15_000)
            return
        self.set_status(f"Updated to v{version}. Restarting…")
        self.after(1200, self.restart)

    def restart(self) -> None:
        """Close, and have main.py start a fresh copy once everything is shut down."""
        self.restart_requested = True
        self.destroy()

    # -- status bar --------------------------------------------------------------------------

    async def _show_library_summary(self) -> None:
        stats = await self._services.library.stats()
        synced = (
            f"last synced {stats.last_synced.astimezone():%d %b %H:%M}"
            if stats.last_synced
            else "not synced yet"
        )
        self._idle_status = (
            f"{stats.tracked_shows} tracked shows · {stats.episodes:,} episodes cached · {synced}"
        )
        if self._status_job is None:
            self._status.configure(text=self._idle_status)

    def set_status(self, text: str, *, clear_after_ms: int | None = None) -> None:
        """Show a message; with `clear_after_ms`, fall back to the library summary afterwards."""
        if self._status_job is not None:
            self.after_cancel(self._status_job)
            self._status_job = None
        self._status.configure(text=text)
        if clear_after_ms is not None:
            self._status_job = self.after(clear_after_ms, self._restore_status)

    def _on_sync_progress(self, p: SyncProgress) -> None:
        try:
            self.set_status(
                f"Syncing library… {p.done}/{p.total}" + (f" · {p.current}" if p.current else "")
            )
        except tk.TclError:
            pass  # window already destroyed

    def _restore_status(self) -> None:
        self._status_job = None
        self._status.configure(text=self._idle_status)

    # -- error reporting ---------------------------------------------------------------------

    def _report_task_error(self, error: BaseException) -> None:
        try:
            self.set_status(f"⚠ {type(error).__name__}: {error}", clear_after_ms=10_000)
        except tk.TclError:
            pass  # window already destroyed

    def report_callback_exception(
        self,
        exc: type[BaseException],
        val: BaseException,
        tb: TracebackType | None,
    ) -> None:
        """Errors raised inside synchronous Tk callbacks (Tk would otherwise print them)."""
        log.error("Unhandled error in UI callback", exc_info=(exc, val, tb))
        self._report_task_error(val)
