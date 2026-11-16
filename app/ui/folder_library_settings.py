"""Settings → Library: the "Your own folders" box (mockups/folder-library.html, states 1–3) over
FolderLibraryService (app/services/folder_library/service.py; docs/folder-library/).

Three looks, by the library's source:
- **Off** (no Plex, not switched on): "Your own folders" with an "Off" pill, one muted line on
  what it is for, the film folders and the series folders (each a 📁 row with ✕, then "+ Add a
  folder", Windows' folder picker), and the accent "Use these folders". With no folder set yet,
  MediaForge's Movies and Series folders are filled in as a suggestion (`suggest`, playback's
  `forge_folders`), with a line saying so; nothing is saved or read until "Use these folders"
  (`switch_on`, which reads the folders first and saves them once switched on). While it reads,
  the button says so; when no folder could be reached, one plain line says nothing was switched
  on, and the folders stay as entered (when they answered but weren't read in time, the line
  says so, and to try again in a minute).
- **On**: an "On" pill, a green dot with "412 films · 38 series · 1,906 episodes · checked 3 min
  ago", how many files couldn't be recognised (they are listed on the Library page), an amber dot
  per folder that couldn't be reached ("what was found there last time still counts"; one that
  answered but wasn't read in time "takes a while"; library_words' `reach_lines`, the Library page's too), the
  folders (a change is read and saved at once, through `switch_on` again), "↻ Check now"
  (`check_soon`) and "Stop using folders" (asked first; `switch_off`: what was read is forgotten,
  the folders stay set, no file is touched).
- **Plex connected**: one muted line, "Plex is your library. Your own folders count again if you
  disconnect Plex." The folders stay as they are, unread.

Everything acts at once (like the Plex box), not on Save. The box follows the service
(`subscribe`, which also hears Plex connecting and disconnecting) and stops when it goes;
`on_state` tells Settings which look it has, so the library's box comes first.
"""

from __future__ import annotations

import asyncio
import functools
import ntpath
import tkinter as tk
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Literal, Protocol

import customtkinter as ctk

from app.i18n import N_, number, t
from app.models.folder_library import FolderLibraryStatus, SwitchOutcome
from app.services.library_source import library_source
from app.ui import theme
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.dialog_kit import BUTTON_HEIGHT, SMALL_SIZE, primary_button, secondary_button
from app.ui.library_words import CANT_REACH_TEXT as CANT_REACH_TEXT  # the box says it
from app.ui.library_words import SLOW_TEXT as SLOW_TEXT
from app.ui.library_words import reach_lines
from app.ui.missing_control import mark_dot
from app.ui.settings_layout import BOX_INNER, TEXT_SIZE, SettingsBox, SettingsPage
from app.ui.tasks import spawn
from app.ui.tooltip import Tooltip
from app.ui.watchlist_cards import searched_ago
from app.ui.widgets import elided, hairline

if TYPE_CHECKING:
    from app.services.playback import LibraryFolders
    from app.services.settings import SettingsService

BoxState = Literal["off", "on", "plex"]
FolderKind = Literal["film", "series"]

