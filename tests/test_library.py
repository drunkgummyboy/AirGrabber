"""LibraryService against a real SQLite file and a TVMazeClient on httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.config import AppPaths
from app.database import Database
from app.models import DownloadRecord, Episode, MediaType, Settings, SyncProgress, SyncReport
from app.models.tvmaze import TVMazeShow
from app.services import Services, SettingsService
from app.services.library import LibraryService, downloaded_ids, next_episode
from app.services.tvmaze import TVMazeClient, TVMazeError, TVMazeNotFound

FIXTURES = Path(__file__).parent / "fixtures" / "tvmaze"
LOGGER = "app.services"  # the library service and its TVMaze client

DUNE = 42846  # show_embed.json: 6 episodes from 2024, updated 1765814630
DRAGON = 44778  # web_show.json: 26 episodes, updated 1791048810
SEVERANCE = 44933  # first hit in search.json
DUNE_UPDATED = 1765814630
DRAGON_UPDATED = 1791048810


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def episode_payload(episode_id: int, day: date, *, number: int = 1) -> dict[str, Any]:
    return {"id": episode_id, "season": 1, "number": number, "airdate": day.isoformat()}


def show_payload(
    show_id: int,
    name: str | None = None,
    *,
    status: str = "Running",
    updated: int = 1_700_000_000,
    episodes: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "id": show_id,
        "name": name or f"Show {show_id}",
        "status": status,
        "updated": updated,
        "_embedded": {"episodes": list(episodes)},
    }


def schedule_item(
    episode_id: int, show_id: int, day: date, *, weight: int = 80, updated: int = 1_700_000_000
) -> dict[str, Any]:
    show = {
        "id": show_id,
        "name": f"Show {show_id}",
        "type": "Scripted",
        "language": "English",
        "weight": weight,
        "updated": updated,
    }
    return {**episode_payload(episode_id, day), "show": show}


class FakeTVMaze:
    """Serves TVMaze endpoints from fixtures and inline payloads, counting requests per path.

    Schedule requests are counted as `/schedule?<date>`. A path in `failures` answers with that
    status; a path in `gates` blocks until the event is set.
    """

    def __init__(self) -> None:
        self.shows: dict[int, Any] = {DUNE: load("show_embed.json"), DRAGON: load("web_show.json")}
        self.searches: dict[str, Any] = {"severance": load("search.json")}
        self.schedules: dict[date, Any] = {}
        self.updates: dict[str, int] = {}
        self.failures: dict[str, int] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.delay = 0.0
        self.requests: Counter[str] = Counter()
        self.in_flight = 0
        self.max_in_flight = 0
        self.cancelled = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        key = path
        if path == "/schedule":
            key = f"/schedule?{request.url.params['date']}"
        self.requests[key] += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if (gate := self.gates.get(path)) is not None:
                await gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._answer(request, key)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1

    def _answer(self, request: httpx.Request, key: str) -> httpx.Response:
        path = request.url.path
        if (status := self.failures.get(key)) is not None:
            return httpx.Response(status)
        if path == "/search/shows":
            return httpx.Response(200, json=self.searches.get(request.url.params["q"].lower(), []))
        if path == "/updates/shows":
            return httpx.Response(200, json=self.updates)
        if path == "/schedule":
            day = date.fromisoformat(request.url.params["date"])
            return httpx.Response(200, json=self.schedules.get(day, []))
        if path.startswith("/shows/"):
            show = self.shows.get(int(path.removeprefix("/shows/")))
            return httpx.Response(404) if show is None else httpx.Response(200, json=show)
        return httpx.Response(404)


async def until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=5)


class LibraryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = await Database.open(Path(self.tmp.name) / "library.db")
        self.server = FakeTVMaze()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server))
        self.service = LibraryService(
            self.db.library, self.db.history, TVMazeClient(self.http, retry_delay=0)
        )
        self.notifications = 0
        self.service.subscribe(self.on_change)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await self.db.close()
        self.tmp.cleanup()

    def on_change(self) -> None:
        self.notifications += 1

    async def store_tracked(self, payload: dict[str, Any], *, synced: bool = True) -> None:
        """Serve `payload` and track it via the service, or (never synced) straight in SQLite."""
        self.server.shows[payload["id"]] = payload
        if synced:
            await self.service.track(payload["id"])
        else:
            wire = TVMazeShow.model_validate(payload)
            await self.db.library.save_show(wire.to_show(), wire.to_episodes())
            await self.db.library.track(wire.id)
        self.notifications = 0

    async def synced_at(self) -> dict[int, datetime | None]:
        return {entry.show.id: entry.synced_at for entry in await self.service.tracked()}


class ListenerTests(LibraryTestCase):
    async def test_failing_listener_is_logged_and_others_still_run(self) -> None:
        calls: list[str] = []

        def broken() -> None:
            raise RuntimeError("boom")

        self.service.subscribe(broken)
        self.service.subscribe(lambda: calls.append("after"))
        with self.assertLogs(LOGGER, "ERROR"):
            await self.service.untrack(DUNE)
        self.assertEqual(calls, ["after"])
        self.assertEqual(self.notifications, 1)

    async def test_unsubscribe_is_idempotent(self) -> None:
        calls: list[str] = []
        unsubscribe = self.service.subscribe(lambda: calls.append("a"))
        self.service.subscribe(lambda: calls.append("b"))
        unsubscribe()
        unsubscribe()
        await self.service.untrack(DUNE)
        self.assertEqual(calls, ["b"])

    async def test_remove_ended_notifies_only_when_it_removed_something(self) -> None:
        await self.service.track(DUNE)
        self.notifications = 0
        self.assertEqual(await self.service.remove_ended(), 0)
        self.assertEqual(self.notifications, 0)
        await self.store_tracked(show_payload(1, status="Ended"))
        self.assertEqual(await self.service.remove_ended(), 1)
        self.assertEqual(self.notifications, 1)
        self.assertEqual([e.show.id for e in await self.service.tracked()], [DUNE])


class TrackTests(LibraryTestCase):
    async def test_track_saves_show_and_episodes(self) -> None:
        show = await self.service.track(DUNE)
        self.assertEqual((show.id, show.name), (DUNE, "Dune: Prophecy"))
        [entry] = await self.service.tracked()
        self.assertEqual(entry.show, show)
        self.assertIsNotNone(entry.synced_at)
        self.assertEqual(len(await self.db.library.episodes(DUNE)), 6)
        self.assertEqual(self.notifications, 1)

    async def test_track_is_idempotent_and_refreshes(self) -> None:
        await self.service.track(DUNE)
        first_sync = (await self.synced_at())[DUNE]
        self.server.shows[DUNE]["name"] = "Dune: Prophecy (renamed)"
        show = await self.service.track(DUNE)
        self.assertEqual(show.name, "Dune: Prophecy (renamed)")
        [entry] = await self.service.tracked()
        self.assertEqual(entry.show.name, "Dune: Prophecy (renamed)")
        assert entry.synced_at is not None and first_sync is not None
        self.assertGreaterEqual(entry.synced_at, first_sync)
        self.assertEqual(len(await self.db.library.episodes(DUNE)), 6)
        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 2)
        self.assertEqual(self.notifications, 2)

    async def test_track_propagates_tvmaze_errors(self) -> None:
        with self.assertRaises(TVMazeNotFound):
            await self.service.track(12345)
        self.assertEqual(await self.service.tracked(), [])
        self.assertEqual(self.notifications, 0)


class SearchTests(LibraryTestCase):
    async def test_search_flags_tracked_shows(self) -> None:
        await self.store_tracked(show_payload(SEVERANCE, "Severance"))
        results = await self.service.search("severance")
        self.assertEqual([r.show.id for r in results], [SEVERANCE, 85532, 70294])
        self.assertEqual([r.is_tracked for r in results], [True, False, False])

    async def test_search_applies_limit(self) -> None:
        results = await self.service.search("severance", limit=2)
        self.assertEqual(len(results), 2)

    async def test_search_without_hits(self) -> None:
        self.assertEqual(await self.service.search("nothing like this"), [])


class ImportTests(LibraryTestCase):
    async def test_entries_land_in_the_right_buckets(self) -> None:
        self.server.shows[SEVERANCE] = show_payload(SEVERANCE, "Severance")
        self.server.failures["/shows/99999"] = 500
        await self.store_tracked(load("web_show.json"))  # House of the Dragon
        progress: list[tuple[int, int]] = []

        with self.assertLogs(LOGGER, "WARNING"):
            result = await self.service.import_shows(
                [
                    f" {DUNE} ",  # by id
                    "Severance",  # by name
                    "",
                    "Nothing Matches",
                    str(DRAGON),  # already tracked
                    "99999",  # TVMaze answers 500
                    "12345",  # TVMaze answers 404
                    str(DUNE),  # duplicate line
                    "severance",  # resolves to a show added above
                    "   ",
                ],
                lambda done, total: progress.append((done, total)),
            )

        self.assertEqual(result.added, ("Dune: Prophecy", "Severance"))
        self.assertEqual(result.already_tracked, ("House of the Dragon", "Severance"))
        self.assertEqual(result.not_found, ("Nothing Matches", "12345"))
        self.assertEqual(result.failed, ("99999",))
        self.assertEqual(progress, [(n, 7) for n in range(1, 8)])
        self.assertEqual(self.notifications, 1)
        self.assertEqual(
            {e.show.id for e in await self.service.tracked()}, {DUNE, DRAGON, SEVERANCE}
        )
        self.assertEqual(self.server.requests[f"/shows/{DRAGON}"], 1)  # only the initial track
        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 1)

    async def test_nothing_added_means_no_notification(self) -> None:
        result = await self.service.import_shows(["Nothing Matches", ""])
        self.assertEqual(result.not_found, ("Nothing Matches",))
        self.assertEqual(self.notifications, 0)


class SuggestionTests(LibraryTestCase):
    def serve_schedule(self, *days: date) -> None:
        for index, day in enumerate(days):
            self.server.schedules[day] = [schedule_item(1000 + index, 500 + index, day)]

    async def fetch_day_as_of(self, day: date, fetched_at: datetime) -> None:
        with mock.patch("app.database._now", return_value=fetched_at.isoformat()):
            await self.db.library.save_schedule_day(day, [], [])

    async def test_fetches_missing_days_then_serves_from_cache(self) -> None:
        today = date.today()
        days = [today + timedelta(days=n) for n in range(3)]
        self.serve_schedule(*days)

        entries = await self.service.suggestions(days[0], days[-1], per_day=3)
        self.assertEqual([e.episode.air_date for e in entries], days)
        self.assertTrue(all(not e.is_tracked for e in entries))
        self.assertEqual(sum(self.server.requests.values()), 3)

        again = await self.service.suggestions(days[0], days[-1], per_day=3)
        self.assertEqual(again, entries)
        self.assertEqual(sum(self.server.requests.values()), 3)

    async def test_only_missing_or_stale_days_are_fetched(self) -> None:
        today = date.today()
        long_ago = datetime.now(UTC) - timedelta(days=3)
        recently = datetime.now(UTC) - timedelta(hours=1)
        await self.fetch_day_as_of(today - timedelta(days=3), long_ago)  # past and final
        await self.fetch_day_as_of(today - timedelta(days=2), long_ago)  # past and final
        await self.fetch_day_as_of(today - timedelta(days=1), long_ago)  # yesterday: stale
        await self.fetch_day_as_of(today, long_ago)  # stale
        await self.fetch_day_as_of(today + timedelta(days=1), recently)  # within the TTL
        # today + 2 was never fetched

        start, end = today - timedelta(days=3), today + timedelta(days=2)
        await self.service.suggestions(start, end, per_day=3)

        fetched = {key.removeprefix("/schedule?") for key in self.server.requests}
        expected = {today - timedelta(days=1), today, today + timedelta(days=2)}
        self.assertEqual(fetched, {day.isoformat() for day in expected})
        self.assertEqual(sum(self.server.requests.values()), 3)

    async def test_failing_day_is_skipped_and_retried_later(self) -> None:
        today = date.today()
        days = [today + timedelta(days=n) for n in range(3)]
        self.serve_schedule(*days)
        failing = f"/schedule?{days[1].isoformat()}"
        self.server.failures[failing] = 500

        with self.assertLogs(LOGGER, "WARNING"):
            entries = await self.service.suggestions(days[0], days[-1], per_day=3)
        self.assertEqual([e.episode.air_date for e in entries], [days[0], days[2]])
        fetched = await self.db.library.schedule_fetches(days[0], days[-1])
        self.assertEqual(set(fetched), {days[0], days[2]})

        del self.server.failures[failing]
        self.server.requests.clear()
        entries = await self.service.suggestions(days[0], days[-1], per_day=3)
        self.assertEqual(list(self.server.requests), [failing])
        self.assertEqual([e.episode.air_date for e in entries], days)

    async def test_other_errors_propagate(self) -> None:
        today = date.today()
        broken = RuntimeError("db")
        with mock.patch.object(self.db.library, "save_schedule_day", side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, "db"):
                await self.service.suggestions(today, today, per_day=3)

    async def test_concurrent_calls_share_in_flight_fetches(self) -> None:
        today = date.today()
        days = [today, today + timedelta(days=1)]
        self.serve_schedule(*days)
        gate = self.server.gates["/schedule"] = asyncio.Event()

        first = asyncio.create_task(self.service.suggestions(days[0], days[1], per_day=3))
        await until(lambda: self.server.in_flight == 2)
        second = asyncio.create_task(self.service.suggestions(days[0], days[1], per_day=3))
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()

        a, b = await asyncio.gather(first, second)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 2)
        self.assertEqual(sum(self.server.requests.values()), 2)

    async def test_cancellation_cancels_in_flight_fetches(self) -> None:
        today = date.today()
        self.server.gates["/schedule"] = asyncio.Event()
        tomorrow = today + timedelta(days=1)
        task = asyncio.create_task(self.service.suggestions(today, tomorrow, per_day=3))
        await until(lambda: self.server.in_flight == 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.server.cancelled, 2)
        self.assertEqual(await self.db.library.schedule_fetches(today, tomorrow), {})

    async def test_fetch_survives_when_only_one_waiter_is_cancelled(self) -> None:
        today = date.today()
        self.serve_schedule(today)
        gate = self.server.gates["/schedule"] = asyncio.Event()
        first = asyncio.create_task(self.service.suggestions(today, today, per_day=3))
        await until(lambda: self.server.in_flight == 1)
        second = asyncio.create_task(self.service.suggestions(today, today, per_day=3))
        for _ in range(10):
            await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        gate.set()
        self.assertEqual(len(await second), 1)
        self.assertEqual(self.server.cancelled, 0)
        self.assertEqual(sum(self.server.requests.values()), 1)

    async def test_schedule_never_overwrites_tracked_shows(self) -> None:
        await self.service.track(DUNE)
        today = date.today()
        self.server.schedules[today] = [
            schedule_item(1, DUNE, today, updated=DUNE_UPDATED + 999),
            schedule_item(2, 600, today),
        ]
        await self.service.suggestions(today, today, per_day=3)
        stored = await self.db.library.get_show(DUNE)
        assert stored is not None
        self.assertEqual(stored.updated, DUNE_UPDATED)
        self.assertIsNotNone(await self.db.library.get_show(600))


class SyncTests(LibraryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.service.track(DUNE)
        await self.service.track(DRAGON)
        self.server.requests.clear()
        self.notifications = 0

    async def test_only_shows_with_newer_update_stamps_are_refetched(self) -> None:
        self.server.updates = {str(DUNE): DUNE_UPDATED + 100, str(DRAGON): DRAGON_UPDATED, "1": 5}
        before = datetime.now(UTC)
        progress: list[SyncProgress] = []

        report = await self.service.sync(progress.append)

        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 1)
        self.assertEqual(self.server.requests[f"/shows/{DRAGON}"], 0)
        self.assertEqual(self.server.requests["/updates/shows"], 1)
        self.assertEqual((report.checked, report.refreshed, report.failed), (2, 1, 0))
        self.assertLessEqual(before, report.started_at)
        self.assertLessEqual(report.started_at, report.finished_at)
        self.assertEqual(progress, [SyncProgress(done=1, total=1, current="Dune: Prophecy")])
        synced = await self.synced_at()
        self.assertTrue(all(at is not None and at >= before for at in synced.values()))
        self.assertEqual(self.notifications, 1)
        self.assertEqual(len(await self.db.library.episodes(DUNE)), 6)

    async def test_unchanged_library_refetches_nothing(self) -> None:
        report = await self.service.sync()
        self.assertEqual((report.checked, report.refreshed, report.failed), (2, 0, 0))
        self.assertEqual(sum(self.server.requests.values()), 1)  # just /updates/shows
        self.assertEqual(self.notifications, 1)

    async def test_never_or_long_ago_synced_shows_are_refetched(self) -> None:
        await self.store_tracked(show_payload(1), synced=False)
        month_ago = datetime.now(UTC) - timedelta(days=29)
        with mock.patch("app.database._now", return_value=month_ago.isoformat()):
            await self.db.library.mark_synced(DRAGON)
        self.server.requests.clear()

        report = await self.service.sync()

        self.assertEqual(report.refreshed, 2)
        self.assertEqual(self.server.requests["/shows/1"], 1)
        self.assertEqual(self.server.requests[f"/shows/{DRAGON}"], 1)
        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 0)

    async def test_force_refreshes_everything(self) -> None:
        report = await self.service.sync(force=True)
        self.assertEqual((report.checked, report.refreshed, report.failed), (2, 2, 0))
        self.assertEqual(self.server.requests["/updates/shows"], 0)

    async def test_unavailable_updates_feed_refreshes_everything(self) -> None:
        self.server.failures["/updates/shows"] = 500
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.sync()
        self.assertEqual(report.refreshed, 2)

    async def test_missing_show_counts_as_failed_without_aborting(self) -> None:
        await self.store_tracked(show_payload(77777), synced=False)
        del self.server.shows[77777]
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.sync(force=True)
        self.assertEqual((report.checked, report.refreshed, report.failed), (3, 2, 1))
        self.assertIsNone((await self.synced_at())[77777])
        self.assertEqual(self.notifications, 1)

    async def test_refreshes_at_most_three_shows_at_once(self) -> None:
        for show_id in range(1, 6):
            await self.store_tracked(show_payload(show_id))
        self.server.delay = 0.02
        self.server.max_in_flight = 0
        progress: list[SyncProgress] = []
        report = await self.service.sync(progress.append, force=True)
        self.assertEqual(report.refreshed, 7)
        self.assertEqual(self.server.max_in_flight, 3)
        self.assertEqual([p.done for p in progress], list(range(1, 8)))
        self.assertTrue(all(p.total == 7 for p in progress))

    async def test_concurrent_calls_share_one_run(self) -> None:
        gate = self.server.gates["/updates/shows"] = asyncio.Event()
        first = asyncio.create_task(self.service.sync())
        await until(lambda: self.server.in_flight == 1)
        second = asyncio.create_task(self.service.sync(force=True))
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()
        a, b = await asyncio.gather(first, second)
        self.assertIs(a, b)
        self.assertEqual(self.server.requests["/updates/shows"], 1)
        self.assertEqual(self.notifications, 1)

        # Once that run is over, the next call starts a new one.
        third = await self.service.sync()
        self.assertIsNot(third, a)
        self.assertEqual(self.server.requests["/updates/shows"], 2)

    async def test_cancelling_the_only_caller_cancels_the_run(self) -> None:
        self.server.gates["/updates/shows"] = asyncio.Event()
        task = asyncio.create_task(self.service.sync())
        await until(lambda: self.server.in_flight == 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.server.cancelled, 1)
        self.assertEqual(self.notifications, 0)

    async def test_sync_prunes_old_untracked_schedule_data(self) -> None:
        old = date.today() - timedelta(days=90)
        self.server.schedules[old] = [schedule_item(1, 600, old)]
        await self.service.suggestions(old, old, per_day=3)
        self.assertIsNotNone(await self.db.library.get_show(600))
        await self.service.sync()
        self.assertIsNone(await self.db.library.get_show(600))
        self.assertEqual(await self.db.library.schedule_fetches(old, old), {})
        self.assertEqual(len(await self.db.library.episodes(DUNE)), 6)  # aired in 2024


class PeriodicSyncTests(LibraryTestCase):
    async def test_keeps_looping_after_a_failed_run(self) -> None:
        calls = 0
        now = datetime.now(UTC)
        report = SyncReport(checked=0, refreshed=0, failed=0, started_at=now, finished_at=now)

        async def flaky_sync(progress: Callable[[SyncProgress], None] | None = None) -> SyncReport:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TVMazeError("TVMaze down")
            if calls == 2:
                raise RuntimeError("unexpected")
            return report

        with mock.patch.object(self.service, "sync", side_effect=flaky_sync):
            with self.assertLogs(LOGGER, "ERROR") as logs:
                task = asyncio.create_task(
                    self.service.run_periodic_sync(interval=timedelta(0), initial_delay=0)
                )
                await until(lambda: calls >= 4)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(logs.records), 2)

    async def test_runs_real_syncs(self) -> None:
        await self.service.track(DUNE)
        progress: list[SyncProgress] = []
        task = asyncio.create_task(
            self.service.run_periodic_sync(
                interval=timedelta(milliseconds=1), initial_delay=0, progress=progress.append
            )
        )
        await until(lambda: self.server.requests["/updates/shows"] >= 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class PruneScheduleTests(LibraryTestCase):
    async def test_never_deletes_tracked_shows_episodes(self) -> None:
        await self.service.track(DUNE)  # every episode aired in 2024
        today = date.today()
        old, recent = today - timedelta(days=90), today - timedelta(days=5)
        self.server.schedules[old] = [
            schedule_item(1, 600, old),  # untracked, only old episodes
            schedule_item(2, 601, old),  # untracked, also airs recently
            schedule_item(3, DUNE, old),  # a tracked show's episode
        ]
        self.server.schedules[recent] = [schedule_item(4, 601, recent)]
        await self.service.suggestions(old, old, per_day=3)
        await self.service.suggestions(recent, recent, per_day=3)

        deleted = await self.db.library.prune_schedule(today - timedelta(days=60))

        self.assertEqual(deleted, 2)
        self.assertIsNone(await self.db.library.get_show(600))
        self.assertEqual([e.id for e in await self.db.library.episodes(601)], [4])
        self.assertEqual(len(await self.db.library.episodes(DUNE)), 7)
        self.assertEqual(await self.db.library.schedule_fetches(old, old), {})
        self.assertEqual(set(await self.db.library.schedule_fetches(recent, recent)), {recent})
        self.assertEqual([e.show.id for e in await self.service.tracked()], [DUNE])

    async def test_mark_synced_many(self) -> None:
        await self.store_tracked(show_payload(1), synced=False)
        await self.store_tracked(show_payload(2), synced=False)
        await self.store_tracked(show_payload(3), synced=False)
        await self.db.library.mark_synced_many([1, 3])
        await self.db.library.mark_synced_many([])
        synced = await self.synced_at()
        self.assertIsNotNone(synced[1])
        self.assertIsNone(synced[2])
        self.assertIsNotNone(synced[3])


SHOW_DETAILS_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)  # House of the Dragon S03E03 aired
DRAGON_S1 = {2304397, 2307790, 2307791, 2304477, 2304476, 2304475, 2304474, 2304473, 2304472, 2304470}
DRAGON_S3E2, DRAGON_S3E4 = 3605745, 3605747


class ShowDetailsTests(LibraryTestCase):
    """LibraryService.show_details: tracked shows from the library, others from TVMaze."""

    async def grab(self, show_id: int, season: int, number: int | None) -> None:
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=show_id,
                season=season,
                episode=number,
                release_title=f"Show {show_id} S{season}E{number}",
            )
        )

    async def test_tracked_show_comes_from_the_library(self) -> None:
        await self.service.track(DRAGON)
        await self.service.set_auto_download(DRAGON, True)
        await self.grab(DRAGON, 1, None)  # a season pack
        await self.grab(DRAGON, 3, 2)
        await self.grab(DUNE, 1, 1)  # another show's

        details = await self.service.show_details(DRAGON, now=SHOW_DETAILS_NOW)

        self.assertEqual(self.server.requests[f"/shows/{DRAGON}"], 1)  # only the track
        self.assertEqual(details.show.name, "House of the Dragon")
        self.assertTrue(details.tracked)
        self.assertTrue(details.auto_download)
        self.assertEqual(len(details.episodes), 26)
        self.assertEqual(
            [(e.season, e.number) for e in details.episodes],
            sorted((e.season, e.number or 0) for e in details.episodes),
        )
        self.assertEqual(details.downloaded, frozenset(DRAGON_S1 | {DRAGON_S3E2}))
        assert details.next_episode is not None
        self.assertEqual(details.next_episode.id, DRAGON_S3E4)

    async def test_untracked_show_is_fetched_and_not_stored(self) -> None:
        await self.grab(DUNE, 1, 2)  # downloaded from its details window, say

        details = await self.service.show_details(DUNE, now=SHOW_DETAILS_NOW)

        self.assertFalse(details.tracked)
        self.assertFalse(details.auto_download)
        self.assertEqual(details.show.name, "Dune: Prophecy")
        self.assertEqual([e.number for e in details.episodes], [1, 2, 3, 4, 5, 6])
        self.assertEqual(details.downloaded, frozenset({3031113}))
        self.assertIsNone(details.next_episode)  # every episode aired in 2024
        self.assertIsNone(await self.db.library.get_show(DUNE))
        self.assertEqual(await self.db.library.episodes(DUNE), [])
        self.assertEqual(await self.service.tracked(), [])
        self.assertEqual(self.notifications, 0)

    async def test_untracked_show_is_kept_in_memory_until_tracked(self) -> None:
        await self.service.show_details(DUNE)
        await self.service.show_details(DUNE)
        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 1)
        await self.service.track(DUNE)
        await self.service.untrack(DUNE)
        await self.service.show_details(DUNE)  # the copy from before tracking is dropped
        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 3)

    async def test_tracked_show_without_cached_episodes_is_fetched(self) -> None:
        await self.store_tracked(show_payload(DUNE, "Dune"), synced=False)  # no episodes stored
        self.server.shows[DUNE] = load("show_embed.json")
        details = await self.service.show_details(DUNE)
        self.assertTrue(details.tracked)
        self.assertEqual(len(details.episodes), 6)
        self.assertEqual(self.server.requests[f"/shows/{DUNE}"], 1)
        self.assertEqual(await self.db.library.episodes(DUNE), [])  # still nothing written

    async def test_tvmaze_errors_propagate(self) -> None:
        with self.assertRaises(TVMazeNotFound):
            await self.service.show_details(12345)
        self.server.failures[f"/shows/{DUNE}"] = 500
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(TVMazeError):
            await self.service.show_details(DUNE)


def numbered(
    episode_id: int,
    season: int,
    number: int | None,
    *,
    airs: datetime | None = None,
    air_date: date | None = None,
) -> Episode:
    return Episode(
        id=episode_id,
        show_id=1,
        season=season,
        number=number,
        airstamp=airs,
        air_date=air_date if air_date is not None else (airs.date() if airs else None),
    )


class NextEpisodeTests(unittest.TestCase):
    NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)

    def test_first_numbered_regular_episode_still_to_air(self) -> None:
        episodes = [
            numbered(1, 2, 3, airs=self.NOW - timedelta(days=7)),
            numbered(2, 2, 4, airs=self.NOW + timedelta(hours=1)),
            numbered(3, 2, 5, airs=self.NOW + timedelta(days=7)),
            numbered(4, 0, 1, airs=self.NOW + timedelta(minutes=5)),  # a special
            numbered(5, 2, None, airs=self.NOW + timedelta(minutes=5)),  # unnumbered
        ]
        upcoming = next_episode(reversed(episodes), self.NOW)
        assert upcoming is not None
        self.assertEqual(upcoming.id, 2)

    def test_without_an_airstamp_its_air_date_counts_from_today(self) -> None:
        today = self.NOW.date()
        episodes = [
            numbered(1, 1, 1, air_date=today - timedelta(days=1)),
            numbered(2, 1, 2, air_date=today),
            numbered(3, 1, 3),  # no date at all
        ]
        upcoming = next_episode(episodes, self.NOW)
        assert upcoming is not None
        self.assertEqual(upcoming.id, 2)

    def test_none_when_nothing_is_upcoming(self) -> None:
        self.assertIsNone(next_episode([numbered(1, 1, 1, airs=self.NOW)], self.NOW))
        self.assertIsNone(next_episode([], self.NOW))


class DownloadedIdsTests(unittest.TestCase):
    def test_episodes_and_season_packs(self) -> None:
        episodes = [
            numbered(11, 1, 1),
            numbered(12, 1, None),  # unnumbered: only its season's pack covers it
            numbered(21, 2, 1),
            numbered(22, 2, 2),
        ]
        self.assertEqual(downloaded_ids(episodes, {(1, None), (2, 2)}), {11, 12, 22})
        self.assertEqual(downloaded_ids(episodes, {(2, 1), (3, None)}), {21})
        self.assertEqual(downloaded_ids(episodes, set()), frozenset())


class ServicesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.paths = AppPaths(Path(self.tmp.name))
        # A real httpx client loads its CA bundle synchronously (~0.2 s): not worth a debug warning.
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_create_and_aclose(self) -> None:
        network = AssertionError("no network calls during create")
        with mock.patch.object(httpx.AsyncClient, "send", side_effect=network):
            services = await Services.create(self.paths)
        try:
            self.assertIsInstance(services.settings.current, Settings)
            self.assertEqual((await services.library.stats()).tracked_shows, 0)
            self.assertIn("AirGrabber/", services._http.headers["User-Agent"])
        finally:
            await services.aclose()
        self.assertTrue(services._http.is_closed)
        with self.assertRaises(ValueError):  # the aiosqlite connection is closed
            await services.library.stats()

    async def test_failed_create_closes_what_it_opened(self) -> None:
        real_close = Database.close
        with (
            mock.patch.object(SettingsService, "load", side_effect=RuntimeError("boom")),
            mock.patch.object(Database, "close", autospec=True, side_effect=real_close) as close,
            mock.patch.object(httpx.AsyncClient, "aclose", autospec=True) as aclose,
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await Services.create(self.paths)
        close.assert_awaited_once()
        aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
