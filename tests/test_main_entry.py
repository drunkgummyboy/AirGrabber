"""main.py's command line: `--tray` starts with only the tray icon showing; `--setup-qbittorrent`
only sets qBittorrent up."""

from __future__ import annotations

import unittest

from main import Options, parse_options


class ParseOptionsTests(unittest.TestCase):
    def test_options(self) -> None:
        cases: dict[tuple[str, ...], Options] = {
            (): Options(tray=False),
            ("--tray",): Options(tray=True),
            ("--tray", "--something-newer"): Options(tray=True),  # unknown ones are ignored
            ("stray",): Options(tray=False),
            # install.ps1, right after installing qBittorrent (tests/test_qbittorrent_setup.py).
            ("--setup-qbittorrent",): Options(setup_qbittorrent=True),
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(parse_options(argv), expected)


if __name__ == "__main__":
    unittest.main()
