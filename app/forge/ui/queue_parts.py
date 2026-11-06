"""The queue page's constants and pure helpers (the view: app/forge/ui/queue_view.py): the option
buttons, the sizes, a band's `Choice`, the Forge summary line. No widgets are made here, so Settings
can read `OPTION_LABELS` without loading the queue."""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.forge.core.executor import RunResult
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.media import TypeHint
from app.forge.models.preset import Preset, PresetOptions, with_options
from app.forge.ui.look import Pair
from app.forge.ui.queue_rows import Kind, Show, item_kind
from app.forge.ui.widgets.virtual_list import Sizes
from app.i18n import N_, t, tn
from app.ui import theme

# A preset's extras by their own names (Settings › Naming shows them).
OPTION_LABELS: dict[str, str] = {
    "move_to_library": N_("Move to library folder"),
    "movie_poster": N_("Movie poster"),
    "series_poster": N_("Series poster"),
    "season_poster": N_("Season posters"),
    "trailer": N_("Trailer"),
    "clean_junk": N_("Clean junk"),
    "prune_empty": N_("Remove empty folders"),
    "subtitles": N_("Subtitles"),
    "quality_in_name": N_("Quality in the file name"),
    "nfo": N_("NFO files"),
    "backdrop": N_("Background picture (fanart)"),
}

SUBTITLES_OPTION = "subtitles"  # shown only while this version has a key
QUALITY_OPTION = "quality_in_name"  # its button shows whether the rule has the code


@dataclass(frozen=True, slots=True)
class OptionButtonSpec:
    """An option button of the preset card: its name, its explanation (the tooltip) and the
    preset option it switches for shows and for films (None: not for that kind)."""

    name: str
    tip: str
    series: str | None
    movies: str | None


MOVE = N_("Move")

EXTRAS = N_("Extras")

TIDY_UP = N_("Tidy up")

MOVE_BUTTON = N_("To library folder")

MOVE_TIP = N_(
    "After forging, the files go to your Series or Movies folder. Off: they are only renamed, "
    "where they are."
)

MOVE_ALWAYS_TIP = N_("Downloads MediaForge finds always move to your library folder.")

NO_LIBRARY_FOLDER = N_("{n} file wasn't forged: choose its library folder in Settings › Auto-rename.")

NO_LIBRARY_FOLDERS = N_(
    "{n} files weren't forged: choose their library folder in Settings › Auto-rename."
)

NAME_GROUP = N_("Name")

OPTION_GROUPS: tuple[tuple[str, tuple[OptionButtonSpec, ...]], ...] = (
    (
        MOVE,
        (OptionButtonSpec(MOVE_BUTTON, MOVE_TIP, "move_to_library", "move_to_library"),),
    ),
    (
        NAME_GROUP,
        (
            OptionButtonSpec(
                N_("Quality"),
                N_(
                    "Adds the quality the current file name mentions, like 1080p H.265, to the "
                    "new name"
                ),
                QUALITY_OPTION,
                QUALITY_OPTION,
            ),
        ),
    ),
    (
        EXTRAS,
        (
            OptionButtonSpec(
                N_("Poster"),
                N_("Saves the film or show poster next to the files"),
                "series_poster",
                "movie_poster",
            ),
            OptionButtonSpec(
                N_("Season posters"), N_("One poster per season folder"), "season_poster", None
            ),
            OptionButtonSpec(
                N_("Trailer"), N_("Downloads the trailer next to the files"), "trailer", "trailer"
            ),
            OptionButtonSpec(
                N_("Subtitles"),
                N_("Gets a subtitle per chosen language right after the rename, named to match"),
                SUBTITLES_OPTION,
                SUBTITLES_OPTION,
            ),
            OptionButtonSpec(
                N_("Background"),
                N_("Saves the film's or show's background picture (fanart) with the files"),
                "backdrop",
                "backdrop",
            ),
            OptionButtonSpec(
                N_("NFO"),
                N_("Saves an NFO file with the title, year and summary, for Kodi and Jellyfin"),
                "nfo",
                "nfo",
            ),
        ),
    ),
    (
        TIDY_UP,
        (
            OptionButtonSpec(
                N_("Clean junk"),
                N_("Removes sample files, .nfo and .txt left by the release"),
                "clean_junk",
                "clean_junk",
            ),
            OptionButtonSpec(
                N_("Empty folders"),
                N_("After moving, deletes folders that are left empty"),
                "prune_empty",
                "prune_empty",
            ),
        ),
    ),
)

