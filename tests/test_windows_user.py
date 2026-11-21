"""What Windows says about the user (app/windows_user.py), with fakes for Windows' answers: the
Downloads folder where Windows has it, and qBittorrent's default save folder following it."""

from __future__ import annotations

import unittest
from pathlib import Path

from app import windows_user
from app.services import qbittorrent_setup
from app.windows_user import downloads_folder


def failing() -> str | None:
    raise OSError("Windows said no")


class DownloadsFolderTests(unittest.TestCase):
    def test_where_windows_has_it(self) -> None:
        self.assertEqual(downloads_folder(lambda: r"D:\Alex\Downloads"), Path(r"D:\Alex\Downloads"))

    def test_the_profiles_downloads_when_windows_wont_say(self) -> None:
        for known in (lambda: None, lambda: "", failing):
            with self.subTest(known=known):
                self.assertEqual(downloads_folder(known), Path.home() / "Downloads")

    def test_qbittorrents_default_save_folder_follows_it(self) -> None:
        original = windows_user._known_downloads
        windows_user._known_downloads = lambda: r"E:\Downloads"
        self.addCleanup(setattr, windows_user, "_known_downloads", original)
        self.assertEqual(qbittorrent_setup.default_save_path(), Path(r"E:\Downloads"))
        self.assertEqual(qbittorrent_setup.save_path(None), r"E:\Downloads")
        # A save folder in qBittorrent's own settings still wins.
        text = "[BitTorrent]\nSession\\DefaultSavePath=F:/Torrents\n"
        self.assertEqual(qbittorrent_setup.save_path(text), r"F:\Torrents")
