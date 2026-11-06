"""What an NFO file says: the small XML files Kodi and Jellyfin read beside a video. Pure text from
what the queue already knows of a file; nothing is fetched here."""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from app.forge.models.media import ResolvedMedia

_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_NOT_IN_XML = re.compile(r"[^\t\n\r\x20-퟿-�\U00010000-\U0010FFFF]")


def _line(tag: str, value: str | int | None) -> str:
    """One element, or nothing when there is nothing to say."""
    if value is None or value == "":
        return ""
    text = _NOT_IN_XML.sub("", str(value))
    return f"  <{tag}>{escape(text)}</{tag}>\n"


def _file(root: str, *lines: str) -> str:
    return f"{_HEADER}\n<{root}>\n{''.join(lines)}</{root}>\n"


def _tmdb_id(media: ResolvedMedia) -> str:
    if media.tmdb_id is None:
        return ""
    return f'  <uniqueid type="tmdb" default="true">{media.tmdb_id}</uniqueid>\n'


def _about(media: ResolvedMedia) -> tuple[str, ...]:
    return (
        _line("title", media.title),
        _line("originaltitle", media.original_title),
        _line("year", media.year),
        _line("plot", media.overview),
        _tmdb_id(media),
    )


def movie_nfo(media: ResolvedMedia) -> str:
    return _file("movie", *_about(media))


def show_nfo(media: ResolvedMedia) -> str:
    return _file("tvshow", *_about(media))


def episode_nfo(media: ResolvedMedia) -> str:
    return _file(
        "episodedetails",
        _line("title", media.episode_title),
        _line("showtitle", media.title),
        _line("season", media.season),
        _line("episode", media.episode),
        _line("plot", media.episode_overview),
    )
