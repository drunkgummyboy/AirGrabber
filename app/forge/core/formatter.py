"""Turn a format string like "{ny}/{ny} - {s00e00}" into safe Windows path segments."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PureWindowsPath

from pydantic import BaseModel

from app.forge.models.media import ResolvedMedia

KNOWN_VARIABLES = ("ny", "t", "y", "s", "e", "s00e00", "sxe", "en")

_FORBIDDEN = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
_SPACES = re.compile(r"\s+")
_VARIABLE = re.compile(r"\{([A-Za-z0-9]+)\}")
_SEPARATOR = re.compile(r"[\\/]")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}


class Rendered(BaseModel, frozen=True):
    parts: tuple[str, ...]
    unknown: tuple[str, ...] = ()

    @property
    def relative(self) -> PureWindowsPath:
        return PureWindowsPath(*self.parts)


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch not in _FORBIDDEN)
    return _SPACES.sub(" ", text).strip()


def sanitize_segment(seg: str) -> str:
    """Make one folder or file name safe for Windows."""
    seg = _clean_text(seg).strip(" .")
    if seg.split(".", 1)[0].upper() in _RESERVED:
        seg += "_"
    return seg or "_"


def _values(media: ResolvedMedia) -> dict[str, str]:
    s = f"{media.season:02d}" if media.season is not None else "00"
    e = f"{media.episode:02d}" if media.episode is not None else "00"
    if media.episode_end is not None:
        e += f"-E{media.episode_end:02d}"
    sxe = f"{media.season or 0}x{media.episode or 0:02d}"  # 1x02, 1x02-03
    if media.episode_end is not None:
        sxe += f"-{media.episode_end:02d}"
    raw = {
        "ny": media.ny,
        "t": media.title,
        "y": str(media.year) if media.year else "",
        "s": s,
        "e": e,
        "s00e00": f"S{s}E{e}",
        "sxe": sxe,
        "en": media.episode_title or f"Episode {e}",
    }
    # Values never contain path separators, so a "/" in an episode name can't make a folder.
    return {k: _clean_text(v) for k, v in raw.items()}


def render(fmt: str, media: ResolvedMedia, extension: str) -> Rendered:
    values = _values(media)
    unknown: list[str] = []

    def substitute(m: re.Match[str]) -> str:
        name = m.group(1).lower()
        if name in values:
            return values[name]
        unknown.append(m.group(1))
        return m.group(0)

    # Split on the format's own separators first, then substitute inside each segment.
    segments = [seg for seg in _SEPARATOR.split(fmt) if seg.strip()]
    parts = [sanitize_segment(_VARIABLE.sub(substitute, seg)) for seg in segments] or ["_"]
    parts[-1] += extension.lower()
    return Rendered(parts=tuple(parts), unknown=tuple(unknown))
