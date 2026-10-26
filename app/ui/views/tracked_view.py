"""The user's tracked TV library (My shows): a responsive grid of poster cards (`ShowCard`).

Under the heading a bar sorts the grid (A–Z / Next episode / Recently downloaded) and filters
it (All / Has missing / Auto on / Ended); both choices are kept in Settings (`tracked_sort`,
`tracked_filter`) and only re-order the cards already there. Next to Import Shows a ⋯ menu
holds "Remove ended shows…".
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from datetime import UTC, datetime, time
from typing import Any, NamedTuple

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import Episode, ImportResult, LibraryEntry, Show, ShowActivity
from app.models.settings import TrackedFilter, TrackedSort
from app.services import ImageCache, LibraryService, SettingsService
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.airdates_import import PreviewFinder
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.cards import ShowCard, card_info_line
from app.ui.import_dialog import ImportShowsDialog
from app.ui.missing_control import SEASON_MENU_TIP, MissingActions, popup_menu
from app.ui.poster_card import POSTER_CARD_WIDTH
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, LoadingBar, MessagePanel, show_info

log = logging.getLogger(__name__)

_MAX_NAMES = 10
_EMPTY_HEIGHT = 300

# The texts are shown through t().
TITLE = N_("Tracked TV Shows")
SORT_LABELS: dict[TrackedSort, str] = {
    TrackedSort.NAME: N_("A–Z"),
    TrackedSort.NEXT_EPISODE: N_("Next episode"),
    TrackedSort.RECENT: N_("Recently downloaded"),
}
FILTER_LABELS: dict[TrackedFilter, str] = {
    TrackedFilter.ALL: N_("All"),
    TrackedFilter.MISSING: N_("Has missing"),
    TrackedFilter.AUTO: N_("Auto on"),
    TrackedFilter.ENDED: N_("Ended"),
}
# What the page says when a filter leaves no show.
EMPTY_FILTER_TEXTS: dict[TrackedFilter, str] = {
    TrackedFilter.ALL: N_("No shows."),
    TrackedFilter.MISSING: N_("No shows with missing episodes."),
    TrackedFilter.AUTO: N_("No shows with automatic downloads on."),
    TrackedFilter.ENDED: N_("No ended shows."),
}
SORT_TEXT = N_("Sort:")
FILTER_TEXT = N_("Show:")
MORE_TEXT = "⋯"
MORE_TIP = SEASON_MENU_TIP  # "More options"
REMOVE_ENDED_TEXT = N_("Remove ended shows…")

# The header row and the sort / filter bar, in CTk units.
HEADER_BUTTON_HEIGHT = 30  # Import Shows and the ⋯ next to it
BAR_HEIGHT = 28
BAR_RADIUS = 7
BAR_FONT_SIZE = 13
BAR_LABEL_GAP = 6  # between "Sort:" and its buttons
BAR_GROUP_GAP = 20  # between the sort buttons and "Show:"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_MAX_NAMES])
    extra = len(names) - _MAX_NAMES
    return t("{names} … and {n} more", names=shown, n=extra) if extra > 0 else shown


def import_summary(result: ImportResult) -> str:
    """A short human-readable report of an import, one line per non-empty group."""
    lines: list[str] = []
    if result.added:
        lines.append(tn("Added {n} show.", "Added {n} shows.", len(result.added)))
    if result.already_tracked:
        lines.append(
            t(
                "Already tracked: {n} ({names})",
                n=len(result.already_tracked),
                names=_names(result.already_tracked),
            )
        )
    if result.not_found:
        lines.append(t("Not found: {names}", names=_names(result.not_found)))
    if result.failed:
        lines.append(t("Failed (network): {names}", names=_names(result.failed)))
    return "\n".join(lines) if lines else t("Nothing to import.")


# -- sorting and filtering -----------------------------------------------------------------------


class ShowRow(NamedTuple):
    """What the sort and filter rules know about one tracked show."""

    show: Show
    auto_download: bool = False
    missing: int = 0  # its missing episodes (app/services/missing.py)
    next_episode: Episode | None = None
    last_download: datetime | None = None  # its latest grab, however long ago


def show_rows(
    entries: Iterable[LibraryEntry],
    activity: Mapping[int, ShowActivity],
    missing: Mapping[int, int],
    last_downloads: Mapping[int, datetime],
) -> list[ShowRow]:
    """One row per tracked show, in the library's order."""
    rows: list[ShowRow] = []
    for entry in entries:
        show_id = entry.show.id
        found = activity.get(show_id)
        rows.append(
            ShowRow(
                show=entry.show,
                auto_download=entry.auto_download,
                missing=missing.get(show_id, 0),
                next_episode=found.next_episode if found is not None else None,
                last_download=last_downloads.get(show_id),
            )
        )
    return rows


