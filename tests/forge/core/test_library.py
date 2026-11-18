"""The library folders' folder test: whether a path lies in a folder (Windows' comparison)."""

from __future__ import annotations

from pathlib import Path

from app.forge.core.library import inside
from tests.forge.base import ForgeTestCase

DOWNLOADS = r"\\NAS\Multimedia\Downloads"
SERIES = r"\\NAS\Multimedia\Series"


class LibraryTest(ForgeTestCase):
    def test_inside_ignores_case_and_compares_whole_folder_names(self) -> None:
        assert inside(Path(DOWNLOADS), Path(r"\\nas\multimedia"))
        assert inside(Path(SERIES), Path(SERIES))
        assert inside(Path(r"C:\Media\Series\Show\a.mkv"), Path("c:/media/series"))
        assert not inside(Path(r"\\NAS\Multimedia\Series2"), Path(SERIES))
        assert not inside(Path(r"\\NAS\Other\Series"), Path(r"\\NAS\Multimedia"))
