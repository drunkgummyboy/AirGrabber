"""▶ Play (app/services/playback.py, app/ui/play_control.py, the info windows): Plex's file of a
film and of a show's episodes (a fake Plex Media Server behind httpx.MockTransport, from
tests/fixtures/plex/), the server path mapped onto MediaForge's Series / Movies folders, the
player's choice (VLC found or not, the real starts never made), what the status bar says, the
episode picker, and the windows' "▶ Play" only while the title is in Plex. Nothing reaches the
network, no player starts, no real folder is looked at."""

from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest import mock

import httpx

from app.i18n import t
from app.i18n_nl import NL
from app.models import CalendarEntry, Episode, Settings, Show, ShowDetails, TitleExtras
from app.models.plex import PlexScan, PlexTitle, PlexVideo
from app.services import playback
from app.services.playback import (
    LibraryFolders,
    PlaybackService,
    PlayOutcome,
    PlayProblem,
    ShowTitles,
    TMDBEpisodeNames,
    episode_name,
    find_vlc,
    launch_video,
    local_candidates,
    next_episode,
    path_pairs,
    settings_folders,
    windows_file,
)
from app.services.playback import EPISODES_TTL_S
from app.services import plex as plex_module
from app.services.plex import FAIL_FAST_S, PlexError, PlexUnreachable
from app.services.settings import SettingsService
from app.ui import library_words, play_control, theme
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.library_words import HaveSource
from app.ui.movie_details import MovieDetailsWindow, MovieWindows
from app.ui.play_control import PLAY_TEXT, EpisodePicker, Play, episode_line
from app.ui.show_details import ShowDetailsWindow, ShowWindows
from app.ui.title_info import SendToPhone
from tests.test_backup_dialog import DialogTestCase
from app.services.folder_library.playing import is_folder_video
from tests.test_folder_playing import DUNE as DUNE_FOLDER
from tests.test_folder_playing import SEVERANCE as SEVERANCE_FOLDER
from tests.test_folder_playing import NOW, FolderTestCase, nothing
from tests.test_info_layouts import SHOW, WindowDiscovery, cast_to, no_subscribe
from tests.test_info_polish import MarkedLibrary
from tests.test_info_window import INFO, FILM, FakeDiscovery, FakeImages, FakeWatchlist
from tests.test_plex import LOCAL_HTTP, LOCAL_HTTPS, TOKEN, FakePlex, PlexTestCase, fixture

SERIES = r"\\Alex-NAS\video\TV"
MOVIES = r"\\Alex-NAS\video\Movies"
DUNE_FILE = "/volume1/video/Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv"
DUNE_HERE = MOVIES + r"\Dune Part Two (2024)\Dune Part Two (2024).mkv"
ARK_E02 = "The Ark - S02E02 - Woe's Hollow.mkv"
ARK_E02_HERE = SERIES + r"\The Ark (2023)\Season 02" + "\\" + ARK_E02
PRIVATE = ("Dune", "Ark", "volume1", "Alex-NAS", ".mkv")  # never in a log line


