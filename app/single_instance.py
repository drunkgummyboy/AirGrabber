"""One AirGrabber at a time (Windows), and the mailbox later starts hand Forge requests over through.

The running copy holds the named mutex `Local\\AirGrabber`. A second start finds it there, sets the
named auto-reset event `Local\\AirGrabber-show` and exits; the running copy polls that event
(`show_requested`, non-blocking) and brings its window back.

A second start that carries a Forge request (`main.py --forge [--confirm] [--preset NAME] PATH…`,
the Explorer right-click menu starts one process per selected file) hands it over instead: it
writes the request as a small JSON file into `<data folder>\\handover\\` (as `.part`, then renamed
into place), sets the second auto-reset event `Local\\AirGrabber-forge` (`send_request`) and exits.
The running copy polls that event (`forge_requested`) and, every few seconds, the folder too (an
elevated sender can't open the event; its file still arrives), then reads and deletes the files
oldest first (`take_requests`); requests older than two minutes are dropped. `hand_over` is the
sender's whole part, including the running copy that is shutting down (lock held, nobody polling).

Stdlib only: main.py uses this before logging and the package check. Elsewhere than Windows the
lock and the events are a no-op and `acquire` always succeeds; the mailbox folder works anywhere.
"""

from __future__ import annotations

import ctypes
import functools
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\AirGrabber"
SHOW_EVENT_NAME = "Local\\AirGrabber-show"
FORGE_EVENT_NAME = "Local\\AirGrabber-forge"

HANDOVER_FOLDER = "handover"  # inside the data folder
REQUEST_MAX_AGE_S = 120.0  # older requests are dropped: nothing pops up long after the right-click
HANDOVER_WAIT_S = 3.0  # how long a sender waits for the running copy to take its request
HANDOVER_POLL_S = 0.05
_REQUEST_SUFFIX = ".json"
_PART_SUFFIX = ".part"
_FORMAT_VERSION = 1

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_ASFW_ANY = 0xFFFFFFFF  # AllowSetForegroundWindow: any process
_EVENT_MODIFY_STATE = 0x0002


@dataclass(frozen=True, slots=True)
class ForgeRequest:
    """What a start with `--forge` asks of AirGrabber's Forge (the right-click menu, the
    "MediaForge" shortcut): the files and folders, the preset to rename them with, whether to
    show the confirm window ("Rename N files?") instead of the Forge window."""

    paths: tuple[str, ...] = ()
    preset: str | None = None
    confirm: bool = False
    forge: bool = True  # `--forge` was given; the file carries it so other hand-overs can follow


