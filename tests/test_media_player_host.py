"""▶ Play in AirGrabber's own player, the app's side (app/ui/media_player.py's MediaPlayerHost):
starting the player's process once and reusing it, the Play line it gets (never a token or a
URL), Plex in sync (positions to the timeline, throttled; watched at 90 %; stopped on quitting;
Plex away), the next episode, Find subtitles through a fake MediaForge, Always pick and the
fallback to VLC. The player is a fake process (scripted stdout lines, recorded stdin), Plex a
fake server behind httpx.MockTransport (tests/test_plex_playing.py), the files real ones in a
temporary folder; no player, VLC or Plex is ever reached."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

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
from app.models import PlayIn
from app.models.plex import PlexTitle, PlexVideo
from app.services import playback
from app.services.local_files import LocalPlaces
from app.services.playback import LibraryFolders, PlaybackService
from app.services.vlc_engine import Engine
from app.ui import media_player
from app.ui.media_player import APP_ROOT, MediaPlayerHost
from tests.plex_link import TOKEN
from tests.test_plex_playing import PlayingTestCase

ENGINE = Engine(folder=r"C:\Program Files\VideoLAN\VLC")
DUNE = PlexTitle(kind="movie", key="3001", title="Dune: Part Two", year=2024, tmdb_id=693134)
DUNE_FILE = "/volume1/video/Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv"
ARK = PlexTitle(kind="show", key="1001", title="The Ark", year=2023, tmdb_id=202411)
SEASON = "/volume1/video/TV/The Ark (2023)/Season 02/"
PRIVATE = ("Dune", "Ark", "volume1", ".mkv", ".srt", TOKEN)  # never in a log line


def episode(key: str, number: int, *, offset: int = 0) -> PlexVideo:
    return PlexVideo(
        key=key,
        file=f"{SEASON}The Ark - S02E{number:02d}.mkv",
        season=2,
        episode=number,
        title=f"Part {number}",
        view_offset_ms=offset,
        duration_ms=2700000,
    )


E1, E2, E3 = episode("41", 1), episode("42", 2), episode("43", 3, offset=30000)
SPECIAL = PlexVideo(key="49", file=f"{SEASON}The Ark - S00E01.mkv", season=0, episode=1)
EPISODES = (E1, E2, E3, SPECIAL)
DUNE_VIDEO = PlexVideo(key="3001", file=DUNE_FILE, view_offset_ms=4360000, duration_ms=9000000)


class FakeInput:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.data = b""
        self.closed = False
        self.closed_after_requests: int | None = None

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        self.data += data

    async def drain(self) -> None:
        if self.closed:
            raise ConnectionResetError

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.process.input_closed()


class FakeOutput:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()

    async def read(self, n: int = -1) -> bytes:
        return b""


class FakeProcess:
    """The player: what it says is scripted (`say`), what it was sent is recorded (`sent`);
    stdin's end ends it (after `on_close`, a last line it says then), as the real one does."""

    def __init__(self, *, ready: bool = True) -> None:
        self.stdin = FakeInput(self)
        self.stdout = FakeOutput()
        self.returncode: int | None = None
        self.killed = False
        self.on_close: Message | None = None
        self.ends_on_close = True  # False: hangs after its stdin ends (only a kill ends it)
        self._ended = asyncio.Event()
        if ready:
            self.say(Ready())

    def say(self, message: Message) -> None:
        self.stdout.lines.put_nowait((encode(message) + "\n").encode("ascii"))

    def end(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self.stdout.lines.put_nowait(b"")
            self._ended.set()

    def input_closed(self) -> None:
        if self.on_close is not None:
            self.say(self.on_close)
        if self.ends_on_close:
            self.end(0)

    async def wait(self) -> int:
        await self._ended.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.end(1)

    def sent(self) -> list[Message]:
        found: list[Message] = []
        for line in self.stdin.data.decode("ascii").splitlines():
            message = decode(line)
            assert message is not None, line
            found.append(message)
        return found

    def plays(self) -> list[Play]:
        return [message for message in self.sent() if isinstance(message, Play)]


class FakeStarter:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.cwds: list[Path] = []
        self.processes: list[FakeProcess] = []
        self.ready = True
        self.error: OSError | None = None

    async def __call__(self, command: Sequence[str], cwd: Path) -> FakeProcess:
        self.commands.append(list(command))
        self.cwds.append(cwd)
        if self.error is not None:
            raise self.error
        process = FakeProcess(ready=self.ready)
        self.processes.append(process)
        return process


class FakeLauncher:
    def __init__(self) -> None:
        self.player: playback.Player = "vlc"
        self.paths: list[str] = []

    async def __call__(self, path: str) -> playback.Player:
        self.paths.append(path)
        return self.player


class FakeSubtitles:
    """MediaForge's subtitle service: `outcome` says how the next fetch goes ("saved" writes
    "<stem>.<lang>.srt" beside the video, as MediaForge does)."""

    def __init__(self) -> None:
        self.outcome = "saved"
        self.message = ""
        self.asked: list[tuple[Path, Any, list[str]]] = []

    async def fetch(
        self,
        video: Path,
        identity: Any,
        languages: Sequence[str] | None = None,
        *,
        run_id: int | None = None,
    ) -> Any:
        from app.forge.models.subtitles import SubtitleFetch, SubtitleFetchOutcome

        wanted = list(languages or ())
        self.asked.append((video, identity, wanted))
        if self.outcome == "saved":
            for code in wanted:
                video.with_name(f"{video.stem}.{code}.srt").write_text("1\n", encoding="utf-8")
            return SubtitleFetch(outcome=SubtitleFetchOutcome.SAVED, saved=tuple(wanted))
        return SubtitleFetch(
            outcome=SubtitleFetchOutcome(self.outcome), not_found=tuple(wanted), message=self.message
        )


class HostTestCase(PlayingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.private: tuple[str, ...] = (*PRIVATE, self.root.name)
        self.series, self.movies = self.root / "TV", self.root / "Movies"
        self.dune_here = self.movies / "Dune Part Two (2024)" / "Dune Part Two (2024).mkv"
        season = self.series / "The Ark (2023)" / "Season 02"
        for path in (self.dune_here, *(season / f"The Ark - S02E0{n}.mkv" for n in (1, 2, 3))):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
        self.season_here = season
        folders = LibraryFolders(series=str(self.series), movies=str(self.movies))

        async def lookup() -> LibraryFolders | None:
            return folders

        self.launch = FakeLauncher()
        self.playback = PlaybackService(self.plex, lookup, launch=self.launch)
        self.starter = FakeStarter()
        self.engine: Engine | None = ENGINE
        self.engine_asked = 0
        self.subtitles: FakeSubtitles | None = FakeSubtitles()
        self.now = 0.0
        self.status: list[str] = []
        self.places = LocalPlaces(self.root / "played-files.json")
        self.idle = 0
        self.host = self.make()
        self.addAsyncCleanup(self.host.aclose)

    def make(self, **kwargs: Any) -> MediaPlayerHost:
        def find() -> Engine | None:
            self.engine_asked += 1
            return self.engine

        async def subtitles() -> FakeSubtitles | None:
            return self.subtitles

        host = MediaPlayerHost(
            self.playback,
            self.plex,
            self.settings,
            subtitles,
            starter=self.starter,
            find_engine=find,
            engine_problem=lambda: "not_64bit",
            look=lambda: ["--background", "#10161C", "--title", "AirGrabber"],
            python="python.exe",
            clock=lambda: self.now,
            places=self.places,
            **kwargs,
        )
        host.on_status = self.status.append

        def idle() -> None:
            self.idle += 1

        host.on_idle = idle
        return host

    async def settle(self) -> None:
        for _ in range(4):
            for _ in range(10):
                await asyncio.sleep(0)
            await self.host.settled()

    async def play(
        self, title: PlexTitle, video: PlexVideo, episodes: Sequence[PlexVideo] = ()
    ) -> FakeProcess:
        self.host.play(title, video, episodes=episodes)
        await self.settle()
        return self.starter.processes[-1]

    async def spin(self) -> None:
        """Let the loop run a while without waiting for the host's jobs."""
        for _ in range(20):
            await asyncio.sleep(0)

    async def tell(self, process: FakeProcess, *messages: Message) -> None:
        for message in messages:
            process.say(message)
        await self.settle()

    def timeline(self) -> list[tuple[str, str, int]]:
        return [
            (r.url.params["ratingKey"], r.url.params["state"], int(r.url.params["time"]))
            for r in self.server.requests
            if r.url.path == "/:/timeline"
        ]

    def scrobbles(self) -> list[str]:
        return [r.url.params["key"] for r in self.server.requests if r.url.path == "/:/scrobble"]

    def assert_private(self, lines: Sequence[str]) -> None:
        self.assertFalse([line for line in lines if any(p in line for p in self.private)])


class StartTests(HostTestCase):
    async def test_play_sends_play_without_token(self) -> None:
        with self.assertLogs("app.ui.media_player", "INFO") as logs:
            process = await self.play(DUNE, DUNE_VIDEO)
        command = self.starter.commands[0]
        self.assertEqual(command[:3], ["python.exe", "-m", "app.media_player"])
        self.assertEqual(command[-2:], ["--engine", ENGINE.folder])
        self.assertIn("--background", command)
        self.assertEqual(self.starter.cwds, [APP_ROOT])
        self.assertTrue((APP_ROOT / "app" / "media_player.py").is_file())
        written = process.stdin.data.decode("ascii")
        for text in (" ".join(command), written):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("http", text)
            self.assertNotIn("://", text)
        [sent] = process.plays()
        self.assertEqual(sent.file, str(self.dune_here))
        self.assertEqual((sent.title, sent.line), ("Dune: Part Two", "2024"))
        self.assertEqual((sent.start_ms, sent.ask_resume), (4360000, True))
        self.assertEqual((sent.intro, sent.credits_ms, sent.next_label), (None, None, None))
        self.assertEqual((sent.always_audio, sent.always_subtitles), ("", ""))
        self.assertEqual((sent.carry_audio, sent.carry_subtitles), ("", ""))
        self.assertEqual(self.status, ["▶ Playing Dune: Part Two."])
        self.assertEqual(self.launch.paths, [])
        self.assert_private(logs.output)

    async def test_resume_asked_only_with_a_position(self) -> None:
        process = await self.play(ARK, E2, EPISODES)
        [sent] = process.plays()
        self.assertEqual((sent.start_ms, sent.ask_resume), (0, False))
        self.assertEqual((sent.title, sent.line), ("The Ark", "S02E02 · Part 2"))
        self.assertEqual(sent.file, str(self.season_here / "The Ark - S02E02.mkv"))
        # The episode's markers, from Plex; the next one's label for the countdown.
        self.assertEqual((sent.intro, sent.credits_ms), ((61000, 142000), 2550000))
        self.assertEqual(sent.next_label, "S02E03")
        await self.play(ARK, E3, EPISODES)
        self.assertEqual(
            [(p.start_ms, p.ask_resume) for p in process.plays()], [(0, False), (30000, True)]
        )

    async def test_always_pick_from_settings_and_stored_from_the_player(self) -> None:
        await self.settings.update(always_audio="en", always_subtitles="off")
        process = await self.play(DUNE, DUNE_VIDEO)
        [sent] = process.plays()
        self.assertEqual((sent.always_audio, sent.always_subtitles), ("en", "off"))
        await self.tell(process, AlwaysPick(kind="subtitles", language="nl"))
        await self.tell(process, AlwaysPick(kind="audio", language=""))
        self.assertEqual(
            (self.settings.current.always_audio, self.settings.current.always_subtitles), ("", "nl")
        )

    async def test_markers_that_fail_are_none(self) -> None:
        self.server.status = 500
        process = await self.play(ARK, E2, EPISODES)
        [sent] = process.plays()
        self.assertEqual((sent.intro, sent.credits_ms), (None, None))
        self.assertEqual(self.status, ["▶ Playing The Ark S02E02."])

    async def test_a_file_not_found_says_so_and_starts_nothing(self) -> None:
        self.dune_here.unlink()
        self.host.play(DUNE, DUNE_VIDEO)
        await self.settle()
        self.assertEqual(self.starter.commands, [])
        self.assertEqual(len(self.status), 1)
        self.assertIn("Movies folder", self.status[0])

    async def test_tests_never_start_a_real_player(self) -> None:
        with self.assertRaises(AssertionError):
            await media_player.start_player_process(["python.exe"], APP_ROOT)


class ReuseTests(HostTestCase):
    async def test_second_play_reuses_window_and_stops_old(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        await self.tell(process, Position(ms=4400000, duration_ms=9000000, state="playing"))
        self.host.play(ARK, E2, episodes=EPISODES)
        await self.settle()
        self.assertEqual(len(self.starter.processes), 1)  # the same window
        self.assertEqual([p.file for p in process.plays()][-1], str(self.season_here / "The Ark - S02E02.mkv"))
        # A position in flight for the film, then the player's Stopped for it, then the episode's
        # (the film's pause not sent yet: its Stopped replaces it).
        self.now = 1.0
        await self.tell(
            process,
            Position(ms=4401000, duration_ms=9000000, state="paused"),
            Stopped(ms=4401000, duration_ms=9000000),
            Position(ms=0, duration_ms=2700000, state="playing"),
        )
        self.assertEqual(
            self.timeline(),
            [
                ("3001", "playing", 4400000),
                ("3001", "stopped", 4401000),
                ("42", "playing", 0),
            ],
        )

    async def test_a_closed_window_is_started_again(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        process.on_close = Stopped(ms=4360000, duration_ms=9000000)
        self.host.close()  # sends nothing to Plex itself: the player's Stopped does
        await self.settle()
        self.assertTrue(process.stdin.closed)
        self.assertEqual(self.timeline(), [("3001", "stopped", 4360000)])
        second = await self.play(DUNE, DUNE_VIDEO)
        self.assertIsNot(second, process)
        self.assertEqual(len(second.plays()), 1)
        self.assertEqual(self.engine_asked, 1)  # once per run

    async def test_tracks_carry_to_the_next_episode_in_this_window(self) -> None:
        process = await self.play(ARK, E1, EPISODES)
        await self.tell(process, Tracks(audio="en", subtitles="nl"))
        await self.play(ARK, E2, EPISODES)
        await self.play(DUNE, DUNE_VIDEO)  # a film: not the show's
        plays = process.plays()
        self.assertEqual(
            [(p.carry_audio, p.carry_subtitles) for p in plays], [("", ""), ("en", "nl"), ("", "")]
        )


class SyncTests(HostTestCase):
    async def test_reports_throttled_to_ten_seconds(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        for second in (0, 5, 10, 15, 20):
            self.now = float(second)
            await self.tell(process, Position(ms=4360000 + second * 1000, duration_ms=9000000, state="playing"))
        self.now = 21.0
        await self.tell(process, Position(ms=4381000, duration_ms=9000000, state="paused"))
        self.now = 22.0
        await self.tell(process, Position(ms=4381000, duration_ms=9000000, state="playing"))
        self.now = 23.0  # a seek: far from where it would be
        await self.tell(process, Position(ms=6000000, duration_ms=9000000, state="playing"))
        self.assertEqual(
            self.timeline(),
            [
                ("3001", "playing", 4360000),
                ("3001", "playing", 4370000),
                ("3001", "playing", 4380000),
                ("3001", "paused", 4381000),
                ("3001", "playing", 4381000),
                ("3001", "playing", 6000000),
            ],
        )
        request = next(r for r in self.server.requests if r.url.path == "/:/timeline")
        self.assertEqual(request.url.params["duration"], "9000000")
        self.assert_token_in_header_only(request)

    async def test_watched_once_at_ninety_percent(self) -> None:
        process = await self.play(ARK, E2, EPISODES)
        await self.tell(process, Position(ms=2420000, duration_ms=2700000, state="playing"))
        self.assertEqual(self.scrobbles(), [])
        self.now = 10.0
        await self.tell(process, Position(ms=2430000, duration_ms=2700000, state="playing"))
        self.now = 20.0
        await self.tell(process, Position(ms=2440000, duration_ms=2700000, state="playing"))
        await self.tell(process, Ended(duration_ms=2700000))
        self.assertEqual(self.scrobbles(), ["42"])
        self.assertEqual(self.timeline()[-1], ("42", "stopped", 2700000))

    async def test_held_seeks_send_only_the_latest_place(self) -> None:
        # Right held down: a seek every moment. One report on its way, then only the newest.
        process = await self.play(DUNE, DUNE_VIDEO)
        gate = asyncio.Event()
        sent: list[tuple[str, int]] = []

        async def report(key: str, state: str, position_ms: int, duration_ms: int) -> None:
            sent.append((state, position_ms))
            await gate.wait()

        with mock.patch.object(self.plex, "report", report):
            process.say(Position(ms=4360000, duration_ms=9000000, state="playing"))
            await self.spin()
            self.assertEqual(sent, [("playing", 4360000)])  # in flight
            for step in range(1, 21):
                self.now = step / 10
                process.say(
                    Position(ms=4360000 + step * 30000, duration_ms=9000000, state="playing")
                )
                await self.spin()
            self.assertEqual(len(sent), 1)
            gate.set()
            await self.settle()
        self.assertEqual(sent, [("playing", 4360000), ("playing", 4960000)])

    async def test_a_player_that_crashes_reports_stopped(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        await self.tell(process, Position(ms=4420000, duration_ms=9000000, state="playing"))
        self.now = 3.0
        await self.tell(process, Position(ms=4423000, duration_ms=9000000, state="playing"))
        with self.assertLogs("app.ui.media_player", "INFO") as logs:
            process.end(3221225477)  # a native crash: no Stopped
            await self.settle()
        self.assertEqual(self.timeline()[-1], ("3001", "stopped", 4423000))
        self.assert_private(logs.output)

    async def test_a_player_that_closes_normally_reports_once(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        await self.tell(process, Position(ms=4420000, duration_ms=9000000, state="playing"))
        process.on_close = Stopped(ms=4421000, duration_ms=9000000)
        self.host.close()
        await self.settle()
        self.assertEqual(
            self.timeline(), [("3001", "playing", 4420000), ("3001", "stopped", 4421000)]
        )

    async def test_an_end_without_a_length_is_no_end(self) -> None:
        # An Ended the player can't place: stopped where it was seen, never watched.
        process = await self.play(ARK, E2, EPISODES)
        await self.tell(process, Position(ms=1200000, duration_ms=0, state="playing"))
        await self.tell(process, Ended(duration_ms=0))
        self.assertEqual(self.timeline()[-1], ("42", "stopped", 1200000))
        self.assertEqual(self.scrobbles(), [])

    async def test_plex_away_keeps_playing_and_says_so_once(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        self.server.status = 500
        with self.assertLogs("app.ui.media_player", "INFO") as logs:
            for second in (0, 10, 20):
                self.now = float(second)
                await self.tell(process, Position(ms=8200000 + second * 1000, duration_ms=9000000, state="playing"))
        away = "Plex didn't answer, so your place isn't saved there right now."
        self.assertEqual(self.status, ["▶ Playing Dune: Part Two.", away])
        self.assertFalse(process.stdin.closed)  # it plays on
        self.assertEqual(self.scrobbles(), ["3001"])  # tried once
        self.assert_private(logs.output)
        await self.play(ARK, E2, EPISODES)  # another video: said again, once
        await self.tell(process, Stopped(ms=8220000, duration_ms=9000000))
        self.now = 30.0
        await self.tell(process, Position(ms=1000, duration_ms=2700000, state="playing"))
        self.assertEqual(self.status[-2:], ["▶ Playing The Ark S02E02.", away])

    async def test_quit_reports_stopped_then_closes(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        await self.tell(process, Position(ms=4420000, duration_ms=9000000, state="playing"))
        real_close = process.stdin.close

        def close() -> None:
            process.stdin.closed_after_requests = len(self.timeline())
            real_close()

        process.stdin.close = close  # type: ignore[method-assign]
        process.on_close = Stopped(ms=4421000, duration_ms=9000000)
        await self.host.aclose()
        # aclose waited for the player to end by itself (its window's place saved), no kill.
        self.assertEqual((process.returncode, process.killed), (0, False))
        await self.settle()
        self.assertTrue(process.stdin.closed)
        self.assertEqual(self.timeline()[-1], ("3001", "stopped", 4420000))
        self.assertEqual(process.stdin.closed_after_requests, 2)  # the report came first
        self.assertEqual(len(self.timeline()), 2)  # the player's own Stopped isn't sent again


    async def test_quit_waits_for_the_player_to_end(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        ended_when_closed: list[bool] = []
        real_close = process.stdin.close

        def close() -> None:
            real_close()
            ended_when_closed.append(process.returncode is not None)

        process.stdin.close = close  # type: ignore[method-assign]
        process.ends_on_close = False

        async def ends_a_moment_later() -> int:
            await asyncio.sleep(0.05)  # saving its settings, letting libVLC go
            process.end(0)
            return 0

        real_wait = process.wait

        async def wait() -> int:
            if process.stdin.closed and process.returncode is None:
                return await ends_a_moment_later()
            return await real_wait()

        process.wait = wait  # type: ignore[method-assign]
        await self.host.aclose()
        self.assertEqual(ended_when_closed, [False])  # not ended when its stdin closed
        self.assertEqual((process.returncode, process.killed), (0, False))  # aclose waited

    async def test_quit_ends_a_player_that_hangs(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        process.ends_on_close = False  # never ends by itself
        with (
            mock.patch.object(media_player, "QUIT_EXIT_TIMEOUT_S", 0.05),
            self.assertLogs("app.ui.media_player", "WARNING"),
        ):
            await asyncio.wait_for(self.host.aclose(), 2.0)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.killed)

    def test_quitting_fits_the_quit_gates_bound(self) -> None:
        from app.ui.forge_link import PLAYER_CLOSE_S

        bound = media_player.QUIT_REPORT_TIMEOUT_S + media_player.QUIT_EXIT_TIMEOUT_S
        self.assertLess(bound, PLAYER_CLOSE_S)


class NextEpisodeTests(HostTestCase):
    async def test_want_next_plays_next_episode_without_asking(self) -> None:
        process = await self.play(ARK, E2, EPISODES)
        await self.tell(process, Ended(duration_ms=2700000), WantNext())
        self.assertEqual(len(self.starter.processes), 1)
        first, then = process.plays()
        self.assertEqual(then.file, str(self.season_here / "The Ark - S02E03.mkv"))
        self.assertEqual((then.start_ms, then.ask_resume), (30000, False))  # Plex's place, no prompt
        self.assertEqual(then.line, "S02E03 · Part 3")
        self.assertIsNone(then.next_label)  # the last regular one: the special doesn't follow
        # Play now before the end: the player's Stopped for E1 comes after the next Play.
        await self.play(ARK, E1, EPISODES)
        await self.tell(process, Stopped(ms=30000, duration_ms=2700000))  # E3, replaced
        await self.tell(process, Position(ms=100000, duration_ms=2700000, state="playing"), WantNext())
        await self.tell(process, Stopped(ms=101000, duration_ms=2700000))
        self.assertEqual(process.plays()[-1].file, str(self.season_here / "The Ark - S02E02.mkv"))
        self.assertEqual(process.plays()[-1].next_label, "S02E03")
        self.assertEqual(self.timeline()[-1], ("41", "stopped", 101000))

    async def test_the_next_episode_marks_the_one_before_watched(self) -> None:
        # The countdown starts at the credits: on a short episode below 90 %.
        process = await self.play(ARK, E2, EPISODES)
        await self.tell(process, Position(ms=2300000, duration_ms=2700000, state="playing"))
        self.assertEqual(self.scrobbles(), [])
        await self.tell(process, WantNext())
        await self.tell(process, Stopped(ms=2310000, duration_ms=2700000))
        self.assertEqual(self.scrobbles(), ["42"])  # once
        self.assertEqual(process.plays()[-1].file, str(self.season_here / "The Ark - S02E03.mkv"))
        self.assertEqual(self.timeline()[-1], ("42", "stopped", 2310000))

    async def test_closing_while_the_next_is_found_plays_nothing(self) -> None:
        process = await self.play(ARK, E2, EPISODES)
        gate = asyncio.Event()
        locate = self.playback.locate

        async def slow(title: PlexTitle, video: PlexVideo) -> Any:
            await gate.wait()  # the NAS waking up
            return await locate(title, video)

        with mock.patch.object(self.playback, "locate", slow):
            process.say(WantNext())
            await self.spin()
            process.say(Stopped(ms=2560000, duration_ms=2700000))  # the viewer closed it
            process.end(0)
            await self.spin()
            gate.set()
            await self.settle()
        self.assertEqual(len(self.starter.processes), 1)  # no new window popping up
        self.assertEqual(len(process.plays()), 1)
        self.assertEqual(self.launch.paths, [])

    async def test_last_episode_want_next_does_nothing(self) -> None:
        process = await self.play(ARK, E3, EPISODES)
        await self.tell(process, Ended(duration_ms=2700000), WantNext())
        self.assertEqual(len(process.plays()), 1)
        film = await self.play(DUNE, DUNE_VIDEO)  # a film has no next one either
        await self.tell(film, WantNext())
        self.assertEqual(len(film.plays()), 2)


class SubtitleTests(HostTestCase):
    async def test_find_subtitles_loads_on_success_and_notices_otherwise(self) -> None:
        process = await self.play(ARK, E2, EPISODES)
        video = self.season_here / "The Ark - S02E02.mkv"
        with self.assertLogs("app.ui.media_player", "INFO") as logs:
            await self.tell(process, FindSubtitles(language="nl"))
        assert self.subtitles is not None
        [(asked, identity, languages)] = self.subtitles.asked
        self.assertEqual((asked, languages), (video, ["nl"]))
        self.assertEqual(
            (identity.kind, identity.title, identity.year, identity.tmdb_id),
            ("episode", "The Ark", 2023, 202411),
        )
        self.assertEqual((identity.season, identity.episode, identity.release), (2, 2, video.name))
        self.assertEqual(process.sent()[-1], LoadSubtitle(path=str(video.with_name(video.stem + ".nl.srt"))))
        self.assert_private(logs.output)

        self.subtitles.outcome = "not_found"
        await self.tell(process, FindSubtitles(language="de"))
        last = process.sent()[-1]
        assert isinstance(last, Notice)
        self.assertEqual(last.text, "OpenSubtitles has no subtitles in that language for this video.")

        self.subtitles.outcome = "problem"
        self.subtitles.message = "OpenSubtitles didn't answer."
        await self.tell(process, FindSubtitles(language="de"))
        self.assertEqual(process.sent()[-1], Notice(text="OpenSubtitles didn't answer."))

        self.subtitles = None
        await self.tell(process, FindSubtitles(language="de"))
        self.assertEqual(
            process.sent()[-1], Notice(text="MediaForge is off, so subtitles can't be found.")
        )

    async def test_a_film_is_asked_for_as_a_movie(self) -> None:
        process = await self.play(DUNE, DUNE_VIDEO)
        await self.tell(process, FindSubtitles(language="en"))
        assert self.subtitles is not None
        identity = self.subtitles.asked[0][1]
        self.assertEqual((identity.kind, identity.tmdb_id, identity.season), ("movie", 693134, None))


class LocalFileTests(HostTestCase):
    """A video from Explorer's "Open with" (`play_file`): no Plex at all; its place on this PC
    (LocalPlaces), the next file of its folder, no Find subtitles."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.folder = self.root / "Downloads" / "Lantern Hill"
        self.folder.mkdir(parents=True)
        self.e3, self.e4 = (str(self.folder / f"Lantern.Hill.S01E0{n}.mkv") for n in (3, 4))
        self.holiday = str(self.root / "Downloads" / "Holiday 2025.mp4")
        for path in (self.e3, self.e4, self.holiday):
            Path(path).write_bytes(b"video")
        self.private = (*self.private, "Lantern", "Holiday", "Downloads")

    async def open(self, path: str) -> FakeProcess:
        self.host.play_file(path)
        await self.settle()
        return self.starter.processes[-1]

    def plex_asked(self) -> list[str]:
        return [request.url.path for request in self.server.requests]

    async def test_a_file_plays_without_plex(self) -> None:
        with self.assertLogs("app.ui.media_player", "INFO") as logs:
            process = await self.open(self.holiday)
            await self.tell(
                process,
                Position(ms=60_000, duration_ms=5_400_000, state="playing"),
                Position(ms=5_000_000, duration_ms=5_400_000, state="playing"),
            )
        settings = self.settings.current
        self.assertEqual(
            process.plays(),
            [
                Play(
                    file=self.holiday,
                    title="Holiday 2025",
                    line="",
                    start_ms=0,
                    ask_resume=False,
                    intro=None,
                    credits_ms=None,
                    next_label=None,
                    always_audio=settings.always_audio,
                    always_subtitles=settings.always_subtitles,
                    carry_audio="",
                    carry_subtitles="",
                    find_subtitles=False,
                )
            ],
        )
        self.assertEqual(self.plex_asked(), [])  # no markers, timeline or watched mark
        self.assertEqual(self.status, ["▶ Playing Holiday 2025."])
        self.assert_private(logs.output)

    async def test_where_it_was_left_is_asked(self) -> None:
        self.places.remember(self.holiday, 600_000, 5_400_000)
        process = await self.open(self.holiday)
        (message,) = process.plays()
        self.assertEqual((message.start_ms, message.ask_resume), (600_000, True))

    async def test_the_place_is_kept_and_forgotten_at_the_end(self) -> None:
        process = await self.open(self.holiday)
        await self.tell(process, Position(ms=60_000, duration_ms=5_400_000, state="playing"))
        await self.tell(process, Stopped(ms=900_000, duration_ms=5_400_000))
        self.assertEqual(LocalPlaces(self.root / "played-files.json").place(self.holiday), 900_000)
        self.host.play_file(self.holiday)
        await self.settle()
        await self.tell(process, Position(ms=5_390_000, duration_ms=5_400_000, state="playing"))
        await self.tell(process, Ended(duration_ms=5_400_000))
        self.assertEqual(LocalPlaces(self.root / "played-files.json").place(self.holiday), 0)
        self.assertEqual(self.plex_asked(), [])

    async def test_the_next_file_of_the_folder(self) -> None:
        self.places.remember(self.e4, 120_000, 2_700_000)
        process = await self.open(self.e3)
        self.assertEqual(process.plays()[0].next_label, "S01E04")
        await self.tell(
            process,
            Position(ms=2_600_000, duration_ms=2_700_000, state="playing"),
            Tracks(audio="en", subtitles="nl"),
            WantNext(),
        )
        second = process.plays()[-1]
        self.assertEqual(second.file, self.e4)
        self.assertEqual((second.start_ms, second.ask_resume), (120_000, False))  # no asking
        self.assertEqual((second.carry_audio, second.carry_subtitles), ("en", "nl"))
        self.assertIsNone(second.next_label)  # the last one
        self.assertEqual(len(self.starter.processes), 1)  # the same window
        self.assertEqual(self.plex_asked(), [])

    async def test_quitting_keeps_the_place(self) -> None:
        process = await self.open(self.holiday)
        await self.tell(process, Position(ms=700_000, duration_ms=5_400_000, state="playing"))
        await self.host.aclose()
        self.assertEqual(LocalPlaces(self.root / "played-files.json").place(self.holiday), 700_000)
        self.assertEqual(self.plex_asked(), [])

    async def test_a_file_that_isnt_there(self) -> None:
        self.host.play_file(str(self.folder / "Gone.mkv"))
        await self.settle()
        self.assertEqual(self.starter.commands, [])
        self.assertEqual(self.status, ["That video isn't there any more."])
        self.assertEqual(self.idle, 1)

    async def test_without_engine_it_opens_elsewhere(self) -> None:
        self.engine = None
        self.host.play_file(self.holiday)
        await self.settle()
        self.assertEqual(self.launch.paths, [self.holiday])
        self.assertEqual(self.idle, 1)

    async def test_the_vlc_setting_doesnt_count(self) -> None:
        await self.settings.update(play_in=PlayIn.VLC)
        await self.open(self.holiday)
        self.assertEqual(self.launch.paths, [])

    async def test_idle_once_the_player_closes(self) -> None:
        process = await self.open(self.holiday)
        self.assertEqual(self.idle, 0)
        process.end()
        await self.settle()
        self.assertEqual(self.idle, 1)
        self.assertFalse(self.host.open)


class FallbackTests(HostTestCase):
    async def test_falls_back_to_vlc_without_engine(self) -> None:
        self.engine = None
        with self.assertLogs("app.ui.media_player", "INFO") as logs:
            self.host.play(DUNE, DUNE_VIDEO)
            await self.settle()
        self.assertEqual(self.launch.paths, [str(self.dune_here)])
        self.assertEqual(
            self.status, ["AirGrabber's player needs VLC installed (64-bit), so this opens in VLC."]
        )
        self.assertTrue([line for line in logs.output if "not_64bit" in line])
        self.assert_private(logs.output)
        self.host.play(DUNE, DUNE_VIDEO)  # said once per run
        await self.settle()
        self.assertEqual(self.status[-1], "▶ Playing Dune: Part Two in VLC.")
        self.assertEqual((self.starter.commands, self.engine_asked), ([], 1))

    async def test_without_engine_in_your_video_player(self) -> None:
        self.engine = None
        self.launch.player = "default"
        self.host.play(DUNE, DUNE_VIDEO)
        await self.settle()
        self.assertEqual(
            self.status,
            ["AirGrabber's player needs VLC installed (64-bit), so this opens in your video player."],
        )

    async def test_vlc_setting_uses_vlc(self) -> None:
        await self.settings.update(play_in=PlayIn.VLC)
        self.host.play(DUNE, DUNE_VIDEO)
        await self.settle()
        self.assertEqual(self.launch.paths, [str(self.dune_here)])
        self.assertEqual(self.status, ["▶ Playing Dune: Part Two in VLC."])
        self.assertEqual((self.starter.commands, self.engine_asked), ([], 0))

    async def test_no_ready_in_time_falls_back(self) -> None:
        self.host = self.make(ready_timeout=0.05)
        self.addAsyncCleanup(self.host.aclose)
        self.starter.ready = False
        self.host.play(DUNE, DUNE_VIDEO)
        await asyncio.sleep(0.1)
        await self.settle()
        self.assertTrue(self.starter.processes[0].killed)
        self.assertEqual(self.launch.paths, [str(self.dune_here)])
        self.assertEqual(self.status, ["AirGrabber's player didn't start, so this opens in VLC."])
        # A player that can't be started at all: VLC again (the why said once already).
        self.starter.error = FileNotFoundError("gone")
        self.host.play(DUNE, DUNE_VIDEO)
        await self.settle()
        self.assertEqual(len(self.launch.paths), 2)
        self.assertEqual(self.status[-1], "▶ Playing Dune: Part Two in VLC.")

    async def test_a_start_that_hung_isnt_tried_again(self) -> None:
        self.host = self.make(ready_timeout=0.05)
        self.addAsyncCleanup(self.host.aclose)
        self.starter.ready = False
        self.host.play(DUNE, DUNE_VIDEO)
        await asyncio.sleep(0.1)
        await self.settle()
        self.assertEqual(len(self.starter.commands), 1)
        # Later plays this run: VLC at once, without the 10 s wait.
        self.starter.ready = True
        for _ in range(2):
            self.host.play(DUNE, DUNE_VIDEO)
            await self.settle()
        self.assertEqual(len(self.starter.commands), 1)
        self.assertEqual(self.launch.paths, [str(self.dune_here)] * 3)
        self.assertEqual(self.status[-1], "▶ Playing Dune: Part Two in VLC.")
