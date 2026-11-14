"""▶ Play (app/services/playback.py, app/ui/play_control.py, the info windows): Plex's file of a
film and of a show's episodes (a fake Plex Media Server behind httpx.MockTransport, from
tests/fixtures/plex/), the server path mapped onto MediaForge's Series / Movies folders, the
player's choice (VLC found or not, the real starts never made), what the status bar says, the
episode picker, and the windows' "▶ Play" only while the title is in Plex. Nothing reaches the
network, no player starts, no real folder is looked at."""

from __future__ import annotations

import time
import unittest
from collections.abc import Sequence
from typing import Any, cast
from unittest import mock

import httpx

from app.i18n_nl import NL
from app.models import CalendarEntry, Show, TitleExtras
from app.models.plex import PlexTitle, PlexVideo
from app.services import playback
from app.services.playback import (
    LibraryFolders,
    PlaybackService,
    PlayOutcome,
    PlayProblem,
    episode_name,
    find_vlc,
    launch_video,
    local_candidates,
    next_episode,
)
from app.services.plex import PlexError, PlexUnreachable
from app.ui import play_control, theme
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.movie_details import MovieDetailsWindow, MovieWindows
from app.ui.play_control import PLAY_TEXT, EpisodePicker, Play, episode_line
from app.ui.show_details import ShowDetailsWindow, ShowWindows
from tests.test_backup_dialog import DialogTestCase
from tests.test_info_layouts import SHOW, WindowDiscovery, cast_to, no_subscribe
from tests.test_info_polish import MarkedLibrary
from tests.test_info_window import INFO, FILM, FakeDiscovery, FakeImages, FakeWatchlist
from tests.test_plex import LOCAL_HTTPS, TOKEN, FakePlex, PlexTestCase, fixture

SERIES = r"\\Alex-NAS\video\TV"
MOVIES = r"\\Alex-NAS\video\Movies"
DUNE_FILE = "/volume1/video/Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv"
DUNE_HERE = MOVIES + r"\Dune Part Two (2024)\Dune Part Two (2024).mkv"
ARK_E02 = "The Ark - S02E02 - Woe's Hollow.mkv"
ARK_E02_HERE = SERIES + r"\The Ark (2023)\Season 02" + "\\" + ARK_E02
PRIVATE = ("Dune", "Ark", "volume1", "Alex-NAS", ".mkv")  # never in a log line


