"""The tray menu's own lines (pure: the tray icon's thread only reads what these make): today's
episodes, the downloads in progress, and the automatic downloads' pause."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.i18n import N_, t
from app.models import CalendarEntry, DownloadsOverview

TODAY_MAX = 3  # names in the line; the rest is "+2 more"
PAUSE_HOURS = 3

NOTHING_TODAY = N_("Nothing airs today")
PAUSE_MENU = N_("Pause automatic downloads")
PAUSE_FOR = N_("For {n} hours")
PAUSE_UNTIL_TOMORROW = N_("Until tomorrow")
RESUME = N_("Resume automatic downloads (paused until {time})")


def today_line(entries: Sequence[CalendarEntry]) -> str:
    """"Today: Reacher S04E08, The Ark S03E05 +2 more", or "Nothing airs today"."""
    if not entries:
        return t(NOTHING_TODAY)
    names = [f"{e.show.name} {e.episode.code}" for e in entries[:TODAY_MAX]]
    more = len(entries) - TODAY_MAX
    text = ", ".join(names) + (" " + t("+{n} more", n=more) if more > 0 else "")
    return t("Today: {episodes}", episodes=text)


def downloads_line(overview: DownloadsOverview | None) -> str:
    """"Downloading 2 · 45%" (their average progress, when the program tells it); "" when
    nothing is on its way."""
    if overview is None:
        return ""
    busy = [e for e in overview.entries if e.state.in_progress]
    if not busy:
        return ""
    known = [e.progress for e in busy if e.progress is not None]
    if known:
        share = round(100 * sum(known) / len(known))
        return t("Downloading {n} · {percent}%", n=len(busy), percent=share)
    return t("Downloading {n}", n=len(busy))


def pause_until(now: datetime, hours: int | None) -> datetime:
    """When a pause ends: after `hours`, or (None) at the start of tomorrow, local time."""
    if hours is not None:
        return now + timedelta(hours=hours)
    local = now.astimezone()
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def resume_text(until: datetime) -> str:
    """"Resume automatic downloads (paused until 14:30)"."""
    return t(RESUME, time=until.astimezone().strftime("%H:%M"))
