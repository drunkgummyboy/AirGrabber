"""Removing the old MediaForge 1.x, and the guard that keeps the new renamer idle meanwhile
(docs/forge/INTEGRATION.md 3.10, D4).

The guard: while the old MediaForge is still registered under Installed apps it starts with
Windows and watches the same download folders, so the new renamer stays idle
(`AutoRenamer.blocked_by_old_install`, set by ForgeRuntime at start from
app/forge/migration.py's `blocked_by_old_install()`; the stored Off / Trial / On is kept). Once
the old install is gone, `OldInstallRemover` clears the flag and calls `reconfigure()`, so the
stored mode applies at once.

The remove action (`OldInstallRemover.remove`): "Remove the old MediaForge", offered once in the
import result (app/forge/ui/import_step.py, through `ForgeHost.on_remove_old`) and in
MediaForge Settings › Library & watching while the old install is registered
(`RemoveOldBox`; there only once its data was brought over or when it has none, so no data is
lost by removing it first). It reads the old entry's `UninstallString` and accepts it only when it is
Windows PowerShell running `-File "<InstallLocation>\\src\\mediaforge\\uninstall.ps1"` with
nothing after it (`uninstaller_command`); it then starts that script itself, from Windows
PowerShell's own path and without a console window, and waits for it. The old uninstaller asks
the owner itself ("Uninstall MediaForge?", "Also delete your settings and history?"). When it has
finished, the entry is looked for again and the outcome says whether it is gone.

Safe for AirGrabber: the old uninstaller (its `uninstall.ps1`) only removes right-click menu
keys whose command names its own `main.py` (`Remove-Menu`), so the menu AirGrabber took over
(F-26) stays; and it only closes Python processes started from its own `main.py`
(`Close-App`), never AirGrabber's.

One remover per ForgeRuntime (`remover_for`), shared by the main window and Settings, so a
removal started in Settings carries on when Settings closes. Finding the install
(`find`, app/forge/migration.py's `find_old_install` by default) and starting the uninstaller
(`launch`) are injectable; the real start goes only through `_run_uninstaller`, which tests
refuse. `startup_notice` gives the main window's one-time status-bar notice while an old
install's data waits to be brought over (a marker file beside forge.db remembers it was shown).

Never logs the uninstall string, paths or exception messages (they name the user's folders).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ntpath
import os
import subprocess
import weakref
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.i18n import N_, t

if TYPE_CHECKING:
    from app.forge.migration import OldInstall
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

UNINSTALLER = r"src\mediaforge\uninstall.ps1"  # under the old InstallLocation
NOTICE_MARKER = "old-mediaforge-noticed"  # beside forge.db: the one-time notice was shown
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_POWERSHELL = "powershell.exe"
_POWERSHELL_DIR = r"\windowspowershell\v1.0"  # where Windows PowerShell lives (any drive)
# The switches the registered command may carry before -File (MediaForge's install.ps1 writes
# -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden); the value each takes, if any.
_SWITCHES: dict[str, frozenset[str] | None] = {
    "-noprofile": None,
    "-nologo": None,
    "-executionpolicy": frozenset({"bypass", "remotesigned", "unrestricted"}),
    "-windowstyle": frozenset({"hidden", "minimized", "normal"}),
}

NOTICE_TEXT = N_("Your MediaForge is now part of AirGrabber: open it to bring your settings over.")
GUARD_TEXT = N_(
    "Renaming on its own waits while the old MediaForge is installed: both would move the same "
    "downloads."
)
REMOVE_OLD = N_("Remove the old MediaForge")
# Settings, while the old data waits to be brought over (no remove button until then).
BRING_FIRST_TEXT = N_("Bring your settings over first; then you can remove the old MediaForge.")
WAITING_TEXT = N_("Waiting for the old MediaForge's uninstaller…")


class Outcome(StrEnum):
    REMOVED = "removed"  # the entry is gone
    KEPT = "kept"  # the uninstaller ran, the entry is still there (the owner said No)
    REFUSED = "refused"  # the uninstall string isn't the old MediaForge's own uninstaller
    FAILED = "failed"  # the uninstaller couldn't be started


OUTCOME_TEXTS: dict[Outcome, str] = {
    Outcome.REMOVED: N_("The old MediaForge was removed."),
    Outcome.KEPT: N_("The old MediaForge is still installed."),
    Outcome.REFUSED: N_(
        "The old MediaForge's uninstaller wasn't recognised, so it wasn't started. You can "
        "remove MediaForge under Installed apps in Windows' Settings."
    ),
    Outcome.FAILED: N_(
        "The old MediaForge's uninstaller couldn't be started. You can remove MediaForge under "
        "Installed apps in Windows' Settings."
    ),
}


def outcome_text(outcome: Outcome) -> str:
    return t(OUTCOME_TEXTS[outcome])


# --- the uninstall string ----------------------------------------------------------------------


def split_command(command: str) -> list[str]:
    """A Windows command line split into its parts: spaces outside double quotes separate, the
    quotes go (enough for an uninstall string; no backslash escapes)."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    started = False
    for char in command:
        if char == '"':
            quoted = not quoted
            started = True
        elif char.isspace() and not quoted:
            if started:
                parts.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
    if quoted:
        raise ValueError("unbalanced quotes")
    if started:
        parts.append("".join(current))
    return parts


