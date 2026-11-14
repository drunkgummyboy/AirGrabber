"""The series info window: the shared info look (app/ui/title_info.py: the backdrop with the
trailer, the poster, title, meta line, genres, synopsis, track state, the cast), and the show's
episodes by season, each with its download controls (⬇ Auto with ☰ Choose…: app/ui/
row_actions.py; or when it airs). Whether an episode was downloaded is never shown as such (owner's
decision for 3.1.0): only while Plex is connected does the window show what you have and miss,
and then only Plex counts as had (a quiet ✓ "In Plex"; app/services/plex.py). "☰ Choose…" shows that episode's releases (app/ui/release_panel.py)
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

Above the selected season's rows its bar (3.0.1): with Plex, on a tracked show's latest season
"3 missing", and for any season its download control (SeasonActions, in the rows' style):
"⬇ Whole season" + "☰ Choose…" once an episode has aired, also when it was grabbed before or
Plex has it all (the season pack through the download queue; when none is good enough the queue
fetches its episodes not grabbed yet instead and the status bar says so; ☰ Choose… shows the
pack's releases), with Plex "⬇ All missing (3)" in its place while it airs and episodes are
missing (exactly the ones the bar above counts, not the new ones; queued as one batch). Their looks follow the queue: "Queued", "Finding the best one…" / "Downloading 2
of 3…", then the outcome.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import Image

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
    ROW_HEIGHT as CONTROL_HEIGHT,
)
from app.ui.calendar_stack import StateOf, entry_key, needed_entries
from app.ui.download_control import (
    QUEUED_TEXT,
    WORKING_SHORT,
    WORKING_TEXT,
    Base,
    BatchState,
    DownloadActions,
    DownloadKey,
    Phase,
    RunState,
    episode_key,
    outcome_text,
)
from app.ui.missing_control import (
    CHIP_MISSING_TEXT,
    HAVE_MARK,
    MARK_DOT,
    MARK_DOT_GAP,
    MARK_FONT_SIZE,
    MISSING_COLOR,
    NOTHING_MISSING_TEXT,
    PLEX_TIP,
    mark_label,
    missing_text,
    progress_texts,
)
from app.ui.play_control import Play, play_button
from app.ui.poster_card import FOLLOWING_TEXT, TRACK_TEXT
from app.ui.release_panel import IndexerTarget, PanelServices, ReleasePanel, TvTarget
from app.ui.row_actions import NOTHING_SHORT, NOTHING_TEXT, RowLook
from app.ui.row_actions import FONT_SIZE as ACTIONS_FONT_SIZE
from app.ui.row_actions import RowActions, button_width
from app.ui.row_actions import slot_width as actions_slot_width
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, Measure, elide_to_width, unscaled_width
from app.ui.title_info import (
    SUMMARY_LINES,
    TitleInfoWindow,
    collapsed_summary,
    exists,
    join_meta,
    rating_text,
    rounded_mask,
    target_code,
)
from app.ui.tooltip import add_tooltip
from app.ui.season_progress import (
    EpisodeSquares,
    SeasonStrip,
    counts_text,
    had_text,
    progress_shown,
    season_states,
)
from app.ui.widgets import FlexGrid, LoadingBar

if TYPE_CHECKING:
    from app.models.plex import PlexTitle
    from app.services import DiscoveryService, ImageCache, LibraryService
    from app.services.playback import PlaybackService
    from app.ui.play_control import PlayerHost
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

RowState = Literal["downloadable", "upcoming"]
# With Plex, the latest season's amber "● Missing" / blue "● New · aired today" / the muted ✓ of
# an episode in the user's Plex libraries ("plex", tipped "In Plex").
Mark = Literal["missing", "new", "plex"]
Listener = Callable[[], None]
RGB = tuple[int, int, int]
Subscribe = Callable[[Listener], Callable[[], None]]  # returns the unsubscribe function

__all__ = ["SUMMARY_LINES", "ShowDetailsWindow", "ShowWindows", "collapsed_summary"]

# Logical (unscaled) CTk units.
CHIP_WIDTH = 96
CHIP_HEIGHT = 32
PROGRESS_INSET = 10  # the bar under a chip: this much in from its ends
PROGRESS_GAP = 4  # between the chip and its bar
SQUARES_GAP = 14  # before the open season's squares, and before their counts
EPISODE_ROW_HEIGHT = 52
CODE_WIDTH = 72
DATE_WIDTH = 64
SLOT_WIDTH = 200  # the row's right side (at least): its download controls, or when it airs
MARK_WIDTH = 72  # the latest season's mark, left of that (at least; `mark_slot_width`)
MARK_PAD = 12  # beyond a mark's dot and text (the text renders a little wider when scaled)
MARK_HEIGHT = 26
ROW_RADIUS = 8
STRIPE = 3  # a missing episode's amber line along its row's left edge
ROWS_PER_BATCH = 12  # rows built before Tk gets to draw and handle input again

# Shown through t().
SEASON_TRAILER = N_("Season {n} trailer")
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
# The season bar's download control (3.0.1).
WHOLE_SEASON_TEXT = N_("⬇ Whole season")
WHOLE_SEASON_TIP = N_(
    "Download the whole season in one go (a season pack). Without one, the episodes you "
    "still need are queued one by one."
)
ALL_MISSING_TEXT = N_("⬇ All missing ({n})")
ALL_MISSING_TIP = N_("Download this season's missing episodes")
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
    """The season shown first: the latest season while it has missing episodes (with Plex), else
    the next episode's, else the latest with an aired episode, else the first; None when the
    show lists no seasons."""
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


def episode_row_state(episode: Episode, *, now: datetime) -> RowState:
    """upcoming (not aired yet) or downloadable. Whether it was downloaded before is never
    shown."""
    return "downloadable" if episode.has_aired(now) else "upcoming"


def marks_shown(details: ShowDetails, season: int | None) -> bool:
    """Whether `season`'s rows keep a slot for the marks: Plex is connected and it is the
    tracked show's latest season."""
    return (
        details.plex_connected
        and details.tracked
        and season is not None
        and season == details.latest_season
    )