TITLE_TEXT = N_("Your own folders")
OFF_TEXT = N_("Off")
ON_TEXT = N_("On")
INTRO_TEXT = N_("No Plex? AirGrabber can read the folders that hold your films and series instead.")
FILMS_LABEL = N_("Film folders")
SERIES_LABEL = N_("Series folders")
ADD_TEXT = N_("+ Add a folder")
REMOVE_TIP = N_("Leave this folder out")
SUGGESTED_TEXT = N_(
    "Filled in from MediaForge's folders. Change them if your films and series are somewhere else."
)
USE_TEXT = N_("Use these folders")
READING_TEXT = N_("Reading your folders…")
UNREACHABLE_ALL_TEXT = N_(
    "None of these folders could be reached, so nothing was switched on. Check that they're "
    "there, then try again."
)
UNREACHABLE_CHANGE_TEXT = N_(
    "None of these folders could be reached, so the change wasn't saved: the folders below are "
    "the ones still in use."
)
SLOW_ALL_TEXT = N_(
    "These folders answered, but reading them takes longer than usual, so nothing was switched "
    "on yet. AirGrabber goes on reading: try again in a minute."
)
SLOW_CHANGE_TEXT = N_(
    "These folders answered, but reading them takes longer than usual, so the change wasn't "
    "saved yet. AirGrabber goes on reading: try again in a minute."
)
CHECK_NOW_TEXT = N_("↻ Check now")
CHECKING_TEXT = N_("Checking…")
CHECKING_FOLDERS_TEXT = N_("Checking your folders…")
NOT_CHECKED_TEXT = N_("Not checked yet")
STOP_TEXT = N_("Stop using folders")
STOP_TITLE = N_("Stop using your folders?")
STOP_QUESTION = N_(
    "The marks and the Library page go until you switch them on again. No file is touched."
)
PLEX_LINE_TEXT = N_("Plex is your library. Your own folders count again if you disconnect Plex.")
COUNTS_TEXT = N_("{films} · {series} · {episodes} · checked {when}")
FILM_ONE, FILM_MORE = N_("{n} film"), N_("{n} films")
SERIES_ONE, SERIES_MORE = N_("{n} series"), N_("{n} series")
EPISODE_ONE, EPISODE_MORE = N_("{n} episode"), N_("{n} episodes")
UNRECOGNISED_ONE = N_("{n} file couldn't be recognised: it's listed on the Library page.")
UNRECOGNISED_MORE = N_("{n} files couldn't be recognised: they're listed on the Library page.")

USE_WIDTH = 190
CHECK_WIDTH = 140
STOP_WIDTH = 180
PILL_HEIGHT = 22
ROW_RADIUS = 8
ROW_PAD = 10
REMOVE_SIDE = 26
ICON_WIDTH = 24
# The path's room in a folder row: the box's inside less the 📁, ✕ and the row's padding.
PATH_WIDTH = BOX_INNER - 2 * ROW_PAD - ICON_WIDTH - REMOVE_SIDE - 24


class FolderLibrary(Protocol):
    """FolderLibraryService as the box uses it (tests pass a stand-in)."""

    @property
    def status(self) -> FolderLibraryStatus: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...

    async def switch_on(self, films: Sequence[str], series: Sequence[str]) -> SwitchOutcome: ...

    async def switch_off(self) -> None: ...

    def check_soon(self, delay_s: float = 0) -> None: ...


Suggest = Callable[[], Awaitable["LibraryFolders | None"]]


def _now() -> datetime:
    return datetime.now(UTC)


def _counted(one: str, more: str, n: int) -> str:
    """`one` / `more` (N_ texts) with `{n}` as "1,906" (Dutch: "1.906")."""
    return t(one if n == 1 else more, n=number(n, ","))


def counts_text(status: FolderLibraryStatus, now: datetime) -> str:
    """"412 films · 38 series · 1,906 episodes · checked 3 min ago" ("Not checked yet" before
    the first check)."""
    if status.checked_at is None:
        return t(NOT_CHECKED_TEXT)
    return t(
        COUNTS_TEXT,
        films=_counted(FILM_ONE, FILM_MORE, status.films),
        series=_counted(SERIES_ONE, SERIES_MORE, status.shows),
        episodes=_counted(EPISODE_ONE, EPISODE_MORE, status.episodes),
        when=searched_ago(status.checked_at, now),
    )


def unrecognised_text(n: int) -> str:
    """"6 files couldn't be recognised: they're listed on the Library page."."""
    return _counted(UNRECOGNISED_ONE, UNRECOGNISED_MORE, n)


