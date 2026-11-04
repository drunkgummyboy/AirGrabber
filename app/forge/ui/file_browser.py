"""Open in Explorer: a queue card's 📂.

One file: Explorer with that file selected. Several: the first one's folder (`explorer_target`).
`open_explorer` starts Explorer and never waits for it; it goes through `_start`, the one place
that starts a real process, which tests/forge/base.py refuses (tests use a fake at that seam).
The command is one string: subprocess's quoting of a list breaks `/select,"…"`. No shell. Logs
give counts only, never a path.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)


def explorer_target(paths: Sequence[Path]) -> tuple[Path, bool]:
    """What Explorer shows for a band's files (at least one): (where, select it). One file: itself,
    selected. Several, in one folder or not: the first one's folder."""
    return (paths[0], True) if len(paths) == 1 else (paths[0].parent, False)


def explorer_command(target: Path, *, select: bool) -> str:
    """The command line (Windows' file names can't hold a double quote)."""
    return f'explorer /select,"{target}"' if select else f'explorer "{target}"'


def _start(command: str) -> None:
    subprocess.Popen(command)  # noqa: S603


def open_explorer(target: Path, *, select: bool) -> None:
    """Start Explorer at `target` (`select`: with that file selected). Not there any more is the
    caller's check (through a thread: a NAS can hang)."""
    try:
        _start(explorer_command(target, select=select))
    except OSError as exc:
        log.warning("Could not start Explorer: %s", type(exc).__name__)