def _same_path(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def _is_windows_powershell(program: str) -> bool:
    """powershell.exe by name, or Windows PowerShell's own folder (never pwsh or another
    program called powershell somewhere else)."""
    folder, name = ntpath.split(program)
    if name.lower() not in (_POWERSHELL, "powershell"):
        return False
    return not folder or ntpath.normcase(ntpath.normpath(folder)).endswith(_POWERSHELL_DIR)


def uninstaller_script(old: OldInstall) -> str | None:
    """The old uninstaller's script when the registered `UninstallString` is Windows PowerShell
    running exactly `-File "<InstallLocation>\\src\\mediaforge\\uninstall.ps1"` (only the
    switches MediaForge's installer writes before it, nothing after it); else None."""
    location = old.install_location
    drive, _rest = ntpath.splitdrive(location)
    if not drive or not ntpath.isabs(location) or not old.uninstall_string:
        return None
    try:
        parts = split_command(old.uninstall_string)
    except ValueError:
        return None
    if len(parts) < 3 or not _is_windows_powershell(parts[0]):
        return None
    index = 1
    while index < len(parts) and parts[index].lower() != "-file":
        switch = parts[index].lower()
        if switch not in _SWITCHES:
            return None
        values = _SWITCHES[switch]
        if values is not None:
            index += 1
            if index >= len(parts) or parts[index].lower() not in values:
                return None
        index += 1
    if index != len(parts) - 2:  # "-File" then the script, and nothing after it
        return None
    script = parts[-1]
    if not _same_path(script, ntpath.join(location, UNINSTALLER)):
        return None
    return script


def uninstaller_command(script: str, powershell: str) -> list[str]:
    """What is started: the script through Windows PowerShell's own path (never the registered
    text itself), hidden, unsigned script allowed, the old uninstaller's dialogs as they are."""
    return [
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
        "-File", script,
    ]  # fmt: skip


def _powershell_exe() -> str:
    from app.services.powershell import powershell_exe

    return powershell_exe()


async def _run_uninstaller(command: Sequence[str]) -> int:
    """Start the uninstaller without a console window and wait for it (the real one; tests
    refuse this name). Cancelling stops waiting only: the uninstaller carries on by itself."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    return await process.wait()


Launch = Callable[[Sequence[str]], Awaitable[int]]
FindOldInstall = Callable[[], "OldInstall | None"]


def _default_find() -> OldInstall | None:
    from app.forge.migration import find_old_install

    return find_old_install()


def _default_launch(command: Sequence[str]) -> Awaitable[int]:
    return _run_uninstaller(command)


# --- the remover -------------------------------------------------------------------------------


class _Renamer(Protocol):
    blocked_by_old_install: bool

    def reconfigure(self) -> None: ...


class _Services(Protocol):
    @property
    def auto(self) -> _Renamer: ...


class Runtime(Protocol):
    """What the remover needs of ForgeRuntime."""

    @property
    def services(self) -> _Services | None: ...


Listener = Callable[["Outcome | None"], None]  # None: a removal started


class OldInstallRemover:
    """Removes the old MediaForge through its own uninstaller and lifts the guard when it is
    gone. `find` is blocking (run in a thread); `launch(command)` starts the uninstaller and
    returns its exit code once it has finished."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        find: FindOldInstall | None = None,
        launch: Launch | None = None,
        powershell: Callable[[], str] | None = None,
    ) -> None:
        self._runtime = runtime
        self._find: FindOldInstall = find or _default_find
        self._launch: Launch = launch or _default_launch
        self._powershell = powershell or _powershell_exe
        self._task: asyncio.Future[Outcome] | None = None
        self._listeners: list[Listener] = []
        self.last: Outcome | None = None  # the last removal's outcome

    @property
    def running(self) -> bool:
        """Whether a removal is under way (the old uninstaller may be waiting for the owner)."""
        return self._task is not None and not self._task.done()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """`listener(None)` when a removal starts, `listener(outcome)` when it has finished."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _tell(self, outcome: Outcome | None) -> None:
        for listener in list(self._listeners):
            try:
                listener(outcome)
            except Exception:
                log.exception("A listener of the old MediaForge's removal failed")

    async def remove(self) -> Outcome:
        """Run the old uninstaller and wait for it; a second call while one runs shares it.
        Waiting may be cancelled (the uninstaller isn't)."""
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._remove())
        return await asyncio.shield(self._task)

    async def _remove(self) -> Outcome:
        self._tell(None)
        try:
            outcome = await self._run()
        except Exception as exc:
            log.error("Removing the old MediaForge failed (%s)", type(exc).__name__)
            outcome = Outcome.FAILED
        self.last = outcome
        self._tell(outcome)
        return outcome

    async def _run(self) -> Outcome:
        old = await asyncio.to_thread(self._find)
        if old is None or not old.registered:
            log.info("The old MediaForge is gone already")
            self.lift_guard()
            return Outcome.REMOVED
        script = uninstaller_script(old)
        if script is None or not await asyncio.to_thread(os.path.isfile, script):
            log.warning("The old MediaForge's uninstall entry wasn't recognised: not started")
            return Outcome.REFUSED
        command = uninstaller_command(script, self._powershell())
        log.info("Starting the old MediaForge's uninstaller")
        try:
            code = await self._launch(command)
        except OSError as exc:
            log.error("The old MediaForge's uninstaller couldn't start (%s)", type(exc).__name__)
            return Outcome.FAILED
        log.info("The old MediaForge's uninstaller finished (exit code %s)", code)
        return await self.check()

    async def check(self) -> Outcome:
        """Look for the old install again: gone → the guard is lifted (REMOVED), else KEPT."""
        try:
            old = await asyncio.to_thread(self._find)
        except Exception as exc:
            log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
            return Outcome.KEPT
        if old is not None and old.registered:
            log.info("The old MediaForge is still installed")
            return Outcome.KEPT
        log.info("The old MediaForge was removed")
        self.lift_guard()
        return Outcome.REMOVED

    def lift_guard(self) -> None:
        """The old install is gone: the stored Off / Trial / On applies again."""
        services = self._runtime.services
        if services is None:  # Forge hasn't started: its start finds no old install
            return
        auto: _Renamer | None = getattr(services, "auto", None)  # a test's fake may have none
        if auto is not None and auto.blocked_by_old_install:
            auto.blocked_by_old_install = False
            log.info("Renaming on its own no longer waits for the old MediaForge")
            auto.reconfigure()


