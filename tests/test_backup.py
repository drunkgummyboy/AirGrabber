"""Backup & move (app/services/backup.py, app/models/backup.py, BackupRepository): the secret
settings' one list, the file format, a round trip from one temporary database into a fresh
one, keys and passwords left out or included (and never wiped by a load), Replace against
Add to what I have, the history added once, and files that aren't (or no longer) loadable.
Every database is a temporary file; nothing touches the real data folder."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from pydantic import SecretStr

from app.database import BackupRepository, Database
from app.i18n import Language
from app.models import Accent, DownloadRecord, Episode, MediaType, Movie, Settings, Show
from app.models.backup import FORMAT_NAME, FORMAT_VERSION, BackupFile, KeptDownloads, LoadMode
from app.services import SettingsService
from app.services.backup import (
    LOCAL_DOWNLOAD_FIELDS,
    MACHINE_FIELDS,
    MACHINE_SECRETS,
    NOT_SECRET_FIELDS,
    SECRET_FIELDS,
    SECRET_NAME,
    BackupError,
    BackupProblem,
    BackupService,
    decode,
    default_file_name,
    encode,
    kept_downloads,
    replace_changes,
    settings_for_backup,
    summarize,
    valid_settings,
)
from app.services.profiles import ProfileService
from app.services.watchlist import WatchlistService
from tests.plex_link import have_marks, mark_have

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
AUTO_SINCE = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
LOGGER = "app.services.backup"

SECRETS = {
    "tmdb_api_key": "tmdb-key-of-alex",
    "prowlarr_api_key": "prowlarr-key-of-alex",
    "remote_password": "nas-password-of-alex",
    "opensubtitles_password": "os-password-of-alex",
    "opensubtitles_own_key": "os-key-of-alex",
}
# PC A's settings: its own choices, its markers and every secret.
SETTINGS_A: dict[str, Any] = {
    **SECRETS,
    "language": "nl",
    "accent": "ocean",
    "quality_tv": "720p",
    "download_mode": "remote",
    "download_dir": r"D:\Downloads\Alex",
    "remote_url": "http://192.168.1.20:5000",
    "remote_username": "alex",
    "opensubtitles_username": "alex",
    "subtitles_folder": r"\\NAS\video",
    "movie_download_dir": r"D:\Downloads\Alex\Films",
    "remote_destination": "video/tv",
    "movie_remote_destination": "video/movies",
    "excluded_keywords": "cam, ts",
    "setup_done": True,
    "last_run_version": "2.0.9",
    "tray_notice_shown": True,
    "start_with_windows": True,
    "prowlarr_seeded_url": "http://localhost:9696",
}


# A PC whose downloads go to its own qBittorrent, the one AirGrabber set up (Web UI on 127.0.0.1).
LOCAL_QBITTORRENT: dict[str, Any] = {
    "download_mode": "remote",
    "remote_kind": "qbittorrent",
    "remote_url": "http://127.0.0.1:8081",
    "remote_username": "airgrabber",
    "remote_password": "web-ui-password-here",
    "remote_destination": "",
}


def show(show_id: int, name: str) -> Show:
    return Show(id=show_id, name=name, status="Running", premiered=date(2020, 1, 1))


def episodes(show_id: int, count: int) -> list[Episode]:
    return [
        Episode(
            id=show_id * 100 + number,
            show_id=show_id,
            season=1,
            number=number,
            name=f"Episode {number}",
            airstamp=NOW - timedelta(days=7 * (count - number + 1)),
        )
        for number in range(1, count + 1)
    ]


def movie(movie_id: int, title: str) -> Movie:
    return Movie(id=movie_id, title=title, release_date=date(2026, 1, 1), poster_path="/p.jpg")


@dataclass
class Place:
    """One "PC": a temporary database with the services a backup needs."""

    db: Database
    settings: SettingsService
    watchlist: WatchlistService
    backup: BackupService
    loads: list[int]


class BackupTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.file = self.folder / "AirGrabber backup 2026-10-31.airgrabber"

    async def place(self, name: str, **settings: Any) -> Place:
        db = await Database.open(self.folder / f"{name}.db")
        self.addAsyncCleanup(db.close)
        service = await SettingsService.load(db.settings)
        if settings:
            await service.update(**settings)
        watchlist = WatchlistService(db.watchlist, mock.MagicMock(), mock.MagicMock())
        profiles = ProfileService(db.profiles, service, watchlist)
        await profiles.load()
        backup = BackupService(
            BackupRepository(db), service, profiles, app_version="2.0.9", clock=lambda: NOW
        )
        loads: list[int] = []
        backup.subscribe(lambda: loads.append(1))
        return Place(db, service, watchlist, backup, loads)

    async def pc_a(self) -> Place:
        """Two followed shows (Fargo with automatic downloads and two episodes marked "I
        already have this"), two films on the watchlist, three grabs (one without an info hash,
        one cleared off the Downloads page, one a film) and every kind of setting."""
        place = await self.place("a", **SETTINGS_A)
        db = place.db
        await db.library.save_show(show(1, "Fargo"), episodes(1, 6))
        await db.library.track(1, profile_id=1)
        await db.library.set_auto_download(1, True, since=AUTO_SINCE, profile_id=1)
        await mark_have(db, [101, 102], NOW.isoformat())  # old marks: kept, unread since 3.1.0
        await db.library.save_show(show(2, "Severance"), episodes(2, 3))
        await db.library.track(2, profile_id=1)
        await db.library.save_show(show(9, "Not followed"), episodes(9, 2))  # schedule cache
        await db.watchlist.add(movie(10, "Dune"), auto_download=True, when=NOW, profile_id=1)
        await db.watchlist.add(movie(11, "Arrival"), auto_download=False, when=NOW, profile_id=1)
        await db.history.add(
            DownloadRecord(
                media_type=MediaType.TV, media_id=1, season=1, episode=3,
                release_title="Fargo.S01E03.1080p.WEB", info_hash="a" * 40,
                saved_path=r"D:\Downloads\Alex\x.torrent", downloaded_at=NOW - timedelta(days=2),
            )
        )
        await db.history.add(
            DownloadRecord(
                media_type=MediaType.TV, media_id=1, season=1, episode=4,
                release_title="Fargo.S01E04.1080p.WEB", downloaded_at=NOW - timedelta(days=1),
            )
        )
        await db.history.add(
            DownloadRecord(
                media_type=MediaType.MOVIE, media_id=12, release_title="Heat.1995.1080p",
                info_hash="b" * 40, downloaded_at=NOW,
            )
        )
        first = (await db.history.recent())[-1]
        assert first.id is not None
        await db.history.clear([first.id], NOW)
        await place.watchlist.load()
        return place

    async def saved_backup(self, *, include_secrets: bool = False) -> Place:
        place = await self.pc_a()
        await place.backup.save(self.file, include_secrets=include_secrets)
        return place

    async def tracked(self, place: Place) -> dict[int, bool]:
        return {entry.show.id: entry.auto_download for entry in await place.db.library.tracked(profile_id=1)}

    async def films(self, place: Place) -> dict[int, bool]:
        return {entry.movie_id: entry.auto_download for entry in await place.db.watchlist.entries(profile_id=1)}


# -- the one list of secrets -------------------------------------------------------------------


class SecretFieldTests(unittest.TestCase):
    def test_every_secret_looking_setting_is_classified(self) -> None:
        unclassified = sorted(
            name
            for name in Settings.model_fields
            if SECRET_NAME.search(name)
            and name not in SECRET_FIELDS
            and name not in NOT_SECRET_FIELDS
            and name not in MACHINE_SECRETS
        )
        self.assertEqual(
            unclassified, [], "add to SECRET_FIELDS (or NOT_SECRET_FIELDS) in app/services/backup.py"
        )

    def test_every_hidden_value_is_a_secret(self) -> None:
        hidden = {
            name
            for name, field in Settings.model_fields.items()
            if isinstance(field.default, SecretStr)
        }
        self.assertEqual(hidden - SECRET_FIELDS - MACHINE_SECRETS, set())

    def test_the_lists_name_real_settings(self) -> None:
        for names in (SECRET_FIELDS, NOT_SECRET_FIELDS, MACHINE_FIELDS):
            with self.subTest(names=sorted(names)):
                self.assertLessEqual(names, set(Settings.model_fields))
        self.assertFalse(SECRET_FIELDS & NOT_SECRET_FIELDS)
        self.assertFalse(SECRET_FIELDS & MACHINE_FIELDS)
        self.assertLessEqual(MACHINE_SECRETS, MACHINE_FIELDS)  # this PC's: never in a backup

    def test_the_secrets_the_brief_names(self) -> None:
        self.assertEqual(
            SECRET_FIELDS,
            {
                "tmdb_api_key",
                "prowlarr_api_key",
                "remote_password",
                "opensubtitles_password",
                "opensubtitles_own_key",
            },
        )


# -- pure parts --------------------------------------------------------------------------------


class PureTests(unittest.TestCase):
    def test_the_suggested_file_name(self) -> None:
        self.assertEqual(
            default_file_name(date(2026, 10, 31)), "AirGrabber backup 2026-10-31.airgrabber"
        )

    def test_settings_without_secrets_and_markers(self) -> None:
        settings = Settings.model_validate(SETTINGS_A)
        kept = settings_for_backup(settings, include_secrets=False)
        self.assertFalse(set(kept) & SECRET_FIELDS)
        self.assertFalse(set(kept) & MACHINE_FIELDS)
        self.assertEqual(kept["language"], "nl")
        self.assertEqual(kept["download_dir"], r"D:\Downloads\Alex")  # paths stay
        self.assertEqual(kept["remote_username"], "alex")
        self.assertEqual(kept["excluded_keywords"], ["cam", "ts"])

    def test_settings_with_secrets(self) -> None:
        kept = settings_for_backup(Settings.model_validate(SETTINGS_A), include_secrets=True)
        self.assertEqual({name: kept[name] for name in SECRET_FIELDS}, SECRETS)
        self.assertFalse(set(kept) & MACHINE_FIELDS)

    def test_empty_secrets_never_go_in(self) -> None:
        kept = settings_for_backup(Settings(), include_secrets=True)
        self.assertFalse(set(kept) & SECRET_FIELDS)

    def test_replace_keeps_this_pcs_markers_and_missing_secrets(self) -> None:
        current = Settings(tmdb_api_key="mine", setup_done=True, last_run_version="2.0.9")
        stored = {"language": "nl", "tmdb_api_key": "", "setup_done": False, "quality_tv": "2160p"}
        changes = replace_changes(valid_settings(stored), current)
        self.assertEqual(changes["language"], Language.NL)
        self.assertNotIn("tmdb_api_key", changes)  # empty in the file: kept
        self.assertNotIn("setup_done", changes)
        self.assertNotIn("last_run_version", changes)

    def test_replace_takes_a_secret_the_file_has(self) -> None:
        changes = replace_changes(
            valid_settings({"remote_password": "theirs"}), Settings(remote_password="mine")
        )
        self.assertEqual(changes["remote_password"], SecretStr("theirs"))

    def test_replace_drops_a_value_that_doesnt_validate(self) -> None:
        current = Settings(card_size="l")
        with self.assertLogs(LOGGER, "WARNING"):
            loaded = valid_settings({"card_size": "huge", "language": "nl"})
        changes = replace_changes(loaded, current)
        self.assertEqual(changes["card_size"], "m")  # the default, as SettingsRepository.load
        self.assertEqual(changes["language"], Language.NL)

    def test_a_nas_carries_over(self) -> None:
        loaded = Settings.model_validate(SETTINGS_A)
        self.assertIs(kept_downloads(loaded, Settings()), KeptDownloads.NONE)
        changes = replace_changes(loaded, Settings())
        for name in ("download_mode", "remote_url", "remote_username", "remote_password"):
            with self.subTest(name=name):
                self.assertEqual(changes[name], getattr(loaded, name))

    def test_this_pcs_own_qbittorrent_stays(self) -> None:
        current = Settings.model_validate(LOCAL_QBITTORRENT)
        loaded = Settings.model_validate(SETTINGS_A)  # a NAS
        self.assertIs(kept_downloads(loaded, current), KeptDownloads.LOCAL)
        changes = replace_changes(loaded, current)
        self.assertFalse(set(changes) & LOCAL_DOWNLOAD_FIELDS)
        self.assertEqual(changes["movie_remote_destination"], "video/movies")  # from the backup
        self.assertEqual(changes["language"], Language.NL)

    def test_the_other_pcs_own_qbittorrent_stays_behind(self) -> None:
        current = Settings(download_mode="app")
        loaded = Settings.model_validate({**LOCAL_QBITTORRENT, "language": "nl"})
        self.assertIs(kept_downloads(loaded, current), KeptDownloads.THIS_PC)
        changes = replace_changes(loaded, current)
        self.assertFalse(set(changes) & LOCAL_DOWNLOAD_FIELDS)
        self.assertEqual(changes["language"], Language.NL)
        # In folder mode it may still be the program for magnet links: the same rule.
        folder = Settings.model_validate({**LOCAL_QBITTORRENT, "download_mode": "folder"})
        self.assertIs(kept_downloads(folder, current), KeptDownloads.THIS_PC)

    def test_a_qbittorrent_elsewhere_is_not_its_own(self) -> None:
        nas = {**LOCAL_QBITTORRENT, "remote_url": "http://192.168.1.20:8080"}
        self.assertIs(kept_downloads(Settings.model_validate(nas), Settings()), KeptDownloads.NONE)
        mine = {**LOCAL_QBITTORRENT, "remote_username": "alex"}
        self.assertIs(kept_downloads(Settings.model_validate(mine), Settings()), KeptDownloads.NONE)


# -- the file ----------------------------------------------------------------------------------


class FileFormatTests(BackupTestCase):
    async def test_the_header_and_what_is_in_it(self) -> None:
        place = await self.pc_a()
        summary = await place.backup.save(self.file, include_secrets=False)
        raw = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(raw["format"], FORMAT_NAME)
        self.assertEqual(raw["version"], FORMAT_VERSION)
        self.assertEqual(raw["app_version"], "2.0.9")
        self.assertEqual(datetime.fromisoformat(raw["created"]), NOW)
        self.assertEqual([item["show"]["name"] for item in raw["shows"]], ["Fargo", "Severance"])
        fargo = raw["shows"][0]
        self.assertTrue(fargo["auto_download"])
        self.assertEqual(sorted(fargo["have"]), [101, 102])
        self.assertEqual(len(fargo["episodes"]), 6)
        self.assertEqual([film["movie"]["title"] for film in raw["watchlist"]], ["Dune", "Arrival"])
        self.assertEqual(len(raw["history"]), 3)
        self.assertEqual([grab["cleared"] for grab in raw["history"]], [True, False, False])
        self.assertTrue(all(grab["record"]["id"] is None for grab in raw["history"]))
        self.assertFalse(set(raw["settings"]) & (SECRET_FIELDS | MACHINE_FIELDS))
        self.assertEqual((summary.shows, summary.films, summary.grabs), (2, 2, 3))
        self.assertTrue(summary.settings)
        self.assertFalse(summary.secrets)
        # Left out entirely: no secret value anywhere in the file, no cached show.
        text = self.file.read_text(encoding="utf-8")
        for value in SECRETS.values():
            self.assertNotIn(value, text)
        self.assertNotIn("Not followed", text)

    async def test_keys_and_passwords_go_in_when_asked(self) -> None:
        await self.saved_backup(include_secrets=True)
        raw = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual({name: raw["settings"][name] for name in SECRET_FIELDS}, SECRETS)
        self.assertTrue(summarize(decode(self.file.read_bytes())).secrets)

    async def test_saving_where_it_cant_says_so(self) -> None:
        place = await self.pc_a()
        target = self.folder / "missing folder" / "backup.airgrabber"
        with self.assertRaises(BackupError) as caught, self.assertLogs(LOGGER, "WARNING"):
            await place.backup.save(target, include_secrets=False)
        self.assertIs(caught.exception.problem, BackupProblem.UNWRITABLE)
        self.assertNotIn("missing folder", str(caught.exception))
        self.assertFalse(target.parent.exists())

    async def test_saving_over_an_earlier_backup(self) -> None:
        self.file.write_text("old", encoding="utf-8")
        await self.saved_backup()
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["format"], FORMAT_NAME)
        self.assertEqual([p.name for p in self.folder.glob("*.part")], [])


class DecodeTests(unittest.TestCase):
    def problem(self, data: bytes) -> BackupError:
        with self.assertRaises(BackupError) as caught:
            decode(data)
        return caught.exception

    @staticmethod
    def raw(**fields: Any) -> bytes:
        header = {"format": FORMAT_NAME, "version": 1, "created": "2026-10-31T12:00:00Z"}
        return json.dumps({**header, **fields}).encode("utf-8")

    def test_another_kind_of_file(self) -> None:
        for data in (b"hello", b"\xff\xfe\x00", b"[1, 2]", b'{"format": "other", "version": 1}'):
            with self.subTest(data=data):
                error = self.problem(data)
                self.assertIs(error.problem, BackupProblem.NOT_A_BACKUP)
                self.assertEqual(str(error), "That file isn't an AirGrabber backup.")

    def test_a_damaged_backup(self) -> None:
        cases = (
            self.raw()[:-10],  # cut off
            self.raw(shows="lots"),
            self.raw(shows=[{"show": {"id": 1}}]),  # no name, no tracked_at
            self.raw(version="one"),
            self.raw(settings=["not", "a", "dict"]),
            self.raw(
                shows=[
                    {
                        "show": {"id": 1, "name": "Fargo"},
                        "tracked_at": "2026-10-01T00:00:00Z",
                        "episodes": [{"id": 201, "show_id": 2, "season": 1}],
                    }
                ]
            ),
        )
        for data in cases:
            with self.subTest(data=data[:80]):
                error = self.problem(data)
                self.assertIs(error.problem, BackupProblem.DAMAGED)
                self.assertEqual(str(error), "This backup is damaged and can't be loaded.")

    def test_a_newer_version(self) -> None:
        error = self.problem(self.raw(version=FORMAT_VERSION + 1, app_version="3.1.0"))
        self.assertIs(error.problem, BackupProblem.NEWER)
        self.assertIn("3.1.0", str(error))
        self.assertIn("Update AirGrabber", str(error))
        error = self.problem(self.raw(version=FORMAT_VERSION + 1))
        self.assertIs(error.problem, BackupProblem.NEWER)

    def test_a_newer_versions_extra_fields_are_ignored(self) -> None:
        data = self.raw(
            something_new=True,
            shows=[
                {
                    "show": {"id": 1, "name": "Fargo", "new_field": 1},
                    "tracked_at": "2026-10-01T00:00:00Z",
                    "colour": "red",
                    "episodes": [{"id": 101, "show_id": 1, "season": 1, "stars": 5}],
                }
            ],
            watchlist=[{"movie": {"id": 10, "title": "Dune", "x": 1}, "added_at": NOW.isoformat()}],
            history=[
                {"record": {"media_type": "movie", "media_id": 12, "release_title": "Heat",
                            "downloaded_at": NOW.isoformat(), "y": 2}}
            ],
            settings={"language": "nl", "a_new_setting": 1},
        )
        backup = decode(data)
        self.assertEqual(backup.shows[0].show.name, "Fargo")
        self.assertEqual(backup.shows[0].episodes[0].id, 101)
        self.assertEqual(backup.watchlist[0].movie.title, "Dune")
        self.assertEqual(backup.history[0].record.release_title, "Heat")

    def test_encode_and_decode_agree(self) -> None:
        backup = decode(self.raw(settings={"language": "nl"}))
        self.assertEqual(decode(encode(backup)), backup)


# -- loading -----------------------------------------------------------------------------------


class RoundTripTests(BackupTestCase):
    async def test_into_a_fresh_database(self) -> None:
        a = await self.saved_backup()
        b = await self.place("b", tmdb_api_key="tmdb-key-of-b")
        backup = await b.backup.open(self.file)
        result = await b.backup.load(backup, LoadMode.REPLACE)

        # Shows (switch, since when, episodes, marks), films and the history: as on PC A.
        self.assertEqual(await BackupRepository(b.db).read(), await BackupRepository(a.db).read())
        self.assertEqual(await self.tracked(b), {1: True, 2: False})
        self.assertEqual(await have_marks(b.db, 1), {101, 102})
        self.assertTrue(all(entry.synced_at is None for entry in await b.db.library.tracked(profile_id=1)))
        self.assertIsNone(await b.db.library.get_show(9))  # the cache stayed behind
        self.assertEqual(await self.films(b), {10: True, 11: False})
        self.assertTrue(await b.db.history.has_episode(1, 1, 3))
        self.assertTrue(await b.db.history.has_movie(12))
        self.assertEqual(len(await b.db.history.listed()), 2)  # one was cleared off the page

        settings = b.settings.current
        self.assertIs(settings.language, Language.NL)
        self.assertIs(settings.accent, Accent.OCEAN)
        self.assertEqual(str(settings.download_dir), r"D:\Downloads\Alex")
        self.assertEqual(str(settings.movie_download_dir), r"D:\Downloads\Alex\Films")
        self.assertEqual(settings.remote_destination, "video/tv")
        self.assertEqual(settings.movie_remote_destination, "video/movies")
        self.assertIs(result.downloads_kept, KeptDownloads.NONE)  # a NAS carries over
        self.assertEqual(settings.remote_url, "http://192.168.1.20:5000")
        self.assertEqual(settings.quality_tv, "720p")
        # Not in the file: B's key stays, B's markers stay.
        self.assertEqual(settings.tmdb_api_key.get_secret_value(), "tmdb-key-of-b")
        self.assertEqual(settings.remote_password.get_secret_value(), "")
        self.assertFalse(settings.setup_done)
        self.assertFalse(settings.start_with_windows)
        self.assertEqual(settings.last_run_version, "")
        self.assertEqual(settings.prowlarr_seeded_url, "")
        # Stored too, not only in memory.
        self.assertEqual(await b.db.settings.load(), settings)

        self.assertEqual(result.counts.shows_added, 2)
        self.assertEqual(result.counts.films_added, 2)
        self.assertEqual(result.counts.grabs_added, 3)
        self.assertTrue(result.settings_replaced)
        self.assertFalse(result.secrets_loaded)
        self.assertIs(result.language, Language.NL)
        self.assertIs(result.accent, Accent.OCEAN)
        self.assertTrue(b.watchlist.contains(10))  # read again
        self.assertEqual(b.loads, [1])  # the pages hear it

    async def test_keys_and_passwords_come_along_when_included(self) -> None:
        await self.saved_backup(include_secrets=True)
        b = await self.place("b", tmdb_api_key="tmdb-key-of-b")
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.REPLACE)
        current = b.settings.current
        for name, value in SECRETS.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(current, name).get_secret_value(), value)
        self.assertTrue(result.secrets_loaded)

    async def test_a_load_never_wipes_a_key(self) -> None:
        await self.saved_backup(include_secrets=True)
        backup = await (await self.place("c")).backup.open(self.file)
        emptied = backup.model_copy(
            update={"settings": {**backup.settings, "tmdb_api_key": "", "remote_password": " "}}
        )
        b = await self.place("b", tmdb_api_key="mine", remote_password="nas-of-b")
        await b.backup.load(emptied, LoadMode.REPLACE)
        self.assertEqual(b.settings.current.tmdb_api_key.get_secret_value(), "mine")
        self.assertEqual(b.settings.current.remote_password.get_secret_value(), "nas-of-b")
        self.assertEqual(
            b.settings.current.opensubtitles_own_key.get_secret_value(), "os-key-of-alex"
        )


class ModeTests(BackupTestCase):
    async def pc_b(self) -> Place:
        """Already has Fargo (automatic downloads off, no marks) and Mythic Quest, Arrival
        (switch on) and Heat (which PC A downloaded) on the watchlist, and English."""
        b = await self.place("b", language="en", quality_tv="1080p")
        db = b.db
        await db.library.save_show(show(1, "Fargo"), episodes(1, 7))
        await db.library.track(1, profile_id=1)
        await db.library.mark_synced(1)
        await db.library.save_show(show(3, "Mythic Quest"), episodes(3, 2))
        await db.library.track(3, auto_download=True, profile_id=1)
        await mark_have(db, [301], NOW.isoformat())
        await db.watchlist.add(movie(11, "Arrival"), auto_download=True, when=NOW, profile_id=1)
        await db.watchlist.add(movie(12, "Heat"), auto_download=True, when=NOW, profile_id=1)
        await db.watchlist.add(movie(20, "Past Lives"), auto_download=False, when=NOW, profile_id=1)
        await b.watchlist.load()
        return b

    async def test_add_keeps_what_is_here(self) -> None:
        await self.saved_backup()
        b = await self.pc_b()
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.ADD)
        self.assertEqual(await self.tracked(b), {1: False, 2: False, 3: True})
        self.assertEqual(await have_marks(b.db, 1), frozenset())  # B's (none) stay
        self.assertEqual(await have_marks(b.db, 3), {301})
        self.assertEqual(len(await b.db.library.episodes(1)), 7)  # B's own episodes stay
        # Heat was downloaded on PC A: it leaves the list; Arrival keeps B's switch.
        self.assertEqual(await self.films(b), {10: True, 11: True, 20: False})
        self.assertFalse(b.watchlist.contains(12))
        self.assertEqual(b.settings.current.language, Language.EN)
        self.assertEqual(b.settings.current.quality_tv, "1080p")
        self.assertEqual(
            (result.counts.shows_added, result.counts.shows_removed), (1, 0)
        )
        self.assertEqual((result.counts.films_added, result.counts.films_removed), (1, 0))
        self.assertFalse(result.settings_replaced)
        self.assertIs(result.language, Language.EN)

    async def test_replace_makes_it_the_backups(self) -> None:
        await self.saved_backup()
        b = await self.pc_b()
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.REPLACE)
        self.assertEqual(await self.tracked(b), {1: True, 2: False})
        self.assertEqual(await have_marks(b.db, 1), {101, 102})
        entries = {entry.show.id: entry for entry in await b.db.library.tracked(profile_id=1)}
        self.assertIsNotNone(entries[1].synced_at)  # followed here already: its sync state stays
        self.assertEqual(len(await b.db.library.episodes(1)), 7)  # and its fresher episodes
        self.assertEqual(await self.films(b), {10: True, 11: False})
        self.assertEqual(b.settings.current.language, Language.NL)
        self.assertEqual(b.settings.current.quality_tv, "720p")
        self.assertEqual((result.counts.shows_added, result.counts.shows_removed), (1, 1))
        self.assertEqual((result.counts.films_added, result.counts.films_removed), (1, 1))
        self.assertTrue(result.settings_replaced)

    async def test_the_auto_switch_keeps_its_moment(self) -> None:
        a = await self.saved_backup()
        b = await self.place("b")
        await b.backup.load(await b.backup.open(self.file), LoadMode.ADD)
        mine = {item.show.id: item.auto_since for item in (await BackupRepository(b.db).read()).shows}
        theirs = {item.show.id: item.auto_since for item in (await BackupRepository(a.db).read()).shows}
        self.assertEqual(mine, theirs)
        self.assertEqual(mine[1], AUTO_SINCE)

    async def test_the_history_is_added_once(self) -> None:
        await self.saved_backup()
        b = await self.place("b")
        backup = await b.backup.open(self.file)
        first = await b.backup.load(backup, LoadMode.ADD)
        again = await b.backup.load(backup, LoadMode.ADD)
        self.assertEqual(first.counts.grabs_added, 3)
        self.assertEqual(again.counts.grabs_added, 0)
        self.assertEqual(len(await b.db.history.recent()), 3)
        self.assertEqual((again.counts.shows_added, again.counts.films_added), (0, 0))


class LocalQBittorrentTests(BackupTestCase):
    """The qBittorrent on a PC that AirGrabber set up only works on that PC: on Replace, where
    downloads go stays as it is here when either PC uses its own."""

    def assert_connection(self, settings: Settings, expected: dict[str, Any]) -> None:
        for name, value in expected.items():
            with self.subTest(name=name):
                stored = getattr(settings, name)
                if isinstance(stored, SecretStr):
                    stored = stored.get_secret_value()
                self.assertEqual(stored, value)

    async def test_this_pcs_own_qbittorrent_keeps_the_downloads(self) -> None:
        await self.saved_backup(include_secrets=True)  # PC A sends to a NAS, with its password
        b = await self.place("b", **LOCAL_QBITTORRENT)
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.REPLACE)
        current = b.settings.current
        self.assert_connection(current, LOCAL_QBITTORRENT)
        self.assertTrue(current.remote_verify_tls)
        # Everything else is the backup's, films' folder there and the other keys included.
        self.assertEqual(current.movie_remote_destination, "video/movies")
        self.assertEqual(current.language, Language.NL)
        self.assertEqual(current.tmdb_api_key.get_secret_value(), SECRETS["tmdb_api_key"])
        self.assertIs(result.downloads_kept, KeptDownloads.LOCAL)
        self.assertTrue(result.settings_replaced)

    async def test_the_other_pcs_own_qbittorrent_stays_behind(self) -> None:
        a = await self.place("a", **{**SETTINGS_A, **LOCAL_QBITTORRENT})
        await a.backup.save(self.file, include_secrets=True)
        b = await self.place("b", download_mode="app")
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.REPLACE)
        current = b.settings.current
        self.assert_connection(
            current,
            {
                "download_mode": "app",
                "remote_kind": "synology",  # the default: nothing set up here
                "remote_url": "",
                "remote_username": "",
                "remote_password": "",
                "remote_destination": "",
            },
        )
        self.assertEqual(current.language, Language.NL)
        self.assertEqual(str(current.download_dir), r"D:\Downloads\Alex")
        self.assertIs(result.downloads_kept, KeptDownloads.THIS_PC)

    async def test_both_own_qbittorrent_keeps_this_ones_port_and_password(self) -> None:
        other = {**LOCAL_QBITTORRENT, "remote_url": "http://127.0.0.1:8090",
                 "remote_password": "the-other-pcs"}
        a = await self.place("a", **other)
        await a.backup.save(self.file, include_secrets=True)
        b = await self.place("b", **LOCAL_QBITTORRENT)
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.REPLACE)
        self.assert_connection(b.settings.current, LOCAL_QBITTORRENT)
        self.assertIs(result.downloads_kept, KeptDownloads.LOCAL)

    async def test_add_never_touches_them(self) -> None:
        await self.saved_backup(include_secrets=True)
        b = await self.place("b", **LOCAL_QBITTORRENT)
        result = await b.backup.load(await b.backup.open(self.file), LoadMode.ADD)
        self.assert_connection(b.settings.current, LOCAL_QBITTORRENT)
        self.assertIs(result.downloads_kept, KeptDownloads.NONE)


class BadFileTests(BackupTestCase):
    async def assert_unchanged(self, b: Place, path: Path, problem: BackupProblem) -> None:
        before = await BackupRepository(b.db).read()
        settings = b.settings.current
        with self.assertRaises(BackupError) as caught:
            await b.backup.open(path)
        self.assertIs(caught.exception.problem, problem)
        self.assertEqual(await BackupRepository(b.db).read(), before)
        self.assertEqual(b.settings.current, settings)
        self.assertEqual(b.loads, [])

    async def test_nothing_changes(self) -> None:
        b = await self.place("b", language="en")
        await b.db.library.save_show(show(3, "Mythic Quest"), episodes(3, 2))
        await b.db.library.track(3, profile_id=1)
        cases = {
            "notes.txt": (b"just some notes", BackupProblem.NOT_A_BACKUP),
            "other.json": (b'{"format": "something-else", "version": 1}', BackupProblem.NOT_A_BACKUP),
            "cut.airgrabber": (
                b'{"format": "airgrabber-backup", "version": 1, "shows": [{"show"',
                BackupProblem.DAMAGED,
            ),
            "newer.airgrabber": (
                json.dumps(
                    {"format": FORMAT_NAME, "version": 99, "app_version": "9.0", "shows": []}
                ).encode(),
                BackupProblem.NEWER,
            ),
        }
        for name, (data, problem) in cases.items():
            with self.subTest(name=name):
                path = self.folder / name
                path.write_bytes(data)
                await self.assert_unchanged(b, path, problem)
        await self.assert_unchanged(b, self.folder / "gone.airgrabber", BackupProblem.UNREADABLE)

    async def test_an_empty_backup_replaces_with_nothing_but_keeps_the_settings_markers(
        self,
    ) -> None:
        b = await self.place("b", setup_done=True)
        await b.db.library.save_show(show(3, "Mythic Quest"), episodes(3, 2))
        await b.db.library.track(3, profile_id=1)
        empty = BackupFile(format=FORMAT_NAME, version=FORMAT_VERSION, created=NOW)
        result = await b.backup.load(empty, LoadMode.REPLACE)
        self.assertEqual(await self.tracked(b), {})
        self.assertFalse(result.settings_replaced)  # no settings in it: the settings stay
        self.assertTrue(b.settings.current.setup_done)


if __name__ == "__main__":
    unittest.main()