class InstanceLock:
    """The single-instance lock of one process. Names are injectable for tests; `data_dir` is the
    data folder whose mailbox `take_requests` reads."""

    def __init__(
        self,
        mutex_name: str = MUTEX_NAME,
        show_event_name: str = SHOW_EVENT_NAME,
        forge_event_name: str = FORGE_EVENT_NAME,
        *,
        data_dir: Path | None = None,
    ) -> None:
        self._mutex_name = mutex_name
        self._event_name = show_event_name
        self._forge_event_name = forge_event_name
        self._data_dir = data_dir
        self._mutex: int | None = None
        self._event: int | None = None
        self._forge_event: int | None = None

    @property
    def forge_event_name(self) -> str:
        return self._forge_event_name

    def acquire(self) -> bool:
        """Claim the lock. False: another AirGrabber holds it (call `signal_show`, then exit).

        True outside Windows, and also when Windows refuses to create the objects: two copies
        running beats none starting.
        """
        if sys.platform != "win32":
            return True
        kernel32 = _kernel32()
        # The events first, so a second start that finds the mutex taken can always reach them.
        self._event = kernel32.CreateEventW(None, False, False, self._event_name) or None
        self._forge_event = kernel32.CreateEventW(None, False, False, self._forge_event_name) or None
        ctypes.set_last_error(0)
        mutex = kernel32.CreateMutexW(None, False, self._mutex_name)
        error = ctypes.get_last_error()
        if not mutex:
            log.warning("Could not create the single-instance lock (error %d); starting anyway", error)
            return True
        if error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(mutex)  # only the running copy keeps the mutex alive
            return False
        self._mutex = mutex
        return True

    def signal_show(self) -> bool:
        """Ask the running copy to show its window (after `acquire` returned False)."""
        if sys.platform != "win32" or self._event is None:
            return False
        # This process was just started by the user, so it may bring another one to the front.
        _user32().AllowSetForegroundWindow(_ASFW_ANY)
        return bool(_kernel32().SetEvent(self._event))

    def show_requested(self) -> bool:
        """Whether a second start asked for the window since the last call. Never blocks."""
        return self._signalled(self._event)

    def forge_requested(self) -> bool:
        """Whether a second start handed a Forge request over since the last call (then call
        `take_requests`). Never blocks. The folder may also hold requests without this."""
        return self._signalled(self._forge_event)

    def take_requests(self, *, now: float | None = None) -> list[ForgeRequest]:
        """The waiting Forge requests, oldest first; their files are deleted, stale ones dropped.
        Nothing without a data folder."""
        if self._data_dir is None:
            return []
        return take_requests(self._data_dir, now=now)

    def release(self) -> None:
        """Let a new copy start (at exit, and before a restart). Safe to call more than once."""
        if sys.platform != "win32":
            return
        mutex, self._mutex = self._mutex, None
        event, self._event = self._event, None
        forge_event, self._forge_event = self._forge_event, None
        for handle in (mutex, event, forge_event):  # the mutex first: a start in between then just runs
            if handle is not None:
                _kernel32().CloseHandle(handle)

    def _signalled(self, event: int | None) -> bool:
        if sys.platform != "win32" or event is None:
            return False
        return bool(_kernel32().WaitForSingleObject(event, 0) == _WAIT_OBJECT_0)


def handover_folder(data_dir: Path) -> Path:
    return data_dir / HANDOVER_FOLDER


