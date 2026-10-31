"""The series info window: the shared info look (app/ui/title_info.py: the backdrop with the
trailer, the poster, title, meta line, genres, synopsis, track state, the cast), and the show's
episodes by season, each with its download state (✓ Downloaded with ↻ Again, ⬇ Auto with ☰
Choose…: app/ui/row_actions.py; or when it airs) and, once downloaded, its subtitles (CC ▾:
app/ui/subtitle_control.py). "☰ Choose…" shows that episode's releases (app/ui/release_panel.py)
in this window: in place of the episodes under a "← Episodes" bar ("S03E05 · The Crossing"),
or in the Releases tab ("Releases · S03E05"), by the layout (Settings' `info_layout`).

Not modal; one window per show (`ShowWindows` raises the open one, and every "☰ Choose…" of an
episode opens its show's window on that episode's releases). It works for any TVMaze
show, tracked or not: `LibraryService.show_details` reads a tracked show from the library and
fetches any other from TVMaze. TMDB shows (Discover) are matched to TVMaze first. The backdrop,
the trailer, the cast and the synopsis and genres in the app's language come from the show's
TMDB counterpart (`DiscoveryService.show_extras`); without one the poster stands in, blurred,
for the backdrop and TVMaze's summary is the synopsis; without a TMDB key there is no backdrop
and a plain hint says what a key adds.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import tkinter as tk
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.i18n import N_, day_short, month_short, t, tn
from app.models import (
    CalendarEntry,
    DiscoverShow,
    Episode,
    InfoLayout,
    Show,
    ShowDetails,
    TitleExtras,
)
from app.services.discovery import NoTVMazeMatch
from app.services.tmdb import TMDBError, TMDBNotConfigured
from app.services.tvmaze import TVMazeError
from app.ui import dialog_kit, theme
from app.ui.cards import (
    AUTO_SWITCH_TEXT,
    AUTO_SWITCH_TIP,
    auto_switch_color,
    tvmaze_poster_url,
)
from app.ui.download_control import (
    CORNER_RADIUS as CONTROL_RADIUS,
)
from app.ui.download_control import (
    FONT_SIZE as CONTROL_FONT_SIZE,
)
from app.ui.download_control import (
    ROW_HEIGHT as CONTROL_HEIGHT,
)
from app.ui.download_control import (
    Base,
    DownloadActions,
    episode_key,
)
from app.ui.missing_control import (
    CHIP_HAVE_TEXT,
    CHIP_MISSING_TEXT,
    CHIP_TIP,
    HAVE_EPISODE_TEXT,
    HAVE_SEASON_TEXT,
    MARK_MISSING_TEXT,
    MISSING_COLOR,
    NOTHING_MISSING_TEXT,
    SEASON_MENU_TIP,
    MenuItem,
    MissingActions,
    MissingButton,
    chip,
    missing_text,
    popup_menu,
)
from app.ui.poster_card import FOLLOWING_TEXT, TRACK_TEXT
from app.ui.release_panel import IndexerTarget, PanelServices, ReleasePanel, TvTarget
from app.ui.row_actions import FONT_SIZE as ACTIONS_FONT_SIZE
from app.ui.row_actions import RowActions
from app.ui.row_actions import slot_width as actions_slot_width
from app.ui.subtitle_control import CCControl, SubtitleActions
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, Measure, elide_to_width, unscaled_width
from app.ui.title_info import (
    SUMMARY_LINES,
    TitleInfoWindow,
    collapsed_summary,
    exists,
    join_meta,
    rating_text,
    target_code,
)
from app.ui.tooltip import add_tooltip
from app.ui.widgets import FlexGrid, LoadingBar

if TYPE_CHECKING:
    from app.services import DiscoveryService, ImageCache, LibraryService
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

RowState = Literal["downloaded", "downloadable", "upcoming"]
# The latest season's amber "Missing" / blue "New · aired today" / muted "✓ Have it".
Mark = Literal["missing", "new", "have"]
Listener = Callable[[], None]
Subscribe = Callable[[Listener], Callable[[], None]]  # returns the unsubscribe function
# Per (season, episode) of a show (TVMaze id), its subtitle mark ("CC EN NL").
SubtitleMarks = Callable[[int], Awaitable[Mapping[tuple[int, int], str]]]

__all__ = ["SUMMARY_LINES", "ShowDetailsWindow", "ShowWindows", "collapsed_summary"]

# Logical (unscaled) CTk units.
CHIP_WIDTH = 96
CHIP_HEIGHT = 32
EPISODE_ROW_HEIGHT = 52
CODE_WIDTH = 72
DATE_WIDTH = 64
SLOT_WIDTH = 200  # the row's right side (at least): its download controls, or when it airs
MARK_WIDTH = 92  # the latest season's chip, left of that (at least; `mark_slot_width`)
MARK_PAD = 12  # left and right of a chip's text
MARK_HEIGHT = 26
MISSING_BUTTON_WIDTH = 300  # the season bar's "⬇ Download missing (3)"
ROWS_PER_BATCH = 12  # rows built before Tk gets to draw and handle input again
CC_FONT_SIZE = 12  # the "CC EN NL" subtitle mark beside a downloaded episode's title
CC_GAP = 8

# Shown through t().
NEW_TODAY_TEXT = N_("New · aired today")
NEW_YESTERDAY_TEXT = N_("New · aired yesterday")
NEW_TEXT = N_("New")
NEW_TIP = N_(
    "Aired less than a day ago: a release usually turns up within hours. It counts as missing "
    "after a day."
)
AIRS_TODAY_TEXT = N_("airs today")
AIRS_TOMORROW_TEXT = N_("airs tomorrow")
ADDING_TEXT = N_("Adding…")
NOT_ON_TVMAZE_TEXT = N_("Couldn't find this show on TVMaze")
NO_EPISODES_TEXT = N_("No episodes listed yet.")
UNKNOWN_DATE_TEXT = N_("TBA")
# TVMaze's show statuses in plain words (another status shows as TVMaze writes it).
_STATUS_NAMES = {
    "Running": N_("Running"),
    "Ended": N_("Ended"),
    "To Be Determined": N_("To Be Determined"),
    "In Development": N_("In Development"),
}


# TVMaze's genres in plain words (another genre shows as TVMaze writes it).
_GENRE_NAMES = {
    "Action": N_("Action"),
    "Adult": N_("Adult"),
    "Adventure": N_("Adventure"),
    "Anime": N_("Anime"),
    "Children": N_("Children"),
    "Comedy": N_("Comedy"),
    "Crime": N_("Crime"),
    "DIY": N_("DIY"),
    "Drama": N_("Drama"),
    "Espionage": N_("Espionage"),
    "Family": N_("Family"),
    "Fantasy": N_("Fantasy"),
    "Food": N_("Food"),
    "History": N_("History"),
    "Horror": N_("Horror"),
    "Legal": N_("Legal"),
    "Medical": N_("Medical"),
    "Music": N_("Music"),
    "Mystery": N_("Mystery"),
    "Nature": N_("Nature"),
    "Romance": N_("Romance"),
    "Science-Fiction": N_("Science fiction"),
    "Sports": N_("Sports"),
    "Supernatural": N_("Supernatural"),
    "Thriller": N_("Thriller"),
    "Travel": N_("Travel"),
    "War": N_("War"),
    "Western": N_("Western"),
}


# -- text and state ----------------------------------------------------------------------------


def meta_line(show: Show, seasons: int | None = None) -> str:
    """"2022 · Apple TV · 3 seasons · ★ 8.4 · Running": year, network or streaming service,
    the number of seasons, rating and status, each left out when unknown ("" for none)."""
    return join_meta(
        (
            str(show.year) if show.year else None,
            show.network,
            tn("{n} season", "{n} seasons", seasons) if seasons else None,
            rating_text(show.rating),
            show_status(show.status) if show.status else None,
        )
    )


def genre_names(show: Show, extras: TitleExtras | None) -> tuple[str, ...]:
    """The genre pills: TMDB's (in the app's language) when known, else TVMaze's in plain
    words."""
    if extras is not None and extras.info.genres:
        return extras.info.genres
    return tuple(t(name) if (name := _GENRE_NAMES.get(genre)) else genre for genre in show.genres)


def synopsis(show: Show, extras: TitleExtras | None) -> str:
    """TMDB's overview (in the app's language, else English) when it has one, else TVMaze's
    summary."""
    if extras is not None and extras.info.overview:
        return extras.info.overview
    return show.summary


def show_status(status: str) -> str:
    """TVMaze's show status in plain words ("Running" / "Loopt"); another one as it is."""
    name = _STATUS_NAMES.get(status)
    return t(name) if name is not None else status