class PlayingPlex(FakePlex):
    """FakePlex that also answers /library/metadata/{key} and …/allLeaves."""

    def _server(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/library/metadata/") and self._authorised(request):
            if path == "/library/metadata/3001":
                return httpx.Response(200, json=fixture("metadata_movie.json"))
            if path == "/library/metadata/1001/allLeaves":
                return httpx.Response(200, json=fixture("all_leaves.json"))
            return httpx.Response(404)
        return super()._server(request)


class PlexPlayTestCase(PlexTestCase):
    async def asyncSetUp(self) -> None:
        with mock.patch("tests.test_plex.FakePlex", PlayingPlex):
            await super().asyncSetUp()
        await self.settings.update(plex_server_uri=LOCAL_HTTPS)


# -- Plex's files ---------------------------------------------------------------------------------


class PlexFileTests(PlexPlayTestCase):
    async def test_a_films_file_is_its_first_versions_first_part(self) -> None:
        video = await self.plex.film_video("3001")
        assert video is not None
        self.assertEqual(video.file, DUNE_FILE)  # the 4K one, Plex's first
        self.assertEqual(video.key, "3001")
        self.assertTrue(video.watched)
        asked = self.fake.requests[-1]
        self.assertEqual(asked.url.path, "/library/metadata/3001")
        self.assertEqual(asked.headers["X-Plex-Token"], TOKEN)
        self.assertNotIn(TOKEN, str(asked.url))

    async def test_a_film_the_server_doesnt_have(self) -> None:
        self.assertIsNone(await self.plex.film_video("3999"))

    async def test_a_shows_episodes_with_their_files_in_order(self) -> None:
        show = PlexTitle(kind="show", key="1001", title="The Ark")
        videos = await self.plex.show_videos(show)
        self.assertEqual(
            [(v.season, v.episode) for v in videos], [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (0, 1)]
        )  # the special last, the one without a file left out
        self.assertEqual([v.watched for v in videos], [True, True, True, False, False, False])
        self.assertTrue(videos[3].file.endswith("/The Ark (2023)/Season 02/" + ARK_E02))
        self.assertEqual(self.fake.requests[-1].url.path, "/library/metadata/1001/allLeaves")

    async def test_nothing_without_plex(self) -> None:
        await self.settings.update(plex_token="")
        self.assertIsNone(await self.plex.film_video("3001"))
        self.assertEqual(await self.plex.show_videos(PlexTitle(kind="show", key="1001", title="X")), [])
        self.assertEqual(await self.plex.locations(), [])
        self.assertEqual(self.fake.requests, [])

    async def test_plex_away(self) -> None:
        self.fake.answering = set()
        with self.assertRaises(PlexUnreachable):
            await self.plex.film_video("3001")
        with self.assertRaises(PlexError):
            await self.plex.show_videos(PlexTitle(kind="show", key="1001", title="X"))

    async def test_a_key_that_doesnt_read_is_never_asked(self) -> None:
        with self.assertRaises(PlexError):
            await self.plex.film_video("1/../2")
        self.assertEqual(self.fake.requests, [])


# -- the path (pure) ------------------------------------------------------------------------------


class MappingTests(unittest.TestCase):
    LOCATIONS = ["/volume1/video/TV", "/volume1/video/Movies"]

    def test_below_the_library_folder_that_holds_it(self) -> None:
        found = local_candidates(DUNE_FILE, self.LOCATIONS, MOVIES)
        self.assertEqual(found[0], DUNE_HERE)

    def test_the_folders_own_ending_when_the_library_folder_is_higher_up(self) -> None:
        file = "/volume1/video/TV/The Ark (2023)/Season 02/" + ARK_E02
        found = local_candidates(file, ["/volume1/video"], SERIES)
        self.assertEqual(found[0], SERIES + r"\TV\The Ark (2023)\Season 02" + "\\" + ARK_E02)
        self.assertEqual(found[1], ARK_E02_HERE)  # TV is the share's TV

    def test_the_last_names_when_nothing_else_fits(self) -> None:
        found = local_candidates(DUNE_FILE, [], r"\\Alex-NAS\Films")
        self.assertEqual(
            found,
            [
                r"\\Alex-NAS\Films\Movies\Dune Part Two (2024)\Dune Part Two (2024).mkv",
                r"\\Alex-NAS\Films\Dune Part Two (2024)\Dune Part Two (2024).mkv",
            ],
        )

    def test_a_server_on_windows(self) -> None:
        file = r"D:\Media\Movies\Dune (2021)\Dune (2021).mkv"
        found = local_candidates(file, [r"D:\Media\Movies", r"E:\Movies"], r"\\Alex-PC\Movies")
        self.assertEqual(found[0], r"\\Alex-PC\Movies\Dune (2021)\Dune (2021).mkv")

    def test_a_mapped_drive(self) -> None:
        found = local_candidates(DUNE_FILE, self.LOCATIONS, "M:\\")
        self.assertEqual(found[0], r"M:\Dune Part Two (2024)\Dune Part Two (2024).mkv")

    def test_a_miss(self) -> None:
        self.assertEqual(local_candidates(DUNE_FILE, self.LOCATIONS, ""), [])
        self.assertEqual(local_candidates("", self.LOCATIONS, MOVIES), [])

    def test_never_above_the_folder(self) -> None:
        found = local_candidates("/tv/../../secret/a/b.mkv", ["/tv"], SERIES)
        self.assertTrue(all(".." not in path for path in found))
        self.assertTrue(all(path.startswith(SERIES + "\\") for path in found))


class EpisodeTests(unittest.TestCase):
    def video(self, season: int, episode: int, *, watched: bool = False) -> PlexVideo:
        return PlexVideo(key=f"{season}{episode}", file="/f", season=season, episode=episode, watched=watched)

    def test_the_next_one_is_the_first_unwatched(self) -> None:
        videos = [self.video(1, 1, watched=True), self.video(1, 2), self.video(2, 1), self.video(0, 1)]
        self.assertEqual(next_episode(videos), videos[1])

    def test_all_watched_the_first(self) -> None:
        videos = [self.video(1, 1, watched=True), self.video(1, 2, watched=True), self.video(0, 1)]
        self.assertEqual(next_episode(videos), videos[0])  # not the unwatched special

    def test_only_specials(self) -> None:
        videos = [self.video(0, 1, watched=True), self.video(0, 2)]
        self.assertEqual(next_episode(videos), videos[1])
        self.assertIsNone(next_episode([]))

    def test_names(self) -> None:
        video = self.video(2, 3)
        self.assertEqual(episode_name("The Ark", video), "The Ark S02E03")
        self.assertEqual(episode_line(video.model_copy(update={"title": "Woe"})), "E03 · Woe")
        self.assertEqual(episode_line(PlexVideo(key="1", file="/f", title="Special")), "Special")


# -- the player -----------------------------------------------------------------------------------


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    def test_vlc_in_program_files_then_x86_then_the_path(self) -> None:
        environ = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"}
        x64 = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
        x86 = r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"
        self.assertEqual(find_vlc(environ, is_file={x64, x86}.__contains__, which=lambda _: None), x64)
        self.assertEqual(find_vlc(environ, is_file={x86}.__contains__, which=lambda _: None), x86)
        self.assertEqual(
            find_vlc(environ, is_file=lambda _: False, which=lambda name: rf"D:\{name}.exe"), r"D:\vlc.exe"
        )
        self.assertIsNone(find_vlc({}, is_file=lambda _: True, which=lambda _: None))

    async def test_vlc_when_installed(self) -> None:
        started: list[list[str]] = []
        with (
            mock.patch.object(playback, "find_vlc", return_value=r"C:\VLC\vlc.exe"),
            mock.patch.object(playback, "_start_process", started.append),
            mock.patch.object(playback, "_open_with_windows") as windows,
        ):
            self.assertEqual(await launch_video(DUNE_HERE), "vlc")
        self.assertEqual(started, [[r"C:\VLC\vlc.exe", DUNE_HERE]])
        windows.assert_not_called()

    async def test_windows_own_player_without_vlc(self) -> None:
        opened: list[str] = []
        with (
            mock.patch.object(playback, "find_vlc", return_value=None),
            mock.patch.object(playback, "_start_process") as start,
            mock.patch.object(playback, "_open_with_windows", opened.append),
        ):
            self.assertEqual(await launch_video(DUNE_HERE), "default")
        self.assertEqual(opened, [DUNE_HERE])
        start.assert_not_called()

    async def test_tests_never_start_a_real_player(self) -> None:
        with mock.patch.object(playback, "find_vlc", return_value=r"C:\VLC\vlc.exe"):
            with self.assertRaises(AssertionError):
                await launch_video(DUNE_HERE)


# -- the service ----------------------------------------------------------------------------------


class SplitPlex:
    """PlexService's titles and show_videos for a show Plex keeps as two titles (it does when
    some of its files got matched apart: Reacher as 30 episodes and as the other 2)."""

    connected = True

    def __init__(self) -> None:
        self.stray = PlexTitle(
            kind="show", key="75569", title="Reacher", episodes=2, show_id=43031, tvdb_id=366924
        )
        self.main = PlexTitle(
            kind="show", key="19054", title="Reacher", episodes=30, show_id=43031, tvdb_id=366924
        )
        other = PlexTitle(kind="show", key="500", title="Other", episodes=9, show_id=7)
        self.listed = [self.stray, self.main, other]
        self.videos = {
            "75569": [self.video("s1", 3, 6), self.video("s2", 4, 1)],
            "19054": [
                self.video(f"m{season}{episode}", season, episode)
                for season in (1, 2, 3, 4)
                for episode in range(1, 9)
                if (season, episode) != (4, 1)  # S03E06 here too: the main title's copy wins
            ]
            + [self.video("m0", 0, 1)],
            "500": [self.video("o", 1, 1)],
        }

    @staticmethod
    def video(key: str, season: int, episode: int) -> PlexVideo:
        return PlexVideo(key=key, file=f"/v/{key}.mkv", season=season, episode=episode)

    async def titles(self) -> list[PlexTitle]:
        return list(self.listed)

    async def show_videos(self, show: PlexTitle) -> list[PlexVideo]:
        return list(self.videos[show.key])


class SplitShowTests(unittest.IsolatedAsyncioTestCase):
    """Plex keeps one show as two titles: ▶ Play's picker has the episodes of both."""

    async def asyncSetUp(self) -> None:
        self.plex = SplitPlex()

        async def folders() -> LibraryFolders | None:
            return None

        self.service = PlaybackService(cast(Any, self.plex), folders, launch=FakeLauncher())

    async def test_the_show_is_its_title_with_the_most_episodes(self) -> None:
        found = await self.service.show(Show(id=43031, name="Reacher"))
        self.assertEqual(found, self.plex.main)

    async def test_the_episodes_of_every_title_of_the_show(self) -> None:
        for title in (self.plex.main, self.plex.stray):
            with self.subTest(title=title.key):
                videos = await self.service.episodes(title)
                spots = [(video.season, video.episode) for video in videos]
                self.assertEqual(
                    spots,
                    [(season, episode) for season in (1, 2, 3, 4) for episode in range(1, 9)]
                    + [(0, 1)],
                )  # in order, the special last, each once
                self.assertEqual(videos[spots.index((3, 6))].key, "m36")
                self.assertEqual(videos[spots.index((4, 1))].key, "s2")

    async def test_matched_by_tvdb_or_imdb_when_not_by_tvmaze(self) -> None:
        self.plex.listed = [
            self.plex.main.model_copy(update={"show_id": None}),
            self.plex.stray.model_copy(update={"show_id": None, "tvdb_id": None, "imdb_id": "tt1"}),
        ]
        self.assertEqual(len(await self.service.episodes(self.plex.listed[0])), 32)  # alone
        self.plex.listed[0] = self.plex.listed[0].model_copy(update={"imdb_id": "tt1"})
        self.assertEqual(len(await self.service.episodes(self.plex.listed[0])), 33)  # with S04E01

    async def test_a_show_alone_is_just_its_own(self) -> None:
        other = self.plex.listed[2]
        self.assertEqual([v.key for v in await self.service.episodes(other)], ["o"])


class FakeLauncher:
    def __init__(self, player: playback.Player = "vlc", error: OSError | None = None) -> None:
        self.player = player
        self.error = error
        self.paths: list[str] = []

    async def __call__(self, path: str) -> playback.Player:
        if self.error is not None:
            raise self.error
        self.paths.append(path)
        return self.player


class ServiceTests(PlexPlayTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.plex.check()  # the Library's titles
        self.folders: LibraryFolders | None = LibraryFolders(series=SERIES, movies=MOVIES)
        self.here = {DUNE_HERE, ARK_E02_HERE}
        self.launch = FakeLauncher()
        self.service = self.make()

    def make(self, **kwargs: Any) -> PlaybackService:
        async def folders() -> LibraryFolders | None:
            return self.folders

        return PlaybackService(
            self.plex, folders, launch=self.launch, exists=lambda path: path in self.here, **kwargs
        )

    async def dune(self) -> PlexTitle:
        title = await self.service.film(693134)
        assert title is not None
        return title

    async def test_the_titles_in_plex(self) -> None:
        self.assertEqual((await self.dune()).key, "3001")
        self.assertEqual(await self.service.film(None, "tt15239678"), await self.dune())
        self.assertIsNone(await self.service.film(1))
        ark = await self.service.show(Show(id=101, name="The Ark", tvdb_id=412345))
        assert ark is not None
        self.assertEqual(ark.key, "1001")
        by_imdb = await self.service.show(Show(id=9, name="?", imdb_id="tt11280740"))
        self.assertEqual(by_imdb.key if by_imdb else None, "1002")
        self.assertIsNone(await self.service.show(Show(id=9, name="Lost")))

    async def test_a_film_plays_at_once_in_vlc(self) -> None:
        with self.assertLogs("app.services.playback", "INFO") as logs:
            outcome = await self.service.play_film(await self.dune())
        self.assertEqual(outcome, PlayOutcome("Dune: Part Two", "movie", player="vlc"))
        self.assertEqual(outcome.text, "▶ Playing Dune: Part Two in VLC.")
        self.assertEqual(self.launch.paths, [DUNE_HERE])
        self.assertFalse([line for line in logs.output if any(p in line for p in PRIVATE)])

    async def test_an_episode(self) -> None:
        ark = await self.service.show(Show(id=101, name="The Ark", tvdb_id=412345))
        assert ark is not None
        videos = await self.service.episodes(ark)
        chosen = next_episode(videos)
        assert chosen is not None
        self.launch.player = "default"
        outcome = await self.service.play(ark, chosen)
        self.assertEqual(self.launch.paths, [ARK_E02_HERE])
        self.assertEqual(outcome.text, "▶ Playing The Ark S02E02 in your video player.")

    async def test_not_in_the_folder_from_this_pc(self) -> None:
        self.here = set()
        with self.assertLogs("app.services.playback", "INFO") as logs:
            outcome = await self.service.play_film(await self.dune())
        self.assertEqual(outcome.problem, PlayProblem.NOT_FOUND)
        self.assertIn("Movies folder", outcome.text)
        self.assertNotIn("Alex-NAS", outcome.text)  # the setting is named, not the path
        self.assertEqual(self.launch.paths, [])
        self.assertFalse([line for line in logs.output if any(p in line for p in PRIVATE)])

    async def test_a_share_that_hangs(self) -> None:
        def hangs(path: str) -> bool:
            time.sleep(0.3)
            return True

        service = PlaybackService(
            self.plex, self.service._folders, launch=self.launch, exists=hangs, timeout_s=0.05
        )
        outcome = await service.play_film(await self.dune())
        self.assertEqual(outcome.problem, PlayProblem.NOT_FOUND)

    async def test_mediaforges_folders(self) -> None:
        dune = await self.dune()
        self.folders = None
        outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.FORGE_OFF)
        self.folders = LibraryFolders(series=SERIES)
        outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.NO_FOLDER)
        self.assertIn("Movies folder in Settings › Auto-rename", outcome.text)

    async def test_plex_away(self) -> None:
        dune = await self.dune()
        self.fake.answering = set()
        outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.PLEX_AWAY)
        self.assertEqual(outcome.text, "Plex didn't answer, so try ▶ Play again in a moment.")

    async def test_plex_names_no_file(self) -> None:
        outcome = await self.service.play_film(PlexTitle(kind="movie", key="3999", title="Gone"))
        self.assertEqual(outcome.problem, PlayProblem.NO_FILE)

    async def test_the_libraries_folders_unread_the_last_names_still_find_it(self) -> None:
        dune = await self.dune()
        real = self.fake._server

        def no_sections(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/library/sections":
                return httpx.Response(500)
            return real(request)

        self.fake._server = no_sections  # type: ignore[method-assign]
        outcome = await self.service.play_film(dune)
        self.assertTrue(outcome.played)
        self.assertEqual(self.launch.paths, [DUNE_HERE])

    async def test_the_player_doesnt_start(self) -> None:
        self.launch.error = FileNotFoundError("gone")
        dune = await self.dune()
        with self.assertLogs("app.services.playback", "WARNING"):
            outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.NO_PLAYER)

    async def test_plex_disconnected(self) -> None:
        dune = await self.dune()
        await self.settings.update(plex_token="")
        self.assertFalse(self.service.available)
        outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.NOT_CONNECTED)

    async def test_a_films_video_for_the_player(self) -> None:
        dune = await self.dune()
        video = await self.service.film_video(dune)
        assert isinstance(video, PlexVideo)
        self.assertEqual(video.key, "3001")
        self.assertEqual(self.launch.paths, [])  # nothing opened: the player plays it
        gone = await self.service.film_video(PlexTitle(kind="movie", key="3999", title="Gone"))
        self.assertEqual(gone, PlayOutcome("Gone", "movie", problem=PlayProblem.NO_FILE))
        self.fake.answering = set()
        away = await self.service.film_video(dune)
        assert isinstance(away, PlayOutcome)
        self.assertEqual(away.problem, PlayProblem.PLEX_AWAY)
        await self.settings.update(plex_token="")
        off = await self.service.film_video(dune)
        assert isinstance(off, PlayOutcome)
        self.assertEqual(off.problem, PlayProblem.NOT_CONNECTED)


