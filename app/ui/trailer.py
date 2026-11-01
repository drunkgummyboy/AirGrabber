"""Trailers in AirGrabber's own player window (app/trailer_player.py) instead of the web browser.

`open_trailer(url)` (the info window's "▶ Watch trailer", app/ui/title_info.py): a YouTube link
plays in a borderless window run by a process of its own, `python -m app.trailer_player
<options> -- <video id>`: pywebview (Edge WebView2) runs its own WinForms loop, which can't share
this process's Tk loop, so never a thread here. It is the interpreter running AirGrabber
(pythonw.exe when it runs windowless), without a console, from the folder holding `app/`. Its
colours are theme.py's dark halves (`player_look`: the player is always dark), its ✕'s tip
"Close (Esc)" in the app's language.

The browser instead, as before, for a link that isn't YouTube's (`youtube_id`), when the player
can't run here (`player_available`: not Windows, or pywebview isn't installed) or doesn't come
up: it can't be started, it ends before its page has loaded (no WebView2, pythonnet trouble:
EXIT_CODES), or it says nothing for READY_TIMEOUT_S (then it is ended). Each such case logs
one short English line, never the link or the video id. A video YouTube won't play embedded
the player itself opens in the browser.

One trailer at a time (TrailerPlayer, one per app behind `open_trailer` / `close_trailer`): a
new one closes the one showing — its stdin is closed, so it closes its window and ends — and so
does AirGrabber shutting down (`tasks.cancel_all` cancels the task that watches it). The player
also closes when AirGrabber's end of its stdin goes away, however AirGrabber ends.

`on_state` (optional, per trailer) hears how it goes, on the loop: OPENING when the player is
being started, PLAYING once it says "ready" (its page has loaded), then CLOSED when it ends
(closed by the user, replaced, AirGrabber shutting down), or FAILED when the trailer went to the
browser instead (also at once, without OPENING, for a link that isn't YouTube's or without
pywebview). A failing listener is logged, never raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import subprocess
import sys
import webbrowser
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.parse import parse_qs, urlsplit

from app import APP_NAME
from app.i18n import current_language, t
from app.trailer_player import EXIT_CODES, EXIT_IN_BROWSER, READY_LINE, VIDEO_ID
from app.ui import palettes, theme
from app.ui.borderless import CLOSE_TIP
from app.ui.tasks import spawn

log = logging.getLogger(__name__)

TrailerState = Literal["opening", "playing", "closed", "failed"]
OPENING: Final = "opening"  # the player is starting
PLAYING: Final = "playing"  # its window shows the trailer
CLOSED: Final = "closed"  # it ended
FAILED: Final = "failed"  # the trailer went to the browser instead
StateListener = Callable[[TrailerState], object]

PLAYER_MODULE = "app.trailer_player"
READY_TIMEOUT_S = 20.0  # the player's page loaded by then, or the browser instead
END_TIMEOUT_S = 5.0  # a player that didn't come up: this long to learn its exit code
APP_ROOT = Path(__file__).resolve().parents[2]  # the folder holding app/ (`-m` runs from there)
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_PATH_KINDS = frozenset({"embed", "shorts", "v", "live"})


def youtube_id(url: str) -> str | None:
    """The video id in a YouTube link (watch?v=…, youtu.be/…, /embed/…, /shorts/…, /v/…,
    /live/…); None for anything else."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    candidate: str | None = None
    if host in _SHORT_HOSTS:
        candidate = segments[0] if segments else None
    elif host in _YOUTUBE_HOSTS:
        if segments == ["watch"]:
            values = parse_qs(parts.query).get("v")
            candidate = values[0] if values else None
        elif len(segments) >= 2 and segments[0] in _PATH_KINDS:
            candidate = segments[1]
    if candidate is not None and VIDEO_ID.fullmatch(candidate):
        return candidate
    return None


def player_available() -> bool:
    """Whether the player can run here: Windows, with pywebview installed (looked up, not
    imported: it is only ever imported in the player's process)."""
    if sys.platform != "win32":
        return False
    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


