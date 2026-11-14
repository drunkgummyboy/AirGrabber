"""The messages between the app and its video player (app/media_protocol.py)."""

from __future__ import annotations

import dataclasses
import json
import unittest

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


def _play(**changes: object) -> Play:
    play = Play(
        file="C:\\Films\\Dune (2021)\\Dune.mkv",
        title="Dune",
        line="2021 · 2 h 35 min",
        start_ms=4_360_000,
        ask_resume=True,
        intro=(30_000, 95_000),
        credits_ms=9_000_000,
        next_label="S01E04",
        always_audio="en",
        always_subtitles="nl",
        carry_audio="",
        carry_subtitles="off",
    )
    return dataclasses.replace(play, **changes)  # type: ignore[arg-type]


ALL: tuple[Message, ...] = (
    _play(),
    _play(intro=None, credits_ms=None, next_label=None, ask_resume=False),
    _play(find_subtitles=False),  # a file from Explorer: no Find subtitles
    LoadSubtitle(path="C:\\Films\\Dune.nl.srt"),
    Notice(text="Subtitles not found"),
    Ready(),
    Position(ms=12_000, duration_ms=9_300_000, state="playing"),
    Position(ms=0, duration_ms=0, state="paused"),
    Stopped(ms=5_000, duration_ms=9_300_000),
    Ended(duration_ms=9_300_000),
    WantNext(),
    FindSubtitles(language="nl"),
    Tracks(audio="en", subtitles="off"),
    AlwaysPick(kind="subtitles", language="off"),
    AlwaysPick(kind="audio", language=""),
)


class RoundTripTests(unittest.TestCase):
    def test_every_message_round_trips(self) -> None:
        for message in ALL:
            with self.subTest(message=type(message).__name__):
                self.assertEqual(decode(encode(message)), message)

    def test_encode_is_one_line_of_json_with_a_type(self) -> None:
        for message in ALL:
            with self.subTest(message=type(message).__name__):
                line = encode(message)
                self.assertNotIn("\n", line)
                self.assertNotIn("\r", line)
                self.assertIsInstance(json.loads(line)["type"], str)

    def test_non_ascii_and_newlines_stay_on_one_line(self) -> None:
        message = Notice(text="Ondertitels niet gevonden ✗\nnext line")
        line = encode(message)
        self.assertNotIn("\n", line)
        self.assertEqual(decode(line), message)

    def test_decode_ignores_surrounding_whitespace(self) -> None:
        self.assertEqual(decode(encode(Ready()) + "\r\n"), Ready())

    def test_intro_comes_back_as_a_tuple(self) -> None:
        decoded = decode(encode(_play()))
        assert isinstance(decoded, Play)
        self.assertEqual(decoded.intro, (30_000, 95_000))

    def test_messages_are_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Ready().__setattr__("x", 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(Tracks(audio="en", subtitles=""), "audio", "nl")


class DecodeRefusesTests(unittest.TestCase):
    def test_decode_garbage_is_none(self) -> None:
        for line in (
            "",
            "   ",
            "ready",
            "{",
            "[]",
            "null",
            "42",
            '"play"',
            "{}",
            '{"type": "launch_missiles"}',
            '{"type": 3}',
            '{"type": null}',
            "[" * 100_000,
            "\x00\xff",
        ):
            with self.subTest(line=line[:20]):
                self.assertIsNone(decode(line))

    def test_a_play_without_find_subtitles_has_it(self) -> None:
        line = json.loads(encode(_play(find_subtitles=False)))
        del line["find_subtitles"]
        decoded = decode(json.dumps(line))
        assert isinstance(decoded, Play)
        self.assertTrue(decoded.find_subtitles)

    def test_missing_field_is_none(self) -> None:
        self.assertIsNone(decode('{"type": "position", "ms": 1, "duration_ms": 2}'))
        self.assertIsNone(decode('{"type": "load_subtitle"}'))

    def test_wrong_field_types_are_none(self) -> None:
        good = json.loads(encode(_play()))
        for field, value in (
            ("file", 3),
            ("title", None),
            ("start_ms", "12"),
            ("start_ms", 1.5),
            ("start_ms", True),
            ("ask_resume", 1),
            ("intro", [1]),
            ("intro", [1, 2, 3]),
            ("intro", ["1", 2]),
            ("intro", [True, 2]),
            ("intro", "1-2"),
            ("credits_ms", "end"),
            ("next_label", 4),
            ("always_audio", None),
            ("carry_subtitles", []),
        ):
            with self.subTest(field=field, value=value):
                self.assertIsNone(decode(json.dumps({**good, field: value})))

    def test_always_pick_kind_must_be_audio_or_subtitles(self) -> None:
        line = json.dumps({"type": "always_pick", "kind": "video", "language": "nl"})
        self.assertIsNone(decode(line))

    def test_position_state_must_be_playing_or_paused(self) -> None:
        line = '{"type": "position", "ms": 1, "duration_ms": 2, "state": "stopped"}'
        self.assertIsNone(decode(line))

    def test_extra_fields_are_ignored(self) -> None:
        line = '{"type": "ended", "duration_ms": 5, "later": "field"}'
        self.assertEqual(decode(line), Ended(duration_ms=5))


class SecurityTests(unittest.TestCase):
    def test_play_message_has_no_secret_fields(self) -> None:
        names = [field.name for field in dataclasses.fields(Play)]
        self.assertEqual(
            names,
            [
                "file",
                "title",
                "line",
                "start_ms",
                "ask_resume",
                "intro",
                "credits_ms",
                "next_label",
                "always_audio",
                "always_subtitles",
                "carry_audio",
                "carry_subtitles",
                "find_subtitles",
            ],
        )
        for name in names:
            self.assertNotIn("token", name)
            self.assertNotIn("url", name)
        self.assertNotIn("token", encode(_play()).lower())


if __name__ == "__main__":
    unittest.main()