def _same(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def _ask_folder(owner: tk.Misc) -> str:
    # Windows' own modal folder picker: the event loop pauses until it closes.
    return filedialog.askdirectory(parent=owner, mustexist=True) or ""


class FolderLibraryBox:
    """The "Your own folders" box on Settings → Library (see the module docstring)."""

    def __init__(
        self,
        page: SettingsPage,
        folders: FolderLibrary,
        settings: SettingsService,
        *,
        suggest: Suggest | None = None,
        pick_folder: Callable[[tk.Misc], str] = _ask_folder,
        clock: Callable[[], datetime] = _now,
        on_state: Callable[[BoxState], None] | None = None,
    ) -> None:
        """`suggest`: MediaForge's Movies and Series folders (None: no suggestion);
        `pick_folder(owner)`: a folder the user picked ("" for none); `on_state(state)`: after
        every drawing, which look the box has. The suggestion is asked for only once the page
        shows (`page_shown`): asking may start MediaForge."""
        self._folders = folders
        self._settings = settings
        self._suggest = suggest
        self._pick_folder = pick_folder
        self._clock = clock
        self._on_state = on_state
        self._tasks: set[asyncio.Task[None]] = set()
        self._draw_task: asyncio.Task[None] | None = None
        current = settings.current
        # The folders as entered here: the saved ones, or MediaForge's as a suggestion.
        self.films: list[str] = [f for f in current.film_folders if f.strip()]
        self.series: list[str] = [f for f in current.series_folders if f.strip()]
        self.suggested = False
        self._asked = False
        self._page_shown = False  # `page_shown`: the suggestion may be asked for
        self._switching = False  # a switch_on from here is under way
        self._message: str | None = None  # "nothing was switched on"
        self.state: BoxState = "off"
        self.confirm: ConfirmDialog | None = None
        self.box: SettingsBox = page.box(None, None)
        page.boxes[-1] = (t(TITLE_TEXT), self.box.frame)
        self.content = ctk.CTkFrame(self.box.body, fg_color="transparent")
        self.box.add(self.content, gap=0)
        self.content.grid_columnconfigure(0, weight=1)
        # Filled by `show`.
        self.use_button: ctk.CTkButton | None = None
        self.check_button: ctk.CTkButton | None = None
        self.stop_button: ctk.CTkButton | None = None
        self.add_buttons: dict[FolderKind, ctk.CTkButton | None] = {"film": None, "series": None}
        self.remove_buttons: dict[FolderKind, list[ctk.CTkButton]] = {"film": [], "series": []}
        self._unsubscribe = folders.subscribe(self.refresh)
        # Tk's own bind: CTkFrame.bind would bind its canvas.
        tk.Misc.bind(self.content, "<Destroy>", self._on_destroy, add="+")
        self.show()

    # -- following the service ---------------------------------------------------------------

    def refresh(self) -> None:
        """Draw the box again for the service's status, once the current event is done (the
        newest call wins)."""
        if self._draw_task is not None and not self._draw_task.done():
            self._draw_task.cancel()
        self._draw_task = self._run(self._draw_later(), "FolderLibraryBox.draw")

    async def _draw_later(self) -> None:
        await asyncio.sleep(0)
        if _exists(self.content):
            self.show()

    async def loaded(self) -> None:
        """Wait until nothing from here is under way (tests)."""
        while True:
            pending = {task for task in self._tasks if not task.done()}
            if not pending:
                return
            await asyncio.wait(pending)

    def _run(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.content:
            return
        self._unsubscribe()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # -- drawing -----------------------------------------------------------------------------

    def _current_state(self) -> BoxState:
        if library_source(self._settings.current) == "plex":
            return "plex"
        return "on" if self._folders.status.on else "off"

    def show(self) -> None:
        """Draw the box for the service's status now."""
        status = self._folders.status
        self.state = state = self._current_state()
        if state == "on" and not self._switching:
            # Switched on (by this box, or by a switch that outlived an earlier Settings): what
            # is shown is what is saved, never an old copy or a suggestion.
            self._take_saved()
        for child in self.content.winfo_children():
            child.destroy()
        self.use_button = self.check_button = self.stop_button = None
        self.add_buttons = {"film": None, "series": None}
        self.remove_buttons = {"film": [], "series": []}
        row = 0

        def place(widget: Any, *, gap: int = 8, sticky: str = "w") -> None:
            nonlocal row
            widget.grid(row=row, column=0, sticky=sticky, pady=(0 if row == 0 else gap, 0))
            row += 1

        if state == "plex":
            place(self._text(t(PLEX_LINE_TEXT)))
        else:
            place(self._header(on=state == "on"))
            if state == "on":
                self._place_status(place, status)
            else:
                place(self._text(t(INTRO_TEXT)), gap=4)
            self._place_folders(place, on=state == "on")
            if state == "off" and self.suggested:
                place(self._text(t(SUGGESTED_TEXT), size=SMALL_SIZE), gap=8)
            if self._message:
                place(self._dot_line(self._message, theme.WARNING), gap=10)
            place(hairline(self.content, theme.PANEL_EDGE), gap=14, sticky="ew")
            place(self._buttons(status, on=state == "on"), gap=14)
        if (
            state == "off"
            and self._page_shown
            and not self.busy  # a switch under way (maybe from an earlier Settings) decides
            and not self.films
            and not self.series
            and not self._asked
        ):
            self._asked = True
            if self._suggest is not None:
                self._run(self._fill_in(self._suggest), "FolderLibraryBox.suggest")
        if self._on_state is not None:
            self._on_state(state)

    def page_shown(self) -> None:
        """Settings shows the Library page: MediaForge's folders may be asked for now."""
        if not self._page_shown:
            self._page_shown = True
            self.refresh()

    @property
    def busy(self) -> bool:
        """A switch or a check runs: the folders can't change meanwhile."""
        return self._switching or self._folders.status.checking

    def _header(self, *, on: bool) -> ctk.CTkFrame:
        line = ctk.CTkFrame(self.content, fg_color="transparent")
        ctk.CTkLabel(
            line, text=t(TITLE_TEXT), font=theme.font(16, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            line,
            text=t(ON_TEXT if on else OFF_TEXT),
            width=0,
            height=PILL_HEIGHT,
            corner_radius=PILL_HEIGHT // 2,
            fg_color=theme.GRABBED if on else theme.CHIP_HOVER,
            text_color=theme.ON_COLOR if on else theme.TEXT,
            font=theme.font(12, bold=True),
        ).pack(side="left", padx=(10, 0), ipadx=8)
        return line

    def _place_status(self, place: Callable[..., None], status: FolderLibraryStatus) -> None:
        if status.checked_at is None:
            text = t(CHECKING_FOLDERS_TEXT if status.checking else NOT_CHECKED_TEXT)
        else:
            text = counts_text(status, self._clock())
        place(self._dot_line(text, theme.SUCCESS), gap=6)
        if status.unrecognised:
            place(self._text(unrecognised_text(len(status.unrecognised)), size=SMALL_SIZE), gap=4)
        for line in reach_lines(status):
            place(self._dot_line(line, theme.WARNING), gap=6)

    def _place_folders(self, place: Callable[..., None], *, on: bool) -> None:
        total = len(self.films) + len(self.series)
        parts: tuple[tuple[FolderKind, str, list[str]], ...] = (
            ("film", FILMS_LABEL, self.films),
            ("series", SERIES_LABEL, self.series),
        )
        for kind, label, shown in parts:
            place(
                ctk.CTkLabel(
                    self.content, text=t(label), font=theme.font(TEXT_SIZE, bold=True),
                    text_color=theme.TEXT, anchor="w",
                ),
                gap=14,
            )
            for index, folder in enumerate(shown):
                # Switched on, the last folder isn't taken out here: "Stop using folders" is.
                removable = not on or total > 1
                place(self._folder_row(kind, index, folder, removable), gap=6, sticky="ew")
            busy = self.busy
            add = ctk.CTkButton(
                self.content,
                text=t(ADD_TEXT),
                width=0,
                height=BUTTON_HEIGHT - 12,
                corner_radius=6,
                font=theme.font(TEXT_SIZE, bold=True),
                fg_color="transparent",
                hover_color=theme.CHIP,
                text_color=theme.LINK,
                anchor="w",
                command=functools.partial(self.add, kind),
                state="disabled" if busy else "normal",
            )
            self.add_buttons[kind] = add
            place(add, gap=4)

    def _folder_row(self, kind: FolderKind, index: int, folder: str, removable: bool) -> ctk.CTkFrame:
        row = ctk.CTkFrame(
            self.content,
            fg_color=theme.BG,
            border_width=1,
            border_color=theme.PANEL_EDGE,
            corner_radius=ROW_RADIUS,
        )
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text="📁", width=ICON_WIDTH, font=theme.font(TEXT_SIZE)).grid(
            row=0, column=0, padx=(ROW_PAD, 4), pady=4
        )
        font = theme.font(TEXT_SIZE)
        path = elided(folder, lambda text: font.measure(text) <= PATH_WIDTH)
        ctk.CTkLabel(
            row, text=path, font=font, text_color=theme.TEXT, anchor="w"
        ).grid(row=0, column=1, sticky="ew", pady=4)
        if removable:
            remove = ctk.CTkButton(
                row,
                text="✕",
                width=REMOVE_SIDE,
                height=REMOVE_SIDE,
                corner_radius=6,
                font=theme.font(SMALL_SIZE),
                fg_color="transparent",
                hover_color=theme.CHIP_HOVER,
                text_color=theme.TEXT_MUTED,
                command=lambda: self.remove(kind, index),
                state="disabled" if self.busy else "normal",
            )
            remove.grid(row=0, column=2, padx=(4, 6), pady=4)
            Tooltip(remove, t(REMOVE_TIP))
            self.remove_buttons[kind].append(remove)
        return row

    def _buttons(self, status: FolderLibraryStatus, *, on: bool) -> ctk.CTkFrame:
        line = ctk.CTkFrame(self.content, fg_color="transparent")
        busy = status.checking or self._switching
        if not on:
            self.use_button = primary_button(
                line, t(READING_TEXT) if busy else t(USE_TEXT), self.use, width=USE_WIDTH
            )
            if busy or not (self.films or self.series):
                self.use_button.configure(state="disabled")
            self.use_button.pack(side="left")
            return line
        self.check_button = secondary_button(
            line, t(CHECKING_TEXT) if busy else t(CHECK_NOW_TEXT), self.check_now, width=CHECK_WIDTH
        )
        if busy:
            self.check_button.configure(state="disabled")
        self.check_button.pack(side="left")
        self.stop_button = secondary_button(line, t(STOP_TEXT), self.ask_stop, width=STOP_WIDTH)
        self.stop_button.pack(side="left", padx=(12, 0))
        return line

    def _text(self, text: str, *, size: int = TEXT_SIZE) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            self.content,
            text=text,
            font=theme.font(size),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER,
        )

    def _dot_line(self, text: str, colour: theme.Color) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            self.content,
            text=text,
            image=mark_dot(self.content, colour),
            compound="left",
            font=theme.font(TEXT_SIZE),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER - 20,
        )

    # -- actions -------------------------------------------------------------------------------

    async def _fill_in(self, suggest: Suggest) -> None:
        """MediaForge's Movies and Series folders, as a suggestion (not saved)."""
        found = await suggest()
        if found is None or not _exists(self.content) or self.films or self.series:
            return
        if self.busy or self._current_state() != "off":
            self._asked = False  # asked again once nothing runs, if it is still off and empty
            return
        self.films = [found.movies] if found.movies.strip() else []
        self.series = [found.series] if found.series.strip() else []
        self.suggested = bool(self.films or self.series)
        if self.suggested:
            self.show()

    def _list(self, kind: FolderKind) -> list[str]:
        return self.films if kind == "film" else self.series

    def add(self, kind: FolderKind) -> None:
        """"+ Add a folder": Windows' folder picker; a folder already listed isn't added twice.
        Not while a switch or a check runs."""
        if self.busy:
            return
        chosen = self._pick_folder(self.content.winfo_toplevel())
        if not chosen or not _exists(self.content):
            return
        folder = str(Path(chosen)) if "/" in chosen else chosen  # Windows separators, UNC kept
        folders = self._list(kind)
        if any(_same(folder, known) for known in folders):
            return
        folders.append(folder)
        self._changed()

    def remove(self, kind: FolderKind, index: int) -> None:
        """✕: the folder leaves the list (nothing on disk changes)."""
        folders = self._list(kind)
        if not self.busy and 0 <= index < len(folders):
            del folders[index]
            self._changed()

    def _changed(self) -> None:
        """Off: only the list changes (saved by "Use these folders"). On: read and saved now."""
        self.suggested = False
        self._message = None
        if self.state == "on":
            self._switch_on()
        else:
            self.refresh()

    def use(self) -> None:
        """"Use these folders": read them, and switch on once that is done."""
        if self.films or self.series:
            self._switch_on()

    def _switch_on(self) -> None:
        self._switching = True
        self._message = None
        self.refresh()
        self._run(self._switching_on(list(self.films), list(self.series)), "folder-library-on")

    async def _switching_on(self, films: list[str], series: list[str]) -> None:
        """The service's switch runs on its own: Settings closed meanwhile doesn't stop it."""
        was_on = self.state == "on"
        try:
            outcome = await self._folders.switch_on(films, series)
        finally:
            self._switching = False
        if not _exists(self.content):
            return
        if outcome == "unreachable":
            self._message = t(UNREACHABLE_CHANGE_TEXT if was_on else UNREACHABLE_ALL_TEXT)
        elif outcome == "slow":  # they answered: never "can't be reached"
            self._message = t(SLOW_CHANGE_TEXT if was_on else SLOW_ALL_TEXT)
        if outcome == "on" or was_on:
            # What is shown is what is saved (and read): a change that wasn't saved goes.
            self._take_saved()
        self.refresh()

    def _take_saved(self) -> None:
        current = self._settings.current
        self.films = [f for f in current.film_folders if f.strip()]
        self.series = [f for f in current.series_folders if f.strip()]
        self.suggested = False

    def check_now(self) -> None:
        if self.check_button is not None:
            self.check_button.configure(text=t(CHECKING_TEXT), state="disabled")
        self._folders.check_soon()

    def ask_stop(self) -> None:
        """"Stop using folders", asked first."""
        dialog = self.confirm
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            return
        owner = self.content.winfo_toplevel()
        self.confirm = ConfirmDialog(
            owner,
            title=t(STOP_TITLE),
            text=t(STOP_QUESTION),
            confirm=t(STOP_TEXT),
            on_confirm=self.stop,
        )
        # Settings is modal: it gets the grab back once the question goes.
        dialog = self.confirm
        dialog.bind("<Destroy>", lambda event: _regrab(event, dialog, owner), add="+")

    def stop(self) -> None:
        self._message = None
        self._run(self._folders.switch_off(), "folder-library-off")


def _regrab(event: Any, dialog: tk.Misc, owner: tk.Misc) -> None:
    if event.widget is not dialog:
        return
    try:
        if owner.winfo_exists() and isinstance(owner, ctk.CTkToplevel):
            owner.after_idle(lambda: _grab(owner))
    except tk.TclError:
        pass


def _grab(owner: tk.Misc) -> None:
    try:
        if owner.winfo_exists():
            owner.grab_set()
            owner.focus_force()
    except tk.TclError:
        pass


def _exists(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
