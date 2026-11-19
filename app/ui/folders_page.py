"""Settings → Folders, set once (docs/settings/2026-11-19-settings-redesign.md §4;
mockups/settings-reorganized.html): the "Series" and "Movies" boxes.

Each kind has **one list**, the folder library's (`Settings.series_folders` / `film_folders`):
the folders AirGrabber counts what's in them from (while "Count what's in them" is on,
app/ui/folder_library_settings.py) and where sorted files go. MediaForge's destination of the
kind (`ForgeSettings.series_folder` / `movies_folder`, through `Destinations`: the Forge side's
`LibraryDestinations` in app/forge/ui/settings_pages.py) is marked in it: "sorted series go here".

- Each folder is a 📁 row with its path, the mark or "Sort into this one" (makes it the
  destination), and ✕ (takes it out of the list; nothing on disk changes). Removing the
  destination's row clears the destination; an amber line then says sorted files stay where
  they were downloaded (the rules' notes on Sorting say so too). While the folders count, the
  last folder of both lists isn't taken out here: "Count what's in them" is switched off instead.
- "+ Add a folder" (Windows' folder picker) adds one; a kind's first folder (its list was empty)
  also becomes the destination when the kind has none. Any later folder only joins the list:
  adding a folder to count it never changes where sorted files go. A folder already listed isn't
  added twice.
- A destination that isn't in its list (chosen while this page couldn't see MediaForge, or by an
  import) shows as a row of its own, marked and "not counted" (it isn't in the list); its ✕
  clears the destination.
- Each add, remove or "Sort into this one" is one change (`Committer`: stored at once, refused in
  red in its box with nothing stored, "✓ Saved · Undo"). While the folders count, a list change is
  read first (FolderLibraryService's `switch_on`, as before: refused when no folder could be
  read); otherwise it is stored as it is (nothing is read or counted). A destination change is
  refused when a rule on Sorting would be (`Destinations.problem`), and stored as Sorting stores
  its settings (the rules that watch more wait once: nothing moves unseen). Not while a change,
  a switch (on or off: one busy state with "Count what's in them", `FoldersBusy`) or a check
  runs; an Undo is refused while another change of the folders runs. A change of both whose
  second half fails puts the first back (the box says why the change failed, not why putting
  it back did); Undo puts the destination back
  first, then the list, and the destination again as the change left it when the list is
  refused: never half a change.
- MediaForge may not be started: the page starts it on its first visit (`page_shown`) and shows
  the lists at once; the marks appear when it is ready (until then one muted line per box, or
  why it isn't available). Without MediaForge there are no marks at all.

The page follows what is stored (AirGrabber's settings, the folder library's status, the
destinations) and stops listening when it goes. Logs never name a folder.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import ntpath
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Literal, Protocol

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.dialog_kit import BUTTON_HEIGHT, SMALL_SIZE
from app.ui.folder_library_settings import FolderLibrary, FoldersBusy, read_and_switch_on
from app.ui.missing_control import mark_dot
from app.ui.settings_commit import Committer, Refused
from app.ui.settings_layout import TEXT_SIZE, SettingsBox, SettingsPage, box_inner
from app.ui.settings_pages_registry import FOLDERS_SPOT
from app.ui.settings_search import add_words
from app.ui.tasks import spawn
from app.ui.tooltip import Tooltip
from app.ui.widgets import elided

if TYPE_CHECKING:
    from app.services.settings import SettingsService

log = logging.getLogger(__name__)

FolderKind = Literal["series", "film"]
KINDS: tuple[FolderKind, ...] = ("series", "film")

ADD_TEXT = N_("+ Add a folder")
REMOVE_TIP = N_("Leave this folder out")
SORT_INTO_TEXT = N_("Sort into this one")
MARKS: dict[FolderKind, str] = {
    "series": N_("sorted series go here"),
    "film": N_("sorted films go here"),
}
NO_DESTINATION: dict[FolderKind, str] = {
    "series": N_("No Series folder yet: sorted series stay where they were downloaded."),
    "film": N_("No Movies folder yet: sorted films stay where they were downloaded."),
}
WAITING_TEXT = N_("Where sorted files go shows once MediaForge is ready.")
NOT_COUNTED = N_("not counted")
NOT_COUNTED_TIP = N_(
    "Sorted files go here, but it isn't in the list: what's in it isn't counted. "
    "\"+ Add a folder\" lists it."
)
# What Settings' search also finds the two boxes by: the old box's name, what they hold.
SEARCH_WORDS = (N_("Library folders"), N_("Where sorted files go"))

_FIELDS: dict[FolderKind, str] = {"series": "series_folders", "film": "film_folders"}
_TITLES: dict[FolderKind, str] = {"series": N_("Series"), "film": N_("Movies")}

ROW_RADIUS = 8
ROW_PAD = 10
REMOVE_SIDE = 26
ICON_WIDTH = 24
SORT_WIDTH = 150
MARK_HEIGHT = 22


def path_width() -> int:
    """The path's room in a folder row: the box's inside (the open window's page) less the 📁,
    the mark or "Sort into this one", ✕ and the row's padding."""
    return box_inner() - 2 * ROW_PAD - ICON_WIDTH - SORT_WIDTH - REMOVE_SIDE - 40


class Destinations(Protocol):
    """MediaForge's Series and Movies folders as the page marks them (the Forge side's
    LibraryDestinations; tests pass a stand-in)."""

    @property
    def ready(self) -> bool:
        """Whether the destinations are known (MediaForge has started)."""
        ...

    @property
    def reason(self) -> str:
        """Why MediaForge isn't available ("" while it starts, or once it has)."""
        ...

    def folder(self, kind: FolderKind) -> str: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...

    async def start(self) -> bool: ...

    def close(self) -> None: ...

    def problem(self, folders: Mapping[FolderKind, str]) -> str | None: ...

    async def store(self, folders: Mapping[FolderKind, str]) -> bool: ...


@dataclass
class FolderRow:
    """One folder of a box."""

    folder: str
    listed: bool  # in the list (else only the destination)
    marked: bool  # where sorted files of the kind go
    frame: ctk.CTkFrame
    remove: ctk.CTkButton | None  # ✕ (none for the last folder while they count)
    sort: ctk.CTkButton | None  # "Sort into this one" (once the marks are known)


def same_folder(a: str, b: str) -> bool:
    """Whether `a` and `b` are the same folder as Windows compares them (case, slashes, a
    trailing separator)."""
    # Twin of app/forge/core/folders_carry.py's `same_folder` (this module may not import
    # Forge): change both together.
    return _key(a) == _key(b)


def _key(folder: str) -> str:
    return ntpath.normcase(ntpath.normpath(folder.strip())).rstrip("\\/")


def _ask_folder(owner: tk.Misc) -> str:
    # Windows' own modal folder picker: the event loop pauses until it closes.
    return filedialog.askdirectory(parent=owner, mustexist=True) or ""


class FoldersPage:
    """The Folders page's "Series" and "Movies" boxes (see the module docstring)."""

    def __init__(
        self,
        page: SettingsPage,
        settings: SettingsService,
        committer: Committer,
        *,
        folders: FolderLibrary | None = None,
        destinations: Destinations | None = None,
        pick_folder: Callable[[tk.Misc], str] = _ask_folder,
        busy: FoldersBusy | None = None,
    ) -> None:
        """`folders`: the folder library (None: the lists are stored as they are); `destinations`:
        MediaForge's (None: no marks); `pick_folder(owner)`: a folder the user picked ("");
        `busy`: the state the lists share with "Count what's in them"'s switch (own if None)."""
        self._settings = settings
        self._committer = committer
        self._folders = folders
        self._destinations = destinations
        self._pick_folder = pick_folder
        self._busy = busy if busy is not None else FoldersBusy()  # changes under way
        self._started = False  # `page_shown` asked MediaForge to start
        self._draw_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.boxes: dict[FolderKind, SettingsBox] = {}
        self._lists: dict[FolderKind, ctk.CTkFrame] = {}
        self.rows: dict[FolderKind, list[FolderRow]] = {"series": [], "film": []}
        self.add_buttons: dict[FolderKind, ctk.CTkButton] = {}
        self.notes: dict[FolderKind, ctk.CTkLabel | None] = {"series": None, "film": None}
        for kind in KINDS:
            box = self.boxes[kind] = page.box(t(_TITLES[kind]))
            add_words(box.frame, *SEARCH_WORDS)
            content = self._lists[kind] = ctk.CTkFrame(box.body, fg_color="transparent")
            content.grid_columnconfigure(0, weight=1)
            box.add(content, gap=10)
            add = self.add_buttons[kind] = ctk.CTkButton(
                box.body,
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
            )
            box.add(add, gap=4, sticky="w")
        self.spots: dict[str, tk.Misc] = {FOLDERS_SPOT: self.boxes["series"].frame}
        self._stop: list[Callable[[], None]] = [
            settings.subscribe(lambda _s: self.refresh()),
            self._busy.subscribe(self.refresh),  # the count switch runs, or ended
        ]
        if folders is not None:
            self._stop.append(folders.subscribe(self.refresh))
        if destinations is not None:
            self._stop.append(destinations.subscribe(self.refresh))
        anchor = self._lists["series"]
        self._anchor = anchor
        # Tk's own bind: CTkFrame.bind would bind its canvas.
        tk.Misc.bind(anchor, "<Destroy>", self._on_destroy, add="+")
        self.show()

    # -- following what is stored --------------------------------------------------------------

    def refresh(self) -> None:
        """Draw again once the current event is done (the newest call wins)."""
        if not _exists(self._anchor):
            return
        if self._draw_task is not None and not self._draw_task.done():
            self._draw_task.cancel()
        self._draw_task = self._run(self._draw_later(), "FoldersPage.draw")

    async def _draw_later(self) -> None:
        await asyncio.sleep(0)
        if _exists(self._anchor):
            self.show()

    async def loaded(self) -> None:
        """Wait until nothing from here is under way (tests)."""
        while True:
            pending = {task for task in self._tasks if not task.done()}
            if not pending:
                return
            await asyncio.wait(pending)

    def _run(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self._anchor:
            return
        for stop in self._stop:
            stop()
        self._stop.clear()
        if self._draw_task is not None:
            self._draw_task.cancel()
        if self._destinations is not None:
            self._destinations.close()
        # A change under way is still stored; only the page isn't touched any more.

    def page_shown(self) -> None:
        """Settings shows the Folders page: MediaForge is started for the marks (once; started
        already, the destinations are followed from now on)."""
        destinations = self._destinations
        if self._started or destinations is None:
            return
        self._started = True
        self._run(destinations.start(), "FoldersPage.forge")

    # -- drawing -------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """A change from here, "Count what's in them"'s switch (on or off) or the folder
        library's check is under way."""
        return self._busy.busy or (self._folders is not None and self._folders.status.checking)

    def _counting(self) -> bool:
        return self._folders is not None and self._folders.status.on

    def listed(self, kind: FolderKind) -> list[str]:
        """The kind's list as stored."""
        folders: tuple[str, ...] = getattr(self._settings.current, _FIELDS[kind])
        return [folder for folder in folders if folder.strip()]

    def destination(self, kind: FolderKind) -> str | None:
        """Where sorted files of `kind` go ("": nowhere chosen), None while not known."""
        destinations = self._destinations
        if destinations is None or not destinations.ready:
            return None
        return destinations.folder(kind)

    def show(self) -> None:
        """Draw both boxes for what is stored now."""
        total = len(self.listed("series")) + len(self.listed("film"))
        for kind in KINDS:
            self._show_kind(kind, total)

    def _show_kind(self, kind: FolderKind, total: int) -> None:
        content = self._lists[kind]
        for child in content.winfo_children():
            child.destroy()
        self.rows[kind] = []
        self.notes[kind] = None
        busy = self.busy
        destination = self.destination(kind)
        shown: list[tuple[str, bool]] = [(folder, True) for folder in self.listed(kind)]
        if destination and not any(same_folder(destination, f) for f, _ in shown):
            shown.append((destination, False))
        row_no = 0
        for folder, listed in shown:
            marked = bool(destination) and same_folder(folder, destination or "")
            # While the folders count, the last one isn't taken out here: the switch is.
            removable = not (listed and self._counting() and total <= 1)
            row = self._folder_row(kind, folder, listed=listed, marked=marked,
                                   removable=removable, busy=busy)
            row.frame.grid(row=row_no, column=0, sticky="ew", pady=(0 if row_no == 0 else 6, 0))
            self.rows[kind].append(row)
            row_no += 1
        note: tuple[str, theme.Color] | None = None
        if self._destinations is not None:
            if destination is None:
                note = (self._destinations.reason or t(WAITING_TEXT), theme.TEXT_MUTED)
            elif not destination:
                note = (t(NO_DESTINATION[kind]), theme.WARNING)
        if note is not None:
            text, colour = note
            label = ctk.CTkLabel(
                content,
                text=text,
                image=mark_dot(content, colour) if colour == theme.WARNING else None,
                compound="left",
                font=theme.font(TEXT_SIZE),
                text_color=colour,
                anchor="w",
                justify="left",
                wraplength=box_inner() - 20,
            )
            label.grid(row=row_no, column=0, sticky="w", pady=(0 if row_no == 0 else 8, 0))
            self.notes[kind] = label
            row_no += 1
        if row_no:
            content.grid()
        else:
            content.grid_remove()  # an empty CTkFrame keeps its default size
        self.add_buttons[kind].configure(state="disabled" if busy else "normal")

    def _folder_row(
        self,
        kind: FolderKind,
        folder: str,
        *,
        listed: bool,
        marked: bool,
        removable: bool,
        busy: bool,
    ) -> FolderRow:
        content = self._lists[kind]
        frame = ctk.CTkFrame(
            content,
            fg_color=theme.BG,
            border_width=1,
            border_color=theme.PANEL_EDGE,
            corner_radius=ROW_RADIUS,
        )
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text="📁", width=ICON_WIDTH, font=theme.font(TEXT_SIZE)).grid(
            row=0, column=0, padx=(ROW_PAD, 4), pady=4
        )
        font = theme.font(TEXT_SIZE)
        room = path_width()
        if not listed:  # only the destination: "not counted", with why as its tip
            small = theme.font(SMALL_SIZE)
            cue = ctk.CTkLabel(
                frame, text=t(NOT_COUNTED), font=small, text_color=theme.TEXT_MUTED
            )
            cue.grid(row=0, column=2, padx=(8, 0), pady=4)
            Tooltip(cue, t(NOT_COUNTED_TIP))
            room -= int(small.measure(t(NOT_COUNTED))) + 8
        path = elided(folder, lambda text: font.measure(text) <= room)
        ctk.CTkLabel(frame, text=path, font=font, text_color=theme.TEXT, anchor="w").grid(
            row=0, column=1, sticky="ew", pady=4
        )
        state = "disabled" if busy else "normal"
        sort: ctk.CTkButton | None = None
        if marked:
            ctk.CTkLabel(
                frame,
                text=t(MARKS[kind]),
                width=0,
                height=MARK_HEIGHT,
                corner_radius=MARK_HEIGHT // 2,
                fg_color=theme.CHIP,
                text_color=theme.SUCCESS,
                font=theme.font(SMALL_SIZE, bold=True),
            ).grid(row=0, column=3, padx=(8, 0), pady=4, ipadx=8)
        elif self.destination(kind) is not None:
            sort = ctk.CTkButton(
                frame,
                text=t(SORT_INTO_TEXT),
                width=0,
                height=MARK_HEIGHT + 4,
                corner_radius=6,
                font=theme.font(SMALL_SIZE),
                fg_color="transparent",
                hover_color=theme.CHIP_HOVER,
                text_color=theme.LINK,
                command=functools.partial(self.sort_into, kind, folder),
                state=state,
            )
            sort.grid(row=0, column=3, padx=(8, 0), pady=4)
        remove: ctk.CTkButton | None = None
        if removable:
            remove = ctk.CTkButton(
                frame,
                text="✕",
                width=REMOVE_SIDE,
                height=REMOVE_SIDE,
                corner_radius=6,
                font=theme.font(SMALL_SIZE),
                fg_color="transparent",
                hover_color=theme.CHIP_HOVER,
                text_color=theme.TEXT_MUTED,
                command=functools.partial(self.remove, kind, folder),
                state=state,
            )
            remove.grid(row=0, column=4, padx=(4, 6), pady=4)
            Tooltip(remove, t(REMOVE_TIP))
        else:
            ctk.CTkFrame(frame, width=REMOVE_SIDE, height=1, fg_color="transparent").grid(
                row=0, column=4, padx=(4, 6)
            )
        return FolderRow(folder, listed, marked, frame, remove, sort)

    # -- changes -------------------------------------------------------------------------------

    def add(self, kind: FolderKind) -> asyncio.Task[bool] | None:
        """"+ Add a folder": Windows' folder picker; a kind's first folder (its list empty)
        becomes the destination too when the kind has none. Not while something else is under
        way."""
        if self.busy:
            return None
        chosen = self._pick_folder(self._anchor.winfo_toplevel())
        if not chosen or not _exists(self._anchor) or self.busy:
            return None
        folder = str(Path(chosen)) if "/" in chosen else chosen  # Windows separators, UNC kept
        listed = self.listed(kind)
        if any(same_folder(folder, known) for known in listed):
            return None
        destination = self.destination(kind)
        # Only the kind's first folder: adding one to count it never moves where files go.
        marks = {kind: folder} if destination == "" and not listed else None
        return self._change(kind, [*listed, folder], marks)

    def remove(self, kind: FolderKind, folder: str) -> asyncio.Task[bool] | None:
        """✕: the folder leaves the list (nothing on disk changes); the destination's row
        clears the destination."""
        if self.busy:
            return None
        listed = self.listed(kind)
        kept = [known for known in listed if not same_folder(known, folder)]
        if len(kept) == len(listed):
            new_list: list[str] | None = None  # the destination's own row
        elif self._counting() and not kept and not self._other(kind):
            return None  # the last one while they count: the switch takes them all
        else:
            new_list = kept
        destination = self.destination(kind)
        marks = {kind: ""} if destination and same_folder(destination, folder) else None
        if new_list is None and marks is None:
            return None
        return self._change(kind, new_list, marks)

    def sort_into(self, kind: FolderKind, folder: str) -> asyncio.Task[bool] | None:
        """"Sort into this one": `folder` becomes where sorted files of `kind` go."""
        destination = self.destination(kind)
        if self.busy or destination is None or same_folder(destination, folder):
            return None
        return self._change(kind, None, {kind: folder})

    def _other(self, kind: FolderKind) -> list[str]:
        return self.listed("film" if kind == "series" else "series")

    def _change(
        self,
        kind: FolderKind,
        new_list: Sequence[str] | None,
        marks: Mapping[FolderKind, str] | None,
    ) -> asyncio.Task[bool]:
        """One change of `kind`'s list (None: unchanged) and destinations (None: unchanged),
        stored together; Undo stores both as they were."""
        self._busy.begin()
        self.show()
        return self._run(self._store(kind, new_list, marks), "FoldersPage.change")

    async def _store(
        self,
        kind: FolderKind,
        new_list: Sequence[str] | None,
        marks: Mapping[FolderKind, str] | None,
    ) -> bool:
        destinations = self._destinations
        before_list = list(self.listed(kind)) if new_list is not None else None
        before_marks = (
            {name: destinations.folder(name) for name in marks}
            if marks is not None and destinations is not None
            else None
        )

        async def apply() -> bool:
            if marks is not None and destinations is not None:
                problem = destinations.problem(marks)
                if problem is not None:
                    raise Refused(problem)
            changed = False
            if new_list is not None:
                changed = await self._store_list(kind, new_list)
            if marks is not None and destinations is not None:
                try:
                    changed = await destinations.store(marks) or changed
                except Exception:
                    if new_list is not None and before_list is not None:
                        await self._put_back(kind, before_list)  # nothing stays half-stored
                    raise  # why the change failed (not why putting it back did)
            return changed

        async def undo() -> bool:
            return await self._busy.undoing(undo_now)  # refused while another change runs

        async def undo_now() -> bool:
            # The destination first (checked against the rules, cheap), then the list; a list
            # that can't go back (a folder that can't be read) takes the destination along.
            changed = False
            if before_marks is not None and destinations is not None:
                changed = await destinations.store(before_marks)
            if before_list is not None:
                try:
                    changed = await self._store_list(kind, before_list) or changed
                except Exception:
                    if before_marks is not None and destinations is not None and marks:
                        await self._put_back_marks(destinations, marks)
                    raise
            return changed

        try:
            return await self._committer.commit(apply, undo, box=self.boxes[kind])
        finally:
            self._busy.end()
            if _exists(self._anchor):
                self.show()

    async def _put_back(self, kind: FolderKind, folders: Sequence[str]) -> None:
        """The list as it was, after the destination half of a change failed; a refusal here
        isn't the one to say (the change's own is)."""
        try:
            await self._store_list(kind, folders)
        except Exception as exc:  # Refused, or the settings couldn't be written
            # The kind and the type only: never a folder.
            log.warning("Putting the %s folder list back failed (%s)", kind, type(exc).__name__)

    @staticmethod
    async def _put_back_marks(destinations: Destinations, marks: Mapping[FolderKind, str]) -> None:
        """The destinations as the change left them, after Undo's list was refused."""
        try:
            await destinations.store(marks)
        except Exception as exc:  # its own refusal isn't the one to say
            kinds = "+".join(sorted(marks))  # the kinds and the type only: never a folder
            log.warning("Putting the %s destination back failed (%s)", kinds, type(exc).__name__)

    async def _store_list(self, kind: FolderKind, folders: Sequence[str]) -> bool:
        """`kind`'s list stored: read first while the folders count (refused when none could
        be read), as it is otherwise. False when it is so already."""
        current = self._settings.current
        wanted = tuple(folders)
        if tuple(getattr(current, _FIELDS[kind])) == wanted:
            return False
        if self._folders is not None and self._counting():
            films = wanted if kind == "film" else current.film_folders
            series = wanted if kind == "series" else current.series_folders
            return await read_and_switch_on(self._folders, films, series, change=True)
        await self._settings.update(**{_FIELDS[kind]: wanted})
        return True


def _exists(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
