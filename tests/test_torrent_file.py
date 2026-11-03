"""The .torrent check (app/services/torrent_file.py), on tiny torrents built here.

`bencode`, `make_torrent` and `info_hash_of` are also used by tests/test_downloads.py.
"""

from __future__ import annotations

import hashlib
import unittest
from typing import Any

from app.services.torrent_file import MAX_DEPTH, torrent_problem


def bencode(value: Any) -> bytes:
    """A small bencoder for test data: dictionaries keep their order (keys given as str)."""
    if isinstance(value, bool):
        raise TypeError("no booleans in bencode")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in value.items()) + b"e"
    raise TypeError(type(value).__name__)


def single_info(name: Any = "Some.Show.S01E02", **changes: Any) -> dict[str, Any]:
    """A single-file info dictionary; a field given as None is left out."""
    info: dict[str, Any] = {
        "length": 1048576,
        "name": name,
        "piece length": 262144,
        "pieces": b"\x01" * 80,
    }
    info.update(changes)
    return {k: v for k, v in info.items() if v is not None}


def multi_info(name: str = "Some.Show.S01") -> dict[str, Any]:
    return {
        "files": [
            {"length": 1000, "path": ["E01.mkv"]},
            {"length": 0, "path": ["Subs", "E01.en.srt"]},
        ],
        "name": name,
        "piece length": 16384,
        "pieces": b"\x02" * 20,
    }


def make_torrent(
    info: dict[str, Any] | None = None, announce: str = "udp://tracker.example.org:1337"
) -> bytes:
    return bencode({"announce": announce, "info": info if info is not None else single_info()})


def info_hash_of(info: dict[str, Any]) -> str:
    return hashlib.sha1(bencode(info)).hexdigest()


class AcceptedTests(unittest.TestCase):
    def test_single_file_torrent_with_its_hash(self) -> None:
        info = single_info()
        data = make_torrent(info)
        self.assertIsNone(torrent_problem(data))
        self.assertIsNone(torrent_problem(data, info_hash_of(info)))
        self.assertIsNone(torrent_problem(data, info_hash_of(info).upper()))  # any case

    def test_multi_file_torrent_with_its_hash(self) -> None:
        info = multi_info()
        self.assertIsNone(torrent_problem(make_torrent(info), info_hash_of(info)))

    def test_hybrid_torrent(self) -> None:
        info = single_info(**{"meta version": 2, "file tree": {"x": {"": {"length": 1}}}})
        self.assertIsNone(torrent_problem(make_torrent(info), info_hash_of(info)))

    def test_hash_is_of_the_exact_info_bytes(self) -> None:
        # Unsorted keys: re-encoding a decoded dict sorted would give another hash.
        info = {"pieces": b"\x03" * 20, "name": "x", "piece length": 1, "length": 1}
        self.assertIsNone(torrent_problem(make_torrent(info), info_hash_of(info)))

    def test_negative_and_zero_ints_elsewhere_are_fine(self) -> None:
        data = bencode({"creation date": -5, "x": 0, "info": single_info()})
        self.assertIsNone(torrent_problem(data))


