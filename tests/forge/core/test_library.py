"""The library folders: what still needs setting up, and which watched folder a file came from."""

from __future__ import annotations

from pathlib import Path

from app.forge.core.library import inside, library_problems, watched_root
from app.forge.models.settings import ForgeSettings
from app.i18n import Language, set_language
from tests.forge.base import ForgeTestCase

DOWNLOADS = r"\\NAS\Multimedia\Downloads"
SERIES = r"\\NAS\Multimedia\Series"
MOVIES = r"\\NAS\Multimedia\Movies"
OVERLAP = "A watched folder can't be inside a library folder, or the other way round."


def settings(**changes: object) -> ForgeSettings:
    values: dict[str, object] = {
        "watch_folders": [DOWNLOADS],
        "series_folder": SERIES,
        "movies_folder": MOVIES,
    }
    values.update(changes)
    return ForgeSettings.model_validate(values)


class LibraryTest(ForgeTestCase):
    def test_library_problems(self) -> None:
        assert library_problems(settings()) == []
        assert library_problems(settings(watch_folders=[])) == ["Choose a download folder to watch."]
        assert library_problems(settings(series_folder="")) == ["Choose the Series library folder."]
        assert library_problems(settings(movies_folder="")) == ["Choose the Movies library folder."]
        assert library_problems(ForgeSettings()) == [
            "Choose a download folder to watch.",
            "Choose the Series library folder.",
            "Choose the Movies library folder.",
        ]
        for watch in (SERIES, r"\\nas\multimedia\series\Downloads", r"\\nas\multimedia"):
            assert library_problems(settings(watch_folders=[watch])) == [OVERLAP], watch
        assert library_problems(settings(watch_folders=[r"\\NAS\Multimedia\Series2"])) == []

    def test_inside_ignores_case_and_compares_whole_folder_names(self) -> None:
        assert inside(Path(DOWNLOADS), Path(r"\\nas\multimedia"))
        assert inside(Path(SERIES), Path(SERIES))
        assert inside(Path(r"C:\Media\Series\Show\a.mkv"), Path("c:/media/series"))
        assert not inside(Path(r"\\NAS\Multimedia\Series2"), Path(SERIES))
        assert not inside(Path(r"\\NAS\Other\Series"), Path(r"\\NAS\Multimedia"))

    def test_watched_root_finds_the_folder_a_file_came_from(self) -> None:
        second = r"D:\Torrents"
        both = settings(watch_folders=[DOWNLOADS, second])
        assert watched_root(Path(r"D:\torrents\Show.S01E01\show.mkv"), both) == Path(second)
        assert watched_root(Path(DOWNLOADS) / "a.mkv", both) == Path(DOWNLOADS)
        assert watched_root(Path(r"D:\Elsewhere\a.mkv"), both) is None

    def test_nested_watched_folders_are_refused(self) -> None:
        nested = settings(watch_folders=[DOWNLOADS, DOWNLOADS + r"\Movies"])
        assert library_problems(nested) == ["A watched folder can't be inside another watched folder."]

    def test_problems_follow_the_language(self) -> None:
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        assert library_problems(settings(watch_folders=[SERIES])) == [
            "Een bewaakte map kan niet in een bibliotheekmap liggen, of andersom."
        ]
        assert library_problems(settings(movies_folder="")) == ["Kies de bibliotheekmap voor films."]