_KINDS: tuple[Kind, ...] = ("series", "movies")

FOR_LINE = N_("For: {name}")

FOR_NEW = N_("For: new files")

USE_FOR_ALL = N_("Use for all ▾")

ALL_FILMS = N_("All films ({n})")

ALL_SHOWS = N_("All shows ({n})")

EVERYTHING = N_("Everything in the queue ({n})")

NO_FIT = N_(" · doesn't fit")

CHANGED = N_("Changed for this item")

CUSTOM_TIP = N_("Type a naming rule for this item only; it isn't saved.")

_FOR_MAX = 300  # the "For:" line's name at most (CTk units; cut with "…")

FILTERS: tuple[tuple[Show, str], ...] = (
    ("all", N_("All")),
    ("needs", N_("Needs you")),
    ("ready", N_("Ready")),
)

# The empty queue when Explorer drops can't reach it (attaching failed).
NO_DROPS_TITLE = N_("Add video files or folders")

NO_DROPS_HINT = N_("with the buttons above. Nothing is renamed until you click Forge.")

NO_EXAMPLE = N_("Add files to see how they'll be named.")

# The queue's bigger lines (mockups/forge-chosen.html): CTk units.
QUEUE_SIZES = Sizes(
    item_title=13,
    band_title=21,
    band_sub=14,
    button=14,
    note=13,
    line=16,
    code=16,
    mono=13,
    state=13,
    button_height=34,
    button_pad=15,
    band_gap=10,
    band_gap_below=False,
    band_radius=10,
    band_inset=16,
    poster=(58, 86),
    cards=True,
)

BAND_HEIGHT = 10 + 86 + 2 * 12  # the gap above, the poster and the band's padding

PAD_X = 0  # the card and the filters' row in the page, which has AirGrabber's margin

LIST_PAD = (0, 0)  # the list: its cards line up with the preset card

LINE_HEIGHT = 40

# The card's colours: (light, dark) pairs from the Forge look's tokens.
_CODE = re.compile(r"\{[^{}]*\}")

_PROGRESS_STATUS = {
    "working": ItemStatus.WORKING,
    "done": ItemStatus.DONE,
    "error": ItemStatus.ERROR,
    "skipped": ItemStatus.SKIPPED,
}

_REFRESH_EVENT_MS = 80  # a burst of queue events becomes one refresh

_CARD_BAKERS = 3  # card pictures fetched and baked at the same time, the latest asked first

FILE_GONE = N_("That file isn't there any more.")

# Preview's lines are in Activity: the status bar says where.
PREVIEW_READY = N_("Preview is in History › Activity. Nothing has been changed.")

# ... and while events keep coming (1,000 lookups landing for a minute), refreshes take at most
# this share of the window's time: the next one waits for the last one's cost / share (F-33).
_REFRESH_SHARE = 0.25

_REFRESH_FORMAT_MS = 150  # typing a Custom rule

_REFRESH_PROGRESS_MS = 60

_TYPING_CLASSES = ("Entry", "Text", "TEntry")  # a key there is typing, never a shortcut

_KEYS: dict[str, str] = {
    "<Return>": "preview",
    "<Control-Return>": "forge",
    "<Control-z>": "undo",
    "<Control-Z>": "undo",
    "<Up>": "up",
    "<Down>": "down",
}


type AskFn = Callable[[str, str, Sequence[str]], Awaitable[str | None]]
"""A question with a button per choice: the chosen label, or None (closed)."""


type AskTextFn = Callable[[str, str, str], Awaitable[str | None]]
"""A question for a short text (title, message, main button): the text, or None (closed)."""


def card_colours(look: theme.Look) -> tuple[Pair, Pair, Pair]:
    """The preset card's (ground, edge, the naming rule's box): warm in both looks."""
    return look.STACK_FAR, look.CHIP_HOVER, (look.SURFACE[0], look.BG[1])


def fits(preset: Preset, kind: Kind) -> bool:
    """A preset that can name `kind`: made for it, or for both (Auto)."""
    if preset.type is TypeHint.AUTO:
        return True
    return preset.type is (TypeHint.TV if kind == "series" else TypeHint.MOVIE)


