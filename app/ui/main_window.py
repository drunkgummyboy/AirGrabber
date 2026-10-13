"""The root window: header (mode switch, search, settings), page area and status bar."""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Awaitable, Callable
from enum import StrEnum
from types import TracebackType

import customtkinter as ctk
from async_tkinter_loop.mixins import AsyncCTk

from app import APP_NAME, __version__
from app.models import (
    CalendarEntry,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    ProwlarrSetupResult,
    QuickDownloadResult,
    Settings,
    SyncProgress,
    UpdateInfo,
)
from app.services import Services, UpdateError
from app.services.downloads import GrabError
from app.services.synology import DownloadStationError
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.branding import load_logo, set_window_icon
from app.ui.download_control import (
    DownloadActions,
    DownloadKey,
    DownloadStates,
    Phase,
    RunState,
    download_status_text,
    episode_key,
    movie_key,
    nothing_found_text,
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
from app.ui.tasks import on_task_error, spawn
from app.ui.tooltip import add_tooltip
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
        "ds_url",
        "ds_username",
        "ds_password",
        "ds_verify_tls",
        "ds_destination",
        "download_mode",
        "setup_done",
        "auto_update",
        "last_run_version",
    }
)


def settings_affect_pages(previous: Settings, updated: Settings) -> bool:
    """Whether a settings change touches anything a page shows (see _OFF_PAGE_SETTINGS)."""
    return any(
        getattr(previous, name) != getattr(updated, name)
        for name in Settings.model_fields
        if name not in _OFF_PAGE_SETTINGS
    )


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


class Mode(StrEnum):
    SERIES = "Series"
    MOVIES = "Movies"


class MainWindow(AsyncCTk, ctk.CTk):
    """Owns layout and navigation only; every page fetches its own data through services."""

    def __init__(self, services: Services) -> None:
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

    def async_mainloop(self, event_loop: asyncio.AbstractEventLoop | None = None) -> None:
        loop = event_loop or asyncio.new_event_loop()
        # Not after_idle(): CTk pumps Tk events (update()) before the asyncio loop is running,
        # and nothing may spawn coroutines until it is.
        loop.call_soon(self._start)
        super().async_mainloop(event_loop=loop)

    def destroy(self) -> None:
        self._suggestions.destroy()
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
            ),
            "Tracked": TrackedView(
                tabs.add("Tracked"), library=library, images=images, status=self.set_status
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
        )

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
        spawn(self._services.images.prune(), name="poster-prune")
        spawn(self._load_logo(), name="logo")
        spawn(self._note_version(), name="version-notice")
        spawn(self._services.updates.register_install(), name="installed-apps-entry")
        spawn(self._check_for_update(), name="update-check")
        spawn(self._set_up_prowlarr(), name="prowlarr-setup")
        spawn(self._first_run(), name="first-run")

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
            on_open_settings=self._open_settings,
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
        if result.grab is None:
            self._download_states.set(key, RunState(Phase.NOTHING))
            self._report(nothing_found_text(label), _DOWNLOAD_STATUS_MS)
            return
        self._download_states.set(key, RunState(Phase.DONE, result.grab.outcome))
        self._report(download_status_text(label, result), _DOWNLOAD_STATUS_MS)

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
            update_info=self._update_info,
            on_update=self._start_update,
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
