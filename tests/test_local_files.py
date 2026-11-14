"""Videos opened from Explorer ("Open with" AirGrabber player, app/services/local_files.py): the
next file of a folder by its SxxEyy, the folder's videos, and where a file was left (by a hash
of its path, never the path)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.services.local_files import (
    PLACES_LIMIT,
    LocalPlaces,
    episode_code,
    next_file,
    video_names,
)

FOLDER = "\\\\NAS\\Vidéo\\Lantern Hill"


def path(name: str) -> str:
    return os.path.join(FOLDER, name)


class NextFileTests(unittest.TestCase):
    NAMES = (
        "Lantern.Hill.S01E02.mkv",
        "Lantern.Hill.S01E03.mkv",
        "Lantern Hill - s01e05 - The Bell.mp4",  # E04 is missing: the nearest later one
        "Lantern.Hill.S02E01.mkv",
        "Other.Show.S01E04.mkv",  # another show in the same folder
        "Lantern.Hill.S01E04.nl.srt",  # not a video
        "Holiday 2025.mp4",
    )

    def test_the_next_episode_of_the_same_show(self) -> None:
        self.assertEqual(
            next_file(path("Lantern.Hill.S01E02.mkv"), self.NAMES), path("Lantern.Hill.S01E03.mkv")
        )
        self.assertEqual(
            next_file(path("Lantern.Hill.S01E03.mkv"), self.NAMES),
            path("Lantern Hill - s01e05 - The Bell.mp4"),
        )
        self.assertEqual(
            next_file(path("Lantern Hill - s01e05 - The Bell.mp4"), self.NAMES),
            path("Lantern.Hill.S02E01.mkv"),
        )

    def test_none_after_the_last_or_without_a_code(self) -> None:
        self.assertIsNone(next_file(path("Lantern.Hill.S02E01.mkv"), self.NAMES))
        self.assertIsNone(next_file(path("Holiday 2025.mp4"), self.NAMES))
        self.assertIsNone(next_file(path("Lantern.Hill.S01E03.mkv"), ()))

    def test_episode_code(self) -> None:
        self.assertEqual(episode_code("Lantern.Hill.S01E03.mkv"), "S01E03")
        self.assertEqual(episode_code("lantern hill s1e4.mp4"), "S01E04")
        self.assertIsNone(episode_code("Holiday 2025.mp4"))
        self.assertIsNone(episode_code("Release.2160p.x265.mkv"))


class VideoNamesTests(unittest.TestCase):
    def test_only_videos(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a.S01E01.mkv", "b.MP4", "c.srt", "d.txt"):
                Path(folder, name).write_bytes(b"")
            os.mkdir(os.path.join(folder, "e.mkv"))  # a folder, not a video
            self.assertEqual(video_names(folder), ["a.S01E01.mkv", "b.MP4"])

    def test_a_folder_that_isnt_there(self) -> None:
        self.assertEqual(video_names(os.path.join(tempfile.gettempdir(), "no-such-folder-x")), [])


class PlacesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "played-files.json"
        self.now = 1_000.0
        self.places = self.make()

    def make(self) -> LocalPlaces:
        return LocalPlaces(self.file, now=lambda: self.now)

    def test_remembered_and_read_back(self) -> None:
        video = path("Lantern.Hill.S01E03.mkv")
        self.assertEqual(self.places.place(video), 0)
        self.places.remember(video, 860_000, 2_700_000)
        self.assertEqual(self.places.place(video), 860_000)
        self.assertEqual(self.make().place(video), 860_000)  # in the file
        self.assertEqual(self.make().place(video.upper()), 860_000)  # Windows' paths: any case

    def test_the_file_holds_no_path_or_name(self) -> None:
        self.places.remember(path("Lantern.Hill.S01E03.mkv"), 860_000, 2_700_000)
        text = self.file.read_text(encoding="utf-8")
        for private in ("Lantern", "NAS", "Vid", ".mkv"):
            self.assertNotIn(private, text)

    def test_watched_to_the_end_is_forgotten(self) -> None:
        video = path("Lantern.Hill.S01E03.mkv")
        self.places.remember(video, 860_000, 2_700_000)
        self.places.remember(video, 2_500_000, 2_700_000)  # past 90 %
        self.assertEqual(self.places.place(video), 0)

    def test_just_begun_is_nothing_to_resume(self) -> None:
        video = path("Lantern.Hill.S01E03.mkv")
        self.places.remember(video, 860_000, 2_700_000)
        self.places.remember(video, 4_000, 2_700_000)
        self.assertEqual(self.places.place(video), 0)

    def test_an_unknown_length_keeps_the_place(self) -> None:
        video = path("Holiday 2025.mp4")
        self.places.remember(video, 60_000, 0)
        self.assertEqual(self.places.place(video), 60_000)

    def test_the_newest_are_kept(self) -> None:
        for n in range(PLACES_LIMIT + 3):
            self.now += 1
            self.places.remember(path(f"{n}.mkv"), 60_000, 0)
        self.assertEqual(len(json.loads(self.file.read_text(encoding="utf-8"))), PLACES_LIMIT)
        self.assertEqual(self.make().place(path("0.mkv")), 0)
        self.assertEqual(self.make().place(path(f"{PLACES_LIMIT + 2}.mkv")), 60_000)

    def test_a_broken_file_counts_as_none(self) -> None:
        self.file.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.make().place(path("a.mkv")), 0)
        self.file.write_text('{"x": "y", "z": {"ms": "1"}}', encoding="utf-8")
        places = self.make()
        self.assertEqual(places.place(path("a.mkv")), 0)
        places.remember(path("a.mkv"), 60_000, 0)  # still writes
        self.assertEqual(self.make().place(path("a.mkv")), 60_000)


if __name__ == "__main__":
    unittest.main()
