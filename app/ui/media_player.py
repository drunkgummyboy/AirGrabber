"""▶ Play in AirGrabber's own player: the app's side (MediaPlayerHost, one per app).

The player (app/media_player.py) is a process of its own and only a view; everything that needs
Plex, the network or AirGrabber's settings happens here, on the loop
(docs/player/2026-11-13-pc-player-design.md):

- `play(title, video, episodes=…)` finds the file from this PC (`PlaybackService.locate`), then
  sends it to the player's window: the process is started once (`python -m app.media_player
  <look> --engine <VLC folder>`, the interpreter running AirGrabber, without a console, from the
  folder holding `app/`, like app/ui/trailer.py) and kept for later videos; it must say `Ready`
  within READY_TIMEOUT_S. Its `Play` line holds the local file, the texts, where to start (Plex's
  position; Resume / Start over asked when there is one, except for a next episode), Plex's intro
  and credits markers (`PlexService.markers`; none when that fails), the next episode's label,
  Settings' Always pick and the languages picked by hand in the episode before (`Tracks`, kept
  for the same show in the same window).
- Plex in sync: `Position` → Plex's timeline (`PlexService.report`) on a new state (playing /
  paused), a seek (a position SEEK_SLACK_MS away from where it would be) and otherwise once per
  REPORT_EVERY_S; the first position at WATCHED_SHARE of the length marks it watched once;
  `Ended` and `Stopped` (the player's, when a video is closed or replaced) report "stopped"
  (an `Ended` without a length: where it was last seen, not watched). A player that ends
  without a `Stopped` (a crash) has Plex told "stopped" where it was last seen. A PlexError
  there says so once per video on the status bar and the video plays on. The calls go one at a
  time, in order; a video's position report still waiting for its turn is replaced by a newer
  one (seeks held down send the newest place, not each).
- `WantNext` marks the video watched and plays the next episode of `episodes` (same show, the
  specials only after a special) in the same window, where Plex left it, without asking; after
  the last one nothing happens. The window closed while that one is still being found: nothing
  opens.
- `play_file(path)`: a video from Explorer's "Open with" (main.py --play, through the
  single-instance mailbox; docs/player/2026-11-14-open-with-design.md). It never reaches Plex:
  no markers, timeline or watched mark. Where it was left is kept on this PC (`places`,
  app/services/local_files.py's LocalPlaces: Resume / Start over asked), the next file of its
  folder by SxxEyy plays on WantNext, hand-picked languages carry on within the folder, and its
  menu has no Find subtitles. "Play in: VLC" doesn't apply (the user chose this player).
  `on_idle` says no player window is open any more (a start for such a file then leaves).
- `FindSubtitles` asks MediaForge's subtitle service (`forge_subtitles`) and sends `LoadSubtitle`
  with the file it saved beside the video, else a `Notice` in plain words. `AlwaysPick` is stored
  in Settings (`always_audio` / `always_subtitles`).
- Falling back (3.14.1's ▶ Play: `PlaybackService.launch`, VLC or Windows' player): when Settings'
  Play in says VLC, when this PC has no engine the player can use (app/services/vlc_engine.py,
  looked up once per run in a thread), or when the player doesn't start or say `Ready` in time
  (then for the rest of the run too: no 10 s wait on every ▶ Play). The status bar says why
  once per run.

`close()` closes the player's window (its stdin is closed; the player's own `Stopped` reports the
place). `aclose()`, on quitting, reports "stopped" at the last known place itself, then closes it
and waits up to QUIT_EXIT_TIMEOUT_S for the player to end (it saves its window's size and place
when its stdin ends; the loop's tasks cancelled after quitting would end it mid-way), ending it
after that.
The Plex token never reaches the player: not its command line, not a line on its stdin. Logs give
ids, counts and outcome names: never a path, file name, title or URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app import APP_NAME
from app.i18n import N_, t
from app.media_player import EXIT_CODES, text_arguments
from app.media_protocol import (
    AlwaysPick,
    Ended,
    FindSubtitles,
    LoadSubtitle,
    Message,
    Notice,
    Play,
    Position,
    Ready,
    Stopped,
    Tracks,
    WantNext,
    decode,
    encode,
)
from app.models import PlayIn, Settings
from app.models.plex import PlexTitle, PlexVideo
from app.services.local_files import episode_code as file_code
from app.services.local_files import next_file, video_names
from app.services.playback import Located, PlayOutcome, episode_name
from app.services.plex import Markers, PlayState, PlexError
from app.services.vlc_engine import Engine, EngineProblem, engine_problem, find_engine
from app.ui import palettes, theme
from app.ui.tasks import spawn

if TYPE_CHECKING:  # Forge's types only: nothing of Forge is imported at the start
    from app.forge.models.subtitles import SubtitleFetch, SubtitleIdentity
    from app.forge.runtime import ForgeRuntime

log = logging.getLogger(__name__)

PLAYER_MODULE = "app.media_player"
APP_ROOT = Path(__file__).resolve().parents[2]  # the folder holding app/ (`-m` runs from there)
READY_TIMEOUT_S = 10.0  # the player's window and engine are up by then, or VLC instead
END_TIMEOUT_S = 5.0  # a player that didn't come up: this long to learn its exit code
MARKERS_TIMEOUT_S = 5.0  # Plex's markers by then, or none (the video doesn't wait longer)
QUIT_REPORT_TIMEOUT_S = 4.0  # quitting: the last "stopped" report by then, or not at all
QUIT_EXIT_TIMEOUT_S = 3.0  # quitting: the player saves its window's place and ends by then, or is
# ended (both within forge_link's PLAYER_CLOSE_S, 8 s)
REPORT_EVERY_S = 10.0  # a position to Plex this often while nothing else changes
REPORT_SLACK_S = 0.5  # the player's own 10 s, measured by its clock, still counts
SEEK_SLACK_MS = 3000  # a position this far from where it would be is a seek
WATCHED_SHARE = 0.9  # marked watched in Plex from here
SUBTITLE_SUFFIXES = frozenset({".srt", ".ass", ".ssa", ".sub", ".vtt"})
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PLAYING_TEXT = N_("▶ Playing {name}.")
PLEX_AWAY_TEXT = N_("Plex didn't answer, so your place isn't saved there right now.")
NO_ENGINE_VLC_TEXT = N_("AirGrabber's player needs VLC installed (64-bit), so this opens in VLC.")
NO_ENGINE_DEFAULT_TEXT = N_(
    "AirGrabber's player needs VLC installed (64-bit), so this opens in your video player."
)
NOT_STARTED_VLC_TEXT = N_("AirGrabber's player didn't start, so this opens in VLC.")
NOT_STARTED_DEFAULT_TEXT = N_(
    "AirGrabber's player didn't start, so this opens in your video player."
)
FORGE_OFF_TEXT = N_("MediaForge is off, so subtitles can't be found.")
SUBTITLES_NOT_FOUND_TEXT = N_("OpenSubtitles has no subtitles in that language for this video.")
SUBTITLES_NO_VIDEO_TEXT = N_("The video isn't there any more, so no subtitles were saved.")
SUBTITLES_UNAVAILABLE_TEXT = N_("Finding subtitles isn't available in this version.")
SUBTITLES_FAILED_TEXT = N_("The subtitles couldn't be fetched.")
FILE_GONE_TEXT = N_("That video isn't there any more.")
FOLDER_TIMEOUT_S = 10.0  # a file from Explorer: its folder's listing (the next file) by then

Fallback = Literal["no_engine", "not_started"]
_WHY: dict[tuple[Fallback, str], str] = {
    ("no_engine", "vlc"): NO_ENGINE_VLC_TEXT,
    ("no_engine", "default"): NO_ENGINE_DEFAULT_TEXT,
    ("not_started", "vlc"): NOT_STARTED_VLC_TEXT,
    ("not_started", "default"): NOT_STARTED_DEFAULT_TEXT,
}


# -- what the host uses (the services; tests pass fakes or the real ones on fakes) ----------------


class Locator(Protocol):
    """PlaybackService: where a video's file is from this PC, and 3.14.1's ▶ Play."""

    async def locate(self, title: PlexTitle, video: PlexVideo) -> Located | PlayOutcome: ...

    async def launch(self, located: Located) -> PlayOutcome: ...


class PlexPlaying(Protocol):
    """PlexService's calls for playing (each raises PlexError)."""

    async def markers(self, key: str) -> Markers: ...

    async def report(
        self, key: str, state: PlayState, position_ms: int, duration_ms: int
    ) -> None: ...

    async def mark_watched(self, key: str) -> None: ...


