"""The automatic downloads' pause (`Settings.auto_paused_until`): how long the tray menu's
choices last (app/ui/tray_menu.py, app/ui/tray.py), and the longest pause there is, which a
phone's pause is capped to (app/services/phone/settings.py). Pure: no I/O."""

from __future__ import annotations

from datetime import datetime, timedelta

PAUSE_HOURS = 3  # the tray menu's "For 3 hours"


def pause_until(now: datetime, hours: int | None) -> datetime:
    """When a pause ends: after `hours`, or (None) at the start of tomorrow, local time."""
    if hours is not None:
        return now + timedelta(hours=hours)
    local = now.astimezone()
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def longest_pause(now: datetime) -> datetime:
    """The latest a pause started at `now` may end: the later of the tray menu's two choices
    (just before midnight "Until tomorrow" is the sooner one)."""
    return max(pause_until(now, PAUSE_HOURS), pause_until(now, None))