def air_day(episode: Episode, now: datetime) -> date | None:
    """The day the episode airs in `now`'s time zone (its airstamp), else its listed air date."""
    if episode.airstamp is not None:
        return episode.airstamp.astimezone(now.tzinfo).date()
    return episode.air_date


def short_date(day: date) -> str:
    """"17 Jan"."""
    return f"{day.day} {month_short(day)}"


def next_line(episode: Episode | None, now: datetime) -> str | None:
    """"Next: S02E04 · Thu 16 Oct" ("Next: S02E04" without a date); None without an episode."""
    if episode is None:
        return None
    day = air_day(episode, now)
    if day is None:
        return t("Next: {code}", code=episode.code)
    when = f"{day_short(day)} {short_date(day)}"
    return t("Next: {code} · {when}", code=episode.code, when=when)


def season_numbers(episodes: Iterable[Episode]) -> list[int]:
    """The show's seasons in order, specials (season 0) left out."""
    return sorted({episode.season for episode in episodes if episode.season > 0})


def default_season(details: ShowDetails, now: datetime) -> int | None:
    """The season shown first: the latest season while it has missing episodes, else the next
    episode's, else the latest with an aired episode, else the first; None when the show lists
    no seasons."""
    seasons = season_numbers(details.episodes)
    if not seasons:
        return None
    if details.missing and details.latest_season in seasons:
        return details.latest_season
    upcoming = details.next_episode
    if upcoming is not None and upcoming.season in seasons:
        return upcoming.season
    aired = [e.season for e in details.episodes if e.season > 0 and e.has_aired(now)]
    return max(aired) if aired else seasons[0]


def season_episodes(episodes: Iterable[Episode], season: int) -> list[Episode]:
    """The season's episodes as listed: by number, then the unnumbered ones by air date."""
    return sorted(
        (episode for episode in episodes if episode.season == season),
        key=lambda e: (e.number is None, e.number or 0, e.air_date or date.max),
    )


def episode_row_state(episode: Episode, *, downloaded: bool, now: datetime) -> RowState:
    """upcoming (not aired yet), downloaded (aired and in the history) or downloadable."""
    if not episode.has_aired(now):
        return "upcoming"
    return "downloaded" if downloaded else "downloadable"


def row_mark(details: ShowDetails, episode: Episode) -> Mark | None:
    """The chip a row of a tracked show's latest season gets: "missing" (see
    app.services.missing), "new" (aired less than a day ago, not missing yet), "have" (marked
    "I already have this" and not downloaded), else None. Older seasons never get one."""
    if not details.tracked or episode.season != details.latest_season:
        return None
    if details.is_missing(episode):
        return "missing"
    if details.is_new(episode):
        return "new"
    if details.has_it(episode) and not details.is_downloaded(episode):
        return "have"
    return None


def season_marks(details: ShowDetails, season: int | None) -> tuple[list[int], list[int]]:
    """The ids of `season`'s missing episodes (oldest first) and of its episodes marked "I
    already have this"; both empty unless it is the tracked show's latest season."""
    if not details.tracked or season is None or season != details.latest_season:
        return [], []
    episodes = season_episodes(details.episodes, season)
    missing = [e.id for e in episodes if details.is_missing(e)]
    have = [e.id for e in episodes if row_mark(details, e) == "have"]
    return missing, have


SeasonAction = Literal["mark", "unmark"]