class SettingsStore(Protocol):
    """SettingsService."""

    @property
    def current(self) -> Settings: ...

    async def update(self, **changes: Any) -> Settings: ...


class Places(Protocol):
    """LocalPlaces (app/services/local_files.py): where a file from Explorer was left."""

    def place(self, path: str) -> int: ...

    def remember(self, path: str, ms: int, duration_ms: int) -> None: ...


class SubtitleFinder(Protocol):
    """MediaForge's subtitle service (`ForgeServices.subtitles`)."""

    async def fetch(
        self,
        video: Path,
        identity: SubtitleIdentity,
        languages: Sequence[str] | None = None,
        *,
        run_id: int | None = None,
    ) -> SubtitleFetch: ...


SubtitleLookup = Callable[[], Awaitable[SubtitleFinder | None]]


def forge_subtitles(runtime: ForgeRuntime) -> SubtitleLookup:
    """MediaForge's subtitle service when asked (Forge started first when it hasn't yet); None
    while it is off."""

    async def current() -> SubtitleFinder | None:
        services = runtime.services or await runtime.start()
        return None if services is None else services.subtitles

    return current


# -- the player's process -------------------------------------------------------------------------


class PlayerInput(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class PlayerOutput(Protocol):
    async def readline(self) -> bytes: ...

    async def read(self, n: int = -1) -> bytes: ...


class PlayerProcess(Protocol):
    """What the host uses of an asyncio subprocess (tests pass fakes)."""

    @property
    def stdin(self) -> PlayerInput | None: ...

    @property
    def stdout(self) -> PlayerOutput | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


Starter = Callable[[Sequence[str], Path], Awaitable[PlayerProcess]]


async def start_player_process(command: Sequence[str], cwd: Path) -> PlayerProcess:
    """The player's process: stdin and stdout piped (the messages), its stderr nowhere, no
    console window. The one real start (tests/__init__.py refuses it)."""
    return await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def player_look() -> list[str]:
    """The player's options for its look: theme.py's dark halves as #rrggbb (a colour that
    can't be written so keeps the player's own default), its title and every text in the app's
    language."""
    tokens: dict[str, theme.Color] = {
        "--background": theme.BG,
        "--surface": theme.SURFACE,
        "--accent": theme.ACCENT,
        "--accent-hover": theme.ACCENT_HOVER,
        "--foreground": theme.TEXT,
        "--muted": theme.TEXT_MUTED,
    }
    options: list[str] = []
    for option, token in tokens.items():
        try:
            options += [option, palettes.to_hex(theme.pick(token, dark=True))]
        except ValueError:
            continue
    return [*options, "--title", APP_NAME, *text_arguments(t)]


def exit_text(code: int | None) -> str:
    """A player's exit code for the log ("exit code 2: the engine didn't start")."""
    if code is None:
        return "no answer"
    meaning = EXIT_CODES.get(code)
    return f"exit code {code}: {meaning}" if meaning else f"exit code {code}"


def subtitle_file(video: str, language: str) -> str | None:
    """The subtitle in `language` beside `video`: "<stem>.<lang>.srt" (what MediaForge saves),
    else a variant such as "<stem>.<lang>.forced.srt" / ".<lang>.ass"; None when there is none.
    Blocking: called through `asyncio.to_thread`."""
    path = Path(video)
    exact = path.with_name(f"{path.stem}.{language}.srt")
    if exact.is_file():
        return str(exact)
    prefix = f"{path.stem}.{language}.".casefold()
    try:
        names = sorted(os.listdir(path.parent))
    except OSError:
        return None
    for name in names:
        if name.casefold().startswith(prefix) and Path(name).suffix.casefold() in SUBTITLE_SUFFIXES:
            return str(path.parent / name)
    return None


def next_in_show(video: PlexVideo, episodes: Sequence[PlexVideo]) -> PlexVideo | None:
    """The episode after `video` in `episodes`' order: a regular one after a regular one, a
    special after a special; None after the last."""
    special = not video.season
    same = [one for one in episodes if (not one.season) == special]
    keys = [one.key for one in same]
    if video.key not in keys:
        return None
    index = keys.index(video.key) + 1
    return same[index] if index < len(same) else None


def episode_code(video: PlexVideo) -> str:
    """"S02E03" (the player's line and its countdown's label)."""
    return episode_name("", video)


# -- the host -------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Session:
    """One video sent to the player, as Plex hears about it."""

    title: PlexTitle
    video: PlexVideo
    path: str
    episodes: tuple[PlexVideo, ...]
    ms: int  # where it is, as far as the app knows
    duration_ms: int
    state: PlayState | None = None  # last reported to Plex
    reported_at: float | None = None
    seen_at: float | None = None  # the last Position: when, and its state
    seen_state: str | None = None
    ended: bool = False  # the player said Ended (and hasn't played it again)
    watched: bool = False  # marked watched (or tried)
    plex_said: bool = False  # the Plex-away line was shown for it
    done: bool = False  # its "stopped" was reported on quitting: nothing more for it
    # A position report waiting for its turn (state, ms, duration): a newer one replaces it.
    waiting: tuple[PlayState, int, int] | None = None
    local: bool = False  # a file from Explorer: no Plex, its place in `Places`
    following: str | None = None  # a local file's next one in its folder
    carry_key: str | None = None  # what hand-picked languages carry on within (a show, a folder)


@dataclass(eq=False)
class _Window:
    """A running player process and the videos sent to it: `sessions[0]` is the one it shows
    (as far as the app knows), any after it were sent but not reached yet."""

    process: PlayerProcess
    sessions: list[_Session] = field(default_factory=list)
    carry: tuple[str, str, str] | None = None  # (show key, audio, subtitles) picked by hand
    reader: asyncio.Task[None] | None = None
    closing: bool = False


class MediaPlayerHost:
    """▶ Play in AirGrabber's own player (see the module docstring). One per app."""

    def __init__(
        self,
        playback: Locator,
        plex: PlexPlaying,
        settings: SettingsStore,
        subtitles: SubtitleLookup,
        *,
        on_status: Callable[[str], None] | None = None,
        starter: Starter | None = None,
        find_engine: Callable[[], Engine | None] = find_engine,
        engine_problem: Callable[[], EngineProblem | None] = engine_problem,
        look: Callable[[], Sequence[str]] = player_look,
        python: str | None = None,
        cwd: Path = APP_ROOT,
        ready_timeout: float = READY_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        places: Places | None = None,
    ) -> None:
        """`starter`: None starts the real process (`start_player_process`); `find_engine` and
        `engine_problem` run in a thread, the first once per run. `on_status` hears the status
        bar's lines (it can be set later, too); `on_idle` that no player window is open any more
        (it closed, or a file from Explorer opened elsewhere or not at all). `places`: where
        files from Explorer were left (None: each starts at its beginning)."""
        self.on_status: Callable[[str], None] = on_status or (lambda _text: None)
        self.on_idle: Callable[[], None] = lambda: None
        self._places = places
        self._playback = playback
        self._plex = plex
        self._settings = settings
        self._subtitles = subtitles
        self._starter = starter
        self._find_engine = find_engine
        self._engine_problem = engine_problem
        self._look = look
        self._python = python or sys.executable
        self._cwd = cwd
        self._ready_timeout = ready_timeout
        self._clock = clock
        self._engine_known = False
        self._engine: Engine | None = None
        self._why_said = False
        self._window: _Window | None = None
        self._play_task: asyncio.Task[None] | None = None
        self._next_for: _Window | None = None  # the window a next episode's play is meant for
        self._start_failed = False  # the player didn't come up once: VLC for the rest of the run
        self._jobs: set[asyncio.Task[Any]] = set()
        self._plex_lock = asyncio.Lock()
        self._closed = False

    # -- what the app calls ------------------------------------------------------------------------

    def play(
        self, title: PlexTitle, video: PlexVideo, *, episodes: Sequence[PlexVideo] = ()
    ) -> None:
        """Play `video` of `title` (a film, or an episode of the show whose `episodes`, in order,
        give the next one); a play still looking for its file is dropped. From a Tk callback."""
        self._start_play(title, video, tuple(episodes), from_next=False)

    def play_file(self, path: str) -> None:
        """Play a video file from Explorer's "Open with" (main.py --play): no Plex; where it was
        left on this PC, the next file of its folder, no Find subtitles. From a Tk callback."""
        self._start_local(path, from_next=False)

    @property
    def open(self) -> bool:
        """A player window is open (or a play is on its way to one)."""
        window = self._window
        running = window is not None and not window.closing
        return running or (self._play_task is not None and not self._play_task.done())

    def close(self) -> None:
        """Close the player's window (its own Stopped tells Plex where it was)."""
        self._cancel_play()
        window, self._window = self._window, None
        if window is not None:
            window.closing = True
            _close_input(window.process)

    async def aclose(self) -> None:
        """AirGrabber quits: Plex hears "stopped" at the last known place, then the player's
        window closes and the player ends (ended after QUIT_EXIT_TIMEOUT_S). At most
        QUIT_REPORT_TIMEOUT_S + QUIT_EXIT_TIMEOUT_S. Call before the loop's tasks are cancelled."""
        if self._closed:
            return
        self._closed = True
        self._cancel_play()
        window, self._window = self._window, None
        if window is None:
            return
        window.closing = True
        for session in window.sessions:
            session.done = True
        head = window.sessions[0] if window.sessions else None
        if head is not None and head.local and not head.ended and head.seen_at is not None:
            self._keep_place(head, head.ms)
        elif head is not None and not head.ended and head.seen_at is not None:
            try:
                async with asyncio.timeout(QUIT_REPORT_TIMEOUT_S), self._plex_lock:
                    await self._plex.report(head.video.key, "stopped", head.ms, head.duration_ms)
                log.info("Player: Plex told %s stopped on quitting", head.video.key)
            except (PlexError, TimeoutError) as exc:
                log.info("Player: the last report didn't reach Plex (%s)", type(exc).__name__)
        _close_input(window.process)
        process = window.process
        try:
            code = await asyncio.wait_for(process.wait(), QUIT_EXIT_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "AirGrabber's player didn't end within %.0f s: ending it", QUIT_EXIT_TIMEOUT_S
            )
            _kill(process)
        else:
            log.info("AirGrabber's player ended on quitting (%s)", exit_text(code))

    async def settled(self) -> None:
        """Wait for the play under way and the Plex / subtitle / settings jobs (tests)."""
        while True:
            pending = [task for task in (self._play_task, *self._jobs) if task and not task.done()]
            if not pending:
                return
            await asyncio.wait(pending)

    # -- playing ---------------------------------------------------------------------------------

    def _start_play(
        self,
        title: PlexTitle,
        video: PlexVideo,
        episodes: tuple[PlexVideo, ...],
        *,
        from_next: bool,
        window: _Window | None = None,
    ) -> None:
        """`window`: the one a next episode is for (its end cancels the play: `_read`)."""
        if self._closed:
            return
        self._cancel_play()
        self._next_for = window if from_next else None
        self._play_task = spawn(
            self._play(title, video, episodes, from_next=from_next), name="media player"
        )

    def _cancel_play(self) -> None:
        task, self._play_task = self._play_task, None
        self._next_for = None
        if task is not None and not task.done():
            task.cancel()

    async def _play(
        self, title: PlexTitle, video: PlexVideo, episodes: tuple[PlexVideo, ...], *, from_next: bool
    ) -> None:
        found = await self._playback.locate(title, video)
        if isinstance(found, PlayOutcome):
            self._say(found.text)
            return
        if self._settings.current.play_in == PlayIn.VLC:
            await self._fall_back(found, None)
            return
        engine = await self._engine_here()
        if engine is None:
            await self._fall_back(found, "no_engine")
            return
        if self._start_failed and not self._running():
            await self._fall_back(found, "not_started")  # it didn't come up before this run
            return
        markers = self._job(self._markers(video.key), "player markers")
        try:
            window = await self._window_for(engine)
            if window is None:
                await self._fall_back(found, "not_started")
                return
            intro, credits_ms = await markers
        finally:
            markers.cancel()
        following = next_in_show(video, episodes) if title.kind == "show" else None
        carry = window.carry if window.carry and window.carry[0] == title.key else None
        settings = self._settings.current
        message = Play(
            file=found.path,
            title=title.title,
            line=_line(title, video),
            start_ms=max(0, video.view_offset_ms),
            ask_resume=video.view_offset_ms > 0 and not from_next,
            intro=intro,
            credits_ms=credits_ms,
            next_label=episode_code(following) if following is not None else None,
            always_audio=settings.always_audio,
            always_subtitles=settings.always_subtitles,
            carry_audio=carry[1] if carry else "",
            carry_subtitles=carry[2] if carry else "",
        )
        session = _Session(
            title=title,
            video=video,
            path=found.path,
            episodes=episodes if title.kind == "show" else (),
            ms=message.start_ms,
            duration_ms=video.duration_ms,
            carry_key=title.key if title.kind == "show" else None,
        )
        if window.sessions and window.sessions[-1].ended:
            window.sessions.pop()  # played to its end: the player says no Stopped for it
        window.sessions.append(session)
        if not await _send(window.process, message):
            window.sessions.remove(session)
            log.warning("Player: the player's window went away; opening VLC instead")
            self._drop(window)
            await self._fall_back(found, "not_started")
            return
        log.info(
            "Player: %s %s sent (resume asked: %s, intro: %s, next: %s)",
            title.kind, video.key, message.ask_resume, intro is not None, following is not None,
        )
        self._say(t(PLAYING_TEXT, name=found.name))

    def _running(self) -> bool:
        window = self._window
        return not (window is None or window.closing or window.process.returncode is not None)

    async def _window_for(self, engine: Engine) -> _Window | None:
        """The running player's window, else a new one; None when it doesn't start (then not
        again this run: `_start_failed`)."""
        if self._running():
            return self._window
        if self._start_failed:
            return None
        window = await self._start(engine)
        if window is None:
            self._start_failed = True
            return None
        self._window = window
        return window

    # -- a file from Explorer ("Open with") ------------------------------------------------------

    def _start_local(self, path: str, *, from_next: bool, window: _Window | None = None) -> None:
        if self._closed:
            return
        self._cancel_play()
        self._next_for = window if from_next else None
        self._play_task = spawn(self._play_local(path, from_next=from_next), name="media player")

    async def _play_local(self, path: str, *, from_next: bool) -> None:
        name = PureWindowsPath(path).stem
        located = Located(name=name, kind="movie", path=path)
        if not await _within(asyncio.to_thread(os.path.isfile, path), False):
            log.info("Player: a file from Explorer isn't there")
            self._say(t(FILE_GONE_TEXT))
            self._idle_check()
            return
        engine = await self._engine_here()
        if engine is None:
            await self._fall_back(located, "no_engine")
            self._idle_check()
            return
        folder = os.path.dirname(path)
        no_names: list[str] = []
        listing = self._job(
            _within(asyncio.to_thread(video_names, folder), no_names), "player folder"
        )
        try:
            window = await self._window_for(engine)
            names: list[str] = await listing
        finally:
            listing.cancel()
        if window is None:
            await self._fall_back(located, "not_started")
            self._idle_check()
            return
        following = next_file(path, names)
        carry_key = "folder:" + os.path.normcase(folder)
        carry = window.carry if window.carry and window.carry[0] == carry_key else None
        start = self._places.place(path) if self._places is not None else 0
        settings = self._settings.current
        message = Play(
            file=path,
            title=name,
            line="",
            start_ms=start,
            ask_resume=start > 0 and not from_next,
            intro=None,
            credits_ms=None,
            next_label=file_code(following) if following is not None else None,
            always_audio=settings.always_audio,
            always_subtitles=settings.always_subtitles,
            carry_audio=carry[1] if carry else "",
            carry_subtitles=carry[2] if carry else "",
            find_subtitles=False,
        )
        session = _Session(
            title=PlexTitle(kind="movie", key="", title=name),
            video=PlexVideo(key="", file=path),
            path=path,
            episodes=(),
            ms=start,
            duration_ms=0,
            local=True,
            following=following,
            carry_key=carry_key,
        )
        if window.sessions and window.sessions[-1].ended:
            window.sessions.pop()
        window.sessions.append(session)
        if not await _send(window.process, message):
            window.sessions.remove(session)
            log.warning("Player: the player's window went away; opening VLC instead")
            self._drop(window)
            await self._fall_back(located, "not_started")
            self._idle_check()
            return
        log.info(
            "Player: a file from Explorer sent (resume asked: %s, next: %s)",
            message.ask_resume, following is not None,
        )
        self._say(t(PLAYING_TEXT, name=name))

    def _keep_place(self, session: _Session, ms: int) -> None:
        if self._places is not None:
            self._places.remember(session.path, ms, session.duration_ms)

    def _idle_check(self) -> None:
        """No player window open (any more): `on_idle`."""
        if self._running():
            return
        try:
            self.on_idle()
        except Exception:
            log.exception("The player's idle callback failed")

    async def _markers(self, key: str) -> Markers:
        try:
            async with asyncio.timeout(MARKERS_TIMEOUT_S):
                return await self._plex.markers(key)
        except (PlexError, TimeoutError) as exc:
            log.info("Player: no markers for %s (%s)", key, type(exc).__name__)
            return None, None

    async def _engine_here(self) -> Engine | None:
        """The engine the player can use, looked up once per run (in a thread: file reads)."""
        if not self._engine_known:
            engine = await asyncio.to_thread(self._find_engine)
            if engine is None:
                problem = await asyncio.to_thread(self._engine_problem)
                log.info(
                    "AirGrabber's player can't run here (%s); ▶ Play opens VLC or the video player",
                    problem,
                )
            self._engine, self._engine_known = engine, True
        return self._engine

    async def _fall_back(self, found: Located, why: Fallback | None) -> None:
        """3.14.1's ▶ Play; the status bar says why once per run."""
        outcome = await self._playback.launch(found)
        if why is not None and outcome.played and outcome.player and not self._why_said:
            self._why_said = True
            self._say(t(_WHY[why, outcome.player]))
            return
        self._say(outcome.text)

    async def _start(self, engine: Engine) -> _Window | None:
        """The player's process, once it said Ready; None when it didn't start or say it."""
        command = [self._python, "-m", PLAYER_MODULE, *self._look(), "--engine", engine.folder]
        starter = self._starter or start_player_process
        try:
            process = await starter(command, self._cwd)
        except (OSError, ValueError) as exc:
            log.warning("AirGrabber's player didn't start (%s)", type(exc).__name__)
            return None
        try:
            ready = await self._ready(process)
        except asyncio.CancelledError:
            _close_input(process)
            _kill(process)
            raise
        if not ready:
            code = await _exit_code(process)
            log.warning("AirGrabber's player didn't come up (%s)", exit_text(code))
            return None
        log.info("AirGrabber's player is up")
        window = _Window(process)
        window.reader = spawn(self._read(window), name="media player events")
        return window

    async def _ready(self, process: PlayerProcess) -> bool:
        """Whether the player says Ready in time; ended if it doesn't."""
        stdout = process.stdout
        if stdout is None:
            _kill(process)
            return False
        try:
            async with asyncio.timeout(self._ready_timeout):
                while line := await stdout.readline():
                    if isinstance(decode(line.decode("ascii", "replace")), Ready):
                        return True
        except (TimeoutError, ValueError):
            pass
        _kill(process)
        return False

    def _drop(self, window: _Window) -> None:
        window.closing = True
        if self._window is window:
            self._window = None
        _close_input(window.process)
        _kill(window.process)

    # -- what the player says --------------------------------------------------------------------

    async def _read(self, window: _Window) -> None:
        process = window.process
        stdout = process.stdout
        try:
            if stdout is not None:
                with contextlib.suppress(ValueError):  # a line beyond the reader's limit
                    while line := await stdout.readline():
                        message = decode(line.decode("ascii", "replace"))
                        if message is not None:
                            self._heard(window, message)
                while await stdout.read(4096):
                    pass
            code = await process.wait()
            log.info("AirGrabber's player closed (%s)", exit_text(code))
            self._lost(window)
        finally:
            window.closing = True
            window.sessions.clear()
            if self._window is window:
                self._window = None
            if self._next_for is window:
                self._cancel_play()  # the next episode was for this window: closed, so nothing
            if self._play_task is None or self._play_task.done():
                self._idle_check()

    def _lost(self, window: _Window) -> None:
        """The player ended without a `Stopped` for the video it showed (a crash): Plex hears
        "stopped" where it was last seen."""
        head = window.sessions[0] if window.sessions else None
        if head is None or head.ended or head.done or head.seen_at is None:
            return
        log.info(
            "Player: no Stopped for %s before the player ended",
            "a file from Explorer" if head.local else head.video.key,
        )
        self._report(head, "stopped", head.ms)

    def _heard(self, window: _Window, message: Message) -> None:
        head = window.sessions[0] if window.sessions else None
        if isinstance(message, AlwaysPick):
            self._job(self._always(message), "player always pick")
        if head is None:
            return
        if isinstance(message, Position):
            self._position(head, message)
        elif isinstance(message, Stopped):
            window.sessions.pop(0)
            if message.duration_ms > 0:
                head.duration_ms = message.duration_ms
            head.ms = message.ms
            self._report(head, "stopped", message.ms)
        elif isinstance(message, Ended):
            head.ended = True
            if message.duration_ms > 0:
                head.duration_ms = head.ms = message.duration_ms
            # Without the player's length it can't be placed: stopped where it was last seen,
            # not watched (the player only says Ended near the end, and knows the length then).
            self._report(head, "stopped", head.ms)
            if message.duration_ms > 0:
                self._check_watched(head, head.duration_ms)
            if len(window.sessions) > 1:
                # It ended before the player read the next Play: no Stopped will come for it.
                window.sessions.pop(0)
        elif isinstance(message, WantNext):
            self._want_next(window, head)
        elif isinstance(message, FindSubtitles):
            self._job(self._find_subtitles(window, head, message.language), "player subtitles")
        elif isinstance(message, Tracks):
            if head.carry_key is not None:
                window.carry = (head.carry_key, message.audio, message.subtitles)

    def _position(self, session: _Session, message: Position) -> None:
        if session.done:
            return
        now = self._clock()
        if message.duration_ms > 0:
            session.duration_ms = message.duration_ms
        jumped = False
        if session.seen_at is not None:
            moved = round((now - session.seen_at) * 1000) if session.seen_state == "playing" else 0
            jumped = abs(message.ms - (session.ms + moved)) > SEEK_SLACK_MS
        session.ended = False  # played again after its end
        session.ms, session.seen_at, session.seen_state = message.ms, now, message.state
        due = (
            session.reported_at is None
            or now - session.reported_at >= REPORT_EVERY_S - REPORT_SLACK_S
        )
        if message.state != session.state or jumped or due:
            self._report(session, message.state, message.ms)
        self._check_watched(session, message.ms)

    def _report(self, session: _Session, state: PlayState, ms: int) -> None:
        """Plex's timeline for `session`. A report of it still waiting for its turn is
        replaced (only the newest place goes: seeks held down don't queue one call each)."""
        if session.done:
            return
        session.state, session.reported_at = state, self._clock()
        if session.local:
            self._keep_place(session, ms)  # no Plex: where it was left, on this PC
            return
        waiting, session.waiting = session.waiting, (state, ms, session.duration_ms)
        if waiting is not None:
            return  # the call already waiting sends this one instead
        key = session.video.key

        async def call() -> None:
            report, session.waiting = session.waiting, None
            if report is not None:
                await self._plex.report(key, *report)

        self._to_plex(session, "report", call)

    def _check_watched(self, session: _Session, ms: int) -> None:
        duration = session.duration_ms
        if session.local or session.watched or session.done or duration <= 0:
            return
        if ms < duration * WATCHED_SHARE:
            return
        session.watched = True
        key = session.video.key
        log.info("Player: %s watched to %d%%", key, round(100 * WATCHED_SHARE))
        self._to_plex(session, "watched mark", lambda: self._plex.mark_watched(key))

    def _to_plex(
        self, session: _Session, what: str, call: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        async def send() -> None:
            async with self._plex_lock:  # one at a time, in order
                try:
                    await call()
                except PlexError as exc:
                    log.info(
                        "Player: Plex's %s for %s failed (%s)",
                        what, session.video.key, type(exc).__name__,
                    )
                    if not session.plex_said:
                        session.plex_said = True
                        self._say(t(PLEX_AWAY_TEXT))

        self._job(send(), "player plex")

    def _want_next(self, window: _Window, session: _Session) -> None:
        if session is not window.sessions[-1]:
            return  # another video is on its way already
        if session.local:
            if session.following is None:
                log.info("Player: no file after this one in its folder")
            else:
                log.info("Player: the next file of the folder")
                self._start_local(session.following, from_next=True, window=window)
            return
        following = next_in_show(session.video, session.episodes)
        if following is None:
            log.info("Player: no episode after %s", session.video.key)
            return
        log.info("Player: the next episode after %s", session.video.key)
        # The countdown starts at the credits, below WATCHED_SHARE on a short episode or long
        # credits: moving on means it was watched.
        self._check_watched(session, session.duration_ms)
        self._start_play(
            session.title, following, session.episodes, from_next=True, window=window
        )

    async def _always(self, message: AlwaysPick) -> None:
        name = "always_audio" if message.kind == "audio" else "always_subtitles"
        await self._settings.update(**{name: message.language})
        log.info("Player: Always pick for %s changed", message.kind)

    async def _find_subtitles(self, window: _Window, session: _Session, language: str) -> None:
        if session.local:  # its menu has no Find subtitles: a player from before asked
            await _send(window.process, Notice(text=t(SUBTITLES_UNAVAILABLE_TEXT)))
            return
        text: str | None = None
        found: str | None = None
        finder = await self._subtitles()
        if finder is None:
            log.info("Player: subtitles for %s: MediaForge is off", session.video.key)
            text = t(FORGE_OFF_TEXT)
        else:
            try:
                fetched = await finder.fetch(
                    Path(session.path), _identity(session), [language]
                )
            except Exception as exc:  # it never raises for OpenSubtitles or the folder
                log.warning("Player: subtitles failed (%s)", type(exc).__name__)
                text = t(SUBTITLES_FAILED_TEXT)
            else:
                outcome = str(fetched.outcome)
                log.info("Player: subtitles for %s: %s", session.video.key, outcome)
                if outcome in ("saved", "partly", "already"):
                    found = await asyncio.to_thread(subtitle_file, session.path, language)
                text = None if found else _subtitle_text(outcome, fetched.message)
        if window.closing or not window.sessions or window.sessions[-1] is not session:
            return  # another video by now
        if found is not None:
            await _send(window.process, LoadSubtitle(path=found))
        elif text:
            await _send(window.process, Notice(text=text))

    # -- helpers ---------------------------------------------------------------------------------

    def _job(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = spawn(coro, name=name)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        return task

    def _say(self, text: str) -> None:
        try:
            self.on_status(text)
        except Exception:  # the status bar's window may be gone
            log.exception("The player's status line failed")


def _line(title: PlexTitle, video: PlexVideo) -> str:
    """The player's title line: "S02E03 · Title" for an episode, the year for a film."""
    if title.kind == "show":
        return " · ".join(part for part in (episode_code(video), video.title) if part)
    return str(title.year) if title.year else ""


def _identity(session: _Session) -> SubtitleIdentity:
    """What OpenSubtitles finds the video by (Forge's type, imported only when asked)."""
    from app.forge.models.media import MediaKind
    from app.forge.models.subtitles import SubtitleIdentity

    episode = session.title.kind == "show"
    return SubtitleIdentity(
        kind=MediaKind.EPISODE if episode else MediaKind.MOVIE,
        title=session.title.title,
        year=session.title.year,
        tmdb_id=session.title.tmdb_id,
        season=session.video.season if episode else None,
        episode=session.video.episode if episode else None,
        release=PureWindowsPath(session.path).name,
    )


def _subtitle_text(outcome: str, message: str) -> str:
    """A Find subtitles that loaded nothing, in plain words."""
    if outcome in ("quota", "problem") and message:
        return message  # MediaForge's own words, already translated
    if outcome == "not_found":
        return t(SUBTITLES_NOT_FOUND_TEXT)
    if outcome == "no_video":
        return t(SUBTITLES_NO_VIDEO_TEXT)
    if outcome == "unavailable":
        return t(SUBTITLES_UNAVAILABLE_TEXT)
    return t(SUBTITLES_FAILED_TEXT)


async def _within[T](work: Awaitable[T], instead: T) -> T:
    """`work`'s answer, or `instead` after FOLDER_TIMEOUT_S (a share that hangs)."""
    try:
        return await asyncio.wait_for(work, FOLDER_TIMEOUT_S)
    except TimeoutError:
        return instead


async def _send(process: PlayerProcess, message: Message) -> bool:
    """One line to the player; False when its stdin is gone."""
    stdin = process.stdin
    if stdin is None:
        return False
    try:
        stdin.write((encode(message) + "\n").encode("ascii"))
        await stdin.drain()
    except (OSError, RuntimeError):
        return False
    return True


async def _exit_code(process: PlayerProcess) -> int | None:
    """A player that didn't come up: its exit code once it has ended (None after END_TIMEOUT_S)."""
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
    if process.returncode is None:
        with contextlib.suppress(OSError):  # ProcessLookupError: it has just ended
            process.kill()
