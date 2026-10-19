"""The show details window: a TV show's poster, summary, track state and its episodes by season,
each with its download state (✓ Downloaded, the one-click Download row, or when it airs).

Not modal; one window per show (`ShowWindows` raises the open one). It works for any TVMaze
show, tracked or not: `LibraryService.show_details` reads a tracked show from the library and
fetches any other from TVMaze. TMDB shows (Discover) are matched to TVMaze first.
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

from app.models import CalendarEntry, DiscoverShow, Episode, Show, ShowDetails
from app.services.discovery import NoTVMazeMatch
from app.services.tmdb import TMDBError
from app.services.tvmaze import TVMazeError
from app.ui import dialog_kit, theme
from app.ui.branding import set_window_icon
from app.ui.cards import AUTO_SWITCH_TEXT, auto_switch_color, tvmaze_poster_url
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
    DownloadControl,
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
from app.ui.subtitle_settings import CREDIT_TEXT
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines, unscaled_width
from app.ui.tooltip import add_tooltip
from app.ui.widgets import FlexGrid, LoadingBar, PosterImage

if TYPE_CHECKING:
    from app.services import DiscoveryService, ImageCache, LibraryService
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

RowState = Literal["downloaded", "downloadable", "upcoming"]
Mark = Literal["missing", "have"]  # the latest season's amber "Missing" / muted "✓ Have it"
Listener = Callable[[], None]
Subscribe = Callable[[Listener], Callable[[], None]]  # returns the unsubscribe function
# Per (season, episode) of a show (TVMaze id), its subtitle mark ("CC EN NL").
SubtitleMarks = Callable[[int], Awaitable[Mapping[tuple[int, int], str]]]

# Logical (unscaled) CTk units.
WINDOW_WIDTH = 860
WINDOW_HEIGHT = 680
SCREEN_MARGIN = 80  # the window stays this much smaller than the screen
MIN_WIDTH = 560
MIN_HEIGHT = 360
POSTER_WIDTH = 160
POSTER_HEIGHT = 240
POSTER_GAP = 24  # between the poster and the text beside it
SCROLLBAR_ROOM = 24  # the scroll frame's scrollbar, for the first guess at the text's width
SUMMARY_LINES = 6  # collapsed; "More" shows the rest
CHIP_WIDTH = 96
CHIP_HEIGHT = 32
EPISODE_ROW_HEIGHT = 52
CODE_WIDTH = 72
DATE_WIDTH = 64
SLOT_WIDTH = 200  # the row's right side: ✓ Downloaded, the Download row, or when it airs
MARK_WIDTH = 92  # the latest season's "Missing" / "✓ Have it" chip, left of that
MARK_HEIGHT = 26
MISSING_BUTTON_WIDTH = 300  # the season bar's "⬇ Download missing (3)"
ROWS_PER_BATCH = 12  # rows built before Tk gets to draw and handle input again
CC_FONT_SIZE = 12  # the "CC EN NL" subtitle mark beside a downloaded episode's title
CC_GAP = 8
NEAR_DAYS = 6  # an episode airing within this many days is named by its weekday

DOWNLOADED_TEXT = "✓ Downloaded"
ADDING_TEXT = "Adding…"
MORE_TEXT = "More"
NOT_ON_TVMAZE_TEXT = "Couldn't find this show on TVMaze"
NO_EPISODES_TEXT = "No episodes listed yet."
UNKNOWN_DATE_TEXT = "TBA"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# -- text and state ----------------------------------------------------------------------------


def meta_line(show: Show) -> str:
    """"2022 · Apple TV · Running · ★ 8.4": year, network or streaming service, status and
    rating, each left out when unknown ("" for none)."""
    parts: list[str] = []
    if show.year:
        parts.append(str(show.year))
    if show.network:
        parts.append(show.network)
    if show.status:
        parts.append(show.status)
    if show.rating is not None:
        parts.append(f"★ {show.rating:.1f}")
    return " · ".join(parts)


def air_day(episode: Episode, now: datetime) -> date | None:
    """The day the episode airs in `now`'s time zone (its airstamp), else its listed air date."""
    if episode.airstamp is not None:
        return episode.airstamp.astimezone(now.tzinfo).date()
    return episode.air_date


