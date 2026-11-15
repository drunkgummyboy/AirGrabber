"""Windows' CopyFile2 through ctypes: one small real copy of a temporary file on the same local
disk (no share, no network), its progress, FAIL_IF_EXISTS and a Stop."""

from __future__ import annotations

import sys
import unittest

from app.forge.core import fsops, win_copy
from app.forge.core.win_copy import ERROR_REQUEST_ABORTED, CopyCancelled, _code
from tests.forge.base import ForgeTestCase


class CodeTest(unittest.TestCase):
    def test_the_windows_code_inside_an_hresult(self) -> None:
        self.assertEqual(_code(-2147023661), ERROR_REQUEST_ABORTED)  # 0x800704D3
        self.assertEqual(_code(-2147024816), 80)  # 0x80070050: ERROR_FILE_EXISTS
        self.assertEqual(_code(-2147467259), 0x80004005)  # E_FAIL: kept whole


@unittest.skipUnless(sys.platform == "win32" and win_copy.copier() is not None, "CopyFile2")
class RealCopyTest(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        copy = win_copy.copier()
        assert copy is not None
        self.copy = copy
        self.src = self.tmp / "a.mkv"
        self.src.write_bytes(b"video" * 20_000)  # 100 kB

    def test_a_small_copy_with_its_progress(self) -> None:
        dst = self.tmp / "b.mkv"
        heard: list[tuple[int, int]] = []
        self.copy(
            fsops.long_path(self.src),
            fsops.long_path(dst),
            lambda copied, size: heard.append((copied, size)),
            lambda: False,
        )
        self.assertEqual(dst.read_bytes(), self.src.read_bytes())
        self.assertTrue(heard)
        self.assertEqual(heard[-1], (100_000, 100_000))
        with self.assertRaises(FileExistsError):  # never over a file that is there
            self.copy(fsops.long_path(self.src), fsops.long_path(dst), None, None)
        self.assertEqual(dst.read_bytes(), self.src.read_bytes())

    def test_a_stop_leaves_nothing(self) -> None:
        dst = self.tmp / "b.mkv"
        with self.assertRaises(CopyCancelled):
            self.copy(fsops.long_path(self.src), fsops.long_path(dst), None, lambda: True)
        self.assertFalse(dst.exists())
        self.assertTrue(self.src.exists())

    def test_a_progress_that_fails_ends_the_copy_with_its_error(self) -> None:
        def broken(_copied: int, _size: int) -> None:
            raise ValueError("the progress broke")

        dst = self.tmp / "b.mkv"
        with self.assertRaises(ValueError):
            self.copy(fsops.long_path(self.src), fsops.long_path(dst), broken, None)
        self.assertFalse(dst.exists())


if __name__ == "__main__":
    unittest.main()
