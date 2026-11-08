"""Settings' search box (above the sidebar): type "trailer" or "Plex", pick a match, and the window
shows its page scrolled to that box.

What it searches: every built page's boxes, by their title and the words in them (labels, tick
boxes, switches, choices), and each page's own title; a MediaForge page not built yet by the words
its sections list (`FORGE_SEARCH` in app/forge/ui/settings_pages.py). Whole words don't matter:
every word typed has to appear somewhere in a match, ignoring case."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme

PLACEHOLDER = N_("Search settings")
NO_MATCH = N_("Nothing found")
MAX_RESULTS = 8
RESULT_HEIGHT = 30


class Match(NamedTuple):
    """A place to go: its section, the box's title ("" for the page itself) and the box's frame
    (None for a page not built yet, or the page itself)."""

    section: str
    title: str
    frame: Any


class Place(NamedTuple):
    """What a search looks through: a match, and the words it is found by."""

    match: Match
    words: str


def _texts(widget: Any) -> list[str]:
    """The words shown in a box: its labels, tick boxes, switches and choices, nested too."""
    found: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop()
        if isinstance(current, (ctk.CTkLabel, ctk.CTkCheckBox, ctk.CTkSwitch, ctk.CTkRadioButton)):
            try:
                text = str(current.cget("text"))
            except (tk.TclError, ValueError):
                text = ""
            if text:
                found.append(text)
        elif isinstance(current, ctk.CTkSegmentedButton):
            found.extend(str(value) for value in current._value_list)
        try:
            stack.extend(current.winfo_children())
        except tk.TclError:
            pass
    return found


def places(
    pages: Mapping[str, Sequence[tuple[str, Any]]],
    unbuilt: Mapping[str, Sequence[str]],
) -> list[Place]:
    """Everything a search looks through. `pages`: per built section (untranslated name), its
    boxes as (shown title, frame); `unbuilt`: per section not built yet, the words it lists."""
    found: list[Place] = []
    for section, boxes in pages.items():
        found.append(Place(Match(section, "", None), t(section)))
        for title, frame in boxes:
            words = " ".join([title, *_texts(frame)])
            found.append(Place(Match(section, title, frame), f"{t(section)} {words}"))
    for section, listed in unbuilt.items():
        found.append(
            Place(Match(section, "", None), " ".join([t(section), *(t(w) for w in listed)]))
        )
    return found


def search(query: str, everything: Iterable[Place], limit: int = MAX_RESULTS) -> list[Match]:
    """The places whose words hold every word of `query`, boxes before whole pages, each once."""
    wanted = query.casefold().split()
    if not wanted:
        return []
    hits = [p.match for p in everything if all(w in p.words.casefold() for w in wanted)]
    hits.sort(key=lambda match: match.title == "")  # a box first, then a page
    seen: set[tuple[str, str]] = set()
    unique: list[Match] = []
    for match in hits:
        key = (match.section, match.title)
        if key not in seen:
            seen.add(key)
            unique.append(match)
    return unique[:limit]


def result_text(match: Match) -> str:
    """"Remembered matches · Naming", or the page's name alone."""
    return f"{match.title} · {t(match.section)}" if match.title else t(match.section)


class SettingsSearch(ctk.CTkFrame):
    """The box and, while something is typed, up to MAX_RESULTS matches under it. `look_up()`
    gives what to search right now; `on_pick(match)` goes there."""

    def __init__(
        self,
        master: Any,
        *,
        look_up: Callable[[], list[Place]],
        on_pick: Callable[[Match], None],
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._look_up = look_up
        self._on_pick = on_pick
        self.matches: list[Match] = []
        self.entry = ctk.CTkEntry(
            self,
            placeholder_text="🔍 " + t(PLACEHOLDER),
            height=32,
            font=theme.font(13),
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            text_color=theme.TEXT,
        )
        self.entry.pack(fill="x")
        self.entry.bind("<KeyRelease>", lambda _event: self.refresh(), add="+")
        self.entry.bind("<Return>", lambda _event: self._pick_first(), add="+")
        self.entry.bind("<Escape>", self._clear, add="+")
        self._results = ctk.CTkFrame(self, fg_color="transparent")

    def refresh(self) -> None:
        for child in self._results.winfo_children():
            child.destroy()
        query = self.entry.get()
        self.matches = search(query, self._look_up()) if query.strip() else []
        if not query.strip():
            self._results.pack_forget()
            return
        if not self._results.winfo_manager():
            self._results.pack(fill="x", pady=(4, 0))
        if not self.matches:
            ctk.CTkLabel(
                self._results,
                text=t(NO_MATCH),
                font=theme.font(12),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).pack(fill="x", padx=8)
            return
        for match in self.matches:
            ctk.CTkButton(
                self._results,
                text=result_text(match),
                anchor="w",
                height=RESULT_HEIGHT,
                font=theme.font(12),
                fg_color="transparent",
                hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT,
                command=lambda m=match: self._on_pick(m),
            ).pack(fill="x")

    def _pick_first(self) -> None:
        if self.matches:
            self._on_pick(self.matches[0])

    def _clear(self, _event: Any = None) -> str:
        self.entry.delete(0, "end")
        self.refresh()
        return "break"  # Escape doesn't also close Settings
