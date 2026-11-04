"""The root window: the spotlight band (search, Downloads, settings, the name button, tonight's
episodes), the tabs row (the Series tabs on the left, the Series / Movies / MediaForge switch on
the right), page area and status bar.

MediaForge is a page here (`Mode.FORGE`; docs/forge/PAGE.md). It is built by its host
(app/forge/ui/host.py) at the first visit, into a frame of its own (`_forge_holder`) and the tabs
row's left part; while it shows the band has only its top row (`SpotlightBand.set_compact`). The
page isn't a BaseView: `_visible_view` is None on it and a change of AirGrabber's settings or
library leaves it alone.

People (app/services/profiles.py): the band's name button opens the people menu
(app/ui/people_dialogs.py). When another person is selected, every info window and the search
results close (they were opened for the person before: none of their buttons may follow a show
or fill a watchlist for the wrong person; reloading each for the new person would take far more
than closing), the pages and the band reload, and the status bar says "Now showing …'s list"."""

from __future__ import annotations

import asyncio
import logging
import time
import tkinter as tk
from collections.abc import Callable, Sequence
from enum import StrEnum
from types import TracebackType
from typing import TYPE_CHECKING, Any, NamedTuple

import customtkinter as ctk
from async_tkinter_loop.mixins import AsyncCTk

from app import APP_NAME, __version__
from app.i18n import N_, month_short, number, t
from app.models import (
    AutoDownloaded,
    AutoError,
    AutoEvent,
    AutoGaveUp,
    AutoMovieDownloaded,
    AutoMovieGaveUp,
    CalendarEntry,
    DownloadEntry,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    ProwlarrSetupResult,
    QueueEvent,
    SearchRequest,
    Settings,
    Show,
    SpotlightItem,
    SpotlightSide,
    SyncProgress,
    UpdateInfo,
)
from app.services import Services, UpdateError
from app.services.download_queue import QueuedDownload
from app.services.profiles import ProfileChange
from app.services.missing import MAX_PER_RUN
from app.services.auto_download import GIVE_UP_AFTER
from app.services.watchlist import GIVE_UP_AFTER as MOVIE_GIVE_UP_AFTER
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.card_scale import CardScale
from app.ui.download_control import (
    BatchKey,
    BatchState,
    DownloadActions,
    DownloadStates,
)
from app.ui.get_started import GetStartedDialog, get_started_due
from app.ui.missing_control import (
    OUTCOME_MS,
    MissingActions,
    MissingRuns,
    MissingRunState,
    outcome_status_text,
    queued_run_report,
)
from app.ui.movie_details import MovieWindows, movie_title
from app.ui.people import person_initial, person_name, switched_text
from app.ui.people_dialogs import PeopleActions
from app.ui.queue_link import QueueLink
from app.ui.release_panel import PanelServices, TvTarget
from app.forge.ui.host import ForgeHost
from app.single_instance import ForgeRequest
from app.ui.forge_link import Mailbox, QuitGate, RequestPump, quits_after_forge
from app.ui.search import SearchResultsWindow, SearchSuggestions
from app.ui.settings_dialog import SettingsDialog
from app.ui.show_details import ShowWindows
from app.ui.spotlight import PAD_X, SWITCH_MARGIN, SwitchPart
from app.ui.spotlight_band import ModeSwitch, SpotlightBand
from app.ui.tasks import on_task_error, spawn
from app.ui.tooltip import add_tooltip
from app.ui.torrent_app_status import QBITTORRENT
from app.ui.tray import TrayHost
from app.ui.views import BaseView, CalendarView, DiscoverView, MovieView, TrackedView
from app.ui.views.downloads_view import DownloadsView, PageActions
from app.ui.welcome_dialog import FirstRun, WelcomeDialog, first_run_action, first_run_defaults
from app.ui.whats_new_dialog import WhatsNewDialog
from app.ui.widgets import Tabview, install_edit_menus, install_window_release
from app.whats_new import WhatsNew, whats_new

if TYPE_CHECKING:
    from pathlib import Path

    from app.forge.old_install import Outcome
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

_UPDATE_CHECK_DELAY_S = 5  # let the pages load first
_DOWNLOAD_STATUS_MS = 10_000  # how long a one-click download's result stays in the status bar
_DETAILS_WAIT_S = 5.0  # a one-click movie download waits this long for TMDB details (IMDb id)
_UPDATE_STATUS_MS = 10_000  # how long "… is ready" / "Updated to …" stay in the status bar
_FORGE_START_DELAY_S = 15  # MediaForge starts this long after the window: not on AirGrabber's start
# The update badges' hover tips.
UPDATE_TIP = N_("Open Settings to update")
RESTART_TIP = N_("Installs the downloaded update and restarts AirGrabber")
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
        "movie_remote_destination",
        "download_mode",
        "auto_download_new_shows",
        "setup_done",
        "get_started_shown",
        "auto_update",
        "last_run_version",
        "keep_in_tray",
        "tray_notice_shown",
        "start_with_windows",
        # MediaForge's subtitles (its Settings page stores them here), and the folder box.
        "subtitles_enabled",
        "subtitle_languages",
        "subtitles_folder",
        "subtitles_mode",
        "subtitles_hearing_impaired",
        "subtitles_for",
        "opensubtitles_username",
        "opensubtitles_password",
        "opensubtitles_own_key",
        # My shows' sort and filter: that page re-arranges its own cards.
        "tracked_sort",
        "tracked_filter",
        # Discover's and Movies' "Hide what I already have": the page re-filters itself.
        "hide_owned_discover",
        "hide_owned_movies",
        # Light / dark: every widget repaints itself (theme.apply_appearance).
        "theme_mode",
        # The language: it takes effect at the next start (Settings offers "Restart now").
        "language",
        # The info window's layout: a window reads it when it opens.
        "info_layout",
        # The spotlight band turning by itself: the band follows it (`set_motion`).
        "spotlight_motion",
        # The accent colour: applied at the next start (main.py), like the language.
        "accent",
        # Who is selected: a switch reloads the pages itself (_on_profiles_changed), once that
        # person's watchlist is read.
        "current_profile",
        # Plex's link: connecting, disconnecting and a check that changes what counts as had
        # reload the pages themselves (through the library's listeners, app/services/plex.py).
        "plex_client_id",
        "plex_token",
        "plex_server_id",
        "plex_server_name",
        "plex_server_uri",
        "plex_sections_off",
    }
)


def settings_affect_pages(previous: Settings, updated: Settings) -> bool:
    """Whether a settings change touches anything a page shows (see _OFF_PAGE_SETTINGS)."""
    return any(
        getattr(previous, name) != getattr(updated, name)
        for name in Settings.model_fields
        if name not in _OFF_PAGE_SETTINGS
    )


