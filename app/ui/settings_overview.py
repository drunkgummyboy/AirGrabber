"""Settings → Overview (docs/settings/2026-11-19-settings-redesign.md §5;
mockups/settings-reorganized.html): how AirGrabber is set up at a glance, one card per part, in
two columns, each a dot (green: fine, amber: needs you, grey: off), its page's name and one line;
a click opens that page. Quick setup's box follows them (app/ui/settings_dialog.py).

`overview_cards` makes the cards from what is stored and what the window already knows (pure: no
service is asked): the torrent program's last look (`TorrentAppStatus`), Plex's status (read by
the Plex page too), the folder library's status, and MediaForge's facts (`ForgeFacts`) only while
it runs: the Overview never starts MediaForge (it may not have started when Settings opens), and
until it runs the Folders card counts the lists alone and Sorting's says it is getting ready.
`OverviewCards` draws them; the window draws them again when something they show changes and
each time the page is shown.

Light at import: no Forge module (AirGrabber's start imports the Settings window's modules).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.indexers import ApibayIndexer, BitsearchIndexer, EztvIndexer, ProwlarrIndexer, YtsIndexer
from app.models import SUBTITLE_LANGUAGES, DownloadMode, Settings
from app.models.settings import remote_ready
from app.services.library_source import folders_on
from app.services.plex import plex_connected
from app.services.qbittorrent_local import set_up_by_airgrabber
from app.ui import theme
from app.ui.dialog_kit import DETAIL_SIZE, SMALL_SIZE
from app.ui.plex_settings import problem_text
from app.ui.remote_program_form import remote_host
from app.ui.settings_layout import TEXT_SIZE, SettingsPage, page_width
from app.ui.settings_pages_registry import (
    PAGE_ACCOUNTS,
    PAGE_AUTOMATIC,
    PAGE_FOLDERS,
    PAGE_PLEX,
    PAGE_QUALITY,
    PAGE_SORTING,
    PAGE_SUBTITLES,
    PAGE_TORRENT,
    PAGE_UPDATES,
    PAGE_WINDOWS,
)
from app.ui.torrent_app_status import TorrentStatus

if TYPE_CHECKING:
    from app.models.folder_library import FolderLibraryStatus
    from app.models.plex import PlexStatus

CardState = Literal["ok", "warn", "off"]


class Card(NamedTuple):
    """One card: the page it opens (its key, shown as its name), its dot and its line."""

    page: str
    state: CardState
    line: str


class ForgeFacts(NamedTuple):
    """What the Folders and Sorting cards show of MediaForge (app/forge/ui/settings_pages.py's
    `overview_facts`, only while it runs)."""

    watching: bool  # the "Watch folders" switch is on
    rules_on: int  # the rules switched on
    series_folder: str  # where sorted series go ("": not chosen)
    movies_folder: str
    needs_series: bool  # a rule that is on moves series there (presets don't count)
    needs_movies: bool


# -- texts ----------------------------------------------------------------------------------------

TORRENT_LOOKING = N_("On this PC: looking for the torrent program…")
TORRENT_INSTALLING = N_("On this PC: installing qBittorrent…")
TORRENT_SETTING_UP = N_("On this PC: setting up qBittorrent…")
TORRENT_REINSTALLING = N_("On this PC: reinstalling qBittorrent…")
TORRENT_APP_READY = N_("{program} on this PC, ready")
TORRENT_APP_FOUND = N_("{program} on this PC")
TORRENT_APP_MISSING = N_("On this PC: no torrent program found yet")
TORRENT_APP_NEEDS = N_("On this PC: the torrent program needs you")
TORRENT_SETUP_NEEDED = N_("qBittorrent on this PC still asks about each download")
TORRENT_SETUP_WAITING = N_("On this PC: close qBittorrent to finish setting it up")
TORRENT_LOCAL = N_("qBittorrent on this PC (set up by AirGrabber)")
TORRENT_REMOTE = N_("{program} at {host}")
TORRENT_REMOTE_UNSET = N_("A NAS or another computer, not set up yet")
TORRENT_FOLDER = N_(".torrent files to a folder")

QUALITY_BOTH = N_("{quality} · {sources}")
QUALITY_EACH = N_("Series {series}, movies {movies} · {sources}")
SITE_ONE, SITE_MORE = N_("{n} site"), N_("{n} sites")
WITH_PROWLARR = N_("{sites} + Prowlarr")
NO_SOURCE = N_("No site to search")

AUTO_BOTH = N_("On for new shows and films · waits {hours}")
AUTO_SHOWS = N_("On for new shows · waits {hours}")
AUTO_FILMS = N_("On for new films · waits {hours}")
AUTO_OFF = N_("Off for new shows and films")
HOURS = N_("{n} h")

FOLDERS_MISSING_BOTH = N_("Your Series and Movies folders aren't chosen yet")
FOLDERS_MISSING_SERIES = N_("Your Series folder isn't chosen yet")
FOLDERS_MISSING_MOVIES = N_("Your Movies folder isn't chosen yet")
FOLDERS_COUNTS = N_("{films} · {series}")
SERIES_FOLDER_ONE, SERIES_FOLDER_MORE = N_("{n} series folder"), N_("{n} series folders")
FILM_FOLDER_ONE, FILM_FOLDER_MORE = N_("{n} film folder"), N_("{n} film folders")
FOLDERS_NONE = N_("No folders chosen yet")

SORTING_WATCHING = N_("Watching · {rules}")
SORTING_NO_RULE = N_("Watching, but no rule is on")
SORTING_PAUSED = N_("Paused · {rules}")
RULE_ONE, RULE_MORE = N_("{n} rule on"), N_("{n} rules on")
SORTING_WAITING = N_("Getting MediaForge ready…")
# A start that ended without MediaForge and without a reason (the Settings window says it).
SORTING_UNAVAILABLE = N_("MediaForge isn't available.")

NO_LANGUAGE = N_("No language chosen")

PLEX_AND_FOLDERS = N_("{plex} · your folders too")
FOLDERS_LIBRARY = N_("Your own folders: {films} · {series}")
FILM_ONE, FILM_MORE = N_("{n} film"), N_("{n} films")
SERIES_ONE, SERIES_MORE = N_("{n} series"), N_("{n} series")
NO_LIBRARY = N_("No library yet: connect Plex or count your folders")
YOUR_SERVER = N_("your Plex server")
PLEX_CONNECTED = N_("Connected to {server}")

TMDB_SET = N_("TMDB key set")
TMDB_MISSING = N_("No TMDB key yet")
OPENSUBTITLES_ACCOUNT = N_("OpenSubtitles account: {name}")
OPENSUBTITLES_NONE = N_("OpenSubtitles without an account")

UPDATES_AUTO = N_("Version {version} · updates by themselves")
UPDATES_MANUAL = N_("Version {version} · updates when you choose")

IN_TRAY = N_("In the tray")
CLOSES = N_("Closes fully")
STARTS = N_("starts with Windows")
DOESNT_START = N_("doesn't start with Windows")

# The websites AirGrabber searches itself (Prowlarr apart), as the Sites box lists them.
_BUILT_IN = tuple(cls.name for cls in (ApibayIndexer, EztvIndexer, BitsearchIndexer, YtsIndexer))
# Busy on this PC: no verdict yet, but each says what it is doing.
_BUSY: dict[TorrentStatus, str] = {
    TorrentStatus.LOOKING: TORRENT_LOOKING,
    TorrentStatus.INSTALLING: TORRENT_INSTALLING,
    TorrentStatus.SETTING_UP: TORRENT_SETTING_UP,
    TorrentStatus.REINSTALLING: TORRENT_REINSTALLING,
}
_FAILED = frozenset(
    {TorrentStatus.FAILED, TorrentStatus.SETUP_FAILED, TorrentStatus.REINSTALL_FAILED}
)

# -- the cards (pure) -----------------------------------------------------------------------------


def _program(status: TorrentStatus | None, name: str, ready: str) -> Card:
    """The torrent program on this PC as the Torrent program page's status line has it
    (app/ui/torrent_app_status.py's `torrent_look`): `ready` is the line while all is well."""
    if status is None or status in _BUSY:  # not known yet (or busy): no dot of a verdict
        return Card(PAGE_TORRENT, "off", t(_BUSY[status or TorrentStatus.LOOKING]))
    if status is TorrentStatus.MISSING:
        return Card(PAGE_TORRENT, "warn", t(TORRENT_APP_MISSING))
    if status in _FAILED:
        return Card(PAGE_TORRENT, "warn", t(TORRENT_APP_NEEDS))
    if status is TorrentStatus.SETUP_WAITING:
        return Card(PAGE_TORRENT, "warn", t(TORRENT_SETUP_WAITING))
    if status is TorrentStatus.SETUP_NEEDED:  # the page offers "Set up qBittorrent …"
        return Card(PAGE_TORRENT, "warn", t(TORRENT_SETUP_NEEDED))
    if status is TorrentStatus.FOUND and name:
        return Card(PAGE_TORRENT, "ok", t(TORRENT_APP_FOUND, program=name))
    return Card(PAGE_TORRENT, "ok", ready)


def _torrent(
    settings: Settings,
    status: TorrentStatus | None,
    name: str,
    local: TorrentStatus | None,
) -> Card:
    """`status` / `name`: the program on this PC (the first choice's look); `local`:
    AirGrabber's own qBittorrent's look while a NAS / other computer choice points at it."""
    mode = settings.download_mode
    if mode is DownloadMode.REMOTE:
        if set_up_by_airgrabber(settings):
            return _program(local, "qBittorrent", t(TORRENT_LOCAL))
        kind = settings.remote_kind
        password = settings.remote_password.get_secret_value()
        if not remote_ready(kind, settings.remote_url, settings.remote_username, password):
            return Card(PAGE_TORRENT, "warn", t(TORRENT_REMOTE_UNSET))
        host = remote_host(settings.remote_url)
        line = t(TORRENT_REMOTE, program=kind.program, host=host) if host else kind.program
        return Card(PAGE_TORRENT, "ok", line)
    if mode is DownloadMode.FOLDER:
        return Card(PAGE_TORRENT, "ok", t(TORRENT_FOLDER))
    return _program(status, name, t(TORRENT_APP_READY, program=name or "qBittorrent"))


def _quality(settings: Settings) -> Card:
    from app.ui.settings_dialog import quality_name  # the Quality box's names (no cycle then)

    off = set(settings.disabled_indexers)
    sites = sum(1 for name in _BUILT_IN if name not in off)
    # Prowlarr searches only once it has an address and a key (ProwlarrIndexer.supports).
    prowlarr = (
        ProwlarrIndexer.name not in off
        and bool(settings.prowlarr_url.strip())
        and bool(settings.prowlarr_api_key.get_secret_value().strip())
    )
    if not sites and not prowlarr:
        return Card(PAGE_QUALITY, "warn", t(NO_SOURCE))
    sources = tn(SITE_ONE, SITE_MORE, sites) if sites else ""
    if prowlarr:
        sources = t(WITH_PROWLARR, sites=sources) if sites else "Prowlarr"
    series, movies = quality_name(settings.quality_tv), quality_name(settings.quality_movie)
    if series == movies:
        line = t(QUALITY_BOTH, quality=series, sources=sources)
    else:
        line = t(QUALITY_EACH, series=series, movies=movies, sources=sources)
    return Card(PAGE_QUALITY, "ok", line)


def _automatic(settings: Settings) -> Card:
    shows, films = settings.auto_download_new_shows, settings.auto_download_new_films
    hours = t(HOURS, n=settings.auto_wait_hours)
    if shows and films:
        return Card(PAGE_AUTOMATIC, "ok", t(AUTO_BOTH, hours=hours))
    if shows:
        return Card(PAGE_AUTOMATIC, "ok", t(AUTO_SHOWS, hours=hours))
    if films:
        return Card(PAGE_AUTOMATIC, "ok", t(AUTO_FILMS, hours=hours))
    return Card(PAGE_AUTOMATIC, "off", t(AUTO_OFF))


def _listed(folders: Sequence[str]) -> int:
    return sum(1 for folder in folders if folder.strip())


def _folders(settings: Settings, forge: ForgeFacts | None) -> Card:
    """Amber when a kind has no destination while a rule that is on sends files there (known
    only while MediaForge runs; presets don't count); otherwise how many folders each list
    holds."""
    if forge is not None:
        series = forge.needs_series and not forge.series_folder
        movies = forge.needs_movies and not forge.movies_folder
        if series and movies:
            return Card(PAGE_FOLDERS, "warn", t(FOLDERS_MISSING_BOTH))
        if series:
            return Card(PAGE_FOLDERS, "warn", t(FOLDERS_MISSING_SERIES))
        if movies:
            return Card(PAGE_FOLDERS, "warn", t(FOLDERS_MISSING_MOVIES))
    series_n, films_n = _listed(settings.series_folders), _listed(settings.film_folders)
    if not series_n and not films_n:
        return Card(PAGE_FOLDERS, "off", t(FOLDERS_NONE))
    line = t(
        FOLDERS_COUNTS,
        series=tn(SERIES_FOLDER_ONE, SERIES_FOLDER_MORE, series_n),
        films=tn(FILM_FOLDER_ONE, FILM_FOLDER_MORE, films_n),
    )
    return Card(PAGE_FOLDERS, "ok", line)


def _sorting(forge: ForgeFacts | None, reason: str) -> Card:
    if forge is None:
        if reason:  # MediaForge can't start (guessit missing, forge.db): why
            return Card(PAGE_SORTING, "warn", reason)
        return Card(PAGE_SORTING, "off", t(SORTING_WAITING))
    rules = tn(RULE_ONE, RULE_MORE, forge.rules_on)
    if forge.watching and not forge.rules_on:  # watching, and nothing to do it with
        return Card(PAGE_SORTING, "warn", t(SORTING_NO_RULE))
    if forge.watching:
        return Card(PAGE_SORTING, "ok", t(SORTING_WATCHING, rules=rules))
    return Card(PAGE_SORTING, "off", t(SORTING_PAUSED, rules=rules))


def _subtitles(settings: Settings) -> Card:
    names = [
        t(SUBTITLE_LANGUAGES[code]) if code in SUBTITLE_LANGUAGES else code
        for code in settings.subtitle_languages
    ]
    if not names:
        return Card(PAGE_SUBTITLES, "warn", t(NO_LANGUAGE))
    return Card(PAGE_SUBTITLES, "ok", ", ".join(names))


def _plex(
    settings: Settings,
    plex: PlexStatus | None,
    folders: FolderLibraryStatus | None,
    now: datetime,
) -> Card:
    counting = folders.on if folders is not None else folders_on(settings)
    if plex_connected(settings) and (plex is None or plex.connected):
        state: CardState = "ok"
        if plex is not None and plex.problem is not None:
            line, state = problem_text(plex.problem, plex.counts, now), "warn"
        else:
            server = settings.plex_server_name or t(YOUR_SERVER)
            line = t(PLEX_CONNECTED, server=server)
        if counting:
            line = t(PLEX_AND_FOLDERS, plex=line)
        return Card(PAGE_PLEX, state, line)
    if counting:
        films = folders.films if folders is not None else 0
        shows = folders.shows if folders is not None else 0
        line = t(
            FOLDERS_LIBRARY,
            films=tn(FILM_ONE, FILM_MORE, films),
            series=tn(SERIES_ONE, SERIES_MORE, shows),
        )
        return Card(PAGE_PLEX, "ok", line)
    return Card(PAGE_PLEX, "off", t(NO_LIBRARY))


def _accounts(settings: Settings, *, opensubtitles: bool) -> Card:
    has_key = bool(settings.tmdb_api_key.get_secret_value().strip())
    parts = [t(TMDB_SET if has_key else TMDB_MISSING)]
    if opensubtitles:
        name = settings.opensubtitles_username.strip()
        # What is stored: whether signing in works shows only when it is tried (in memory).
        parts.append(t(OPENSUBTITLES_ACCOUNT, name=name) if name else t(OPENSUBTITLES_NONE))
    return Card(PAGE_ACCOUNTS, "ok" if has_key else "warn", " · ".join(parts))


def _updates(settings: Settings, version: str) -> Card:
    if settings.auto_update:
        return Card(PAGE_UPDATES, "ok", t(UPDATES_AUTO, version=version))
    return Card(PAGE_UPDATES, "off", t(UPDATES_MANUAL, version=version))


def _windows(settings: Settings, start_with_windows: bool) -> Card:
    tray = settings.keep_in_tray
    starts = t(STARTS if start_with_windows else DOESNT_START)
    line = " · ".join([t(IN_TRAY if tray else CLOSES), starts])
    return Card(PAGE_WINDOWS, "ok" if tray and start_with_windows else "off", line)


def overview_cards(
    settings: Settings,
    *,
    pages: Collection[str],
    version: str,
    start_with_windows: bool,
    torrent: TorrentStatus | None = None,
    torrent_name: str = "",
    local_torrent: TorrentStatus | None = None,
    plex: PlexStatus | None = None,
    folders: FolderLibraryStatus | None = None,
    forge: ForgeFacts | None = None,
    forge_reason: str = "",
    now: datetime | None = None,
) -> list[Card]:
    """The Overview's cards, in order, for the pages the window shows (`pages`): Torrent
    program, Quality & sources, Automatic, Folders, Sorting, Subtitles, Plex & library, Accounts
    & keys, Updates & backup, Windows. `start_with_windows`: the real shortcut; `torrent` /
    `torrent_name`: the torrent program's last look, `local_torrent` AirGrabber's own
    qBittorrent's (the NAS / other computer choice pointed at it); `plex`: Plex's status (None: from the
    settings alone); `folders`: the folder library's; `forge`: MediaForge's facts while it runs
    (None: not running), `forge_reason` why it can't."""
    now = now if now is not None else datetime.now(UTC)
    made: dict[str, Callable[[], Card]] = {
        PAGE_TORRENT: lambda: _torrent(settings, torrent, torrent_name, local_torrent),
        PAGE_QUALITY: lambda: _quality(settings),
        PAGE_AUTOMATIC: lambda: _automatic(settings),
        PAGE_FOLDERS: lambda: _folders(settings, forge),
        PAGE_SORTING: lambda: _sorting(forge, forge_reason),
        PAGE_SUBTITLES: lambda: _subtitles(settings),
        PAGE_PLEX: lambda: _plex(settings, plex, folders, now),
        PAGE_ACCOUNTS: lambda: _accounts(settings, opensubtitles=PAGE_SUBTITLES in pages),
        PAGE_UPDATES: lambda: _updates(settings, version),
        PAGE_WINDOWS: lambda: _windows(settings, start_with_windows),
    }
    return [make() for page, make in made.items() if page in pages]


# -- drawing --------------------------------------------------------------------------------------

CARD_GAP = 12  # between the cards, both ways
CARD_PAD_X = 14
CARD_PAD_Y = 12
CARD_RADIUS = 12
DOTS: dict[CardState, theme.Color] = {
    "ok": theme.SUCCESS,
    "warn": theme.WARNING,
    "off": theme.TEXT_FAINT,
}


def line_wrap() -> int:
    """Where a card's line wraps: inside its half of the page (the open window's)."""
    return (page_width() - CARD_GAP) // 2 - 2 * (CARD_PAD_X + 1)


class CardView(ctk.CTkFrame):
    """One card: the dot and its page's name, its line under them; a click opens the page."""

    def __init__(self, master: Any, card: Card, on_open: Callable[[str], None]) -> None:
        self._edge: theme.Color = theme.WARNING if card.state == "warn" else theme.PANEL_EDGE
        super().__init__(
            master,
            fg_color=theme.TAB_BG,
            border_width=1,
            border_color=self._edge,
            corner_radius=CARD_RADIUS,
            cursor="hand2",
        )
        self.card = card
        self._on_open = on_open
        self._hovered = False
        # The dot is a coloured "●" (a text: CTk repaints it on a theme switch, and it needs no
        # image of the screen it is on).
        head = ctk.CTkFrame(self, fg_color="transparent", cursor="hand2")
        head.pack(fill="x", padx=CARD_PAD_X, pady=(CARD_PAD_Y, 0))
        self.dot = ctk.CTkLabel(
            head,
            text="●",
            width=14,
            font=theme.font(SMALL_SIZE),
            text_color=DOTS[card.state],
            cursor="hand2",
        )
        self.dot.pack(side="left", padx=(0, 6))
        self.title = ctk.CTkLabel(
            head,
            text=t(card.page),
            font=theme.font(DETAIL_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
            cursor="hand2",
        )
        self.title.pack(side="left", fill="x", expand=True)
        self.line = ctk.CTkLabel(
            self,
            text=card.line,
            font=theme.font(TEXT_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=line_wrap(),
            cursor="hand2",
        )
        self.line.pack(fill="x", padx=CARD_PAD_X, pady=(2, CARD_PAD_Y))
        for widget in (self, head, self.dot, self.title, self.line):
            widget.bind("<Button-1>", lambda _event: self.click(), add="+")
            widget.bind("<Enter>", lambda _event: self._hover(True), add="+")
            widget.bind("<Leave>", lambda _event: self._hover(False), add="+")

    def click(self) -> None:
        self._on_open(self.card.page)

    def show(self, card: Card) -> None:
        """Show `card` (the same page's, changed) in place: its dot, line and edge."""
        if card == self.card:
            return
        self.card = card
        self._edge = theme.WARNING if card.state == "warn" else theme.PANEL_EDGE
        self.configure(border_color=theme.EDGE if self._hovered else self._edge)
        self.dot.configure(text_color=DOTS[card.state])
        self.line.configure(text=card.line)

    def _hover(self, hovered: bool) -> None:
        self._hovered = hovered
        if self.winfo_exists():
            self.configure(border_color=theme.EDGE if hovered else self._edge)


class OverviewCards:
    """The cards in two columns on the Overview page (`show` draws them again)."""

    def __init__(self, page: SettingsPage, *, on_open: Callable[[str], None]) -> None:
        self._on_open = on_open
        self.frame = ctk.CTkFrame(page.frame, fg_color="transparent")
        self.frame.grid_columnconfigure((0, 1), weight=1, uniform="cards")
        page.add(self.frame)
        self.cards: list[Card] = []
        self.views: dict[str, CardView] = {}

    def show(self, cards: Sequence[Card]) -> None:
        """Draw `cards`: nothing changes when they are the same; for the same pages only the
        cards that changed are drawn again, in place (a whole redraw of the ten took 60-160 ms
        on the Tk thread, each status or settings change)."""
        cards = list(cards)
        if cards == self.cards or not self.frame.winfo_exists():
            return
        same_pages = [card.page for card in cards] == [card.page for card in self.cards]
        self.cards = cards
        if same_pages and len(self.views) == len(cards):
            for card in cards:
                self.views[card.page].show(card)
            return
        for view in self.views.values():
            view.destroy()
        self.views = {}
        for index, card in enumerate(cards):
            view = CardView(self.frame, card, self._on_open)
            row, column = divmod(index, 2)
            view.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0, CARD_GAP // 2) if column == 0 else (CARD_GAP // 2, 0),
                pady=(0 if row == 0 else CARD_GAP, 0),
            )
            self.views[card.page] = view

    def card(self, page: str) -> Card | None:
        """The card that opens `page` (tests)."""
        return next((card for card in self.cards if card.page == page), None)