def player_look() -> list[str]:
    """The player's options for its look: theme.py's dark halves as #rrggbb (a colour that
    can't be written so keeps the player's own default) and the texts in the app's language."""
    tokens: dict[str, theme.Color] = {
        "--background": theme.BG,
        "--button": theme.CLOSE_BUTTON,
        "--button-hover": theme.CLOSE_BUTTON_HOVER,
        "--glyph": theme.CLOSE_GLYPH,
        "--glyph-hover": theme.ON_COLOR,
    }
    options: list[str] = []
    for option, token in tokens.items():
        try:
            options += [option, palettes.to_hex(theme.pick(token, dark=True))]
        except ValueError:
            continue
    return [*options, "--tip", t(CLOSE_TIP), "--title", APP_NAME, "--language", current_language().value]


def player_command(video_id: str, python: str, look: Sequence[str] = ()) -> list[str]:
    """The player's command line ("--" first: a video id may start with "-")."""
    return [python, "-m", PLAYER_MODULE, *look, "--", video_id]


def exit_text(code: int | None) -> str:
    """A player's exit code for the log ("exit code 4: no WebView2")."""
    if code is None:
        return "no answer"
    meaning = EXIT_CODES.get(code)
    return f"exit code {code}: {meaning}" if meaning else f"exit code {code}"


# -- the player's process ------------------------------------------------------------------------


class PlayerInput(Protocol):
    def close(self) -> None: ...


class PlayerOutput(Protocol):
    async def readline(self) -> bytes: ...

    async def read(self, n: int = -1) -> bytes: ...


class PlayerProcess(Protocol):
    """What TrailerPlayer uses of an asyncio subprocess (tests pass fakes)."""

    @property
    def stdin(self) -> PlayerInput | None: ...

    @property
    def stdout(self) -> PlayerOutput | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


Starter = Callable[[Sequence[str], Path], Awaitable[PlayerProcess]]


async def start_process(command: Sequence[str], cwd: Path) -> PlayerProcess:
    """The player's process: stdin and stdout piped (its "ready" line, its signal to close),
    its stderr nowhere, no console window."""
    return await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


