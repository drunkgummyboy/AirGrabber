"""After MediaForge moves files into a library folder, Plex scans those folders (one of the
bridges between AirGrabber and Forge, through Forge's move records only, never forge.db): a short
wait gathers a run's files, then `PlexService.scan_folders` asks for each folder once.

What was scanned counts only once the scan went through: a scan that failed is asked again with
the next move (its moves are read again), and a move heard while a scan runs brings one more scan
right after it. A scan that went through has Plex checked AFTER_SCAN_CHECK_S later
(`PlexService.check_later`), so the Library page and the ✓ marks catch up with what Plex took in.
Logs give counts only, never a path."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.forge.models.moves import MoveRecord

log = logging.getLogger(__name__)

GATHER_S = 15.0  # after a move: the rest of its run comes in meanwhile
AFTER_SCAN_CHECK_S = 120.0  # Plex has taken the files in by then: one check


class MoveRecords(Protocol):
    """What this reads of Forge's move records (ForgeMoveService)."""

    async def moved_after(self, when: float) -> list[MoveRecord]: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class FolderScanner(Protocol):
    """PlexService: `scan_folders` raises when the scan didn't go through."""

    async def scan_folders(self, folders: Iterable[str]) -> int: ...

    def check_later(self, delay_s: float) -> None: ...


class PlexScansAfterMoves:
    """`attach(moves)` once Forge runs; from then on every move brings a scan of its folder."""

    def __init__(
        self,
        plex: FolderScanner,
        *,
        gather_s: float = GATHER_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._plex = plex
        self._gather = gather_s
        self._clock = clock
        self._moves: MoveRecords | None = None
        self._since = 0.0
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._scanning = False  # the moves are being read or scanned
        self._again = False  # a move came in meanwhile: one more scan after this one

    def attach(self, moves: MoveRecords) -> None:
        """Hear Forge's moves from now on (what was moved before is Plex's own business)."""
        if self._moves is not None:
            return
        self._moves = moves
        self._since = self._clock()
        self._unsubscribe = moves.subscribe(self._moved)

    def _moved(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._scan_soon(), name="plex-scan-after-moves"
            )
        elif self._scanning:
            self._again = True  # too late for this scan: its own after it

    async def _scan_soon(self) -> None:
        while True:
            self._again = False
            await asyncio.sleep(self._gather)
            self._scanning = True
            try:
                await self._scan()
            finally:
                self._scanning = False
            if not self._again:
                return

    async def _scan(self) -> None:
        moves = self._moves
        if moves is None:
            return
        try:
            records = await moves.moved_after(self._since)
            if not records:
                return
            asked = await self._plex.scan_folders(
                {str(record.target.parent) for record in records}
            )
        except Exception as exc:  # the type only: a message could hold a path
            log.warning("Asking Plex to scan the moved files failed (%s)", type(exc).__name__)
            return  # `_since` stays: the next move asks for these again
        self._since = max(record.moved_at.timestamp() for record in records)
        if asked:
            self._plex.check_later(AFTER_SCAN_CHECK_S)

    async def aclose(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