# Widget classes the user types in: F5 leaves them alone (Ctrl+F still goes to the search box).
_TEXT_INPUT_CLASSES = frozenset({"Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox"})


def typing_in(widget: Any) -> bool:
    """Whether `widget` (a key event's) is a text field the user may be typing in."""
    try:
        return str(widget.winfo_class()) in _TEXT_INPUT_CLASSES
    except (AttributeError, tk.TclError):
        return False  # no widget tkinter knows


def prowlarr_setup_message(result: ProwlarrSetupResult) -> str | None:
    """The status-bar text for what the automatic Prowlarr setup did; None when it did nothing."""
    added = ", ".join(result.added)
    if result.connected and added:
        return t("Connected to Prowlarr on this PC · added {sites} to your sites", sites=added)
    if result.connected:
        return t("Connected to Prowlarr on this PC")
    if added:
        return t("Prowlarr: added {sites} to your sites", sites=added)
    return None


def update_notice(last_run_version: str, running: str) -> str | None:
    """The status-bar text after an update: the version that ran last differs from this one."""
    if last_run_version and last_run_version != running:
        return t("Updated to v{version}", version=running)
    return None


_SWITCHED_STATUS_MS = 6_000  # "Now showing …'s list" after a switch
_AUTO_STATUS_MS = 10_000  # how long an automatic download's line stays in the status bar
_OLD_FORGE_STATUS_MS = 15_000  # the old MediaForge's notice and its removal's outcome
_FORGE_STATUS_MS = 10_000  # what MediaForge's queue says (a run's summary, "Put back 1 file")
AUTO_ERROR_INTERVAL_S = 3600.0  # at most one "Automatic download didn't work" per hour


class AutoNotice(NamedTuple):
    """How an automatic-download event is told: a tray notification (title, body) and the
    status-bar line (`status`), for 10 s. Without a tray icon, only the status bar."""

    title: str
    body: str
    status: str


def auto_notice(event: AutoEvent) -> AutoNotice:
    if isinstance(event, AutoDownloaded):
        body = (
            t("{quality} · picked automatically", quality=event.quality)
            if event.quality
            else t("Picked automatically")
        )
        return AutoNotice(
            title=t("{label} is downloading", label=event.label),
            body=body,
            status=t("{label} · downloading (picked automatically)", label=event.label),
        )
    if isinstance(event, AutoGaveUp):
        title = t("Couldn't find {label}", label=event.label)
        body = t(
            "No good download turned up in {days} days. Choose one yourself from the calendar.",
            days=GIVE_UP_AFTER.days,
        )
        return AutoNotice(title=title, body=body, status=f"{title}. {body}")
    if isinstance(event, AutoMovieDownloaded):
        how = t("picked automatically from your watchlist")
        return AutoNotice(
            title=t("Downloaded {title}", title=event.title),
            body=f"{event.quality} · {how}" if event.quality else how.capitalize(),
            status=t("Downloaded {title} ({how})", title=event.title, how=how),
        )
    if isinstance(event, AutoMovieGaveUp):
        title = t("Couldn't find {label}", label=event.title)
        body = t(
            "No good download turned up in {days} days. "
            "Choose one yourself from your watchlist on the Movies page.",
            days=MOVIE_GIVE_UP_AFTER.days,
        )
        return AutoNotice(title=title, body=body, status=f"{title}. {body}")
    title = t("Automatic download didn't work")
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
    DOWNLOADS = "Downloads"  # the band's ⬇ button; the switch leads back
    FORGE = "MediaForge"  # the switch's third part: MediaForge's page


# The switch's parts and their modes (the Downloads page lights none), and whose titles the band
# shows.
_PART_MODES = {
    SwitchPart.SERIES: Mode.SERIES,
    SwitchPart.MOVIES: Mode.MOVIES,
    SwitchPart.FORGE: Mode.FORGE,
}
_MODE_SIDES = {Mode.SERIES: SpotlightSide.SERIES, Mode.MOVIES: SpotlightSide.MOVIES}
# The Series tabs' texts. CTkTabview names a tab by its text, so they key the views translated.
_CALENDAR, _DISCOVER, _TRACKED = N_("Calendar"), N_("Discover"), N_("Tracked")


def build_tabs_row(
    master: Any, *, on_part: Callable[[SwitchPart], None]
) -> tuple[ctk.CTkFrame, ModeSwitch]:
    """The tabs row under the band (its owner grids it): the Series tabs on the left
    (`show_tabs`) or MediaForge's buttons and status there, the switch at the right end, its pill
    level with the band's settings button (PAD_X from the edge). The switch shows in every mode,
    so it never moves; it is the row's tallest part, so the row keeps its height while the tabs
    are hidden (Movies, Downloads)."""
    row = ctk.CTkFrame(master, fg_color="transparent", corner_radius=0)
    row.grid_columnconfigure(1, weight=1)
    switch = ModeSwitch(row, on_part=on_part)
    # A plain canvas: Tk's grid doesn't scale its padding as CTk's widgets' does.
    scale = ctk.ScalingTracker.get_widget_scaling(row)
    switch.grid(row=0, column=2, sticky="e", padx=(0, round((PAD_X - SWITCH_MARGIN) * scale)))
    return row, switch


def show_tabs(pills: ctk.CTkFrame, shown: bool) -> None:
    """The Series tabs on the tabs row's left (level with the band's poster), or hidden."""
    if shown:
        pills.grid(row=0, column=0, sticky="w", padx=(15, 0))
    else:
        pills.grid_remove()


