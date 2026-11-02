"""Disk operations that never overwrite anything, long paths included."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from unittest import mock

from app.forge.core import fsops
from app.forge.core.fsops import (
    long_path,
    make_dirs,
    move_file,
    remove_empty_dir,
    remove_file,
    stamp,
    write_new_file,
)
from tests.forge.base import ForgeTestCase


class FsopsTest(ForgeTestCase):
    def test_move_file_moves(self) -> None:
        (self.tmp / "a.mkv").write_bytes(b"v")
        (self.tmp / "sub").mkdir()
        move_file(self.tmp / "a.mkv", self.tmp / "sub" / "b.mkv")
        assert (self.tmp / "sub" / "b.mkv").read_bytes() == b"v"
        assert not (self.tmp / "a.mkv").exists()

    def test_move_refuses_overwrite(self) -> None:
        (self.tmp / "a.mkv").write_bytes(b"a")
        (self.tmp / "b.mkv").write_bytes(b"b")
        with self.assertRaises(FileExistsError):
            move_file(self.tmp / "a.mkv", self.tmp / "b.mkv")
        assert (self.tmp / "a.mkv").read_bytes() == b"a"
        assert (self.tmp / "b.mkv").read_bytes() == b"b"

    def test_case_only_rename_works(self) -> None:
        # Review focus 1
        (self.tmp / "the office.mkv").write_bytes(b"v")
        move_file(self.tmp / "the office.mkv", self.tmp / "The Office.mkv")
        assert os.listdir(self.tmp) == ["The Office.mkv"]

    def test_cross_volume_move_falls_back_to_copy(self) -> None:
        src, dst = self.tmp / "a.mkv", self.tmp / "b.mkv"
        src.write_bytes(b"data" * 1000)

        real_rename = os.rename

        def no_rename(a: str, b: str) -> None:
            if a.endswith(fsops.PARTIAL_SUFFIX):  # the finished copy gets its name, in place
                real_rename(a, b)
                return
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        self.enterContext(mock.patch.object(os, "rename", no_rename))
        move_file(src, dst)
        assert dst.read_bytes() == b"data" * 1000
        assert not src.exists()

    def test_cross_volume_copy_failure_leaves_source(self) -> None:
        src, dst = self.tmp / "a.mkv", self.tmp / "b.mkv"
        src.write_bytes(b"data")

        real_rename = os.rename

        def no_rename(a: str, b: str) -> None:
            if a.endswith(fsops.PARTIAL_SUFFIX):  # the finished copy gets its name, in place
                real_rename(a, b)
                return
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        def broken_copy(a: object, b: object, *args: object, **kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        self.enterContext(mock.patch.object(os, "rename", no_rename))
        self.enterContext(mock.patch.object(shutil, "copyfileobj", broken_copy))
        with self.assertRaises(OSError):
            move_file(src, dst)
        assert src.read_bytes() == b"data"
        assert not dst.exists()

    def test_long_path_roundtrip(self) -> None:
        # Review focus 4: deep series folders + long names go past 260 characters.
        deep = self.tmp.joinpath(*["Some Very Long Series Name (2026) - Season 01" for _ in range(6)])
        assert len(str(deep / "x.mkv")) > 300
        created = make_dirs(deep)
        assert created[-1] == deep
        size, _ = write_new_file(deep / "x.mkv", b"12345")
        assert size == 5
        move_file(deep / "x.mkv", deep / "Some Very Long Series Name (2026) - S01E01 - Pilot.mkv")
        assert os.path.exists(
            long_path(deep / "Some Very Long Series Name (2026) - S01E01 - Pilot.mkv")
        )
        assert stamp(deep / "Some Very Long Series Name (2026) - S01E01 - Pilot.mkv")[0] == 5

    def test_long_path_prefixes(self) -> None:
        assert long_path(Path(r"\\nas\share\a")) == r"\\?\UNC\nas\share\a"
        assert long_path(Path(r"C:\a\b")) == r"\\?\C:\a\b"
        assert long_path(Path(r"\\?\C:\a")) == r"\\?\C:\a"

    def test_make_dirs_reports_created_only(self) -> None:
        (self.tmp / "a").mkdir()
        assert make_dirs(self.tmp / "a" / "b" / "c") == [
            self.tmp / "a" / "b",
            self.tmp / "a" / "b" / "c",
        ]
        assert make_dirs(self.tmp / "a" / "b" / "c") == []

    def test_write_new_file_never_overwrites(self) -> None:
        (self.tmp / "p.jpg").write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            write_new_file(self.tmp / "p.jpg", b"new")
        assert (self.tmp / "p.jpg").read_bytes() == b"old"

    def test_remove_helpers(self) -> None:
        (self.tmp / "d").mkdir()
        (self.tmp / "d" / "f").write_bytes(b"x")
        assert remove_empty_dir(self.tmp / "d") is False
        remove_file(self.tmp / "d" / "f")
        assert remove_empty_dir(self.tmp / "d") is True
        assert not (self.tmp / "d").exists()

    def test_cross_volume_never_deletes_a_file_that_appeared_meanwhile(self) -> None:
        # Review I6: something else created dst between the existence check and the copy.
        src, dst = self.tmp / "a.mkv", self.tmp / "b.mkv"
        src.write_bytes(b"mine")

        def rename_then_race(a: str, b: str) -> None:
            dst.write_bytes(b"someone else's")
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        self.enterContext(mock.patch.object(os, "rename", rename_then_race))
        with self.assertRaises(FileExistsError):
            move_file(src, dst)
        assert dst.read_bytes() == b"someone else's"
        assert src.read_bytes() == b"mine"

    def test_cross_volume_locked_source_leaves_no_copy(self) -> None:
        # Review I6: the copy succeeded but the source is locked; don't leave a duplicate.
        src, dst = self.tmp / "a.mkv", self.tmp / "b.mkv"
        src.write_bytes(b"video")

        real_rename = os.rename

        def no_rename(a: str, b: str) -> None:
            if a.endswith(fsops.PARTIAL_SUFFIX):  # the finished copy gets its name, in place
                real_rename(a, b)
                return
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        real_remove = os.remove

        def locked_remove(path: str) -> None:
            if path.endswith("a.mkv"):
                raise PermissionError(32, "The process cannot access the file")
            real_remove(path)

        self.enterContext(mock.patch.object(os, "rename", no_rename))
        self.enterContext(mock.patch.object(os, "remove", locked_remove))
        with self.assertRaises(PermissionError):
            move_file(src, dst)
        assert src.read_bytes() == b"video"
        assert not dst.exists()

    def test_a_cut_off_copy_never_leaves_a_file_under_the_real_name(self) -> None:

        src, dst = self.tmp / "dl" / "a.mkv", self.tmp / "lib" / "a.mkv"
        src.parent.mkdir()
        dst.parent.mkdir()
        src.write_bytes(b"video" * 1000)

        def other_share(a: str, b: str) -> None:
            raise OSError(errno.EXDEV, "not the same device")

        def cut_off(reader: object, writer: object, length: int = 0) -> None:
            writer.write(b"half")  # type: ignore[attr-defined]
            raise OSError("the network name is no longer available")

        self.enterContext(mock.patch.object(os, "rename", other_share))
        self.enterContext(mock.patch.object(shutil, "copyfileobj", cut_off))
        with self.assertRaises(OSError):
            fsops.move_file(src, dst)
        assert src.read_bytes() == b"video" * 1000
        assert list(dst.parent.iterdir()) == []  # neither a half file under the name nor a partial

    def test_a_copy_between_shares_lands_whole(self) -> None:

        src, dst = self.tmp / "dl" / "a.mkv", self.tmp / "lib" / "a.mkv"
        src.parent.mkdir()
        dst.parent.mkdir()
        src.write_bytes(b"video")
        real_rename = os.rename

        def rename(a: str, b: str) -> None:
            if "dl" in a and "lib" in b and not a.endswith(fsops.PARTIAL_SUFFIX):
                raise OSError(errno.EXDEV, "not the same device")
            real_rename(a, b)

        self.enterContext(mock.patch.object(os, "rename", rename))
        fsops.move_file(src, dst)
        assert dst.read_bytes() == b"video" and not src.exists()
        assert [p.name for p in dst.parent.iterdir()] == ["a.mkv"]