def season_menu_items(missing: Sequence[int], have: Sequence[int]) -> list[tuple[str, SeasonAction]]:
    """The season bar's ⋯ menu: "I already have this season" while episodes are missing,
    "Mark as missing again" while some are marked."""
    items: list[tuple[str, SeasonAction]] = []
    if missing:
        items.append((t(HAVE_SEASON_TEXT), "mark"))
    if have:
        items.append((t(MARK_MISSING_TEXT), "unmark"))
    return items


def new_text(episode: Episode, now: datetime) -> str:
    """A new episode's blue chip: "New · aired today", "New · aired yesterday" (in `now`'s
    time zone), else just "New"."""
    day = air_day(episode, now)
    days = (now.date() - day).days if day is not None else None
    if days == 0:
        return t(NEW_TODAY_TEXT)
    if days == 1:
        return t(NEW_YESTERDAY_TEXT)
    return t(NEW_TEXT)


def mark_text(mark: Mark, episode: Episode, now: datetime) -> str:
    """The chip's text: "Missing", "✓ Have it" or "New · aired today"."""
    if mark == "missing":
        return t(CHIP_MISSING_TEXT)
    if mark == "have":
        return t(CHIP_HAVE_TEXT)
    return new_text(episode, now)


def mark_color(mark: Mark) -> theme.Color:
    """Amber for missing, blue for new, muted for "✓ Have it"."""
    if mark == "missing":
        return MISSING_COLOR
    if mark == "new":
        return theme.LINK  # a quiet blue: aired, a release is on its way, nothing missing yet
    return theme.TEXT_MUTED


def mark_slot_width(measure: Measure) -> int:
    """The chips' slot (logical units, in the current language): room for the widest of
    "Missing", "✓ Have it" and the new chips, so a season's rows stay aligned."""
    texts = (CHIP_MISSING_TEXT, CHIP_HAVE_TEXT, NEW_TODAY_TEXT, NEW_YESTERDAY_TEXT, NEW_TEXT)
    widest = max(round(measure(t(text))) for text in texts)
    return max(MARK_WIDTH, widest + 2 * MARK_PAD)


def upcoming_text(episode: Episode, now: datetime) -> str:
    """When an episode still to air airs, counted in days from today: "airs today", "airs
    tomorrow", "airs in 7 days"; "" when unknown (the date column says "TBA")."""
    day = air_day(episode, now)
    if day is None:
        return ""
    days = (day - now.date()).days
    if days <= 0:
        return t(AIRS_TODAY_TEXT)
    if days == 1:
        return t(AIRS_TOMORROW_TEXT)
    return tn("airs in {n} day", "airs in {n} days", days)


def episode_label(target: TvTarget, episodes: Iterable[Episode]) -> str:
    """The "← Episodes" bar's label: "S03E05 · The Crossing" ("S03E05" while its name isn't
    known), "Season 3 · Pack" for a season pack."""
    if target.episode is None:
        return f"{t('Season {n}', n=target.season)} · {t('Pack')}"
    code = target_code(target)
    for episode in episodes:
        if episode.season == target.season and episode.number == target.episode:
            return f"{code} · {episode.name}" if episode.name else code
    return code


def date_text(episode: Episode, now: datetime) -> str:
    """The row's muted date ("17 Jan"), aired or not; "TBA" when unknown."""
    day = air_day(episode, now)
    return short_date(day) if day is not None else t(UNKNOWN_DATE_TEXT)


_exists = exists


# -- one episode row ---------------------------------------------------------------------------