# -- the UI ---------------------------------------------------------------------------------------


ARK = PlexTitle(kind="show", key="1001", title="The Ark")
DUNE = PlexTitle(kind="movie", key="3001", title="Dune: Part Two", tmdb_id=FILM.id)


def leaves() -> list[PlexVideo]:
    found: list[PlexVideo] = []
    for item in fixture("all_leaves.json")["MediaContainer"]["Metadata"]:
        if item["Media"]:
            found.append(
                PlexVideo(
                    key=item["ratingKey"],
                    file=item["Media"][0]["Part"][0]["file"],
                    season=item["parentIndex"],
                    episode=item["index"],
                    title=item["title"],
                    watched=bool(item.get("viewCount")),
                )
            )
    return sorted(found, key=lambda v: (not v.season, v.season or 0, v.episode or 0))


class FakePlayback:
    """PlaybackService's side the windows use (the playing itself is the player's)."""

    def __init__(
        self,
        *,
        film: PlexTitle | None = DUNE,
        show: PlexTitle | None = ARK,
        videos: list[PlexVideo] | None = None,
    ) -> None:
        self._film = film
        self._show = show
        self._videos = videos
        self.available = True
        self.fail = False
        self.film_problem: PlayProblem | None = None

    async def film(self, tmdb_id: int | None, imdb_id: str | None = None) -> PlexTitle | None:
        return self._film

    async def show(self, show: Show) -> PlexTitle | None:
        return self._show

    async def episodes(self, title: PlexTitle) -> list[PlexVideo]:
        if self.fail:
            raise PlexUnreachable("away")
        return list(self._videos) if self._videos is not None else leaves()

    async def film_video(self, title: PlexTitle) -> PlexVideo | PlayOutcome:
        if self.film_problem is not None:
            return PlayOutcome(title.title, "movie", problem=self.film_problem)
        return PlexVideo(key=title.key, file=DUNE_FILE, view_offset_ms=60000, duration_ms=9000000)


