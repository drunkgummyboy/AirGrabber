"""Reusable annotated field types and text helpers shared by the models."""

from __future__ import annotations

import base64
import html
import re
from datetime import UTC, date, datetime, time
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator

_TAG_RE = re.compile(r"<[^>]+>")
_PARAGRAPH_BREAK_RE = re.compile(r"</p>\s*<p[^>]*>", re.IGNORECASE)
_HEX_HASH_RE = re.compile(r"[0-9a-fA-F]{40}")
_BASE32_HASH_RE = re.compile(r"[A-Za-z2-7]{32}")


def strip_html(value: str | None) -> str:
    """API summaries are HTML fragments; reduce them to plain text."""
    if not value:
        return ""
    text = _PARAGRAPH_BREAK_RE.sub("\n\n", value)
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _empty_to_none(value: Any) -> Any:
    return None if value == "" else value


def _normalize_imdb_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.isdigit():
        return f"tt{int(text):07d}"
    return text


def _to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _coerce_count(value: Any) -> int:
    """Indexers report counts as ints, '1,234', '' or None — and sometimes -1 for 'unknown'."""
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        value = value.replace(",", "").strip() or 0
    return max(0, int(float(value)))


def _normalize_info_hash(value: Any) -> str | None:
    """Canonical form is 40 lowercase hex chars; base32 hashes (from some magnets) are converted."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("info hash must be a string")
    text = value.strip()
    if _HEX_HASH_RE.fullmatch(text):
        return text.lower()
    if _BASE32_HASH_RE.fullmatch(text):
        return base64.b32decode(text.upper()).hex()
    raise ValueError(f"not a BitTorrent v1 info hash: {text!r}")


OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]
OptionalTime = Annotated[time | None, BeforeValidator(_empty_to_none)]
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]
ImdbId = Annotated[str | None, BeforeValidator(_normalize_imdb_id)]
Count = Annotated[int, BeforeValidator(_coerce_count)]
InfoHash = Annotated[str | None, BeforeValidator(_normalize_info_hash)]