class TrailerPlayer:
    """One trailer window at a time (see the module docstring)."""

    def __init__(
        self,
        *,
        starter: Starter = start_process,
        opener: Callable[[str], object] = webbrowser.open,
        available: Callable[[], bool] = player_available,
        look: Callable[[], Sequence[str]] = player_look,
        python: str | None = None,
        cwd: Path = APP_ROOT,
        ready_timeout: float = READY_TIMEOUT_S,
    ) -> None:
        self._starter = starter
        self._opener = opener
        self._available = available
        self._look = look
        self._python = python or sys.executable
        self._cwd = cwd
        self._ready_timeout = ready_timeout
        self._task: asyncio.Task[None] | None = None
        self._process: PlayerProcess | None = None
        self._notify: _Notifier | None = None

    @property
    def process(self) -> PlayerProcess | None:
        """The player starting or showing now."""
        return self._process

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The task watching it (done once it has ended)."""
        return self._task

    def open(self, url: str, on_state: StateListener | None = None) -> None:
        """Play `url` (closing the trailer showing), or open it in the browser; `on_state`
        hears how it goes (see the module docstring)."""
        self.close()
        notify = _Notifier(on_state)
        video_id = youtube_id(url)
        if video_id is None:
            log.info("The trailer isn't on YouTube; opening it in the browser")
            self._browser(url)
            notify(FAILED)
            return
        if not self._available():
            log.info("The trailer player isn't available (pywebview); opening the browser")
            self._browser(url)
            notify(FAILED)
            return
        command = player_command(video_id, self._python, self._look())
        notify(OPENING)
        self._notify = notify
        self._task = spawn(self._play(url, command, notify), name="trailer")

    def close(self) -> None:
        """Close the trailer showing, if any (the player closes its window and ends). Its
        listener hears CLOSED now, before anything a next trailer tells its own."""
        task, self._task = self._task, None
        notify, self._notify = self._notify, None
        if task is not None and not task.done():
            if notify is not None:
                notify(CLOSED)
            task.cancel()

    async def _play(self, url: str, command: Sequence[str], notify: _Notifier) -> None:
        try:
            try:
                process = await self._starter(command, self._cwd)
            except (OSError, ValueError) as exc:
                log.warning(
                    "Couldn't start the trailer player (%s); opening the browser instead",
                    type(exc).__name__,
                )
                self._browser(url)
                notify(FAILED)
                return
            await self._watch(process, url, notify)
        finally:
            notify(CLOSED)  # whatever ended it (nothing when it already said how it ended)

    async def _watch(self, process: PlayerProcess, url: str, notify: _Notifier) -> None:
        self._process = process
        try:
            if await self._ready(process):
                log.info("Playing the trailer in AirGrabber's player window")
                notify(PLAYING)
                await _until_ended(process)
                if process.returncode == EXIT_IN_BROWSER:
                    log.info("The trailer player handed the trailer to the browser")
                    notify(FAILED)
                return
            code = await _exit_code(process)
            log.warning(
                "The trailer player didn't start (%s); opening the browser instead", exit_text(code)
            )
            self._browser(url)
            notify(FAILED)
        except asyncio.CancelledError:
            _close_input(process)  # it closes its window and ends by itself
            raise
        finally:
            if self._process is process:
                self._process = None

    async def _ready(self, process: PlayerProcess) -> bool:
        """Whether the player says "ready" (its page loaded) in time; ended if it doesn't."""
        stdout = process.stdout
        if stdout is None:
            return False
        try:
            line = await asyncio.wait_for(stdout.readline(), self._ready_timeout)
        except TimeoutError:
            _kill(process)
            return False
        return line.strip() == READY_LINE

    def _browser(self, url: str) -> None:
        try:
            self._opener(url)
        except Exception as exc:  # webbrowser rarely fails; never take the window with it
            log.warning("Couldn't open the browser (%s)", type(exc).__name__)


class _Notifier:
    """One trailer's `on_state`: CLOSED or FAILED ends it (later calls do nothing); a listener
    that raises is logged."""

    def __init__(self, listener: StateListener | None) -> None:
        self._listener = listener
        self._ended = False

    def __call__(self, state: TrailerState) -> None:
        if self._ended or self._listener is None:
            return
        self._ended = state in (CLOSED, FAILED)
        try:
            self._listener(state)
        except Exception:
            log.exception("A trailer state listener failed")


async def _until_ended(process: PlayerProcess) -> None:
    """Wait for the player to end, reading what it might still write so it never blocks."""
    stdout = process.stdout
    if stdout is not None:
        while await stdout.read(4096):
            pass
    await process.wait()


async def _exit_code(process: PlayerProcess) -> int | None:
    """A player that didn't come up: its exit code once it has ended (ended after
    END_TIMEOUT_S; None then)."""
    try:
        return await asyncio.wait_for(process.wait(), END_TIMEOUT_S)
    except TimeoutError:
        _kill(process)
        return None


def _close_input(process: PlayerProcess) -> None:
    stdin = process.stdin
    if stdin is not None:
        with contextlib.suppress(OSError, RuntimeError):
            stdin.close()


def _kill(process: PlayerProcess) -> None:
    with contextlib.suppress(OSError):  # ProcessLookupError: it has just ended
        process.kill()


_player: TrailerPlayer | None = None


def open_trailer(url: str, on_state: StateListener | None = None) -> None:
    """Play a trailer in AirGrabber's player window (the browser when it can't); the one
    showing closes. `on_state` hears OPENING / PLAYING / CLOSED / FAILED on the loop (it may
    outlive its widget: check `winfo_exists()`). From a Tk callback: the loop is running."""
    global _player
    if _player is None:
        _player = TrailerPlayer()
    _player.open(url, on_state)


def close_trailer() -> None:
    """Close the trailer window showing, if any."""
    if _player is not None:
        _player.close()
