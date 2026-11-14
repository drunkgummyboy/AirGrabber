"""AirGrabber's own video player (app/media_player.py, app/media_player_window.py) on a hidden
Tk root with a FakeEngine (no libVLC) and fake stdin / stdout: what the stdin messages do, the
event lines it writes, the keys, the controls' fading, the resume prompt, Skip intro, the next
episode countdown, Find subtitles and Always pick, what it remembers, and closing while the
engine hangs. No real player process, VLC or video is ever started."""

from __future__ import annotations

import ctypes
import dataclasses
import json
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk

from app import media_player
from app.media_player import (
    EXIT_CLOSED,
    EXIT_CODES,
    EXIT_NO_ENGINE,
    TEXTS,
    Look,
    parse_options,
    text_arguments,
)
from app.media_player_logic import (
    JUMP_BACK_MS,
    JUMP_FORWARD_MS,
    KEYS,
    MINI_GAP,
    Track,
    first_geometry,
    first_mini,
)
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
from app.ui import theme

FAR_AWAY = (-30_000, -30_000)  # the pointer, nowhere near the window
WORK = (0, 0, 1920, 1040)


class FakeEngine:
    """EngineSeam without libVLC: records each call; the position, duration and tracks are what
    the test sets. `hang` makes position_ms block until it is set (a stalled network share)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.position = 0
        self.duration = 0
        self.audio: list[Track] = []
        self.subtitles: list[Track] = []
        self.hang: threading.Event | None = None
        self.hanging = threading.Event()
        self._ended: Callable[[], None] | None = None

    def load(self, path: str) -> None:
        self.calls.append(("load", path))

    def play(self) -> None:
        self.calls.append(("play",))

    def pause(self) -> None:
        self.calls.append(("pause",))

    def seek(self, ms: int) -> None:
        self.calls.append(("seek", ms))
        self.position = ms

    def position_ms(self) -> int:
        if self.hang is not None:
            self.hanging.set()
            self.hang.wait()
        return self.position

    def duration_ms(self) -> int:
        return self.duration

    def audio_tracks(self) -> list[Track]:
        return list(self.audio)

    def subtitle_tracks(self) -> list[Track]:
        return list(self.subtitles)

    def set_audio(self, track_id: int) -> None:
        self.calls.append(("set_audio", track_id))

    def set_subtitle(self, track_id: int) -> None:
        self.calls.append(("set_subtitle", track_id))

    def add_subtitle(self, path: str) -> None:
        self.calls.append(("add_subtitle", path))

    def volume(self, level: int) -> None:
        self.calls.append(("volume", level))

    def mute(self, on: bool) -> None:
        self.calls.append(("mute", on))

    def attach(self, hwnd: int) -> None:
        self.calls.append(("attach", hwnd))

    def release(self) -> None:
        self.calls.append(("release",))

    def on_end(self, callback: Callable[[], None]) -> None:
        self._ended = callback

    def end(self) -> None:
        """libVLC's end-of-video event (on a thread of its own)."""
        assert self._ended is not None
        threading.Thread(target=self._ended, daemon=True).start()


class FakeStdin:
    """The app's end of the pipe: lines fed one by one; `close` is its end."""

    def __init__(self) -> None:
        self._lines: queue.Queue[bytes] = queue.Queue()

    def feed(self, message: Message) -> None:
        self._lines.put((encode(message) + "\n").encode("ascii"))

    def close(self) -> None:
        self._lines.put(b"")

    def readline(self) -> bytes:
        return self._lines.get()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def play(**changes: Any) -> Play:
    message = Play(
        file="C:\\Videos\\Lantern Hill\\Lantern.Hill.S01E03.mkv",
        title="Lantern Hill",
        line="S01E03 · The Night Watch",
        start_ms=0,
        ask_resume=False,
        intro=None,
        credits_ms=None,
        next_label=None,
        always_audio="",
        always_subtitles="",
        carry_audio="",
        carry_subtitles="",
    )
    return dataclasses.replace(message, **changes)


class PlayerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()
        self.born = time.monotonic()
        self.folder = Path(tempfile.mkdtemp())
        self.settings = self.folder / "media-player.json"
        self.engine = FakeEngine()
        self.stdin = FakeStdin()
        self.lines: list[str] = []
        self.clock = FakeClock()
        self.pointer = FAR_AWAY
        self.addCleanup(self._end)

    def _end(self) -> None:
        """After each window's own cleanup (`start`): the engine and stdin let go, the root."""
        if self.engine.hang is not None:
            self.engine.hang.set()
        self.stdin.close()
        try:
            # customtkinter sets a new root's icon 200 ms after it opens: not on a dead root.
            while time.monotonic() - self.born < 0.25:
                self.root.update()
                time.sleep(0.01)
            self.root.destroy()
        except tk.TclError:
            pass

    def start(self, **options: Any) -> Any:
        from app.media_player_window import start_player

        window = start_player(
            self.root,
            self.engine,
            Look(),
            stdin=self.stdin,
            send=self.lines.append,
            settings_path=self.settings,
            work_area=lambda: WORK,
            pointer=lambda: self.pointer,
            now=self.clock,
            **options,
        )
        self.addCleanup(self._close, window)
        return window

    def _close(self, window: Any) -> None:
        try:
            if window.winfo_exists():
                window.close()
        except tk.TclError:
            pass

    def pump(self, until: Callable[[], bool], what: str = "", timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if until():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what or until}")

    def wait(self, seconds: float) -> None:
        """Keep Tk running `seconds` (the engine's polls arrive meanwhile)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)

    def sent(self) -> list[Message | None]:
        return [decode(line) for line in self.lines]

    def called(self, *call: Any) -> Callable[[], bool]:
        return lambda: call in self.engine.calls

    def playing(self, window: Any, message: Play | None = None, *, at: int = 0) -> None:
        """Feed `message` and wait until it plays, the window knowing position `at`."""
        self.engine.calls.clear()
        self.engine.position = at
        self.stdin.feed(message or play())
        self.pump(lambda: ("play",) in self.engine.calls and window.position_ms == at, "playing")


class StartTests(PlayerTestCase):
    def test_play_message_loads_and_reports_ready(self) -> None:
        window = self.start()
        self.assertEqual(self.lines[:1], [encode(Ready())])
        message = play()
        self.stdin.feed(message)
        self.pump(self.called("play"), "play")
        calls = self.engine.calls
        self.assertIn(("attach", window.video.winfo_id()), calls)
        self.assertLess(calls.index(("load", message.file)), calls.index(("play",)))
        self.assertLess(calls.index(("attach", window.video.winfo_id())), calls.index(("play",)))
        self.assertEqual(window.title_label.cget("text"), "Lantern Hill")
        self.assertEqual(window.line_label.cget("text"), "S01E03 · The Night Watch")
        self.assertEqual(self.sent().count(Ready()), 1)

    def test_start_position_without_asking(self) -> None:
        window = self.start()
        self.stdin.feed(play(start_ms=95_000))
        self.pump(self.called("play"), "play")
        calls = self.engine.calls
        self.assertLess(calls.index(("seek", 95_000)), calls.index(("play",)))
        self.assertFalse(window.prompt_shown)

    def test_resume_prompt_then_start_over_seeks_zero(self) -> None:
        window = self.start()
        self.stdin.feed(play(start_ms=4_360_000, ask_resume=True))
        self.pump(lambda: window.prompt_shown, "the prompt")
        self.assertEqual(window.resume_button.cget("text"), "Resume from 1:12:40")
        self.assertEqual(window.start_over_button.cget("text"), "Start over")
        self.root.update()
        self.assertNotIn(("play",), self.engine.calls)  # nothing plays before the answer
        window.start_over_button.invoke()
        self.pump(self.called("play"), "play")
        calls = self.engine.calls
        self.assertLess(calls.index(("seek", 0)), calls.index(("play",)))
        self.assertNotIn(("seek", 4_360_000), calls)
        self.assertFalse(window.prompt_shown)

    def test_resume_seeks_to_the_position(self) -> None:
        window = self.start()
        self.stdin.feed(play(start_ms=4_360_000, ask_resume=True))
        self.pump(lambda: window.prompt_shown, "the prompt")
        window.resume_button.invoke()
        self.pump(self.called("play"), "play")
        calls = self.engine.calls
        self.assertLess(calls.index(("seek", 4_360_000)), calls.index(("play",)))

    def test_close_at_the_prompt_keeps_the_place(self) -> None:
        window = self.start()
        self.engine.duration = 5_520_000
        self.engine.position = 0  # libVLC before play: no position
        self.stdin.feed(play(start_ms=4_360_000, ask_resume=True))
        self.pump(lambda: window.prompt_shown, "the prompt")
        polls = len(self.engine.calls)
        deadline = time.monotonic() + 0.8  # several polls of position 0 meanwhile
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(window.position_ms, 4_360_000)
        self.assertGreaterEqual(len(self.engine.calls), polls)
        window.close()
        self.assertEqual(self.sent()[-1], Stopped(ms=4_360_000, duration_ms=5_520_000))
        self.assertFalse(any(isinstance(m, Position) for m in self.sent()))

    def test_new_play_at_the_prompt_keeps_the_place(self) -> None:
        window = self.start()
        self.engine.duration = 5_520_000
        self.engine.position = 0
        self.stdin.feed(play(start_ms=4_360_000, ask_resume=True))
        self.pump(lambda: window.prompt_shown, "the prompt")
        self.pump(lambda: window.remaining_label.cget("text") != "", "the duration")
        second = play(file="C:\\Videos\\Other.mkv", title="Other")
        self.stdin.feed(second)
        self.pump(self.called("load", second.file), "the second video")
        self.assertIn(Stopped(ms=4_360_000, duration_ms=5_520_000), self.sent())
        self.assertNotIn(Stopped(ms=0, duration_ms=5_520_000), self.sent())

    def test_zero_right_after_a_later_start_is_passed_over(self) -> None:
        window = self.start()
        self.engine.duration = 5_520_000
        self.stdin.feed(play(start_ms=95_000))
        self.pump(self.called("play"), "play")
        self.engine.position = 0  # libVLC hasn't applied the start time yet
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(window.position_ms, 95_000)
        self.assertFalse(any(isinstance(m, Position) and m.ms == 0 for m in self.sent()))
        self.engine.position = 96_000
        self.pump(lambda: window.position_ms == 96_000, "the real position")
        self.assertIn(Position(ms=96_000, duration_ms=5_520_000, state="playing"), self.sent())

    def test_zero_is_never_believed_after_a_later_start(self) -> None:
        # A slow open (4K on a sleeping NAS) says 0 for long: never Plex's place.
        window = self.start()
        self.engine.duration = 5_520_000
        self.stdin.feed(play(start_ms=95_000))
        self.pump(self.called("play"), "play")
        self.engine.position = 0
        self.clock.now += 60
        self.wait(0.6)
        self.assertEqual(window.position_ms, 95_000)
        self.clock.now += 600
        self.wait(0.6)
        self.assertEqual(window.position_ms, 95_000)
        self.assertFalse(any(isinstance(m, Position) and m.ms == 0 for m in self.sent()))

    def test_a_failed_open_closes_where_it_would_start(self) -> None:
        window = self.start()
        self.stdin.feed(play(start_ms=95_000))
        self.pump(self.called("play"), "play")
        self.engine.position = 0  # libVLC never opened it: always 0, no length
        self.clock.now += 60
        self.wait(0.6)
        window.close()
        self.assertEqual(self.sent()[-1], Stopped(ms=95_000, duration_ms=0))

    def test_seeking_to_the_start_is_believed(self) -> None:
        window = self.start()
        self.engine.duration = 5_520_000
        self.stdin.feed(play(start_ms=95_000))
        self.pump(self.called("play"), "play")
        self.pump(lambda: window.remaining_label.cget("text") != "", "the duration")
        window.seek_to_fraction(0.0)
        self.pump(self.called("seek", 0), "the seek to the start")
        self.wait(0.6)
        self.assertEqual(window.position_ms, 0)
        self.assertIn(Position(ms=0, duration_ms=5_520_000, state="playing"), self.sent())

    def test_no_prompt_from_the_start(self) -> None:
        window = self.start()
        self.stdin.feed(play(start_ms=0, ask_resume=True))
        self.pump(self.called("play"), "play")
        self.assertFalse(window.prompt_shown)

    def test_always_pick_and_carry_choose_the_tracks(self) -> None:
        window = self.start()
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "dut", "Nederlands")]
        self.engine.subtitles = [Track(3, "eng", "English"), Track(4, "dut", "Dutch")]
        self.stdin.feed(play(always_audio="nl", always_subtitles="off", carry_subtitles="en"))
        self.pump(self.called("set_subtitle", 3), "the carried subtitles")
        self.pump(self.called("set_audio", 2), "Always pick's audio")
        self.assertNotIn(("set_subtitle", -1), self.engine.calls)  # the carry beats "off"
        # Nothing picked by hand: no Tracks line (Always pick isn't a hand pick).
        self.assertFalse(any(isinstance(m, Tracks) for m in self.sent()))
        self.assertTrue(window.winfo_exists())

    def test_sound_given_again_once_it_plays(self) -> None:
        window = self.start()
        self.engine.duration = 60_000
        self.playing(window, at=0)
        self.engine.calls.clear()
        self.engine.position = 1_500
        self.pump(self.called("mute", False), "the sound set again")
        self.assertIn(("volume", 100), self.engine.calls)

    def test_hand_pick_keeps_the_other_carry(self) -> None:
        window = self.start()
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "dut", "Dutch")]
        self.playing(window, play(carry_subtitles="nl"), at=1_000)
        self.pump(lambda: window.audio_tracks != (), "the tracks")
        window.choose_audio(1)
        self.assertEqual(self.sent()[-1], Tracks(audio="en", subtitles="nl"))

    def test_always_pick_off_turns_subtitles_off(self) -> None:
        self.start()
        self.engine.subtitles = [Track(3, "eng", "English")]
        self.stdin.feed(play(always_subtitles="off"))
        self.pump(self.called("set_subtitle", -1), "subtitles off")


class KeyTests(PlayerTestCase):
    def test_keys_drive_the_engine(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "dut", "Dutch")]
        self.engine.subtitles = [Track(3, "eng", "English"), Track(4, "dut", "Dutch")]
        self.playing(window, at=600_000)
        self.pump(lambda: window.audio_tracks != (), "the tracks")
        seen: set[str] = set()

        def press(keysym: str, *call: Any) -> None:
            seen.add(KEYS[keysym])
            window.press(keysym)
            if call:
                self.pump(self.called(*call), f"{keysym} → {call}")

        press("space", "pause")
        self.assertIn(Position(ms=600_000, duration_ms=3_600_000, state="paused"), self.sent())
        self.engine.calls.clear()
        press("space", "play")
        press("Right", "seek", 600_000 + JUMP_FORWARD_MS)
        press("Left", "seek", 600_000 + JUMP_FORWARD_MS - JUMP_BACK_MS)
        self.assertIn(
            Position(ms=620_000, duration_ms=3_600_000, state="playing"), self.sent()
        )
        press("Down", "volume", 95)
        press("Up", "volume", 100)
        press("m", "mute", True)
        press("M", "mute", False)
        press("v", "set_subtitle", 3)  # from the file's default (off) to the first track
        press("V", "set_subtitle", 4)
        press("b", "set_audio", 2)  # from the file's default (the first) to the next
        self.assertIn(Tracks(audio="nl", subtitles="nl"), self.sent())
        press("v", "set_subtitle", -1)  # past the last: off
        self.assertEqual(
            [m for m in self.sent() if isinstance(m, Tracks)][-1], Tracks(audio="nl", subtitles="off")
        )
        press("f")
        self.assertTrue(window.full_screen)
        press("Escape")  # leaves full screen first
        self.assertFalse(window.full_screen)
        self.assertTrue(window.winfo_exists())
        press("F")
        self.assertTrue(window.full_screen)
        press("F")
        self.assertFalse(window.full_screen)
        self.assertEqual(seen, set(KEYS.values()))
        press("Escape")  # in a window: closes
        self.root.update()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self.sent()[-1], Stopped(ms=620_000, duration_ms=3_600_000))

    def test_jumps_stay_inside_the_video(self) -> None:
        window = self.start()
        self.engine.duration = 100_000
        self.playing(window, at=5_000)
        window.press("Left")
        self.pump(self.called("seek", 0), "seek to 0")
        window.press("Right")
        self.pump(self.called("seek", 30_000), "seek 30 s")
        self.engine.position = 90_000
        self.pump(lambda: window.position_ms == 90_000)
        window.press("Right")
        self.pump(self.called("seek", 99_000), "seek to just before the end")

    def test_keys_are_bound_and_double_click_toggles_full_screen(self) -> None:
        window = self.start()
        self.assertTrue(window.bind("<KeyPress>"))
        self.assertTrue(window.overlay.bind("<KeyPress>"))
        self.assertTrue(window.video.bind("<Double-Button-1>"))
        self.root.update()
        for _ in range(2):  # Tk makes the two presses a double click
            window.video.event_generate("<ButtonPress-1>", x=10, y=10)
            window.video.event_generate("<ButtonRelease-1>", x=10, y=10)
        self.root.update()
        self.assertTrue(window.full_screen)
        window.toggle_full_screen()
        self.assertFalse(window.full_screen)


class ControlTests(PlayerTestCase):
    def test_controls_fade_after_idle(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=1_000)
        window.activity()
        window.refresh_controls()
        self.assertTrue(window.controls_visible)
        self.clock.now += 2.9
        window.refresh_controls()
        self.assertTrue(window.controls_visible)
        self.clock.now += 0.2  # 3.1 s idle
        window.refresh_controls()
        self.assertFalse(window.controls_visible)
        window.activity()  # the pointer moves
        window.refresh_controls()
        self.assertTrue(window.controls_visible)
        # The pointer resting on the controls keeps them.
        self.root.update()
        band = window.bottom_band
        self.pointer = (
            band.winfo_rootx() + band.winfo_width() // 2,
            band.winfo_rooty() + band.winfo_height() // 2,
        )
        self.clock.now += 10
        window.refresh_controls()
        self.assertTrue(window.controls_visible)
        self.pointer = FAR_AWAY
        window.refresh_controls()
        self.assertFalse(window.controls_visible)
        # Paused: they stay.
        window.press("space")
        self.clock.now += 10
        window.refresh_controls()
        self.assertTrue(window.controls_visible)

    def test_seek_bar_shows_elapsed_and_remaining(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=754_000)
        self.assertEqual(window.elapsed_label.cget("text"), "12:34")
        self.assertEqual(window.remaining_label.cget("text"), "−47:26")
        window.seek_to_fraction(0.5)
        self.pump(self.called("seek", 1_800_000), "seek to the middle")

    def test_stay_on_top_and_full_screen_buttons(self) -> None:
        window = self.start()
        self.assertFalse(window.on_top)
        window.on_top_button.invoke()
        self.assertTrue(window.on_top)
        self.assertTrue(bool(window.attributes("-topmost")))
        window.full_screen_button.invoke()
        self.assertTrue(window.full_screen)
        self.assertFalse(window.on_top_button.winfo_ismapped())  # window mode only
        window.full_screen_button.invoke()
        self.assertFalse(window.full_screen)

    def test_notice_shows_then_goes(self) -> None:
        window = self.start()
        self.stdin.feed(Notice(text="Plex didn't answer"))
        self.pump(lambda: window.notice_text == "Plex didn't answer", "the notice")
        self.clock.now += 10
        window.refresh_controls()
        self.assertEqual(window.notice_text, "")


class MarkerTests(PlayerTestCase):
    def test_skip_intro_shows_inside_intro_and_seeks_to_its_end(self) -> None:
        window = self.start()
        self.engine.duration = 2_700_000
        self.playing(window, play(intro=(30_000, 95_000)), at=10_000)
        self.assertFalse(window.skip_shown)
        self.engine.position = 30_000
        self.pump(lambda: window.skip_shown, "Skip intro")
        self.assertEqual(window.skip_button.cget("text"), "Skip intro")
        window.skip_button.invoke()
        self.pump(self.called("seek", 95_000), "the seek to the intro's end")
        self.pump(lambda: not window.skip_shown, "Skip intro gone")
        self.assertIn(Position(ms=95_000, duration_ms=2_700_000, state="playing"), self.sent())

    def test_no_skip_intro_without_a_marker(self) -> None:
        window = self.start()
        self.engine.duration = 2_700_000
        self.playing(window, at=40_000)
        self.assertFalse(window.skip_shown)

    def test_countdown_sends_want_next_at_zero_and_cancel_stops_it(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        message = play(next_label="S01E04")
        self.playing(window, message, at=1_700_000)
        self.assertFalse(window.countdown_shown)
        self.engine.position = 1_780_000  # 20 s before the end: no credits marker
        self.pump(lambda: window.countdown_shown, "the countdown")
        self.assertEqual(window.countdown_label.cget("text"), "Next episode S01E04 in 10…")
        self.engine.position = 1_785_000
        self.pump(lambda: window.countdown_label.cget("text").endswith("in 5…"), "5 left")
        self.assertNotIn(WantNext(), self.sent())
        self.engine.position = 1_790_000
        self.pump(lambda: WantNext() in self.sent(), "WantNext")
        self.engine.position = 1_791_000
        self.pump(lambda: window.position_ms == 1_791_000)
        self.assertEqual(self.sent().count(WantNext()), 1)

        # The same with Cancel: no WantNext, not even at the end.
        self.lines.clear()
        self.playing(window, dataclasses.replace(message, credits_ms=1_700_000), at=1_000)
        self.engine.position = 1_700_000  # the credits marker
        self.pump(lambda: window.countdown_shown, "the countdown at the credits")
        window.cancel_button.invoke()
        self.assertFalse(window.countdown_shown)
        self.engine.position = 1_799_000
        self.pump(lambda: window.position_ms == 1_799_000)
        self.engine.end()
        self.pump(lambda: Ended(duration_ms=1_800_000) in self.sent(), "Ended")
        self.assertNotIn(WantNext(), self.sent())
        self.assertFalse(window.countdown_shown)

    def test_play_now_and_the_end_ask_for_the_next(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, play(next_label="S01E04", credits_ms=1_795_000), at=1_795_000)
        self.pump(lambda: window.countdown_shown, "the countdown")
        window.play_now_button.invoke()
        self.assertEqual(self.sent().count(WantNext()), 1)
        # A countdown still running when the video ends: the next one now.
        self.lines.clear()
        self.playing(window, play(next_label="S01E05", credits_ms=1_795_000), at=1_795_000)
        self.pump(lambda: window.countdown_shown, "the second countdown")
        self.engine.end()
        self.pump(lambda: WantNext() in self.sent(), "WantNext at the end")

    def test_no_countdown_without_a_next_episode(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=1_790_000)
        self.assertFalse(window.countdown_shown)


class SubtitleTests(PlayerTestCase):
    def test_find_subtitles_sends_request_and_loads_reply(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=1_000)
        labels = window.subtitle_menu_labels()
        self.assertIn("Find subtitles…", labels)
        self.assertIn("Always pick: The video's own", labels)
        window.find_subtitles("nl")
        self.assertIn(FindSubtitles(language="nl"), self.sent())
        self.assertEqual(window.notice_text, "Looking for subtitles…")
        self.stdin.feed(LoadSubtitle(path="C:\\Videos\\Lantern Hill\\Lantern.Hill.S01E03.nl.srt"))
        self.pump(
            self.called("add_subtitle", "C:\\Videos\\Lantern Hill\\Lantern.Hill.S01E03.nl.srt"),
            "the subtitle loaded",
        )
        self.pump(lambda: Tracks(audio="", subtitles="nl") in self.sent(), "the hand pick")
        self.assertEqual(window.notice_text, "")
        self.stdin.feed(Notice(text="No Dutch subtitles found."))
        self.pump(lambda: window.notice_text == "No Dutch subtitles found.", "the notice")

    def test_no_find_subtitles_for_a_file_from_explorer(self) -> None:
        window = self.start()
        self.playing(window, play(find_subtitles=False), at=1_000)
        labels = window.subtitle_menu_labels()
        self.assertNotIn("Find subtitles…", labels)
        self.assertIn("Always pick: The video's own", labels)

    def test_always_pick_sends_the_choice(self) -> None:
        window = self.start()
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "dut", "Dutch")]
        self.engine.subtitles = [Track(3, "eng", "English"), Track(4, "dut", "Dutch")]
        self.playing(window, at=1_000)
        self.pump(lambda: window.subtitle_tracks != (), "the tracks")
        window.always_pick("subtitles", "nl")
        self.assertIn(AlwaysPick(kind="subtitles", language="nl"), self.sent())
        self.pump(self.called("set_subtitle", 4), "Dutch subtitles now")
        self.assertIn("Always pick: Dutch", window.subtitle_menu_labels())
        window.always_pick("audio", "")
        self.assertIn(AlwaysPick(kind="audio", language=""), self.sent())
        self.assertIn("Always pick: The video's own", window.audio_menu_labels())
        # Always pick is no hand pick: nothing to carry.
        self.assertFalse(any(isinstance(m, Tracks) for m in self.sent()))

    def test_track_menus_list_the_tracks(self) -> None:
        window = self.start()
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "", "")]
        self.engine.subtitles = [Track(3, "dut", "Dutch")]
        self.playing(window, at=1_000)
        self.pump(lambda: window.audio_tracks != (), "the tracks")
        audio = window.audio_menu_labels()
        self.assertIn("English", audio)
        self.assertIn("Track 2", audio)
        subtitles = window.subtitle_menu_labels()
        self.assertEqual(subtitles[:2], ["Off", "Dutch"])
        window.choose_subtitle(-1)
        self.pump(self.called("set_subtitle", -1), "subtitles off")
        self.assertIn(Tracks(audio="", subtitles="off"), self.sent())


class LifeTests(PlayerTestCase):
    def test_new_play_reports_stopped_for_the_old(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=120_000)
        second = play(file="C:\\Videos\\Lantern Hill\\Lantern.Hill.S01E04.mkv", title="Next")
        self.stdin.feed(second)
        self.pump(self.called("load", second.file), "the second video")
        sent = self.sent()
        self.assertIn(Stopped(ms=120_000, duration_ms=1_800_000), sent)
        self.assertEqual(sent.count(Ready()), 1)
        self.assertEqual(window.title_label.cget("text"), "Next")

    def test_position_every_ten_seconds_while_playing(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=1_000)

        def reported() -> list[Position]:
            return [m for m in self.sent() if isinstance(m, Position)]

        before = len(reported())
        self.clock.now += 4
        self.engine.position = 5_000
        self.pump(lambda: window.position_ms == 5_000)
        self.assertEqual(len(reported()), before)
        self.clock.now += 6.5
        self.engine.position = 11_500
        self.pump(lambda: len(reported()) == before + 1, "a position after 10 s")
        self.assertEqual(
            reported()[-1], Position(ms=11_500, duration_ms=1_800_000, state="playing")
        )

    def test_stdin_end_closes(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=42_000)
        self.stdin.close()
        self.pump(lambda: not window.winfo_exists(), "the window closed")
        self.assertEqual(self.sent()[-1], Stopped(ms=42_000, duration_ms=1_800_000))
        self.pump(self.called("release"), "the engine released")

    def test_an_early_end_is_a_pause_where_it_stopped(self) -> None:
        # libVLC's end event part-way (the share or Wi-Fi gone, a cut-off file): not watched.
        window = self.start()
        self.engine.duration = 2_700_000
        message = play(next_label="S01E04")
        self.playing(window, message, at=1_200_000)
        self.engine.end()
        paused = Position(ms=1_200_000, duration_ms=2_700_000, state="paused")
        self.pump(lambda: paused in self.sent(), "a pause where it stopped")
        self.assertFalse(any(isinstance(m, (Ended, WantNext)) for m in self.sent()))
        self.assertTrue(window.paused)
        self.engine.position = 0  # libVLC after its end: nothing it says counts
        self.wait(0.6)
        self.assertEqual(window.position_ms, 1_200_000)
        # Play opens the file again there.
        self.engine.calls.clear()
        window.toggle_pause()
        self.pump(self.called("play"), "playing again")
        calls = self.engine.calls
        self.assertLess(calls.index(("load", message.file)), calls.index(("seek", 1_200_000)))
        self.assertLess(calls.index(("seek", 1_200_000)), calls.index(("play",)))
        self.assertFalse(window.paused)
        self.engine.position = 1_210_000
        self.pump(lambda: window.position_ms == 1_210_000, "playing on")
        window.close()
        self.assertEqual(self.sent()[-1], Stopped(ms=1_210_000, duration_ms=2_700_000))

    def test_an_early_end_played_again_keeps_the_tracks_picked_by_hand(self) -> None:
        # The file opened again has no tracks until libVLC has opened it: a pick sent before then
        # is lost. The hand pick goes again once its fresh track lists come.
        window = self.start()
        self.engine.duration = 2_700_000
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "dut", "Dutch")]
        self.engine.subtitles = [Track(3, "eng", "English"), Track(4, "dut", "Dutch")]
        self.playing(window, at=1_200_000)
        self.pump(lambda: window.subtitle_tracks != (), "the tracks")
        window.choose_subtitle(4)
        window.choose_audio(2)
        self.engine.end()
        self.pump(lambda: window.paused, "paused")
        self.engine.audio, self.engine.subtitles = [], []  # the file being opened again
        self.engine.calls.clear()
        window.toggle_pause()
        self.pump(self.called("play"), "playing again")
        self.wait(0.6)
        self.assertNotIn(("set_subtitle", 4), self.engine.calls)
        self.assertNotIn(("set_audio", 2), self.engine.calls)
        self.assertEqual(window.subtitle_tracks, ())
        self.engine.audio = [Track(1, "eng", "English"), Track(2, "dut", "Dutch")]
        self.engine.subtitles = [Track(3, "eng", "English"), Track(4, "dut", "Dutch")]
        self.pump(self.called("set_subtitle", 4), "Dutch subtitles again")
        self.pump(self.called("set_audio", 2), "Dutch audio again")
        self.assertEqual(len(window.subtitle_tracks), 2)

    def test_an_early_end_closed_is_stopped_where_it_stopped(self) -> None:
        window = self.start()
        self.engine.duration = 2_700_000
        self.playing(window, at=1_200_000)
        self.engine.end()
        self.pump(lambda: window.paused, "paused")
        window.close()
        self.assertEqual(self.sent()[-1], Stopped(ms=1_200_000, duration_ms=2_700_000))
        self.assertFalse(any(isinstance(m, Ended) for m in self.sent()))

    def test_an_end_near_the_end_or_past_the_credits_is_the_end(self) -> None:
        window = self.start()
        self.engine.duration = 2_700_000
        self.playing(window, at=2_600_000)  # within 5 % (135 s) of the end
        self.engine.end()
        self.pump(lambda: Ended(duration_ms=2_700_000) in self.sent(), "Ended")
        self.lines.clear()
        self.playing(window, play(credits_ms=2_300_000), at=2_350_000)  # past the credits
        self.engine.end()
        self.pump(lambda: Ended(duration_ms=2_700_000) in self.sent(), "Ended after the credits")

    def test_ended_is_reported_once_and_no_stopped_after_it(self) -> None:
        window = self.start()
        self.engine.duration = 60_000
        self.playing(window, at=59_000)
        self.engine.end()
        self.pump(lambda: Ended(duration_ms=60_000) in self.sent(), "Ended")
        self.assertTrue(window.paused)
        window.close()
        self.assertFalse(any(isinstance(m, Stopped) for m in self.sent()))

    def test_geometry_and_stay_on_top_are_remembered(self) -> None:
        window = self.start()
        x, y, width, height = first_geometry(WORK)
        scale = ctk.ScalingTracker.get_window_scaling(window)
        self.root.update()
        self.assertEqual(window.winfo_width(), width)
        self.assertEqual(window.winfo_height(), height)
        window.on_top_button.invoke()
        window.geometry(f"{round(800 / scale)}x{round(450 / scale)}+120+80")
        self.root.update()
        window.close()
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertTrue(saved["on_top"])
        self.assertEqual(saved["geometry"], [120, 80, round(800 / scale), round(450 / scale)])
        again = self.start()
        self.root.update()
        self.assertTrue(again.on_top)
        self.assertEqual((again.winfo_width(), again.winfo_height()), (800, 450))
        self.assertTrue(again.geometry().endswith("+120+80"))
        del x, y

    def test_closing_the_controls_window_closes_the_player(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=42_000)
        handler = str(window.overlay.protocol("WM_DELETE_WINDOW"))
        self.assertTrue(handler)
        window.overlay.tk.eval(handler)  # Alt+F4 on the controls
        self.root.update()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self.sent()[-1], Stopped(ms=42_000, duration_ms=1_800_000))

    def test_controls_window_gone_closes_the_player(self) -> None:
        window = self.start()
        self.engine.duration = 1_800_000
        self.playing(window, at=42_000)
        window.overlay.destroy()
        self.pump(lambda: not window.winfo_exists(), "the player closed")
        self.assertEqual(self.sent()[-1], Stopped(ms=42_000, duration_ms=1_800_000))
        self.pump(self.called("release"), "the engine released")

    def test_a_broken_settings_file_starts_afresh(self) -> None:
        self.settings.write_text("{not json", encoding="utf-8")
        window = self.start()
        self.assertFalse(window.on_top)

    def test_close_while_engine_blocks(self) -> None:
        from app.media_player_window import PlayerWindow, run_player

        self.engine.duration = 1_800_000
        self.engine.hang = threading.Event()
        self.stdin.feed(play())
        timing: dict[str, float] = {}

        def close_when_hanging() -> None:
            if not self.engine.hanging.is_set():
                self.root.after(20, close_when_hanging)
                return
            window = next(w for w in self.root.winfo_children() if isinstance(w, PlayerWindow))
            timing["clicked"] = time.monotonic()
            window.close_button.invoke()

        self.root.after(50, close_when_hanging)
        code = run_player(
            self.root,
            self.engine,
            Look(),
            stdin=self.stdin,
            send=self.lines.append,
            settings_path=self.settings,
            work_area=lambda: WORK,
            pointer=lambda: FAR_AWAY,
        )
        self.assertLess(time.monotonic() - timing["clicked"], 1.0)
        self.assertEqual(code, EXIT_CLOSED)
        self.assertEqual(self.sent()[-1], Stopped(ms=0, duration_ms=0))


class MiniTests(PlayerTestCase):
    """The mini player: a small always-on-top window in a corner (Ctrl+H)."""

    def ctrl_h(self, window: Any) -> None:
        self.assertEqual(window._on_key(SimpleNamespace(keysym="h", state=0x4)), "break")
        self.root.update()

    def place(self, window: Any) -> tuple[int, int, int, int]:
        """The window's x, y (physical) and its width, height (physical)."""
        self.root.update()
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", window.geometry())
        assert match is not None
        return (int(match[3]), int(match[4]), window.winfo_width(), window.winfo_height())

    def assertNear(self, got: tuple[int, ...], wanted: tuple[int, ...]) -> None:
        """Equal within a pixel (customtkinter rounds sizes through its units)."""
        self.assertEqual(len(got), len(wanted))
        for a, b in zip(got, wanted, strict=True):
            self.assertLessEqual(abs(a - b), 1, f"{got} != {wanted}")

    def scale(self, window: Any) -> float:
        return float(ctk.ScalingTracker.get_window_scaling(window))

    def test_ctrl_h_toggles_mini(self) -> None:
        window = self.start()
        normal = self.place(window)
        self.assertFalse(window.mini)
        self.ctrl_h(window)
        self.assertTrue(window.mini)
        self.assertNear(self.place(window), first_mini(WORK, self.scale(window)))
        self.assertTrue(bool(window.attributes("-topmost")))
        self.assertTrue(bool(window.overlay.attributes("-topmost")))
        self.ctrl_h(window)
        self.assertFalse(window.mini)
        self.assertNear(self.place(window), normal)
        self.assertFalse(bool(window.attributes("-topmost")))  # the pin is off

    def test_leaving_mini_keeps_the_pin(self) -> None:
        window = self.start()
        window.on_top_button.invoke()
        self.ctrl_h(window)
        self.ctrl_h(window)
        self.assertTrue(bool(window.attributes("-topmost")))

    def test_mini_button_and_escape(self) -> None:
        window = self.start()
        window.mini_button.invoke()
        self.assertTrue(window.mini)
        window.press("Escape")  # back to the window, not closed
        self.root.update()
        self.assertFalse(window.mini)
        self.assertTrue(window.winfo_exists())

    def test_ctrl_h_in_full_screen_goes_mini(self) -> None:
        window = self.start()
        window.toggle_full_screen()
        self.ctrl_h(window)
        self.assertFalse(window.full_screen)
        self.assertTrue(window.mini)

    def test_f_in_mini_goes_full_screen_and_back_to_the_window(self) -> None:
        window = self.start()
        normal = self.place(window)
        self.ctrl_h(window)
        window.press("f")
        self.root.update()
        self.assertTrue(window.full_screen)
        self.assertFalse(window.mini)
        window.press("Escape")
        self.assertFalse(window.full_screen)
        self.assertNear(self.place(window), normal)

    def test_mini_place_is_remembered_apart(self) -> None:
        window = self.start()
        normal = self.place(window)
        self.ctrl_h(window)
        scale = self.scale(window)
        width, height = round(600 / scale), round(338 / scale)
        window.geometry(f"{width}x{height}+300+200")
        self.root.update()
        window.close()
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(saved["geometry"][:2], list(normal[:2]))
        self.assertEqual(saved["mini"], [300, 200, width, height])
        again = self.start()
        self.assertFalse(again.mini)
        self.assertEqual(self.place(again)[:2], normal[:2])
        self.ctrl_h(again)
        self.assertEqual(self.place(again)[:2], (300, 200))

    def test_mini_place_off_screen_starts_in_the_corner(self) -> None:
        self.settings.write_text(json.dumps({"mini": [-5000, -5000, 480, 270]}), encoding="utf-8")
        window = self.start(on_screen=lambda rect: rect[0] >= 0)
        self.ctrl_h(window)
        self.assertNear(self.place(window), first_mini(WORK, self.scale(window)))

    def mapped(self, *widgets: Any) -> list[bool]:
        self.root.update()
        return [bool(widget.winfo_ismapped()) for widget in widgets]

    def test_mini_shows_its_own_controls(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=1_000)
        mini = (window.mini_play_button, window.mini_window_button, window.mini_close_button,
                window.mini_strip)
        bands = (window.top_band, window.bottom_band)
        self.ctrl_h(window)
        window.refresh_controls()
        self.assertEqual(self.mapped(*mini, *bands), [True] * 4 + [False] * 2)
        self.clock.now += 3.1  # idle while playing: they fade
        window.refresh_controls()
        self.assertEqual(self.mapped(*mini), [False] * 4)
        window.activity()
        self.ctrl_h(window)  # back to the window: its bands
        window.refresh_controls()
        self.assertEqual(self.mapped(*mini, *bands), [False] * 4 + [True] * 2)

    def test_pointer_on_the_mini_controls_keeps_them(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=1_000)
        self.ctrl_h(window)
        self.root.update()
        strip = window.mini_strip
        self.pointer = (
            strip.winfo_rootx() + strip.winfo_width() // 2,
            strip.winfo_rooty() + strip.winfo_height() // 2,
        )
        self.clock.now += 10
        window.refresh_controls()
        self.assertTrue(window.controls_visible)

    def test_mini_play_and_seek(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=754_000)
        self.ctrl_h(window)
        self.assertEqual(window.mini_time_label.cget("text"), "12:34 / 1:00:00")
        window.mini_play_button.invoke()
        self.assertTrue(window.paused)
        self.root.update()
        bar = window.mini_seek_bar
        middle = bar.winfo_width() // 2
        bar.event_generate("<ButtonPress-1>", x=middle, y=4)
        bar.event_generate("<ButtonRelease-1>", x=middle, y=4)
        self.pump(
            lambda: any(
                call[0] == "seek" and abs(call[1] - 1_800_000) < 90_000
                for call in self.engine.calls
            ),
            "a seek to the middle",
        )

    def test_mini_window_and_close_buttons(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=42_000)
        self.ctrl_h(window)
        window.mini_window_button.invoke()
        self.assertFalse(window.mini)
        self.ctrl_h(window)
        window.mini_close_button.invoke()
        self.root.update()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self.sent()[-1], Stopped(ms=42_000, duration_ms=3_600_000))

    def test_next_video_stays_mini(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, at=1_000)
        self.ctrl_h(window)
        second = play(file="C:\\Videos\\Lantern Hill\\Lantern.Hill.S01E04.mkv", title="Next")
        self.stdin.feed(second)
        self.pump(self.called("load", second.file), "the second video")
        window.activity()
        self.assertTrue(window.mini)
        self.assertEqual(self.mapped(window.mini_strip, window.top_band), [True, False])

    def test_skip_intro_shows_in_mini(self) -> None:
        window = self.start()
        self.engine.duration = 3_600_000
        self.playing(window, play(intro=(0, 90_000)), at=10_000)
        self.ctrl_h(window)
        self.pump(lambda: window.skip_shown, "Skip intro")
        self.assertEqual(self.mapped(window.skip_button), [True])

    def test_tuck_into_the_corner(self) -> None:
        window = self.start()
        self.ctrl_h(window)
        gap = round(MINI_GAP * self.scale(window))
        window.geometry(f"+{5 + gap}+{5 + gap}")  # dropped just off the top-left corner
        self.root.update()
        window.tuck()
        self.root.update()
        self.assertEqual((window.video.winfo_rootx(), window.video.winfo_rooty()), (gap, gap))
        window.geometry("+600+400")  # in the middle: stays
        self.root.update()
        where = (window.video.winfo_rootx(), window.video.winfo_rooty())
        window.tuck()
        self.root.update()
        self.assertEqual((window.video.winfo_rootx(), window.video.winfo_rooty()), where)

    def resized(self, window: Any, width: int, height: int) -> None:
        scale = self.scale(window)
        window.geometry(f"{round(width / scale)}x{round(height / scale)}")
        self.root.update()

    def test_shape_back_to_16_9(self) -> None:
        window = self.start(button_down=lambda: False)
        self.ctrl_h(window)
        self.resized(window, 900, 400)  # above MINI_LEAST at 150 % too (600 x 337)
        window.reshape()
        self.root.update()
        self.assertNear((window.winfo_width(), window.winfo_height()), (900, 506))

    def test_shape_waits_for_the_button(self) -> None:
        down = [True]
        window = self.start(button_down=lambda: down[0])
        self.ctrl_h(window)
        self.resized(window, 900, 400)
        window.reshape()
        self.root.update()
        self.assertNear((window.winfo_width(), window.winfo_height()), (900, 400))
        down[0] = False
        self.pump(lambda: abs(window.winfo_height() - 506) <= 1, "the shape once let go")

    def test_wheel_turns_the_volume(self) -> None:
        window = self.start()
        self.assertTrue(window.bind("<MouseWheel>"))
        self.assertTrue(window.overlay.bind("<MouseWheel>"))
        window.set_volume(50)
        window._on_wheel(SimpleNamespace(delta=120))
        self.assertEqual(window.volume, 55)
        window._on_wheel(SimpleNamespace(delta=-240))
        self.assertEqual(window.volume, 50)

    def test_double_click_in_mini_goes_back(self) -> None:
        window = self.start()
        self.ctrl_h(window)
        window._on_double(None)
        self.assertFalse(window.mini)
        self.assertFalse(window.full_screen)


