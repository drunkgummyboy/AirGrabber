"""VLC's engine for AirGrabber's own player (app/services/vlc_engine.py): a 64-bit VLC 3 in
Program Files (or Program Files (x86)) with the python-vlc binding importable. Every file read and
the binding's lookup are injected fakes: no real file is read, `vlc` is never imported."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Collection
from unittest import mock

from app.services import playback
from app.services.vlc_engine import Engine, engine_problem, find_engine

PROGRAMS = r"C:\Program Files"
PROGRAMS_X86 = r"C:\Program Files (x86)"
VLC_64 = PROGRAMS + r"\VideoLAN\VLC"
VLC_32 = PROGRAMS_X86 + r"\VideoLAN\VLC"
ENVIRON = {"ProgramFiles": PROGRAMS, "ProgramFiles(x86)": PROGRAMS_X86}

X64 = 0x8664
I386 = 0x014C


def pe_head(machine: int, *, offset: int = 0x80) -> bytes:
    """The first 4 KiB of a PE file built for `machine`: "MZ", e_lfanew at 0x3C, "PE\\0\\0", machine."""
    head = bytearray(4096)
    head[0:2] = b"MZ"
    head[0x3C:0x40] = offset.to_bytes(4, "little")
    head[offset : offset + 4] = b"PE\0\0"
    head[offset + 4 : offset + 6] = machine.to_bytes(2, "little")
    return bytes(head)


def files(*paths: str) -> Callable[[str], bool]:
    present = {path.casefold() for path in paths}
    return lambda path: path.casefold() in present


def vlc_files(folder: str, *, core: bool = True) -> list[str]:
    return [folder + r"\libvlc.dll", *([folder + r"\libvlccore.dll"] if core else [])]


def heads(machines: dict[str, int]) -> Callable[[str], bytes]:
    """read_head: the DLL in each folder built for its machine; anything else is never read."""

    def read(path: str) -> bytes:
        for folder, machine in machines.items():
            if path.casefold() == (folder + r"\libvlc.dll").casefold():
                return pe_head(machine)
        raise AssertionError("read a file that isn't libvlc.dll")

    return read


def binding(present: bool) -> Callable[[], bool]:
    return lambda: present


def both(
    environ: dict[str, str],
    is_file: Callable[[str], bool],
    read_head: Callable[[str], bytes],
    has_binding: Callable[[], bool],
) -> tuple[Engine | None, str | None]:
    """find_engine's and engine_problem's answers for the same fakes."""
    return (
        find_engine(environ, is_file=is_file, read_head=read_head, has_binding=has_binding),
        engine_problem(environ, is_file=is_file, read_head=read_head, has_binding=has_binding),
    )


class FindEngineTests(unittest.TestCase):
    def look(
        self,
        present: Collection[str],
        machines: dict[str, int],
        *,
        has_binding: bool = True,
        environ: dict[str, str] | None = None,
    ) -> tuple[Engine | None, str | None]:
        return both(
            ENVIRON if environ is None else environ,
            files(*present),
            heads(machines),
            binding(has_binding),
        )

    def test_finds_64bit_vlc(self) -> None:
        engine, problem = self.look(vlc_files(VLC_64), {VLC_64: X64})
        self.assertEqual(engine, Engine(folder=VLC_64))
        self.assertIsNone(problem)

    def test_32bit_vlc_is_not_an_engine(self) -> None:
        engine, problem = self.look(vlc_files(VLC_32), {VLC_32: I386})
        self.assertIsNone(engine)
        self.assertEqual(problem, "not_64bit")

    def test_a_32bit_vlc_beside_a_64bit_one_is_passed_over(self) -> None:
        # the 32-bit one comes first here: Program Files holds it, (x86) the 64-bit one
        engine, problem = self.look(
            [*vlc_files(VLC_64), *vlc_files(VLC_32)], {VLC_64: I386, VLC_32: X64}
        )
        self.assertEqual(engine, Engine(folder=VLC_32))
        self.assertIsNone(problem)

    def test_no_binding_no_engine(self) -> None:
        engine, problem = self.look(vlc_files(VLC_64), {VLC_64: X64}, has_binding=False)
        self.assertIsNone(engine)
        self.assertEqual(problem, "no_binding")

    def test_no_vlc_says_so_before_the_binding(self) -> None:
        engine, problem = self.look([], {}, has_binding=False)
        self.assertIsNone(engine)
        self.assertEqual(problem, "no_vlc")

    def test_path_only_vlc_is_not_an_engine(self) -> None:
        # no Program Files variables; a vlc.exe the PATH would find, every file "there"
        with mock.patch("shutil.which", return_value=r"D:\Tools\VLC\vlc.exe"):
            engine, problem = self.look(
                [*vlc_files(r"D:\Tools\VLC"), r"D:\Tools\VLC\vlc.exe"],
                {r"D:\Tools\VLC": X64},
                environ={"PATH": r"D:\Tools\VLC"},
            )
        self.assertIsNone(engine)
        self.assertEqual(problem, "no_vlc")

    def test_vlc_older_than_3_is_not_an_engine(self) -> None:
        engine, problem = self.look(vlc_files(VLC_64, core=False), {VLC_64: X64})
        self.assertIsNone(engine)
        self.assertEqual(problem, "no_vlc")

    def test_a_dll_that_does_not_read_counts_as_no_vlc(self) -> None:
        def unreadable(path: str) -> bytes:
            raise PermissionError(path)

        for read in (unreadable, lambda path: b"MZ", lambda path: b"not a program" * 400):
            with self.subTest(read=read):
                engine, problem = both(ENVIRON, files(*vlc_files(VLC_64)), read, binding(True))
                self.assertIsNone(engine)
                self.assertEqual(problem, "no_vlc")

    def test_the_header_far_into_the_head_still_reads(self) -> None:
        def read(path: str) -> bytes:
            return pe_head(X64, offset=0xF00)

        engine = find_engine(ENVIRON, is_file=files(*vlc_files(VLC_64)), read_head=read, has_binding=binding(True))
        self.assertEqual(engine, Engine(folder=VLC_64))

    def test_the_folders_are_playbacks_vlc_places(self) -> None:
        looked: list[str] = []

        def is_file(path: str) -> bool:
            looked.append(path)
            return False

        find_engine(ENVIRON, is_file=is_file, read_head=heads({}), has_binding=binding(True))
        self.assertEqual(
            looked,
            [VLC_64 + r"\libvlc.dll", VLC_32 + r"\libvlc.dll"],
        )
        self.assertEqual(len(playback.VLC_PLACES), 2)


if __name__ == "__main__":
    unittest.main()
