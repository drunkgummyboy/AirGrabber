"""Importing the shows followed on airdates.tv: paste the export, review the matches, import.

AirdatesImportPanel is shared by the first-run welcome (step 2) and the Import Shows dialog's
"From airdates.tv" tab. It previews through `services.airdates` and imports the ticked TVMaze
ids through `LibraryService.import_shows`; its host learns the outcome through `on_done(count)`.
Shows that couldn't be imported (TVMaze unreachable) are named under the result, with "Try
again" for just those. A "Paste" button beside the paste box pastes what was copied.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import Any, Protocol

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import AirdatesPreview, ImportMatch, ImportResult
from app.services.airdates import AirdatesError, parse_export
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.dialog_kit import (
    BODY_SIZE,
    CONTENT_WIDTH,
    DETAIL_SIZE,
    LINE_GAP,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines

AIRDATES_URL = "https://www.airdates.tv/"
# Shown through t().
NETWORK_ERROR = N_("Couldn't reach airdates.tv. Check your internet connection and try again.")
REVIEW_HINT = N_("Untick any that don't look right.")
NOTHING_TO_PASTE = N_("Nothing to paste yet: copy the text on airdates.tv first.")

Progress = Callable[[int, int], None]

_NUMBER_SIZE = 17
_NUMBER_WIDTH = 28
_ROW_GAP = 14
_SIDE_BUTTON_WIDTH = 150
_REVIEW_HEADING_SIZE = 20
_PASTE_HEIGHT = 84  # about three lines of DETAIL_SIZE text
_CHECKBOX_SIZE = 22
_ROW_TEXT_GAP = 10
_LIST_INSET = 44  # list padding, its scrollbar and the checkbox column
_PASTE_BUTTON_WIDTH = 100
_PASTE_BUTTON_HEIGHT = 32
_DETAIL_LINES = 2  # the "Couldn't import: …" line wraps to at most this many lines


class PreviewFinder(Protocol):
    """`services.airdates` (AirdatesImportService), or a fake in screenshots and tests."""

    async def find(
        self, export_text: str, progress: Progress | None = None
    ) -> AirdatesPreview: ...


class ShowImporter(Protocol):
    """`services.library` (LibraryService), or a fake in screenshots and tests."""

    async def import_shows(
        self, entries: Sequence[str], progress: Progress | None = None
    ) -> ImportResult: ...


# -- texts and decisions (pure, tested) ----------------------------------------------------------


def ticked_by_default(match: ImportMatch) -> bool:
    """Found shows not followed yet start ticked; already-followed ones have no checkbox."""
    return match.found and not match.already_tracked


def _plain(name: str) -> str:
    return "".join(char for char in name.casefold() if char.isalnum())


def from_note(match: ImportMatch) -> str | None:
    """' · from "<source name>"' when airdates.tv's name differs from TVMaze's beyond case and
    punctuation, else None."""
    if match.show_name is None or _plain(match.source_name) == _plain(match.show_name):
        return None
    return " · " + t('from "{name}"', name=match.source_name)


def total_shows(preview: AirdatesPreview) -> int:
    return len(preview.matches) + len(preview.unknown_ids) + preview.failed


def review_heading(preview: AirdatesPreview) -> str:
    """'Found 35 of 37 shows': found on TVMaze, of everything in the export."""
    found = sum(match.found for match in preview.matches)
    total = total_shows(preview)
    return tn("Found {found} of {n} show", "Found {found} of {n} shows", total, found=found)


def review_notes(preview: AirdatesPreview) -> list[str]:
    """The muted lines under the list: names not found, ids airdates.tv didn't know, failures."""
    lines: list[str] = []
    missing = [match.source_name for match in preview.matches if not match.found]
    if missing:
        lines.append(t("Couldn't find: {names}", names=", ".join(missing)))
    if unknown := len(preview.unknown_ids):
        lines.append(
            tn(
                "airdates.tv didn't recognise {n} show",
                "airdates.tv didn't recognise {n} shows",
                unknown,
            )
        )
    if preview.failed:
        lines.append(t("{n} couldn't be looked up; try again later", n=preview.failed))
    return lines


def nothing_reached(preview: AirdatesPreview) -> bool:
    """Every lookup failed: airdates.tv (or TVMaze) couldn't be reached at all."""
    return preview.failed > 0 and not preview.matches and not preview.unknown_ids


