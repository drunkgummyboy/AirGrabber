"""MediaForge's own place in Windows (D8): its "MediaForge" Start-menu shortcut and its
AppUserModelID.

The shortcut carries `APP_USER_MODEL_ID` and starts this install's `main.py --forge` (AirGrabber's
main window on MediaForge's page), run by the pythonw.exe beside AirGrabber's Python.

Who makes the shortcut:
- install.ps1, beside AirGrabber's own (New-ForgeShortcut; keep its constants in step with
  `APP_USER_MODEL_ID`, `ICON_PATH` and `DESCRIPTION`); app/uninstall.ps1 removes it when it
  starts that install.
- An AirGrabber that updated to 3.0.0 has no such shortcut: `ensure_start_menu_shortcut` (at the
  first visit to the page in a run, app/forge/ui/page.py) makes it once. Only in an installed copy (never a development
  copy), only when this install's Start-menu AirGrabber.lnk is there (an install made with
  -NoShortcuts gets none), never over a MediaForge.lnk that starts another program (the old
  MediaForge's: it is made once that one has gone). Once made, or found made by install.ps1, a
  marker in Forge's data folder (`MARKER_NAME`) keeps it from coming back after the user removed
  it.

The shortcuts are read and written through PowerShell (app/services/powershell.py, the shared
helper app/shortcut_app_id.cs for the ID; the writing script is app/services/startup.py's). The
runner, the Start-menu folder and the marker are injectable, so tests never start PowerShell or
touch the real Start menu. Stdlib only, no Tk.
"""

from __future__ import annotations

import base64
import binascii
import logging
from enum import Enum
from pathlib import Path, PureWindowsPath

from app.forge.context_menu import python_runner
from app.services.app_identity import (
    SHORTCUT_NAME as APP_SHORTCUT_NAME,
    default_programs_dir,
    starts_this_install,
)
from app.services.powershell import PowerShellResult, PowerShellRunner, run_powershell
from app.services.startup import ShortcutSpec, shortcut_command
from app.services.updates import is_development_copy

log = logging.getLogger(__name__)

INSTALL_DIR = Path(__file__).resolve().parents[2]
# The "MediaForge" shortcut's AppUserModelID (AirGrabber's is app/identity.py's). Keep in step with
# install.ps1's $ForgeAppId.
APP_USER_MODEL_ID = "drunkgummyboy.AirGrabber.MediaForge"
SHORTCUT_NAME = "MediaForge.lnk"  # in the Start menu, beside AirGrabber.lnk
FORGE_FLAG = "--forge"  # main.py: open MediaForge's page
ICON_PATH = r"app\assets\forge\MediaForge.ico"  # as install.ps1's $ForgeIconPath
DESCRIPTION = "Rename and organise video files"  # as install.ps1 (and the old MediaForge's)
MARKER_NAME = "start_menu_shortcut"  # in Forge's data folder: the shortcut was made once

_APP_LNK_ENV = "AIRGRABBER_FORGE_APP_LNK"
_FORGE_LNK_ENV = "AIRGRABBER_FORGE_LNK"
_APP_TAG = "APP:"
_FORGE_TAG = "FORGE:"


def _base64_line(tag: str, expression: str) -> str:
    """A PowerShell expression printing `tag` + base64 of `expression` in UTF-8."""
    return f"'{tag}' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]({expression})))"


# Prints both shortcuts' Arguments ("" for one that isn't there).
READ_SCRIPT = "; ".join(
    (
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        "function Get-LinkArguments([string]$Path) "
        "{ if (Test-Path -LiteralPath $Path) { return $shell.CreateShortcut($Path).Arguments }; return '' }",
        _base64_line(_APP_TAG, f"Get-LinkArguments $env:{_APP_LNK_ENV}"),
        _base64_line(_FORGE_TAG, f"Get-LinkArguments $env:{_FORGE_LNK_ENV}"),
    )
)


class Outcome(Enum):
    MADE = "made"
    OURS = "already there"  # install.ps1 made it (or an earlier run)
    DONE_BEFORE = "made before"  # the marker: never again
    OTHER_PROGRAM = "MediaForge.lnk starts another program"  # the old MediaForge's
    NO_APP_SHORTCUT = "no Start-menu AirGrabber.lnk of this install"
    DEVELOPMENT_COPY = "development copy"
    NOT_WINDOWS = "not Windows"
    FAILED = "failed"


# -- what the shortcut holds (pure, tested) ------------------------------------------------------


def shortcut_spec(install_dir: Path, runner: Path, programs_dir: Path) -> ShortcutSpec:
    """The "MediaForge" Start-menu shortcut of the copy in `install_dir`, started by `runner`
    (pythonw.exe; `app.forge.context_menu.python_runner`). Pure: nothing is looked up on disk."""
    root = PureWindowsPath(install_dir)
    return ShortcutSpec(
        path=str(PureWindowsPath(programs_dir, SHORTCUT_NAME)),
        target=str(PureWindowsPath(runner)),
        arguments=f'"{root / "main.py"}" {FORGE_FLAG}',
        working_dir=str(root),
        icon=f"{root / ICON_PATH},0",
        description=DESCRIPTION,
        app_id=APP_USER_MODEL_ID,
    )


