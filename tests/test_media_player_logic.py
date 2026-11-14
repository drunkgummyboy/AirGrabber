"""The video player's pure logic (app/media_player_logic.py): no Tk, no VLC."""

from __future__ import annotations

import unittest

from app.media_player_logic import (
    KEYS,
    Track,
    clock,
    controls_shown,
    countdown_from,
    first_geometry,
    first_mini,
    intro_button,
    is_mini_key,
    mini_shape,
    pick_track,
    played_to_the_end,
    tucked,
)


class ClockTests(unittest.TestCase):
    def test_clock_formats(self) -> None:
        self.assertEqual(clock(4_360_000), "1:12:40")
        self.assertEqual(clock(245_000), "4:05")
        self.assertEqual(clock(0), "0:00")
        self.assertEqual(clock(59_999), "0:59")
        self.assertEqual(clock(3_600_000), "1:00:00")
        self.assertEqual(clock(36_000_000), "10:00:00")

    def test_clock_never_negative(self) -> None:
        self.assertEqual(clock(-1), "0:00")
        self.assertEqual(clock(-90_000), "0:00")


class ControlsShownTests(unittest.TestCase):
    def test_hidden_only_when_idle_playing_and_off_the_controls(self) -> None:
        self.assertFalse(controls_shown(3.0, paused=False, pointer_on_controls=False))
        self.assertFalse(controls_shown(10.0, paused=False, pointer_on_controls=False))

    def test_shown_otherwise(self) -> None:
        self.assertTrue(controls_shown(2.9, paused=False, pointer_on_controls=False))
        self.assertTrue(controls_shown(0.0, paused=False, pointer_on_controls=False))
        self.assertTrue(controls_shown(30.0, paused=True, pointer_on_controls=False))
        self.assertTrue(controls_shown(30.0, paused=False, pointer_on_controls=True))


class PickTrackTests(unittest.TestCase):
    TRACKS = (
        Track(1, "en", "English"),
        Track(2, "nl", "Nederlands"),
        Track(3, "de", ""),
    )

    def test_always_pick_missing_language_keeps_default(self) -> None:
        self.assertIsNone(pick_track([Track(1, "en", "")], "nl", ""))

    def test_rule_picks_its_language(self) -> None:
        self.assertEqual(pick_track(self.TRACKS, "nl", ""), 2)

    def test_no_rule_no_carry_keeps_default(self) -> None:
        self.assertIsNone(pick_track(self.TRACKS, "", ""))
        self.assertIsNone(pick_track([], "", ""))

    def test_carried_beats_rule(self) -> None:
        self.assertEqual(pick_track(self.TRACKS, "nl", "de"), 3)

    def test_carried_missing_falls_back_to_rule(self) -> None:
        self.assertEqual(pick_track(self.TRACKS, "nl", "fr"), 2)

    def test_subtitles_off_rule(self) -> None:
        self.assertEqual(pick_track(self.TRACKS, "off", "", subtitles=True), -1)
        self.assertEqual(pick_track([], "Off", "", subtitles=True), -1)

    def test_carried_off_beats_rule(self) -> None:
        self.assertEqual(pick_track(self.TRACKS, "nl", "off", subtitles=True), -1)

    def test_carried_language_beats_off_rule(self) -> None:
        self.assertEqual(pick_track(self.TRACKS, "off", "en", subtitles=True), 1)

    def test_audio_off_keeps_default(self) -> None:
        self.assertIsNone(pick_track(self.TRACKS, "off", ""))
        self.assertIsNone(pick_track(self.TRACKS, "", "off"))
        self.assertEqual(pick_track(self.TRACKS, "nl", "off"), 2)

    def test_first_matching_track_wins(self) -> None:
        tracks = (Track(4, "en", "Commentary"), Track(5, "en", "Main"))
        self.assertEqual(pick_track(tracks, "en", ""), 4)

    def test_three_letter_codes_and_names_match(self) -> None:
        self.assertEqual(pick_track([Track(7, "dut", "")], "nl", ""), 7)
        self.assertEqual(pick_track([Track(7, "nld", "")], "nl", ""), 7)
        self.assertEqual(pick_track([Track(8, "ENG", "")], "en", ""), 8)
        self.assertEqual(pick_track([Track(9, "English", "")], "en", ""), 9)
        self.assertEqual(pick_track([Track(9, "en-US", "")], "en", ""), 9)

    def test_name_used_when_language_unknown(self) -> None:
        tracks = (Track(1, "", "Track 1 - [English]"), Track(2, "", "Track 2 - [Dutch]"))
        self.assertEqual(pick_track(tracks, "nl", ""), 2)

    def test_bracket_at_the_end_wins_over_words_in_the_title(self) -> None:
        tracks = (
            Track(1, "", "Ondertitels voor doven en slechthorenden - [Dutch]"),
            Track(2, "", "Track 2 - [English]"),
        )
        self.assertEqual(pick_track(tracks, "en", ""), 2)
        self.assertEqual(pick_track(tracks, "nl", "", subtitles=True), 1)

    def test_bracket_with_a_code_or_more_words(self) -> None:
        self.assertEqual(pick_track([Track(1, "", "Track 1 - [en]")], "en", ""), 1)
        self.assertEqual(pick_track([Track(1, "", "Track 1 - [English SDH]")], "en", ""), 1)

    def test_codes_in_a_name_are_ordinary_words(self) -> None:
        self.assertIsNone(pick_track([Track(3, "", "Commentary - it is fun")], "it", ""))
        dutch_title = Track(3, "", "Ondertitels voor doven en slechthorenden")
        self.assertIsNone(pick_track([dutch_title], "en", ""))
        self.assertIsNone(pick_track([Track(3, "", "Meer dan woorden")], "da", ""))

    def test_full_name_in_a_name_without_bracket(self) -> None:
        self.assertEqual(pick_track([Track(5, "", "Italian commentary")], "it", ""), 5)

    def test_other_languages_do_not_match(self) -> None:
        self.assertIsNone(pick_track([Track(1, "eng", "English")], "nl", ""))
        self.assertIsNone(pick_track([Track(1, "", "")], "nl", ""))