def row_mark(details: ShowDetails, episode: Episode) -> Mark | None:
    """The mark a row of a tracked show's latest season gets while Plex is connected:
    "missing" (see app.services.missing), "new" (aired less than a day ago, not missing yet),
    "plex" (in Plex), else None (a grabbed episode Plex doesn't have yet is on its way: no
    mark). Older seasons never get one, nor does any row without Plex."""
    if not marks_shown(details, episode.season):
        return None
    if details.is_missing(episode):
        return "missing"
    if details.is_new(episode):
        return "new"
    if details.is_in_plex(episode):
        return "plex"
    return None


def season_marks(details: ShowDetails, season: int | None) -> tuple[list[int], list[int]]:
    """The ids of `season`'s missing episodes (oldest first) and of its episodes in Plex; both
    empty unless Plex is connected and it is the tracked show's latest season."""
    if not marks_shown(details, season):
        return [], []
    assert season is not None
    episodes = season_episodes(details.episodes, season)
    missing = [e.id for e in episodes if details.is_missing(e)]
    in_plex = [e.id for e in episodes if row_mark(details, e) == "plex"]
    return missing, in_plex


# What the season bar's download control offers: "whole" = ⬇ Whole season (a season pack, its
# episodes not grabbed yet when there is none) + ☰ Choose… (the pack's releases); "missing" =
# ⬇ All missing (n) (the season's missing episodes; only with Plex, while it airs).
SeasonKind = Literal["whole", "missing"]


def season_kind(episodes: Iterable[Episode], season: int, now: datetime) -> SeasonKind | None:
    """"whole" once `season` has finished airing (it lists numbered episodes and all of them
    have aired), "missing" while it airs (some have), None before any has."""
    aired = [e.has_aired(now) for e in episodes if e.season == season and e.number is not None]
    if not any(aired):
        return None
    return "whole" if all(aired) else "missing"


def season_entries(details: ShowDetails, season: int) -> tuple[CalendarEntry, ...]:
    """The season's episodes as the download controls know them (the rows' entries)."""
    return tuple(
        CalendarEntry(
            show=details.show,
            episode=episode,
            is_tracked=details.tracked,
            is_downloaded=details.is_downloaded(episode),
        )
        for episode in season_episodes(details.episodes, season)
    )


def season_needed(
    details: ShowDetails, season: int, state_of: StateOf
) -> tuple[CalendarEntry, ...]:
    """The season's episodes still needed: aired, not downloaded (before, or by a run just
    now) and not in Plex (none are without Plex), with a number to download by; the calendar
    stack's "Download all" rule (`needed_entries`)."""
    return needed_entries(season_entries(details, season), state_of, details.in_plex)


def season_look(kind: SeasonKind, state: RunState | None, batch: BatchState | None) -> RowLook:
    """The season control's look: its pack's run ("Queued", the working label, then the
    outcome; amber "No good release found" + ☰ Choose… when no pack was good enough and no
    episode was needed instead), else the run of a batch of its episodes ("All missing", or
    the episodes the pack queued instead: "Queued" until one starts, then "Downloading 2 of
    3…"), else its buttons."""
    if state is not None and state.phase is Phase.WORKING:
        return RowLook("working", (t(WORKING_TEXT), t(WORKING_SHORT)))
    if state is not None and state.phase is Phase.QUEUED:
        return RowLook("queued", (t(QUEUED_TEXT),))
    if batch is not None:
        if batch.queued:
            return RowLook("queued", (t(QUEUED_TEXT),))
        return RowLook("working", progress_texts(batch.index, batch.total))
    if state is not None and state.phase is Phase.DONE and state.outcome is not None:
        return RowLook("done", (outcome_text(state.outcome),), theme.SUCCESS)
    if state is not None and state.phase is Phase.NOTHING:
        return RowLook(
            "nothing", (t(NOTHING_TEXT), t(NOTHING_SHORT)), theme.PENDING, ("choose",), alert=True
        )
    return RowLook("download", buttons=("auto", "choose") if kind == "whole" else ("auto",))


