"""The Downloads page in sync with the torrent program (3.0.2): every task shows (a grab's,
an older grab's, or one added in the program, recognised when AirGrabber knows the show or
film), removals in the program are mirrored, a grab the program never had waits an hour and
then is a problem, the ⋯ menu's pause / resume / remove and "Clear finished" show at once and
come back on a failure, and the v9 migration with its upgrade rule. The program is a fake
client; the history lives in a real database."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.database import MIGRATIONS, SEEN_MIGRATION, Database
from app.models import (
    DownloadEntry,
    DownloadMode,
    DownloadState,
    MediaType,
    Movie,
    RemoteKind,
    RemoteTask,
    Show,
    TaskEntry,
)
from app.services import download_sync as sync
from app.services.download_status import Located
from app.services.download_sync import (
    GoneGrabs,
    KnownTitles,
    Recognition,
    pickup_state,
    recognise,
    title_key,
)
from app.services.remote import RemoteError
from tests.test_download_status import (
    FALLOUT,
    HASH_A,
    HASH_B,
    NOW,
    FakeStatusClient,
    ServiceTestCase,
    record,
    task,
)

SEVERANCE = Show(id=202, name="Severance", poster_url="https://static.tvmaze.com/severance.jpg")
SHOGUN = Show(id=303, name="Shōgun")
OFFICE = Show(id=404, name="The Office")
DUNE = Movie(
    id=693134, title="Dune: Part Two", release_date=date(2024, 2, 27), poster_path="/dune.jpg"
)


def program_task(
    name: str,
    state: DownloadState = DownloadState.DOWNLOADING,
    progress: float = 0.42,
    *,
    task_id: str,
    info_hash: str | None = None,
    added: datetime = NOW - timedelta(hours=2),
    size: int | None = 2 * 1024**3,
) -> RemoteTask:
    return RemoteTask(
        name=name,
        info_hash=info_hash,
        progress=progress,
        state=state,
        task_id=task_id,
        size=size,
        added_at=added,
    )


# -- the pure rules ------------------------------------------------------------------------------


class RecognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.known = KnownTitles.build(
            [SEVERANCE, SHOGUN, OFFICE],
            [DUNE],
            [record("Gladiator.II.2024.1080p.WEB", media_type=MediaType.MOVIE, media_id=558449)],
        )

    def test_an_episode_of_a_known_show(self) -> None:
        title, what = recognise("Severance.S02E04.1080p.WEB.h264-GRP", self.known) or ("", None)
        self.assertEqual(title, "Severance S02E04")
        assert what is not None
        self.assertEqual((what.name, what.show, what.movie_id), ("Severance", SEVERANCE, None))
        # Accents, a year or a country in the release name, "2x04".
        self.assertEqual(recognise("Shogun.S01E03.720p", self.known)[0], "Shōgun S01E03")  # type: ignore[index]
        self.assertEqual(recognise("The.Office.US.S05E10.720p", self.known)[0], "The Office S05E10")  # type: ignore[index]
        self.assertEqual(recognise("Severance 2022 2x04 HDTV", self.known)[0], "Severance S02E04")  # type: ignore[index]

    def test_a_known_film(self) -> None:
        title, what = recognise("Dune.Part.Two.2024.2160p.WEB-DL", self.known) or ("", None)
        self.assertEqual(title, "Dune: Part Two (2024)")
        assert what is not None
        self.assertEqual((what.movie_id, what.show), (DUNE.id, None))
        self.assertEqual(what.poster_url, "https://image.tmdb.org/t/p/w185/dune.jpg")
        # A release year one off still counts; a film from the history has no poster.
        self.assertEqual(recognise("Dune Part Two 2025 1080p", self.known)[0], "Dune: Part Two (2024)")  # type: ignore[index]
        title, what = recognise("Gladiator.II.2024.2160p.REMUX", self.known) or ("", None)
        self.assertEqual(title, "Gladiator II (2024)")
        assert what is not None
        self.assertEqual((what.movie_id, what.poster_url), (558449, None))

    def test_anything_else_is_not_recognised(self) -> None:
        for name in (
            "ubuntu-24.04.1-desktop-amd64.iso",
            "Andor.S02E01.1080p",  # a show nobody follows
            "Some.Film.2023.1080p",  # a film AirGrabber doesn't know
            "S01E01",  # no name before the marker
            "",
        ):
            self.assertIsNone(recognise(name, self.known), name)

    def test_keys(self) -> None:
        self.assertEqual(title_key("Shōgun"), "shogun")
        self.assertEqual(title_key("Marvel's Agents of S.H.I.E.L.D."), "marvels agents of s h i e l d")

    def test_answers_are_kept_until_the_titles_change(self) -> None:
        recognition = Recognition()
        self.assertIsNone(recognition.of("Severance.S02E04"))
        recognition.update(self.known)
        found = recognition.of("Severance.S02E04")
        assert found is not None
        self.assertIs(recognition.of("Severance.S02E04"), found)


class RuleTests(unittest.TestCase):
    def test_waiting_then_never_picked_up(self) -> None:
        self.assertIs(pickup_state(NOW - timedelta(minutes=59), NOW), DownloadState.PENDING)
        self.assertIs(pickup_state(NOW - timedelta(hours=1), NOW), DownloadState.NOT_FOUND)
        self.assertTrue(DownloadState.PENDING.in_progress)  # the badge counts it
        self.assertFalse(DownloadState.NOT_FOUND.in_progress)

    def test_gone_grabs_are_confirmed_by_a_second_check_a_little_later(self) -> None:
        gone = GoneGrabs()
        self.assertEqual(gone.confirm({1, 2}, 100.0), [])
        self.assertEqual(gone.confirm({1, 2}, 110.0), [])  # too soon after the first
        self.assertEqual(gone.confirm({1}, 120.0), [])  # 2 came back: it starts over
        self.assertEqual(gone.confirm({1, 2}, 130.0), [1])
        self.assertEqual(gone.confirm({2}, 170.0), [2])
        self.assertEqual(dict(gone.waiting), {})


# -- the service ---------------------------------------------------------------------------------


class FakeSyncClient(FakeStatusClient):
    """A status client with the ⋯ menu's actions; `fail` makes the next one fail."""

    def __init__(self, kind: RemoteKind) -> None:
        super().__init__(kind)
        self.actions: list[tuple[str, list[str]]] = []
        self.fail: RemoteError | None = None

    async def _do(self, name: str, task_ids: Collection[str]) -> None:
        if self.fail is not None:
            failure, self.fail = self.fail, None
            raise failure
        self.actions.append((name, list(task_ids)))
        if name == "remove":
            self.tasks = tuple(t for t in self.tasks if t.task_id not in task_ids)

    async def pause(self, task_ids: Collection[str]) -> None:
        await self._do("pause", task_ids)

    async def resume(self, task_ids: Collection[str]) -> None:
        await self._do("resume", task_ids)

    async def remove(self, task_ids: Collection[str]) -> None:
        await self._do("remove", task_ids)


