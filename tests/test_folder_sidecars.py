"""A folder library video's subtitle files (app/services/folder_library/sidecars.py): which of its
folder's files belong to it, and an SRT as WebVTT. Pure: names and texts only, no disk."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.forge.core.subtitles import SUBTITLE_EXTENSIONS
from app.services.folder_library import sidecars as sidecars_module
from app.services.folder_library.sidecars import Sidecar, sidecars, srt_to_vtt

VIDEO = Path(r"\\nas\films\Film (2020)\Film.mkv")


class SidecarTests(unittest.TestCase):
    def test_sidecars_next_to_a_video(self) -> None:
        names = [
            "Film.nl.srt",
            "Film 2.nl.srt",  # another video's
            "Film.en.forced.srt",
            "Film.nl.txt",  # not a subtitle
            "Film.mkv",
            "Film.srt",
            "Film.eng.ass",
        ]
        found = sidecars(VIDEO, names)
        self.assertEqual(
            [(one.language, one.forced, one.format) for one in found],
            [
                ("nl", False, "srt"),
                ("en", True, "srt"),
                (None, False, "srt"),
                ("eng", False, "ass"),
            ],
        )
        self.assertEqual(found[0], Sidecar(VIDEO.with_name("Film.nl.srt"), "nl", False, "srt"))
        self.assertTrue({".srt", ".ass"} <= SUBTITLE_EXTENSIONS)  # MediaForge's

    def test_any_case_and_the_other_formats(self) -> None:
        found = sidecars(VIDEO, ["film.NL.SRT", "Film.Forced.vtt", "Film.ssa", "Film.nl.sub"])
        self.assertEqual(
            [(one.path.name, one.language, one.forced, one.format) for one in found],
            [
                ("film.NL.SRT", "nl", False, "srt"),
                ("Film.Forced.vtt", None, True, "vtt"),
                ("Film.ssa", None, False, "ssa"),
                # A .sub (VobSub, MicroDVD) is none of the contract's formats: left out.
            ],
        )

    def test_a_word_that_isnt_a_language_says_none(self) -> None:
        [found] = sidecars(VIDEO, ["Film.Directors Cut.srt"])
        self.assertIsNone(found.language)
        [found] = sidecars(VIDEO, ["Film.pt-BR.srt"])
        self.assertEqual(found.language, "pt-br")


class VttTests(unittest.TestCase):
    def test_srt_to_vtt(self) -> None:
        srt = (
            "\ufeff1\r\n"
            "00:00:01,500 --> 00:00:03,000\r\n"
            "Hello there.\r\n"
            "\r\n"
            "2\r\n"
            "0:01:02,250 --> 0:01:04,000 X1:10 X2:20 Y1:30 Y2:40\r\n"
            "Two\r\n"
            "lines.\r\n"
        )
        self.assertEqual(
            srt_to_vtt(srt),
            "WEBVTT\n"
            "\n"
            "1\n"
            "00:00:01.500 --> 00:00:03.000\n"
            "Hello there.\n"
            "\n"
            "2\n"
            "00:01:02.250 --> 00:01:04.000\n"
            "Two\n"
            "lines.\n",
        )

    def test_an_empty_srt_is_an_empty_vtt(self) -> None:
        self.assertEqual(srt_to_vtt(""), "WEBVTT\n")

    def test_the_source_holds_no_invisible_byte_order_mark(self) -> None:
        # Written as "\ufeff": an invisible character in the source is easily lost in an edit.
        source = Path(sidecars_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\ufeff", source)
        self.assertEqual(srt_to_vtt("\ufeff"), "WEBVTT\n")


if __name__ == "__main__":
    unittest.main()
