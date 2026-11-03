"""A real check of a fetched .torrent file before it is saved, opened or sent anywhere.

Download Station (and other programs) reject a damaged file as "invalid torrent file", so a
file is accepted only when it bdecodes completely and strictly, is a BitTorrent v1 (or hybrid)
torrent, and — when the release's info hash is known — is the torrent for that hash: SHA-1 of
the `info` dictionary's exact bytes in the file (never re-encoded).

Pure stdlib, no I/O. The reasons returned are short English texts for the log; they never hold
the hash, a URL or the file's content.
"""

from __future__ import annotations

import hashlib
from typing import NoReturn

MAX_DEPTH = 64  # nesting of lists / dictionaries; a hostile file can't recurse deeper
_MAX_DIGITS = 20  # an integer or string length longer than this is refused unparsed

BValue = int | bytes | list["BValue"] | dict[bytes, "BValue"]


class _Invalid(Exception):
    """Bencode that doesn't decode; the message is the reason."""


def torrent_problem(data: bytes, info_hash: str | None = None) -> str | None:
    """None when `data` is a usable .torrent (for `info_hash`, when given), else the reason."""
    try:
        top, info_span = _decode_top(data)
    except _Invalid as exc:
        return str(exc)
    info = top.get(b"info")
    if not isinstance(info, dict):
        return "no info dictionary"
    problem = _info_problem(info)
    if problem is not None:
        return problem
    if info_hash:
        start, end = info_span
        if hashlib.sha1(data[start:end]).hexdigest() != info_hash.strip().lower():
            return "info hash differs"
    return None


def _info_problem(info: dict[bytes, BValue]) -> str | None:
    if not isinstance(info.get(b"name"), bytes):
        return "no name"
    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or piece_length <= 0:
        return "no valid piece length"
    pieces = info.get(b"pieces")
    if pieces is None:
        if b"file tree" in info or info.get(b"meta version") == 2:
            return "a v2-only torrent"
        return "no pieces"
    if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
        return "pieces is not a multiple of 20 bytes"
    has_length, has_files = b"length" in info, b"files" in info
    if has_length == has_files:
        return "both length and files" if has_length else "neither length nor files"
    if has_length:
        length = info[b"length"]
        if not isinstance(length, int) or length < 0:
            return "no valid length"
        return None
    files = info[b"files"]
    if not isinstance(files, list) or not files:
        return "no files"
    for entry in files:
        if not isinstance(entry, dict):
            return "a file entry is not a dictionary"
        size = entry.get(b"length")
        if not isinstance(size, int) or size < 0:
            return "a file has no valid length"
        path = entry.get(b"path")
        if not isinstance(path, list) or not path:
            return "a file has no path"
        if not all(isinstance(part, bytes) for part in path):
            return "a file path is not text"
    return None


def _decode_top(data: bytes) -> tuple[dict[bytes, BValue], tuple[int, int]]:
    """The top-level dictionary and the (start, end) of its `info` value's bytes."""
    if not data.startswith(b"d"):
        return _fail("not a bencoded dictionary")
    result: dict[bytes, BValue] = {}
    info_span = (0, 0)
    pos = 1
    while True:
        if pos >= len(data):
            return _fail("truncated")
        if data[pos] == 0x65:  # 'e'
            pos += 1
            break
        key, pos = _decode_string(data, pos)
        if key in result:
            return _fail("a duplicate key")
        start = pos
        value, pos = _decode(data, pos, 1)
        if key == b"info":
            info_span = (start, pos)
        result[key] = value
    if pos != len(data):
        return _fail("trailing bytes")
    return result, info_span


def _decode(data: bytes, pos: int, depth: int) -> tuple[BValue, int]:
    if pos >= len(data):
        return _fail("truncated")
    lead = data[pos]
    if 0x30 <= lead <= 0x39:  # a digit: a byte string
        return _decode_string(data, pos)
    if lead == 0x69:  # 'i'
        end = data.find(b"e", pos + 1, pos + 2 + _MAX_DIGITS)
        if end < 0:
            return _fail("truncated" if len(data) <= pos + 1 + _MAX_DIGITS else "a bad integer")
        return _parse_int(data[pos + 1 : end]), end + 1
    if lead not in (0x6C, 0x64):  # 'l', 'd'
        return _fail("not valid bencode")
    if depth >= MAX_DEPTH:
        return _fail("nested too deep")
    pos += 1
    if lead == 0x6C:
        items: list[BValue] = []
        while True:
            if pos >= len(data):
                return _fail("truncated")
            if data[pos] == 0x65:
                return items, pos + 1
            item, pos = _decode(data, pos, depth + 1)
            items.append(item)
    entries: dict[bytes, BValue] = {}
    while True:
        if pos >= len(data):
            return _fail("truncated")
        if data[pos] == 0x65:
            return entries, pos + 1
        key, pos = _decode_string(data, pos)
        if key in entries:
            return _fail("a duplicate key")
        entries[key], pos = _decode(data, pos, depth + 1)


def _decode_string(data: bytes, pos: int) -> tuple[bytes, int]:
    if pos >= len(data):
        return _fail("truncated")
    if not 0x30 <= data[pos] <= 0x39:
        return _fail("a key is not a byte string")
    colon = data.find(b":", pos, pos + 1 + _MAX_DIGITS)
    if colon < 0:
        return _fail("truncated" if len(data) <= pos + _MAX_DIGITS else "a bad string length")
    digits = data[pos:colon]
    if not digits.isdigit() or (len(digits) > 1 and digits.startswith(b"0")):
        return _fail("a bad string length")
    end = colon + 1 + int(digits)
    if end > len(data):
        return _fail("truncated")
    return data[colon + 1 : end], end


def _parse_int(text: bytes) -> int:
    digits = text[1:] if text.startswith(b"-") else text
    if not digits.isdigit():
        return _fail("a bad integer")
    if digits.startswith(b"0") and (len(digits) > 1 or text.startswith(b"-")):
        return _fail("an integer with a leading zero")
    return int(text)


def _fail(reason: str) -> NoReturn:
    raise _Invalid(reason)