class FakePlayer:
    """MediaPlayerHost's `play` (app/ui/media_player.py): what it was asked to play."""

    def __init__(self) -> None:
        self.played: list[tuple[str, str, tuple[str, ...]]] = []

    def play(
        self, title: PlexTitle, video: PlexVideo, *, episodes: Sequence[PlexVideo] = ()
    ) -> None:
        self.played.append((title.key, video.key, tuple(one.key for one in episodes)))


class Status:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str, *, clear_after_ms: int | None = None) -> None:
        self.lines.append(text)


HOUR_MS = 3600000


def half_watched() -> list[PlexVideo]:
    """A season: E01 not started, E02 half watched, E03 at 95 %, E04 begun but of unknown length."""

    def one(number: int, offset: int, duration: int = HOUR_MS) -> PlexVideo:
        return PlexVideo(
            key=f"212{number}",
            file=f"/tv/The Ark/S02E0{number}.mkv",
            season=2,
            episode=number,
            title=f"Part {number}",
            view_offset_ms=offset,
            duration_ms=duration,
        )

    return [one(1, 0), one(2, HOUR_MS // 2), one(3, HOUR_MS * 95 // 100), one(4, 1000, 0)]


class PickerTests(DialogTestCase):
    async def open(self, fake: FakePlayback | None = None) -> tuple[EpisodePicker, list[PlexVideo]]:
        chosen: list[PlexVideo] = []
        picker = EpisodePicker(
            self.root,
            ARK,
            episodes=(fake or FakePlayback()).episodes,
            on_play=lambda video, _episodes: chosen.append(video),
        )
        self.keep(picker)
        await picker.loaded()
        await self.settle()
        return picker, chosen

    async def test_picker_progress_line_only_when_half_watched(self) -> None:
        picker, _chosen = await self.open(FakePlayback(videos=half_watched()))
        self.assertEqual(list(picker.progress), ["2122"])  # not E01 (0), E03 (≥ 90 %), E04 (no length)
        bar = picker.progress["2122"]
        self.assertAlmostEqual(bar.get(), 0.5, places=2)
        self.assertEqual(bar.cget("progress_color"), theme.ACCENT)
        self.assertIs(bar.master, picker.rows["2122"].master)  # under its row, as wide

    async def test_the_next_unwatched_one_is_selected(self) -> None:
        picker, _chosen = await self.open()
        self.assertEqual(list(picker.chips), [1, 2, 0])  # Specials last
        self.assertEqual(picker.chips[0].cget("text"), "Specials")
        self.assertEqual(picker.season, 2)
        assert picker.selected is not None
        self.assertEqual((picker.selected.season, picker.selected.episode), (2, 2))
        self.assertEqual(
            [b.cget("text") for b in picker.rows.values()],
            ["E01 · Return", "E02 · Woe's Hollow", "E03 · Into the Dark"],
        )

    async def test_a_click_plays_that_one_and_closes(self) -> None:
        picker, chosen = await self.open()
        picker.rows["2123"].invoke()
        self.assertEqual([v.key for v in chosen], ["2123"])
        self.assertFalse(picker.winfo_exists())

    async def test_enter_plays_the_selection_and_the_arrows_move_it(self) -> None:
        picker, chosen = await self.open()
        picker.move(-1)
        picker.event_generate("<Return>")
        await self.settle()
        self.assertEqual([v.key for v in chosen], ["2121"])

    async def test_another_season(self) -> None:
        picker, _chosen = await self.open()
        picker.chips[1].invoke()
        self.assertEqual(picker.season, 1)
        assert picker.selected is not None
        self.assertEqual(picker.selected.key, "2111")  # all watched: the first

    async def test_escape_closes(self) -> None:
        picker, chosen = await self.open()
        picker.event_generate("<Escape>")
        await self.settle()
        self.assertFalse(picker.winfo_exists())
        self.assertEqual(chosen, [])

    async def test_plex_away(self) -> None:
        fake = FakePlayback()
        fake.fail = True
        with self.assertLogs("app.ui.play_control", "INFO"):
            picker, _chosen = await self.open(fake)
        self.assertEqual(picker.note.cget("text"), "Plex didn't answer, so try again in a moment.")
        self.assertEqual(picker.rows, {})

    async def test_episode_play_passes_the_season_list(self) -> None:
        fake, player, status = FakePlayback(), FakePlayer(), Status()
        play = Play(cast(Any, fake), player, status)
        picker = play.pick(self.root, ARK)
        self.keep(picker)
        await picker.loaded()
        await self.settle()
        self.assertIs(play.pick(self.root, ARK), picker)  # one at a time
        picker.play_selected()
        await play.settled()
        everything = tuple(video.key for video in leaves())  # the next one may be a season on
        self.assertEqual(player.played, [("1001", "2122", everything)])
        # The player says how it went itself: Play only says it is looking.
        self.assertEqual(status.lines, ["▶ Finding The Ark S02E02…"])

    async def test_each_pick_reads_the_episodes_again(self) -> None:
        fake, player = FakePlayback(videos=half_watched()), FakePlayer()
        play = Play(cast(Any, fake), player, Status())
        first = play.pick(self.root, ARK)
        await first.loaded()
        first.play_selected()
        fake._videos = [v.model_copy(update={"view_offset_ms": 0}) for v in half_watched()]
        second = play.pick(self.root, ARK)
        self.keep(second)
        self.assertIsNot(second, first)
        await second.loaded()
        await self.settle()
        self.assertEqual(second.progress, {})  # Plex's places as they are now


class FilmWindowTests(DialogTestCase):
    async def window(
        self, *, in_plex: bool, fake: FakePlayback, player: FakePlayer | None = None
    ) -> tuple[MovieDetailsWindow, Status]:
        actions = mock.MagicMock()
        actions.states.get.return_value = None
        status = Status()

        async def plex(movie_id: int, imdb_id: str | None) -> bool:
            return in_plex

        windows = MovieWindows(
            self.root,
            images=cast(Any, FakeImages()),
            discovery=cast(Any, FakeDiscovery(TitleExtras(info=INFO))),
            watchlist=cast(Any, FakeWatchlist()),
            actions=actions,
            in_plex=plex,
            playback=cast(Any, fake),
            player=player or FakePlayer(),
            status=status,
        )
        self.addCleanup(windows.close)
        windows.open_movie(FILM)
        for _ in range(3):
            await self.settle()
        [window] = windows._live().values()
        return window, status

    async def test_play_only_while_in_plex(self) -> None:
        window, _status = await self.window(in_plex=False, fake=FakePlayback())
        self.assertIsNone(window.play_button)

    async def test_the_play_tip_says_this_pc(self) -> None:
        # Not "VLC": ▶ Play opens AirGrabber's own player unless Settings → Plex says VLC.
        tips: list[str] = []
        with mock.patch.object(play_control, "add_tooltip", lambda _widget, text: tips.append(text)):
            await self.window(in_plex=True, fake=FakePlayback())
        self.assertEqual(tips, ["Play it on this PC"])
        self.assertEqual(NL[play_control.PLAY_TIP], "Afspelen op deze pc")

    async def test_film_play_goes_to_the_host(self) -> None:
        player = FakePlayer()
        window, status = await self.window(in_plex=True, fake=FakePlayback(), player=player)
        assert window.play_button is not None
        self.assertEqual(window.play_button.cget("text"), PLAY_TEXT)
        window.play_button.invoke()
        assert window._play is not None
        await window._play.settled()
        self.assertEqual(player.played, [("3001", "3001", ())])  # a film: no episodes
        # The player says how it went itself: Play only says it is looking.
        self.assertEqual(status.lines[-1:], ["▶ Finding Dune: Part Two…"])

    async def test_a_film_plex_cant_read_says_so_and_plays_nothing(self) -> None:
        fake, player = FakePlayback(), FakePlayer()
        fake.film_problem = PlayProblem.PLEX_AWAY
        window, status = await self.window(in_plex=True, fake=fake, player=player)
        assert window.play_button is not None and window._play is not None
        window.play_button.invoke()
        await window._play.settled()
        self.assertEqual(player.played, [])
        self.assertEqual(
            status.lines[-2:],
            ["▶ Finding Dune: Part Two…", "Plex didn't answer, so try ▶ Play again in a moment."],
        )

    async def test_no_play_without_the_player(self) -> None:
        actions = mock.MagicMock()
        actions.states.get.return_value = None

        async def plex(movie_id: int, imdb_id: str | None) -> bool:
            return True

        windows = MovieWindows(
            self.root,
            images=cast(Any, FakeImages()),
            discovery=cast(Any, FakeDiscovery(TitleExtras(info=INFO))),
            watchlist=cast(Any, FakeWatchlist()),
            actions=actions,
            in_plex=plex,
            playback=cast(Any, FakePlayback()),
            status=Status(),
        )
        self.addCleanup(windows.close)
        windows.open_movie(FILM)
        for _ in range(3):
            await self.settle()
        [window] = windows._live().values()
        self.assertIsNone(window.play_button)

    async def test_in_plex_without_a_plex_title(self) -> None:
        window, _status = await self.window(in_plex=True, fake=FakePlayback(film=None))
        self.assertIsNone(window.play_button)
        self.assertIsNotNone(window.plex_mark)


class ShowWindowTests(DialogTestCase):
    async def window(self, fake: FakePlayback, player: FakePlayer | None = None) -> ShowDetailsWindow:
        shows = ShowWindows(
            self.root,
            library=cast_to(Any, MarkedLibrary()),
            discovery=cast_to(Any, WindowDiscovery()),
            images=cast(Any, FakeImages()),
            status=Status(),
            episode_actions=DownloadActions[CalendarEntry](
                download=lambda entry: None, choose=lambda entry: None, states=DownloadStates()
            ),
            downloads_changed=no_subscribe,
            profile=lambda: 1,
            playback=cast(Any, fake),
            player=player or FakePlayer(),
        )
        self.addCleanup(shows.close)
        shows.open_show(SHOW)
        for _ in range(4):
            await self.settle()
        window = shows._live().get(SHOW.id)
        assert isinstance(window, ShowDetailsWindow)
        return window

    async def test_play_opens_the_episode_picker(self) -> None:
        player = FakePlayer()
        window = await self.window(FakePlayback(), player)
        assert window.play_button is not None
        window.play_button.invoke()
        assert window._play is not None
        picker = window._play.picker
        assert picker is not None
        await picker.loaded()
        await self.settle()
        picker.rows["2122"].invoke()
        await window._play.settled()
        self.assertEqual(
            [(show, video) for show, video, _episodes in player.played], [("1001", "2122")]
        )
        self.assertEqual(len(player.played[0][2]), len(leaves()))
        self.assertFalse(picker.winfo_exists())

    async def test_no_play_when_not_in_plex(self) -> None:
        window = await self.window(FakePlayback(show=None))
        self.assertIsNone(window.play_button)


if __name__ == "__main__":
    unittest.main()
