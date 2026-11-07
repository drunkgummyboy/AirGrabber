"""What is inside a .torrent, before anything downloads: a video's torrent that holds a program is
malware dressed up as an episode ("Show S01E01 1080p WEB.exe" with VLC's icon). The file only
lists names, so this reads them; nothing is ever run or opened. Stdlib only.

A file that doesn't read gives no names: it is the torrent program's to refuse, not a program."""

from __future__ import annotations

from typing import Any

# What Windows runs on a double-click. Not ".url" (a shortcut to a site, harmless clutter that many
# release groups add), nor archives: a .rar can't start anything by itself.
PROGRAM_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".com",
        ".cpl",
        ".exe",
        ".hta",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".msi",
        ".pif",
        ".ps1",
        ".reg",
        ".scr",
        ".vbe",
        ".vbs",
        ".wsf",
        ".wsh",
    }
)
_MAX_DEPTH = 64  # a real file's nesting is far below this; a hostile one stops here


def _decode(data: bytes, at: int, depth: int) -> tuple[Any, int]:
    """The value at `at` and where it ends. ValueError / IndexError when it doesn't read."""
    if depth > _MAX_DEPTH:
        raise ValueError("nested too deep")
    kind = data[at]
    if kind == ord("i"):
        end = data.index(b"e", at)
        return int(data[at + 1 : end]), end + 1
    if kind == ord("l"):
        at += 1
        items = []
        while data[at] != ord("e"):
            item, at = _decode(data, at, depth + 1)
            items.append(item)
        return items, at + 1
    if kind == ord("d"):
        at += 1
        found: dict[bytes, Any] = {}
        while data[at] != ord("e"):
            key, at = _decode(data, at, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError("a key that isn't text")
            found[key], at = _decode(data, at, depth + 1)
        return found, at + 1
    colon = data.index(b":", at)
    length = int(data[at:colon])
    if length < 0 or colon + 1 + length > len(data):
        raise ValueError("a text past the end")
    return data[colon + 1 : colon + 1 + length], colon + 1 + length


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


def _tree_names(tree: Any, depth: int = 0) -> list[str]:
    """A v2 torrent's "file tree": folders of folders, a file's name a key holding an empty key."""
    if not isinstance(tree, dict) or depth > _MAX_DEPTH:
        return []
    names: list[str] = []
    for key, value in tree.items():
        if key == b"":
            continue
        if isinstance(value, dict) and b"" in value:
            names.append(_text(key))
        names.extend(_tree_names(value, depth + 1))
    return names


def file_names(content: bytes) -> list[str]:
    """The names of the files a .torrent holds (without their folders); [] when it doesn't read."""
    try:
        decoded, _end = _decode(content, 0, 0)
    except (ValueError, IndexError, RecursionError):
        return []
    info = decoded.get(b"info") if isinstance(decoded, dict) else None
    if not isinstance(info, dict):
        return []
    names = _tree_names(info.get(b"file tree"))
    files = info.get(b"files")
    if isinstance(files, list):
        for item in files:
            path = item.get(b"path") if isinstance(item, dict) else None
            if isinstance(path, list) and path:
                names.append(_text(path[-1]))
    elif not names:
        names.append(_text(info.get(b"name")))
    return names


def _suffix(name: str) -> str:
    # Windows ignores trailing dots and spaces: "episode.exe. " still runs.
    stripped = name.rstrip(" .").lower()
    dot = stripped.rfind(".")
    return stripped[dot:] if dot >= 0 else ""


def program_inside(content: bytes) -> str | None:
    """The suffix of a program among a .torrent's files (".exe"), or None."""
    for name in file_names(content):
        suffix = _suffix(name)
        if suffix in PROGRAM_SUFFIXES:
            return suffix
    return None
