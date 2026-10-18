"""Give an older install's Start-menu shortcut AirGrabber's AppUserModelID (app/identity.py).

Windows only takes a notification's name and icon from a Start-menu shortcut that carries the
process's AppUserModelID. install.ps1 now writes the ID into its shortcuts, but copies installed
before that have a Start-menu AirGrabber.lnk without it. `ensure_start_menu_app_id` (main.py, once
per launch, in the background) adds the ID to that one shortcut when its Arguments start this
install's main.py and it has no ID yet. Never in a development copy, never to any other shortcut,
and an ID that is already there is left alone.

Read and written through PowerShell (app/services/powershell.py). The runner and the Start-menu
folder are injectable, so tests never start PowerShell or touch the real Start menu.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import sys
from enum import Enum
from pathlib import Path

from app.identity import APP_USER_MODEL_ID
from app.services.install_info import same_path
from app.services.powershell import (
    HELPER_TYPE,
    LOAD_HELPER,
    PowerShellResult,
    PowerShellRunner,
    helper_env,
    run_powershell,
)
from app.services.updates import is_development_copy

log = logging.getLogger(__name__)

INSTALL_DIR = Path(__file__).resolve().parents[2]
SHORTCUT_NAME = "AirGrabber.lnk"  # install.ps1's Start-menu shortcut
START_MENU_SUBDIR = ("Microsoft", "Windows", "Start Menu", "Programs")  # in %APPDATA%

_LINK_ENV = "AIRGRABBER_APPID_LNK"
_APP_ID_ENV = "AIRGRABBER_APPID_VALUE"
_ARGUMENTS_TAG = "ARGUMENTS:"
_APP_ID_TAG = "APP_ID:"


def _base64_line(tag: str, expression: str) -> str:
    """A PowerShell expression printing `tag` + `expression` as base64 of UTF-8 (ASCII-safe
    whatever the console code page)."""
    return f"'{tag}' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]({expression})))"


# Prints the shortcut's Arguments and AppUserModelID ("" when it has none).
READ_SCRIPT = "; ".join(
    (
        "$ErrorActionPreference = 'Stop'",
        LOAD_HELPER,
        f"$lnk = $env:{_LINK_ENV}",
        _base64_line(_ARGUMENTS_TAG, "(New-Object -ComObject WScript.Shell).CreateShortcut($lnk).Arguments"),
        _base64_line(_APP_ID_TAG, f"[{HELPER_TYPE}]::Get($lnk)"),
    )
)
WRITE_SCRIPT = "; ".join(
    (
        "$ErrorActionPreference = 'Stop'",
        LOAD_HELPER,
        f"[{HELPER_TYPE}]::Set($env:{_LINK_ENV}, $env:{_APP_ID_ENV})",
    )
)


class Outcome(Enum):
    FIXED = "fixed"  # the ID was added
    HAS_APP_ID = "has an AppUserModelID"
    NOT_THIS_INSTALL = "starts another copy"
    NO_SHORTCUT = "no Start-menu shortcut"
    DEVELOPMENT_COPY = "development copy"
    NOT_WINDOWS = "not Windows"
    FAILED = "failed"


# -- decisions (pure, tested) --------------------------------------------------------------------


def first_argument(arguments: str) -> str:
    """The first argument of a shortcut's Arguments: the text inside the first pair of quotes
    (`"<install>\\main.py" --tray` → `<install>\\main.py`), else the whole trimmed text.
    As uninstall.ps1's Get-FirstArgument."""
    text = arguments.strip()
    if not text.startswith('"'):
        return text
    end = text.find('"', 1)
    return text[1:] if end < 0 else text[1:end]


def starts_this_install(arguments: str, install_dir: Path) -> bool:
    """Whether a shortcut with these Arguments starts `install_dir`'s main.py."""
    return same_path(first_argument(arguments), str(install_dir / "main.py"))


def skip_reason(arguments: str, app_id: str, install_dir: Path) -> Outcome | None:
    """Why a shortcut with these Arguments and AppUserModelID is left alone; None: it needs the ID."""
    if not starts_this_install(arguments, install_dir):
        return Outcome.NOT_THIS_INSTALL
    if app_id.strip():
        return Outcome.HAS_APP_ID
    return None


def parse_read_output(stdout: str) -> tuple[str, str] | None:
    """(arguments, app id) from READ_SCRIPT's output; None when it is not there."""
    found: dict[str, str] = {}
    for line in stdout.splitlines():
        for tag in (_ARGUMENTS_TAG, _APP_ID_TAG):
            if line.startswith(tag):
                try:
                    found[tag] = base64.b64decode(line[len(tag) :].strip(), validate=True).decode()
                except (binascii.Error, UnicodeDecodeError):
                    return None
    if _ARGUMENTS_TAG not in found or _APP_ID_TAG not in found:
        return None
    return found[_ARGUMENTS_TAG], found[_APP_ID_TAG]


# -- the check-and-fix ---------------------------------------------------------------------------


def default_programs_dir() -> Path | None:
    """%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs; None outside Windows."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    return Path(appdata, *START_MENU_SUBDIR) if appdata else None


async def ensure_start_menu_app_id(
    *,
    install_dir: Path = INSTALL_DIR,
    programs_dir: Path | None = None,
    run: PowerShellRunner | None = None,
) -> Outcome:
    """Add AirGrabber's AppUserModelID to this install's Start-menu AirGrabber.lnk when it has
    none. Never raises (failures are logged); runs PowerShell, so the loop stays free.

    Without an injected `programs_dir`, does nothing outside Windows.
    """
    if is_development_copy(install_dir):
        return Outcome.DEVELOPMENT_COPY
    folder = programs_dir if programs_dir is not None else default_programs_dir()
    if folder is None:
        return Outcome.NOT_WINDOWS
    link = folder / SHORTCUT_NAME
    if not link.is_file():
        return Outcome.NO_SHORTCUT
    runner = run if run is not None else run_powershell
    env = {**helper_env(), _LINK_ENV: str(link)}
    try:
        result = await runner(READ_SCRIPT, env)
        read = parse_read_output(result.stdout) if result.returncode == 0 else None
        if read is None:
            _log_failure("could not read the Start-menu shortcut", result)
            return Outcome.FAILED
        skip = skip_reason(*read, install_dir)
        if skip is not None:
            log.debug("Start-menu shortcut left alone: %s", skip.value)
            return skip
        result = await runner(WRITE_SCRIPT, {**env, _APP_ID_ENV: APP_USER_MODEL_ID})
        if result.returncode != 0:
            _log_failure("could not give the Start-menu shortcut its AppUserModelID", result)
            return Outcome.FAILED
    except (OSError, TimeoutError) as exc:
        log.warning("PowerShell for the Start-menu shortcut failed: %r", exc)
        return Outcome.FAILED
    log.info("Gave the Start-menu shortcut AirGrabber's AppUserModelID (%s)", APP_USER_MODEL_ID)
    return Outcome.FIXED


def _log_failure(what: str, result: PowerShellResult) -> None:
    detail = result.stderr.strip()[-500:]
    log.warning("PowerShell %s (exit %s): %s", what, result.returncode, detail)
