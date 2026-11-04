"""Launch-time package check: install whatever requirements.txt lists that is missing or too old.

pip installs into the interpreter running the app while a small window shows its output. This module
runs *before* the check has passed, so it (like `app.config`) must stay stdlib-only. That is also why
the window is plain tkinter with the theme colours copied in: `app.ui.theme` imports customtkinter.

The packages below a "# optional" comment line in requirements.txt are optional: AirGrabber works
without them (pywebview: trailers then open in the browser). They are installed one by one after
the required ones, and a failure (offline, a package that won't build) never stops the launch; it
is logged and remembered in the data folder (OPTIONAL_STATE_FILE), so the next try comes only
OPTIONAL_RETRY_S later, or at once when the package's line in requirements.txt changes or
`retry_optional(name)` forgot the failure (MediaForge's page's "Try again" for guessit).
install.ps1 reads the file the same way.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import logging
import operator
import os
import re
import site
import subprocess
import sys
import time
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk

from app import APP_NAME
from app.config import AppPaths

log = logging.getLogger(__name__)

REQUIREMENTS_FILE = Path(__file__).resolve().parent.parent / "requirements.txt"
_ICON_FILE = Path(__file__).resolve().parent / "assets" / "airgrabber.ico"

# Copies of app.ui.theme tokens (that module needs customtkinter, which may be what is missing).
_BG = "#161616"
_SURFACE = "#242424"
_ACCENT = "#4E6A85"
_TEXT = "#FFFFFF"
_TEXT_MUTED = "#A4B2C6"

_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no console flash for pip
_POLL_MS = 150
_STATUS_CHARS = 64

OPTIONAL_MARKER = "optional"  # a comment line starting so: the requirements below it are optional
OPTIONAL_RETRY_S = 24 * 3600  # an optional package that failed to install is tried again this much later
OPTIONAL_STATE_FILE = "optional-packages.json"  # in the data folder: requirement line -> last failure (epoch s)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CLAUSE = re.compile(r"(==|!=|>=|<=|~=|>|<)\s*(\S+)")
_RELEASE = re.compile(r"\d+(?:\.\d+)*")
_COMPARE: dict[str, Callable[[tuple[int, ...], tuple[int, ...]], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "~=": operator.ge,  # compatible release: only its lower bound is checked here
}


@dataclass(frozen=True, slots=True)
class Requirement:
    name: str
    clauses: tuple[tuple[str, str], ...] = ()  # e.g. ((">=", "2.9"),)
    optional: bool = False  # below the "# optional" line: tried, never blocks the launch
    line: str = field(default="", compare=False)  # as written (comment dropped): what pip is given

    @property
    def specifier(self) -> str:
        return ",".join(op + version for op, version in self.clauses)

    @property
    def spec(self) -> str:
        """The requirement for pip's command line."""
        return self.line or self.name + self.specifier


@dataclass(frozen=True, slots=True)
class Problem:
    requirement: Requirement
    installed: str | None  # None: not installed at all

    def __str__(self) -> str:
        if self.installed is None:
            return f"{self.requirement.name} (not installed)"
        return f"{self.requirement.name} {self.installed} (needs {self.requirement.specifier})"