def matches(row: ShowRow, show_filter: TrackedFilter) -> bool:
    """Whether the filter lets the show through."""
    if show_filter is TrackedFilter.MISSING:
        return row.missing > 0
    if show_filter is TrackedFilter.AUTO:
        return row.auto_download
    if show_filter is TrackedFilter.ENDED:
        return row.show.is_ended
    return True


def _name_key(row: ShowRow) -> tuple[str, int]:
    return row.show.name.casefold(), row.show.id


def _air_instant(episode: Episode | None) -> datetime | None:
    if episode is None:
        return None
    if episode.airstamp is not None:
        return episode.airstamp
    if episode.air_date is not None:
        return datetime.combine(episode.air_date, time.min, tzinfo=UTC)
    return None


def sort_rows(rows: Iterable[ShowRow], order: TrackedSort) -> list[ShowRow]:
    """A–Z by name; Next episode: the soonest first, shows without one last (A–Z); Recently
    downloaded: the latest grab first, shows never downloaded last (A–Z)."""
    if order is TrackedSort.NEXT_EPISODE:

        def next_key(row: ShowRow) -> tuple[bool, datetime, tuple[str, int]]:
            when = _air_instant(row.next_episode)
            return when is None, when or _EPOCH, _name_key(row)

        return sorted(rows, key=next_key)
    if order is TrackedSort.RECENT:

        def recent_key(row: ShowRow) -> tuple[bool, float, tuple[str, int]]:
            when = row.last_download
            return when is None, -when.timestamp() if when is not None else 0.0, _name_key(row)

        return sorted(rows, key=recent_key)
    return sorted(rows, key=_name_key)


def arrange(
    rows: Iterable[ShowRow], order: TrackedSort, show_filter: TrackedFilter
) -> list[ShowRow]:
    """The shows the grid shows, in the order it shows them."""
    return sort_rows((row for row in rows if matches(row, show_filter)), order)


def heading_text(shown: int, total: int, show_filter: TrackedFilter) -> str:
    """ "Tracked TV Shows (40)"; while a filter is on "Tracked TV Shows (12 of 40)"."""
    if show_filter is TrackedFilter.ALL:
        return f"{t(TITLE)} ({total})"
    return t("{title} ({shown} of {total})", title=t(TITLE), shown=shown, total=total)


def empty_filter_text(show_filter: TrackedFilter) -> str:
    """The friendly line when the filter leaves no show."""
    return t(EMPTY_FILTER_TEXTS[show_filter])


def sort_from_label(label: str) -> TrackedSort | None:
    return next((order for order, text in SORT_LABELS.items() if t(text) == label), None)


def filter_from_label(label: str) -> TrackedFilter | None:
    return next((choice for choice, text in FILTER_LABELS.items() if t(text) == label), None)


def _choices(
    master: Any, values: Sequence[str], command: Callable[[str], None]
) -> ctk.CTkSegmentedButton:
    """Compact segmented buttons, as the calendar's week count."""
    return ctk.CTkSegmentedButton(
        master,
        values=list(values),
        height=BAR_HEIGHT,
        corner_radius=BAR_RADIUS,
        font=theme.font(BAR_FONT_SIZE),
        fg_color=theme.SURFACE,
        selected_color=theme.SELECTED,
        selected_hover_color=theme.SELECTED_HOVER,
        unselected_color=theme.SURFACE,
        unselected_hover_color=theme.CHIP_HOVER,
        text_color=theme.BUTTON_TEXT,
        command=command,
    )


