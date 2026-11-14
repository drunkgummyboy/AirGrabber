"""AirGrabber's own video player, in a process of its own: `python -m app.media_player [look]`.

The app (app/ui/media_player.py) starts it on ▶ Play and keeps it for later videos. The player
is a view only: it reads JSON lines on stdin (app/media_protocol.py: `Play`, `LoadSubtitle`,
`Notice`) and writes event lines on stdout (`Ready` once the window and the engine are up, then
`Position`, `Stopped`, `Ended`, `WantNext`, `FindSubtitles`, `Tracks`, `AlwaysPick`). Plex, the
network and AirGrabber's settings stay in the app; nothing secret ever reaches this process.
When stdin ends (the app closing it, or the app ending) the window closes.

The look comes on the command line, as for the trailer player (`parse_options`): the colours
as #rrggbb (theme.py's dark halves; anything else keeps the default), every text already in the
app's language (`TEXTS`' names as options: `text_arguments(t)` builds them), and `--engine`,
the VLC folder holding libvlc.dll (app/services/vlc_engine.py's `Engine.folder`).

The window (app/media_player_window.py, customtkinter) shows the video through libVLC
(`VlcEngine`, python-vlc, imported in `main` only) behind `EngineSeam`, which the tests fake.
This process may use threads, and does: the stdin reader (`read_input`) and the engine's own
thread (`EngineWorker`: every libVLC call, so a call that hangs on a stalled share never blocks
Tk) hand their news to Tk only through one `queue.Queue` the window polls; libVLC's end event
does the same. Automatic garbage collection is off, Tk's thread collects on a timer
(app/ui/gc_guard.py, CLAUDE.md's D6), so a cycle holding a Tk object is never freed on the
other threads. Stdlib only at import time (the app and the tests import this module).
Never printed or logged: a path, a file name, a title.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias

from app.i18n import N_
from app.media_player_logic import Track
from app.media_protocol import Message, decode

EXIT_CLOSED = 0  # shown, then closed
EXIT_NO_ENGINE = 2  # libVLC (python-vlc, libvlc.dll) didn't start
EXIT_USAGE = 3  # bad arguments
EXIT_FAILED = 4  # the window failed otherwise
EXIT_CODES = {
    EXIT_CLOSED: "closed",
    EXIT_NO_ENGINE: "the engine didn't start",
    EXIT_USAGE: "bad arguments",
    EXIT_FAILED: "failed",
}

SETTINGS_FILE = "media-player.json"  # in the data folder: {"geometry": [x, y, w, h], "on_top": …}
POLL_S = 0.25  # how often the engine thread asks libVLC where it is
RELEASE_WAIT_S = 0.5  # how long a closed window waits for libVLC to let go (then the exit)

_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# The texts the window shows, by option name: the English here, the app's language on the
# command line (`text_arguments`). Placeholders are filled by name (`Look.text`).
TEXTS: Mapping[str, str] = MappingProxyType(
    {
        "close_tip": N_("Close (Esc)"),
        "play_tip": N_("Play (Space)"),
        "pause_tip": N_("Pause (Space)"),
        "back_tip": N_("Back 10 seconds (←)"),
        "forward_tip": N_("Forward 30 seconds (→)"),
        "mute_tip": N_("Mute (M)"),
        "unmute_tip": N_("Sound on (M)"),
        "full_screen_tip": N_("Full screen (F)"),
        "window_tip": N_("Leave full screen (Esc)"),
        "on_top_tip": N_("Stay on top"),
        "mini_tip": N_("Mini player (Ctrl+H)"),
        "back_to_window_tip": N_("Back to the window (Ctrl+H)"),
        "close_mini_tip": N_("Close"),
        "audio": N_("Audio"),
        "subtitles": N_("Subtitles"),
        "subtitles_off": N_("Off"),
        "find_subtitles": N_("Find subtitles…"),
        "looking": N_("Looking for subtitles…"),
        "always_pick": N_("Always pick: {language}"),
        "video_default": N_("The video's own"),
        "track": N_("Track {number}"),
        "resume": N_("Resume from {time}"),
        "start_over": N_("Start over"),
        "skip_intro": N_("Skip intro"),
        "next_episode": N_("Next episode {label} in {n}…"),
        "play_now": N_("Play now"),
        "cancel": N_("Cancel"),
        "dutch": N_("Dutch"),
        "english": N_("English"),
        "german": N_("German"),
        "french": N_("French"),
        "spanish": N_("Spanish"),
    }
)

# Always pick's and Find subtitles' languages: (code, the TEXTS name of the language's name).
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("nl", "dutch"),
    ("en", "english"),
    ("de", "german"),
    ("fr", "french"),
    ("es", "spanish"),
)


@dataclass(frozen=True, slots=True)
class Look:
    """The window's colours (#rrggbb: theme.py's dark halves, from the app) and texts (already
    in the app's language, by TEXTS' names)."""

    background: str = "#10161C"  # theme.BG: the frame around the video
    surface: str = "#1D242B"  # theme.SURFACE: the control bands and cards
    accent: str = "#4E6A85"  # theme.ACCENT: the seek bar, the main buttons
    accent_hover: str = "#427839"  # theme.ACCENT_HOVER
    foreground: str = "#FFFFFF"  # theme.TEXT
    muted: str = "#A4B2C6"  # theme.TEXT_MUTED
    title: str = "AirGrabber"  # the window's title before the first video
    texts: Mapping[str, str] = field(default_factory=lambda: dict(TEXTS))

    def text(self, name: str, **values: object) -> str:
        """The text `name` with its placeholders filled ("{time}" → values["time"]); braces
        that name no value stay as they are."""
        template = self.texts.get(name, TEXTS.get(name, name))

        def fill(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(values[key]) if key in values else match.group(0)

        return _PLACEHOLDER.sub(fill, template)


_COLOUR_OPTIONS = ("background", "surface", "accent", "accent_hover", "foreground", "muted")


@dataclass(frozen=True, slots=True)
class Options:
    look: Look
    engine_folder: str | None


def _option(name: str) -> str:
    return "--" + name.replace("_", "-")


def text_arguments(translate: Callable[[str], str]) -> list[str]:
    """The texts' options in the app's language (`translate`: the app's `t`)."""
    arguments: list[str] = []
    for name, english in TEXTS.items():
        arguments += [_option(name), translate(english)]
    return arguments


def parse_options(argv: Sequence[str]) -> Options | None:
    """The command line; None when it isn't usable."""
    parser = argparse.ArgumentParser(prog="media_player", add_help=False, exit_on_error=False)
    defaults = Look()
    for name in (*_COLOUR_OPTIONS, "title"):
        parser.add_argument(_option(name), dest=name, default=getattr(defaults, name))
    for name in TEXTS:
        parser.add_argument(_option(name), dest="text_" + name, default=None)
    parser.add_argument("--engine", dest="engine", default=None)
    try:
        namespace, _unknown = parser.parse_known_args(list(argv))
    except (argparse.ArgumentError, SystemExit):
        return None
    colours: dict[str, str] = {}
    for name in _COLOUR_OPTIONS:
        value = str(getattr(namespace, name))
        colours[name] = value if _HEX.fullmatch(value) else getattr(defaults, name)
    texts = dict(TEXTS)
    for name in TEXTS:
        value = getattr(namespace, "text_" + name)
        if isinstance(value, str) and value.strip():
            texts[name] = value
    look = Look(**colours, title=str(namespace.title), texts=texts)
    engine = namespace.engine if isinstance(namespace.engine, str) and namespace.engine else None
    return Options(look=look, engine_folder=engine)


# -- the engine ----------------------------------------------------------------------------------


class EngineSeam(Protocol):
    """What the window asks of libVLC (`VlcEngine`; the tests' FakeEngine). Called only on the
    engine's own thread (`EngineWorker`), except `on_end`, once, before the first video."""

    def load(self, path: str) -> None:
        """Make `path` the video (not playing yet; a `seek` before `play` is where it starts)."""
        ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def seek(self, ms: int) -> None: ...

    def position_ms(self) -> int:
        """Where it is (may block on a stalled share)."""
        ...

    def duration_ms(self) -> int:
        """How long it is (0 while unknown)."""
        ...

    def audio_tracks(self) -> list[Track]: ...

    def subtitle_tracks(self) -> list[Track]:
        """The subtitle tracks (not "off": that is SUBTITLES_OFF)."""
        ...

    def set_audio(self, track_id: int) -> None: ...

    def set_subtitle(self, track_id: int) -> None:
        """Show subtitle track `track_id` (SUBTITLES_OFF: none)."""
        ...

    def add_subtitle(self, path: str) -> None:
        """Load a subtitle file and show it."""
        ...

    def volume(self, level: int) -> None:
        """0–100."""
        ...

    def mute(self, on: bool) -> None: ...

    def attach(self, hwnd: int) -> None:
        """Draw the video into window `hwnd` (Tk's `winfo_id()`)."""
        ...

    def release(self) -> None: ...

    def on_end(self, callback: Callable[[], None]) -> None:
        """Call `callback` (on libVLC's own thread) when a video plays to its end."""
        ...


class VlcEngine:
    """EngineSeam on python-vlc. A thin adapter: everything the player decides lives in the
    window. `start` raises when the binding or libvlc.dll can't be loaded."""

    def __init__(self, vlc: Any, instance: Any) -> None:
        self._vlc = vlc
        self._instance = instance
        self._player = instance.media_player_new()
        self._media: Any = None
        self._path = ""
        self._started = False

    @classmethod
    def start(cls, folder: str | None) -> VlcEngine:
        """libVLC from `folder` (the VLC folder holding libvlc.dll; None: python-vlc looks)."""
        arguments = ["--no-video-title-show", "--quiet"]
        if folder:
            os.environ["PYTHON_VLC_LIB_PATH"] = str(Path(folder) / "libvlc.dll")
            os.environ["PYTHON_VLC_MODULE_PATH"] = str(Path(folder) / "plugins")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(folder)
            arguments.append("--plugin-path=" + str(Path(folder) / "plugins"))
        try:
            import vlc  # optional (requirements.txt), in this process only

            instance = vlc.Instance(*arguments)
        except BaseException as exc:  # python-vlc's find_lib may sys.exit(1) without a libvlc
            raise OSError("libVLC didn't start") from exc
        if instance is None:
            raise OSError("libVLC didn't start")
        return cls(vlc, instance)

    def load(self, path: str) -> None:
        self._path = path
        self._set_media(path)
        self._started = False

    def _set_media(self, path: str) -> None:
        """A fresh media for `path` (no start time on it), the one before released."""
        previous = self._media
        self._media = self._instance.media_new_path(path)
        self._player.set_media(self._media)
        if previous is not None:
            previous.release()

    def play(self) -> None:
        if self._player.get_state() == self._vlc.State.Ended and self._path:
            # Played to the end: again from the beginning, so a fresh media without the
            # `:start-time` the first start may have had (libVLC plays an ended one only anew).
            self._player.stop()
            self._set_media(self._path)
        self._player.play()
        self._started = True

    def pause(self) -> None:
        self._player.set_pause(1)

    def seek(self, ms: int) -> None:
        if not self._started and self._media is not None:
            # Not playing yet: libVLC can't seek, so the video starts there instead.
            self._media.add_option(f":start-time={max(0, ms) / 1000:.3f}")
            return
        self._player.set_time(max(0, ms))

    def position_ms(self) -> int:
        return max(0, int(self._player.get_time()))

    def duration_ms(self) -> int:
        return max(0, int(self._player.get_length()))

    def audio_tracks(self) -> list[Track]:
        return self._tracks(self._player.audio_get_track_description())

    def subtitle_tracks(self) -> list[Track]:
        return self._tracks(self._player.video_get_spu_description())

    def set_audio(self, track_id: int) -> None:
        self._player.audio_set_track(track_id)

    def set_subtitle(self, track_id: int) -> None:
        self._player.video_set_spu(track_id)

    def add_subtitle(self, path: str) -> None:
        uri = Path(path).as_uri()
        self._player.add_slave(self._vlc.MediaSlaveType.subtitle, uri, True)

    def volume(self, level: int) -> None:
        self._player.audio_set_volume(max(0, min(100, level)))

    def mute(self, on: bool) -> None:
        self._player.audio_set_mute(on)

    def attach(self, hwnd: int) -> None:
        self._player.set_hwnd(hwnd)
        # The window gets the mouse (double-click, the pointer that shows the controls) and
        # the keys, not libVLC's own handling.
        self._player.video_set_mouse_input(False)
        self._player.video_set_key_input(False)

    def release(self) -> None:
        self._player.stop()
        self._player.release()
        self._instance.release()

    def on_end(self, callback: Callable[[], None]) -> None:
        events = self._player.event_manager()
        events.event_attach(self._vlc.EventType.MediaPlayerEndReached, lambda _event: callback())

    @staticmethod
    def _tracks(described: Any) -> list[Track]:
        """libVLC's (id, name) list without its "Disable" entry (id -1)."""
        tracks: list[Track] = []
        for track_id, name in described or ():
            if int(track_id) < 0:
                continue
            text = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
            tracks.append(Track(int(track_id), _language_in(text), text))
        return tracks


_BRACKET = re.compile(r"\[([^\[\]]+)\]\s*$")


def _language_in(name: str) -> str:
    """libVLC's track names end in "[English]": that part, else ""."""
    match = _BRACKET.search(name)
    return match.group(1) if match else ""


# -- what reaches Tk's thread -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Incoming:
    """A message from the app."""

    message: Message


@dataclass(frozen=True, slots=True)
class InputEnded:
    """Stdin reached its end: the app closed this player, or ended."""


@dataclass(frozen=True, slots=True)
class Tick:
    """Where video `generation` is; `done`: how many engine jobs had run by then."""

    generation: int
    ms: int
    duration_ms: int
    done: int


@dataclass(frozen=True, slots=True)
class TrackLists:
    generation: int
    audio: tuple[Track, ...]
    subtitles: tuple[Track, ...]


@dataclass(frozen=True, slots=True)
class VideoEnded:
    generation: int


Event: TypeAlias = Incoming | InputEnded | Tick | TrackLists | VideoEnded


def read_input(stdin: Any, events: queue.Queue[Event]) -> None:
    """The stdin reader (a thread of its own): each message to Tk, then InputEnded."""
    try:
        while True:
            raw = stdin.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            message = decode(text)
            if message is not None:
                events.put(Incoming(message))
    except (OSError, ValueError):
        pass  # the pipe broke: as good as its end
    events.put(InputEnded())


Job = Callable[["EngineSeam"], None]


class EngineWorker:
    """The engine's own thread: runs the window's jobs in order, and every POLL_S while a
    video is on asks where it is (`Tick`) and which tracks it has (`TrackLists`, on a change).
    A libVLC call that hangs holds up only this thread; the window stays responsive."""

    def __init__(
        self, engine: EngineSeam, events: queue.Queue[Event], *, poll_s: float = POLL_S
    ) -> None:
        self.engine = engine
        self._events = events
        self._poll_s = poll_s
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._submitted = 0
        self._done = 0
        self._generation = 0  # the video being watched; 0: none
        self._lists: tuple[tuple[Track, ...], tuple[Track, ...]] | None = None
        self._thread = threading.Thread(target=self._run, name="media-engine", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, job: Job) -> int:
        """Run `job(engine)` on the engine's thread; its number (Tick.done reaches it once run)."""
        self._submitted += 1
        self._jobs.put(job)
        return self._submitted

    def watch(self, generation: int) -> None:
        """From the next poll on, report video `generation` (0: nothing)."""

        def job(_engine: EngineSeam) -> None:
            self._generation = generation
            self._lists = None

        self.submit(job)

    @property
    def generation(self) -> int:
        return self._generation

    def stop(self) -> None:
        """Release the engine (after the jobs before) and end the thread."""
        self._jobs.put(_RELEASE)
        self._jobs.put(None)

    def join(self, timeout: float) -> bool:
        """Wait at most `timeout` for the thread to end; whether it did."""
        if not self._thread.is_alive():
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        last = 0.0
        while True:
            wait = max(0.0, last + self._poll_s - time.monotonic())
            try:
                job = self._jobs.get(timeout=wait)
            except queue.Empty:
                job = _POLL
            if job is None:
                return
            if job is not _POLL:
                try:
                    job(self.engine)
                except Exception:
                    pass  # libVLC refused: the window carries on
                if job is not _RELEASE:
                    self._done += 1
            if time.monotonic() - last >= self._poll_s:
                last = time.monotonic()
                self._poll()

    def _poll(self) -> None:
        generation = self._generation
        if not generation:
            return
        try:
            ms = self.engine.position_ms()
            duration = self.engine.duration_ms()
            lists = (tuple(self.engine.audio_tracks()), tuple(self.engine.subtitle_tracks()))
        except Exception:
            return
        self._events.put(Tick(generation, ms, duration, self._done))
        if lists != self._lists:
            self._lists = lists
            self._events.put(TrackLists(generation, *lists))


def _poll_marker(_engine: EngineSeam) -> None:
    pass


def _release(engine: EngineSeam) -> None:
    engine.release()


_POLL: Job = _poll_marker
_RELEASE: Job = _release


# -- the process --------------------------------------------------------------------------------


Rect = tuple[int, int, int, int]  # x, y, width, height (physical pixels)


def work_area_under_pointer() -> Rect:
    """The work area of the monitor under the pointer (x, y, width, height)."""
    from app.trailer_player import _work_area_under_pointer

    (left, top, right, bottom), _scale = _work_area_under_pointer()
    return left, top, right - left, bottom - top


def on_screen(rect: Rect) -> bool:
    """Whether the middle of `rect` is on a monitor (a remembered place on one that's gone isn't)."""
    if sys.platform != "win32":
        return True
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        x, y, width, height = rect
        point = wintypes.POINT(x + width // 2, y + height // 2)
        return bool(user32.MonitorFromPoint(point, 0))  # MONITOR_DEFAULTTONULL
    except (OSError, AttributeError):
        return True


def settings_path() -> Path:
    from app.config import AppPaths

    return AppPaths.default().root / SETTINGS_FILE


def dpi_aware() -> None:
    """Windows' scaling known before Tk starts: Tk keeps the screen's size from its start, and
    customtkinter asks for this only after Tk has started. Without it Tk's screen is the scaled-down
    one (1707×1067 for 2560×1600 at 150 %) and full screen covers only that much of it."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per monitor, as customtkinter's
        except (AttributeError, OSError):
            pass


def _quiet_output() -> Callable[[str], None]:
    """Keep stdout's pipe for the event lines only (libVLC's and Tk's prints go nowhere); the
    function that writes one line there."""
    try:
        fd: int | None = os.dup(1)
    except OSError:
        fd = None
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    except OSError:
        pass
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    lock = threading.Lock()

    def send(line: str) -> None:
        nonlocal fd
        with lock:
            if fd is None:
                return
            try:
                os.write(fd, line.encode("ascii", "replace") + b"\n")
            except OSError:
                fd = None

    return send


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_options(sys.argv[1:] if argv is None else argv)
    if options is None:
        return EXIT_USAGE
    send = _quiet_output()
    try:
        engine = VlcEngine.start(options.engine_folder)
    except Exception:
        return EXIT_NO_ENGINE
    from app.identity import set_process_app_id

    set_process_app_id()  # AirGrabber's taskbar group, not Python's
    try:
        import customtkinter as ctk

        from app.media_player_window import run_player
        from app.models import ThemeMode
        from app.ui import theme
        from app.ui.branding import set_window_icon

        theme.apply_appearance(ThemeMode.DARK)  # the player is always dark
        dpi_aware()
        root = ctk.CTk()
        root.withdraw()
        # D6 (app/ui/gc_guard.py): the stdin reader's and the engine's threads must never run
        # the cycle collector and free a Tk object there; Tk's thread collects on a timer.
        from app.ui import gc_guard

        gc_guard.start(root)
        set_window_icon(root)
        code = run_player(
            root,
            engine,
            options.look,
            stdin=sys.stdin.buffer,
            send=send,
            settings_path=settings_path(),
            work_area=work_area_under_pointer,
            on_screen=on_screen,
        )
    except Exception:
        return EXIT_FAILED
    return code


def load_settings(path: Path) -> dict[str, Any]:
    """The remembered settings ({} when missing or unreadable). Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path: Path, data: Mapping[str, Any]) -> None:
    """Write the settings (beside, then over the old file). Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        spare = path.with_name(path.name + ".tmp")
        spare.write_text(json.dumps(dict(data)), encoding="utf-8")
        os.replace(spare, path)
    except OSError:
        pass


if __name__ == "__main__":
    # Straight out once the window is gone: a libVLC stuck on a stalled share would otherwise
    # keep the process (its threads) alive.
    os._exit(main())
