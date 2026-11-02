"""Junk and leftover artwork under a dropped folder, and what a download leaves behind."""

from __future__ import annotations

from pathlib import Path

from app.forge.core.cleanup import find_cleanup_targets
from tests.forge.base import ForgeTestCase


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


class CleanupTest(ForgeTestCase):
    def test_junk_files_anywhere(self) -> None:
        a = touch(self.tmp / "Thumbs.db")
        b = touch(self.tmp / "sub" / ".DS_Store")
        c = touch(self.tmp / "sub" / "DESKTOP.INI")
        assert set(find_cleanup_targets(self.tmp)) == {a, b, c}

    def test_url_with_matching_video_is_removed(self) -> None:
        url = touch(self.tmp / "Dune (2021) - Trailer.url")
        touch(self.tmp / "Dune (2021) - Trailer.MP4")
        keep = touch(self.tmp / "Other - Trailer.url")
        assert find_cleanup_targets(self.tmp) == [url]
        assert keep.exists()

    def test_orphan_movie_poster_removed(self) -> None:
        orphan = touch(self.tmp / "Old Name (2020) - Poster.jpg")
        assert find_cleanup_targets(self.tmp) == [orphan]

    def test_cleanup_keeps_poster_with_matching_video(self) -> None:
        touch(self.tmp / "Dune (2021) - Poster.jpg")
        touch(self.tmp / "Dune (2021).mkv")
        assert find_cleanup_targets(self.tmp) == []

    def test_series_poster_removed_only_when_tree_has_no_videos(self) -> None:
        kept = touch(self.tmp / "Show" / "Show - Series Poster.jpg")
        touch(self.tmp / "Show" / "Season 01" / "Show - S01E01.mkv")
        gone = touch(self.tmp / "Empty" / "Empty - Series Poster.jpg")
        assert find_cleanup_targets(self.tmp) == [gone]
        assert kept.exists()

    def test_season_poster_removed_when_folder_has_no_videos(self) -> None:
        touch(self.tmp / "S1" / "Season 01 Poster.jpg")
        touch(self.tmp / "S1" / "ep.avi")
        gone = touch(self.tmp / "S2" / "Season 02 Poster.jpg")
        touch(self.tmp / "S2" / "deeper" / "ep.mkv")  # videos in a subfolder don't count
        assert find_cleanup_targets(self.tmp) == [gone]

    def test_ordinary_files_untouched(self) -> None:
        touch(self.tmp / "cover.jpg")
        touch(self.tmp / "notes.txt")
        touch(self.tmp / "movie.mkv")
        assert find_cleanup_targets(self.tmp) == []

    def test_download_leftovers_are_samples_and_notes_only(self) -> None:
        from app.forge.core.cleanup import download_leftovers

        release = self.tmp / "Show.S01E01.1080p-GRP"
        junk = {
            touch(release / "Sample" / "show.s01e01-sample.mkv"),
            touch(release / "show.nfo"),
            touch(release / "RARBG.txt"),
            touch(release / "Visit.url"),
            touch(release / "show.sfv"),
            touch(release / "Thumbs.db"),
        }
        touch(release / "show.s01e01.en.srt")  # subtitles stay
        touch(release / "Screens" / "shot1.jpg")  # pictures stay
        touch(release / "show.s01e01.mkv")  # the video itself is never a leftover
        assert set(download_leftovers(release)) == junk

    def test_a_folder_with_a_download_still_coming_in_is_busy(self) -> None:
        import os
        import time

        from app.forge.core.cleanup import download_busy

        folder = self.tmp / "Show.S01"
        old = touch(folder / "show.nfo")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        assert not download_busy(folder, time.time())
        touch(folder / "Show.S01E02.mkv.!qB")
        assert download_busy(folder, time.time())
        (folder / "Show.S01E02.mkv.!qB").unlink()
        touch(folder / "fresh.txt")  # written a moment ago
        assert download_busy(folder, time.time())