_REMOVERS: weakref.WeakKeyDictionary[object, OldInstallRemover] = weakref.WeakKeyDictionary()


def remover_for(runtime: Runtime) -> OldInstallRemover:
    """The one remover of `runtime` (ForgeRuntime), made on first use."""
    remover = _REMOVERS.get(runtime)
    if remover is None:
        remover = OldInstallRemover(runtime)
        _REMOVERS[runtime] = remover
    return remover


# --- the one-time notice -----------------------------------------------------------------------


def _notice_marker(services: ForgeServices) -> Path:
    return services.paths.forge / NOTICE_MARKER


def _note_shown(marker: Path) -> bool:
    """Write the marker; False when it was there already (or can't be written: then the
    notice isn't shown either, so it never repeats at every start)."""
    try:
        with marker.open("x", encoding="utf-8") as file:
            file.write("shown\n")
    except FileExistsError:
        return False
    except OSError as exc:
        log.warning("The old MediaForge notice's marker wasn't written (%s)", type(exc).__name__)
        return False
    return True


async def startup_notice(
    services: ForgeServices, *, find: FindOldInstall | None = None
) -> str | None:
    """The main window's one-time status-bar notice (translated), or None: shown once while an
    old MediaForge's data exists and nothing was imported yet."""
    if services.settings.current.imported_from:
        return None
    marker = _notice_marker(services)
    if await asyncio.to_thread(marker.exists):
        return None
    try:
        old = await asyncio.to_thread(find or _default_find)
    except Exception as exc:
        log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
        return None
    if old is None or not old.has_data or services.settings.current.imported_from:
        return None
    if not await asyncio.to_thread(_note_shown, marker):
        return None
    return t(NOTICE_TEXT)