class _EpisodeRow(ctk.CTkFrame):
    """"S02E01  Title  17 Jan" with its state on the right: ⬇ Auto + ☰ Choose…, ✓ Downloaded +
    ↻ Again (app/ui/row_actions.py), or when it airs ("airs in 7 days"). A downloaded row (and
    its ✓) opens ↻ Again's menu when clicked; beside its title the subtitles (CC ▾, app/ui/
    subtitle_control.py). In a tracked show's latest season, a chip left of that: amber
    "Missing", blue "New · aired today" or muted "✓ Have it", each opening a menu (`on_mark`)."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        state: RowState,
        now: datetime,
        actions: DownloadActions[CalendarEntry],
        mark: Mark | None = None,
        marks_shown: bool = False,
        on_mark: Callable[[Episode, Mark, Any], None] | None = None,
        cc: str = "",
        subtitles: SubtitleActions | None = None,
        slot_width: int = SLOT_WIDTH,
        mark_width: int = MARK_WIDTH,
    ) -> None:
        super().__init__(
            master, fg_color=theme.SURFACE, corner_radius=8, height=EPISODE_ROW_HEIGHT
        )
        self._holder_width = 0  # physical px, from the title holder's last <Configure>
        self._fit_job: str | None = None
        self.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._actions = actions
        self._on_mark = on_mark
        self._mark_width = mark_width
        self._state: RowState | None = None
        self._mark: tuple[Mark | None, bool, str] | None = None
        self._mark_slot: ctk.CTkFrame | None = None
        self.row_actions: RowActions[CalendarEntry] | None = None
        self._upcoming: ctk.CTkLabel | None = None
        episode = entry.episode

        code = ctk.CTkLabel(
            self,
            text=episode.code if episode.number is not None else t("Special"),
            width=CODE_WIDTH,
            font=theme.font(13),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        code.grid(row=0, column=0, padx=(14, 6), sticky="w")
        holder = ctk.CTkFrame(self, fg_color="transparent", height=24)
        holder.grid(row=0, column=1, sticky="ew")
        holder.pack_propagate(False)
        title_font = theme.font(15)
        title = ctk.CTkLabel(
            holder, text="", font=title_font, text_color=theme.TEXT, anchor="w", height=24
        )
        title.pack(side="left", fill="x", expand=True)
        # The subtitles right of the title (downloaded rows only): CC ▾ or the plain mark.
        self.cc_control = CCControl(
            holder,
            key=episode_key(entry.show.id, episode.season, episode.number),
            actions=subtitles if episode.number is not None else None,
            on_change=self._cc_changed,
        )
        name = episode.name

        def fit(width: int) -> None:
            if width > 1:
                self._holder_width = width
                room = unscaled_width(holder, width) - SLACK
                if self.cc_control.winfo_manager():
                    cc_width = self.cc_control.winfo_reqwidth()
                    room -= unscaled_width(holder, cc_width) + CC_GAP
                title.configure(text=elide_to_width(name, room, title_font.measure))

        self._fit_title = fit
        self._title = title
        holder.bind("<Configure>", lambda event: fit(event.width))
        self._date = ctk.CTkLabel(
            self, text="", width=DATE_WIDTH, font=theme.font(13), text_color=theme.TEXT_MUTED,
            anchor="e",
        )
        self._date.grid(row=0, column=2, padx=(8, 12), sticky="e")
        self._slot = ctk.CTkFrame(
            self, fg_color="transparent", width=slot_width, height=CONTROL_HEIGHT
        )
        self._slot.pack_propagate(False)
        self._slot.grid(row=0, column=4, padx=(0, 10))
        # A downloaded row opens ↻ Again's menu wherever it is clicked (not on its buttons).
        for widget in (self, code, holder, title, self._date):
            widget.bind("<Button-1>", self._row_clicked, add="+")
        self.set_state(entry, state, now, mark=mark, marks_shown=marks_shown, cc=cc)

    def set_state(
        self,
        entry: CalendarEntry,
        state: RowState,
        now: datetime,
        *,
        mark: Mark | None = None,
        marks_shown: bool = False,
        cc: str = "",
    ) -> None:
        """Show `state` on the right and `mark`'s chip (in a slot kept while `marks_shown`, so
        the season's rows stay aligned), and `cc` ("CC EN NL") beside the title of a
        downloaded row; nothing changes when they already show."""
        episode = entry.episode
        self._set_mark(episode, mark, marks_shown, now)
        self.cc_control.show(cc, shown=state == "downloaded" and episode.number is not None)
        self._date.configure(text=date_text(episode, now))
        if state == self._state:
            if state == "upcoming" and self._upcoming is not None:
                self._upcoming.configure(text=upcoming_text(episode, now))
            return
        self._state = state
        downloaded = state == "downloaded"
        cursor = "hand2" if downloaded else ""
        for widget in (self, self._title, self._date):
            widget.configure(cursor=cursor)
        if state == "upcoming":
            if self.row_actions is not None:
                self.row_actions.destroy()
                self.row_actions = None
            if self._upcoming is None:
                self._upcoming = ctk.CTkLabel(
                    self._slot,
                    text="",
                    height=CONTROL_HEIGHT,
                    font=theme.font(14),
                    text_color=theme.TEXT_MUTED,
                    anchor="e",
                )
                self._upcoming.pack(fill="both", expand=True, padx=(0, 6))
            self._upcoming.configure(text=upcoming_text(episode, now))
            return
        if self._upcoming is not None:
            self._upcoming.destroy()
            self._upcoming = None
        # A special has no number to download by: its row shows its season pack's releases.
        base = Base("search") if episode.number is None else Base("download")
        if self.row_actions is None:
            self.row_actions = RowActions(
                self._slot,
                item=entry,
                key=episode_key(entry.show.id, episode.season, episode.number),
                base=base,
                actions=self._actions,
                downloaded=downloaded,
            )
            self.row_actions.pack(side="right", fill="x", expand=True)
        else:
            self.row_actions.set_base(base, downloaded=downloaded)

    @property
    def state(self) -> RowState | None:
        return self._state

    @property
    def mark(self) -> Mark | None:
        return None if self._mark is None else self._mark[0]

    @property
    def cc(self) -> str:
        """The subtitle mark showing ("CC EN NL"), "" for none (or a row not downloaded)."""
        return self.cc_control.mark if self.cc_control.has_content else ""

    def _row_clicked(self, _event: Any = None) -> None:
        if self._state == "downloaded" and self.row_actions is not None:
            self.row_actions.open_again()

    def _cc_changed(self) -> None:
        """The subtitles beside the title changed: show or hide them, then fit the title
        again once laid out."""
        cc = self.cc_control
        if cc.has_content:
            if not cc.winfo_manager():
                cc.pack(side="right", padx=(CC_GAP, 0), before=self._title)
        elif cc.winfo_manager():
            cc.pack_forget()
        if self._fit_job is not None or not self._holder_width:
            return

        def refit() -> None:
            self._fit_job = None
            if self._holder_width:
                self._fit_title(self._holder_width)

        self._fit_job = self.after_idle(refit)

    def _set_mark(
        self, episode: Episode, mark: Mark | None, marks_shown: bool, now: datetime
    ) -> None:
        text = mark_text(mark, episode, now) if mark is not None else ""
        if (mark, marks_shown, text) == self._mark:
            return
        self._mark = (mark, marks_shown, text)
        if not marks_shown and mark is None:
            if self._mark_slot is not None:
                self._mark_slot.destroy()
                self._mark_slot = None
            return
        if self._mark_slot is None:
            self._mark_slot = ctk.CTkFrame(
                self, fg_color="transparent", width=self._mark_width, height=MARK_HEIGHT
            )
            self._mark_slot.pack_propagate(False)
            self._mark_slot.grid(row=0, column=3, padx=(0, 8))
        for child in self._mark_slot.winfo_children():
            child.destroy()
        if mark is None:
            return
        on_mark = self._on_mark
        button = chip(
            self._mark_slot,
            text,
            mark_color(mark),
            height=MARK_HEIGHT,
            font_size=13,
            width=self._mark_width,
            clickable=on_mark is not None,
        )
        button.pack(fill="both", expand=True)
        if on_mark is not None:
            button.configure(command=functools.partial(on_mark, episode, mark, button))
            add_tooltip(button, t(NEW_TIP) if mark == "new" else t(CHIP_TIP))

    def destroy(self) -> None:
        if self._fit_job is not None:
            try:
                self.after_cancel(self._fit_job)
            except tk.TclError:
                pass
            self._fit_job = None
        super().destroy()


# -- the window --------------------------------------------------------------------------------


class ShowDetailsWindow(TitleInfoWindow):
    """A TVMaze show's info window (not modal); `reload()` refetches its details (library or
    downloads changed). Opened through `ShowWindows`."""

    def __init__(
        self,
        master: ctk.CTk,
        show: Show,
        *,
        library: LibraryService,
        images: ImageCache,
        status: StatusReporter,
        episode_actions: DownloadActions[CalendarEntry],
        missing_actions: MissingActions | None = None,
        subtitle_marks: SubtitleMarks | None = None,
        discovery: DiscoveryService | None = None,
        subtitle_actions: SubtitleActions | None = None,
        layout: InfoLayout = InfoLayout.SIDE,
        releases: PanelServices | None = None,
        on_fix_match: Callable[[TvTarget], None] | None = None,
    ) -> None:
        """`subtitle_marks`: per (season, episode) of a show, its subtitle mark ("CC EN NL");
        without it no row gets one. `discovery`: the TMDB extras (backdrop, trailer, cast);
        without it the window shows only what TVMaze knows. `subtitle_actions`: the downloaded
        rows' CC ▾ menus (without it only the marks). `releases`: what the releases panel
        works with (without it "☰ Choose…" shows nothing here); `on_fix_match`: the panel's
        "✎ Fix Match" found the right show (its window should open on that target)."""
        super().__init__(
            master,
            title=show.name,
            images=images,
            poster=lambda width: tvmaze_poster_url(show, width),
            layout=layout,
            releases=releases,
            episodes=True,
            on_fix_match=on_fix_match,
        )
        self._show = show
        self._discovery = discovery
        self._extras: TitleExtras | None = None
        self._extras_task: asyncio.Task[None] | None = None
        self._subtitle_marks = subtitle_marks
        self._subtitle_actions = subtitle_actions
        self._cc: Mapping[tuple[int, int], str] = {}
        # The rows' slots, wide enough for their texts in the current language.
        self._slot_width = max(
            SLOT_WIDTH, actions_slot_width(theme.font(ACTIONS_FONT_SIZE, bold=True).measure)
        )
        self._mark_width = mark_slot_width(theme.font(13, bold=True).measure)
        self._missing_actions = missing_actions
        self._missing_button: MissingButton | None = None
        self._library = library
        self._images = images
        self._status = status
        self._episode_actions = episode_actions
        self._details: ShowDetails | None = None
        self._season: int | None = None
        self._seasons: list[int] = []
        self._chips: dict[int, ctk.CTkButton] = {}
        self._rows: dict[int, _EpisodeRow] = {}  # by episode id
        self._row_episodes: tuple[Episode, ...] = ()  # what the rows show, in order
        self._load_task: asyncio.Task[None] | None = None
        self._rows_task: asyncio.Task[None] | None = None
        self._tracking = False
        self._buttons_look: tuple[bool, bool, bool, str | None] | None = None
        self._auto_var = ctk.BooleanVar(value=False)
        self._auto_switch: ctk.CTkSwitch | None = None
        self._buttons = self.buttons

        content = self.content
        self._chip_grid = FlexGrid(
            content, item_width=CHIP_WIDTH, pad_x=4, pad_y=4, center=False
        )
        self._chip_grid.pack(fill="x")
        self._season_bar = self._build_season_bar(content)
        self._episode_list = ctk.CTkFrame(content, fg_color="transparent")
        self._episode_list.pack(fill="x", pady=(12, 0))
        LoadingBar(self._episode_list).pack(fill="x", pady=(8, 0))
        self._paint_header(show, None)

        self.reload()
        if discovery is not None:
            # The backdrop's room stays while the extras may still bring one.
            self.masthead.reserve_hero(discovery.has_tmdb_key)
            self._extras_task = spawn(self._load_extras(discovery), name="ShowDetailsWindow.extras")
        else:
            self._show_extras(None)

    # -- releases ----------------------------------------------------------------------------

    def choose(self, target: TvTarget) -> ReleasePanel | None:
        """"☰ Choose…": `target`'s releases in this window (see TitleInfoWindow.open_releases)."""
        panel = self.open_releases(target)
        if panel is not None:
            panel.focus_table()
        return panel

    def target_text(self, target: IndexerTarget) -> str:
        if isinstance(target, TvTarget):
            details = self._details
            return episode_label(target, details.episodes if details is not None else ())
        return super().target_text(target)

    # -- layout ------------------------------------------------------------------------------

    def _build_season_bar(self, master: Any) -> ctk.CTkFrame:
        """"3 missing" + "⬇ Download missing (3)" + ⋯, above the latest season's rows (packed
        while it applies, see `_paint_season_bar`)."""
        bar = ctk.CTkFrame(master, fg_color="transparent", height=CONTROL_HEIGHT)
        self._missing_label = ctk.CTkLabel(
            bar, text="", font=theme.font(15, bold=True), text_color=MISSING_COLOR, anchor="w"
        )
        self._missing_label.pack(side="left")
        self._season_more = ctk.CTkButton(
            bar,
            text="⋯",
            width=CONTROL_HEIGHT,
            height=CONTROL_HEIGHT,
            corner_radius=CONTROL_RADIUS,
            font=theme.font(CONTROL_FONT_SIZE, bold=True),
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
            command=self._open_season_menu,
        )
        add_tooltip(self._season_more, t(SEASON_MENU_TIP))
        if self._missing_actions is not None:
            self._missing_button = MissingButton(
                bar,
                show=self._show,
                count=0,
                actions=self._missing_actions,
                with_count=True,
                height=CONTROL_HEIGHT,
                font_size=CONTROL_FONT_SIZE,
                width=MISSING_BUTTON_WIDTH,
            )
        return bar

    def _paint_header(self, show: Show, details: ShowDetails | None) -> None:
        self._show = show
        self._set_name(show.name)
        seasons = len(season_numbers(details.episodes)) if details is not None else None
        self._set_meta(meta_line(show, seasons or None))
        self.set_genres(genre_names(show, self._extras))
        self._set_summary(synopsis(show, self._extras))
        self._set_note(next_line(details.next_episode, _now()) if details is not None else None)
        if details is not None:
            self._paint_buttons(details)

    # -- TMDB's extras -----------------------------------------------------------------------

    async def _load_extras(self, discovery: DiscoveryService) -> None:
        try:
            extras = await discovery.show_extras(self._show)
        except TMDBNotConfigured:
            if self.winfo_exists():
                self.masthead.set_hero(None)
                self.show_hint(True)
            return
        except TMDBError as exc:
            log.info("No TMDB extras for TVMaze show %d: %s", self._show.id, exc)
            extras = None
        if self.winfo_exists():
            self._show_extras(extras)

    def _show_extras(self, extras: TitleExtras | None) -> None:
        """The backdrop (TMDB's, else the poster blurred, else none), the trailer, the cast,
        and the synopsis and genres in the app's language."""
        self._extras = extras
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        backdrop = extras.info.backdrop_url(self.backdrop_size()) if extras is not None else None
        if backdrop is not None:
            self.masthead.set_hero(backdrop)
        elif self._discovery is not None and self._discovery.has_tmdb_key:
            self.masthead.set_hero(tvmaze_poster_url(self._show, round(210 * scaling)), blur=True)
        else:
            self.masthead.set_hero(None)
        trailer = extras.trailer if extras is not None else None
        self.masthead.set_trailer(trailer.watch_url if trailer is not None else None)
        self.show_cast(extras.cast if extras is not None else ())
        self._paint_header(self._show, self._details)

    def _paint_buttons(self, details: ShowDetails) -> None:
        """+ Track, or ✓ Following with the "Auto" switch (new episodes download by themselves;
        the hover tip says so); then IMDb."""
        imdb_url = details.show.imdb_url
        look = (details.tracked, details.auto_download, self._tracking, imdb_url)
        if look == self._buttons_look:
            return
        self._buttons_look = look
        self._auto_switch = None
        items: list[Any] = []
        if details.tracked:
            following = ctk.CTkButton(
                self._buttons,
                text=t(FOLLOWING_TEXT),
                width=140,
                height=dialog_kit.BUTTON_HEIGHT,
                corner_radius=dialog_kit.BUTTON_RADIUS,
                fg_color="transparent",
                border_width=1,
                border_color=theme.SUCCESS,
                text_color=theme.SUCCESS,
                text_color_disabled=theme.SUCCESS,
                font=theme.font(15, bold=True),
                state="disabled",
            )
            items.append(following)
            self._auto_var.set(details.auto_download)
            self._auto_switch = ctk.CTkSwitch(
                self._buttons,
                text=t(AUTO_SWITCH_TEXT),
                variable=self._auto_var,
                font=theme.font(14),
                text_color=auto_switch_color(details.auto_download),
                progress_color=theme.ACCENT,
                command=self._auto_toggled,
            )
            items.append(self._auto_switch)
            add_tooltip(self._auto_switch, t(AUTO_SWITCH_TIP))
        else:
            track = dialog_kit.primary_button(self._buttons, t(TRACK_TEXT), self._track)
            if self._tracking:
                track.configure(text=t(ADDING_TEXT), state="disabled")
            items.append(track)
        if imdb_url is not None:

            def open_imdb() -> None:
                webbrowser.open(imdb_url)

            items.append(dialog_kit.secondary_button(self._buttons, "IMDb", open_imdb, width=90))
        self._buttons.set_items(items, destroy_old=True)

    # -- loading -----------------------------------------------------------------------------

    def reload(self) -> None:
        """Fetch the details again (a load still in flight is dropped)."""
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = spawn(self._load(), name="ShowDetailsWindow.load")

    async def _load(self) -> None:
        try:
            details = await self._library.show_details(self._show.id)
        except TVMazeError as exc:
            log.warning("Show details of TVMaze show %d: %s", self._show.id, exc)
            if self.winfo_exists() and self._details is None:
                self._episodes_message(
                    t("Couldn't load the episodes: {error}", error=exc), theme.DANGER
                )
            return
        if self._subtitle_marks is not None:
            self._cc = await self._subtitle_marks(details.show.id)
        if self.winfo_exists():
            self._apply(details)

    def _cc_for(self, episode: Episode) -> str:
        if episode.number is None:
            return ""
        return self._cc.get((episode.season, episode.number), "")

    def _apply(self, details: ShowDetails) -> None:
        self._details = details
        self._paint_header(details.show, details)
        if self.panel is not None and exists(self.panel):
            self._paint_releases_title(self.panel.target)  # the episode's name is known now
        seasons = season_numbers(details.episodes)
        if seasons != self._seasons:
            self._seasons = seasons
            self._render_chips()
        if self._season not in seasons:
            self._season = default_season(details, _now())
            self._paint_chips()
        self._show_season()

    def _episodes_message(self, text: str, color: theme.Color) -> None:
        self._cancel_rows()
        for child in self._episode_list.winfo_children():
            child.destroy()
        self._rows = {}
        self._row_episodes = ()
        dialog_kit.text_label(
            self._episode_list, text, size=dialog_kit.DETAIL_SIZE, color=color
        ).pack(anchor="w", pady=(8, 0))

    # -- seasons -----------------------------------------------------------------------------

    def _render_chips(self) -> None:
        for chip in self._chips.values():
            chip.destroy()
        self._chips = {
            season: ctk.CTkButton(
                self._chip_grid,
                text=t("Season {n}", n=season),
                width=CHIP_WIDTH,
                height=CHIP_HEIGHT,
                corner_radius=CHIP_HEIGHT // 2,
                font=theme.font(14),
                fg_color=theme.CHIP,
                hover_color=theme.CHIP_HOVER,
                text_color=theme.BUTTON_TEXT,
                command=lambda s=season: self._select_season(s),
            )
            for season in self._seasons
        }
        self._chip_grid.relayout()
        self._paint_chips()

    def _paint_chips(self) -> None:
        for season, chip in self._chips.items():
            chip.configure(**theme.choice_colors(season == self._season))

    def _select_season(self, season: int) -> None:
        if season != self._season:
            self._season = season
            self._paint_chips()
            self._show_season()

    # -- episodes ----------------------------------------------------------------------------

    def _show_season(self) -> None:
        """The selected season's rows: updated in place when they show the same episodes, else
        built anew."""
        details = self._details
        if details is None:
            return
        if self._season is None:
            self._paint_season_bar()
            self._episodes_message(t(NO_EPISODES_TEXT), theme.TEXT_MUTED)
            return
        self._paint_season_bar()
        episodes = tuple(season_episodes(details.episodes, self._season))
        now = _now()
        if episodes == self._row_episodes and self._rows:
            marks_shown = self._marks_shown()
            for episode in episodes:
                row = self._rows.get(episode.id)
                if row is not None and row.winfo_exists():
                    row.set_state(
                        self._entry(episode),
                        self._row_state(episode, now),
                        now,
                        mark=row_mark(details, episode),
                        marks_shown=marks_shown,
                        cc=self._cc_for(episode),
                    )
            return
        self._cancel_rows()
        for child in self._episode_list.winfo_children():
            child.destroy()
        self._rows = {}
        self._row_episodes = episodes
        self._rows_task = spawn(self._build_rows(episodes), name="ShowDetailsWindow.rows")

    async def _build_rows(self, episodes: Sequence[Episode]) -> None:
        """Build the rows a batch at a time, so a long season doesn't freeze the window."""
        now = _now()
        on_mark = self._open_mark_menu if self._missing_actions is not None else None
        for start in range(0, len(episodes), ROWS_PER_BATCH):
            if start:
                await asyncio.sleep(0.001)
                if not self.winfo_exists():
                    return
            details = self._details
            assert details is not None
            marks_shown = self._marks_shown()
            for episode in episodes[start : start + ROWS_PER_BATCH]:
                row = _EpisodeRow(
                    self._episode_list,
                    self._entry(episode),
                    state=self._row_state(episode, now),
                    now=now,
                    actions=self._episode_actions,
                    mark=row_mark(details, episode),
                    marks_shown=marks_shown,
                    on_mark=on_mark,
                    cc=self._cc_for(episode),
                    subtitles=self._subtitle_actions,
                    slot_width=self._slot_width,
                    mark_width=self._mark_width,
                )
                row.pack(fill="x", pady=3)
                self._rows[episode.id] = row

    def _entry(self, episode: Episode) -> CalendarEntry:
        """What the Download row works on (the calendar's one-click and search paths)."""
        details = self._details
        assert details is not None
        return CalendarEntry(
            show=details.show,
            episode=episode,
            is_tracked=details.tracked,
            is_downloaded=details.is_downloaded(episode),
        )

    def _row_state(self, episode: Episode, now: datetime) -> RowState:
        details = self._details
        assert details is not None
        return episode_row_state(episode, downloaded=details.is_downloaded(episode), now=now)

    # -- missing episodes --------------------------------------------------------------------

    def _marks_shown(self) -> bool:
        """Whether the rows keep a slot for the "Missing" / "✓ Have it" chips: the tracked
        show's latest season is selected (and missing episodes are shown at all)."""
        details = self._details
        return (
            self._missing_actions is not None
            and details is not None
            and details.tracked
            and self._season is not None
            and self._season == details.latest_season
        )

    def _paint_season_bar(self) -> None:
        """Shown on a tracked show's latest season while episodes are missing or marked "I
        already have this", or a "Download missing" run has something to tell."""
        details, actions = self._details, self._missing_actions
        if details is None or actions is None or not self._marks_shown():
            self._season_bar.pack_forget()
            return
        missing, have = season_marks(details, self._season)
        if not (missing or have or actions.runs.get(details.show.id) is not None):
            self._season_bar.pack_forget()
            return
        self._missing_label.configure(
            text=missing_text(len(missing)) if missing else t(NOTHING_MISSING_TEXT),
            text_color=MISSING_COLOR if missing else theme.TEXT_MUTED,
        )
        self._season_more.pack_forget()
        if self._missing_button is not None:
            self._missing_button.pack_forget()
        if season_menu_items(missing, have):
            self._season_more.pack(side="right")
        if self._missing_button is not None:
            self._missing_button.set_count(len(missing))
            self._missing_button.pack(side="right", padx=(0, 8))
        if not self._season_bar.winfo_manager():
            self._season_bar.pack(fill="x", pady=(12, 0), before=self._episode_list)

    def _open_season_menu(self) -> None:
        details, actions = self._details, self._missing_actions
        if details is None or actions is None:
            return
        missing, have = season_marks(details, self._season)
        show = details.show
        items: list[MenuItem] = []
        for label, kind in season_menu_items(missing, have):
            ids = tuple(missing if kind == "mark" else have)
            act = actions.mark_have if kind == "mark" else actions.unmark_have
            items.append((label, functools.partial(act, show, ids)))
        popup_menu(self._season_more, items)

    def _open_mark_menu(self, episode: Episode, mark: Mark, anchor: Any) -> None:
        details, actions = self._details, self._missing_actions
        if details is None or actions is None:
            return
        ids = (episode.id,)
        if mark in ("missing", "new"):
            item = (t(HAVE_EPISODE_TEXT), functools.partial(actions.mark_have, details.show, ids))
        else:
            item = (
                t(MARK_MISSING_TEXT), functools.partial(actions.unmark_have, details.show, ids)
            )
        popup_menu(anchor, [item])

    def _cancel_rows(self) -> None:
        if self._rows_task is not None and not self._rows_task.done():
            self._rows_task.cancel()
        self._rows_task = None

    # -- tracking ----------------------------------------------------------------------------

    def _track(self) -> None:
        if self._tracking or self._details is None or self._details.tracked:
            return
        self._tracking = True
        self._paint_buttons(self._details)
        # Not cancelled with the window: closing it doesn't take back the click.
        spawn(self._track_show(self._show), name="ShowDetailsWindow.track")

    async def _track_show(self, show: Show) -> None:
        try:
            await self._library.track(show.id)
        except TVMazeError as exc:
            self._report(t("Couldn't track {name}: {error}", name=show.name, error=exc), 6000)
        else:
            self._report(t("Now tracking {name}", name=show.name), 4000)
        finally:
            self._tracking = False
        # The library's change reloads every details window; this one may be gone by now.
        if self.winfo_exists():
            self.reload()

    def _auto_toggled(self) -> None:
        on = bool(self._auto_var.get())
        if self._auto_switch is not None:
            self._auto_switch.configure(text_color=auto_switch_color(on))
        spawn(self._set_auto(self._show, on), name="ShowDetailsWindow.auto-download")

    async def _set_auto(self, show: Show, on: bool) -> None:
        try:
            await self._library.set_auto_download(show.id, on)
        except Exception:
            log.exception("Switching automatic downloads for TVMaze show %d failed", show.id)
            text = (
                t("Could not switch automatic downloads on for {name}", name=show.name)
                if on
                else t("Could not switch automatic downloads off for {name}", name=show.name)
            )
            self._report(text, 8000)
            if self.winfo_exists():
                self.reload()  # shows the switch as it really is

    def _report(self, text: str, clear_after_ms: int) -> None:
        # The status bar belongs to the main window, which may be gone by now.
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        if self._load_task is not None:
            self._load_task.cancel()
        if self._extras_task is not None:
            self._extras_task.cancel()
        self._cancel_rows()
        super().destroy()