class SyncTestCase(ServiceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        for kind in RemoteKind:
            self.clients[kind] = FakeSyncClient(kind)
        self.remote._clients = dict(self.clients)
        self.now = NOW
        self.service._now = lambda: self.now
        self.known_shows: list[Show] = [SEVERANCE]
        self.known_films: list[Movie] = [DUNE]

        async def known() -> tuple[list[Show], list[Movie]]:
            return self.known_shows, self.known_films

        self.service._known_lookup = known

    @property
    def nas(self) -> FakeSyncClient:
        client = self.clients[RemoteKind.SYNOLOGY]
        assert isinstance(client, FakeSyncClient)
        return client

    def ids(self, rows: Any) -> list[str]:
        return [
            f"grab:{row.record.id}" if isinstance(row, DownloadEntry) else f"task:{row.task.task_id}"
            for row in rows
        ]


class EveryTaskTests(SyncTestCase):
    async def test_grabs_older_grabs_and_tasks_added_in_the_program(self) -> None:
        fallout = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        waiting = await self.add(record("Fallout.S02E04.1080p.WEB", episode=4,
                                        when=NOW - timedelta(minutes=10)))
        old = await self.add(record("Fallout.S01E08.1080p.WEB", season=1, episode=8,
                                    info_hash=HASH_B, when=NOW - timedelta(days=40)))
        self.nas.tasks = (
            program_task("whatever", task_id="dbid_1", info_hash=HASH_A),
            program_task("Severance.S02E04.1080p.WEB", task_id="dbid_2",
                         added=NOW - timedelta(minutes=30)),
            program_task("ubuntu-24.04.1-desktop-amd64.iso", DownloadState.STOPPED, 0.63,
                         task_id="dbid_3", added=NOW - timedelta(days=3)),
            program_task("Fallout S01E08", DownloadState.READY, 1.0, task_id="dbid_4",
                         info_hash=HASH_B, added=NOW - timedelta(days=40)),
        )
        overview = await self.service.check()

        self.assertTrue(overview.synced)
        self.assertEqual(
            self.ids(overview.rows),
            [f"grab:{waiting.id}", "task:dbid_2", f"grab:{fallout.id}", "task:dbid_3",
             f"grab:{old.id}"],  # newest first: grabbed 10 min, added 30 min, grabbed 1 h ago
        )
        by_id = {row.record.id: row for row in overview.entries}
        self.assertEqual(by_id[fallout.id].task, self.nas.tasks[0])
        self.assertEqual(by_id[fallout.id].state, DownloadState.DOWNLOADING)
        self.assertIs(by_id[waiting.id].state, DownloadState.PENDING)
        self.assertIsNone(by_id[waiting.id].task)
        # A grab older than the page's 30 days whose task is still there: a grab, not "added".
        self.assertEqual((by_id[old.id].title, by_id[old.id].state),
                         ("Fallout S01E08", DownloadState.READY))
        severance, iso = overview.tasks
        self.assertEqual((severance.title, severance.recognised.show if severance.recognised else None),
                         ("Severance S02E04", SEVERANCE))
        self.assertEqual((iso.title, iso.recognised, iso.state, iso.progress),
                         ("ubuntu-24.04.1-desktop-amd64.iso", None, DownloadState.STOPPED, 0.63))
        # The badge counts every task on its way: Fallout, the waiting grab and Severance.
        self.assertEqual(overview.in_progress, 3)

    async def test_a_grab_taken_off_the_list_before_302_shows_again_while_its_task_is_there(
        self,
    ) -> None:
        grab = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        assert grab.id is not None
        await self.db.history.clear([grab.id], NOW)
        self.nas.tasks = (program_task("x", DownloadState.READY, 1.0, task_id="dbid_1",
                                       info_hash=HASH_A),)
        overview = await self.service.check()
        self.assertEqual(self.ids(overview.rows), [f"grab:{grab.id}"])
        self.assertEqual(overview.tasks, ())

    async def test_without_a_program_to_read_the_page_is_as_before(self) -> None:
        await self.settings.update(download_mode=DownloadMode.APP)
        await self.add(record("A.S01E01", saved_path="qBittorrent"))
        self.nas.tasks = (program_task("x", task_id="dbid_1"),)
        overview = await self.service.check()
        self.assertFalse(overview.synced)
        self.assertEqual((len(overview.entries), overview.tasks), (1, ()))
        self.assertEqual(self.nas.asked, [])

    async def test_an_unreachable_program_is_no_sync(self) -> None:
        await self.add(record("A.S01E01"))
        self.nas.error = RemoteError("Can't reach the NAS at 192.168.1.20:5000.")
        overview = await self.service.check()
        self.assertFalse(overview.synced)
        self.assertEqual(overview.problem, "Can't reach the NAS at 192.168.1.20:5000.")
        self.assertIs(overview.entries[0].state, DownloadState.UNKNOWN)

    async def test_a_program_on_this_pc_isnt_woken_by_a_background_check(self) -> None:
        await self.settings.update(
            remote_kind=RemoteKind.QBITTORRENT, remote_url="http://127.0.0.1:8085",
            remote_username="airgrabber",
        )
        qbt = self.clients[RemoteKind.QBITTORRENT]
        overview = await self.service.check()  # hidden, nothing sent there: not asked
        self.assertEqual((qbt.asked, overview.synced), ([], False))
        self.service.set_visible(True)
        overview = await self.service.check()
        self.assertEqual((len(qbt.asked), overview.synced, overview.on_this_pc), (1, True, True))
        self.service.set_visible(False)
        await self.service.check()  # its last answer serves again
        self.assertEqual(len(qbt.asked), 1)

    async def test_recognising_reads_the_known_titles_once_in_a_while(self) -> None:
        calls: list[int] = []

        async def known() -> tuple[list[Show], list[Movie]]:
            calls.append(1)
            return [SEVERANCE], []

        self.service._known_lookup = known
        self.nas.tasks = (program_task("Severance.S02E04", task_id="dbid_1"),)
        await self.service.check()
        await self.service.check()
        self.assertEqual(len(calls), 1)
        self.clock += 5 * 60
        await self.service.check()
        self.assertEqual(len(calls), 2)


class ScaleTests(SyncTestCase):
    async def test_300_tasks_against_the_history(self) -> None:
        # 100 recent grabs and 200 older ones, all still in the program, plus 100 tasks added
        # there: every task listed once, each check quick (measured, printed).
        for n in range(300):
            when = NOW - timedelta(hours=n) if n < 100 else NOW - timedelta(days=31 + n)
            await self.db.history.add(record(
                f"Show.{n}.S01E{n % 90 + 1:02d}.1080p", media_id=1000 + n, season=1,
                episode=n % 90 + 1, info_hash=f"{n:040x}", when=when,
            ))
        self.nas.tasks = tuple(
            program_task(f"task {n}", DownloadState.READY, 1.0, task_id=f"dbid_{n}",
                         info_hash=f"{n:040x}" if n < 300 else None, added=NOW - timedelta(hours=n))
            for n in range(400)
        )
        times = []
        for _ in range(3):
            started = time.perf_counter()
            overview = await self.service.check()
            times.append(time.perf_counter() - started)
        self.assertEqual((len(overview.entries), len(overview.tasks)), (300, 100))
        self.assertEqual(len({row.task.task_id for row in overview.rows if row.task}), 400)
        self.assertLess(times[2], 1.0)


class MirrorTests(SyncTestCase):
    async def test_a_task_removed_in_the_program_leaves_the_page(self) -> None:
        grab = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        other = await self.add(record("Fallout.S02E04.1080p.WEB", episode=4, info_hash=HASH_B))
        self.nas.tasks = (
            program_task("a", task_id="dbid_1", info_hash=HASH_A),
            program_task("b", task_id="dbid_2", info_hash=HASH_B),
        )
        await self.service.check()
        self.assertEqual(await self.db.history.seen([grab.id or 0, other.id or 0]),
                         {grab.id, other.id})
        self.nas.tasks = self.nas.tasks[1:]  # removed in Download Station
        overview = await self.service.check()
        self.assertEqual(self.ids(overview.rows), [f"grab:{other.id}"])  # gone at once
        self.assertEqual(len(await self.db.history.listed(10)), 2)  # not marked yet
        self.clock += sync.MIRROR_CONFIRM_S
        overview = await self.service.check()
        self.assertEqual(self.ids(overview.rows), [f"grab:{other.id}"])
        self.assertEqual([r.id for r in await self.db.history.listed(10)], [other.id])
        # The history keeps it: "✓ Grabbed" and "already downloaded" still know it.
        self.assertTrue(await self.db.history.has_episode(FALLOUT.id, 2, 3))

    async def test_a_half_list_while_the_program_starts_costs_nothing(self) -> None:
        grab = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        full = (program_task("a", task_id="dbid_1", info_hash=HASH_A),)
        self.nas.tasks = full
        await self.service.check()
        self.nas.tasks = ()
        self.assertEqual((await self.service.check()).entries, ())
        self.clock += sync.MIRROR_CONFIRM_S
        self.nas.tasks = full  # it was there after all
        overview = await self.service.check()
        self.assertEqual(self.ids(overview.rows), [f"grab:{grab.id}"])
        self.assertEqual(len(await self.db.history.listed(10)), 1)

    async def test_waiting_for_the_program_then_never_picked_up(self) -> None:
        await self.settings.update(download_mode=DownloadMode.FOLDER, download_dir="D:/watch")
        grab = await self.add(record("Fallout.S02E03", saved_path=r"D:\watch\Fallout.S02E03.torrent",
                                     when=NOW - timedelta(minutes=5)))
        overview = await self.service.check()
        self.assertIs(overview.entries[0].state, DownloadState.PENDING)
        self.now = NOW + timedelta(minutes=56)
        overview = await self.service.check()
        self.assertIs(overview.entries[0].state, DownloadState.NOT_FOUND)
        # Picked up after all: it is seen from then on, and its removal is mirrored.
        self.nas.tasks = (program_task("Fallout.S02E03", task_id="dbid_1"),)
        overview = await self.service.check()
        self.assertIs(overview.entries[0].state, DownloadState.DOWNLOADING)
        self.assertEqual(await self.db.history.seen([grab.id or 0]), {grab.id})


class ActionTests(SyncTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.grab = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        self.nas.tasks = (
            program_task("a", task_id="dbid_1", info_hash=HASH_A),
            program_task("ubuntu.iso", DownloadState.READY, 1.0, task_id="dbid_2"),
            program_task("Severance.S02E01", DownloadState.READY, 1.0, task_id="dbid_3"),
            program_task("Broken.S01E01", DownloadState.PROBLEM, 0.2, task_id="dbid_4"),
        )
        await self.service.check()
        self.overviews.clear()

    def row(self, task_id: str) -> Any:
        latest = self.service.latest
        assert latest is not None
        return next(row for row in latest.rows if row.task and row.task.task_id == task_id)

    async def test_pause_shows_at_once_and_the_next_check_confirms(self) -> None:
        self.assertIsNone(await self.service.pause(self.row("dbid_1")))
        self.assertEqual(self.nas.actions, [("pause", ["dbid_1"])])
        self.assertIs(self.overviews[0].entries[0].state, DownloadState.STOPPED)  # at once
        self.assertIs(self.row("dbid_1").state, DownloadState.STOPPED)
        # The next check is the program's word (it says paused too).
        self.nas.tasks = (program_task("a", DownloadState.STOPPED, task_id="dbid_1",
                                       info_hash=HASH_A), *self.nas.tasks[1:])
        await self.service.check()
        self.assertIs(self.row("dbid_1").state, DownloadState.STOPPED)
        self.assertEqual(self.service._pending, {})
        self.assertIsNone(await self.service.resume(self.row("dbid_1")))
        self.assertIs(self.row("dbid_1").state, DownloadState.DOWNLOADING)

    async def test_a_failed_pause_puts_the_row_back_with_the_reason(self) -> None:
        self.nas.fail = RemoteError("Download Station can't do that with this download right now.")
        message = await self.service.pause(self.row("dbid_1"))
        self.assertEqual(message, "Download Station can't do that with this download right now.")
        self.assertEqual(
            [o.entries[0].state for o in self.overviews],
            [DownloadState.STOPPED, DownloadState.DOWNLOADING],  # at once, then back
        )

    async def test_remove_takes_the_row_off_at_once_and_keeps_the_history(self) -> None:
        self.assertIsNone(await self.service.remove(self.row("dbid_1")))
        self.assertEqual(self.nas.actions, [("remove", ["dbid_1"])])
        self.assertEqual(self.overviews[0].entries, ())
        self.assertEqual(await self.db.history.listed(10), [])  # off the list for good
        self.assertTrue(await self.db.history.has_episode(FALLOUT.id, 2, 3))
        overview = await self.service.check()
        self.assertEqual(overview.entries, ())
        # A task added in the program goes the same way.
        self.assertIsNone(await self.service.remove(self.row("dbid_4")))
        latest = self.service.latest
        assert latest is not None
        self.assertNotIn("task:dbid_4", self.ids(latest.rows))

    async def test_a_failed_remove_brings_the_row_back(self) -> None:
        self.nas.fail = RemoteError("Can't reach the NAS at 192.168.1.20:5000.")
        message = await self.service.remove(self.row("dbid_1"))
        self.assertEqual(message, "Can't reach the NAS at 192.168.1.20:5000.")
        self.assertEqual([len(o.entries) for o in self.overviews], [0, 1])
        self.assertEqual(len(await self.db.history.listed(10)), 1)

    async def test_clear_finished_takes_only_the_finished_ones(self) -> None:
        finished = self.service.finished()
        self.assertEqual(self.ids(finished), ["task:dbid_2", "task:dbid_3"])
        self.assertIsNone(await self.service.clear_finished())
        self.assertEqual(self.nas.actions, [("remove", ["dbid_2", "dbid_3"])])
        latest = self.service.latest
        assert latest is not None
        self.assertEqual(self.ids(latest.rows), [f"grab:{self.grab.id}", "task:dbid_4"])

    async def test_a_waiting_grab_without_a_task_has_nothing_to_remove_in_the_program(self) -> None:
        waiting = await self.add(record("Fallout.S02E05", episode=5, when=NOW - timedelta(minutes=5)))
        await self.service.check()
        row = next(r for r in self.service.latest.rows  # type: ignore[union-attr]
                   if isinstance(r, DownloadEntry) and r.record.id == waiting.id)
        self.assertIsNone(row.task)
        self.assertIsNone(await self.service.pause(row))
        self.assertEqual(self.nas.actions, [])
        self.assertEqual(await self.service.clear([waiting.id or 0]), 1)  # Remove from list

    async def test_a_task_added_in_the_program_is_found_in_the_finished_folder(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        video = Path(tmp.name) / "ubuntu.iso.dir" / "Severance.S02E01" / "s.mkv"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"\0" * 10)
        await self.settings.update(subtitles_folder=tmp.name)
        found = await self.service.locate(self.row("dbid_3"))
        self.assertEqual(found, Located(str(video.parent), ""))
        self.assertEqual(await self.service.locate(self.row("dbid_2")), Located(None, "not_found"))
        self.assertIsInstance(self.row("dbid_3"), TaskEntry)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_v9_is_the_seen_marks_and_self_contained(self) -> None:
        self.assertEqual(MIGRATIONS[8], SEEN_MIGRATION)
        self.assertNotIn("ALTER TABLE", SEEN_MIGRATION)

    async def test_a_v8_database_is_upgraded_old_grabs_counting_as_seen(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v8.db"
        now = datetime.now(UTC)
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:8])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 8;\nCOMMIT;")
            with raw:
                for title, when in (
                    ("Old.S01E01", now - timedelta(days=3)),
                    ("Hour.S01E02", now - timedelta(minutes=61)),
                    ("Fresh.S01E03", now - timedelta(minutes=5)),
                ):
                    raw.execute(
                        "INSERT INTO download_history (media_type, media_id, season, episode, "
                        "release_title, saved_path, downloaded_at) "
                        "VALUES ('tv', 1, 1, 1, ?, 'Download Station', ?)",
                        (title, when.isoformat()),
                    )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], len(MIGRATIONS))
        grabs = {r.release_title: r.id or 0 for r in await db.history.listed(10)}
        seen = await db.history.seen(grabs.values())
        self.assertEqual(
            {title for title, history_id in grabs.items() if history_id in seen},
            {"Old.S01E01", "Hour.S01E02"},
        )
        # Marking again is harmless; a mark goes with its grab.
        self.assertEqual(await db.history.mark_seen(grabs.values(), now), 1)
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM download_history")
        row = await db.fetch_one("SELECT COUNT(*) FROM seen_downloads")
        assert row is not None
        self.assertEqual(row[0], 0)


class UpgradeRuleTests(SyncTestCase):
    async def test_old_grabs_the_program_no_longer_has_leave_quietly(self) -> None:
        # As after the upgrade: the old grab counts as seen, the new one doesn't.
        old = await self.add(record("Fallout.S02E01.1080p", episode=1, when=NOW - timedelta(days=2)))
        new = await self.add(record("Fallout.S02E02.1080p", episode=2, when=NOW - timedelta(minutes=5)))
        await self.db.history.mark_seen([old.id or 0], NOW)
        overview = await self.service.check()
        self.assertEqual(self.ids(overview.rows), [f"grab:{new.id}"])  # no problem row
        self.assertIs(overview.entries[0].state, DownloadState.PENDING)


if __name__ == "__main__":
    unittest.main()
