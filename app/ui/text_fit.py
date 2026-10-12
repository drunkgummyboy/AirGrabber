"""Fitting text into a width: eliding, word wrap within a line limit, title breaks.

Widths are CTk's unscaled units, the units `CTkFont.measure()` reports in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

Measure = Callable[[str], float]

ELLIPSIS = "…"
# Headroom in unscaled px: widgets render CTkFont sizes rounded to whole scaled pixels, so text
# can come out slightly wider than CTkFont.measure() predicts.
SLACK = 2
# Where a title splits into a main title and a subtitle.
_TITLE_SEPARATORS = (": ", " - ")


def elide_to_width(text: str, max_width: float, measure: Measure) -> str:
    """`text` if it fits in `max_width`, else its longest prefix that fits followed by "…".

    Returns "…" when not even one character fits beside it, "" when "…" alone doesn't fit.
    """
    if measure(text) <= max_width:
        return text
    if measure(ELLIPSIS) > max_width:
        return ""
    low, high = 0, len(text) - 1  # prefix lengths; the whole text is known not to fit
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid].rstrip() + ELLIPSIS) <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + ELLIPSIS


def _first_line(text: str, max_width: float, measure: Measure) -> tuple[str, str]:
    """Greedy word wrap: the first line of `text` and the rest.

    Breaks after the last word that fits; a first word wider than a line is broken inside.
    """
    if measure(text) <= max_width:
        return text, ""
    cut = 0
    for index, char in enumerate(text):
        if char == " ":
            if measure(text[:index]) > max_width:
                break
            cut = index
    if cut == 0:
        low, high = 1, len(text)  # widest prefix that fits, but at least one character
        while low < high:
            mid = (low + high + 1) // 2
            if measure(text[:mid]) <= max_width:
                low = mid
            else:
                high = mid - 1
        cut = low
    return text[:cut].rstrip(), text[cut:].lstrip()


def fit_to_lines(text: str, max_width: float, max_lines: int, measure: Measure) -> str:
    """`text` unchanged if word wrap fits it in `max_lines` lines of `max_width`.

    Line breaks already in `text` are kept: each of its lines wraps on its own, and all of them
    together get `max_lines`. When more are needed, the result is the first `max_lines - 1`
    wrapped lines, newline-separated, and a last line holding the rest (its source lines joined
    by spaces) elided to `max_width`.
    """
    sources = [line.strip() for line in text.strip().split("\n")]
    lines: list[str] = []
    for index, source in enumerate(sources):
        rest = source
        while True:
            if len(lines) == max_lines - 1:
                # One line left: it has to hold everything that remains.
                remaining = [rest, *sources[index + 1 :]]
                if len(remaining) == 1 and not _first_line(rest, max_width, measure)[1]:
                    return text
                tail = " ".join(part for part in remaining if part)
                return "\n".join([*lines, elide_to_width(tail, max_width, measure)])
            line, rest = _first_line(rest, max_width, measure)
            lines.append(line)
            if not rest:
                break
    return text


def title_lines(title: str) -> str:
    """`title` with its first ": " or " - " separator turned into a line break, so a long title
    wraps between title and subtitle: "Mission: Impossible – Dead Reckoning" becomes
    "Mission\\nImpossible – Dead Reckoning". Unchanged without a separator, or when one would
    leave either side empty."""
    found = [(index, sep) for sep in _TITLE_SEPARATORS if (index := title.find(sep)) != -1]
    if not found:
        return title
    index, sep = min(found)
    head, tail = title[:index].strip(), title[index + len(sep) :].strip()
    if not head or not tail:
        return title
    return f"{head}\n{tail}"


def unscaled_width(widget: Any, physical_px: int) -> float:
    """A width from a Tk event (physical pixels) in CTk's unscaled units."""
    scaling: float = ctk.ScalingTracker.get_widget_scaling(widget)
    return physical_px / scaling