class TrackedView(BaseView):
    def __init__(
        self,
        master: Any,
        *,
        library: LibraryService,
        settings: SettingsService,
        airdates: PreviewFinder,
        images: ImageCache,
        status: StatusReporter,
        on_open_show: Callable[[Show], None] | None = None,
        missing_actions: MissingActions | None = None,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        """`on_open_show`: what a click on a card's poster or title does (the show details).
        `missing_actions`: the cards show their missing episodes and "Download missing".
        `card_scale`: the card size (Settings), read at every load. `settings`: the sort and
        filter choices (`tracked_sort`, `tracked_filter`)."""
        super().__init__(master, **kwargs)
        self._card_scale = card_scale
        self._library = library
        self._settings = settings
        self._airdates = airdates
        self._images = images
        self._status = status
        self._on_open_show = on_open_show
        self._missing_actions = missing_actions
        self._tasks: set[asyncio.Task[None]] = set()
        self._import_dialog: ImportShowsDialog | None = None
        self._cards: dict[int, ShowCard] = {}
        self._rows: list[ShowRow] = []  # every tracked show, as the last load found them
        self._grid: FlexGrid | None = None  # None while loading or without shows
        self._no_match: ctk.CTkLabel | None = None  # the line when the filter leaves nothing
        prefs = settings.current
        self._sort = prefs.tracked_sort
        self._filter = prefs.tracked_filter
        # Switch changes in flight: the library change they cause needs no reload of this page.
        self._own_changes = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        self._title = ctk.CTkLabel(
            header, text=t(TITLE), font=theme.font(18, bold=True), text_color=theme.TEXT
        )
        self._title.pack(side="left", padx=(0, 10))
        self._import_button = ctk.CTkButton(
            header,
            text=t("Import Shows"),
            width=120,
            height=HEADER_BUTTON_HEIGHT,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._open_import,
        )
        self._import_button.pack(side="left", padx=5)
        self._more_button = ctk.CTkButton(
            header,
            text=MORE_TEXT,
            width=HEADER_BUTTON_HEIGHT,
            height=HEADER_BUTTON_HEIGHT,
            font=theme.font(14, bold=True),
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
            command=self._open_more_menu,
        )
        self._more_button.pack(side="left")
        add_tooltip(self._more_button, t(MORE_TIP))

        self._bar = ctk.CTkFrame(self, fg_color="transparent")
        self._bar.grid(row=1, column=0, sticky="w", padx=15, pady=(0, 8))
        self._bar_label(t(SORT_TEXT)).pack(side="left", padx=(0, BAR_LABEL_GAP))
        sorts = [t(label) for label in SORT_LABELS.values()]
        self._sort_choice = _choices(self._bar, sorts, self._choose_sort)
        self._sort_choice.pack(side="left")
        self._bar_label(t(FILTER_TEXT)).pack(side="left", padx=(BAR_GROUP_GAP, BAR_LABEL_GAP))
        self._filter_choice = _choices(
            self._bar, [t(label) for label in FILTER_LABELS.values()], self._choose_filter
        )
        self._filter_choice.pack(side="left")
        self._show_choices()

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 5))

    def _bar_label(self, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            self._bar, text=text, font=theme.font(BAR_FONT_SIZE), text_color=theme.TEXT_MUTED
        )

    # -- loading -----------------------------------------------------------------------------

    def _clear_body(self) -> None:
        self._cards.clear()
        self._grid = None
        self._no_match = None
        for child in self._body.winfo_children():
            child.destroy()

    def refresh(self) -> None:
        if self._own_changes:  # this page's own switch: it already shows the change
            self._stale = False
            return
        super().refresh()

    async def load(self) -> None:
        prefs = self._settings.current
        self._sort, self._filter = prefs.tracked_sort, prefs.tracked_filter
        self._show_choices()
        self._clear_body()
        LoadingBar(self._body).pack(fill="x")
        entries = await self._library.tracked()
        activity = await self._library.activity()
        missing = (
            await self._library.missing_counts() if self._missing_actions is not None else {}
        )
        last_downloads = await self._library.last_downloads()
        now = datetime.now().astimezone()
        self._clear_body()
        self._rows = show_rows(entries, activity, missing, last_downloads)
        if not entries:
            self._title.configure(text=heading_text(0, 0, TrackedFilter.ALL))
            self._bar.grid_remove()
            MessagePanel(
                self._body,
                t("No tracked shows yet"),
                t("Search for a show at the top right, or use Import Shows to paste a list."),
                height=_EMPTY_HEIGHT,
            ).pack(fill="x", expand=True)
            return
        self._bar.grid()
        scale = self._card_scale()
        grid = FlexGrid(
            self._body, item_width=grid_item_width(POSTER_CARD_WIDTH, scale), pad_x=8, pad_y=10
        )
        grid.pack(fill="x", expand=True)
        for entry in entries:
            self._cards[entry.show.id] = ShowCard(
                grid,
                entry.show,
                images=self._images,
                on_remove=self._remove,
                auto_download=entry.auto_download,
                info_line=card_info_line(activity.get(entry.show.id), now),
                on_auto_download=self._toggle_auto,
                on_open=self._on_open_show,
                missing=missing.get(entry.show.id, 0),
                missing_actions=self._missing_actions,
                scale=scale,
            )
        self._grid = grid
        self._no_match = ctk.CTkLabel(
            self._body, text="", font=theme.font(15), text_color=theme.TEXT_MUTED
        )
        self._arrange()

    # -- sort and filter -------------------------------------------------------------------------

    def _show_choices(self) -> None:
        self._sort_choice.set(t(SORT_LABELS[self._sort]))
        self._filter_choice.set(t(FILTER_LABELS[self._filter]))

    def _choose_sort(self, label: str) -> None:
        order = sort_from_label(label)
        if order is None or order is self._sort:
            return
        self._sort = order
        self._arrange(to_top=True)
        # Not one of the page's tasks: the choice is kept even when the page goes meanwhile.
        spawn(self._settings.update(tracked_sort=order), name="TrackedView.save_sort")

    def _choose_filter(self, label: str) -> None:
        choice = filter_from_label(label)
        if choice is None or choice is self._filter:
            return
        self._filter = choice
        self._arrange(to_top=True)
        spawn(self._settings.update(tracked_filter=choice), name="TrackedView.save_filter")

    def _arrange(self, *, to_top: bool = False) -> None:
        """Show the loaded cards the current filter lets through, in the current order."""
        grid, no_match = self._grid, self._no_match
        if grid is None or no_match is None:
            return  # loading, or no shows
        shown = arrange(self._rows, self._sort, self._filter)
        self._title.configure(text=heading_text(len(shown), len(self._rows), self._filter))
        if shown:
            no_match.pack_forget()
            if not grid.winfo_manager():
                grid.pack(fill="x", expand=True)
            grid.set_order([self._cards[row.show.id] for row in shown])
        else:
            grid.pack_forget()
            no_match.configure(text=empty_filter_text(self._filter))
            no_match.pack(fill="x", pady=40)
        if to_top:
            self._body._parent_canvas.yview_moveto(0)  # CTk has no scroll API

    # -- actions -----------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _report(self, text: str, *, clear_after_ms: int | None = None) -> None:
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # window already destroyed

    def _remove(self, show: Show) -> None:
        self._start(self._untrack(show), "TrackedView.remove")

    def _toggle_auto(self, show: Show, on: bool) -> None:
        self._own_changes += 1
        # The card stays where it is until the next sort, filter or reload.
        self._note_auto(show.id, on)
        self._start(self._set_auto(show, on), "TrackedView.auto-download")

    def _note_auto(self, show_id: int, on: bool) -> None:
        self._rows = [
            row._replace(auto_download=on) if row.show.id == show_id else row for row in self._rows
        ]

    async def _set_auto(self, show: Show, on: bool) -> None:
        try:
            await self._library.set_auto_download(show.id, on)
        except Exception:
            log.exception("Switching automatic downloads for TVMaze show %d failed", show.id)
            self._note_auto(show.id, not on)
            card = self._cards.get(show.id)
            if card is not None and card.winfo_exists():
                card.set_auto_download(not on)
            text = (
                t("Could not switch automatic downloads on for {name}", name=show.name)
                if on
                else t("Could not switch automatic downloads off for {name}", name=show.name)
            )
            self._report(text, clear_after_ms=8000)
        finally:
            self._own_changes -= 1

    async def _untrack(self, show: Show) -> None:
        await self._library.untrack(show.id)
        self._report(t("Removed {name}", name=show.name), clear_after_ms=4000)

    def _open_more_menu(self) -> None:
        popup_menu(self._more_button, [(t(REMOVE_ENDED_TEXT), self._remove_ended)])

    def _remove_ended(self) -> None:
        self._start(self._cleanup(), "TrackedView.cleanup")

    async def _cleanup(self) -> None:
        self._more_button.configure(state="disabled")
        try:
            removed = await self._library.remove_ended()
            if removed:
                text = tn("Removed {n} ended show", "Removed {n} ended shows", removed)
                self._report(text, clear_after_ms=5000)
            else:
                self._report(t("No ended shows to remove"), clear_after_ms=5000)
        finally:
            if self._more_button.winfo_exists():
                self._more_button.configure(state="normal")

    def _open_import(self) -> None:
        dialog = self._import_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            return
        if (window := self._window()) is not None:
            self._import_dialog = ImportShowsDialog(
                window, on_import=self._on_import, airdates=self._airdates, library=self._library
            )

    def _window(self) -> ctk.CTk | ctk.CTkToplevel | None:
        toplevel = self.winfo_toplevel()
        return toplevel if isinstance(toplevel, (ctk.CTk, ctk.CTkToplevel)) else None

    def _on_import(self, lines: list[str]) -> None:
        self._start(self._import(lines), "TrackedView.import")

    async def _import(self, lines: list[str]) -> None:
        self._import_button.configure(state="disabled")
        try:
            result = await self._library.import_shows(
                lines,
                progress=lambda done, total: self._report(
                    t("Importing shows… {done}/{total}", done=done, total=total)
                ),
            )
        except TVMazeError as exc:
            self._report(t("Import failed: {error}", error=exc), clear_after_ms=8000)
            return
        finally:
            if self._import_button.winfo_exists():
                self._import_button.configure(state="normal")
        self._report(t("Import finished"), clear_after_ms=3000)
        if self.winfo_exists() and (window := self._window()) is not None:
            show_info(window, t("Import complete"), import_summary(result))

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()