def new_text(episode: Episode, now: datetime) -> str:
    """A new episode's blue mark: "New · aired today", "New · aired yesterday" (in `now`'s
    time zone), else just "New"."""
    day = air_day(episode, now)
    days = (now.date() - day).days if day is not None else None
    if days == 0:
        return t(NEW_TODAY_TEXT)
    if days == 1:
        return t(NEW_YESTERDAY_TEXT)
    return t(NEW_TEXT)


def mark_text(mark: Mark, episode: Episode, now: datetime) -> str:
    """The mark's text: "Missing", "✓" (in Plex) or "New · aired today"."""
    if mark == "missing":
        return t(CHIP_MISSING_TEXT)
    if mark == "plex":
        return HAVE_MARK
    return new_text(episode, now)


def mark_color(mark: Mark) -> theme.Color:
    """Amber for missing, blue for new, muted for the ✓."""
    if mark == "missing":
        return MISSING_COLOR
    if mark == "new":
        return theme.LINK  # a quiet blue: aired, a release is on its way, nothing missing yet
    return theme.TEXT_MUTED


def mark_slot_width(measure: Measure) -> int:
    """The marks' slot (logical units, in the current language): room for the dot and the
    widest of "Missing" and the new texts (the ✓ is narrower), so a season's rows stay
    aligned."""
    texts = (CHIP_MISSING_TEXT, NEW_TODAY_TEXT, NEW_YESTERDAY_TEXT, NEW_TEXT)
    widest = max(round(measure(t(text))) for text in texts)
    return max(MARK_WIDTH, MARK_DOT + MARK_DOT_GAP + widest + MARK_PAD)


def stripe_image(
    size: tuple[int, int], radius: int, stripe: int, *, line: RGB, fill: RGB, outside: RGB
) -> Image.Image:
    """The left end of a row with rounded corners, `size` physical pixels: `line` on its first
    `stripe` columns and `fill` after them inside the corners' `radius`, `outside` beyond
    them (the row's background). RGB."""
    width, height = size
    mask = rounded_mask((2 * width + 2, height), radius).crop((0, 0, width, height))
    inside = Image.new("RGB", size, fill)
    inside.paste(line, (0, 0, stripe, height))
    out = Image.new("RGB", size, outside)
    out.paste(inside, (0, 0), mask)
    return out


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
    """"S02E01  Title  17 Jan" with its state on the right: ⬇ Auto + ☰ Choose… (app/ui/
    row_actions.py), or when it airs ("airs in 7 days"). With Plex, in a tracked show's latest
    season, a quiet mark left of that, no box: amber "● Missing" (and a thin amber line along
    the row's left edge), blue "● New · aired today" or a grey ✓ ("In Plex")."""

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
        slot_width: int = SLOT_WIDTH,
        mark_width: int = MARK_WIDTH,
    ) -> None:
        super().__init__(
            master, fg_color=theme.SURFACE, corner_radius=ROW_RADIUS, height=EPISODE_ROW_HEIGHT
        )
        self._holder_width = 0  # physical px, from the title holder's last <Configure>
        self._fit_job: str | None = None
        self.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._actions = actions
        self._mark_width = mark_width
        self._state: RowState | None = None
        self._mark: tuple[Mark | None, bool, str] | None = None
        self._mark_slot: ctk.CTkFrame | None = None
        self._stripe: ctk.CTkLabel | None = None  # a missing row's amber line
        self.row_actions: RowActions[CalendarEntry] | None = None
        self._upcoming: ctk.CTkLabel | None = None
        episode = entry.episode
        self._episode = episode

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
        name = episode.name

        def fit(width: int) -> None:
            if width > 1:
                self._holder_width = width
                room = unscaled_width(holder, width) - SLACK
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
        self.set_state(entry, state, now, mark=mark, marks_shown=marks_shown)

    def set_state(
        self,
        entry: CalendarEntry,
        state: RowState,
        now: datetime,
        *,
        mark: Mark | None = None,
        marks_shown: bool = False,
    ) -> None:
        """Show `state` on the right and `mark` (in a slot kept while `marks_shown`, so
        the season's rows stay aligned); nothing changes when they already show."""
        episode = entry.episode
        self._set_mark(episode, mark, marks_shown, now)
        self._date.configure(text=date_text(episode, now))
        if state == self._state:
            if state == "upcoming" and self._upcoming is not None:
                self._upcoming.configure(text=upcoming_text(episode, now))
            return
        self._state = state
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
            )
            self.row_actions.pack(side="right", fill="x", expand=True)
        else:
            self.row_actions.set_base(base)

    @property
    def state(self) -> RowState | None:
        return self._state

    @property
    def mark(self) -> Mark | None:
        return None if self._mark is None else self._mark[0]

    def _set_mark(
        self, episode: Episode, mark: Mark | None, marks_shown: bool, now: datetime
    ) -> None:
        self._episode = episode
        text = mark_text(mark, episode, now) if mark is not None else ""
        if (mark, marks_shown, text) == self._mark:
            return
        self._mark = (mark, marks_shown, text)
        self._paint_stripe(mark == "missing")
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
        label = mark_label(
            self._mark_slot,
            text,
            mark_color(mark),
            dot=mark != "plex",
            height=MARK_HEIGHT,
        )
        label.pack(side="left", fill="y")
        if mark == "plex":
            add_tooltip(label, t(PLEX_TIP))
        elif mark == "new":
            add_tooltip(label, t(NEW_TIP))

    def _paint_stripe(self, shown: bool) -> None:
        """A missing row's thin amber line along its left edge, inside its rounded corners: the
        row's left end baked (light and dark) with the line in it, over the row's own."""
        if not shown:
            if self._stripe is not None:
                self._stripe.destroy()
                self._stripe = None
            return
        if self._stripe is not None:
            return
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        size = (round(ROW_RADIUS * scaling), round(EPISODE_ROW_HEIGHT * scaling))
        outside = self.cget("bg_color")

        def baked(dark: bool) -> Image.Image:
            def rgb(color: Any) -> RGB:
                if isinstance(color, (list, tuple)):
                    color = color[1 if dark else 0]
                red, green, blue = self.winfo_rgb(theme.pick(str(color)))
                return red >> 8, green >> 8, blue >> 8

            return stripe_image(
                size,
                round(ROW_RADIUS * scaling),
                max(1, round(STRIPE * scaling)),
                line=rgb(MISSING_COLOR),
                fill=rgb(theme.SURFACE),
                outside=rgb(outside),
            )

        image = ctk.CTkImage(
            light_image=baked(False), dark_image=baked(True), size=(ROW_RADIUS, EPISODE_ROW_HEIGHT)
        )
        self._stripe = ctk.CTkLabel(
            self,
            text="",
            image=image,
            width=ROW_RADIUS,
            height=EPISODE_ROW_HEIGHT,
            corner_radius=0,
            fg_color="transparent",
        )
        self._stripe.place(x=0, y=0)

    def destroy(self) -> None:
        if self._fit_job is not None:
            try:
                self.after_cancel(self._fit_job)
            except tk.TclError:
                pass
            self._fit_job = None
        super().destroy()


