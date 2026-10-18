"""Windows PowerShell 5.1 for AirGrabber's shortcuts (no pywin32).

`run_powershell` runs one short script without a console window and hands back its exit code and
output. Shortcuts are written with WScript.Shell; their AppUserModelID (app/identity.py) with the
C# helper app/shortcut_app_id.cs, which `LOAD_HELPER` compiles (once per PowerShell run) from the
file named in the environment variable `HELPER_ENV` (`helper_env()`). install.ps1 compiles the
same file from the folder it has just installed.

Used by app/services/startup.py and app/services/app_identity.py. Stdlib only.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 30.0
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SHORTCUT_HELPER = Path(__file__).resolve().parents[1] / "shortcut_app_id.cs"
HELPER_TYPE = "AirGrabber.ShortcutAppId"  # the class in SHORTCUT_HELPER
HELPER_ENV = "AIRGRABBER_SHORTCUT_HELPER"
# One PowerShell statement: makes [AirGrabber.ShortcutAppId] available, compiling it only once.
LOAD_HELPER = (
    f"if (-not ('{HELPER_TYPE}' -as [type])) "
    f"{{ Add-Type -TypeDefinition ([IO.File]::ReadAllText($env:{HELPER_ENV})) }}"
)


@dataclass(frozen=True, slots=True)
class PowerShellResult:
    returncode: int
    stdout: str
    stderr: str


# (script, extra environment variables) -> result. Injectable so tests never start PowerShell.
PowerShellRunner = Callable[[str, Mapping[str, str]], Awaitable[PowerShellResult]]


def helper_env(helper: Path = SHORTCUT_HELPER) -> dict[str, str]:
    """The environment `LOAD_HELPER` needs."""
    return {HELPER_ENV: str(helper)}


async def run_powershell(
    script: str, env: Mapping[str, str], *, timeout: float = DEFAULT_TIMEOUT_S
) -> PowerShellResult:
    """Run `script` with `env` added to this process's environment (pass values that way, so they
    never need quoting inside the script).

    Raises OSError when PowerShell cannot start and TimeoutError when it ran out of time (it is
    killed then). Cancelling waits for PowerShell to finish (within `timeout`) before the
    CancelledError goes on: a shortcut is never left half-written.
    """
    command = [
        powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]  # fmt: skip
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env},
        creationflags=NO_WINDOW,
    )
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        async with asyncio.timeout_at(deadline):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        await _kill(process)
        raise
    except asyncio.CancelledError:
        try:
            async with asyncio.timeout_at(deadline):
                await process.wait()
        except TimeoutError:
            await _kill(process)
        raise
    return PowerShellResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


def powershell_exe() -> str:
    """Windows PowerShell 5.1, which every Windows 10/11 has (as install.ps1 is written for)."""
    system_root = os.environ.get("SystemRoot")
    if system_root:
        exe = Path(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if exe.is_file():
            return str(exe)
    return "powershell.exe"


async def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()