def skip_reason(app_arguments: str | None, forge_arguments: str | None, install_dir: Path) -> Outcome | None:
    """Why the shortcut isn't made, from both shortcuts' Arguments (None: that one isn't there);
    None: make it."""
    if forge_arguments is not None:
        return Outcome.OURS if starts_this_install(forge_arguments, install_dir) else Outcome.OTHER_PROGRAM
    if app_arguments is None or not starts_this_install(app_arguments, install_dir):
        return Outcome.NO_APP_SHORTCUT
    return None


def parse_read_output(stdout: str) -> tuple[str, str] | None:
    """(AirGrabber.lnk's, MediaForge.lnk's Arguments) from READ_SCRIPT's output; None when they
    aren't there."""
    found: dict[str, str] = {}
    for line in stdout.splitlines():
        for tag in (_APP_TAG, _FORGE_TAG):
            if line.startswith(tag):
                try:
                    found[tag] = base64.b64decode(line[len(tag) :].strip(), validate=True).decode()
                except (binascii.Error, UnicodeDecodeError):
                    return None
    if _APP_TAG not in found or _FORGE_TAG not in found:
        return None
    return found[_APP_TAG], found[_FORGE_TAG]


# -- the check-and-make --------------------------------------------------------------------------


def default_marker() -> Path:
    """The marker in Forge's data folder (honours AIRGRABBER_DATA_DIR)."""
    from app.config import AppPaths

    return AppPaths.default().forge / MARKER_NAME


async def ensure_start_menu_shortcut(
    *,
    install_dir: Path = INSTALL_DIR,
    programs_dir: Path | None = None,
    marker: Path | None = None,
    python: Path | None = None,
    run: PowerShellRunner | None = None,
) -> Outcome:
    """Make this install's "MediaForge" Start-menu shortcut once, for an AirGrabber that updated
    to 3.0.0 (see the module's notes). Never raises (failures are logged); runs PowerShell, so the
    loop stays free.

    Without an injected `programs_dir`, does nothing outside Windows.
    """
    if is_development_copy(install_dir):
        return Outcome.DEVELOPMENT_COPY
    folder = programs_dir if programs_dir is not None else default_programs_dir()
    if folder is None:
        return Outcome.NOT_WINDOWS
    done = marker if marker is not None else default_marker()
    if done.exists():
        return Outcome.DONE_BEFORE
    app_link, forge_link = folder / APP_SHORTCUT_NAME, folder / SHORTCUT_NAME
    if not app_link.is_file() and not forge_link.is_file():
        return Outcome.NO_APP_SHORTCUT
    runner = run if run is not None else run_powershell
    try:
        result = await runner(READ_SCRIPT, {_APP_LNK_ENV: str(app_link), _FORGE_LNK_ENV: str(forge_link)})
        read = parse_read_output(result.stdout) if result.returncode == 0 else None
        if read is None:
            _log_failure("could not read the Start-menu shortcuts", result)
            return Outcome.FAILED
        app_arguments = read[0] if app_link.is_file() else None
        forge_arguments = read[1] if forge_link.is_file() else None
        outcome = skip_reason(app_arguments, forge_arguments, install_dir)
        if outcome is None:
            spec = shortcut_spec(install_dir, python_runner(python), folder)
            script, env = shortcut_command(spec)
            result = await runner(script, env)
            if result.returncode != 0:
                _log_failure("could not make the MediaForge Start-menu shortcut", result)
                return Outcome.FAILED
            if result.stderr.strip():
                log.warning("The MediaForge Start-menu shortcut was made, but: %s", _detail(result))
            log.info("Made the MediaForge Start-menu shortcut")
            outcome = Outcome.MADE
    except (OSError, TimeoutError) as exc:
        log.warning("PowerShell for the MediaForge Start-menu shortcut failed: %r", exc)
        return Outcome.FAILED
    if outcome in (Outcome.MADE, Outcome.OURS):
        _write_marker(done)
    else:
        log.debug("MediaForge Start-menu shortcut not made: %s", outcome.value)
    return outcome


def _write_marker(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("The MediaForge Start-menu shortcut was made once; it isn't made again.\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not remember the MediaForge Start-menu shortcut (%s)", type(exc).__name__)


def _detail(result: PowerShellResult) -> str:
    """PowerShell's error text, its end, without the user's home folder (it holds their name)."""
    return result.stderr.strip()[-500:].replace(str(Path.home()), "~")


def _log_failure(what: str, result: PowerShellResult) -> None:
    log.warning("PowerShell %s (exit %s): %s", what, result.returncode, _detail(result))

