"""Gather right-click requests into one confirm window per preset (ported from MediaForge's
`ConfirmBatcher` in platform/single_instance.py).

Explorer starts AirGrabber once per selected file, so 200 selected files arrive as 200 requests
within a few seconds (app/single_instance.py's mailbox, docs/forge/INTEGRATION.md 3.11). They are
collected until none has come for QUIET_S, then handed over as one batch per preset. Everything
runs on the one asyncio loop: no threads or locks. A path that arrives twice for the same preset
(a sender that couldn't delete its own request starts AirGrabber anyway) is kept once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger(__name__)

QUIET_S = 0.4  # no new request for this long: the batch is complete

type BatchHandler = Callable[[str | None, list[Path]], None]
"""Called with a preset name (None: the default one) and its files and folders."""


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


class ConfirmBatcher:
    """Collect right-click paths until no more arrive for `quiet_s`, then hand them over: one
    `on_batch(preset, paths)` call per preset, in the order the presets first came.

    `add` arms a timer on the running loop (when there is one) that flushes the batch once it's
    quiet; `flush_due(now)` does the same by hand (tests, or a host that polls). `clock` is
    injectable for tests."""

    def __init__(
        self,
        on_batch: BatchHandler,
        quiet_s: float = QUIET_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_batch = on_batch
        self._quiet = quiet_s
        self._clock = clock
        self._pending: dict[str | None, list[Path]] = {}
        self._seen: dict[str | None, set[str]] = {}
        self._last = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    @property
    def pending(self) -> int:
        """How many paths are waiting."""
        return sum(len(paths) for paths in self._pending.values())

    def add(self, paths: Iterable[str | Path], preset: str | None, now: float | None = None) -> None:
        """Add one request's paths for `preset` and restart the quiet time."""
        if self._closed:
            return
        batch = self._pending.setdefault(preset, [])
        seen = self._seen.setdefault(preset, set())
        for raw in paths:
            path = Path(raw)
            key = _key(path)
            if key not in seen:
                seen.add(key)
                batch.append(path)
        if not batch:  # nothing new (or an empty request): no window for it
            del self._pending[preset]
            del self._seen[preset]
        self._last = self._clock() if now is None else now
        self._arm()

    def _arm(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a plain test): flush_due by hand
        if self._timer is not None:
            self._timer.cancel()
        self._timer = loop.call_later(self._quiet, self._fire)

    def _fire(self) -> None:
        # Every add re-arms the timer, so when it fires the quiet time has passed.
        self._timer = None
        self._flush()

    def flush_due(self, now: float | None = None) -> None:
        """Hand over the waiting batches if nothing has come for the quiet time."""
        current = self._clock() if now is None else now
        if self._pending and current - self._last >= self._quiet:
            self._flush()

    def _flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        batches, self._pending, self._seen = self._pending, {}, {}
        for preset, paths in batches.items():
            log.info("Right-click batch: %d path(s)", len(paths))
            try:
                self._on_batch(preset, paths)
            except Exception:
                log.exception("Handing over a right-click batch failed")

    def close(self) -> None:
        """Drop what is waiting and take no more (shutting down)."""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()
        self._seen.clear()