class RefusedTests(unittest.TestCase):
    def assert_refused(self, data: bytes, reason: str, info_hash: str | None = None) -> None:
        self.assertEqual(torrent_problem(data, info_hash), reason)

    def test_truncated(self) -> None:
        data = make_torrent()
        for cut in (1, 10, len(data) // 2, len(data) - 1):
            with self.subTest(cut=cut):
                self.assert_refused(data[:cut], "truncated")

    def test_trailing_junk(self) -> None:
        self.assert_refused(make_torrent() + b"\n", "trailing bytes")
        self.assert_refused(make_torrent() + make_torrent(), "trailing bytes")

    def test_leading_zero_ints(self) -> None:
        for bad in (b"i01e", b"i-0e", b"i-01e", b"i00e"):
            with self.subTest(bad=bad):
                data = b"d1:x" + bad + make_torrent()[1:]
                self.assert_refused(data, "an integer with a leading zero")
        self.assert_refused(b"d1:xie" + make_torrent()[1:], "a bad integer")
        self.assert_refused(b"d1:xi1x2e" + make_torrent()[1:], "a bad integer")

    def test_leading_zero_string_length(self) -> None:
        self.assert_refused(b"d01:xi1e" + make_torrent()[1:], "a bad string length")

    def test_string_longer_than_the_file(self) -> None:
        self.assert_refused(b"d1:x999999:abce", "truncated")

    def test_keys_must_be_byte_strings(self) -> None:
        self.assert_refused(b"di1ei2ee", "a key is not a byte string")

    def test_duplicate_keys(self) -> None:
        self.assert_refused(b"d1:xi1e" + make_torrent()[1:-1] + b"1:xi2ee", "a duplicate key")

    def test_pieces_not_a_multiple_of_20(self) -> None:
        for pieces in (b"\x01" * 19, b"\x01" * 41, b""):
            with self.subTest(size=len(pieces)):
                info = single_info(pieces=pieces)
                self.assert_refused(make_torrent(info), "pieces is not a multiple of 20 bytes")

    def test_v2_only_torrent(self) -> None:
        info = {
            "file tree": {"x.mkv": {"": {"length": 1, "pieces root": b"\x00" * 32}}},
            "meta version": 2,
            "name": "x",
            "piece length": 16384,
        }
        self.assert_refused(make_torrent(info), "a v2-only torrent")

    def test_both_or_neither_length_and_files(self) -> None:
        both = single_info(files=[{"length": 1, "path": ["a"]}])
        self.assert_refused(make_torrent(both), "both length and files")
        self.assert_refused(make_torrent(single_info(length=None)), "neither length nor files")

    def test_bad_files(self) -> None:
        cases = {
            "no files": [],
            "a file has no path": [{"length": 1, "path": []}],
            "a file has no valid length": [{"length": -1, "path": ["a"]}],
            "a file path is not text": [{"length": 1, "path": [1]}],
            "a file entry is not a dictionary": [b"a"],
        }
        for reason, files in cases.items():
            with self.subTest(reason=reason):
                info = {**multi_info(), "files": files}
                self.assert_refused(make_torrent(info), reason)

    def test_missing_fields(self) -> None:
        self.assert_refused(bencode({"announce": "x"}), "no info dictionary")
        self.assert_refused(bencode({"info": [1]}), "no info dictionary")
        self.assert_refused(make_torrent(single_info(name=None)), "no name")
        self.assert_refused(make_torrent(single_info(**{"piece length": 0})), "no valid piece length")
        self.assert_refused(make_torrent(single_info(pieces=None)), "no pieces")
        self.assert_refused(make_torrent(single_info(length=-1)), "no valid length")

    def test_wrong_info_hash(self) -> None:
        other = info_hash_of(single_info("Another.Show"))
        self.assert_refused(make_torrent(), "info hash differs", other)

    def test_html_page(self) -> None:
        self.assert_refused(b"<!DOCTYPE html><html>4:info</html>", "not a bencoded dictionary")
        self.assert_refused(b"", "not a bencoded dictionary")
        self.assert_refused(b"le", "not a bencoded dictionary")

    def test_too_deep_nesting(self) -> None:
        deep = b"l" * (MAX_DEPTH + 5) + b"e" * (MAX_DEPTH + 5)
        self.assert_refused(b"d1:x" + deep + make_torrent()[1:], "nested too deep")
        hostile = b"d1:x" + b"l" * 1_000_000  # no RecursionError
        self.assert_refused(hostile, "nested too deep")

    def test_nesting_up_to_the_cap_is_fine(self) -> None:
        nested = b"l" * (MAX_DEPTH - 1) + b"e" * (MAX_DEPTH - 1)
        self.assertIsNone(torrent_problem(b"d1:x" + nested + make_torrent()[1:]))

    def test_reasons_never_hold_the_hash(self) -> None:
        info_hash = info_hash_of(single_info("Another.Show"))
        reason = torrent_problem(make_torrent(), info_hash) or ""
        self.assertNotIn(info_hash, reason)
        self.assertNotIn(info_hash.upper(), reason)


if __name__ == "__main__":
    unittest.main()