def _now() -> datetime:
    return datetime.now().astimezone()


# -- opening -----------------------------------------------------------------------------------


class ShowWindows:
    """Opens show details windows, one per TVMaze show: opening an open show raises its window.
    Every open window reloads when the library or the download history changes."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        library: LibraryService,
        discovery: DiscoveryService,
        images: ImageCache,
        status: StatusReporter,
        episode_actions: DownloadActions[CalendarEntry],
        downloads_changed: Subscribe,
        missing_actions: MissingActions | None = None,
        subtitle_marks: SubtitleMarks | None = None,
        subtitles_changed: Subscribe | None = None,
        subtitle_actions: SubtitleActions | None = None,
        releases: PanelServices | None = None,
    ) -> None:
        """`subtitle_marks` / `subtitles_changed`: the rows' "CC EN NL" marks and when they
        change (subtitles saved); `subtitle_actions`: their CC ▾ menus. `releases`: the
        releases panels' services; its settings say the layout a new window opens in."""
        self._master = master
        self._missing_actions = missing_actions
        self._subtitle_marks = subtitle_marks
        self._subtitle_actions = subtitle_actions
        self._library = library
        self._discovery = discovery
        self._images = images
        self._status = status
        self._episode_actions = episode_actions
        self._releases = releases
        self._windows: dict[int, ShowDetailsWindow] = {}  # by TVMaze id
        self._resolving: set[int] = set()  # TMDB ids being matched to TVMaze right now
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe = [library.subscribe(self.reload_all), downloads_changed(self.reload_all)]
        if subtitles_changed is not None:
            self._unsubscribe.append(subtitles_changed(self.reload_all))

    def layout(self) -> InfoLayout:
        """The layout a window opens in now (Settings)."""
        if self._releases is None:
            return InfoLayout.SIDE
        layout: InfoLayout = self._releases.settings.current.info_layout
        return layout

    def open_show(self, show: Show, *, target: TvTarget | None = None) -> None:
        """Open the details of a TVMaze show, or raise its window; with `target` (☰ Choose…)
        on that episode's (or season pack's) releases."""
        window = self._live().get(show.id)
        if window is None:
            window = self._windows[show.id] = self._new_window(show)
        else:
            window.raise_window()
        if target is not None:
            window.choose(target)

    def _fix_match(self, target: TvTarget) -> None:
        """"✎ Fix Match" in a window's releases found the right show: its window, on the same
        episode."""
        self.open_show(target.show, target=target)

    def _new_window(self, show: Show) -> ShowDetailsWindow:
        return ShowDetailsWindow(
            self._master,
            show,
            library=self._library,
            images=self._images,
            status=self._status,
            episode_actions=self._episode_actions,
            missing_actions=self._missing_actions,
            subtitle_marks=self._subtitle_marks,
            discovery=self._discovery,
            subtitle_actions=self._subtitle_actions,
            layout=self.layout(),
            releases=self._releases,
            on_fix_match=self._fix_match,
        )

    def open_discover(self, show: DiscoverShow) -> None:
        """Open the details of a TMDB show through its TVMaze counterpart; the status bar says
        when there is none."""
        if show.id in self._resolving:
            return
        self._resolving.add(show.id)
        self._report(t("Opening {name}…", name=show.name), 3000)
        self._start(self._open_discover(show), "ShowWindows.open_discover")

    async def _open_discover(self, show: DiscoverShow) -> None:
        try:
            match = await self._discovery.resolve(show)
        except NoTVMazeMatch:
            self._report(t(NOT_ON_TVMAZE_TEXT), 6000)
            return
        except (TMDBError, TVMazeError) as exc:
            self._report(t("Couldn't open {name}: {error}", name=show.name, error=exc), 8000)
            return
        finally:
            self._resolving.discard(show.id)
        if _exists(self._master):
            self.open_show(match)

    def reload_all(self) -> None:
        for window in self._live().values():
            window.reload()

    def close(self) -> None:
        """Stop listening, cancel lookups and close every window (the main window is closing)."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        for task in list(self._tasks):
            task.cancel()
        for window in self._live().values():
            window.destroy()
        self._windows = {}

    def _live(self) -> dict[int, ShowDetailsWindow]:
        """The windows still open (the user closes them without telling anyone)."""
        self._windows = {
            show_id: window for show_id, window in self._windows.items() if _exists(window)
        }
        return self._windows

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _report(self, text: str, clear_after_ms: int) -> None:
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # the main window is gone
