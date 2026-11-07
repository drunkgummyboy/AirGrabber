"""Reading a .torrent's file names: a program among them is malware dressed up as a video."""

from __future__ import annotations

import unittest

from app.services.torrent_files import file_names, program_inside


def text(value: str) -> bytes:
    raw = value.encode()
    return str(len(raw)).encode() + b":" + raw


def single(name: str) -> bytes:
    return b"d4:infod6:lengthi5e4:name" + text(name) + b"12:piece lengthi1eee"


def several(*names: str) -> bytes:
    files = b"".join(b"d6:lengthi5e4:pathl" + text("Sub") + text(name) + b"ee" for name in names)
    return b"d4:infod5:filesl" + files + b"e4:name" + text("Folder") + b"ee"


def tree(*names: str) -> bytes:
    leaves = b"".join(text(name) + b"d0:d6:lengthi5eee" for name in names)
    return b"d4:infod9:file treed" + leaves + b"e4:name" + text("Folder") + b"ee"


class FileNamesTests(unittest.TestCase):
    def test_one_file_several_and_a_v2_tree(self) -> None:
        self.assertEqual(file_names(single("Show.S01E01.mkv")), ["Show.S01E01.mkv"])
        self.assertEqual(file_names(several("a.mkv", "b.nfo")), ["a.mkv", "b.nfo"])
        self.assertEqual(file_names(tree("a.mkv", "b.exe")), ["a.mkv", "b.exe"])

    def test_what_does_not_read_gives_no_names(self) -> None:
        deep = b"l" * 100_000
        for broken in (b"", b"not a torrent", b"d4:info", b"i5e", b"999:short", deep):
            self.assertEqual(file_names(broken), [], broken[:20])


class ProgramInsideTests(unittest.TestCase):
    def test_the_gold_rush_case(self) -> None:
        name = "Gold Rush S17E06 Rivers of Gold 1080p WEB H264-CAKES.exe"
        self.assertEqual(program_inside(single(name)), ".exe")

    def test_a_program_beside_the_video_or_hidden_in_its_name(self) -> None:
        self.assertEqual(program_inside(several("Show.mkv", "Codec.Setup.MSI")), ".msi")
        self.assertEqual(program_inside(single("Show.S01E01.mkv.scr")), ".scr")
        self.assertEqual(program_inside(single("Show.S01E01.exe. ")), ".exe")
        self.assertEqual(program_inside(tree("Show.mkv", "play.lnk")), ".lnk")

    def test_videos_and_their_usual_company_pass(self) -> None:
        clean = several("Show.S01E01.mkv", "Show.nfo", "Sample.mp4", "Site.url", "subs.srt", "a.rar")
        self.assertIsNone(program_inside(clean))
        self.assertIsNone(program_inside(single("Show.exe.Season.1")))
        self.assertIsNone(program_inside(single("No suffix at all")))
        self.assertIsNone(program_inside(b"not a torrent"))