def short_date(day: date) -> str:
    """"17 Jan"."""
    return f"{day.day} {_MONTHS[day.month - 1]}"


def next_line(episode: Episode | None, now: datetime) -> str | None:
    """"Next: S02E04 · Thu 16 Oct" ("Next: S02E04" without a date); None without an episode."""
    if episode is None:
        return None
    day = air_day(episode, now)
    if day is None:
        return f"Next: {episode.code}"
    return f"Next: {episode.code} · {_WEEKDAYS[day.weekday()]} {short_date(day)}"


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
    app.services.missing), "have" (marked "I already have this" and not downloaded), else
    None. Older seasons never get one."""
    if not details.tracked or episode.season != details.latest_season:
        return None
    if details.is_missing(episode):
        return "missing"
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
        items.append((HAVE_SEASON_TEXT, "mark"))
    if have:
        items.append((MARK_MISSING_TEXT, "unmark"))
    return items


def upcoming_text(episode: Episode, now: datetime) -> str:
    """When an episode still to air airs: "airs Thu" within the next NEAR_DAYS days, else its
    date ("23 Oct"); "TBA" when unknown."""
    day = air_day(episode, now)
    if day is None:
        return UNKNOWN_DATE_TEXT
    if 0 <= (day - now.date()).days <= NEAR_DAYS:
        return f"airs {_WEEKDAYS[day.weekday()]}"
    return short_date(day)


def aired_text(episode: Episode, now: datetime) -> str:
    """The row's muted air date ("17 Jan") for an aired episode; "" for one still to air (its
    date is on the right then)."""
    if not episode.has_aired(now):
        return ""
    day = air_day(episode, now)
    return short_date(day) if day is not None else ""


def collapsed_summary(
    text: str, width: float, measure: Callable[[str], float]
) -> tuple[str, bool]:
    """The summary wrapped into at most SUMMARY_LINES lines of `width`, and whether that cut it
    short (then "More" shows the rest)."""
    fitted = fit_to_lines(text, width, SUMMARY_LINES, measure)
    return fitted, fitted != text


def _exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole interpreter is gone
        return False


# -- one episode row ---------------------------------------------------------------------------


class _EpisodeRow(ctk.CTkFrame):
    """"S02E01  Title  17 Jan" with its state on the right: ✓ Downloaded, the Download row (⬇
    Download + ⋯), or when it airs. In a tracked show's latest season, a chip left of that:
    amber "Missing" or muted "✓ Have it", each opening a menu (`on_mark`)."""

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
    ) -> None:
        super().__init__(
            master, fg_color=theme.SURFACE, corner_radius=8, height=EPISODE_ROW_HEIGHT
        )
        self._cc_text = ""
        self._holder_width = 0  # physical px, from the title holder's last <Configure>
        self.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._actions = actions
        self._on_mark = on_mark
        self._state: RowState | None = None
        self._mark: tuple[Mark | None, bool] | None = None
        self._mark_slot: ctk.CTkFrame | None = None
        episode = entry.episode

        ctk.CTkLabel(
            self,
            text=episode.code if episode.number is not None else "Special",
            width=CODE_WIDTH,
            font=theme.font(13),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).grid(row=0, column=0, padx=(14, 6), sticky="w")
        holder = ctk.CTkFrame(self, fg_color="transparent", height=24)
        holder.grid(row=0, column=1, sticky="ew")
        holder.pack_propagate(False)
        title_font = theme.font(15)
        title = ctk.CTkLabel(
            holder, text="", font=title_font, text_color=theme.TEXT, anchor="w", height=24
        )
        title.pack(side="left", fill="x", expand=True)
        # "CC EN NL" right of the title once subtitles were saved (downloaded rows only).
        self._cc = ctk.CTkLabel(
            holder,
            text="",
            font=theme.font(CC_FONT_SIZE, bold=True),
            text_color=theme.SUCCESS,
            height=24,
        )
        add_tooltip(self._cc, CREDIT_TEXT)  # the credit OpenSubtitles asks for
        name = episode.name

        def fit(width: int) -> None:
            if width > 1:
                self._holder_width = width
                room = unscaled_width(holder, width) - SLACK
                if self._cc_text:
                    room -= unscaled_width(holder, self._cc.winfo_reqwidth()) + CC_GAP
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
            self, fg_color="transparent", width=SLOT_WIDTH, height=CONTROL_HEIGHT
        )
        self._slot.pack_propagate(False)
        self._slot.grid(row=0, column=4, padx=(0, 8))
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
        self._set_mark(entry.episode, mark, marks_shown)
        self._set_cc(cc if state == "downloaded" else "")
        if state == self._state:
            return
        self._state = state
        for child in self._slot.winfo_children():
            child.destroy()
        episode = entry.episode
        self._date.configure(text=aired_text(episode, now))
        if state == "downloaded":
            ctk.CTkLabel(
                self._slot,
                text=DOWNLOADED_TEXT,
                height=CONTROL_HEIGHT,
                corner_radius=CONTROL_HEIGHT // 2,
                fg_color=theme.GRABBED,
                text_color=theme.TEXT,
                font=theme.font(CONTROL_FONT_SIZE, bold=True),
            ).pack(fill="both", expand=True)
        elif state == "downloadable":
            # A special has no number to download by: its row opens the search window.
            base = Base("search") if episode.number is None else Base("download")
            DownloadControl(
                self._slot,
                item=entry,
                key=episode_key(entry.show.id, episode.season, episode.number),
                base=base,
                actions=self._actions,
            ).pack(fill="both", expand=True)
        else:
            ctk.CTkLabel(
                self._slot,
                text=upcoming_text(episode, now),
                height=CONTROL_HEIGHT,
                font=theme.font(14),
                text_color=theme.TEXT_MUTED,
                anchor="e",
            ).pack(fill="both", expand=True, padx=(0, 6))

    @property
    def mark(self) -> Mark | None:
        return None if self._mark is None else self._mark[0]

    @property
    def cc(self) -> str:
        """The subtitle mark showing ("CC EN NL"), "" for none."""
        return self._cc_text

    def _set_cc(self, text: str) -> None:
        if text == self._cc_text:
            return
        self._cc_text = text
        if text:
            self._cc.configure(text=text)
            self._cc.pack(side="right", padx=(CC_GAP, 0), before=self._title)
        else:
            self._cc.pack_forget()
        if self._holder_width:
            self._fit_title(self._holder_width)

    def _set_mark(self, episode: Episode, mark: Mark | None, marks_shown: bool) -> None:
        if (mark, marks_shown) == self._mark:
            return
        self._mark = (mark, marks_shown)
        if not marks_shown and mark is None:
            if self._mark_slot is not None:
                self._mark_slot.destroy()
                self._mark_slot = None
            return
        if self._mark_slot is None:
            self._mark_slot = ctk.CTkFrame(
                self, fg_color="transparent", width=MARK_WIDTH, height=MARK_HEIGHT
            )
            self._mark_slot.pack_propagate(False)
            self._mark_slot.grid(row=0, column=3, padx=(0, 8))
        for child in self._mark_slot.winfo_children():
            child.destroy()
        if mark is None:
            return
        on_mark = self._on_mark
        if mark == "missing":
            text, color = CHIP_MISSING_TEXT, MISSING_COLOR
        else:
            text, color = CHIP_HAVE_TEXT, theme.TEXT_MUTED
        button = chip(
            self._mark_slot,
            text,
            color,
            height=MARK_HEIGHT,
            font_size=13,
            width=MARK_WIDTH,
            clickable=on_mark is not None,
        )
        button.pack(fill="both", expand=True)
        if on_mark is not None:
            button.configure(command=functools.partial(on_mark, episode, mark, button))
            add_tooltip(button, CHIP_TIP)


# -- the window --------------------------------------------------------------------------------


class ShowDetailsWindow(ctk.CTkToplevel):
    """A TVMaze show's details (not modal); `reload()` refetches them (library or downloads
    changed). Opened through `ShowWindows`."""

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
    ) -> None:
        """`subtitle_marks`: per (season, episode) of a show, its subtitle mark ("CC EN NL");
        without it no row gets one."""
        super().__init__(master, fg_color=theme.BG)
        self._show = show
        self._subtitle_marks = subtitle_marks
        self._cc: Mapping[tuple[int, int], str] = {}
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
        self._summary_expanded = False
        self._text_width = float(
            WINDOW_WIDTH - 2 * dialog_kit.PAD_X - POSTER_WIDTH - POSTER_GAP - SCROLLBAR_ROOM
        )

        self.title(show.name)
        set_window_icon(self)
        self.transient(master)
        # CTkToplevel only applies its dark Windows title bar reliably from resizable().
        self.resizable(True, True)
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self._place_over(master)

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        content = ctk.CTkFrame(body, fg_color="transparent")
        content.pack(fill="x", padx=dialog_kit.PAD_X, pady=dialog_kit.PAD_Y)
        self._build_header(content)
        self._chip_grid = FlexGrid(content, item_width=CHIP_WIDTH, pad_x=4, pad_y=4)
        self._chip_grid.pack(fill="x", pady=(dialog_kit.BLOCK_GAP, 0))
        self._season_bar = self._build_season_bar(content)
        self._episode_list = ctk.CTkFrame(content, fg_color="transparent")
        self._episode_list.pack(fill="x", pady=(12, 0))
        LoadingBar(self._episode_list).pack(fill="x", pady=(8, 0))

        self.bind("<Escape>", lambda _event: self.destroy())
        self.reload()
        # CTkToplevel can open behind its parent on Windows; raise it once it is mapped.
        self._activate_job: str | None = self.after(150, self._activate)

    # -- layout ------------------------------------------------------------------------------

    def _place_over(self, master: ctk.CTk) -> None:
        """Centre over `master`, at most the screen less SCREEN_MARGIN (see MainWindow._place
        for the DPI math)."""
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        screen_w, screen_h = self.wm_maxsize()
        width = max(1, min(WINDOW_WIDTH, int(screen_w / scaling) - SCREEN_MARGIN))
        height = max(1, min(WINDOW_HEIGHT, int(screen_h / scaling) - SCREEN_MARGIN))
        width_px, height_px = round(width * scaling), round(height * scaling)
        x = master.winfo_rootx() + (master.winfo_width() - width_px) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height_px) // 2
        x = min(max(0, x), max(0, screen_w - width_px))
        y = min(max(0, y), max(0, screen_h - height_px))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_header(self, master: Any) -> None:
        show = self._show
        header = ctk.CTkFrame(master, fg_color="transparent")
        header.pack(fill="x")
        header.grid_columnconfigure(1, weight=1)
        self._poster = PosterImage(
            header, width=POSTER_WIDTH, height=POSTER_HEIGHT, corner_radius=8
        )
        self._poster.grid(row=0, column=0, sticky="nw", padx=(0, POSTER_GAP))
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        self._poster.show(self._images, tvmaze_poster_url(show, round(POSTER_WIDTH * scaling)))

        info = ctk.CTkFrame(header, fg_color="transparent")
        info.grid(row=0, column=1, sticky="nsew")
        info.grid_columnconfigure(0, weight=1)
        self._name = dialog_kit.text_label(
            info, show.name, size=dialog_kit.HEADING_SIZE, bold=True
        )
        self._name.grid(row=0, column=0, sticky="w")
        self._meta = dialog_kit.text_label(
            info, "", size=dialog_kit.DETAIL_SIZE, color=theme.TEXT_MUTED
        )
        self._meta.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._summary_font = theme.font(dialog_kit.DETAIL_SIZE)
        self._summary = dialog_kit.text_label(info, "", size=dialog_kit.DETAIL_SIZE)
        self._summary.grid(row=2, column=0, sticky="w", pady=(12, 0))
        self._more = ctk.CTkLabel(
            info,
            text=MORE_TEXT,
            font=theme.font(dialog_kit.SMALL_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
            height=20,
        )
        self._more.grid(row=3, column=0, sticky="w", pady=(2, 0))
        self._more.bind("<Button-1>", lambda _event: self._expand_summary())
        self._next = dialog_kit.text_label(
            info, "", size=dialog_kit.DETAIL_SIZE, color=theme.TEXT_MUTED
        )
        self._next.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self._buttons = ctk.CTkFrame(info, fg_color="transparent")
        self._buttons.grid(row=5, column=0, sticky="w", pady=(16, 0))
        info.bind("<Configure>", self._on_info_resize)
        self._paint_header(show, None)

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
            hover_color=theme.ACCENT_HOVER,
            border_width=1,
            border_color=theme.EDGE,
            command=self._open_season_menu,
        )
        add_tooltip(self._season_more, SEASON_MENU_TIP)
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

    def _on_info_resize(self, event: Any) -> None:
        if event.width <= 1:
            return
        width = unscaled_width(self, event.width)
        if abs(width - self._text_width) < 1:
            return
        self._text_width = width
        self._fit_text()

    def _fit_text(self) -> None:
        """Wrap the name, meta line and summary at the text column's width; the summary at most
        SUMMARY_LINES lines until "More" is clicked."""
        width = max(1.0, self._text_width - SLACK)
        wrap = max(1, round(width))
        self._name.configure(wraplength=wrap)
        self._meta.configure(wraplength=wrap)
        self._next.configure(wraplength=wrap)
        summary = self._show.summary.strip()
        if not summary:
            self._summary.grid_remove()
            self._more.grid_remove()
            return
        if self._summary_expanded:
            text, cut = summary, False
        else:
            text, cut = collapsed_summary(summary, width, self._summary_font.measure)
        self._summary.configure(text=text, wraplength=wrap)
        self._summary.grid()
        if cut:
            self._more.grid()
        else:
            self._more.grid_remove()

    def _expand_summary(self) -> None:
        self._summary_expanded = True
        self._fit_text()

    def _paint_header(self, show: Show, details: ShowDetails | None) -> None:
        self._show = show
        self.title(show.name)
        self._name.configure(text=show.name)
        meta = meta_line(show)
        self._meta.configure(text=meta)
        if meta:
            self._meta.grid()
        else:
            self._meta.grid_remove()
        line = next_line(details.next_episode, _now()) if details is not None else None
        self._next.configure(text=line or "")
        if line:
            self._next.grid()
        else:
            self._next.grid_remove()
        self._fit_text()
        if details is not None:
            self._paint_buttons(details)

    def _paint_buttons(self, details: ShowDetails) -> None:
        """+ Track, or ✓ Following with the "Download new episodes" switch; then IMDb."""
        imdb_url = details.show.imdb_url
        look = (details.tracked, details.auto_download, self._tracking, imdb_url)
        if look == self._buttons_look:
            return
        self._buttons_look = look
        for child in self._buttons.winfo_children():
            child.destroy()
        self._auto_switch = None
        if details.tracked:
            ctk.CTkButton(
                self._buttons,
                text=FOLLOWING_TEXT,
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
            ).pack(side="left")
            self._auto_var.set(details.auto_download)
            self._auto_switch = ctk.CTkSwitch(
                self._buttons,
                text=AUTO_SWITCH_TEXT,
                variable=self._auto_var,
                font=theme.font(14),
                text_color=auto_switch_color(details.auto_download),
                progress_color=theme.ACCENT,
                command=self._auto_toggled,
            )
            self._auto_switch.pack(side="left", padx=(16, 0))
        else:
            track = dialog_kit.primary_button(self._buttons, TRACK_TEXT, self._track)
            if self._tracking:
                track.configure(text=ADDING_TEXT, state="disabled")
            track.pack(side="left")
        if imdb_url is not None:

            def open_imdb() -> None:
                webbrowser.open(imdb_url)

            dialog_kit.secondary_button(self._buttons, "IMDb", open_imdb, width=90).pack(
                side="left", padx=(16, 0)
            )

    def _activate(self) -> None:
        self._activate_job = None
        if self.winfo_exists():
            self.lift()
            self.focus_force()

    def raise_window(self) -> None:
        """Bring the window to the front (also from minimised)."""
        if self.state() == "iconic":
            self.deiconify()
        self.lift()
        self.focus_force()

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
                self._episodes_message(f"Couldn't load the episodes: {exc}", theme.DANGER)
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
        seasons = season_numbers(details.episodes)
        if seasons != self._seasons:
            self._seasons = seasons
            self._render_chips()
        if self._season not in seasons:
            self._season = default_season(details, _now())
            self._paint_chips()
        self._show_season()

    def _episodes_message(self, text: str, color: str) -> None:
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
                text=f"Season {season}",
                width=CHIP_WIDTH,
                height=CHIP_HEIGHT,
                corner_radius=CHIP_HEIGHT // 2,
                font=theme.font(14),
                fg_color=theme.CHIP,
                hover_color=theme.CHIP_HOVER,
                command=lambda s=season: self._select_season(s),
            )
            for season in self._seasons
        }
        self._chip_grid.relayout()
        self._paint_chips()

    def _paint_chips(self) -> None:
        for season, chip in self._chips.items():
            selected = season == self._season
            chip.configure(
                fg_color=theme.ACCENT if selected else theme.CHIP,
                hover_color=theme.ACCENT_HOVER if selected else theme.CHIP_HOVER,
            )

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
            self._episodes_message(NO_EPISODES_TEXT, theme.TEXT_MUTED)
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
            text=missing_text(len(missing)) if missing else NOTHING_MISSING_TEXT,
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
        if mark == "missing":
            item = (HAVE_EPISODE_TEXT, functools.partial(actions.mark_have, details.show, ids))
        else:
            item = (MARK_MISSING_TEXT, functools.partial(actions.unmark_have, details.show, ids))
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
            self._report(f"Couldn't track {show.name}: {exc}", 6000)
        else:
            self._report(f"Now tracking {show.name}", 4000)
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
            state = "on" if on else "off"
            self._report(
                f"Could not switch automatic downloads {state} for {show.name}", 8000
            )
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
        self._cancel_rows()
        if self._activate_job is not None:
            try:
                self.after_cancel(self._activate_job)
            except tk.TclError:
                pass
            self._activate_job = None
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
    ) -> None:
        """`subtitle_marks` / `subtitles_changed`: the rows' "CC EN NL" marks and when they
        change (subtitles saved)."""
        self._master = master
        self._missing_actions = missing_actions
        self._subtitle_marks = subtitle_marks
        self._library = library
        self._discovery = discovery
        self._images = images
        self._status = status
        self._episode_actions = episode_actions
        self._windows: dict[int, ShowDetailsWindow] = {}  # by TVMaze id
        self._resolving: set[int] = set()  # TMDB ids being matched to TVMaze right now
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe = [library.subscribe(self.reload_all), downloads_changed(self.reload_all)]
        if subtitles_changed is not None:
            self._unsubscribe.append(subtitles_changed(self.reload_all))

    def open_show(self, show: Show) -> None:
        """Open the details of a TVMaze show, or raise its window."""
        window = self._live().get(show.id)
        if window is not None:
            window.raise_window()
            return
        self._windows[show.id] = ShowDetailsWindow(
            self._master,
            show,
            library=self._library,
            images=self._images,
            status=self._status,
            episode_actions=self._episode_actions,
            missing_actions=self._missing_actions,
            subtitle_marks=self._subtitle_marks,
        )

    def open_discover(self, show: DiscoverShow) -> None:
        """Open the details of a TMDB show through its TVMaze counterpart; the status bar says
        when there is none."""
        if show.id in self._resolving:
            return
        self._resolving.add(show.id)
        self._report(f"Opening {show.name}…", 3000)
        self._start(self._open_discover(show), "ShowWindows.open_discover")

    async def _open_discover(self, show: DiscoverShow) -> None:
        try:
            match = await self._discovery.resolve(show)
        except NoTVMazeMatch:
            self._report(NOT_ON_TVMAZE_TEXT, 6000)
            return
        except (TMDBError, TVMazeError) as exc:
            self._report(f"Couldn't open {show.name}: {exc}", 8000)
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