def looking_up_text(done: int, total: int) -> str:
    """'Looking up 12 of 37…' from `find`'s progress, which counts two lookups per show."""
    shows = max(1, total // 2)
    return t("Looking up {done} of {total}…", done=min(shows, done // 2 + 1), total=shows)


def importing_text(done: int, total: int) -> str:
    return t("Importing {done} of {total}…", done=min(total, done + 1), total=total)


def import_label(ticked: int) -> str:
    return tn("Import {n} show", "Import {n} shows", ticked)


def imported_text(count: int) -> str:
    return tn("Imported {n} show", "Imported {n} shows", count)


def missed_entries(result: ImportResult) -> list[str]:
    """The entries (TVMaze ids) an import didn't add: lookups that failed, then unknown ids."""
    return [*result.failed, *result.not_found]


def import_notes(result: ImportResult, names: Mapping[str, str] | None = None) -> str:
    """A muted line after an import naming the shows that didn't make it (TVMaze unreachable):
    "Couldn't import: Fargo — try again later". `names` maps the imported entries (TVMaze ids)
    to the names shown; an entry without one shows as it is."""
    missed = missed_entries(result)
    if not missed:
        return ""
    known = names or {}
    shown = ", ".join(known.get(entry, entry) for entry in missed)
    return t("Couldn't import: {names} — try again later", names=shown)


def retry_ids(result: ImportResult) -> list[int]:
    """The TVMaze ids "Try again" imports: the ones this import didn't add."""
    return [int(entry) for entry in missed_entries(result) if entry.isascii() and entry.isdigit()]


def _open_airdates() -> None:
    webbrowser.open(AIRDATES_URL)


# -- widget --------------------------------------------------------------------------------------


# A preview with every kind of row and all three notes: a host with a fixed size (the welcome)
# lays the review out with it once to find the panel's tallest size.
SIZING_PREVIEW = AirdatesPreview(
    matches=(
        ImportMatch(source_name="A", show_id=1, show_name="A", year=2000),
        ImportMatch(source_name="B", show_id=2, show_name="B", already_tracked=True),
        ImportMatch(source_name="C"),
    ),
    unknown_ids=(1,),
    failed=1,
)


class _Row:
    def __init__(self, match: ImportMatch, ticked: tk.BooleanVar | None) -> None:
        self.match = match
        self.ticked = ticked
        self.checkbox: ctk.CTkCheckBox | None = None


class AirdatesImportPanel(ctk.CTkFrame):
    """Paste stage (three numbered steps, the paste box, Find my shows) and, once the preview
    arrives, the review stage (the matches to tick, Back, Import N shows) in the same place.

    `on_done(count)` follows a finished import with the number of shows added.
    """

    def __init__(
        self,
        master: Any,
        *,
        airdates: PreviewFinder,
        library: ShowImporter,
        on_done: Callable[[int], None] | None = None,
        width: int = CONTENT_WIDTH,
        list_height: int = 170,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._airdates = airdates
        self._library = library
        self._on_done = on_done
        self._width = width
        self._list_height = list_height
        self._rows: list[_Row] = []
        self._task: asyncio.Task[None] | None = None
        self._imported = 0  # shows added since the review showed, "Try again" included
        self._retry_ids: list[int] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        # Both stages share one cell (the one on top shows), so the panel keeps its size.
        ctk.CTkFrame(self, width=width, height=1, fg_color="transparent").grid(
            row=0, column=0, sticky="n"
        )
        self._paste = self._build_paste()
        self._paste.grid(row=0, column=0, sticky="nsew")
        self._review = self._build_review()
        self._review.grid(row=0, column=0, sticky="nsew")
        self._paste.tkraise()

    # -- paste stage -----------------------------------------------------------------------------

    def _build_paste(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(1, weight=1)
        text_width = self._width - _NUMBER_WIDTH - 12 - _SIDE_BUTTON_WIDTH - 16

        self._number(page, 0, "1")
        text_label(
            page, t("Open airdates.tv in the browser you use for it"), wraplength=text_width
        ).grid(row=0, column=1, sticky="w")
        secondary_button(
            page, t("Open airdates.tv"), _open_airdates, width=_SIDE_BUTTON_WIDTH
        ).grid(row=0, column=2, sticky="e", padx=(16, 0))

        self._number(page, 1, "2")
        step2 = ctk.CTkFrame(page, fg_color="transparent")
        step2.grid(row=1, column=1, columnspan=2, sticky="w", pady=(_ROW_GAP, 0))
        first = ctk.CTkFrame(step2, fg_color="transparent")
        first.pack(anchor="w")
        for text, color in (
            (t("At the very bottom, click "), theme.TEXT_MUTED),
            ('"Export your color selection"', theme.TEXT),  # airdates.tv's own (English) words
        ):
            text_label(first, text, color=color, wraplength=0).pack(side="left")
        text_label(step2, t("and copy the text"), color=theme.TEXT_MUTED, wraplength=0).pack(
            anchor="w"
        )

        self._number(page, 2, "3")
        text_label(page, t("Paste it here"), wraplength=text_width).grid(
            row=2, column=1, sticky="w", pady=(_ROW_GAP, 0)
        )
        self._paste_button = secondary_button(
            page, t("Paste"), self.paste, width=_PASTE_BUTTON_WIDTH
        )
        self._paste_button.configure(height=_PASTE_BUTTON_HEIGHT)
        self._paste_button.grid(row=2, column=2, sticky="se", padx=(16, 0), pady=(_ROW_GAP, 0))

        self._text = ctk.CTkTextbox(
            page,
            height=_PASTE_HEIGHT,
            corner_radius=8,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            font=theme.font(DETAIL_SIZE),
            wrap="char",
        )
        self._text.grid(row=3, column=1, sticky="ew", pady=(10, 0))
        self._find_button = primary_button(
            page, t("Find my shows"), self._find, width=_SIDE_BUTTON_WIDTH
        )
        self._find_button.grid(row=3, column=2, sticky="ne", padx=(16, 0), pady=(10, 0))
        self._paste_status = text_label(
            page, "", size=DETAIL_SIZE, wraplength=self._width - _NUMBER_WIDTH - 12
        )
        self._paste_status.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))
        return page

    def _number(self, page: ctk.CTkFrame, row: int, text: str) -> None:
        ctk.CTkLabel(
            page,
            text=text,
            width=_NUMBER_WIDTH,
            anchor="nw",
            font=theme.font(_NUMBER_SIZE),
            text_color=theme.TEXT_MUTED,
        ).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=(0 if row == 0 else _ROW_GAP, 0))

    def focus_paste(self) -> None:
        self._text.focus_set()

    def paste(self) -> None:
        """The Paste button: what was copied goes into the box at the cursor (replacing a
        selection), as Ctrl+V does; with nothing copied, a line says so."""
        try:
            copied = self.clipboard_get()
        except tk.TclError:  # an empty clipboard, or no text on it
            copied = ""
        if not copied.strip():
            self._paste_status.configure(text=t(NOTHING_TO_PASTE), text_color=theme.WARNING)
            return
        box = self._text
        if box.tag_ranges("sel"):
            box.delete("sel.first", "sel.last")
        box.insert("insert", copied)
        box.see("insert")
        box.focus_set()
        self._paste_status.configure(text="")

    def _find(self) -> None:
        if self._task is not None and not self._task.done():
            return
        text = self._text.get("1.0", "end").strip()
        try:
            count = len(parse_export(text))
        except AirdatesError as exc:
            self._paste_status.configure(text=str(exc), text_color=theme.DANGER)
            return
        self._start(self._run_find(text, count), "airdates-find")

    async def _run_find(self, text: str, count: int) -> None:
        self._find_button.configure(state="disabled")
        self._paste_status.configure(
            text=looking_up_text(0, 2 * count), text_color=theme.TEXT_MUTED
        )
        try:
            preview = await self._airdates.find(text, self._find_progress)
        except AirdatesError as exc:
            self._paste_failed(str(exc))
            return
        except TVMazeError:
            self._paste_failed(t(NETWORK_ERROR))
            return
        except BaseException:
            if self.winfo_exists():
                self._find_button.configure(state="normal")
                self._paste_status.configure(text="")
            raise
        if not self.winfo_exists():
            return
        self._find_button.configure(state="normal")
        if nothing_reached(preview):
            self._paste_failed(t(NETWORK_ERROR))
            return
        self._paste_status.configure(text="")
        self.show_review(preview)

    def _find_progress(self, done: int, total: int) -> None:
        if self.winfo_exists():
            self._paste_status.configure(
                text=looking_up_text(done, total), text_color=theme.TEXT_MUTED
            )

    def _paste_failed(self, message: str) -> None:
        if self.winfo_exists():
            self._find_button.configure(state="normal")
            self._paste_status.configure(text=message, text_color=theme.DANGER)

    # -- review stage ----------------------------------------------------------------------------

    def _build_review(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        self._heading = ctk.CTkLabel(
            page,
            text="",
            font=theme.font(_REVIEW_HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        )
        self._heading.grid(row=0, column=0, sticky="w")
        text_label(page, t(REVIEW_HINT), size=DETAIL_SIZE, color=theme.TEXT_MUTED).grid(
            row=1, column=0, sticky="w", pady=(LINE_GAP, 0)
        )
        self._list = ctk.CTkScrollableFrame(
            page,
            height=self._list_height,
            corner_radius=8,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
        )
        self._list.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        self._list.grid_columnconfigure(1, weight=1)
        self._notes = text_label(
            page, "", size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=self._width
        )
        self._notes.grid(row=3, column=0, sticky="w", pady=(10, 0))

        bottom = ctk.CTkFrame(page, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        bottom.grid_columnconfigure(0, weight=1)
        status = ctk.CTkFrame(bottom, fg_color="transparent")
        status.grid(row=0, column=0, sticky="w")
        status_width = self._width - 2 * _SIDE_BUTTON_WIDTH - 32
        self._review_status = text_label(status, "", size=DETAIL_SIZE, wraplength=status_width)
        self._review_status.pack(anchor="w")
        self._review_detail = text_label(
            status, "", size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=status_width
        )
        self._back_button = secondary_button(
            bottom, t("Back"), self._back, width=_SIDE_BUTTON_WIDTH - 50
        )
        self._back_button.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self._import_button = primary_button(
            bottom, import_label(0), self._import, width=_SIDE_BUTTON_WIDTH
        )
        self._import_button.grid(row=0, column=2, sticky="e", padx=(10, 0))
        # In the Import button's place once some shows couldn't be imported: just those again.
        self._retry_button = primary_button(
            bottom, t("Try again"), self._retry, width=_SIDE_BUTTON_WIDTH
        )
        self._status_width = status_width
        return page

    def show_review(self, preview: AirdatesPreview) -> None:
        """Replace the paste stage with the review of `preview`."""
        for child in self._list.winfo_children():
            child.destroy()
        self._rows = []
        self._heading.configure(text=review_heading(preview))
        row_index = 0
        for match in preview.matches:
            if match.found:
                self._add_row(row_index, match)
                row_index += 1
        # One line each (a long "Couldn't find" list is elided), so a fixed-size host fits them.
        measure = theme.font(DETAIL_SIZE).measure
        notes = [
            elide_to_width(line, self._width - SLACK, measure) for line in review_notes(preview)
        ]
        self._notes.configure(text="\n".join(notes))
        if notes:
            self._notes.grid()
        else:
            self._notes.grid_remove()
        self._review_status.configure(text="")
        self._review_detail.configure(text="")
        self._review_detail.pack_forget()
        self._imported = 0
        self._retry_ids = []
        self._retry_button.grid_remove()
        self._back_button.configure(state="normal")
        self._import_button.grid()
        self._update_import_button()
        self._list._parent_canvas.yview_moveto(0)  # CTk has no scroll API
        self._review.tkraise()

    def _add_row(self, index: int, match: ImportMatch) -> None:
        pady = (8 if index == 0 else 2, 2)
        text_width = self._width - _LIST_INSET - _CHECKBOX_SIZE - _ROW_TEXT_GAP
        texts = ctk.CTkFrame(self._list, fg_color="transparent")
        texts.grid(row=index, column=1, sticky="w", padx=(0, 8), pady=pady)
        if not ticked_by_default(match):  # already following
            following = t("{name} · already following", name=match.show_name)
            self._row_texts(texts, [(following, theme.TEXT_MUTED)], text_width)
            self._rows.append(_Row(match, None))
            return
        ticked = tk.BooleanVar(master=self, value=True)
        row = _Row(match, ticked)
        row.checkbox = ctk.CTkCheckBox(
            self._list,
            text="",
            width=_CHECKBOX_SIZE,
            checkbox_width=_CHECKBOX_SIZE,
            checkbox_height=_CHECKBOX_SIZE,
            corner_radius=5,
            border_width=2,
            border_color=theme.EDGE,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            variable=ticked,
            onvalue=True,
            offvalue=False,
            command=self._update_import_button,
        )
        row.checkbox.grid(row=index, column=0, sticky="w", padx=(10, _ROW_TEXT_GAP), pady=pady)
        parts: list[tuple[str, theme.Color]] = [
            (match.show_name or match.source_name, theme.TEXT)
        ]
        if match.year is not None:
            parts.append((f"  {match.year}", theme.TEXT_MUTED))
        if (note := from_note(match)) is not None:
            parts.append((note, theme.TEXT_MUTED))
        labels = self._row_texts(texts, parts, text_width, cursor="hand2")
        for widget in (texts, *labels):
            widget.bind("<Button-1>", lambda _event, r=row: self._toggle(r), add="+")
        self._rows.append(row)

    def _row_texts(
        self,
        master: ctk.CTkFrame,
        parts: list[tuple[str, theme.Color]],
        width: int,
        *,
        cursor: str = "",
    ) -> list[ctk.CTkLabel]:
        """One line of differently coloured parts, the last ones elided to fit `width`."""
        measure = theme.font(BODY_SIZE).measure
        left = width - SLACK
        labels: list[ctk.CTkLabel] = []
        for text, color in parts:
            fitted = elide_to_width(text, left, measure)
            if not fitted:
                break
            label = ctk.CTkLabel(
                master,
                text=fitted,
                font=theme.font(BODY_SIZE),
                text_color=color,
                anchor="w",
                cursor=cursor,
            )
            label.pack(side="left")
            labels.append(label)
            left -= measure(fitted)
            if fitted != text:
                break
        return labels

    def _toggle(self, row: _Row) -> None:
        if row.ticked is None or row.checkbox is None or row.checkbox.cget("state") == "disabled":
            return
        row.ticked.set(not row.ticked.get())
        self._update_import_button()

    def ticked_ids(self) -> list[int]:
        return [
            row.match.show_id
            for row in self._rows
            if row.ticked is not None and row.ticked.get() and row.match.show_id is not None
        ]

    def _update_import_button(self) -> None:
        count = len(self.ticked_ids())
        self._import_button.configure(
            text=import_label(count), state="normal" if count else "disabled"
        )

    def _back(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self.show_paste()
        self._text.focus_set()

    def show_paste(self) -> None:
        """Back to the paste stage (the pasted text is kept)."""
        self._paste.tkraise()

    def _import(self) -> None:
        ids = self.ticked_ids()
        if not ids or (self._task is not None and not self._task.done()):
            return
        self._imported = 0
        self._start(self._run_import(ids), "airdates-import")

    def _retry(self) -> None:
        """Try again: import just the shows the last import didn't add."""
        ids = list(self._retry_ids)
        if not ids or (self._task is not None and not self._task.done()):
            return
        self._start(self._run_import(ids), "airdates-import-retry")

    def show_names(self) -> dict[str, str]:
        """The review's shows by the entry `import_shows` gets for them (the TVMaze id)."""
        return {
            str(row.match.show_id): row.match.show_name or row.match.source_name
            for row in self._rows
            if row.match.show_id is not None
        }

    async def _run_import(self, ids: list[int]) -> None:
        self._set_review_busy(True)
        self._retry_button.grid_remove()
        self._review_detail.pack_forget()
        self._review_status.configure(
            text=importing_text(0, len(ids)), text_color=theme.TEXT_MUTED
        )
        try:
            result = await self._library.import_shows(
                [str(show_id) for show_id in ids], self._import_progress
            )
        except BaseException:
            if self.winfo_exists():
                self._set_review_busy(False)
                self._review_status.configure(text="")
                if self._retry_ids:  # a "Try again" that didn't finish: it can be tried again
                    self._import_button.grid_remove()
                    self._retry_button.grid()
            raise
        if not self.winfo_exists():
            return
        self._imported += len(result.added)
        self._retry_ids = retry_ids(result)
        self._back_button.configure(state="normal")
        self._import_button.grid_remove()
        self._review_status.configure(
            text=imported_text(self._imported), text_color=theme.SUCCESS
        )
        if detail := import_notes(result, self.show_names()):
            measure = theme.font(DETAIL_SIZE).measure
            fitted = fit_to_lines(detail, self._status_width - SLACK, _DETAIL_LINES, measure)
            self._review_detail.configure(text=fitted)
            self._review_detail.pack(anchor="w")
        if self._retry_ids:
            self._retry_button.configure(state="normal")
            self._retry_button.grid(row=0, column=2, sticky="e", padx=(10, 0))
        if self._on_done is not None:
            self._on_done(self._imported)

    def _import_progress(self, done: int, total: int) -> None:
        if self.winfo_exists():
            self._review_status.configure(text=importing_text(done, total))

    def _set_review_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._back_button.configure(state=state)
        self._import_button.configure(state=state)
        self._retry_button.configure(state=state)
        for row in self._rows:
            if row.checkbox is not None:
                row.checkbox.configure(state=state)
        if not busy:
            self._update_import_button()

    # -- tasks -----------------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        self._task = spawn(coro, name=name)

    def destroy(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        super().destroy()