def send_request(
    data_dir: Path,
    request: ForgeRequest,
    *,
    event_name: str | None = FORGE_EVENT_NAME,
    now: float | None = None,
) -> Path:
    """Put a request in the mailbox for the running copy and wake it (`event_name`; None: only
    the file). Returns the request's file: it is gone once the running copy took it. Raises
    OSError when the file can't be written."""
    created = time.time() if now is None else now
    folder = handover_folder(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    # The name sorts oldest first; pid and a random part keep simultaneous starts apart.
    stem = f"{max(0, int(created * 1_000_000_000)):020d}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    target = folder / f"{stem}{_REQUEST_SUFFIX}"
    part = folder / f"{stem}{_REQUEST_SUFFIX}{_PART_SUFFIX}"
    payload = {
        "version": _FORMAT_VERSION,
        "created": created,
        "paths": list(request.paths),
        "preset": request.preset,
        "confirm": request.confirm,
        "forge": request.forge,
    }
    try:
        with part.open("x", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False)
        os.replace(part, target)  # the reader only ever sees whole files
    except OSError:
        part.unlink(missing_ok=True)
        raise
    if event_name is not None:
        _set_named_event(event_name)
    return target


def take_requests(
    data_dir: Path, *, max_age_s: float = REQUEST_MAX_AGE_S, now: float | None = None
) -> list[ForgeRequest]:
    """Read and delete the waiting requests, oldest first. Requests older than `max_age_s` (or
    that far in the future: a clock change) and unreadable files are dropped; leftover `.part`
    files of crashed senders go once they are that old. A file that can't be deleted yet is left
    for the next call, so no request is ever taken twice. Never raises."""
    current = time.time() if now is None else now
    folder = handover_folder(data_dir)
    try:
        names = sorted(entry.name for entry in os.scandir(folder) if entry.is_file())
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("Could not read the hand-over folder (%s)", type(exc).__name__)
        return []
    requests: list[ForgeRequest] = []
    for name in names:
        path = folder / name
        if name.endswith(_PART_SUFFIX):
            _drop_stale_part(path, current, max_age_s)
            continue
        if not name.endswith(_REQUEST_SUFFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            path.unlink()
        except OSError:
            continue  # gone already, or still locked (a virus scanner): the next call
        decoded = _decode(text)
        if decoded is None:
            log.warning("Dropped an unreadable hand-over request")
            continue
        request, created = decoded
        age = current - created
        if not -max_age_s <= age <= max_age_s:
            log.info("Dropped a hand-over request from %.0f s ago", age)
            continue
        requests.append(request)
    return requests


def hand_over(
    data_dir: Path,
    request: ForgeRequest,
    *,
    lock_factory: Callable[[], InstanceLock] | None = None,
    wait_s: float = HANDOVER_WAIT_S,
    poll_s: float = HANDOVER_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> InstanceLock | None:
    """A start with a Forge request: hand it to the running AirGrabber, or become AirGrabber.

    Returns the acquired lock when this process is to start AirGrabber itself, with `request`
    still its own (no AirGrabber ran, or the running one let go of the lock without taking the
    request; it is no longer in the folder, except in the rare case that its file couldn't be
    deleted); it then takes every other waiting request. None: this process is done and exits;
    the running copy took the request, or it still holds the lock but didn't take it within
    `wait_s` (the request then waits in the folder for it, until it is two minutes old).
    Raises OSError when the request can't be written.
    """
    new_lock = lock_factory or (lambda: InstanceLock(data_dir=data_dir))
    lock = new_lock()
    if lock.acquire():
        return lock
    try:
        file = send_request(data_dir, request, event_name=lock.forge_event_name)
    finally:
        lock.release()  # this process holds no handles while it waits
    deadline = clock() + wait_s
    while True:
        sleep(poll_s)
        if not file.exists():
            return None  # taken
        lock = new_lock()
        if lock.acquire():
            # The running copy is gone and never took it: this start becomes AirGrabber.
            try:
                file.unlink()
            except FileNotFoundError:
                lock.release()  # it was taken just before that copy let go
                return None
            except OSError:
                # Can't take it back (a virus scanner has it open): start anyway; it may then
                # come twice, from the folder too, which beats never.
                log.debug("Could not take a hand-over request back")
            return lock
        lock.release()
        if clock() >= deadline:
            return None


def _decode(text: str) -> tuple[ForgeRequest, float] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    paths = data.get("paths")
    preset = data.get("preset")
    created = data.get("created")
    confirm = data.get("confirm", False)
    forge = data.get("forge", True)
    if (
        not isinstance(paths, list)
        or not all(isinstance(path, str) for path in paths)
        or not (preset is None or isinstance(preset, str))
        or isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not isinstance(confirm, bool)
        or not isinstance(forge, bool)
    ):
        return None
    request = ForgeRequest(paths=tuple(paths), preset=preset, confirm=confirm, forge=forge)
    return request, float(created)


def _drop_stale_part(path: Path, now: float, max_age_s: float) -> None:
    try:
        if now - path.stat().st_mtime > max_age_s:
            path.unlink()
    except OSError:
        pass


def _set_named_event(name: str) -> bool:
    """Set the running copy's event, if it has one open. Never raises."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = _kernel32()
        event = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not event:
            return False  # nobody listens (or an elevated copy): the folder is polled too
        try:
            # This process was just started by the user, so it may bring another one to the front.
            _user32().AllowSetForegroundWindow(_ASFW_ANY)
            return bool(kernel32.SetEvent(event))
        finally:
            kernel32.CloseHandle(event)
    except OSError:
        return False


@functools.cache
def _kernel32() -> Any:
    """kernel32 with typed prototypes (64-bit handles), separate from ctypes.windll's copy."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = (
        wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
    )  # fmt: skip
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


@functools.cache
def _user32() -> Any:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    return user32
