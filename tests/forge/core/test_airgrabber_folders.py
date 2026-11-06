"""MediaForge watches AirGrabber's downloads (app/forge/core/airgrabber_folders.py, B-01): the
folders derived from AirGrabber's settings in every download mode, what is watched next to the
owner's own folders and why not, and the sync that keeps them up to date without ever moving
files from a newly watched folder behind the owner's back."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from pydantic import SecretStr

from app import i18n
from app.forge.core import airgrabber_folders as module
from app.forge.core.activity import ActivityFeed
from app.forge.core.airgrabber_folders import (
    AirGrabberFolderSync,
    airgrabber_in_use,
    ask_text,
    derived_folders,
    nas_path,
    newly_watched,
    pc_path,
    reason_text,
    use_text,
    watched_folders,
)
from app.forge.core.library import library_problems
from app.forge.models.settings import AirGrabberDownloads, AirGrabberFolder, ForgeSettings
from app.i18n import Language
from app.models import DownloadMode, QBittorrentState, RemoteKind, Settings
from app.services.settings import SettingsService
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase

NAS_URL = "http://192.168.1.20:5000"
LOCAL_QB = "http://127.0.0.1:8123"
SAVE_PATH = r"D:\Torrents"
QB_STATE = QBittorrentState(exists=True, quiet=True, save_path=SAVE_PATH + "\\")
SERIES = r"\\192.168.1.20\video\Series"
MOVIES = r"\\192.168.1.20\video\Movies"


def airgrabber(**changes: Any) -> Settings:
    values: dict[str, Any] = {"download_dir": r"C:\Users\Alex\Downloads"}
    values.update(changes)
    return Settings.model_validate(values)


def synology(**changes: Any) -> Settings:
    values: dict[str, Any] = {
        "download_mode": DownloadMode.REMOTE,
        "remote_kind": RemoteKind.SYNOLOGY,
        "remote_url": NAS_URL,
        "remote_username": "alex",
        "remote_password": SecretStr("pw"),
    }
    values.update(changes)
    return airgrabber(**values)


def local_qbittorrent(**changes: Any) -> Settings:
    values: dict[str, Any] = {
        "download_mode": DownloadMode.REMOTE,
        "remote_kind": RemoteKind.QBITTORRENT,
        "remote_url": LOCAL_QB,
        "remote_username": "airgrabber",
        "remote_password": SecretStr("generated"),
    }
    values.update(changes)
    return airgrabber(**values)


def paths(downloads: AirGrabberDownloads) -> list[str]:
    return [f.path for f in downloads.folders]


def folder(path: str, why: str = "finished", **changes: Any) -> AirGrabberFolder:
    return AirGrabberFolder.model_validate({"path": path, "why": why, **changes})


def forge(*folders: AirGrabberFolder, **changes: Any) -> ForgeSettings:
    values: dict[str, Any] = {
        "series_folder": SERIES,
        "movies_folder": MOVIES,
        "airgrabber": AirGrabberDownloads(folders=list(folders)),
    }
    values.update(changes)
    return ForgeSettings.model_validate(values)


class PathTests(ForgeTestCase):
    def test_pc_paths(self) -> None:
        self.assertEqual(pc_path(r"  D:\Films\ "), r"D:\Films")
        self.assertEqual(pc_path("D:/Films/New"), r"D:\Films\New")
        self.assertEqual(pc_path("D:\\"), "D:\\")
        self.assertEqual(pc_path(r"\\NAS\video\ "), r"\\NAS\video")
        self.assertEqual(pc_path("//NAS/video/tv"), r"\\NAS\video\tv")
        for not_one in ("", "   ", "D:", "Films", "video/tv", "/downloads/tv", r"\\NAS"):
            with self.subTest(value=not_one):
                self.assertEqual(pc_path(not_one), "")

    def test_nas_paths(self) -> None:
        self.assertEqual(nas_path(NAS_URL, "video/tv"), r"\\192.168.1.20\video\tv")
        self.assertEqual(nas_path("https://nas.local:5001/", "/video/movies/"),
                         r"\\nas.local\video\movies")
        self.assertEqual(nas_path(NAS_URL, ""), "")
        self.assertEqual(nas_path("", "video"), "")
        self.assertEqual(nas_path("http://[fe80::1]:5000", "video"), "")  # no UNC form here
        self.assertEqual(nas_path("http://[bad", "video"), "")


class DerivedFoldersTests(ForgeTestCase):
    """`derived_folders` for every download mode."""

    def test_folder_mode_watches_where_finished_downloads_end_up(self) -> None:
        settings = airgrabber(
            download_mode=DownloadMode.FOLDER,
            movie_download_dir=r"C:\Users\Alex\Films",
            subtitles_folder=r"\\NAS\video\done",
        )
        derived = derived_folders(settings, None)
        self.assertEqual(paths(derived), [r"\\NAS\video\done"])
        self.assertEqual(derived.folders[0].why, "finished")
        self.assertFalse(derived.folders[0].suggested)
        self.assertEqual(derived.ask, "")

    def test_torrent_file_folders_are_never_watched(self) -> None:
        for mode in DownloadMode:
            settings = airgrabber(download_mode=mode, movie_download_dir=r"C:\Users\Alex\Films")
            with self.subTest(mode=mode):
                derived = derived_folders(settings, None)
                self.assertEqual(derived.folders, [])
                self.assertEqual(derived.ask, "finished")

    def test_app_mode(self) -> None:
        derived = derived_folders(
            airgrabber(download_mode=DownloadMode.APP, subtitles_folder=SAVE_PATH), QB_STATE
        )
        self.assertEqual(paths(derived), [SAVE_PATH])
        self.assertEqual(derived.ask, "")

    def test_the_qbittorrent_airgrabber_set_up(self) -> None:
        settings = local_qbittorrent(movie_remote_destination=r"E:\Films")
        derived = derived_folders(settings, QB_STATE)
        self.assertEqual(paths(derived), [SAVE_PATH, r"E:\Films"])
        self.assertEqual([f.why for f in derived.folders], ["qbittorrent", "films"])
        self.assertEqual(derived.folders[1].program, "qBittorrent")
        self.assertFalse(any(f.suggested for f in derived.folders))
        # Its save folder is also "Where finished downloads end up" (first run): once.
        derived = derived_folders(local_qbittorrent(subtitles_folder=SAVE_PATH), QB_STATE)
        self.assertEqual([f.why for f in derived.folders], ["finished"])
        # Its settings file unknown: only what AirGrabber's settings say.
        self.assertEqual(derived_folders(local_qbittorrent(), None).ask, "finished")

    def test_a_program_on_this_pc_that_airgrabber_did_not_set_up(self) -> None:
        settings = local_qbittorrent(
            remote_username="alex",
            remote_destination=r"D:\Series in",
            movie_remote_destination="films",  # not a full path: can't tell where it is
        )
        derived = derived_folders(settings, QB_STATE)  # not AirGrabber's: its save folder isn't
        self.assertEqual(paths(derived), [r"D:\Series in"])
        self.assertEqual(derived.folders[0].why, "shows")
        transmission = local_qbittorrent(
            remote_kind=RemoteKind.TRANSMISSION,
            remote_url="http://localhost:9091/transmission/rpc",
            remote_destination="/downloads/tv",
        )
        self.assertEqual(derived_folders(transmission, None).ask, "finished")

    def test_download_station_is_only_suggested(self) -> None:
        settings = synology(remote_destination="video/tv", movie_remote_destination="video/movies")
        derived = derived_folders(settings, QB_STATE)
        self.assertEqual(paths(derived), [r"\\192.168.1.20\video\tv", r"\\192.168.1.20\video\movies"])
        self.assertTrue(all(f.suggested for f in derived.folders))
        self.assertEqual([f.why for f in derived.folders], ["shows", "films"])
        self.assertEqual(derived.folders[0].program, "Download Station")
        self.assertIsNone(derived.folders[0].reachable)  # never assumed
        self.assertEqual(derived.ask, "")

    def test_download_station_without_its_folder_asks(self) -> None:
        derived = derived_folders(synology(movie_remote_destination="video/movies"), None)
        self.assertEqual(paths(derived), [r"\\192.168.1.20\video\movies"])
        self.assertEqual(derived.ask, "nas_folder")
        self.assertEqual(derived_folders(synology(), None).ask, "nas_folder")
        # "Where finished downloads end up" answers it; the films suggestion stays.
        derived = derived_folders(
            synology(subtitles_folder=r"\\NAS\video\tv", movie_remote_destination="video/movies"),
            None,
        )
        self.assertEqual(derived.ask, "")
        self.assertEqual([f.suggested for f in derived.folders], [False, True])

    def test_another_computer_asks(self) -> None:
        for kind, program in ((RemoteKind.QBITTORRENT, "qBittorrent"),
                              (RemoteKind.TRANSMISSION, "Transmission")):
            settings = airgrabber(
                download_mode=DownloadMode.REMOTE,
                remote_kind=kind,
                remote_url="http://192.168.1.30:8080",
                remote_username="alex",
                remote_destination="/downloads/tv",
            )
            with self.subTest(kind=kind):
                derived = derived_folders(settings, QB_STATE)
                self.assertEqual(derived.folders, [])
                self.assertEqual((derived.ask, derived.program), ("other_computer", program))
                answered = derived_folders(
                    settings.model_copy(update={"subtitles_folder": r"\\PC2\done"}), None
                )
                self.assertEqual((paths(answered), answered.ask), ([r"\\PC2\done"], ""))

    def test_folder_mode_counts_a_remote_program_only_when_set_up(self) -> None:
        settings = synology(download_mode=DownloadMode.FOLDER, remote_destination="video/tv")
        self.assertEqual(paths(derived_folders(settings, None)), [r"\\192.168.1.20\video\tv"])
        unset = settings.model_copy(update={"remote_password": SecretStr("")})
        self.assertEqual(derived_folders(unset, None).folders, [])
        app = settings.model_copy(update={"download_mode": DownloadMode.APP})
        self.assertEqual(derived_folders(app, None).folders, [])


class InUseTests(ForgeTestCase):
    """What is watched of AirGrabber's folders next to the owner's own."""

    def test_on_for_a_new_setup_off_for_an_imported_one(self) -> None:
        done = folder(r"D:\Done")
        self.assertTrue(forge(done).airgrabber_on)
        self.assertEqual(watched_folders(forge(done, watch_folders=[r"E:\Mine"])),
                         [r"E:\Mine", r"D:\Done"])
        imported = forge(done, imported_from="MediaForge 1.3.0, 2026-11-02")
        self.assertFalse(imported.airgrabber_on)
        self.assertEqual(watched_folders(imported), [])
        chosen = imported.model_copy(update={"use_airgrabber_downloads": True})
        self.assertEqual(watched_folders(chosen), [r"D:\Done"])
        off = forge(done, use_airgrabber_downloads=False)
        self.assertEqual(watched_folders(off), [])

    def test_suggestions_are_never_watched(self) -> None:
        suggestion = folder(r"\\192.168.1.20\video\tv", "shows", suggested=True, reachable=True)
        settings = forge(suggestion)
        self.assertEqual(watched_folders(settings), [])
        self.assertEqual([u.use for u in airgrabber_in_use(settings)], ["suggested"])
        # Added by the owner: their own folder; the suggestion reads as covered.
        added = settings.model_copy(update={"watch_folders": [suggestion.path]})
        self.assertEqual(watched_folders(added), [suggestion.path])
        self.assertEqual([u.use for u in airgrabber_in_use(added)], ["covered"])

    def test_clashes_are_left_out_and_say_why(self) -> None:
        settings = forge(
            folder(r"D:\Downloads\tv"),  # inside the owner's D:\Downloads
            folder(r"E:\All", "qbittorrent"),  # holds the owner's E:\All\Mine
            folder(r"\\192.168.1.20\video", "shows"),  # holds the library folders
            folder(r"F:\Films", "films"),
            watch_folders=[r"D:\Downloads", r"E:\All\Mine"],
        )
        uses = [u.use for u in airgrabber_in_use(settings)]
        self.assertEqual(uses, ["covered", "holds_own", "overlap", "watched"])
        self.assertEqual(watched_folders(settings), [r"D:\Downloads", r"E:\All\Mine", r"F:\Films"])
        self.assertEqual(library_problems(settings), [])  # nothing of AirGrabber's stops watching

    def test_nested_folders_of_airgrabber_watch_the_outer_one(self) -> None:
        settings = forge(folder(r"D:\Torrents\Films", "films"), folder(r"D:\Torrents", "qbittorrent"))
        self.assertEqual([u.use for u in airgrabber_in_use(settings)], ["covered", "watched"])
        self.assertEqual(watched_folders(settings), [r"D:\Torrents"])
        self.assertEqual(library_problems(settings), [])

    def test_airgrabbers_folders_count_for_the_library_rules(self) -> None:
        settings = forge(folder(r"D:\Done"))
        self.assertEqual(library_problems(settings), [])
        self.assertIn("Choose a download folder to watch.",
                      library_problems(settings.model_copy(update={"use_airgrabber_downloads": False})))

    def test_newly_watched(self) -> None:
        before = forge(folder(r"D:\Done"))
        after = forge(folder(r"d:\done\\"), folder(r"E:\Films", "films"))
        self.assertEqual(newly_watched(before, after), [r"E:\Films"])
        self.assertEqual(newly_watched(after, before), [])


class TextTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(i18n.set_language, i18n.current_language())

    def test_reasons_and_asks(self) -> None:
        i18n.set_language(Language.EN)
        shows = folder(r"\\192.168.1.20\video\tv", "shows", program="Download Station",
                       suggested=True, reachable=False)
        self.assertEqual(reason_text(shows), "Download Station saves AirGrabber's downloads here")
        settings = forge(shows)
        self.assertEqual(
            use_text(airgrabber_in_use(settings)[0]),
            "Suggested: Download Station saves AirGrabber's downloads here. This PC can't reach "
            "it right now.",
        )
        reached = forge(shows.model_copy(update={"reachable": True}))
        self.assertTrue(use_text(airgrabber_in_use(reached)[0]).endswith("This PC can reach it."))
        overlap = forge(folder(SERIES + r"\in"))
        self.assertEqual(
            use_text(airgrabber_in_use(overlap)[0]),
            "“Where finished downloads end up” in Settings. Not watched: it "
            "overlaps a library folder.",
        )
        self.assertEqual(ask_text(AirGrabberDownloads()), "")
        other = AirGrabberDownloads(ask="other_computer", program="Transmission")
        self.assertIn("Transmission on another computer", ask_text(other))
        for ask in ("finished", "nas_folder"):
            self.assertIn("Where finished downloads end up",
                          ask_text(AirGrabberDownloads(ask=ask)))

    def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        self.assertEqual(
            reason_text(folder(SAVE_PATH, "qbittorrent")),
            "qBittorrent op deze pc bewaart de downloads van AirGrabber hier",
        )
        self.assertIn("Transmission op een andere computer",
                      ask_text(AirGrabberDownloads(ask="other_computer", program="Transmission")))


# -- the sync ---------------------------------------------------------------------------------------


class FakeForgeSettings:
    def __init__(self, current: ForgeSettings) -> None:
        self.current = current
        self.updates: list[dict[str, Any]] = []
        self.listeners: list[Callable[[ForgeSettings], None]] = []

    async def update(self, **changes: Any) -> ForgeSettings:
        await asyncio.sleep(0)
        self.updates.append(changes)
        self.current = ForgeSettings.model_validate({**self.current.model_dump(), **changes})
        for listener in list(self.listeners):
            listener(self.current)
        return self.current

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class FakeAirGrabberSettings:
    def __init__(self, current: Settings) -> None:
        self.current = current
        self.listeners: list[Callable[[Settings], None]] = []

    def change(self, **changes: Any) -> None:
        self.current = Settings.model_validate({**self.current.model_dump(), **changes})
        for listener in list(self.listeners):
            listener(self.current)

    def subscribe(self, listener: Callable[[Settings], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class SyncTests(ForgeAsyncTestCase):
    def make(
        self,
        settings: Settings,
        current: ForgeSettings | None = None,
        *,
        reachable: Callable[[str], bool] = lambda _p: True,
        timeout_s: float = 2.0,
    ) -> tuple[AirGrabberFolderSync, FakeForgeSettings, FakeAirGrabberSettings]:
        self.forge = FakeForgeSettings(current or forge())
        self.airgrabber = FakeAirGrabberSettings(settings)
        self.read_states = 0
        self.activity = ActivityFeed()

        def read_state() -> QBittorrentState:
            self.read_states += 1
            return QB_STATE

        sync = AirGrabberFolderSync(
            self.forge,
            self.airgrabber,
            qbittorrent_state=read_state,
            is_dir=reachable,
            timeout_s=timeout_s,
            activity=self.activity,
        )
        self.addAsyncCleanup(sync.close)
        return sync, self.forge, self.airgrabber

    async def test_first_look_stores_once(self) -> None:
        sync, fake, _ = self.make(airgrabber(subtitles_folder=r"D:\Done"))
        sync.start()
        await sync.settled()
        self.assertEqual(paths(fake.current.airgrabber), [r"D:\Done"])
        self.assertEqual(len(fake.updates), 1)
        await sync.refresh()  # nothing changed: nothing stored
        self.assertEqual(len(fake.updates), 1)
        self.assertEqual(self.read_states, 0)  # not AirGrabber's qBittorrent: its file unread

    async def test_follows_airgrabbers_changes_that_matter(self) -> None:
        sync, fake, ag = self.make(airgrabber())
        sync.start()
        await sync.settled()
        self.assertEqual(fake.current.airgrabber.ask, "finished")
        ag.change(subtitles_folder=r"\\NAS\video\done")
        await sync.settled()
        self.assertEqual(paths(fake.current.airgrabber), [r"\\NAS\video\done"])
        stored = len(fake.updates)
        ag.change(weeks_to_show=4, download_dir=r"E:\Torrents")  # not where videos land
        await sync.settled()
        self.assertEqual(len(fake.updates), stored)

    async def test_reads_the_qbittorrent_it_set_up_in_a_thread(self) -> None:
        threads: list[str] = []
        sync, fake, _ = self.make(local_qbittorrent())

        def read_state() -> QBittorrentState:
            threads.append(threading.current_thread().name)
            return QB_STATE

        sync._qbittorrent_state = read_state  # pyright: ignore[reportPrivateUsage]
        await sync.refresh()
        self.assertEqual(paths(fake.current.airgrabber), [SAVE_PATH])
        self.assertNotEqual(threads, [threading.main_thread().name])

    async def test_an_unreadable_qbittorrent_file(self) -> None:
        sync, fake, _ = self.make(local_qbittorrent())

        def broken() -> QBittorrentState:
            raise PermissionError("locked")

        sync._qbittorrent_state = broken  # pyright: ignore[reportPrivateUsage]
        with self.assertLogs(module.log, logging.WARNING):
            await sync.refresh()
        self.assertEqual(fake.current.airgrabber.ask, "finished")

    async def test_suggestions_are_checked_with_a_time_limit(self) -> None:
        settings = synology(remote_destination="video/tv", movie_remote_destination="video/movies")
        asked: list[str] = []

        def is_dir(path: str) -> bool:
            asked.append(path)
            if path.endswith("movies"):
                time.sleep(0.5)  # the NAS doesn't answer in time
            return True

        sync, fake, _ = self.make(settings, reachable=is_dir, timeout_s=0.1)
        await sync.refresh()
        self.assertEqual(asked, [r"\\192.168.1.20\video\tv", r"\\192.168.1.20\video\movies"])
        self.assertEqual([f.reachable for f in fake.current.airgrabber.folders], [True, False])
        self.assertEqual(watched_folders(fake.current), [])  # suggestions: never watched

    async def test_an_unreachable_suggestion(self) -> None:
        def gone(_path: str) -> bool:
            raise OSError("network name not found")

        sync, fake, _ = self.make(synology(remote_destination="video/tv"), reachable=gone)
        await sync.refresh()
        self.assertEqual([f.reachable for f in fake.current.airgrabber.folders], [False])

    async def test_on_goes_back_to_trial_for_a_new_folder(self) -> None:
        current = forge(folder(r"D:\Done"), automatic="on")
        sync, fake, ag = self.make(airgrabber(subtitles_folder=r"D:\Done"), current)
        await sync.refresh()
        self.assertEqual(fake.updates, [])  # the same folder: nothing new, stays On
        ag.change(
            download_mode=DownloadMode.REMOTE,
            remote_kind=RemoteKind.QBITTORRENT,
            remote_url=LOCAL_QB,
            remote_username="airgrabber",
            remote_password=SecretStr("generated"),
            movie_remote_destination=r"E:\Films",
        )
        await sync.refresh()
        self.assertEqual(fake.current.automatic, "trial")
        self.assertEqual(fake.updates[-1]["automatic"], "trial")  # in the same update
        line = self.activity.lines()[-1]
        self.assertIn(SAVE_PATH, line.text)  # the first new one
        self.assertEqual(line.level, logging.WARNING)

    async def test_no_trial_when_nothing_new_is_watched(self) -> None:
        for current in (
            forge(automatic="trial"),
            forge(automatic="off"),
            forge(automatic="on", use_airgrabber_downloads=False),
            forge(automatic="on", imported_from="MediaForge 1.3.0, 2026-11-02"),
        ):
            with self.subTest(current=current):
                sync, fake, _ = self.make(airgrabber(subtitles_folder=r"D:\Done"), current)
                await sync.refresh()
                self.assertEqual(fake.current.automatic, current.automatic)
                self.assertEqual(paths(fake.current.airgrabber), [r"D:\Done"])
        sync, fake, _ = self.make(synology(remote_destination="video/tv"), forge(automatic="on"))
        await sync.refresh()  # a suggestion isn't watched
        self.assertEqual(fake.current.automatic, "on")

    async def test_derives_again_after_an_import(self) -> None:
        sync, fake, _ = self.make(airgrabber(subtitles_folder=r"D:\Done"))
        sync.start()
        await sync.settled()
        # The import stores the old MediaForge's whole document (no AirGrabber folders in it).
        await fake.update(**ForgeSettings(imported_from="MediaForge 1.3.0").model_dump())
        await sync.settled()
        self.assertEqual(paths(fake.current.airgrabber), [r"D:\Done"])
        self.assertFalse(fake.current.airgrabber_on)  # imported: off until chosen

    async def test_close_stops_listening(self) -> None:
        sync, fake, ag = self.make(airgrabber())
        sync.start()
        await sync.close()
        self.assertEqual((ag.listeners, fake.listeners), ([], []))
        ag.change(subtitles_folder=r"D:\Done")
        await asyncio.sleep(0.01)
        self.assertEqual(paths(fake.current.airgrabber), [])

    async def test_a_source_that_cannot_be_followed_still_gets_one_look(self) -> None:
        class NoSubscribe:
            current = airgrabber(subtitles_folder=r"D:\Done")

        fake = FakeForgeSettings(forge())
        sync = AirGrabberFolderSync(fake, NoSubscribe(), is_dir=lambda _p: True)  # type: ignore[arg-type]
        self.addAsyncCleanup(sync.close)
        with self.assertNoLogs(module.log, logging.WARNING):
            sync.start()
            await sync.settled()
        self.assertEqual(paths(fake.current.airgrabber), [r"D:\Done"])

    async def test_a_subscribe_that_fails_is_logged_and_forge_goes_on(self) -> None:
        class Broken(FakeAirGrabberSettings):
            def subscribe(self, listener: Callable[[Settings], None]) -> Callable[[], None]:
                raise RuntimeError("closed")

        fake = FakeForgeSettings(forge())
        sync = AirGrabberFolderSync(fake, Broken(airgrabber(subtitles_folder=r"D:\Done")))
        self.addAsyncCleanup(sync.close)
        with self.assertLogs(module.log, logging.ERROR) as logged:
            sync.start()
        self.assertIn("RuntimeError", logged.output[0])
        await sync.settled()
        self.assertEqual(paths(fake.current.airgrabber), [r"D:\Done"])

    async def test_a_stand_in_without_settings_changes_nothing_quietly(self) -> None:
        """What Forge's other tests pass as AirGrabber's settings (Mock(spec=SettingsService)):
        its `current` is no Settings. Nothing is derived, stored or logged above DEBUG."""
        fake = FakeForgeSettings(forge(folder(r"D:\Done")))
        stand_in = mock.Mock(spec=SettingsService)
        sync = AirGrabberFolderSync(fake, stand_in)
        self.addAsyncCleanup(sync.close)
        with self.assertNoLogs(module.log, logging.INFO):
            sync.start()
            await sync.settled()
        self.assertEqual(fake.updates, [])
        self.assertEqual(paths(fake.current.airgrabber), [r"D:\Done"])

    async def test_every_real_mode_derives_without_an_error(self) -> None:
        """Real AirGrabber settings in every mode and program, every folder field filled in or
        left empty: a look never fails (the only failures come from stand-ins)."""
        for mode in DownloadMode:
            for kind in RemoteKind:
                for filled in (False, True):
                    text = r"D:\Done" if filled else ""
                    settings = airgrabber(
                        download_mode=mode,
                        remote_kind=kind,
                        remote_url=NAS_URL if filled else "",
                        remote_username="alex",
                        remote_password=SecretStr("pw" if filled else ""),
                        remote_destination="video/tv" if filled else "",
                        movie_remote_destination="video/movies" if filled else "",
                        subtitles_folder=text,
                    )
                    with self.subTest(mode=mode, kind=kind, filled=filled):
                        sync, fake, _ = self.make(settings)
                        with self.assertNoLogs(module.log, logging.ERROR):
                            await sync.refresh()
