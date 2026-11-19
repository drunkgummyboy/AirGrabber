"""Settings → Folders: the "Count what's in them" box (docs/settings/2026-11-19-settings-redesign.md
§4; mockups/settings-reorganized.html) over FolderLibraryService (app/services/folder_library/
service.py; docs/folder-library/). The folders themselves are the page's lists
(app/ui/folders_page.py): one per kind, set once, also where sorted files go.

- The switch: whether what is in the folders counts as had (`folder_library_on`), with Plex
  connected beside Plex (the second shelf, BESIDE_PLEX_TEXT under it), without Plex as the
  library (INTRO_TEXT). Switching on reads the folders first and switches on once that is done
  (`switch_on`, as "Use these folders" did): while it reads the box says so; when no folder could
  be reached (or none was read in time) it is refused in red, nothing is switched on and the
  switch goes back. Switching off forgets what was read (`switch_off`; the folders stay set, no
  file is touched). Each is a change with the window's Undo bar (Undo switches back the same
  way). Without a folder in either list the switch can't be turned on.
- On: a green dot with "412 films · 38 series · 1,906 episodes · checked 3 min ago" and
  "↻ Check now" (`check_soon`: an action, no Undo), how many files couldn't be recognised (they
  are listed on the Library page), an amber dot per folder that couldn't be reached ("what was
  found there last time still counts"; one that answered but wasn't read in time "takes a
  while"; library_words' `reach_lines`, the Library page's too).

The box follows the service (`subscribe`, which also hears Plex connecting and disconnecting)
and stops when it goes. A switch outlives the window: the service runs it as its own task.
`read_and_switch_on` is the page's lists' way of storing a change while the folders count.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import customtkinter as ctk

from app.i18n import N_, number, t
from app.models.folder_library import FolderLibraryStatus, SwitchOutcome
from app.services.library_source import library_source
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, DETAIL_SIZE, SMALL_SIZE, secondary_button
from app.ui.library_words import CANT_REACH_TEXT as CANT_REACH_TEXT  # the box says it
from app.ui.library_words import SLOW_TEXT as SLOW_TEXT
from app.ui.library_words import reach_lines
from app.ui.missing_control import mark_dot
from app.ui.settings_commit import Committer, Refused
from app.ui.settings_layout import BOX_INNER, TEXT_SIZE, SettingsBox, SettingsPage
from app.ui.tasks import spawn
from app.ui.watchlist_cards import searched_ago

if TYPE_CHECKING:
    from app.services.settings import SettingsService

TITLE_TEXT = N_("Count what's in them")
SWITCH_TEXT = N_("Count these folders as “had”")
SWITCH_BESIDE_PLEX_TEXT = N_("Count these folders as “had”, beside Plex")
INTRO_TEXT = N_("No Plex? AirGrabber can read the folders that hold your films and series instead.")
BESIDE_PLEX_TEXT = N_(
    "A second shelf beside Plex: what's in these folders shows under Downloaded and counts as had."
)
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
COUNTS_TEXT = N_("{films} · {series} · {episodes} · checked {when}")
FILM_ONE, FILM_MORE = N_("{n} film"), N_("{n} films")
SERIES_ONE, SERIES_MORE = N_("{n} series"), N_("{n} series")
EPISODE_ONE, EPISODE_MORE = N_("{n} episode"), N_("{n} episodes")
UNRECOGNISED_ONE = N_("{n} file couldn't be recognised: it's listed on the Library page.")
UNRECOGNISED_MORE = N_("{n} files couldn't be recognised: they're listed on the Library page.")

CHECK_WIDTH = 140


class FolderLibrary(Protocol):
    """FolderLibraryService as Settings uses it (tests pass a stand-in)."""

    @property
    def status(self) -> FolderLibraryStatus: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...

    async def switch_on(self, films: Sequence[str], series: Sequence[str]) -> SwitchOutcome: ...

    async def switch_off(self) -> None: ...

    def check_soon(self, delay_s: float = 0) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def counted(one: str, more: str, n: int) -> str:
    """`one` / `more` (N_ texts) with `{n}` as "1,906" (Dutch: "1.906")."""
    return t(one if n == 1 else more, n=number(n, ","))


def counts_text(status: FolderLibraryStatus, now: datetime) -> str:
    """"412 films · 38 series · 1,906 episodes · checked 3 min ago" ("Not checked yet" before
    the first check)."""
    if status.checked_at is None:
        return t(NOT_CHECKED_TEXT)
    return t(
        COUNTS_TEXT,
        films=counted(FILM_ONE, FILM_MORE, status.films),
        series=counted(SERIES_ONE, SERIES_MORE, status.shows),
        episodes=counted(EPISODE_ONE, EPISODE_MORE, status.episodes),
        when=searched_ago(status.checked_at, now),
    )


def unrecognised_text(n: int) -> str:
    """"6 files couldn't be recognised: they're listed on the Library page."."""
    return counted(UNRECOGNISED_ONE, UNRECOGNISED_MORE, n)


async def read_and_switch_on(
    folders: FolderLibrary, films: Sequence[str], series: Sequence[str], *, change: bool
) -> bool:
    """Read `films` / `series` and switch on with them (the service saves them once read).
    True when stored; False when a later switch came first (nothing stored, nothing to say).
    Refused (said in red, nothing stored) when no folder could be reached or none was read in
    time; `change`: the folders count already and only the lists change (the words say so)."""
    outcome = await folders.switch_on(films, series)
    if outcome == "unreachable":
        raise Refused(t(UNREACHABLE_CHANGE_TEXT if change else UNREACHABLE_ALL_TEXT))
    if outcome == "slow":  # they answered: never "can't be reached"
        raise Refused(t(SLOW_CHANGE_TEXT if change else SLOW_ALL_TEXT))
    return outcome in ("on", "off")


class FolderLibraryBox:
    """The "Count what's in them" box on Settings → Folders (see the module docstring)."""

    def __init__(
        self,
        page: SettingsPage,
        folders: FolderLibrary,
        settings: SettingsService,
        committer: Committer,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._folders = folders
        self._settings = settings
        self._committer = committer
        self._clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()
        self._draw_task: asyncio.Task[None] | None = None
        self._switching = False  # a switch from here is under way
        self.state = "on" if folders.status.on else "off"
        self.box: SettingsBox = page.box(t(TITLE_TEXT))
        self._on = tk.BooleanVar(master=self.box.frame, value=self.state == "on")
        self.switch = ctk.CTkSwitch(
            self.box.body,
            text="",
            variable=self._on,
            height=BUTTON_HEIGHT - 8,
            font=theme.font(DETAIL_SIZE),
            progress_color=theme.ACCENT,
            text_color=theme.TEXT,
            command=self._switch_clicked,
        )
        self.box.add(self.switch, gap=10, sticky="w")
        self.content = ctk.CTkFrame(self.box.body, fg_color="transparent")
        self.box.add(self.content, gap=6)
        self.content.grid_columnconfigure(0, weight=1)
        self.check_button: ctk.CTkButton | None = None  # while on (`show`)
        # The service's status, and the lists (a folder added on the page enables the switch).
        self._stop = [folders.subscribe(self.refresh), settings.subscribe(lambda _s: self.refresh())]
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

    def _run(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.content:
            return
        for stop in self._stop:
            stop()
        self._stop.clear()
        if self._draw_task is not None:
            self._draw_task.cancel()  # a switch under way is the service's: it goes on

    # -- drawing -----------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """A switch or a check runs: the folders can't change meanwhile."""
        return self._switching or self._folders.status.checking

    def _beside_plex(self) -> bool:
        """Plex is connected: the folders are the second shelf."""
        return library_source(self._settings.current) == "plex"

    def _any_folder(self) -> bool:
        current = self._settings.current
        return any(f.strip() for f in (*current.film_folders, *current.series_folders))

    def show(self) -> None:
        """Draw the box for the service's status now."""
        status = self._folders.status
        self.state = "on" if status.on else "off"
        beside_plex = self._beside_plex()
        self.switch.configure(text=t(SWITCH_BESIDE_PLEX_TEXT if beside_plex else SWITCH_TEXT))
        if not self._switching and bool(self._on.get()) != status.on:
            self._on.set(status.on)  # as stored (a refusal, an Undo, a switch from before)
        can_switch = not self.busy and (status.on or self._any_folder())
        self.switch.configure(state="normal" if can_switch else "disabled")
        for child in self.content.winfo_children():
            child.destroy()
        self.check_button = None
        row = 0

        def place(widget: Any, *, gap: int = 6, sticky: str = "w") -> None:
            nonlocal row
            widget.grid(row=row, column=0, sticky=sticky, pady=(0 if row == 0 else gap, 0))
            row += 1

        place(self._text(t(BESIDE_PLEX_TEXT if beside_plex else INTRO_TEXT)))
        if self._switching and not status.on:
            place(self._text(t(READING_TEXT)))
        if status.on:
            self._place_status(place, status)

    def _place_status(self, place: Callable[..., None], status: FolderLibraryStatus) -> None:
        if status.checked_at is None:
            text = t(CHECKING_FOLDERS_TEXT if status.checking else NOT_CHECKED_TEXT)
        else:
            text = counts_text(status, self._clock())
        line = ctk.CTkFrame(self.content, fg_color="transparent")
        line.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            line,
            text=text,
            image=mark_dot(self.content, theme.SUCCESS),
            compound="left",
            font=theme.font(TEXT_SIZE),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=BOX_INNER - CHECK_WIDTH - 40,
        ).grid(row=0, column=0, sticky="w")
        busy = self.busy
        self.check_button = secondary_button(
            line, t(CHECKING_TEXT) if busy else t(CHECK_NOW_TEXT), self.check_now, width=CHECK_WIDTH
        )
        if busy:
            self.check_button.configure(state="disabled")
        self.check_button.grid(row=0, column=1, sticky="e", padx=(12, 0))
        place(line, gap=10, sticky="ew")
        if status.unrecognised:
            place(self._text(unrecognised_text(len(status.unrecognised)), size=SMALL_SIZE), gap=4)
        for reach in reach_lines(status):
            place(self._dot_line(reach, theme.WARNING))

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

    def _switch_clicked(self) -> None:
        self.set_counting(bool(self._on.get()))

    def set_counting(self, on: bool) -> asyncio.Task[bool] | None:
        """The switch: on reads the folders first (refused when none could be read), off
        forgets what was read; a change with Undo. Not while a switch or a check runs."""
        if self.busy or on == self._folders.status.on:
            self.show()
            return None
        self._switching = True
        self.show()
        return self._run(self._switch(on), "folder-library-switch")

    async def _switch(self, on: bool) -> bool:
        """The service's switch runs on its own: Settings closed meanwhile doesn't stop it."""
        try:
            return await self._committer.commit(
                self._counting_on if on else self._counting_off,
                self._counting_off if on else self._counting_on,
                box=self.box,
            )
        finally:
            self._switching = False
            if _exists(self.content):
                self.show()

    async def _counting_on(self) -> bool:
        current = self._settings.current
        return await read_and_switch_on(
            self._folders, current.film_folders, current.series_folders, change=False
        )

    async def _counting_off(self) -> bool:
        await self._folders.switch_off()
        return True

    def check_now(self) -> None:
        """"↻ Check now": the folders read again soon (an action: no Undo)."""
        if self.check_button is not None:
            self.check_button.configure(text=t(CHECKING_TEXT), state="disabled")
        self._folders.check_soon()


def _exists(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