# -- a season's download control ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeasonItem:
    """What a season's download control works on: the season, what it offers (`kind`), its
    episodes still needed (`needed`) and every numbered episode's key (`keys`: the control
    follows their runs)."""

    show: Show
    season: int
    kind: SeasonKind
    needed: tuple[CalendarEntry, ...]
    keys: tuple[DownloadKey, ...]
    aired: int | None = None  # its numbered episodes out so far: what a pack's size is judged by

    @property
    def key(self) -> DownloadKey:
        """The season pack's key."""
        return episode_key(self.show.id, self.season, None)


class _Follow:
    """Repaints a SeasonActions when one of its episodes' runs changes (DownloadStates keeps
    one per key and only uses its `key`, `repaint()` and `winfo_exists()`)."""

    def __init__(self, key: DownloadKey, owner: SeasonActions) -> None:
        self.key = key
        self._owner = owner

    def repaint(self) -> None:
        self._owner.repaint()

    def winfo_exists(self) -> bool:
        return _exists(self._owner)


class SeasonActions(RowActions[SeasonItem]):
    """A season's download control in the show window's season bar, in the rows' style (an
    outlined button, a text-style ☰ Choose…): "⬇ Whole season" + "☰ Choose…", or
    "⬇ All missing (3)" while it airs with episodes missing; while they run the queue's states
    (`season_look`)."""

    def __init__(
        self, master: Any, *, item: SeasonItem, actions: DownloadActions[SeasonItem]
    ) -> None:
        self._season = item  # before RowActions paints the first look
        self._follows: list[_Follow] = []
        super().__init__(
            master,
            item=item,
            key=item.key,
            base=Base("download"),
            actions=actions,
            height=CONTROL_HEIGHT,
            align="left",
            choose=item.kind == "whole",
        )
        self._show_text()
        self._follow(item.keys)

    @property
    def season_item(self) -> SeasonItem:
        return self._season

    def set_item(self, item: SeasonItem) -> None:
        """The same season and kind, its episodes as they are now (a reload)."""
        keys_changed = item.keys != self._season.keys
        self._season = self._item = item
        self._show_text()
        if keys_changed:
            self._follow(item.keys)
        self.repaint()

    def current_look(self) -> RowLook:
        states = self._actions.states
        batch = states.batch_for(self._season.keys)
        return season_look(self._season.kind, states.get(self.key), batch)

    def _show_text(self) -> None:
        item = self._season
        if item.kind == "whole":
            text = t(WHOLE_SEASON_TEXT)
        else:
            text = t(ALL_MISSING_TEXT, n=len(item.needed))
        self.auto.configure(text=text, width=button_width(text, self._font.measure))
        self._update_auto_tip()

    def _update_auto_tip(self, _event: Any = None) -> None:
        tip = WHOLE_SEASON_TIP if self._season.kind == "whole" else ALL_MISSING_TIP
        self._auto_tip.set_text(t(tip))

    def _follow(self, keys: Iterable[DownloadKey]) -> None:
        states = self._actions.states
        for follow in self._follows:
            states.detach(follow)
        self._follows = [_Follow(key, self) for key in keys]
        for follow in self._follows:
            states.attach(follow)

    def destroy(self) -> None:
        for follow in self._follows:
            self._actions.states.detach(follow)
        self._follows = []
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
        profile_id: int,
        discovery: DiscoveryService | None = None,
        layout: InfoLayout = InfoLayout.SIDE,
        releases: PanelServices | None = None,
        on_fix_match: Callable[[TvTarget], None] | None = None,
        playback: PlaybackService | None = None,
        player: PlayerHost | None = None,
    ) -> None:
        """`profile_id`: the person the window shows the show for (followed or not, its Track
        button and switch; ShowWindows closes it when another person is selected).
        `playback` + `player`: "▶ Play" while the show is in Plex (its Plex title found by
        `PlaybackService.show`); a click opens the episode picker (app/ui/play_control.py), the
        episode plays in the app's player.
        `discovery`: the TMDB extras (backdrop, trailer, cast); without it the window shows only
        what TVMaze knows. `releases`: what the releases panel
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
        self._profile_id = profile_id
        self._discovery = discovery
        self._extras: TitleExtras | None = None
        self._extras_task: asyncio.Task[None] | None = None
        # The rows' slots, wide enough for their texts in the current language.
        self._slot_width = max(
            SLOT_WIDTH, actions_slot_width(theme.font(ACTIONS_FONT_SIZE, bold=True).measure)
        )
        self._mark_width = mark_slot_width(theme.font(MARK_FONT_SIZE).measure)
        # The season bar's download control (⬇ Whole season / ⬇ All missing), while it shows.
        self._season_actions: SeasonActions | None = None
        self._library = library
        self._images = images
        self._status = status
        self._episode_actions = episode_actions
        self._details: ShowDetails | None = None
        self._season: int | None = None
        self._seasons: list[int] = []
        self._chips: dict[int, ctk.CTkButton] = {}
        self._chip_slots: dict[int, ctk.CTkFrame] = {}  # a chip with its progress under it
        self._strips: dict[int, tuple[SeasonStrip, ctk.CTkLabel]] = {}
        self._rows: dict[int, _EpisodeRow] = {}  # by episode id
        self._row_episodes: tuple[Episode, ...] = ()  # what the rows show, in order
        self._load_task: asyncio.Task[None] | None = None
        self._rows_task: asyncio.Task[None] | None = None
        self._season_trailer_task: asyncio.Task[None] | None = None
        self._tracking = False
        self._buttons_look: tuple[bool, bool, bool, str | None, str | None] | None = None
        self._playback = playback
        self._play = (
            Play(playback, player, status) if playback is not None and player is not None else None
        )
        self._plex_title: PlexTitle | None = None  # the show in Plex: what ▶ Play picks from
        self._plex_task: asyncio.Task[None] | None = None
        self.play_button: ctk.CTkButton | None = None
        self._auto_var = ctk.BooleanVar(value=False)
        self._auto_switch: ctk.CTkSwitch | None = None
        self._buttons = self.buttons

        content = self.content
        self._chip_grid = FlexGrid(
            content, item_width=CHIP_WIDTH, pad_x=4, pad_y=4, center=False
        )
        self._chip_grid.pack(fill="x")
        self._season_bar = self._build_season_bar(content)
        # The open season's squares and their counts, on a line of their own under the bar.
        self._squares_row = ctk.CTkFrame(content, fg_color="transparent")
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
        """The bar above the selected season's rows: with Plex, on a tracked show's latest
        season "3 missing"; the season's download control (SeasonActions: "⬇ Whole season" +
        "☰ Choose…", or with Plex "⬇ All missing (3)"). Packed while any of it applies, see
        `_paint_season_bar`."""
        bar = ctk.CTkFrame(master, fg_color="transparent", height=CONTROL_HEIGHT)
        self._missing_label: ctk.CTkLabel | None = None  # "3 missing": with Plex only
        # The open season's squares and their counts (season progress, with Plex only).
        self._squares: EpisodeSquares | None = None
        self._counts_label: ctk.CTkLabel | None = None
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
        self._show_trailer()
        self.show_cast(extras.cast if extras is not None else ())
        self._paint_header(self._show, self._details)

    def _show_trailer(self) -> None:
        """The button on the picture: the show's trailer, then the selected season's own as soon
        as it is known."""
        trailer = self._extras.trailer if self._extras is not None else None
        self.masthead.set_trailer(trailer.watch_url if trailer is not None else None)
        if self._season_trailer_task is not None:
            self._season_trailer_task.cancel()
            self._season_trailer_task = None
        if self._discovery is not None and self._season is not None and self._extras is not None:
            self._season_trailer_task = spawn(
                self._load_season_trailer(self._discovery, self._season),
                name="ShowDetailsWindow.season_trailer",
            )

    async def _load_season_trailer(self, discovery: DiscoveryService, season: int) -> None:
        trailer = await discovery.season_trailer(self._show, season)
        if trailer is None or season != self._season or not self.winfo_exists():
            return
        url = trailer.watch_url
        if url is not None:
            self.masthead.set_trailer(url, t(SEASON_TRAILER, n=season))

    def _paint_buttons(self, details: ShowDetails) -> None:
        """+ Track, or ✓ Following with the "Auto" switch (new episodes download by themselves;
        the hover tip says so); ▶ Play while the show is in Plex; then IMDb."""
        imdb_url = details.show.imdb_url
        plex_key = self._plex_title.key if self._plex_title is not None else None
        look = (details.tracked, details.auto_download, self._tracking, imdb_url, plex_key)
        if look == self._buttons_look:
            return
        self._buttons_look = look
        self._auto_switch = None
        self.play_button = None
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
        if plex_key is not None:
            self.play_button = play_button(
                self._buttons, self.play, height=dialog_kit.BUTTON_HEIGHT
            )
            items.append(self.play_button)
        if imdb_url is not None:

            def open_imdb() -> None:
                webbrowser.open(imdb_url)

            items.append(dialog_kit.secondary_button(self._buttons, "IMDb", open_imdb, width=90))
        self._buttons.set_items(items, destroy_old=True)

    def play(self) -> None:
        """"▶ Play": the episode picker, then the chosen episode in the app's player."""
        if self._play is not None and self._plex_title is not None:
            self._play.pick(self, self._plex_title)

    # -- loading -----------------------------------------------------------------------------

    def reload(self) -> None:
        """Fetch the details again (a load still in flight is dropped), and whether the show is
        in Plex (▶ Play)."""
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = spawn(self._load(), name="ShowDetailsWindow.load")
        if self._playback is not None:
            if self._plex_task is not None and not self._plex_task.done():
                self._plex_task.cancel()
            self._plex_task = spawn(self._load_plex(self._playback), name="ShowDetailsWindow.plex")

    async def _load_plex(self, playback: PlaybackService) -> None:
        title = await playback.show(self._show) if playback.available else None
        if not self.winfo_exists():
            return
        self._plex_title = title
        if self._details is not None:
            self._paint_buttons(self._details)

    async def _load(self) -> None:
        try:
            details = await self._library.show_details(
                self._show.id, profile_id=self._profile_id
            )
        except TVMazeError as exc:
            log.warning("Show details of TVMaze show %d: %s", self._show.id, exc)
            if self.winfo_exists() and self._details is None:
                self._episodes_message(
                    t("Couldn't load the episodes: {error}", error=exc), theme.DANGER
                )
            return
        if self.winfo_exists():
            self._apply(details)

    def _apply(self, details: ShowDetails) -> None:
        self._details = details
        self._paint_header(details.show, details)
        if self.panel is not None and exists(self.panel):
            self._paint_releases_title(self.panel.target)  # the episode's name is known now
        seasons = season_numbers(details.episodes)
        if seasons != self._seasons:
            self._seasons = seasons
            self._render_chips()
        else:
            self._paint_progress()
        if self._season not in seasons:
            self._season = default_season(details, _now())
            self._show_trailer()
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
        for slot in self._chip_slots.values():
            slot.destroy()
        self._chip_slots, self._chips, self._strips = {}, {}, {}
        for season in self._seasons:
            slot = ctk.CTkFrame(self._chip_grid, fg_color="transparent", width=CHIP_WIDTH)
            chip = ctk.CTkButton(
                slot,
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
            chip.pack()
            self._chip_slots[season], self._chips[season] = slot, chip
        self._chip_grid.relayout()
        self._paint_chips()
        self._paint_progress()

    def _paint_progress(self) -> None:
        """Under each chip its season's bar and "9 of 12", while progress shows (a followed show
        with Plex connected); none otherwise."""
        details = self._details
        shown = progress_shown(details)
        for season, slot in self._chip_slots.items():
            pair = self._strips.get(season)
            if not shown or details is None:
                if pair is not None:
                    pair[0].destroy()
                    pair[1].destroy()
                    del self._strips[season]
                continue
            states = [state for _episode, state in season_states(details, season, _now())]
            if pair is None:
                strip = SeasonStrip(slot, CHIP_WIDTH - 2 * PROGRESS_INSET)
                strip.pack(pady=(PROGRESS_GAP, 0))
                label = ctk.CTkLabel(
                    slot, text="", font=theme.font(11), text_color=theme.TEXT_MUTED, height=16
                )
                label.pack()
                pair = self._strips[season] = (strip, label)
            pair[0].show(states)
            pair[1].configure(text=had_text(states))

    def _paint_chips(self) -> None:
        for season, chip in self._chips.items():
            chip.configure(**theme.choice_colors(season == self._season))

    def _select_season(self, season: int) -> None:
        if season != self._season:
            self._season = season
            self._paint_chips()
            self._show_season()
            self._show_trailer()

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
            shown = self._marks_shown()
            for episode in episodes:
                row = self._rows.get(episode.id)
                if row is not None and row.winfo_exists():
                    row.set_state(
                        self._entry(episode),
                        episode_row_state(episode, now=now),
                        now,
                        mark=row_mark(details, episode),
                        marks_shown=shown,
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
        for start in range(0, len(episodes), ROWS_PER_BATCH):
            if start:
                await asyncio.sleep(0.001)
                if not self.winfo_exists():
                    return
            details = self._details
            assert details is not None
            shown = self._marks_shown()
            for episode in episodes[start : start + ROWS_PER_BATCH]:
                row = _EpisodeRow(
                    self._episode_list,
                    self._entry(episode),
                    state=episode_row_state(episode, now=now),
                    now=now,
                    actions=self._episode_actions,
                    mark=row_mark(details, episode),
                    marks_shown=shown,
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

    # -- missing episodes --------------------------------------------------------------------

    def _marks_shown(self) -> bool:
        """Whether the rows keep a slot for the "Missing" / "✓" marks: Plex is connected and
        the tracked show's latest season is selected (`marks_shown`)."""
        details = self._details
        return details is not None and marks_shown(details, self._season)

    def _paint_season_bar(self) -> None:
        """Shown while the selected season has something for it: with Plex, on a tracked
        show's latest season its missing episodes ("Nothing missing" when none is but some
        are in Plex), and the season's download control while it applies (`_season_item`)."""
        details, season = self._details, self._season
        if details is None or season is None:
            self._season_bar.pack_forget()
            self._squares_row.pack_forget()
            return
        missing, in_plex = season_marks(details, season)
        item = self._season_item(details, season)
        self._paint_season_actions(item)
        self._paint_missing_label(missing, in_plex)
        self._paint_squares(details, season)
        if not (missing or in_plex or item is not None):
            self._season_bar.pack_forget()
            return
        if self._season_actions is not None:
            self._season_actions.pack_forget()
            # In line with the rows' controls.
            self._season_actions.pack(side="right", padx=(0, 10))
        if not self._season_bar.winfo_manager():
            before = self._squares_row if self._squares_row.winfo_manager() else self._episode_list
            self._season_bar.pack(fill="x", pady=(12, 0), before=before)

    def _paint_squares(self, details: ShowDetails, season: int) -> bool:
        """The open season's squares, after "3 missing", and their counts; whether they show."""
        states = season_states(details, season, _now()) if progress_shown(details) else []
        if not states:
            for widget in (self._squares, self._counts_label):
                if widget is not None:
                    widget.destroy()
            self._squares = self._counts_label = None
            self._squares_row.pack_forget()
            return False
        if self._squares is None:
            self._squares = EpisodeSquares(self._squares_row)
            self._squares.pack(side="left")
            self._counts_label = ctk.CTkLabel(
                self._squares_row, text="", font=theme.font(12), text_color=theme.TEXT_MUTED
            )
            self._counts_label.pack(side="left", padx=(SQUARES_GAP, 0))
        if not self._squares_row.winfo_manager():
            self._squares_row.pack(fill="x", pady=(10, 0), before=self._episode_list)
        self._squares.show([(e.number or 0, state) for e, state in states])
        assert self._counts_label is not None
        self._counts_label.configure(text=counts_text([state for _e, state in states]))
        return True

    def _paint_missing_label(self, missing: Sequence[int], in_plex: Sequence[int]) -> None:
        """"3 missing" (amber), or "Nothing missing" (muted) when none is but some are in
        Plex; no label at all otherwise (always without Plex)."""
        if not (missing or in_plex):
            if self._missing_label is not None:
                self._missing_label.destroy()
                self._missing_label = None
            return
        if self._missing_label is None:
            self._missing_label = ctk.CTkLabel(
                self._season_bar, text="", font=theme.font(15, bold=True), anchor="w"
            )
            self._missing_label.pack(side="left")
        self._missing_label.configure(
            text=missing_text(len(missing)) if missing else t(NOTHING_MISSING_TEXT),
            text_color=MISSING_COLOR if missing else theme.TEXT_MUTED,
        )

    def _season_item(self, details: ShowDetails, season: int) -> SeasonItem | None:
        """What the season's download control works on: "⬇ All missing (n)" while the season
        airs, Plex is connected and episodes are missing (or that run has something to tell),
        else "⬇ Whole season" (when the main window offers season packs), always once an
        episode has aired, also when it was grabbed before or Plex has it all (owner's decision,
        as a row's ⬇ Auto); its fallback the episodes not grabbed yet and not in Plex."""
        kind = season_kind(details.episodes, season, _now())
        if kind is None:
            return None
        states = self._episode_actions.states
        needed = season_needed(details, season, states.get)
        keys = tuple(
            entry_key(entry) for entry in season_entries(details, season)
            if entry.episode.number is not None
        )
        if kind == "missing" and details.plex_connected:
            # Exactly what "N missing" counts: not the new ones.
            missing = tuple(entry for entry in needed if details.is_missing(entry.episode))
            if missing or states.batch_for(keys) is not None:
                return SeasonItem(details.show, season, "missing", missing, keys)
        if self._episode_actions.download_season is None:
            return None
        aired = sum(
            1
            for episode in season_episodes(details.episodes, season)
            if episode.number is not None and episode.has_aired(_now())
        )
        return SeasonItem(details.show, season, "whole", needed, keys, aired or None)

    def _paint_season_actions(self, item: SeasonItem | None) -> None:
        """The season's download control for `item` (None: none): updated in place for the
        same season and kind, else made anew."""
        current = self._season_actions
        if current is not None and (
            item is None
            or (current.season_item.season, current.season_item.kind) != (item.season, item.kind)
            or not current.winfo_exists()
        ):
            current.destroy()
            current = self._season_actions = None
        if item is None:
            return
        if current is None:
            self._season_actions = SeasonActions(
                self._season_bar, item=item, actions=self._season_download_actions()
            )
        else:
            current.set_item(item)

    def _season_download_actions(self) -> DownloadActions[SeasonItem]:
        """What the season control's buttons do: the season pack (its episodes still needed
        when there is none) or the episodes still needed, queued as one batch; ☰ Choose… shows
        the pack's releases here."""
        episodes = self._episode_actions

        def download(item: SeasonItem) -> None:
            if not item.needed and item.kind != "whole":
                return
            if item.kind == "whole" and episodes.download_season is not None:
                episodes.download_season(item.show, item.season, item.needed, item.aired)
            elif episodes.download_all is not None:
                episodes.download_all(tuple(entry_key(e) for e in item.needed), item.needed)
            else:  # not offered: each on its own
                for entry in item.needed:
                    episodes.download(entry)

        def choose(item: SeasonItem) -> None:
            self.choose(TvTarget(item.show, item.season, None, item.aired))

        return DownloadActions(download=download, choose=choose, states=episodes.states)

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
            await self._library.track(show.id, profile_id=self._profile_id)
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
            await self._library.set_auto_download(show.id, on, profile_id=self._profile_id)
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
        if self._season_trailer_task is not None:
            self._season_trailer_task.cancel()
        if self._plex_task is not None:
            self._plex_task.cancel()
        if self._play is not None:
            self._play.close()
        self._cancel_rows()
        super().destroy()


def _now() -> datetime:
    return datetime.now().astimezone()


# -- opening -----------------------------------------------------------------------------------


class ShowWindows:
    """Opens show details windows, one per TVMaze show: opening an open show raises its window.
    Every open window reloads when the library or the download history changes. A window shows
    the show for the person selected when it opened (`profile`); MainWindow closes them all
    when another person is selected (`close_windows`), so none follows or switches anything
    for the wrong person."""

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
        profile: Callable[[], int],
        releases: PanelServices | None = None,
        playback: PlaybackService | None = None,
        player: PlayerHost | None = None,
    ) -> None:
        """`releases`: the releases panels' services; its settings say the layout a new window
        opens in. `playback` / `player`: the windows' "▶ Play" (none without them; `player`
        is the app's one MediaPlayerHost)."""
        self._master = master
        self._profile = profile
        self._library = library
        self._discovery = discovery
        self._images = images
        self._status = status
        self._episode_actions = episode_actions
        self._releases = releases
        self._playback = playback
        self._player = player
        self._windows: dict[int, ShowDetailsWindow] = {}  # by TVMaze id
        self._resolving: set[int] = set()  # TMDB ids being matched to TVMaze right now
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe = [library.subscribe(self.reload_all), downloads_changed(self.reload_all)]

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
            profile_id=self._profile(),
            discovery=self._discovery,
            layout=self.layout(),
            releases=self._releases,
            on_fix_match=self._fix_match,
            playback=self._playback,
            player=self._player,
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
        self.close_windows()

    def close_windows(self) -> None:
        """Cancel lookups and close every window (another person was selected); later ones
        open for the person selected then."""
        for task in list(self._tasks):
            task.cancel()
        self._resolving.clear()
        for window in self._live().values():
            window.destroy()
        self._windows = {}

    def close_shown(self) -> None:
        """Close the windows on screen (a click outside them). One a click has only just opened
        isn't on screen yet and stays."""
        for window in self._live().values():
            if window.winfo_ismapped():
                window.destroy()

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
