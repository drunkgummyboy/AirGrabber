"""What a rename changes, word by word, for the queue's lines: the words of the current name that
the new one leaves out ("1080p", "WEB", "CAKES"), and the words the new name adds ("2021"), as
(start, end) spans. Words are letters and digits; case doesn't count."""

from __future__ import annotations

import re

Span = tuple[int, int]

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _words(text: str) -> list[tuple[str, int, int]]:
    return [(m.group().casefold(), m.start(), m.end()) for m in _WORD.finditer(text)]


def name_changes(old: str, new: str) -> tuple[tuple[Span, ...], tuple[Span, ...]]:
    """(the spans of `old`'s words that `new` doesn't have, the spans of `new`'s words that
    `old` doesn't have)."""
    old_words, new_words = _words(old), _words(new)
    old_set = {word for word, _s, _e in old_words}
    new_set = {word for word, _s, _e in new_words}
    dropped = tuple((s, e) for word, s, e in old_words if word not in new_set)
    added = tuple((s, e) for word, s, e in new_words if word not in old_set)
    return dropped, added


def runs(text: str, spans: tuple[Span, ...]) -> list[tuple[str, bool]]:
    """`text` cut into pieces, each with whether it lies in `spans` (in order, not overlapping).
    The separators between two marked words are marked too, so a dropped "1080p.WEB" reads as
    one stretch."""
    pieces: list[tuple[str, bool]] = []
    at = 0
    for start, end in spans:
        if start > at:
            between = text[at:start]
            joins = bool(pieces) and pieces[-1][1] and not between.strip(" ._-")
            pieces.append((between, joins))
        pieces.append((text[start:end], True))
        at = end
    if at < len(text):
        pieces.append((text[at:], False))
    merged: list[tuple[str, bool]] = []
    for piece, marked in pieces:
        if merged and merged[-1][1] == marked:
            merged[-1] = (merged[-1][0] + piece, marked)
        else:
            merged.append((piece, marked))
    return merged