@unittest.skipUnless(sys.platform == "win32", "Windows' owned windows")
class OverlayAboveTests(unittest.TestCase):
    """Tk gives the player a new wrapper when full screen switches, drawn on top; the overlay
    owned again by it (`_own`) must be put back above it, or the controls hide behind."""

    def test_owned_again_the_overlay_is_above_its_owner(self) -> None:
        from app.media_player_window import _own, _wrapper

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()
        owner = tk.Toplevel(root)
        owner.geometry("300x200+50+50")
        overlay = tk.Toplevel(root)
        overlay.overrideredirect(True)
        overlay.geometry("300x200+50+50")
        root.update()
        owner.lift()
        root.update()
        user32 = ctypes.WinDLL("user32")
        user32.GetWindow.restype = ctypes.c_void_p
        user32.GetWindow.argtypes = (ctypes.c_void_p, ctypes.c_uint)

        def above(upper: int, lower: int) -> bool:
            hwnd = user32.GetWindow(lower, 3)  # GW_HWNDPREV
            while hwnd:
                if hwnd == upper:
                    return True
                hwnd = user32.GetWindow(hwnd, 3)
            return False

        self.assertFalse(above(_wrapper(overlay), _wrapper(owner)))
        _own(overlay, owner)
        self.assertTrue(above(_wrapper(overlay), _wrapper(owner)))