class IntroButtonTests(unittest.TestCase):
    def test_shown_during_the_intro(self) -> None:
        self.assertTrue(intro_button(30_000, (30_000, 95_000)))
        self.assertTrue(intro_button(60_000, (30_000, 95_000)))

    def test_not_shown_outside_it(self) -> None:
        self.assertFalse(intro_button(29_999, (30_000, 95_000)))
        self.assertFalse(intro_button(95_000, (30_000, 95_000)))
        self.assertFalse(intro_button(200_000, (30_000, 95_000)))

    def test_not_shown_without_a_marker(self) -> None:
        self.assertFalse(intro_button(60_000, None))


class CountdownTests(unittest.TestCase):
    def test_countdown_without_credits(self) -> None:
        self.assertEqual(countdown_from(3_000_000, None), 2_980_000)

    def test_countdown_from_credits(self) -> None:
        self.assertEqual(countdown_from(3_000_000, 2_850_000), 2_850_000)

    def test_countdown_of_a_very_short_video(self) -> None:
        self.assertEqual(countdown_from(5_000, None), 0)

    def test_credits_outside_the_video_are_passed_over(self) -> None:
        self.assertEqual(countdown_from(3_000_000, -5_000), 2_980_000)
        self.assertEqual(countdown_from(3_000_000, 3_000_000), 2_980_000)
        self.assertEqual(countdown_from(3_000_000, 9_000_000), 2_980_000)
        self.assertEqual(countdown_from(0, 2_850_000), 0)  # the length unknown yet