@dataclass
class Choice:
    """What names one band: the preset chosen for it, its options for this item and the Custom
    rule (None: the preset's own). `chosen`: the owner (or a request) chose it, so the band's
    other files follow it."""

    preset: Preset | None = None
    options: PresetOptions = field(default_factory=PresetOptions)
    custom: str | None = None
    chosen: bool = False

    def fits(self, kind: Kind) -> bool:
        """Whether it can name `kind`: a Custom rule names anything."""
        return self.custom is not None or (self.preset is not None and fits(self.preset, kind))

    @property
    def label(self) -> str:
        """Its name as the chip and the Activity panel say it (translated)."""
        if self.custom is not None:
            return "✎ " + t("Custom")
        return self.preset.name if self.preset is not None else "…"

    @property
    def rule(self) -> str:
        if self.custom is not None:
            own = self.custom
        else:
            own = self.preset.format if self.preset is not None else "{ny}"
        return with_options(own, self.options)

    @property
    def changed(self) -> bool:
        """The options or the rule differ from the stored preset."""
        if self.preset is None:
            return False
        return self.options != self.preset.options or self.rule != self.preset.rule


def _merge(a: RunResult, b: RunResult) -> RunResult:
    """One summary for a Forge that renamed downloads and dropped files."""
    return RunResult(
        run_id=b.run_id or a.run_id,
        renamed=a.renamed + b.renamed,
        skipped=a.skipped + b.skipped,
        failed=a.failed + b.failed,
        posters=a.posters + b.posters,
        backdrops=a.backdrops + b.backdrops,
        nfos=a.nfos + b.nfos,
        trailers=a.trailers + b.trailers,
        subtitles_fetched=a.subtitles_fetched + b.subtitles_fetched,
        cleaned=a.cleaned + b.cleaned,
        pruned=a.pruned + b.pruned,
        cancelled=a.cancelled or b.cancelled,
        errors=[*a.errors, *b.errors],
    )


type BandId = tuple[Kind, str, int | None]  # a show or film: kind, title (casefolded), year


def band_identity(item: QueueItem) -> BandId:
    """The band a file belongs to whatever its status: its show or film (as queue_rows groups)."""
    return item_kind(item), item.media.title.casefold(), item.media.year


def band_name(items: Sequence[QueueItem]) -> str:
    """The "For:" line's name: a show with its episodes, a film with its year."""
    media = items[0].media
    if item_kind(items[0]) == "series":
        return f"{media.title} · " + tn("{n} episode", "{n} episodes", len(items))
    return f"{media.title} ({media.year})" if media.year else media.title


def _set_text(widget: Any, text: str) -> None:
    """A CTk widget's text, configured only when it changed: CTk redraws on every configure."""
    if widget.cget("text") != text:
        widget.configure(text=text)


def summary_text(result: RunResult, moved: int = 0) -> str:
    """The Activity panel's line after a Forge (translated). `moved`: how many of the renamed
    files went to a library folder."""
    if moved:
        text = t(
            "Forged: {moved} moved, {renamed} renamed, {skipped} already right, {failed} failed",
            moved=moved,
            renamed=max(result.renamed - moved, 0),
            skipped=result.skipped,
            failed=result.failed,
        )
    else:
        text = t(
            "Forged: {renamed} renamed, {skipped} already right, {failed} failed",
            renamed=result.renamed,
            skipped=result.skipped,
            failed=result.failed,
        )
    extras: list[str] = []
    if result.posters:
        extras.append(tn("{n} poster", "{n} posters", result.posters))
    if result.backdrops:
        extras.append(tn("{n} background", "{n} backgrounds", result.backdrops))
    if result.nfos:
        extras.append(tn("{n} NFO file", "{n} NFO files", result.nfos))
    if result.trailers:
        extras.append(tn("{n} trailer", "{n} trailers", result.trailers))
    if result.subtitles_fetched:
        extras.append(tn("{n} subtitle", "{n} subtitles", result.subtitles_fetched))
    if extras:
        text += " · " + ", ".join(extras)
    if result.cancelled:
        text += " · " + t("cancelled")
    return text


def forge_label(pending: int) -> str:
    """The Forge button's text."""
    return "⚒ " + (tn("Forge {n} file", "Forge {n} files", pending) if pending else t("Forge"))


IGNORE_FOLDER = N_("Ignore this whole folder")


def _same(a: Path, b: Path) -> bool:
    return str(a).casefold() == str(b).casefold()


def display_name(item: QueueItem) -> str:
    """How the progress bar names a file: the episode's or the film's title."""
    media = item.media
    return media.episode_title or media.title or item.path.name