@unittest.skipUnless(sys.platform == "win32", "Windows' owned windows")
class OverlayFollowsTests(PlayerTestCase):
    """The overlay owned again (`_own`) and moved in one `_follow`: putting it above its owner
    must not undo the move Tk still has to make (the controls stayed where full screen had
    put them after the mini player, full screen and back)."""

    def test_after_mini_full_screen_and_back_the_overlay_is_on_the_video(self) -> None:
        window = self.start()
        window._win32 = True  # the real owning, on InertNative's window
        self.wait(0.3)
        for _ in range(6):
            window.toggle_mini()
            self.wait(0.3)
            window.toggle_full_screen()
            self.wait(0.3)
            window.toggle_full_screen()
            self.wait(0.3)
            video = window.video
            self.assertEqual(
                (window.overlay.winfo_rootx(), window.overlay.winfo_rooty()),
                (video.winfo_rootx(), video.winfo_rooty()),
            )


class OptionTests(unittest.TestCase):
    def test_colours_checked_and_texts_passed(self) -> None:
        options = parse_options(
            ["--accent", "#123456", "--surface", "red", "--resume", "Hervatten vanaf {time}"]
        )
        assert options is not None
        self.assertEqual(options.look.accent, "#123456")
        self.assertEqual(options.look.surface, Look().surface)  # not #rrggbb: the default
        self.assertEqual(options.look.text("resume", time="1:00"), "Hervatten vanaf 1:00")
        self.assertEqual(options.look.text("start_over"), "Start over")
        self.assertIsNone(options.engine_folder)
        engine = parse_options(["--engine", "C:\\Program Files\\VideoLAN\\VLC"])
        assert engine is not None
        self.assertEqual(engine.engine_folder, "C:\\Program Files\\VideoLAN\\VLC")

    def test_text_arguments_round_trip(self) -> None:
        arguments = text_arguments(str.upper)
        options = parse_options(arguments)
        assert options is not None
        for name, english in TEXTS.items():
            self.assertEqual(options.look.text(name), english.upper())

    def test_braces_in_a_text_never_break_it(self) -> None:
        options = parse_options(["--next-episode", "{oops} {label} {n}"])
        assert options is not None
        self.assertEqual(options.look.text("next_episode", label="S01E04", n=3), "{oops} S01E04 3")

    def test_default_colours_are_the_themes_dark_halves(self) -> None:
        look = Look()
        for name, token in (
            ("background", theme.BG),
            ("surface", theme.SURFACE),
            ("accent", theme.ACCENT),
            ("accent_hover", theme.ACCENT_HOVER),
            ("foreground", theme.TEXT),
            ("muted", theme.TEXT_MUTED),
        ):
            self.assertEqual(getattr(look, name).lower(), theme.pick(token, dark=True).lower())

    def test_exit_codes(self) -> None:
        self.assertEqual(EXIT_CLOSED, 0)
        self.assertEqual(EXIT_NO_ENGINE, 2)
        self.assertIn(EXIT_NO_ENGINE, EXIT_CODES)

    def test_a_binding_that_exits_means_no_engine(self) -> None:
        import builtins
        from unittest import mock

        real_import = builtins.__import__

        def exiting_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "vlc":
                raise SystemExit(1)  # python-vlc's find_lib without a libvlc
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", exiting_import):
            with self.assertRaises(OSError):
                media_player.VlcEngine.start(None)
            with mock.patch.object(media_player, "_quiet_output", lambda: lambda _line: None):
                self.assertEqual(media_player.main([]), EXIT_NO_ENGINE)

    def test_main_keeps_the_cycle_collector_on_tks_thread(self) -> None:
        # D6: the stdin reader and the engine's thread must never collect a Tk object.
        from unittest import mock

        root = mock.Mock(name="root")
        order: list[str] = []

        def run_player(master: Any, *_args: Any, **_kwargs: Any) -> int:
            self.assertIs(master, root)
            order.append("run")
            return EXIT_CLOSED

        def guard(master: Any) -> None:
            self.assertIs(master, root)
            order.append("guard")

        with (
            mock.patch.object(media_player, "_quiet_output", lambda: lambda _line: None),
            mock.patch.object(media_player.VlcEngine, "start", return_value=FakeEngine()),
            mock.patch.object(media_player, "settings_path", return_value=Path("unused.json")),
            mock.patch("app.identity.set_process_app_id"),
            mock.patch("app.ui.theme.apply_appearance"),
            mock.patch("app.ui.branding.set_window_icon"),
            mock.patch("customtkinter.CTk", return_value=root),
            mock.patch("app.ui.gc_guard.start", side_effect=guard),
            mock.patch("app.media_player_window.run_player", side_effect=run_player),
        ):
            self.assertEqual(media_player.main(["--engine", "C:\\VLC"]), EXIT_CLOSED)
        self.assertEqual(order, ["guard", "run"])

    def test_main_is_dpi_aware_before_tk_measures_the_screen(self) -> None:
        # Tk keeps the screen's size from its start: before Windows' scaling is known it is the
        # scaled-down size (1707×1067 for 2560×1600 at 150 %), and full screen covered only that.
        from unittest import mock

        order: list[str] = []

        def make_root() -> Any:
            order.append("tk")
            return mock.Mock(name="root")

        with (
            mock.patch.object(media_player, "_quiet_output", lambda: lambda _line: None),
            mock.patch.object(media_player.VlcEngine, "start", return_value=FakeEngine()),
            mock.patch.object(media_player, "settings_path", return_value=Path("unused.json")),
            mock.patch.object(media_player, "dpi_aware", lambda: order.append("dpi")),
            mock.patch("app.identity.set_process_app_id"),
            mock.patch("app.ui.theme.apply_appearance"),
            mock.patch("app.ui.branding.set_window_icon"),
            mock.patch("customtkinter.CTk", side_effect=make_root),
            mock.patch("app.ui.gc_guard.start"),
            mock.patch("app.media_player_window.run_player", return_value=EXIT_CLOSED),
        ):
            self.assertEqual(media_player.main(["--engine", "C:\\VLC"]), EXIT_CLOSED)
        self.assertEqual(order, ["dpi", "tk"])

    def test_entry_module_is_stdlib_only_at_import(self) -> None:
        import ast
        import sys

        tree = ast.parse(Path(media_player.__file__).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        names -= {"app", "__future__"}
        self.assertEqual(names - set(sys.stdlib_module_names), set())
        app_modules = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app")
        }
        self.assertLessEqual(
            app_modules, {"app.i18n", "app.media_protocol", "app.media_player_logic"}
        )


if __name__ == "__main__":
    unittest.main()