class EndTests(unittest.TestCase):
    """libVLC's end event also comes when a read fails part-way: only near the end is it one."""

    def test_within_a_minute_of_the_end(self) -> None:
        self.assertTrue(played_to_the_end(545_000, 600_000, None))  # 5 % would be 30 s
        self.assertTrue(played_to_the_end(540_000, 600_000, None))
        self.assertFalse(played_to_the_end(535_000, 600_000, None))

    def test_within_five_percent_of_a_long_video(self) -> None:
        self.assertTrue(played_to_the_end(9_600_000, 10_000_000, None))  # 400 s before
        self.assertFalse(played_to_the_end(9_400_000, 10_000_000, None))

    def test_past_the_credits(self) -> None:
        self.assertTrue(played_to_the_end(2_450_000, 2_700_000, 2_400_000))
        self.assertFalse(played_to_the_end(2_350_000, 2_700_000, 2_400_000))

    def test_part_way_is_no_end(self) -> None:
        self.assertFalse(played_to_the_end(1_200_000, 2_700_000, None))
        self.assertFalse(played_to_the_end(0, 2_700_000, None))

    def test_unknown_length_is_no_end(self) -> None:
        self.assertFalse(played_to_the_end(1_200_000, 0, None))
        self.assertFalse(played_to_the_end(0, 0, 2_400_000))


class KeysTests(unittest.TestCase):
    def test_keys(self) -> None:
        expected = {
            "space": "pause",
            "f": "fullscreen",
            "Escape": "escape",
            "Left": "back",
            "Right": "forward",
            "Up": "louder",
            "Down": "softer",
            "m": "mute",
            "v": "subtitle_track",
            "b": "audio_track",
        }
        for keysym, action in expected.items():
            with self.subTest(keysym=keysym):
                self.assertEqual(KEYS[keysym], action)

    def test_capital_letters_act_the_same(self) -> None:
        for letter in "fmvb":
            with self.subTest(letter=letter):
                self.assertEqual(KEYS[letter.upper()], KEYS[letter])

    def test_keys_are_read_only(self) -> None:
        with self.assertRaises(TypeError):
            KEYS["x"] = "y"  # type: ignore[index]


class FirstGeometryTests(unittest.TestCase):
    def test_seventy_percent_wide_sixteen_by_nine_centred(self) -> None:
        x, y, w, h = first_geometry((0, 0, 1920, 1040))
        self.assertEqual((w, h), (1344, 756))
        self.assertEqual((x, y), (288, 142))

    def test_offset_work_area(self) -> None:
        self.assertEqual(first_geometry((1920, 40, 1920, 1040)), (2208, 182, 1344, 756))

    def test_at_most_ninety_percent_tall(self) -> None:
        x, y, w, h = first_geometry((0, 0, 3000, 1000))
        self.assertEqual(h, 900)
        self.assertEqual(w, 1600)
        self.assertEqual((x, y), (700, 50))


class MiniTests(unittest.TestCase):
    WORK = (0, 0, 2560, 1552)

    def test_first_mini_bottom_right(self) -> None:
        self.assertEqual(
            first_mini(self.WORK, 1.5), (2560 - 720 - 24, 1552 - 405 - 24, 720, 405)
        )
        self.assertEqual(
            first_mini((100, 0, 1920, 1040), 1.0),
            (100 + 1920 - 480 - 16, 1040 - 270 - 16, 480, 270),
        )

    def test_tucked_near_a_corner(self) -> None:
        self.assertEqual(tucked((30, 40, 720, 405), self.WORK, 1.5), (24, 24, 720, 405))
        near = (2560 - 720 - 60, 1552 - 405 - 10, 720, 405)
        self.assertEqual(tucked(near, self.WORK, 1.5), (2560 - 720 - 24, 1552 - 405 - 24, 720, 405))

    def test_not_tucked_in_the_middle_or_along_one_edge(self) -> None:
        for rect in ((900, 500, 720, 405), (30, 500, 720, 405)):
            self.assertEqual(tucked(rect, self.WORK, 1.5), rect)

    def test_mini_shape(self) -> None:
        least = (600, 338)
        self.assertEqual(mini_shape((800, 405), (720, 405), least), (800, 450))
        self.assertEqual(mini_shape((720, 500), (720, 405), least), (889, 500))
        self.assertEqual(mini_shape((500, 200), (720, 405), least), (600, 338))
        self.assertEqual(mini_shape((720, 405), (720, 405), least), (720, 405))

    def test_ctrl_h(self) -> None:
        self.assertTrue(is_mini_key("h", 0x4))
        self.assertTrue(is_mini_key("H", 0x4 | 0x1))
        self.assertFalse(is_mini_key("h", 0))
        self.assertFalse(is_mini_key("j", 0x4))


if __name__ == "__main__":
    unittest.main()