def parse_requirements(text: str) -> list[Requirement]:
    """The `name[extras] op version, ...` lines of a requirements file.

    Comments, pip options (`-r`, `--index-url`, ...) and environment markers are skipped. Clauses this
    checker can't read (URLs, wildcards) are dropped, which leaves only a presence check for pip to judge.
    The requirements after a "# optional" comment line are `optional`.
    """
    requirements: list[Requirement] = []
    optional = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            optional = optional or stripped.lstrip("#").strip().lower().startswith(OPTIONAL_MARKER)
            continue
        written = raw.split("#", 1)[0].strip()
        line = written.split(";", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _NAME.match(line)
        if name is None:
            continue
        rest = re.sub(r"^\s*\[[^\]]*\]", "", line[name.end() :])
        clauses: list[tuple[str, str]] = []
        for part in rest.split(","):
            clause = _CLAUSE.fullmatch(part.strip())
            if clause and _RELEASE.fullmatch(clause[2]):
                clauses.append((clause[1], clause[2]))
        requirements.append(Requirement(name[0], tuple(clauses), optional, written))
    return requirements


def version_tuple(version: str) -> tuple[int, ...] | None:
    """Release numbers with trailing zeros dropped: '2.14.0b1' -> (2, 14), '10.0' -> (10,).

    Pre/post/dev/local parts are ignored. Lenient on purpose: this only decides whether pip should run.
    """
    match = _RELEASE.match(version.strip())
    if match is None:
        return None
    parts = [int(part) for part in match[0].split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def unmet(
    requirements: Sequence[Requirement], lookup: Callable[[str], str | None] | None = None
) -> list[Problem]:
    find = lookup or installed_version
    problems: list[Problem] = []
    for requirement in requirements:
        installed = find(requirement.name)
        if installed is None or not _satisfies(installed, requirement):
            problems.append(Problem(requirement, installed))
    return problems


def _satisfies(installed: str, requirement: Requirement) -> bool:
    have = version_tuple(installed)
    if have is None:
        return True  # unreadable version string: leave it to pip
    for op, wanted in requirement.clauses:
        want = version_tuple(wanted)
        if want is not None and not _COMPARE[op](have, want):
            return False
    return True


def pip_install(*specs: str) -> list[str]:
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *specs]


def install_commands(
    required: Sequence[Requirement], optional: Sequence[Requirement] = ()
) -> tuple[list[list[str]], list[list[str]]]:
    """pip's commands: those that must succeed (every required requirement in one go, so pip resolves
    them together; ensurepip first when pip is missing), then the optional ones (a package each, a
    failure tolerated). Without required ones, ensurepip is tolerated too."""
    ensure = [[sys.executable, "-m", "ensurepip", "--upgrade"]] if importlib.util.find_spec("pip") is None else []
    optional_commands = [pip_install(requirement.spec) for requirement in optional]
    if required:
        return [*ensure, pip_install(*(requirement.spec for requirement in required))], optional_commands
    return [], [*ensure, *optional_commands]


def refresh_import_paths() -> None:
    """Let this process see what pip just installed.

    pip falls back to the per-user site-packages when the main one isn't writable, and Python only
    puts that folder on sys.path at startup if it already existed.
    """
    if site.ENABLE_USER_SITE:
        user_site = site.getusersitepackages()
        if user_site not in sys.path and os.path.isdir(user_site):
            site.addsitedir(user_site)
    importlib.invalidate_caches()


def ensure_requirements(
    paths: AppPaths, requirements_file: Path = REQUIREMENTS_FILE, *, clock: Callable[[], float] = time.time
) -> bool:
    """Install missing or outdated requirements. False: required ones are still missing, the app
    can't start (optional ones never decide that)."""
    try:
        requirements = parse_requirements(requirements_file.read_text(encoding="utf-8"))
    except OSError:
        log.warning("Cannot read %s; skipping the package check", requirements_file)
        return True
    problems = unmet(requirements)
    if not problems:
        return True

    now = clock()
    failures = OptionalFailures(paths.root / OPTIONAL_STATE_FILE)
    required = [problem for problem in problems if not problem.requirement.optional]
    optional = [
        problem for problem in problems if problem.requirement.optional and failures.due(problem.requirement, now)
    ]
    waiting = [problem for problem in problems if problem.requirement.optional and problem not in optional]
    if waiting:
        log.info("Optional packages not tried again yet: %s", "; ".join(map(str, waiting)))
    if not required and not optional:
        return True

    log.warning("Installing missing packages: %s", "; ".join(map(str, [*required, *optional])))
    log_file = paths.logs / "install.log"
    commands, optional_commands = install_commands(
        [requirement for requirement in requirements if not requirement.optional] if required else [],
        [problem.requirement for problem in optional],
    )
    returncode = _install(commands, log_file, [*required, *optional], optional=optional_commands)
    if returncode is None:
        failures.record([problem.requirement for problem in optional], now)
        if required:
            log.info("Package installation cancelled")
            return False
        log.info("Optional package installation cancelled; starting without them")
        return True

    refresh_import_paths()
    remaining = unmet(requirements)
    tried = [problem.requirement for problem in optional]
    still_optional = [problem for problem in remaining if problem.requirement.optional and problem.requirement in tried]
    failures.record([problem.requirement for problem in still_optional], now)
    failures.forget([requirement for requirement in tried if all(p.requirement != requirement for p in still_optional)])
    if still_optional:
        log.warning(
            "Optional packages not installed, AirGrabber starts without them: %s. Last pip output:\n%s",
            "; ".join(map(str, still_optional)), _tail(log_file),
        )  # fmt: skip
    remaining = [problem for problem in remaining if not problem.requirement.optional]
    if not remaining:
        if required:
            log.info("Installed the missing packages")
        elif not still_optional:
            log.info("Installed the optional packages")
        return True
    if returncode == 0 and all(problem.installed is not None for problem in remaining):
        # pip says everything is satisfied; on odd version strings the two can disagree, so trust pip.
        log.warning("pip finished, but these still look outdated: %s", "; ".join(map(str, remaining)))
        return True

    log.error(
        "Package installation failed (exit code %s). Last pip output:\n%s",
        returncode, _tail(log_file),
    )  # fmt: skip
    _report_failure(remaining, log_file)
    return False


def retry_optional(name: str, state_file: Path | None = None) -> bool:
    """Forget the remembered install failure of the optional requirement `name` (as pip names it,
    any case, `-` / `_` / `.` alike), so the next start tries it again at once instead of after
    OPTIONAL_RETRY_S. `state_file`: OPTIONAL_STATE_FILE in the default data folder. True when a
    failure was forgotten. Never raises."""
    failures = OptionalFailures(state_file or AppPaths.default().root / OPTIONAL_STATE_FILE)
    return failures.forget_name(name)


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class OptionalFailures:
    """When each optional requirement (by its line in requirements.txt) last failed to install: a
    small JSON file in the data folder, so an optional package that can't be installed (offline, a
    build failure) isn't tried at every launch. Never raises."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._failed = self._load()

    def due(self, requirement: Requirement, now: float) -> bool:
        """Whether to try it now: never failed, failed OPTIONAL_RETRY_S ago or longer, or the clock
        went back."""
        last = self._failed.get(requirement.spec)
        return last is None or not 0 <= now - last < OPTIONAL_RETRY_S

    def record(self, requirements: Sequence[Requirement], now: float) -> None:
        for requirement in requirements:
            self._failed[requirement.spec] = now
        if requirements:
            self._save()

    def forget(self, requirements: Sequence[Requirement]) -> None:
        if [self._failed.pop(requirement.spec) for requirement in requirements if requirement.spec in self._failed]:
            self._save()

    def forget_name(self, name: str) -> bool:
        """Forget every remembered failure of the requirement called `name`, whatever its line."""
        wanted = _canonical_name(name)
        lines = [
            line
            for line in self._failed
            if any(_canonical_name(requirement.name) == wanted for requirement in parse_requirements(line))
        ]
        for line in lines:
            del self._failed[line]
        if lines:
            self._save()
        return bool(lines)

    def _load(self) -> dict[str, float]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): float(value) for key, value in data.items() if isinstance(value, int | float)}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._failed, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            log.warning("Cannot write %s", self._path.name)


class Installer:
    """Runs commands one after another, stopping at the first failure; output goes to `log_file`.
    Then the `optional` commands, each run whatever the others did: their failures only count in
    `optional_failures`, never in `returncode` (none run after a failed required one).

    Non-blocking: call `poll()` repeatedly (from a Tk `after` loop or a sleep loop) until `finished`.
    """

    def __init__(
        self, commands: Sequence[Sequence[str]], log_file: Path, optional: Sequence[Sequence[str]] = ()
    ) -> None:
        self._queue = [list(command) for command in commands]
        self._optional = [list(command) for command in optional]
        self._log_file = log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("", encoding="utf-8")
        self._reader = log_file.open(encoding="utf-8", errors="replace")
        self._partial = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._running_optional = False
        self.returncode: int | None = None  # once finished: 0, or the failing command's exit code
        self.optional_failures = 0

    @property
    def finished(self) -> bool:
        return self.returncode is not None

    def poll(self) -> list[str]:
        """Advance the queue and return the output lines written since the previous call."""
        if self._process is not None and (code := self._process.poll()) is not None:
            self._process = None
            if code != 0:
                self._failed(code)
        while self._process is None and self.returncode is None:
            if self._queue:
                self._start(self._queue.pop(0), optional=False)
            elif self._optional:
                self._start(self._optional.pop(0), optional=True)
            else:
                self.returncode = 0
        return self._read_lines()

    def cancel(self) -> None:
        self._queue.clear()
        self._optional.clear()
        if self._process is not None:
            self._process.kill()
            self._process.wait()
            self._process = None

    def close(self) -> None:
        self.cancel()
        self._reader.close()

    def _failed(self, code: int) -> None:
        if self._running_optional:
            self.optional_failures += 1
        else:
            self.returncode = code

    def _start(self, command: list[str], *, optional: bool) -> None:
        self._running_optional = optional
        with self._log_file.open("a", encoding="utf-8") as out:
            out.write("> " + subprocess.list2cmdline(command) + "\n")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            with self._log_file.open("ab") as out:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=_NO_WINDOW,
                )
        except OSError as exc:
            with self._log_file.open("a", encoding="utf-8") as out:
                out.write(f"Could not start it: {exc}\n")
            self._failed(1)

    def _read_lines(self) -> list[str]:
        lines = (self._partial + self._reader.read()).split("\n")
        self._partial = "" if self.finished else lines.pop()
        return [line for line in lines if line.strip()]


def _install(
    commands: Sequence[Sequence[str]],
    log_file: Path,
    problems: Sequence[Problem],
    *,
    optional: Sequence[Sequence[str]] = (),
) -> int | None:
    """Run the install with a progress window (console output when Tk can't open one).

    Returns the exit code of the required commands (0 when there are none), or None when the user
    cancelled.
    """
    installer = Installer(commands, log_file, optional)
    try:
        try:
            window = _ProgressWindow(installer, problems)
        except tk.TclError:
            return _run_in_console(installer)
        return window.run()
    finally:
        installer.close()


def _run_in_console(installer: Installer) -> int | None:
    try:
        while True:
            _echo(installer.poll())
            if installer.finished:
                return installer.returncode
            time.sleep(_POLL_MS / 1000)
    except KeyboardInterrupt:
        installer.cancel()
        return None


class _ProgressWindow:
    """Plain-tkinter window that drives an Installer and shows pip's latest line."""

    def __init__(self, installer: Installer, problems: Sequence[Problem]) -> None:
        self._installer = installer
        self._cancelled = False
        self._after_id: str | None = None
        _enable_dpi_awareness()
        self._root = root = tk.Tk()
        root.title(APP_NAME)
        if sys.platform == "win32" and _ICON_FILE.is_file():
            root.iconbitmap(str(_ICON_FILE))  # type: ignore[no-untyped-call]  # untyped in typeshed
        root.configure(bg=_BG, padx=24, pady=20)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._cancel)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(
            "Setup.Horizontal.TProgressbar",
            troughcolor=_SURFACE, background=_ACCENT, bordercolor=_BG, lightcolor=_ACCENT, darkcolor=_ACCENT,
        )  # fmt: skip
        names = ", ".join(problem.requirement.name for problem in problems)
        heading = (
            "Installing optional packages"
            if all(problem.requirement.optional for problem in problems)
            else "Installing required packages"
        )
        tk.Label(root, text=heading, font=("Segoe UI", 12, "bold"), bg=_BG, fg=_TEXT, anchor="w").pack(fill="x")
        tk.Label(
            root, text=f"First-time setup: {names}", font=("Segoe UI", 9), bg=_BG, fg=_TEXT_MUTED, anchor="w"
        ).pack(fill="x", pady=(2, 12))
        bar = ttk.Progressbar(root, mode="indeterminate", style="Setup.Horizontal.TProgressbar")
        bar.pack(fill="x")
        bar.start(12)
        self._status = tk.Label(
            root, text="Starting pip…", width=_STATUS_CHARS, font=("Segoe UI", 9), bg=_BG, fg=_TEXT_MUTED, anchor="w"
        )
        self._status.pack(fill="x", pady=(10, 0))

        root.update_idletasks()
        _dark_title_bar(root)
        x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
        y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
        root.geometry(f"+{x}+{y}")

    def run(self) -> int | None:
        self._after_id = self._root.after(0, self._tick)
        self._root.mainloop()
        if self._after_id is not None:
            self._root.after_cancel(self._after_id)
        self._root.destroy()
        return None if self._cancelled else self._installer.returncode

    def _tick(self) -> None:
        lines = self._installer.poll()
        _echo(lines)
        if lines:
            self._status.configure(text=_shorten(lines[-1].strip(), _STATUS_CHARS))
        if self._installer.finished:
            self._after_id = None
            self._root.quit()
        else:
            self._after_id = self._root.after(_POLL_MS, self._tick)

    def _cancel(self) -> None:
        self._cancelled = True
        self._installer.cancel()
        self._root.quit()


def _enable_dpi_awareness() -> None:
    """The call customtkinter makes on start-up; done first so this window is sharp at 150 % too."""
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def _dark_title_bar(window: tk.Tk) -> None:
    """Windows 10/11 dark title bar, matching the customtkinter windows that follow."""
    if sys.platform == "win32":
        import ctypes

        try:
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 before Windows 10 20H1)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), 4) == 0:
                    break
        except (AttributeError, OSError):
            pass


def _report_failure(problems: Sequence[Problem], log_file: Path) -> None:
    message = (
        f"{APP_NAME} could not install the packages it needs:\n\n"
        + "\n".join(f"•  {problem}" for problem in problems)
        + f"\n\nCheck your internet connection and start {APP_NAME} again.\n"
        + f"pip's output is in:\n{log_file}"
    )
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message, parent=root)
        root.destroy()
    except tk.TclError:
        pass


def _echo(lines: Sequence[str]) -> None:
    if sys.stderr is not None:  # None under pythonw.exe
        for line in lines:
            print(f"  {line}", file=sys.stderr)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no output)"