class MainWindow(AsyncCTk, ctk.CTk):
    """Owns layout and navigation only; every page fetches its own data through services."""

    def __init__(
        self,
        services: Services,
        *,
        start_hidden: bool = False,
        show_requests: Callable[[], bool] | None = None,
        forge_mailbox: Mailbox | None = None,
        forge_request: ForgeRequest | None = None,
    ) -> None:
        """`forge_mailbox`: where later starts hand MediaForge requests over (main.py's
        InstanceLock). `forge_request`: the one this start carries (the right-click menu, the
        "MediaForge" shortcut): the window starts hidden; a plain request shows it on MediaForge's
        page, a confirm request only its confirm window (D5: AirGrabber quits or stays in the
        tray when that closes, unless the window was shown meanwhile)."""
        theme.apply_appearance(services.settings.current.theme_mode)
        super().__init__(fg_color=theme.BG)
        self._services = services
        # The name button's menu and its dialogs (add, rename or recolour, remove a person).
        self._people = PeopleActions(self, services.profiles)
        self._mode: Mode | None = None
        self._idle_status = ""
        self._status_job: str | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._get_started: GetStartedDialog | None = None  # "Let's get started"
        self._results_window: SearchResultsWindow | None = None
        self._update_info: UpdateInfo | None = None
        self._update_task: asyncio.Task[None] | None = None
        self.restart_requested = False  # read by main.py once the window has closed
        # One-click downloads: their state per title, shared by every card that shows it.
        # The Download buttons' hover tip says what a download looks for (Settings → Quality).
        self._download_states = DownloadStates(settings=lambda: services.settings.current)
        # They run in the app's download queue (no window owns them); the link shows its
        # states on the buttons ("Queued", working, the outcome) and reports the outcomes.
        self._queue_link = QueueLink(
            services.download_queue,
            self._download_states,
            report=lambda text: self._report(text, _DOWNLOAD_STATUS_MS),
        )
        self._download_tasks: set[asyncio.Task[None]] = set()
        self._episode_actions: DownloadActions[CalendarEntry] = DownloadActions(
            download=self._download_episode,
            choose=self._search_episode,
            states=self._download_states,
            download_all=self._download_episodes,  # calendar stacks, the show window's seasons
            download_season=self._download_season,  # the show window's "⬇ Whole season"
        )
        self._movie_actions: DownloadActions[Movie] = DownloadActions(
            download=self._download_movie,
            choose=self._grab_movie,
            states=self._download_states,
        )
        # "Download missing" (with Plex): its run state per show, shared by every button that
        # shows it.
        self._missing_runs = MissingRuns()
        self._missing_actions = MissingActions(
            download=self._download_missing, runs=self._missing_runs
        )
        # What the info windows' releases panels work with ("☰ Choose…" shows them there).
        releases = PanelServices(
            search=services.search,
            downloads=services.downloads,
            library=services.library,
            discovery=services.discovery,
            settings=services.settings,
            status=self.set_status,
        )
        # The show details windows (one per show), opened from the show cards and from every
        # episode's "☰ Choose…" (on its releases).
        self._show_windows = ShowWindows(
            self,
            library=services.library,
            discovery=services.discovery,
            images=services.images,
            status=self.set_status,
            episode_actions=self._episode_actions,
            downloads_changed=services.downloads.subscribe,
            profile=lambda: services.profiles.current_id,  # a window is for the person then
            releases=releases,
        )
        # The film info windows (one per film), opened from the film cards and from every
        # film's "☰ Choose…" (on its releases).
        self._movie_windows = MovieWindows(
            self,
            images=services.images,
            discovery=services.discovery,
            watchlist=services.watchlist,
            actions=self._movie_actions,
            releases=releases,
            in_plex=self._film_in_plex,
            plex_changed=services.library.subscribe,  # a Plex check that changed something
        )

        self.title(t("{app} — Track your shows. Grab your movies.", app=APP_NAME))
        set_window_icon(self)
        self._place(1650, 900)
        self.minsize(1220, 640)  # narrowest width at which the band's top row still fits
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)  # the page

        self._build_header()
        self._build_pages()
        self._build_status_bar()
        self._bind_shortcuts()
        install_edit_menus(self)  # right-click Cut / Copy / Paste in every text field
        install_window_release(self)  # a closed window doesn't stay in customtkinter's tables
        self._whats_new_binding: str | None = None  # waiting for the window to open (tray start)

        on_task_error(self._report_task_error)
        self._settings_seen = services.settings.current
        self._unsubscribe_settings = services.settings.subscribe(self._on_settings_changed)
        self._unsubscribe_library = services.library.subscribe(self._on_library_changed)
        # A grab changes what is still missing (with Plex).
        self._unsubscribe_downloads = services.downloads.subscribe(self._reload_pages)
        # The automatic downloads: a tray notification (or the status bar) per event.
        self._auto_throttle = AutoNoticeThrottle()
        self._unsubscribe_auto = services.auto_download.subscribe(self._on_auto_event)
        # A loaded backup changed the library and the watchlist: reload the pages.
        self._unsubscribe_backup = services.backup.subscribe(self._on_library_changed)
        # People added, renamed or removed (the name button), or another person selected.
        self._unsubscribe_profiles = services.profiles.subscribe(self._on_profiles_changed)

        # MediaForge (Forge inside AirGrabber): its page, its confirm windows and the requests
        # later starts hand over (app/forge/ui/host.py, app/ui/forge_link.py). Forge itself
        # starts _FORGE_START_DELAY_S after the window, or when something needs it first.
        self._forge = ForgeHost(
            self,
            services.forge,
            page_master=self._forge_holder,
            tabs_row=self._tabs_row,
            show_page=self._show_forge_page,
            show_settings=self._open_settings,
            restart=self.restart,
        )
        self._forge.on_needs = self._on_forge_needs
        self._forge.notify = self._on_forge_notice
        self._forge.on_closed = self._on_forge_closed
        self._forge.on_view = self._on_forge_view
        self._forge.on_message = lambda text: self._report(text, _FORGE_STATUS_MS)
        # The old MediaForge (F-27): the import result's "Remove the old MediaForge"; what a
        # removal came to (from there or from Settings) shows in the status bar.
        from app.forge.old_install import remover_for

        self._old_remover = remover_for(services.forge)
        self._forge.on_remove_old = self._remove_old_mediaforge
        self._unsubscribe_old_remover = self._old_remover.subscribe(self._on_old_removal)
        self._started_for_forge = forge_request is not None
        self._forge_pump = RequestPump(forge_mailbox, self._forge.handle, start_request=forge_request)
        # The right-click menu is rewritten at every Forge start (presets, language, this copy).
        services.forge.when_ready(self._on_forge_ready)
        # Quit and restart first wait for a Forge run to finish its file, then close Forge.
        self._quit_gate = QuitGate(
            self._forge,
            close_forge=services.forge.aclose,
            finish=self._destroy_for_good,
            on_asked=self._forge_pump.stop,  # what arrives from now on waits in the folder
            on_leaving=self.withdraw,
        )

        # Closing the window keeps AirGrabber running in the tray; main.py stops the icon at exit.
        self.tray = TrayHost(
            self,
            settings=services.settings,
            on_check_now=self._check_now,
            show_requests=show_requests,  # a second start of AirGrabber asks for the window
            on_open_forge=self.open_forge,
            on_quit=self.quit_app,
            on_reopen=self._go_home,  # back from the tray: Series › Calendar
        )
        self.protocol("WM_DELETE_WINDOW", self._close_window)
        if start_hidden or self._started_for_forge:
            # main.py --tray: only the tray icon shows; a start for Forge: what its request shows.
            self.withdraw()

    def async_mainloop(self, event_loop: asyncio.AbstractEventLoop | None = None) -> None:
        loop = event_loop or asyncio.new_event_loop()
        # Not after_idle(): CTk pumps Tk events (update()) before the asyncio loop is running,
        # and nothing may spawn coroutines until it is.
        loop.call_soon(self._start)
        super().async_mainloop(event_loop=loop)

    def destroy(self) -> None:
        self._suggestions.destroy()
        self._show_windows.close()
        self._movie_windows.close()
        if self._results_window is not None and self._results_window.winfo_exists():
            self._results_window.destroy()
        self._results_window = None
        for task in list(self._download_tasks):
            task.cancel()
        self._queue_link.close()
        self._unsubscribe_settings()
        self._unsubscribe_library()
        self._unsubscribe_downloads()
        self._unsubscribe_auto()
        self._unsubscribe_backup()
        self._unsubscribe_profiles()
        self._unsubscribe_old_remover()
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
        """The spotlight band across the top, edge to edge (app/ui/spotlight_band.py)."""
        services = self._services
        self._band = SpotlightBand(
            self,
            spotlight=services.spotlight,
            downloads=services.download_status,
            images=services.images,
            motion=services.settings.current.spotlight_motion,
            on_downloads=lambda: self._select_mode(Mode.DOWNLOADS),
            on_history=self._toggle_forge_history,  # 🕘, on MediaForge's page
            on_settings=self._open_settings,
            on_search=self._on_search,
            on_open=self._open_spotlight,
            report=self._report,
            profile=lambda: services.profiles.current_id,  # the Series side's episodes
            on_person=self._open_people_menu,  # the name button
            on_home=self._go_home,  # the logo and "AirGrabber": Series › Calendar
        )
        self._band.grid(row=0, column=0, sticky="ew")
        self._show_person()
        self._tabs_row, self._switch = build_tabs_row(self, on_part=self._part_clicked)
        self._tabs_row.grid(row=1, column=0, sticky="ew")
        self._search = self._band.entry
        self._suggestions = SearchSuggestions(
            self._search,
            root=self,
            library=services.library,
            discovery=services.discovery,
            on_open=self._open_search_results,
            profile=lambda: services.profiles.current_id,
        )

    def _build_pages(self) -> None:
        library, settings = self._services.library, self._services.settings
        images, discovery = self._services.images, self._services.discovery
        # Pill tabs on the tabs row under the band (left-aligned, the chosen one filled with the
        # accent; level with the band's poster).
        self._series_tabs = Tabview(
            self, look="pills", pills_master=self._tabs_row, command=self._on_tab_changed
        )
        tabs = self._series_tabs
        self._series_views: dict[str, BaseView] = {
            t(_CALENDAR): CalendarView(
                tabs.add(t(_CALENDAR)),
                library=library,
                settings=settings,
                images=images,
                status=self.set_status,
                episode_actions=self._episode_actions,
                card_scale=self._card_scale,
                on_open_show=self._show_windows.open_show,
                on_pick_shows=self.open_get_started,  # the empty calendar's box
            ),
            t(_DISCOVER): DiscoverView(
                tabs.add(t(_DISCOVER)),
                discovery=discovery,
                images=images,
                status=self.set_status,
                settings=settings,
                on_open_settings=self._open_settings,
                on_open_show=self._show_windows.open_discover,
                card_scale=self._card_scale,
            ),
            t(_TRACKED): TrackedView(
                tabs.add(t(_TRACKED)),
                library=library,
                settings=settings,
                airdates=self._services.airdates,
                images=images,
                status=self.set_status,
                on_open_show=self._show_windows.open_show,
                missing_actions=self._missing_actions,
                card_scale=self._card_scale,
                on_pick_shows=self.open_get_started,  # "+ Pick shows…"
            ),
        }
        for view in self._series_views.values():
            view.pack(fill="both", expand=True)

        self._movie_view = MovieView(
            self,
            discovery=discovery,
            images=images,
            status=self.set_status,
            settings=settings,
            on_open_settings=self._open_settings,
            movie_actions=self._movie_actions,
            watchlist=self._services.watchlist,
            card_scale=self._card_scale,
            on_open_movie=self._movie_windows.open_movie,
        )
        self._downloads_view = DownloadsView(
            self,
            downloads=self._services.download_status,
            settings=settings,
            images=images,
            discovery=discovery,
            on_open_show=self._show_windows.open_show,
            on_open_film=self._movie_windows.open_movie_id,
            actions=PageActions(again=self._download_entry_again),
            queue=self._services.download_queue,  # its waiting downloads at the top
        )
        # MediaForge's page goes in here at its first visit (ForgeHost.open).
        self._forge_holder = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)

    def _card_scale(self) -> CardScale:
        """The cards' size as Settings has it now; the pages read it at every load."""
        return CardScale.of(self._services.settings.current.card_size)

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=15, pady=(0, 6))
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
        self._go_home()
        spawn(self._show_library_summary(), name="library-summary")
        spawn(
            self._services.library.run_periodic_sync(progress=self._on_sync_progress),
            name="library-sync",
        )
        # Its events arrive in _on_auto_event.
        spawn(self._services.auto_download.run_periodic(), name="auto-download")
        # What is in Plex: checked now, then every 30 minutes (a change reloads the pages).
        spawn(self._services.plex.run_periodic(), name="plex")
        # The Downloads page's progress and the header badge (they listen to it themselves).
        spawn(self._services.download_status.run_periodic(), name="download-status")
        spawn(self._services.images.prune(), name="poster-prune")
        self._band.start()  # its titles, its pictures and their turning
        spawn(self._note_version(), name="version-notice")
        spawn(self._services.updates.register_install(), name="installed-apps-entry")
        spawn(self._check_for_update(), name="update-check")
        spawn(self._set_up_prowlarr(), name="prowlarr-setup")
        spawn(self._first_run(), name="first-run")
        self.tray.start()
        spawn(self._start_forge_later(), name="forge-start")
        self._attach_drops()
        spawn(self._forge_pump.run(), name="forge-requests")  # this start's own request first

    # -- MediaForge -----------------------------------------------------------------------------

    async def _start_forge_later(self) -> None:
        """Forge starts a while after the window (INTEGRATION.md 3.6), unless something
        needed it earlier (`start` is shared and idempotent)."""
        await asyncio.sleep(_FORGE_START_DELAY_S)
        await self._services.forge.start()

    def open_forge(self) -> None:
        """The tray's Open MediaForge: a plain request, this window shown on MediaForge's page."""
        self._forge.handle(ForgeRequest())

    def _attach_drops(self) -> None:
        """Explorer drops anywhere on this window (a light ctypes module; the loop runs)."""
        from app.forge.ui.file_drops import attach_file_drops

        attach_file_drops(self, self._on_drop)

    def _on_drop(self, paths: Sequence[Path]) -> None:
        """A drop from Explorer goes to MediaForge's queue and shows its page, as a plain request
        with those files does (with Forge not started yet they wait for the page)."""
        self._forge.handle(ForgeRequest(paths=tuple(str(path) for path in paths)))

    def _part_clicked(self, part: SwitchPart) -> None:
        """A click on the switch; MediaForge's part also goes back to the queue."""
        mode = _PART_MODES[part]
        self._select_mode(mode)
        if mode is Mode.FORGE:
            self._forge.show_queue()

    def _show_forge_page(self) -> None:
        """A plain request: this window shown, un-minimised and raised, on MediaForge's queue."""
        self.tray.show_window()
        self._part_clicked(SwitchPart.FORGE)

    def _toggle_forge_history(self) -> None:
        """🕘: History or the queue, on MediaForge's page."""
        self._forge.toggle_history()

    def _on_forge_view(self, history: bool) -> None:
        """MediaForge's page shows History or the queue: 🕘 lit or not."""
        if self._mode is Mode.FORGE:
            self._band.set_history_lit(history)

    def _on_forge_ready(self, forge: ForgeServices) -> None:
        """Forge has started: rewrite the Explorer right-click menu if it is on and starts this
        copy (app/forge/context_menu.py never takes another copy's menu over)."""
        from app.forge import context_menu

        spawn(context_menu.refresh_from(forge), name="forge-menu-refresh")
        spawn(self._old_mediaforge_notice(forge), name="forge-old-notice")

    async def _old_mediaforge_notice(self, forge: ForgeServices) -> None:
        """Once: an old MediaForge's data waits to be brought over (app/forge/old_install.py)."""
        from app.forge.old_install import startup_notice

        text = await startup_notice(forge)
        if text is not None:
            self._report(text, _OLD_FORGE_STATUS_MS)

    def _remove_old_mediaforge(self) -> None:
        """The import result's "Remove the old MediaForge": its own uninstaller asks the owner."""
        spawn(self._old_remover.remove(), name="forge-remove-old")

    def _on_old_removal(self, outcome: Outcome | None) -> None:
        """What removing the old MediaForge came to, in the status bar (None: it started)."""
        from app.forge.old_install import WAITING_TEXT, outcome_text

        if outcome is None:
            self._report(t(WAITING_TEXT), _OLD_FORGE_STATUS_MS)
        else:
            self._report(outcome_text(outcome), _OLD_FORGE_STATUS_MS)

    def _on_forge_needs(self, count: int) -> None:
        """How many files need the owner (ForgeHost's count): the badge on MediaForge's word in
        the switch and the tray's."""
        self._switch.set_count(count)
        self.tray.set_forge_count(count)

    def _on_forge_notice(self, title: str, text: str) -> None:
        """The renamer's notification, from the tray icon."""
        self.tray.notify(title, text)

    def _on_forge_closed(self) -> None:
        """A confirm window closed. D5: a start for Forge whose main window never showed leaves
        when the last one closes, unless it stays in the tray (`quits_after_forge`)."""
        if self._quit_gate.asked:
            return
        current = self._services.settings.current
        try:
            hidden = self.state() == "withdrawn"
        except tk.TclError:
            return  # the window is gone already
        if quits_after_forge(
            started_for_forge=self._started_for_forge,
            forge_windows_open=self._forge.windows_open,
            main_hidden=hidden,
            keep_in_tray=current.keep_in_tray,
            tray_running=self.tray.running,
        ):
            log.info("The MediaForge confirm window this start was for closed: quitting")
            self.quit_app()

    def _close_window(self) -> None:
        """The window's ✕: Settings open over it goes first (= Cancel), or it would hide with it
        and keep the grab, leaving MediaForge's confirm windows dead; then into the tray or quit.
        On every page alike: a run on MediaForge's page goes on in the tray, and quitting waits
        for it (`quit_app`)."""
        dialog, self._settings_dialog = self._settings_dialog, None
        if dialog is not None:
            dialog.cancel()
        self.tray.close_window()

    def quit_app(self) -> None:
        """Quit for good (the tray's Quit, closing the window without the tray): a Forge run
        first finishes its current file, then Forge closes (bounded), then the window goes and
        main.py shuts the rest down."""
        self._quit_gate.request()

    def _destroy_for_good(self) -> None:
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass  # gone already

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

    async def _film_in_plex(self, movie_id: int, imdb_id: str | None) -> bool:
        """Whether a film is in Plex (its window's quiet ✓)."""
        return (await self._services.library.plex_films()).has(movie_id, imdb_id)

    def _select_mode(self, mode: Mode) -> None:
        if mode is self._mode:
            return
        leaving_forge = self._mode is Mode.FORGE
        self._mode = mode
        forge = mode is Mode.FORGE
        self._band.set_compact(forge)  # only the top row on MediaForge's page
        if not forge:
            self._band.select(_MODE_SIDES.get(mode))  # None: the Downloads page (⬇ lit)
        parts = {part_mode: part for part, part_mode in _PART_MODES.items()}
        self._switch.select(parts.get(mode))  # None: no part lit
        pills = self._series_tabs.pills
        if pills is not None:
            show_tabs(pills, mode is Mode.SERIES)
        pages: dict[Mode, ctk.CTkFrame] = {
            Mode.SERIES: self._series_tabs,
            Mode.MOVIES: self._movie_view,
            Mode.DOWNLOADS: self._downloads_view,
            Mode.FORGE: self._forge_holder,
        }
        for page_mode, page in pages.items():
            if page_mode is not mode:
                page.grid_remove()
        pages[mode].grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 5))
        if leaving_forge:
            self._forge.close()
        if forge:
            self._forge.show()  # the first visit builds it
        view = self._visible_view()
        if view is not None:
            view.refresh_if_stale()

    def _go_home(self) -> None:
        """The start page, Series › Calendar: at start and when a plain Open brings the window
        back from the tray (TrayHost.open_window), and a click on the band's logo. A window
        shown for a purpose (Settings, an update, a notification) keeps its page."""
        self._select_mode(Mode.SERIES)
        calendar = t(_CALENDAR)
        if self._series_tabs.get() != calendar:
            self._series_tabs.set(calendar)  # CTk's set() doesn't run the tab command
            self._on_tab_changed()

    def _on_tab_changed(self) -> None:
        view = self._visible_view()
        if view is not None:
            view.refresh_if_stale()

    # -- people --------------------------------------------------------------------------------

    def _show_person(self) -> None:
        """The selected person on the band's name button."""
        person = self._services.profiles.current
        self._band.set_person(
            person_name(person), person_initial(person), theme.person_colour(person.colour)
        )

    def _open_people_menu(self) -> None:
        """The name button: the people menu under it."""
        box = self._band.person_box()
        if box is not None:
            self._people.open_menu(self._band, box)

    def _on_profiles_changed(self, change: ProfileChange) -> None:
        """Someone was added, renamed or removed: the name button. Another person selected:
        the info windows and the search results close (see the module docstring), the pages
        and the band reload, and the status bar says whose list shows now."""
        try:
            self._show_person()
        except tk.TclError:
            return  # closing
        if change is not ProfileChange.SWITCHED:
            return
        self._show_windows.close_windows()
        self._movie_windows.close_windows()
        if self._results_window is not None and self._results_window.winfo_exists():
            self._results_window.destroy()
        self._results_window = None
        self._suggestions.hide()
        self._reload_pages()
        spawn(self._show_library_summary(), name="library-summary")
        self._report(switched_text(self._services.profiles.current), _SWITCHED_STATUS_MS)

    def _open_spotlight(self, item: SpotlightItem) -> None:
        """A click on the band's title: its info window (a followed show's, a TMDB show's
        through its TVMaze counterpart, a film's)."""
        if item.show is not None:
            self._show_windows.open_show(item.show)
        elif item.discover_show is not None:
            self._show_windows.open_discover(item.discover_show)
        elif item.movie is not None:
            self._movie_windows.open_movie(item.movie)

    def _visible_view(self) -> BaseView | None:
        """AirGrabber's page on screen; None on MediaForge's, which looks after itself."""
        if self._mode is Mode.FORGE:
            return None
        if self._mode is Mode.MOVIES:
            return self._movie_view
        if self._mode is Mode.DOWNLOADS:
            return self._downloads_view
        return self._series_views[self._series_tabs.get()]

    # -- keyboard shortcuts --------------------------------------------------------------------

    def _bind_shortcuts(self) -> None:
        """In the main window: Ctrl+F = the search box, F5 = refresh the page on screen. Esc is
        each secondary window's own (show window, search results, dialogs)."""
        for sequence in ("<Control-f>", "<Control-F>"):
            self.bind(sequence, self._focus_search)
        self.bind("<F5>", self._refresh_page)

    def _focus_search(self, _event: tk.Event[Any]) -> str:
        self._search.focus_set()
        if self._search.get():  # "" while the placeholder shows
            self._search.select_range(0, "end")
            self._search.icursor("end")
        return "break"

    def _refresh_page(self, event: tk.Event[Any]) -> str | None:
        if typing_in(event.widget):
            return None
        view = self._visible_view()
        if view is None:
            self._forge.refresh_page()
        else:
            view.reload()
        return "break"

    def _on_settings_changed(self, updated: Settings) -> None:
        previous, self._settings_seen = self._settings_seen, updated
        if updated.theme_mode != previous.theme_mode:
            theme.apply_appearance(updated.theme_mode)
        if updated.spotlight_motion != previous.spotlight_motion:
            self._band.set_motion(updated.spotlight_motion)
        if settings_affect_pages(previous, updated):
            self._reload_pages()

    def _reload_pages(self, *, quiet: bool = False) -> None:
        """Settings or library changed: every page is outdated; reload the one on screen now
        (`quiet`, a library change: without a visible rebuild, BaseView.refresh_quietly)."""
        for page in (*self._series_views.values(), self._movie_view):
            page.invalidate()
        view = self._visible_view()
        if view is not None:  # MediaForge's page has nothing of these
            if quiet:
                view.refresh_quietly()
            else:
                view.refresh()
        self._band.reload()  # tonight's episodes, a new TMDB key's pictures

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
            profile_id=self._services.profiles.current_id,  # closes at a switch
            on_open_show=self._show_windows.open_show,
            card_scale=self._card_scale(),
            on_open_movie=self._movie_windows.open_movie,
        )

    def _search_episode(self, entry: CalendarEntry) -> None:
        """An episode's "☰ Choose…": its show's info window on the episode's releases (a
        special, without a number, on its season's pack)."""
        episode = entry.episode
        target = TvTarget(entry.show, episode.season, episode.number)
        self._show_windows.open_show(entry.show, target=target)

    def _grab_movie(self, movie: Movie) -> None:
        """A film's "☰ Choose…": its info window on its releases."""
        self._movie_windows.open_movie(movie, releases=True)

    # -- one-click downloads -------------------------------------------------------------------

    # Every one-click download goes into the app's download queue (QueueLink), which no
    # window owns: closing a window never cancels one, and they run a couple at a time.
    # A click by hand on one title (⬇ / ⬇ Auto, ⬇ Whole season's pack) downloads it also when
    # it was grabbed before (`again`: the user asked for it; 3.1.0 never shows a grab); the
    # batches that fetch what is missing ("All missing", "Download missing", a stack's
    # "Download all", a season's fallback) skip what the download history has.

    def _download_episode(self, entry: CalendarEntry) -> None:
        item = self._episode_item(entry, again=True)
        if item is None:  # a special: nothing to download by (its card says Search)
            self._search_episode(entry)
            return
        self._queue_link.download(item)

    def _episode_item(self, entry: CalendarEntry, *, again: bool = False) -> QueuedDownload | None:
        """An episode's one-click download (None for a special, which has no number to
        download by). `again`: also when it was grabbed before (a click by hand)."""
        show, episode = entry.show, entry.episode
        if episode.number is None:
            return None
        return self._tv_item(show, episode.season, episode.number, again=again)

    def _tv_item(
        self, show: Show, season: int, number: int | None, *, again: bool = False
    ) -> QueuedDownload:
        """The one-click download of an episode (`number` None: its season's pack)."""
        search = self._services.search
        if number is None:
            request = search.season_request(show, season)
            label = f"{show.name} S{season:02d}"
        else:
            request = search.episode_request(show, season, number)
            label = f"{show.name} S{season:02d}E{number:02d}"
        target = GrabTarget(
            media_type=MediaType.TV, media_id=show.id, season=season, episode=number
        )
        return QueuedDownload(label=label, target=target, request=request, again=again)

    def _download_season(self, show: Show, season: int, needed: Sequence[CalendarEntry]) -> None:
        """The show window's "⬇ Whole season": the season's pack (a click by hand: also when
        grabbed before); when none is good enough, the episodes still `needed` (not grabbed
        yet) instead (the queue does that, and says so)."""
        fallback = tuple(
            item for entry in needed if (item := self._episode_item(entry)) is not None
        )
        pack = self._tv_item(show, season, None)
        self._queue_link.download(
            QueuedDownload(
                label=pack.label,
                target=pack.target,
                request=pack.request,
                again=True,
                fallback=fallback,
            )
        )

    def _download_entry_again(self, entry: DownloadEntry, auto: bool) -> None:
        """The Downloads page's "Download again…": ⬇ Auto (`auto`) or ☰ Choose…."""
        record = entry.record
        if record.media_type is MediaType.TV:
            show = entry.show
            if show is None or record.season is None:
                return
            if auto:
                item = self._tv_item(show, record.season, record.episode, again=True)
                self._queue_link.download(item)
            else:
                target = TvTarget(show, record.season, record.episode)
                self._show_windows.open_show(show, target=target)
            return
        spawn(self._film_again(record.media_id, entry.title, auto), name="download-again")

    async def _film_again(self, movie_id: int, title: str, auto: bool) -> None:
        """A film known by its TMDB id downloaded again, or its info window on its releases,
        once TMDB has described it."""
        if not auto:
            if not await self._movie_windows.open_movie_id(movie_id, releases=True):
                self._report(
                    t("Couldn't look up {title} on TMDB", title=title), _DOWNLOAD_STATUS_MS
                )
            return
        try:
            async with asyncio.timeout(_DETAILS_WAIT_S):
                movie = await self._services.discovery.movie(movie_id)
        except (TMDBError, TimeoutError) as exc:
            log.info("Film %d: no TMDB description to download it again: %s", movie_id, exc)
            self._report(t("Couldn't look up {title} on TMDB", title=title), _DOWNLOAD_STATUS_MS)
            return
        self._download_movie(movie)

    def _download_episodes(self, batch: BatchKey, entries: Sequence[CalendarEntry]) -> None:
        """A calendar stack's "⬇ Download all" (and the show window's "⬇ All missing"): its
        episodes queued as one batch (each with its own state; a failed grab ends the batch),
        unless that batch is queued already."""
        items = [item for entry in entries if (item := self._episode_item(entry)) is not None]
        self._queue_link.download_batch(batch, items)

    def _download_movie(self, movie: Movie) -> None:
        """A film's ⬇ / ⬇ Auto (a click by hand: also when it was grabbed before)."""

        async def request() -> SearchRequest:  # when its turn comes
            details = await self._movie_details(movie)
            return self._services.search.movie_request(movie, details)

        target = GrabTarget(media_type=MediaType.MOVIE, media_id=movie.id)
        self._queue_link.download(
            QueuedDownload(label=movie_title(movie), target=target, request=request, again=True)
        )

    async def _movie_details(self, movie: Movie) -> MovieDetails | None:
        """The film's TMDB details (for its IMDb id), as the releases panel waits for them."""
        try:
            async with asyncio.timeout(_DETAILS_WAIT_S):
                return await self._services.discovery.movie_details(movie.id)
        except TMDBError as exc:
            log.info("No TMDB details for %s (TMDB id %d): %s", movie.title, movie.id, exc)
        except TimeoutError:
            log.info("No TMDB details for %s (TMDB id %d) in time", movie.title, movie.id)
        return None

    # -- missing episodes ----------------------------------------------------------------------

    def _download_missing(self, show: Show) -> None:
        """Start "Download missing" for a tracked show (offered only with Plex), unless its run
        is going already: its missing episodes (at most MAX_PER_RUN, oldest first) queued as
        one batch in the download queue."""
        runs = self._missing_runs
        if runs.is_working(show.id):
            return
        runs.set(show.id, MissingRunState())  # "Finding missing episodes…" at once
        # The selected person's: the cards and windows offering it show their shows.
        person = self._services.profiles.current_id
        task = spawn(self._queue_missing(show, person), name="download-missing")
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _queue_missing(self, show: Show, profile_id: int) -> None:
        # The cards may be gone or rebuilt by now: MissingRuns repaints whichever exist.
        runs = self._missing_runs
        try:
            missing = await self._services.library.missing_episodes(
                show.id, profile_id=profile_id
            )
        except BaseException:
            runs.set(show.id, None)
            raise  # cancelled, or logged and reported by the task runner
        todo = missing[:MAX_PER_RUN]
        items = [item for entry in todo if (item := self._episode_item(entry)) is not None]

        def progress(state: BatchState) -> None:
            runs.set(
                show.id,
                MissingRunState(index=state.index, total=state.total, queued=state.queued),
            )

        def done(events: tuple[QueueEvent, ...]) -> None:
            report = queued_run_report(show.id, len(items), events, len(missing) - len(todo))
            finished = MissingRunState(index=report.total, total=report.total, report=report)
            runs.set(show.id, finished)
            self._report(outcome_status_text(show.name, report), _DOWNLOAD_STATUS_MS)
            self.after(OUTCOME_MS, lambda: runs.expire(show.id, finished))

        batch = tuple(item.key for item in items)
        if not self._queue_link.download_batch(batch, items, on_progress=progress, on_done=done):
            runs.set(show.id, None)  # the same episodes are queued already as a batch

    def _report(self, text: str, clear_after_ms: int) -> None:
        try:
            self.set_status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # window already destroyed

    def _on_library_changed(self) -> None:
        self._reload_pages(quiet=True)
        # Also replaces a lingering "Syncing library…" message (unless a timed one is showing).
        spawn(self._show_library_summary(), name="library-summary")

    def _open_settings(self, section: str | None = None) -> None:
        """The one Settings, over this window (at `section`'s page when given); a dialog that is
        open already is raised, and shows `section`'s page when given."""
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            if section is not None:
                self._settings_dialog.show_section(section)
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
            on_restart=self.restart,
            images=self._services.images,
            library=self._services.library,
            backup=self._services.backup,
            plex=self._services.plex,
            forge=self._services.forge,
            section=section,
        )

    async def _set_up_prowlarr(self) -> None:
        message = prowlarr_setup_message(await self._services.prowlarr_setup.run())
        if message and self.winfo_exists():
            self.set_status(message, clear_after_ms=10_000)

    async def _first_run(self) -> None:
        """The welcome for a new library; a library from before it just records it as done.
        A new library with qBittorrent as the torrent program (install.ps1 installs it when there
        is none) first gets "Open in my torrent program" and qBittorrent's save folder
        (`first_run_defaults`)."""
        settings = self._services.settings
        if settings.current.setup_done:
            return
        stats = await self._services.library.stats(profile_id=self._services.profiles.current_id)
        action = first_run_action(settings.current.setup_done, stats.tracked_shows)
        if action is FirstRun.MARK_DONE:
            await settings.update(setup_done=True)
        elif action is FirstRun.WELCOME and self.winfo_exists():
            torrent_app = self._services.torrent_app
            app = await torrent_app.find()
            qbittorrent = app is not None and app.name == QBITTORRENT
            state = await torrent_app.qbittorrent_state() if qbittorrent else None
            if changes := first_run_defaults(settings.current, app, state):
                await settings.update(**changes)
            if not self.winfo_exists():
                return
            # A key saved there reloads the TMDB pages through _on_settings_changed.
            welcome = WelcomeDialog(
                self,
                settings=settings,
                torrent_app=self._services.torrent_app,
                discovery=self._services.discovery,
                airdates=self._services.airdates,
                library=self._services.library,
                remote=self._services.remote,
                on_restart=self.restart,
                backup=self._services.backup,  # "Moving from another PC? Load a backup"
                profiles=self._services.profiles,  # "Who uses AirGrabber?"
            )
            self._watch_welcome(welcome)

    def _watch_welcome(self, welcome: tk.Misc) -> None:
        """When the welcome closes (Finish, its ✕ or a loaded backup; not a restart),
        "Let's get started" may open (`_get_started_after_welcome`)."""

        def closed(event: tk.Event[Any]) -> None:
            if event.widget is not welcome:  # a Toplevel also hears its children go
                return
            if self.restart_requested or not self.winfo_exists():
                return
            spawn(self._get_started_after_welcome(), name="get-started-after-welcome")

        welcome.bind("<Destroy>", closed, add="+")

    async def _get_started_after_welcome(self) -> None:
        """Open "Let's get started" once, right after the welcome, when the selected person
        still follows no shows (they skipped the airdates.tv import, or it found nothing);
        `get_started_shown` keeps it from ever opening by itself again. A library from before
        never gets here: its welcome doesn't show (`_first_run`)."""
        settings = self._services.settings
        if settings.current.get_started_shown:
            return
        stats = await self._services.library.stats(profile_id=self._services.profiles.current_id)
        if not get_started_due(settings.current.get_started_shown, stats.tracked_shows):
            return
        await settings.update(get_started_shown=True)
        if self.winfo_exists():
            self.open_get_started()

    def open_get_started(self) -> None:
        """The "Let's get started" window for the selected person (brought forward when it
        is open already)."""
        dialog = self._get_started
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            return
        self._get_started = GetStartedDialog(
            self,
            library=self._services.library,
            discovery=self._services.discovery,
            images=self._services.images,
            profile_id=self._services.profiles.current_id,
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
            self._version_label.configure(
                text=t("v{current} · v{latest} on GitHub", current=__version__, latest=info.latest)
            )
        elif self._services.settings.current.auto_update:
            await self._stage_update(info)
        else:
            self._show_update_badge(
                t("⬆ Update to v{version}", version=info.latest), t(UPDATE_TIP), self._open_settings
            )

    async def _stage_update(self, info: UpdateInfo) -> None:
        """Download the update in the background; main.py installs it when AirGrabber closes."""
        try:
            version = await self._services.updates.stage()
        except UpdateError as exc:
            log.warning("Downloading the update failed: %s", exc)
            if self.winfo_exists():  # Settings can still install it
                self._show_update_badge(
                t("⬆ Update to v{version}", version=info.latest), t(UPDATE_TIP), self._open_settings
            )
            return
        if not self.winfo_exists() or self._update_task is not None:
            return  # closing, or Settings' "Update to …" is installing it right now
        self._show_update_badge(t("⬆ Restart to update"), t(RESTART_TIP), self.restart)
        self.set_status(
            t("AirGrabber {version} is ready. It installs when you close AirGrabber.", version=version),
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
        """Show "Updated to v…" on the first start after an update, and once "What's new" when
        the version went up (not on a first run: the welcome shows then); then remember this
        version."""
        settings = self._services.settings
        last_run, first_run = settings.current.last_run_version, not settings.current.setup_done
        notice = update_notice(last_run, __version__)
        if notice is not None:
            self.set_status(notice, clear_after_ms=_UPDATE_STATUS_MS)
        notes = None if first_run else whats_new(last_run, __version__)
        await settings.update(last_run_version=__version__)
        if notes is not None and self.winfo_exists():
            self._show_whats_new(notes)

    def _show_whats_new(self, notes: WhatsNew) -> None:
        """The "What's new" dialog; when started hidden in the tray, once the window opens."""
        if self.state() in ("normal", "zoomed"):
            WhatsNewDialog(
                self, version=notes.version, lines=notes.lines, features=notes.features
            )
            return

        def on_map(event: tk.Event[Any]) -> None:
            binding = self._whats_new_binding
            if event.widget is not self or binding is None:
                return  # the root's tag also sees its children's <Map>
            self._whats_new_binding = None
            tk.Misc.unbind(self, "<Map>", binding)
            WhatsNewDialog(
                self, version=notes.version, lines=notes.lines, features=notes.features
            )

        self._whats_new_binding = tk.Misc.bind(self, "<Map>", on_map, add="+")

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
            self.set_status(t("⚠ Update failed: {error}", error=exc), clear_after_ms=15_000)
            return
        self.set_status(t("Updated to v{version}. Restarting…", version=version))
        self.after(1200, self.restart)

    def restart(self) -> None:
        """Close, and have main.py start a fresh copy once everything is shut down (after a
        Forge run has finished its current file: `quit_app`)."""
        self.restart_requested = True
        self._quit_gate.request()

    # -- status bar --------------------------------------------------------------------------

    async def _show_library_summary(self) -> None:
        stats = await self._services.library.stats(profile_id=self._services.profiles.current_id)
        if stats.last_synced:
            when = stats.last_synced.astimezone()
            synced = t("last synced {date}", date=f"{when:%d} {month_short(when)} {when:%H:%M}")
        else:
            synced = t("not synced yet")
        self._idle_status = t(
            "{shows} tracked shows · {episodes} episodes cached · {synced}",
            shows=stats.tracked_shows,
            episodes=number(stats.episodes, ","),
            synced=synced,
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
                t("Syncing library… {done}/{total}", done=p.done, total=p.total)
                + (f" · {p.current}" if p.current else "")
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
