"""Whether Windows keeps the phones out (Settings → Phone connection's hint, `PhoneService.
check_network`): the network this PC's traffic leaves by is set to Public (Windows lets nothing
in there by default), or Windows Firewall has a rule blocking this Python from being reached
(what choosing Cancel on Windows' own question leaves behind; that question calls AirGrabber
"Python", the program that runs it).

Asked of Windows PowerShell (`Get-NetRoute`, `Get-NetConnectionProfile`, `Get-NetFirewallRule`)
as a subprocess of the loop, never in a thread, with CHECK_TIMEOUT_S at most: no answer in time,
or any failure, is "can't tell" (None), which says nothing. Its output names programs' paths:
never logged, and only compared here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 20.0
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# The default route's network's category, and every enabled inbound block rule's program and
# profiles, as one line of JSON in UTF-8.
_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' |
    Sort-Object RouteMetric | Select-Object -First 1
$category = ''
if ($route) {
    $network = Get-NetConnectionProfile -InterfaceIndex $route.InterfaceIndex |
        Select-Object -First 1
    $category = [string]$network.NetworkCategory
}
$blocked = @(
    foreach ($rule in @(Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True)) {
        $filter = $rule | Get-NetFirewallApplicationFilter
        [pscustomobject]@{ program = [string]$filter.Program; profile = [string]$rule.Profile }
    }
)
@{ category = $category; blocked = $blocked } | ConvertTo-Json -Compress -Depth 4
"""


@dataclass(frozen=True, slots=True)
class NetworkFacts:
    """`public`: the network the PC's traffic leaves by is Public. `blocked`: an enabled inbound
    block rule for this Python counts on that network."""

    public: bool
    blocked: bool


NetworkCheck = Callable[[], Awaitable["NetworkFacts | None"]]
Run = Callable[[Sequence[str], float], Awaitable[bytes | None]]


def this_python() -> set[str]:
    """This Python's programs as a firewall rule names them (python.exe and pythonw.exe, also the
    base one a virtual environment's start hands over to), compared as `_same_path` does."""
    found: set[str] = set()
    for one in {sys.executable, getattr(sys, "_base_executable", sys.executable)}:
        if not one:
            continue
        folder = os.path.dirname(one)
        for name in (os.path.basename(one), "python.exe", "pythonw.exe"):
            found.add(_same_path(os.path.join(folder, name)))
    return found


def _same_path(path: str) -> str:
    expanded = os.path.expandvars(path.strip().strip('"'))
    with contextlib.suppress(OSError, ValueError):
        expanded = os.path.realpath(expanded)
    return os.path.normcase(os.path.normpath(expanded))


def read_facts(data: Any, programs: Iterable[str]) -> NetworkFacts | None:
    """The script's JSON → NetworkFacts for these `programs` (`this_python`); None when it isn't
    what the script writes."""
    if not isinstance(data, dict):
        return None
    category = str(data.get("category") or "")
    rules = data.get("blocked") or []
    if isinstance(rules, dict):  # one rule may come as itself, not in a list
        rules = [rules]
    if not isinstance(rules, list):
        return None
    wanted = {_same_path(one) for one in programs}
    # The rule's profiles as Windows names them ("Any", "Public", "Private, Public", "Domain").
    network = {"Public": "public", "Private": "private", "DomainAuthenticated": "domain"}.get(
        category
    )
    blocked = False
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("program"):
            continue
        if _same_path(str(rule["program"])) not in wanted:
            continue
        profiles = {part.strip().lower() for part in str(rule.get("profile", "")).split(",")}
        if network is None or "any" in profiles or network in profiles:
            blocked = True
    return NetworkFacts(public=category == "Public", blocked=blocked)


async def _run(args: Sequence[str], timeout_s: float) -> bytes | None:
    """What the program printed; None when it failed, or didn't end in time (then killed)."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        log.info("Network check: PowerShell didn't start (%s)", type(exc).__name__)
        return None
    try:
        async with asyncio.timeout(timeout_s):
            out, _ = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        log.info("Network check: Windows didn't answer in time")
        return None
    if process.returncode != 0:
        log.info("Network check: PowerShell ended with %s", process.returncode)
        return None
    return out


def _powershell() -> list[str]:
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]  # fmt: skip


async def check_network(
    *, run: Run = _run, programs: Iterable[str] | None = None
) -> NetworkFacts | None:
    """Windows' word on the network and the firewall; None when it can't tell (not Windows, no
    answer in time, a failure). `run` / `programs`: for tests."""
    if sys.platform != "win32" and run is _run:
        return None
    out = await run(_powershell(), CHECK_TIMEOUT_S)
    if not out:
        return None
    try:
        data = json.loads(out.decode("utf-8-sig", errors="replace"))
    except ValueError:
        log.info("Network check: Windows' answer couldn't be read")
        return None
    facts = read_facts(data, this_python() if programs is None else programs)
    if facts is not None:
        log.info("Network check: public %s, blocked %s", facts.public, facts.blocked)
    return facts
