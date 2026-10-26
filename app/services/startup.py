"""Start AirGrabber with Windows: a shortcut in the user's Startup folder that opens it in the tray.

The shortcut mirrors install.ps1's New-Shortcut (pythonw.exe, "<install>\\main.py", the install
folder as working folder, the app icon, AirGrabber's AppUserModelID) plus main.py's `--tray`. Like
install.ps1 it is written by PowerShell's WScript.Shell (no pywin32) and the shared helper
app/shortcut_app_id.cs for the AppUserModelID (app/services/powershell.py); turning it off deletes
the file. Current user only, no administrator permission needed. Stdlib only.

The Startup folder is injectable, so tests never touch the real one.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.i18n import t
from app.identity import APP_USER_MODEL_ID
from app.services.powershell import HELPER_TYPE, LOAD_HELPER, helper_env, run_powershell

log = logging.getLogger(__name__)

INSTALL_DIR = Path(__file__).resolve().parents[2]
SHORTCUT_NAME = "AirGrabber.lnk"
STARTUP_SUBDIR = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")  # in %APPDATA%
ICON_PATH = r"app\assets\airgrabber.ico"  # as install.ps1's $IconPath; also app/ui/branding.py
TRAY_OPTION = "--tray"  # main.py: start with only the tray icon showing
DESCRIPTION = "Track your shows. Grab your movies."  # as install.ps1's shortcuts

# The values arrive as environment variables, so no path ever needs quoting inside the script.
_ENV_PREFIX = "AIRGRABBER_LNK_"
APP_ID_STEP_FAILED = "AppUserModelID not set: "  # on stderr; the shortcut itself was written
_CREATE_SCRIPT = "; ".join(
    (
        "$ErrorActionPreference = 'Stop'",
        f"$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:{_ENV_PREFIX}PATH)",
        f"$link.TargetPath = $env:{_ENV_PREFIX}TARGET",
        f"$link.Arguments = $env:{_ENV_PREFIX}ARGUMENTS",
        f"$link.WorkingDirectory = $env:{_ENV_PREFIX}WORKING_DIR",
        f"$link.IconLocation = $env:{_ENV_PREFIX}ICON",
        f"$link.Description = $env:{_ENV_PREFIX}DESCRIPTION",
        "$link.Save()",
        # The AppUserModelID (app/identity.py), as install.ps1's Set-ShortcutAppId. Optional: the
        # process sets its own ID, so a failure here only warns.
        f"try {{ {LOAD_HELPER}; "
        f"[{HELPER_TYPE}]::Set($env:{_ENV_PREFIX}PATH, $env:{_ENV_PREFIX}APP_ID) }} "
        f"catch {{ [Console]::Error.WriteLine('{APP_ID_STEP_FAILED}' + $_.Exception.Message) }}",
    )
)


class StartupError(Exception):
    """The shortcut could not be added or removed; the message is fit for the UI."""


@dataclass(frozen=True, slots=True)
class ShortcutSpec:
    """Everything the .lnk holds, as Windows path strings."""

    path: str  # the .lnk itself
    target: str  # pythonw.exe
    arguments: str
    working_dir: str
    icon: str  # "<file>,<index>"
    description: str
    app_id: str  # System.AppUserModel.ID


ShortcutWriter = Callable[[ShortcutSpec], Awaitable[None]]


def shortcut_spec(install_dir: Path, python: Path, startup_dir: Path) -> ShortcutSpec:
    """The Startup shortcut for the copy in `install_dir`, run by the pythonw.exe beside `python`
    (sys.executable). Pure: nothing is looked up on disk."""
    root = PureWindowsPath(install_dir)
    return ShortcutSpec(
        path=str(PureWindowsPath(startup_dir, SHORTCUT_NAME)),
        target=str(PureWindowsPath(python).with_name("pythonw.exe")),
        arguments=f'"{root / "main.py"}" {TRAY_OPTION}',
        working_dir=str(root),
        icon=f"{root / ICON_PATH},0",
        description=DESCRIPTION,
        app_id=APP_USER_MODEL_ID,
    )


def default_startup_dir() -> Path | None:
    """%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup; None outside Windows."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    return Path(appdata, *STARTUP_SUBDIR) if appdata else None


def is_enabled(*, startup_dir: Path | None = None) -> bool:
    """Whether AirGrabber's shortcut is in the Startup folder."""
    folder = startup_dir if startup_dir is not None else default_startup_dir()
    return folder is not None and (folder / SHORTCUT_NAME).is_file()


async def set_enabled(
    on: bool,
    *,
    startup_dir: Path | None = None,
    install_dir: Path = INSTALL_DIR,
    python: Path | None = None,
    write: ShortcutWriter | None = None,
) -> None:
    """Add (or rewrite) the Startup shortcut, or remove it. Raises StartupError.

    Without an injected `startup_dir`, does nothing outside Windows.
    """
    folder = startup_dir if startup_dir is not None else default_startup_dir()
    if folder is None:
        return
    link = folder / SHORTCUT_NAME
    if not on:
        try:
            link.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove the Startup shortcut: %r", exc)
            raise StartupError(t("AirGrabber could not be removed from Windows startup.")) from exc
        log.info("Start with Windows: off")
        return

    python = python if python is not None else Path(sys.executable)
    spec = shortcut_spec(install_dir, python, folder)
    if not Path(spec.target).is_file():  # as install.ps1: python.exe when there is no pythonw.exe
        spec = dataclasses.replace(spec, target=str(python))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create the Startup folder: %r", exc)
        raise StartupError(t("AirGrabber could not be added to Windows startup.")) from exc
    await (write or write_shortcut)(spec)
    log.info("Start with Windows: on")


def shortcut_command(spec: ShortcutSpec) -> tuple[str, dict[str, str]]:
    """The PowerShell script that writes `spec`, and the environment variables carrying its values."""
    env = {
        **helper_env(),
        f"{_ENV_PREFIX}PATH": spec.path,
        f"{_ENV_PREFIX}TARGET": spec.target,
        f"{_ENV_PREFIX}ARGUMENTS": spec.arguments,
        f"{_ENV_PREFIX}WORKING_DIR": spec.working_dir,
        f"{_ENV_PREFIX}ICON": spec.icon,
        f"{_ENV_PREFIX}DESCRIPTION": spec.description,
        f"{_ENV_PREFIX}APP_ID": spec.app_id,
    }
    return _CREATE_SCRIPT, env


async def write_shortcut(spec: ShortcutSpec) -> None:
    """Create or overwrite the .lnk through PowerShell's WScript.Shell, with its AppUserModelID.
    Raises StartupError (not when only the AppUserModelID could not be set: that is logged)."""
    script, env = shortcut_command(spec)
    failed = StartupError(t("AirGrabber could not be added to Windows startup."))
    try:
        result = await run_powershell(script, env)
    except OSError as exc:
        log.warning("Could not start PowerShell for the Startup shortcut: %r", exc)
        raise failed from exc
    except TimeoutError:
        log.warning("PowerShell took too long writing the Startup shortcut")
        raise failed from None
    detail = result.stderr.strip()[-500:]
    if result.returncode != 0:
        log.warning("PowerShell could not write the Startup shortcut (%s): %s", result.returncode, detail)
        raise failed
    if detail:
        log.warning("The Startup shortcut was written, but: %s", detail)