class PlayingPlex(FakePlex):
    """FakePlex that also answers /library/metadata/{key} and …/allLeaves."""

    films_page: Any = None  # /library/metadata/3001 instead of the fixture

    def _server(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/library/metadata/") and self._authorised(request):
            if path == "/library/metadata/3001":
                return httpx.Response(200, json=self.films_page or fixture("metadata_movie.json"))
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

    async def test_play_picks_the_4k_version_over_1080p(self) -> None:
        film = fixture("metadata_movie.json")
        item = film["MediaContainer"]["Metadata"][0]
        item["Media"].reverse()  # the 1080p version first
        # Two 4K versions: the bigger file; a version in two parts plays its first.
        small = copy.deepcopy(item["Media"][1])
        small["Part"][0].update(size=30000000000, file="/volume1/video/Movies/Dune small.mkv")
        split = copy.deepcopy(item["Media"][1])
        split["Part"] = [
            {**split["Part"][0], "size": 70000000000, "file": "/volume1/video/Movies/Dune cd1.mkv"},
            {**split["Part"][0], "size": 1000, "file": "/volume1/video/Movies/Dune cd2.mkv"},
        ]
        assert isinstance(self.fake, PlayingPlex)
        self.fake.films_page = film
        video = await self.plex.film_video("3001")
        assert video is not None
        self.assertEqual(video.file, DUNE_FILE)  # the 4K one, though Plex names 1080p first
        small["Part"][0]["key"] = "/library/parts/53/1700000000/file.mkv"
        item["Media"].insert(1, small)  # 1080p, the small 4K, the big 4K
        self.assertEqual((await self.plex.film_video("3001") or video).file, DUNE_FILE)
        # The phone-sized copy keeps Plex's first of equally tall versions.
        copy_file = await self.plex.film_file("3001")
        assert copy_file is not None
        self.assertEqual(copy_file.part_key, "/library/parts/53/1700000000/file.mkv")
        # A version in one file beats a bigger one in two parts of the same height.
        item["Media"].append(split)
        self.assertEqual((await self.plex.film_video("3001") or video).file, DUNE_FILE)
        # The only 4K version in two parts: its first part plays, the parts logged.
        item["Media"] = [item["Media"][0], split]
        with self.assertLogs("app.services.plex", "INFO") as logs:
            found = await self.plex.film_video("3001")
        assert found is not None
        self.assertEqual(found.file, "/volume1/video/Movies/Dune cd1.mkv")
        self.assertIn("film has 2 parts", "\n".join(logs.output))
        self.assertFalse([line for line in logs.output if "Dune" in line or "volume1" in line])

    async def test_unreachable_last_check_fails_at_once(self) -> None:
        self.fake.answering = set()
        self.fake.plex_tv_down = True
        await self.plex.check()  # Plex unreachable
        self.fake.answering = {LOCAL_HTTPS}
        self.fake.plex_tv_down = False
        self.fake.requests.clear()
        with self.assertRaises(PlexUnreachable):
            await self.plex.film_video("3001")
        with self.assertRaises(PlexUnreachable):
            await self.plex.show_videos(PlexTitle(kind="show", key="1001", title="The Ark"))
        self.assertEqual(self.fake.requests, [])  # not even asked: it said so a moment ago
        self.clock.now += timedelta(seconds=FAIL_FAST_S + 1)
        self.assertIsNotNone(await self.plex.film_video("3001"))

    async def test_a_server_that_answers_but_hangs_on_reads_fails_fast(self) -> None:
        await self.plex.check()
        real = self.fake._server

        def stuck(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/library/metadata/"):
                return httpx.Response(503)  # /identity answers, the library doesn't
            return real(request)

        self.fake._server = stuck  # type: ignore[method-assign]
        with self.assertRaises(PlexUnreachable):
            await self.plex.film_video("3001")
        self.fake.requests.clear()
        with self.assertRaises(PlexUnreachable):
            await self.plex.film_video("3001")
        self.assertEqual(self.fake.requests, [])  # not another wait

    async def test_a_hung_nas_ends_within_the_screen_timeout_and_the_reach(self) -> None:
        # The owner's case: every address (and plex.tv) takes the connection and never
        # answers. The mock waits out the request's own read timeout, as httpcore would.
        await self.plex.check()

        async def never(request: httpx.Request) -> httpx.Response:
            timeout = request.extensions.get("timeout", {})
            await asyncio.sleep(timeout.get("read") or 5.0)
            raise httpx.ReadTimeout("no answer", request=request)

        self.http._transport = httpx.MockTransport(never)
        screen = httpx.Timeout(0.2, connect=0.1)
        with (
            mock.patch.object(plex_module, "SCREEN_TIMEOUT", screen),
            mock.patch.object(plex_module, "IDENTITY_TIMEOUT", httpx.Timeout(0.1)),
            mock.patch.object(plex_module, "SCREEN_REACH_S", 0.2),
        ):
            started = time.monotonic()
            with self.assertRaises(PlexUnreachable):
                await self.plex.film_video("3001")
            took = time.monotonic() - started
            self.assertLess(took, 0.2 + 0.2 + 0.3)
            started = time.monotonic()
            with self.assertRaises(PlexUnreachable):
                await self.plex.show_videos(PlexTitle(kind="show", key="1001", title="The Ark"))
            self.assertLess(time.monotonic() - started, 0.05)  # said at once now
        self.assertEqual(plex_module.SCREEN_REACH_S, 2.0)  # 8 s + 2 s: about 10 s

    async def test_a_screen_call_finds_the_server_again_once(self) -> None:
        await self.plex.check()
        self.assertEqual(self.settings.current.plex_server_uri, LOCAL_HTTPS)
        self.fake.answering = {LOCAL_HTTP}  # the address changed
        video = await self.plex.film_video("3001")
        assert video is not None
        self.assertEqual(self.settings.current.plex_server_uri, LOCAL_HTTP)  # stored
        # Nowhere to be found: it gives up, and the next screen call fails at once.
        self.fake.answering = set()
        self.fake.plex_tv_down = True
        with self.assertRaises(PlexUnreachable):
            await self.plex.film_video("3001")
        self.fake.requests.clear()
        with self.assertRaises(PlexUnreachable):
            await self.plex.film_video("3001")
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


# The owner's NAS (3.26.0): four libraries on one share, Settings → Folders set for two of them.
OWNER_LOCATIONS = [
    "/volume1/Multimedia/Movies",
    "/volume1/Multimedia/Series",
    "/volume1/Multimedia/Downloaded and To See/movies",
    "/volume1/Multimedia/Downloaded and To See/series",
]
OWNER_FILM = "/volume1/Multimedia/Movies/Dune (2021)/Dune (2021).mkv"
NAS = r"\\Owner-NAS"
OWNER_FOLDERS = [
    NAS + r"\Multimedia\Downloaded and To See\movies",
    NAS + r"\Multimedia\Downloaded and To See\series",
]


class PathPairTests(unittest.TestCase):
    """`path_pairs`: where the server's files are from this PC, learned from the folders this PC
    knows and the libraries' folders (pure)."""

    def test_the_owners_shape(self) -> None:
        pairs = path_pairs(OWNER_FOLDERS, OWNER_LOCATIONS)
        self.assertEqual(len(pairs), 1)  # /volume1 ↔ \\Owner-NAS, learned twice, kept once
        (pair,) = pairs
        self.assertEqual((pair.server, pair.root, pair.names), (("volume1",), NAS + "\\", ()))
        self.assertEqual(pair.shared, 3)  # Multimedia/Downloaded and To See/movies
        self.assertEqual(
            pair.mapped(OWNER_FILM), NAS + r"\Multimedia\Movies\Dune (2021)\Dune (2021).mkv"
        )
        self.assertEqual(
            pair.mapped("/volume1/Multimedia/Series/The Ark/Season 01/e1.mkv"),
            NAS + r"\Multimedia\Series\The Ark\Season 01\e1.mkv",
        )

    def test_a_shorter_ending_of_another_library_teaches_nothing(self) -> None:
        # "movies" alone is also /volume1/Multimedia/Movies' ending: not a pair of its own.
        pairs = path_pairs(OWNER_FOLDERS[:1], OWNER_LOCATIONS)
        self.assertEqual([pair.root + "\\".join(pair.names) for pair in pairs], [NAS + "\\"])

    def test_a_windows_plex(self) -> None:
        pairs = path_pairs([r"\\Alex-PC\Media\Films"], [r"D:\Media\Films", r"D:\Media\Series"])
        self.assertEqual([(p.windows, p.server, p.root) for p in pairs], [(True, ("D:",), "\\\\Alex-PC\\")])
        self.assertEqual(
            pairs[0].mapped(r"D:\Media\Series\The Ark\Season 01\e1.mkv"),
            r"\\Alex-PC\Media\Series\The Ark\Season 01\e1.mkv",
        )
        self.assertIsNone(pairs[0].mapped(r"E:\Media\Series\e1.mkv"))  # another drive
        self.assertIsNone(pairs[0].mapped("/volume1/Media/e1.mkv"))  # not a Windows path
        # A drive this PC sees as the server does: Plex's own path already finds it.
        self.assertEqual(path_pairs([r"D:\Media\Films"], [r"D:\Media\Films"]), [])
        # A drive of its own on this PC (the share mapped as M:).
        (mapped,) = path_pairs([r"M:\Films"], [r"D:\Media\Films"])
        self.assertEqual(mapped.mapped(r"D:\Media\Series\x.mkv"), r"M:\Series\x.mkv")

    def test_several_pairs(self) -> None:
        pairs = path_pairs(
            [NAS + r"\Multimedia\Downloaded and To See\movies", r"\\Other-NAS\tv\Shows", r"M:\Kids"],
            [
                "/volume1/Multimedia/Downloaded and To See/movies",
                "/volume2/tv/Shows",
                "/data/kids",
            ],
        )
        self.assertEqual(len(pairs), 3)
        film = "/volume2/tv/Shows/The Ark/e1.mkv"
        self.assertEqual(
            [found for pair in pairs if (found := pair.mapped(film))],
            [r"\\Other-NAS\tv\Shows\The Ark\e1.mkv"],
        )
        self.assertIn(r"M:\kids\Bluey\e1.mkv", [pair.mapped("/data/kids/Bluey/e1.mkv") for pair in pairs])
        # A Docker Plex's /movies: the share's own root pairs with the server's root.
        (docker,) = path_pairs([NAS + r"\media\movies"], ["/movies", "/tv"])
        self.assertEqual(docker.mapped("/tv/The Ark/e1.mkv"), NAS + r"\media\tv\The Ark\e1.mkv")

    def test_no_match(self) -> None:
        self.assertEqual(path_pairs([NAS + r"\Films"], OWNER_LOCATIONS), [])
        self.assertEqual(path_pairs([], OWNER_LOCATIONS), [])
        self.assertEqual(path_pairs(OWNER_FOLDERS, []), [])
        self.assertEqual(path_pairs(["", "relative\\movies", "D:movies"], OWNER_LOCATIONS), [])

    def test_compared_as_windows_does(self) -> None:
        (pair,) = path_pairs(
            [NAS + r"\multimedia\DOWNLOADED and to see\Movies"],
            ["/volume1/Multimedia/Downloaded and To See/movies"],
        )
        self.assertEqual(pair.shared, 3)
        self.assertEqual(
            pair.mapped("/Volume1/Multimedia/Movies/Dune/Dune.mkv"),
            NAS + r"\Multimedia\Movies\Dune\Dune.mkv",
        )

    def test_never_above_the_share(self) -> None:
        (pair,) = path_pairs(OWNER_FOLDERS[:1], OWNER_LOCATIONS)
        self.assertIsNone(pair.mapped("/volume1/../secret/a.mkv"))
        self.assertIsNone(pair.mapped("/volume1"))  # nothing below it
        self.assertIsNone(pair.mapped("/volume2/Multimedia/Movies/a.mkv"))


class LocationsPlex:
    """PlexService's part `locate` reads: connected, and the libraries' folders (Locations)."""

    connected = True

    def __init__(self, locations: Sequence[str]) -> None:
        self.listed = [(str(number), path) for number, path in enumerate(locations, 1)]

    async def locations(self) -> list[tuple[str, str]]:
        return list(self.listed)


class PathPairPlayTests(unittest.IsolatedAsyncioTestCase):
    """▶ Play (and the cast stream, which goes through `locate`) finds a Plex file through the
    path pairs: temporary folders as the share, POSIX paths as the NAS's."""

    FILM = PlexTitle(kind="movie", key="3001", title="Dune")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = os.path.realpath(temporary.name)  # stands for \\Owner-NAS
        self.film_here = os.path.join(self.root, "Multimedia", "Movies", "Dune (2021)", "Dune (2021).mkv")
        os.makedirs(os.path.dirname(self.film_here))
        with open(self.film_here, "wb"):
            pass
        downloaded = os.path.join(self.root, "Multimedia", "Downloaded and To See")
        self.films = os.path.join(downloaded, "movies")
        self.series = os.path.join(downloaded, "series")
        self.own: dict[str, list[str]] = {"movie": [self.films], "show": [self.series]}
        self.forge: LibraryFolders | None = None

    def make(self, locations: Sequence[str] = OWNER_LOCATIONS, **kwargs: Any) -> PlaybackService:
        async def forge() -> LibraryFolders | None:
            return self.forge

        return PlaybackService(
            cast(Any, LocationsPlex(locations)),
            forge,
            own_folders=lambda kind: self.own[kind],
            launch=FakeLauncher(),
            **kwargs,
        )

    async def test_the_owners_film_in_a_library_no_folder_is_set_for(self) -> None:
        with self.assertLogs("app.services.playback", "INFO") as logs:
            found = await self.make().locate(self.FILM, PlexVideo(key="3001", file=OWNER_FILM))
        assert isinstance(found, playback.Located), found
        self.assertEqual(found.path, self.film_here)
        text = "\n".join(logs.output)
        self.assertIn("learned 1 path pair(s)", text)
        self.assertIn("a path pair", text)
        private = ("Dune", "volume1", "Multimedia", "Downloaded", self.root, os.path.basename(self.root))
        self.assertFalse([line for line in logs.output if any(p in line for p in private)])

    async def test_learned_from_mediaforges_folders_too(self) -> None:
        self.own = {"movie": [], "show": []}
        self.forge = LibraryFolders(series=self.series)
        found = await self.make().locate(self.FILM, PlexVideo(key="3001", file=OWNER_FILM))
        assert isinstance(found, playback.Located), found
        self.assertEqual(found.path, self.film_here)

    async def test_a_windows_plex(self) -> None:
        # Plex on another Windows PC: D:\Media is this PC's <root>\Media.
        episode = os.path.join(self.root, "Media", "Series", "The Ark", "e1.mkv")
        os.makedirs(os.path.dirname(episode))
        with open(episode, "wb"):
            pass
        self.own = {"movie": [os.path.join(self.root, "Media", "Films")], "show": []}
        service = self.make([r"Q:\Media\Films", r"Q:\Media\Series"])
        show = PlexTitle(kind="show", key="1001", title="The Ark")
        found = await service.locate(show, PlexVideo(key="7", file=r"Q:\Media\Series\The Ark\e1.mkv"))
        assert isinstance(found, playback.Located), found
        self.assertEqual(found.path, episode)

    async def test_no_match_the_outcome_as_before(self) -> None:
        self.own = {"movie": [os.path.join(self.root, "Films")], "show": []}
        with self.assertLogs("app.services.playback", "INFO") as logs:
            found = await self.make().locate(self.FILM, PlexVideo(key="3001", file=OWNER_FILM))
        assert isinstance(found, PlayOutcome), found
        self.assertEqual(found.problem, PlayProblem.NOT_FOUND)
        self.assertIn("isn't in your Movies folder", found.text)
        self.assertIn("0 of 0 path pair(s) fit", "\n".join(logs.output))

    async def test_the_order(self) -> None:
        # Plex's own path first, then the path pair, then the folders' own guesses.
        own_path = r"Q:\Media\Films\Dune\Dune.mkv"
        mapped = r"\\Alex-PC\Media\Films\Dune\Dune.mkv"
        guessed = r"\\Alex-PC\Media\Series\Dune\Dune.mkv"  # below the Series folder
        self.own = {"movie": [r"\\Alex-PC\Media\Series"], "show": []}
        here = {own_path, mapped, guessed}
        service = self.make([r"Q:\Media\Films", r"Q:\Media\Series"], exists=lambda p: p in here)
        video = PlexVideo(key="3001", file=own_path)
        for expected in (own_path, mapped, guessed):
            with self.subTest(expected=expected):
                found = await service.locate(self.FILM, video)
                assert isinstance(found, playback.Located), found
                self.assertEqual(found.path, expected)
                here.discard(expected)
        missing = await service.locate(self.FILM, video)
        assert isinstance(missing, PlayOutcome)
        self.assertEqual(missing.problem, PlayProblem.NOT_FOUND)

    async def test_a_share_that_doesnt_answer(self) -> None:
        mapped = os.path.join(self.root, "Multimedia", "Movies")

        def asleep(path: str) -> bool:
            if path.startswith(mapped):
                time.sleep(0.3)  # the share holding the film is asleep
            return False

        service = self.make(exists=asleep, timeout_s=0.05)
        with self.assertLogs("app.services.playback", "INFO") as logs:
            found = await service.locate(self.FILM, PlexVideo(key="3001", file=OWNER_FILM))
        assert isinstance(found, PlayOutcome), found
        self.assertEqual(found.problem, PlayProblem.TIMED_OUT)
        self.assertIn("one didn't answer in time", "\n".join(logs.output))


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
        self.assertEqual(found, ShowTitles(plex=self.plex.main))

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
        outcome = await self.service.play(ark.main, chosen)
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
        with self.assertLogs("app.services.playback", "INFO") as logs:
            outcome = await service.play_film(await self.dune())
        self.assertEqual(outcome.problem, PlayProblem.TIMED_OUT)  # not "isn't in your folder"
        self.assertEqual(outcome.text, "The folder didn't answer in time. Try again.")
        self.assertFalse([line for line in logs.output if any(p in line for p in PRIVATE)])

    def test_the_timed_out_text_in_dutch(self) -> None:
        self.assertIn("The folder didn't answer in time. Try again.", NL)

    async def test_a_folder_that_hangs_leaves_the_others_theirs(self) -> None:
        films = r"D:\Films"

        def slow_share(path: str) -> bool:
            if path.startswith(films):
                time.sleep(0.5)  # a NAS asleep
                return False
            return path in self.here

        for own in ([films], [MOVIES, films]):
            with self.subTest(own=own):
                self.launch.paths.clear()
                service = self.make(own_folders=lambda kind, own=own: own, timeout_s=0.2)
                service._exists = slow_share
                started = time.monotonic()
                outcome = await service.play_film(await self.dune())
                self.assertTrue(outcome.played, outcome)
                self.assertEqual(self.launch.paths, [DUNE_HERE])  # MediaForge's / the 2nd
                self.assertLess(time.monotonic() - started, 0.45)  # within the overall limit

    async def test_plex_on_this_pc_its_own_file_name_first(self) -> None:
        here = r"D:\Media\Movies\Dune Part Two (2024).mkv"
        self.here = {here}
        self.folders = None  # MediaForge off: not needed
        video = PlexVideo(key="3001", file=here)
        found = await self.service.locate(await self.dune(), video)
        assert isinstance(found, playback.Located), found
        self.assertEqual(found.path, here)
        self.here = set()
        missing = await self.service.locate(await self.dune(), video)
        assert isinstance(missing, PlayOutcome)
        self.assertEqual(missing.problem, PlayProblem.FORGE_OFF)  # nowhere else to look

    def test_windows_file(self) -> None:
        self.assertTrue(windows_file(r"D:\Media\Dune.mkv"))
        self.assertTrue(windows_file(r"\\nas\video\Dune.mkv"))
        self.assertTrue(windows_file("C:/Media/Dune.mkv"))
        for no in ("/volume1/video/Dune.mkv", "Dune.mkv", r"Media\Dune.mkv", "", "D:Dune.mkv"):
            self.assertFalse(windows_file(no), no)

    async def test_the_own_folders_first_whatever_their_switch(self) -> None:
        films = r"E:\Films"
        mine = films + r"\Dune Part Two (2024)\Dune Part Two (2024).mkv"
        forges = r"\\Alex-NAS\Films"  # no library's ending: it teaches no path pair
        self.folders = LibraryFolders(movies=forges)
        self.here = {mine, forges + r"\Dune Part Two (2024)\Dune Part Two (2024).mkv"}
        await self.settings.update(film_folders=(films,), folder_library_on=False)
        service = self.make(own_folders=settings_folders(lambda: self.settings.current))
        self.assertTrue((await service.play_film(await self.dune())).played)
        self.assertEqual(self.launch.paths, [mine])  # before MediaForge's Movies folder
        self.folders = None  # MediaForge off: the own folders still find it
        self.assertTrue((await service.play_film(await self.dune())).played)
        self.here = set()
        outcome = await service.play_film(await self.dune())
        self.assertEqual(outcome.problem, PlayProblem.NOT_FOUND)  # not FORGE_OFF: looked

    async def test_mediaforges_folders(self) -> None:
        dune = await self.dune()
        self.folders = None
        outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.FORGE_OFF)
        self.folders = LibraryFolders(series=SERIES)
        # The Series folder is the share's video/TV: the film is found on the same share.
        self.assertTrue((await self.service.play_film(dune)).played)
        self.assertEqual(self.launch.paths, [DUNE_HERE])
        self.here = set()
        outcome = await self.service.play_film(dune)
        self.assertEqual(outcome.problem, PlayProblem.NO_FOLDER)  # looked, but no Movies folder
        self.assertIn("Movies folder in Settings › Folders", outcome.text)

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

    async def test_the_libraries_folders_are_kept_from_the_check(self) -> None:
        self.fake.requests.clear()
        dune = await self.dune()
        for _ in range(2):
            self.assertTrue((await self.service.play_film(dune)).played)
        sections = [r for r in self.fake.requests if r.url.path == "/library/sections"]
        self.assertEqual(sections, [])  # the 30-minute check's answer, kept for the run
        await self.plex.check()  # and refreshed by it
        sections = [r for r in self.fake.requests if r.url.path == "/library/sections"]
        self.assertEqual(len(sections), 1)
        self.assertEqual(len(self.launch.paths), 2)

    async def test_a_shows_episodes_are_kept_for_five_minutes(self) -> None:
        now = [1000.0]
        service = self.make(clock=lambda: now[0])
        ark = await service.show(Show(id=101, name="The Ark", tvdb_id=412345))
        assert ark is not None
        self.assertIsNone(service.kept_episodes(ark))
        videos = await service.episodes(ark)
        self.assertTrue(videos)
        now[0] += EPISODES_TTL_S - 1
        self.assertEqual(service.kept_episodes(ark), videos)
        now[0] += 2
        self.assertIsNone(service.kept_episodes(ark))  # too old to show

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
        self._show_titles: ShowTitles | None = None
        self.available = True
        self.fail = False
        self.film_problem: PlayProblem | None = None

    async def film(self, tmdb_id: int | None, imdb_id: str | None = None) -> PlexTitle | None:
        return self._film

    async def show(self, show: Show) -> ShowTitles | None:
        if self._show_titles is not None:
            return self._show_titles
        return ShowTitles(plex=self._show) if self._show is not None else None

    async def episodes(self, title: ShowTitles | PlexTitle) -> list[PlexVideo]:
        if self.fail:
            raise PlexUnreachable("away")
        return list(self._videos) if self._videos is not None else leaves()

    def kept_episodes(self, title: ShowTitles | PlexTitle) -> list[PlexVideo] | None:
        return None

    async def film_video(self, title: PlexTitle) -> PlexVideo | PlayOutcome:
        if self.film_problem is not None:
            return PlayOutcome(title.title, "movie", problem=self.film_problem)
        return PlexVideo(key=title.key, file=DUNE_FILE, view_offset_ms=60000, duration_ms=9000000)


class FakePlayer:
    """MediaPlayerHost's `play` (app/ui/media_player.py): what it was asked to play."""

    def __init__(self) -> None:
        self.played: list[tuple[str, str, tuple[str, ...]]] = []

    def play(
        self,
        title: PlexTitle,
        video: PlexVideo,
        *,
        episodes: Sequence[PlexVideo] = (),
        ask_resume: bool = True,
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

    async def test_the_picker_shows_a_kept_list_at_once(self) -> None:
        kept = half_watched()
        fresh = [v.model_copy(update={"view_offset_ms": 0}) for v in kept]
        gate = asyncio.Event()
        fail = False

        async def episodes(title: PlexTitle) -> list[PlexVideo]:
            await gate.wait()  # Plex takes its time
            if fail:
                raise PlexUnreachable("away")
            return fresh

        picker = EpisodePicker(
            self.root, ARK, episodes=episodes, kept=lambda title: kept, on_play=lambda v, e: None
        )
        self.keep(picker)
        await self.settle()
        self.assertEqual(list(picker.progress), ["2122"])  # the kept list, before Plex answers
        self.assertEqual(picker.note.cget("text"), "Choose an episode to play.")
        assert picker.selected is not None
        self.assertEqual(picker.selected.key, "2121")
        gate.set()
        await picker.loaded()
        await self.settle()
        self.assertEqual(picker.progress, {})  # refreshed behind the scenes
        self.assertEqual(len(picker.rows), 4)
        picker.close()

        # A refresh that fails keeps the list on screen.
        fail = True
        with self.assertLogs("app.ui.play_control", "INFO"):
            second = EpisodePicker(
                self.root, ARK, episodes=episodes, kept=lambda title: kept,
                on_play=lambda v, e: None,
            )
            self.keep(second)
            await second.loaded()
            await self.settle()
        self.assertEqual(list(second.progress), ["2122"])
        self.assertEqual(second.note.cget("text"), "Choose an episode to play.")

    async def test_a_kept_list_plex_now_has_empty_leaves_no_gap(self) -> None:
        async def none_now(title: PlexTitle) -> list[PlexVideo]:
            return []

        picker = EpisodePicker(
            self.root, ARK, episodes=none_now, kept=lambda title: leaves(),
            on_play=lambda v, e: None,
        )
        self.keep(picker)
        await picker.loaded()
        await self.settle()
        self.assertEqual((picker.rows, picker.chips), ({}, {}))
        self.assertEqual(picker._chip_row.winfo_manager(), "")  # the chip row is gone too
        self.assertNotEqual(picker.note.cget("text"), "Choose an episode to play.")

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
        self,
        *,
        in_plex: bool,
        fake: FakePlayback,
        player: FakePlayer | None = None,
        send: SendToPhone | None = None,
    ) -> tuple[MovieDetailsWindow, Status]:
        actions = mock.MagicMock()
        actions.states.get.return_value = None
        status = Status()

        async def plex(movie_id: int, imdb_id: str | None) -> HaveSource | None:
            return "plex" if in_plex else None

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
        self.windows = windows
        windows.open_movie(FILM, send=send)
        for _ in range(3):
            await self.settle()
        [window] = windows._live().values()
        return window, status

    async def test_send_to_phone_from_the_library_only(self) -> None:
        # The Library page's cards had it until the owner's choice A + 2: now its windows.
        from app.ui.title_info import SEND_TEXT

        window, _status = await self.window(in_plex=True, fake=FakePlayback())
        self.assertIsNone(window.send_button)  # opened from elsewhere: none
        sent: list[str] = []
        overs: list[Any] = []

        def send(over: Any) -> None:
            sent.append("dune")
            overs.append(over)

        self.windows.open_movie(FILM, send=send)  # again, from the Library
        await self.settle()
        button = window.send_button
        assert button is not None
        self.assertEqual(button.cget("text"), t(SEND_TEXT))
        self.assertIsNotNone(button.cget("image"))  # a drawn phone: 📱 is a box in Tk
        items = list(window.buttons.items)
        self.assertEqual(items.index(button), items.index(window.watch_button) + 1)
        button.invoke()
        self.assertEqual(sent, ["dune"])
        self.assertEqual(overs, [window])  # the dialog opens over this window
        self.windows.open_movie(FILM)  # from elsewhere again: it keeps it
        await self.settle()
        self.assertIs(window.send_button, button)
        self.assertIn(button, window.buttons.items)
        self.assertEqual(NL[SEND_TEXT], "Naar telefoon sturen")

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

        async def plex(movie_id: int, imdb_id: str | None) -> HaveSource | None:
            return "plex"

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


class ShowWindowCase(DialogTestCase):
    """A show's window through ShowWindows, with fakes for the playing."""

    status: Status

    async def window(
        self,
        fake: FakePlayback,
        player: FakePlayer | None = None,
        send: SendToPhone | None = None,
        library: MarkedLibrary | None = None,
    ) -> ShowDetailsWindow:
        self.status = Status()
        shows = ShowWindows(
            self.root,
            library=cast_to(Any, library or MarkedLibrary()),
            discovery=cast_to(Any, WindowDiscovery()),
            images=cast(Any, FakeImages()),
            status=self.status,
            episode_actions=DownloadActions[CalendarEntry](
                download=lambda entry: None, choose=lambda entry: None, states=DownloadStates()
            ),
            downloads_changed=no_subscribe,
            profile=lambda: 1,
            playback=cast(Any, fake),
            player=player or FakePlayer(),
        )
        self.addCleanup(shows.close)
        self.shows = shows
        shows.open_show(SHOW, send=send)
        for _ in range(4):
            await self.settle()
        window = shows._live().get(SHOW.id)
        assert isinstance(window, ShowDetailsWindow)
        return window


class ShowWindowTests(ShowWindowCase):
    async def test_send_to_phone_from_the_library(self) -> None:
        from app.ui.title_info import SEND_TEXT

        sent: list[str] = []
        overs: list[Any] = []

        def send(over: Any) -> None:
            sent.append("severance")
            overs.append(over)

        window = await self.window(FakePlayback(), send=send)
        button = window.send_button
        assert button is not None
        self.assertEqual(button.cget("text"), t(SEND_TEXT))
        items = list(window._buttons.items)
        assert window.play_button is not None
        self.assertEqual(items.index(button), items.index(window.play_button) + 1)  # before IMDb
        button.invoke()
        self.assertEqual(sent, ["severance"])
        self.assertEqual(overs, [window])  # the dialog opens over this window
        self.shows.open_show(SHOW)  # from elsewhere: it keeps it
        await self.settle()
        assert window.send_button is not None
        self.assertIn(window.send_button, window._buttons.items)
        # Opened from elsewhere first: none, until the Library opens it again.
        other = await self.window(FakePlayback(), send=None)
        self.assertIsNone(other.send_button)
        self.shows.open_show(SHOW, send=lambda over: sent.append("again"))
        await self.settle()
        assert other.send_button is not None
        other.send_button.invoke()
        self.assertEqual(sent, ["severance", "again"])

    async def test_play_plays_the_next_unwatched_one_without_a_picker(self) -> None:
        player = FakePlayer()
        window = await self.window(FakePlayback(), player)
        assert window.play_button is not None and window._play is not None
        window.play_button.invoke()
        await window._play.settled()
        everything = tuple(video.key for video in leaves())
        self.assertEqual(player.played, [("1001", "2122", everything)])  # S02E02, the whole list
        self.assertIsNone(window._play.picker)
        self.assertEqual(self.status.lines[-1:], ["▶ Finding The Ark S02E02…"])

    async def test_no_play_when_not_in_plex(self) -> None:
        window = await self.window(FakePlayback(show=None))
        self.assertIsNone(window.play_button)

    async def test_a_show_window_with_only_the_folders_plays(self) -> None:
        player = FakePlayer()
        fake = FakePlayback()
        fake._show_titles = ShowTitles(folders=SEVERANCE_FOLDER)
        window = await self.window(fake, player)
        assert window.play_button is not None and window._play is not None
        window.play_button.invoke()
        await window._play.settled()
        self.assertEqual([show for show, _video, _e in player.played], [SEVERANCE_FOLDER.key])
        self.assertIsNone(window._play.picker)


def ark_video(season: int, number: int, *, watched: bool = False) -> PlexVideo:
    """An episode this PC can play, numbered like the window's rows (MarkedLibrary's season 3)."""
    return PlexVideo(
        key=f"9{season}{number}",
        file=f"/tv/Harbour Lights/S{season:02d}E{number:02d}.mkv",
        season=season,
        episode=number,
        title=f"Part {number}",
        watched=watched,
    )


class TwoSeasons(MarkedLibrary):
    """MarkedLibrary's season 3 and an older season 2 with two episodes."""

    async def show_details(self, show_id: int, *, profile_id: int) -> ShowDetails:
        details = await super().show_details(show_id, profile_id=profile_id)
        aired = datetime.now(UTC) - timedelta(days=400)
        older = tuple(
            Episode(
                id=200 + number, show_id=SHOW.id, season=2, number=number, name=f"Old {number}",
                airstamp=aired, air_date=aired.date(),
            )
            for number in (1, 2)
        )
        return details.model_copy(update={"episodes": older + details.episodes})


class ShowRowPlayTests(ShowWindowCase):
    """Each episode row this PC can play has its own ▶ (the header's ▶ Play the next one)."""

    def played_rows(self, window: ShowDetailsWindow) -> dict[int, str | None]:
        """The shown rows' ▶ by episode id: its video's key, "" for the room only, None for
        neither."""
        rows: dict[int, str | None] = {}
        for episode_id, row in window._rows.items():
            if row.play_button is not None:
                assert row.play_video is not None
                rows[episode_id] = row.play_video.key
            else:
                rows[episode_id] = "" if row._play_slot is not None else None
        return rows

    async def test_a_playable_rows_play_plays_it_with_the_whole_list(self) -> None:
        videos = [ark_video(3, 1, watched=True), ark_video(3, 3)]
        player = FakePlayer()
        tips: list[str] = []
        with mock.patch.object(play_control, "add_tooltip", lambda _w, text: tips.append(text)):
            window = await self.window(FakePlayback(videos=videos), player)
        # 302 and 304 can't be played: no ▶, but its room, so the rows stay aligned.
        self.assertEqual(self.played_rows(window), {301: "931", 302: "", 303: "933", 304: ""})
        row = window._rows[303]
        assert row.play_button is not None and row._play_slot is not None
        self.assertEqual(row.play_button.cget("text"), "▶")
        self.assertEqual(row.play_button.cget("text_color"), theme.BUTTON_TEXT)
        self.assertEqual(row.play_button.cget("border_width"), 1)
        self.assertEqual(row._play_slot.grid_info()["column"], 4)
        self.assertEqual(row._slot.grid_info()["column"], 5)  # left of ⬇ Auto
        # Each row's ▶ (and the header's, before the list was read) says "this PC".
        self.assertEqual(tips.count("Play it on this PC"), 3)
        self.assertIn("Play S03E03 on this PC", tips)  # the header, once the list was read
        row.play_button.invoke()
        self.assertEqual(player.played, [("1001", "933", ("931", "933"))])
        assert window._play is not None
        self.assertIsNone(window._play.picker)

    async def test_the_header_plays_the_next_unwatched_one_and_says_which(self) -> None:
        videos = [ark_video(3, 1, watched=True), ark_video(3, 2, watched=True), ark_video(3, 3)]
        player = FakePlayer()
        tips: list[str] = []
        with mock.patch.object(play_control, "add_tooltip", lambda _w, text: tips.append(text)):
            window = await self.window(FakePlayback(videos=videos), player)
        self.assertIn("Play S03E03 on this PC", tips)
        self.assertEqual(NL[play_control.PLAY_NEXT_TIP], "{code} afspelen op deze pc")
        assert window.play_button is not None and window._play is not None
        window.play_button.invoke()
        self.assertEqual(player.played, [("1001", "933", ("931", "932", "933"))])
        self.assertIsNone(window._play.picker)  # no picker in the show window any more

    async def test_a_season_switch_and_an_update_in_place_keep_them_right(self) -> None:
        fake = FakePlayback(videos=[ark_video(2, 1), ark_video(3, 1)])
        window = await self.window(fake, library=TwoSeasons())
        self.assertEqual(window._season, 3)
        self.assertEqual(self.played_rows(window), {301: "931", 302: "", 303: "", 304: ""})
        window._select_season(2)
        for _ in range(2):
            await self.settle()
        self.assertEqual(self.played_rows(window), {201: "921", 202: ""})
        window._select_season(3)
        for _ in range(2):
            await self.settle()
        rows = dict(window._rows)
        fake._videos = [ark_video(2, 1), ark_video(3, 1), ark_video(3, 4)]  # one more on the PC
        window.reload()
        for _ in range(3):
            await self.settle()
        self.assertEqual(window._rows, rows)  # updated in place, not built anew
        self.assertEqual(self.played_rows(window), {301: "931", 302: "", 303: "", 304: "934"})
        fake._videos = [ark_video(2, 1)]  # season 3 isn't on the PC any more: no room either
        window.reload()
        for _ in range(3):
            await self.settle()
        self.assertEqual(self.played_rows(window), {301: None, 302: None, 303: None, 304: None})

    async def test_nothing_playable_no_buttons(self) -> None:
        window = await self.window(FakePlayback(videos=[]))
        self.assertIsNone(window.play_button)  # nothing to play: hidden, as before
        self.assertEqual(set(self.played_rows(window).values()), {None})
        other = await self.window(FakePlayback(show=None))
        self.assertIsNone(other.play_button)
        self.assertEqual(set(self.played_rows(other).values()), {None})

    async def test_plex_away_no_row_buttons_and_the_header_says_so(self) -> None:
        fake, player = FakePlayback(videos=[ark_video(3, 1)]), FakePlayer()
        fake.fail = True
        with self.assertLogs("app.ui.show_details", "INFO"):
            window = await self.window(fake, player)
        self.assertEqual(set(self.played_rows(window).values()), {None})
        assert window.play_button is not None and window._play is not None
        with self.assertLogs("app.ui.play_control", "INFO"):
            window.play_button.invoke()
            await window._play.settled()
        self.assertEqual(player.played, [])
        self.assertEqual(
            self.status.lines[-1], "Plex didn't answer, so try ▶ Play again in a moment."
        )
        fake.fail = False  # Plex answers again: the header reads the list, then plays
        window.play_button.invoke()
        await window._play.settled()
        self.assertEqual(player.played, [("1001", "931", ("931",))])

    async def test_a_kept_list_shows_at_once_then_the_fresh_one(self) -> None:
        gate = asyncio.Event()

        class Slow(FakePlayback):
            def kept_episodes(self, title: ShowTitles | PlexTitle) -> list[PlexVideo] | None:
                return [ark_video(3, 1)]

            async def episodes(self, title: ShowTitles | PlexTitle) -> list[PlexVideo]:
                await gate.wait()  # Plex takes its time
                return [ark_video(3, 1), ark_video(3, 2)]

        window = await self.window(Slow())
        self.assertEqual(self.played_rows(window), {301: "931", 302: "", 303: "", 304: ""})
        gate.set()
        for _ in range(2):
            await self.settle()
        self.assertEqual(self.played_rows(window), {301: "931", 302: "932", 303: "", 304: ""})


# -- the folder library as the source (no Plex) ---------------------------------------------------


class FolderSourceTests(FolderTestCase):
    """▶ Play by source: the folder library's titles and files without a Plex token, nothing
    while neither is the library."""

    async def test_play_is_there_with_either_source(self) -> None:
        plex = mock.Mock(connected=False)
        playback = PlaybackService(plex, nothing, folder_library=self.playing)
        self.assertTrue(playback.available)
        await self.settings.update(folder_library_on=False)
        self.assertFalse(playback.available)  # no library at all
        plex.connected = True
        self.assertTrue(playback.available)  # Plex, as before
        self.assertFalse(PlaybackService(mock.Mock(connected=False), nothing).available)

    async def test_a_folder_film_without_files_cant_be_found(self) -> None:
        await self.db.library_files.clear()
        outcome = await self.playback().film_video(DUNE_FOLDER)
        assert isinstance(outcome, PlayOutcome)
        self.assertEqual(outcome.problem, PlayProblem.NOT_FOUND)
        self.assertEqual(outcome.text, "AirGrabber can't find Dune in your folders right now.")

    async def test_a_folder_that_hangs_says_it_didnt_answer(self) -> None:
        def slow(path: str) -> bool:
            time.sleep(0.3)  # a NAS waking up
            return True

        playback = PlaybackService(
            cast(Any, mock.Mock(connected=False)), nothing, folder_library=self.playing,
            exists=slow, timeout_s=0.05,
        )
        video = await playback.film_video(DUNE_FOLDER)
        assert isinstance(video, PlexVideo)
        outcome = await playback.locate(DUNE_FOLDER, video)
        assert isinstance(outcome, PlayOutcome)
        self.assertEqual(outcome.problem, PlayProblem.TIMED_OUT)  # not "can't find it"
        self.assertTrue(outcome.folders)
        self.assertEqual(outcome.text, "The folder didn't answer in time. Try again.")


# -- both sources: a show's and a film's window (two shelves) -------------------------------------


PLEX_SEVERANCE = PlexTitle(kind="show", key="7000", title="Severance", show_id=44933, episodes=2)
PLEX_DUNE = PlexTitle(kind="movie", key="3001", title="Dune", tmdb_id=DUNE_FOLDER.tmdb_id)


class BothPlex:
    """PlexService connected, with Severance S01E01 (watched) and S01E02, and Dune: the same show
    and film the folders hold (FolderTestCase)."""

    connected = True

    def __init__(self) -> None:
        self.listed = [PLEX_SEVERANCE, PLEX_DUNE]
        self.asked: list[str] = []
        self.extra: list[PlexVideo] = []  # Plex-only episodes
        self.away = False

    async def titles(self) -> list[PlexTitle]:
        return list(self.listed)

    async def locations(self) -> list[tuple[str, str]]:
        return []

    async def show_videos(self, show: PlexTitle) -> list[PlexVideo]:
        self.asked.append(show.key)
        if self.away:
            raise PlexUnreachable("away")
        return [
            PlexVideo(key="7011", file="/v/Severance/S01E01.mkv", season=1, episode=1,
                      title="Good News About Hell", watched=True),
            PlexVideo(key="7012", file="/v/Severance/S01E02.mkv", season=1, episode=2,
                      title="Half Loop", view_offset_ms=60_000, duration_ms=3_000_000),
            *self.extra,
        ]

    async def film_video(self, key: str) -> PlexVideo | None:
        self.asked.append(key)
        return PlexVideo(key=key, file="/v/Movies/Dune.mkv")


class TwoShelvesPlayTests(FolderTestCase):
    """Plex connected and the folders on: a show's window lists both, each episode from where it
    is, the folder copy when both have one; a film in both plays from the folder."""

    def both(self, plex: Any | None = None, **kwargs: Any) -> tuple[PlaybackService, BothPlex]:
        fake = plex or BothPlex()
        self.plex_series, self.plex_movies = self.root / "PlexTV", self.root / "PlexMovies"

        async def mediaforge() -> LibraryFolders | None:  # Plex's files from this PC
            return LibraryFolders(series=str(self.plex_series), movies=str(self.plex_movies))

        return (
            PlaybackService(
                cast(Any, fake), mediaforge, folder_library=self.playing,
                person=lambda: self.person, **kwargs,
            ),
            fake,
        )

    async def test_watched_from_the_folders_counts_for_plexs_copy(self) -> None:
        # S01E05 and S01E06 were watched from the folders, then moved into Plex's folder by
        # hand: Plex scanned them in unwatched. The person's own mark still counts.
        service, plex = self.both()
        plex.extra = [
            PlexVideo(key=f"701{n}", file=f"/v/Severance/S01E0{n}.mkv", season=1, episode=n)
            for n in (5, 6, 7)
        ]
        for n in (5, 6):
            moved = PlexVideo(key=f"episode:{SEVERANCE_FOLDER.tmdb_id}:1:{n}", file="")
            await self.playing.mark_watched(moved, profile_id=1)
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        videos = {(v.season, v.episode): v for v in await service.episodes(titles)}
        self.assertEqual(
            [videos[1, n].watched for n in (5, 6, 7)], [True, True, False]
        )  # Plex's own copies, the person's mark
        self.assertEqual(videos[1, 5].key, "7015")
        self.person = 2  # another person's marks are theirs
        videos = {(v.season, v.episode): v for v in await service.episodes(titles)}
        self.assertFalse(videos[1, 5].watched)
        # A Plex-only film watched from the folders: the same.
        self.person = 1
        await self.settings.update(folder_library_on=False)  # Dune is Plex's alone now
        dune = await service.film(DUNE_FOLDER.tmdb_id)
        self.assertEqual(dune, PLEX_DUNE)
        assert dune is not None
        self.assertFalse(await self.film_watched(service, dune))
        await self.playing.mark_watched(
            PlexVideo(key=f"movie:{DUNE_FOLDER.tmdb_id}", file=""), profile_id=1
        )
        self.assertTrue(await self.film_watched(service, dune))

    async def test_folder_copies_get_plexs_name_else_tmdbs(self) -> None:
        # S01E01 and S01E02 are in both: Plex's names. S01E03 and S02E01 only in the folders:
        # TMDB's, one request per season, kept for the run.
        asked: list[tuple[int, int]] = []

        async def season(tv_id: int, number: int) -> object:
            asked.append((tv_id, number))
            names = {1: {3: "In Perpetuity"}, 2: {1: "Hello, Ms. Cobel"}}[number]
            return SimpleNamespace(
                episodes=[SimpleNamespace(episode_number=n, name=name) for n, name in names.items()]
            )

        service, _plex = self.both(episode_names=TMDBEpisodeNames(season))
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        videos = {(v.season, v.episode): v for v in await service.episodes(titles)}
        self.assertEqual(videos[1, 1].title, "Good News About Hell")
        self.assertTrue(videos[1, 1].key.startswith("episode:"))  # still the folder copy
        self.assertEqual(videos[1, 2].title, "Half Loop")
        self.assertEqual(videos[1, 3].title, "In Perpetuity")
        self.assertEqual(videos[2, 1].title, "Hello, Ms. Cobel")
        self.assertEqual(sorted(asked), [(SEVERANCE_FOLDER.tmdb_id, 1), (SEVERANCE_FOLDER.tmdb_id, 2)])
        service._kept.clear()
        await service.episodes(titles)
        self.assertEqual(len(asked), 2)  # kept

    async def test_without_tmdbs_answer_the_episodes_go_unnamed(self) -> None:
        async def season(tv_id: int, number: int) -> object:
            raise OSError("no answer")

        service, _plex = self.both(episode_names=TMDBEpisodeNames(season))
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        with self.assertLogs("app.services.playback", "INFO"):
            videos = {(v.season, v.episode): v for v in await service.episodes(titles)}
        self.assertEqual(videos[1, 3].title, "")
        self.assertEqual(videos[1, 1].title, "Good News About Hell")

    async def test_marks_count_once_the_folder_title_is_gone(self) -> None:
        # Every file of the show moved into Plex's folder by hand: the folders' title is gone and
        # Plex's (an older agent) names no TMDB id. TMDB's id of the TVMaze show joins the marks.
        asked: list[int] = []

        async def tmdb_id_of(show: Show) -> int | None:
            asked.append(show.id)
            return SEVERANCE_FOLDER.tmdb_id

        await self.db.folders.replace(PlexScan(titles=(DUNE_FOLDER,)), NOW)
        service, plex = self.both(show_tmdb_id=tmdb_id_of)
        plex.extra = [PlexVideo(key="7015", file="/v/Severance/S01E05.mkv", season=1, episode=5)]
        moved = PlexVideo(key=f"episode:{SEVERANCE_FOLDER.tmdb_id}:1:5", file="")
        await self.playing.mark_watched(moved, profile_id=1)
        titles = await service.show(Show(id=44933, name="Severance"))
        self.assertEqual(
            titles, ShowTitles(plex=PLEX_SEVERANCE, tmdb_id=SEVERANCE_FOLDER.tmdb_id)
        )
        assert titles is not None
        self.assertEqual(titles.main.tmdb_id, SEVERANCE_FOLDER.tmdb_id)
        videos = {(v.season, v.episode): v for v in await service.episodes(titles)}
        self.assertTrue(videos[1, 5].watched)
        self.assertEqual(asked, [44933])
        # Asked only while neither title has the id.
        await self.db.folders.replace(PlexScan(titles=(DUNE_FOLDER, SEVERANCE_FOLDER)), NOW)
        await service.show(Show(id=44933, name="Severance"))
        self.assertEqual(asked, [44933])

    async def test_a_plex_film_without_a_tmdb_id_is_joined_by_imdb(self) -> None:
        await self.settings.update(folder_library_on=False)
        plex = BothPlex()
        old_agent = PlexTitle(kind="movie", key="3001", title="Dune", imdb_id="tt1160419")
        plex.listed = [PLEX_SEVERANCE, old_agent]
        service, _plex = self.both(plex)
        title = await service.film(DUNE_FOLDER.tmdb_id, "tt1160419")
        self.assertEqual(title, old_agent)
        assert title is not None
        await self.playing.mark_watched(
            PlexVideo(key=f"movie:{DUNE_FOLDER.tmdb_id}", file=""), profile_id=1
        )
        self.assertTrue(await self.film_watched(service, title))

    async def test_the_kept_list_is_the_persons_own(self) -> None:
        service, plex = self.both()
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        e02 = PlexVideo(key=f"episode:{SEVERANCE_FOLDER.tmdb_id}:1:2", file="")
        await self.playing.mark_watched(e02, profile_id=1)
        mine = await service.episodes(titles)
        self.assertTrue(mine[1].watched)
        self.person = 2
        self.assertIsNone(service.kept_episodes(titles))  # not person 1's list
        plex.away = True
        with self.assertLogs("app.services.playback", "INFO"):
            theirs = await service.episodes(titles)
        self.assertFalse(theirs[1].watched)  # person 2's own marks, not person 1's
        self.person = 1
        self.assertEqual(service.kept_episodes(titles), mine)

    @staticmethod
    async def film_watched(service: PlaybackService, title: PlexTitle) -> bool:
        video = await service.film_video(title)
        assert isinstance(video, PlexVideo)
        return video.watched

    async def test_a_folder_copy_thats_gone_plays_plexs(self) -> None:
        # Moved into Plex's folder by hand (or the NAS asleep): Plex's copy plays, as Plex's.
        service, plex = self.both()
        here = self.plex_series / "Severance" / "S01E02.mkv"  # Plex's file from this PC
        here.parent.mkdir(parents=True)
        here.write_bytes(b"video")
        self.episodes[1, 2].unlink()
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        videos = await service.episodes(titles)
        e02 = videos[1]
        self.assertEqual(e02.key, f"episode:{SEVERANCE_FOLDER.tmdb_id}:1:2")
        with self.assertLogs("app.services.playback", "INFO") as logs:
            found = await service.locate(titles.main, e02)
        assert isinstance(found, playback.Located)
        self.assertEqual(found.path, str(here))
        assert found.video is not None
        self.assertEqual(found.video.key, "7012")  # it plays, and is reported, as Plex's
        self.assertFalse([line for line in logs.output if str(SEVERANCE_FOLDER.tmdb_id) in line])
        # Neither copy there: the folders' words.
        here.unlink()
        missing = await service.locate(titles.main, e02)
        assert isinstance(missing, PlayOutcome)
        self.assertTrue(missing.folders)
        # A film the same way: its folder copies gone, Plex's plays.
        dune_here = self.plex_movies / "Movies" / "Dune.mkv"
        dune_here.parent.mkdir(parents=True)
        dune_here.write_bytes(b"video")
        title = await service.film(DUNE_FOLDER.tmdb_id)
        assert title is not None
        video = await service.film_video(title)
        assert isinstance(video, PlexVideo)
        self.dune_4k.unlink()
        self.dune_1080.unlink()
        found = await service.locate(title, video)
        assert isinstance(found, playback.Located)
        self.assertEqual(found.path, str(dune_here))
        assert found.video is not None
        self.assertEqual(found.video.key, "3001")

    async def test_plex_away_keeps_the_list_read_a_moment_ago(self) -> None:
        service, plex = self.both()
        plex.extra = [PlexVideo(key="7015", file="/v/Severance/S01E05.mkv", season=1, episode=5)]
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        first = await service.episodes(titles)
        self.assertEqual(len(first), 5)  # the folders' four and Plex's S01E05
        plex.away = True
        with self.assertLogs("app.services.playback", "INFO"):
            again = await service.episodes(titles)
        self.assertEqual(again, first)

    async def test_an_episode_in_both_plays_from_the_folder(self) -> None:
        service, plex = self.both()
        titles = await service.show(Show(id=44933, name="Severance"))
        self.assertEqual(titles, ShowTitles(plex=PLEX_SEVERANCE, folders=SEVERANCE_FOLDER))
        assert titles is not None
        self.assertEqual(titles.main.key, PLEX_SEVERANCE.key)  # the player's and picker's name
        videos = await service.episodes(titles)
        self.assertEqual(
            [(v.season, v.episode, is_folder_video(v)) for v in videos],
            [(1, 1, True), (1, 2, True), (1, 3, True), (2, 1, True)],  # each once, the folder's
        )
        self.assertEqual(videos[1].file, str(self.episodes[1, 2]))
        self.assertTrue(videos[0].watched)  # Plex says so: it counts
        self.assertEqual(videos[1].view_offset_ms, 0)  # the place is the folder copy's own
        self.assertEqual(next_episode(videos), videos[1])
        found = await service.locate(titles.main, videos[1])
        self.assertEqual(found, playback.Located(
            "Severance S01E02", "show", str(self.episodes[1, 2])
        ))
        self.assertEqual(service.kept_episodes(titles), videos)

    async def test_a_film_in_both_plays_from_the_folder(self) -> None:
        service, plex = self.both()
        title = await service.film(DUNE_FOLDER.tmdb_id)
        self.assertEqual(title, DUNE_FOLDER)
        assert title is not None
        video = await service.film_video(title)
        assert isinstance(video, PlexVideo)
        self.assertTrue(is_folder_video(video))
        self.assertEqual(video.file, str(self.dune_4k))  # its best copy
        self.assertEqual(plex.asked, [])  # Plex isn't asked for it

    async def test_plex_only_and_folders_only_are_as_before(self) -> None:
        # Plex alone (the folders off): Plex's show, Plex's episodes, Plex's film.
        await self.settings.update(folder_library_on=False)
        service, plex = self.both()
        titles = await service.show(Show(id=44933, name="Severance"))
        self.assertEqual(titles, ShowTitles(plex=PLEX_SEVERANCE))
        assert titles is not None
        self.assertEqual([v.key for v in await service.episodes(titles)], ["7011", "7012"])
        self.assertEqual(await service.film(DUNE_FOLDER.tmdb_id), PLEX_DUNE)
        self.assertEqual(
            [v.key for v in await service.episodes(PLEX_SEVERANCE)], ["7011", "7012"]
        )  # a lone title (Continue watching) is its own source's
        # The folders alone (no Plex): theirs, as part 1.
        await self.settings.update(folder_library_on=True)
        alone = self.playback()
        titles = await alone.show(Show(id=44933, name="Severance"))
        self.assertEqual(titles, ShowTitles(folders=SEVERANCE_FOLDER))
        assert titles is not None
        self.assertEqual(titles.main, SEVERANCE_FOLDER)
        self.assertEqual(len(await alone.episodes(titles)), 4)
        self.assertEqual(await alone.film(DUNE_FOLDER.tmdb_id), DUNE_FOLDER)
        self.assertIsNone(await alone.show(Show(id=1, name="Other")))
        with self.assertRaises(ValueError):
            ShowTitles()

    async def test_plex_away_still_lists_the_folders(self) -> None:
        class AwayPlex(BothPlex):
            async def show_videos(self, show: PlexTitle) -> list[PlexVideo]:
                raise PlexUnreachable("away")

        service, _plex = self.both(AwayPlex())
        titles = await service.show(Show(id=44933, name="Severance"))
        assert titles is not None
        with self.assertLogs("app.services.playback", "INFO"):
            videos = await service.episodes(titles)
        self.assertEqual(len(videos), 4)  # the folder's, Plex's later
        self.assertIsNone(service.kept_episodes(titles))  # not kept as the whole list
        only_plex = ShowTitles(plex=PLEX_SEVERANCE)
        with self.assertRaises(PlexError):
            await service.episodes(only_plex)


class DownloadedCueTests(DialogTestCase):
    async def test_folder_episodes_have_the_downloaded_cue(self) -> None:
        videos = [
            PlexVideo(key="41", file="/v/a.mkv", season=1, episode=1, watched=True),
            PlexVideo(key="episode:5:1:2", file=r"D:\Series\b.mkv", season=1, episode=2),
        ]

        async def episodes(title: PlexTitle) -> list[PlexVideo]:
            return videos

        picker = EpisodePicker(self.root, ARK, episodes=episodes, on_play=lambda v, e: None)
        self.keep(picker)
        await picker.loaded()
        await self.settle()
        self.assertEqual(list(picker.cues), ["episode:5:1:2"])
        self.assertEqual(picker.cues["episode:5:1:2"].cget("text"), "Downloaded")
        self.assertEqual(NL[play_control.DOWNLOADED_CUE], "Gedownload")
        assert picker.selected is not None
        self.assertEqual(picker.selected.key, "episode:5:1:2")  # Plex's watched: on into it

    async def picker_cues(self, settings: Settings) -> list[str]:
        """The keys of the rows with the "Downloaded" cue, the words following `settings`."""
        library_words.follow(cast(SettingsService, _CurrentSettings(settings)))
        self.addCleanup(library_words.follow, None)
        videos = [
            PlexVideo(key="41", file="/v/a.mkv", season=1, episode=1),
            PlexVideo(key="episode:5:1:2", file=r"D:\Series\b.mkv", season=1, episode=2),
        ]

        async def episodes(title: PlexTitle) -> list[PlexVideo]:
            return videos

        picker = EpisodePicker(self.root, ARK, episodes=episodes, on_play=lambda v, e: None)
        self.keep(picker)
        await picker.loaded()
        await self.settle()
        return list(picker.cues)

    async def test_folders_only_has_no_downloaded_cue(self) -> None:
        # Without Plex every video is the folders': "Downloaded" (on the PC, not in Plex yet)
        # would sit on every row.
        folders = Settings(folder_library_on=True, series_folders=("D:\\Series",))
        self.assertEqual(await self.picker_cues(folders), [])

    async def test_beside_plex_folder_episodes_have_the_cue(self) -> None:
        both = Settings(
            plex_token="made-up-token", folder_library_on=True, series_folders=("D:\\Series",)
        )
        self.assertEqual(await self.picker_cues(both), ["episode:5:1:2"])


class _CurrentSettings:
    def __init__(self, settings: Settings) -> None:
        self.current = settings


if __name__ == "__main__":
    unittest.main()
