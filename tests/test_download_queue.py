"""The download queue (app/services/download_queue.py, 3.0.1): the reproduction of "some
downloads seem to get cancelled along the way" (ten one-click downloads at once starve each
other on a source's two request slots; through the queue every one is found), order, at most
WORKERS at a time, no title queued twice, ✕ (remove), a batch ending at a failed grab, a
season pack and its fallback, a film's request made at its turn, shutdown, and log hygiene.
Fakes and httpx.MockTransport only: no network."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable

import httpx

from app.indexers import EztvIndexer
from app.models import (
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    QueueEvent,
    QueueKey,
    QueueState,
    QuickDownloadResult,
    SearchKind,
    SearchRequest,
    Show,
)
from app.services.download_queue import WORKERS, DownloadQueue, QueuedDownload, target_key
from app.services.downloads import GrabError
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError
from app.services.search import TorrentSearchService, episode_request
from tests.test_quick_download import downloads
from tests.test_search import settings_service

LOGGER = "app.services.download_queue"
GOLD_RUSH = Show(id=7, name="Gold Rush", imdb_id="tt1800864")
SENT = GrabResult(outcome=GrabOutcome.SENT, message="Sent to qBittorrent")
GRABBED = QuickDownloadResult(found=True, quality="1080p", seeders=40, grab=SENT)
NOTHING = QuickDownloadResult(found=False)


def target(season: int, episode: int | None) -> GrabTarget:
    return GrabTarget(
        media_type=MediaType.TV, media_id=GOLD_RUSH.id, season=season, episode=episode
    )


def key(season: int, episode: int | None) -> QueueKey:
    return (MediaType.TV, GOLD_RUSH.id, season, episode)


def item(episode: int, *, season: int = 17, again: bool = False) -> QueuedDownload:
    request = episode_request(GOLD_RUSH, season, episode)
    label = f"Gold Rush S{season:02d}E{episode:02d}"
    return QueuedDownload(label=label, target=target(season, episode), request=request, again=again)


def pack(season: int = 17, *, fallback: tuple[QueuedDownload, ...] = ()) -> QueuedDownload:
    request = SearchRequest(
        kind=SearchKind.SEASON, title="Gold Rush", season=season, queries=("Gold Rush S17",)
    )
    return QueuedDownload(
        label=f"Gold Rush S{season:02d}", target=target(season, None), request=request,
        fallback=fallback,
    )


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeQuick:
    """A QuickDownloadService stand-in: each target's run waits for its gate (when it has
    one) and returns its result (GRABBED by default) or raises its error."""

    def __init__(self) -> None:
        self.gates: dict[QueueKey, asyncio.Event] = {}
        self.results: dict[QueueKey, QuickDownloadResult] = {}
        self.errors: dict[QueueKey, BaseException] = {}
        self.started: list[QueueKey] = []
        self.requests: list[SearchRequest] = []
        self.again: list[bool] = []
        self.running = 0
        self.most = 0

    def gate(self, *keys: QueueKey) -> None:
        for one in keys:
            self.gates[one] = asyncio.Event()

    def open(self, one: QueueKey) -> None:
        self.gates[one].set()

    async def download(
        self, request: SearchRequest, grab_target: GrabTarget, progress: object = None, *,
        again: bool = False,
        avoid: object = (),  # a stuck download's swap
    ) -> QuickDownloadResult:
        one = target_key(grab_target)
        self.started.append(one)
        self.requests.append(request)
        self.again.append(again)
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            gate = self.gates.get(one)
            if gate is not None:
                await gate.wait()
            else:
                await asyncio.sleep(0)
            if one in self.errors:
                raise self.errors[one]
            return self.results.get(one, GRABBED)
        finally:
            self.running -= 1


class QueueTestCase(unittest.IsolatedAsyncioTestCase):
    def queue(self, quick: FakeQuick | None = None, **options: int) -> DownloadQueue:
        self.quick = quick or FakeQuick()
        queue = DownloadQueue(self.quick, **options)  # type: ignore[arg-type]
        self.events: list[QueueEvent] = []
        queue.subscribe(self.events.append)
        self.addCleanup(queue.close)
        return queue

    def states(self, one: QueueKey) -> list[QueueState]:
        return [event.state for event in self.events if event.key == one]

    async def until(self, condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                await asyncio.sleep(0.005)


# -- the reproduction --------------------------------------------------------------------------

LATENCY = 0.06  # each EZTV request (scaled down with the time limits below)
PAGES = 5  # EZTV reads up to five pages of a show's torrents per search


def eztv_page(number: int) -> list[dict[str, object]]:
    """A page of a long-running show's torrents (100 per page), each episode of season 17."""
    rows: list[dict[str, object]] = []
    for index in range(100):
        n = (number - 1) * 100 + index
        episode = n % 30 + 1
        rows.append(
            {
                "title": f"Gold Rush S17E{episode:02d} 1080p WEB h264-GRP{n}",
                "hash": f"{n + 1:040x}",
                "seeds": 50,
                "peers": 5,
                "size_bytes": 1500 * 1024**2,
                "season": "17",
                "episode": str(episode),
                "magnet_url": f"magnet:?xt=urn:btih:{n + 1:040x}",
            }
        )
    return rows


async def eztv(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(LATENCY)
    number = int(request.url.params["page"])
    return httpx.Response(200, json={"torrents": eztv_page(number) if number <= PAGES else None})


class ManyClicksTests(QueueTestCase):
    """3.0.1: "When clicking multiple download buttons, some downloads seem to get cancelled
    along the way." Every click started its own one-click download at once; each searches
    every source, and a source takes two requests at a time (EZTV: up to five pages per
    search), so ten at once wait for each other's requests until the time limits (25 s per
    source, 30 s in all; scaled here to 1 s and 1.2 s) run out, and the later ones come back
    with nothing: "No good release found", although the releases are there."""

    async def asyncSetUp(self) -> None:
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(eztv))
        self.addAsyncCleanup(self.http.aclose)
        prefs = settings_service()
        search = TorrentSearchService([EztvIndexer(self.http)], prefs, timeout=1.0)
        self.grabber = downloads()
        self.service = QuickDownloadService(search, self.grabber, prefs, timeout=1.2)

    async def test_ten_clicks_at_once_starve_each_other(self) -> None:
        """The cause: the same ten downloads started together, as the buttons did."""
        with self.assertLogs("app.services.search", "WARNING"):
            results = await asyncio.gather(
                *(
                    self.service.download(episode_request(GOLD_RUSH, 17, n), target(17, n))
                    for n in range(1, 11)
                )
            )
        self.assertLess(sum(result.found for result in results), 10)

    async def test_ten_clicks_through_the_queue_all_download(self) -> None:
        """The fix: the same ten clicks queued; at most two search at a time, all found."""
        queue = DownloadQueue(self.service)
        self.addCleanup(queue.close)
        done: list[QueueEvent] = []
        queue.subscribe(lambda event: done.append(event) if not event.state.active else None)
        for n in range(1, 11):
            self.assertTrue(queue.add(item(n)))
        await self.until(lambda: len(done) == 10, timeout_s=15)
        self.assertEqual([event.state for event in done], [QueueState.DONE] * 10)
        self.assertEqual(self.grabber.grab.await_count, 10)
        grabbed = [call.args[1].episode for call in self.grabber.grab.await_args_list]
        self.assertEqual(sorted(grabbed), list(range(1, 11)))


# -- the queue ---------------------------------------------------------------------------------


class QueueTests(QueueTestCase):
    async def test_runs_in_order_at_most_two_at_a_time(self) -> None:
        queue = self.queue()
        keys = [key(17, n) for n in range(1, 6)]
        self.quick.gate(*keys)
        for n in range(1, 6):
            queue.add(item(n))
        await settle()
        self.assertEqual(WORKERS, 2)
        self.assertEqual(self.quick.started, keys[:2])
        self.assertEqual([one.key for one in queue.running], keys[:2])
        self.assertEqual([one.key for one in queue.waiting], keys[2:])
        labels = [f"Gold Rush S17E0{n}" for n in (3, 4, 5)]
        self.assertEqual([one.label for one in queue.waiting], labels)
        self.quick.open(keys[1])
        await settle()
        self.assertEqual(self.quick.started, [keys[0], keys[1], keys[2]])
        for one in keys:
            self.quick.open(one)
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(self.quick.started, keys)
        self.assertEqual(self.quick.most, 2)
        self.assertEqual(
            self.states(keys[4]), [QueueState.WAITING, QueueState.RUNNING, QueueState.DONE]
        )
        last = queue.event(keys[4])
        assert last is not None
        self.assertEqual(last.result, GRABBED)
        self.assertEqual(last.label, "Gold Rush S17E05")

    async def test_one_at_a_time_when_asked(self) -> None:
        queue = self.queue(workers=1)
        for n in range(1, 4):
            queue.add(item(n))
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(self.quick.most, 1)

    async def test_the_same_title_is_not_queued_twice(self) -> None:
        queue = self.queue()
        self.quick.gate(key(17, 1), key(17, 2), key(17, 3))
        self.assertTrue(queue.add(item(1)))
        self.assertTrue(queue.add(item(2)))
        self.assertTrue(queue.add(item(3)))
        await settle()
        self.assertFalse(queue.add(item(1)))  # running
        self.assertFalse(queue.add(item(3)))  # waiting
        self.assertFalse(queue.add(item(3, again=True)))
        self.assertEqual(len(queue.waiting), 1)
        self.assertEqual(queue.add_batch([item(2), item(3), item(4)]), (key(17, 4),))
        for one in (key(17, 1), key(17, 2), key(17, 3)):
            self.quick.open(one)
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(self.quick.started.count(key(17, 1)), 1)
        # Finished: it may be asked for again (↻ Again → Auto).
        self.assertTrue(queue.add(item(1, again=True)))
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(self.quick.started.count(key(17, 1)), 2)
        self.assertEqual(self.quick.again[-1], True)

    async def test_remove_takes_a_waiting_download_out(self) -> None:
        queue = self.queue(workers=1)
        self.quick.gate(key(17, 1))
        queue.add(item(1))
        queue.add(item(2))
        queue.add(item(3))
        await settle()
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertTrue(queue.remove(key(17, 2)))
        self.assertIn("Gold Rush S17E02 taken out", "\n".join(logs.output))
        self.assertFalse(queue.remove(key(17, 1)))  # running: stays
        self.assertFalse(queue.remove(key(17, 9)))  # never queued
        self.assertEqual([one.key for one in queue.waiting], [key(17, 3)])
        self.assertEqual(self.states(key(17, 2)), [QueueState.WAITING, QueueState.DROPPED])
        self.quick.open(key(17, 1))
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertNotIn(key(17, 2), self.quick.started)

    async def test_nothing_found_and_already_downloaded(self) -> None:
        queue = self.queue()
        self.quick.results[key(17, 1)] = NOTHING
        self.quick.results[key(17, 2)] = QuickDownloadResult(found=False, already_downloaded=True)
        queue.add(item(1))
        queue.add(item(2))
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(queue.state(key(17, 1)), QueueState.NOTHING)
        self.assertEqual(queue.state(key(17, 2)), QueueState.DONE)
        self.assertIsNone(queue.state(key(17, 3)))

    async def test_a_failed_grab_ends_its_batch_only(self) -> None:
        queue = self.queue(workers=1)
        self.quick.gate(key(17, 1))
        self.quick.errors[key(17, 1)] = GrabError("No torrent program is set up on this PC.")
        queue.add_batch([item(1), item(2), item(3)])
        queue.add(item(9))  # asked for on its own
        await settle()
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.quick.open(key(17, 1))
            await self.until(lambda: not queue.running and not queue.waiting)
        failed = queue.event(key(17, 1))
        assert failed is not None
        self.assertEqual(failed.state, QueueState.FAILED)
        self.assertEqual(failed.message, "No torrent program is set up on this PC.")
        self.assertEqual(queue.state(key(17, 2)), QueueState.DROPPED)
        self.assertEqual(queue.state(key(17, 3)), QueueState.DROPPED)
        self.assertEqual(queue.state(key(17, 9)), QueueState.DONE)
        self.assertEqual(self.quick.started, [key(17, 1), key(17, 9)])
        self.assertIn("dropped the 2 other waiting download(s)", "\n".join(logs.output))

    async def test_a_remote_error_fails_without_ending_the_others(self) -> None:
        queue = self.queue()
        self.quick.errors[key(17, 1)] = RemoteError("qBittorrent didn't add the torrent.")
        with self.assertLogs(LOGGER, "WARNING"):
            queue.add(item(1))
            queue.add(item(2))
            await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(queue.state(key(17, 1)), QueueState.FAILED)
        self.assertEqual(queue.state(key(17, 2)), QueueState.DONE)

    async def test_a_films_request_is_made_when_its_turn_comes(self) -> None:
        queue = self.queue(workers=1)
        self.quick.gate(key(17, 1))
        made: list[str] = []
        film = SearchRequest(
            kind=SearchKind.MOVIE, title="Dune", year=2021, queries=("Dune 2021",)
        )

        async def request() -> SearchRequest:
            made.append("now")
            return film

        queue.add(item(1))
        dune = GrabTarget(media_type=MediaType.MOVIE, media_id=438631)
        dune_item = QueuedDownload(label="Dune (2021)", target=dune, request=request)
        self.assertTrue(queue.add(dune_item))
        await settle()
        self.assertEqual(made, [])  # waiting
        self.quick.open(key(17, 1))
        await self.until(lambda: not queue.running and not queue.waiting)
        self.assertEqual(made, ["now"])
        self.assertEqual(self.quick.requests[-1], film)
        self.assertEqual(queue.state((MediaType.MOVIE, 438631, None, None)), QueueState.DONE)

    async def test_a_listener_failing_doesnt_stop_the_queue(self) -> None:
        queue = self.queue()

        def broken(_event: QueueEvent) -> None:
            raise RuntimeError("listener bug")

        unsubscribe = queue.subscribe(broken)
        with self.assertLogs(LOGGER, "ERROR"):
            queue.add(item(1))
            await self.until(lambda: queue.state(key(17, 1)) is QueueState.DONE)
        unsubscribe()
        unsubscribe()  # twice is fine

    async def test_close_drops_whats_waiting_and_says_how_many(self) -> None:
        queue = self.queue(workers=1)
        self.quick.gate(key(17, 1))
        for n in range(1, 5):
            queue.add(item(n))
        await settle()
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertEqual(queue.close(), 3)
            await settle()
        text = "\n".join(logs.output)
        self.assertIn("Download queue closed: 3 waiting download(s) dropped", text)
        self.assertEqual(queue.waiting, ())
        self.assertFalse(queue.add(item(8)))  # closed
        self.assertEqual(self.quick.started, [key(17, 1)])  # the running one was stopped


class SeasonPackTests(QueueTestCase):
    async def test_a_pack_found_is_the_whole_season(self) -> None:
        queue = self.queue()
        queue.add(pack(fallback=(item(3), item(4))))
        await self.until(lambda: queue.state(key(17, None)) is QueueState.DONE)
        self.assertEqual(self.quick.started, [key(17, None)])
        self.assertEqual(self.quick.requests[0].kind, SearchKind.SEASON)
        event = queue.event(key(17, None))
        assert event is not None
        self.assertEqual(event.fallback, ())

    async def test_no_pack_queues_the_episodes_still_needed(self) -> None:
        queue = self.queue()
        self.quick.results[key(17, None)] = NOTHING
        with self.assertLogs(LOGGER, "INFO") as logs:
            queue.add(pack(fallback=(item(3), item(4))))
            await self.until(
                lambda: queue.state(key(17, 4)) is QueueState.DONE
                and queue.state(key(17, 3)) is QueueState.DONE
            )
        event = queue.event(key(17, None))
        assert event is not None
        self.assertEqual(event.state, QueueState.NOTHING)
        self.assertEqual(event.fallback, (key(17, 3), key(17, 4)))
        self.assertEqual(self.quick.started, [key(17, None), key(17, 3), key(17, 4)])
        # The episodes were queued before the pack's NOTHING was told.
        order = [(e.key, e.state) for e in self.events]
        self.assertLess(
            order.index((key(17, 3), QueueState.WAITING)),
            order.index((key(17, None), QueueState.NOTHING)),
        )
        self.assertIn("no season pack found; queuing its 2 episode(s)", "\n".join(logs.output))

    async def test_no_pack_and_nothing_needed(self) -> None:
        queue = self.queue()
        self.quick.results[key(17, None)] = NOTHING
        queue.add(pack())
        await self.until(lambda: queue.state(key(17, None)) is QueueState.NOTHING)
        event = queue.event(key(17, None))
        assert event is not None
        self.assertEqual(event.fallback, ())


class LogHygieneTests(QueueTestCase):
    async def test_no_urls_keys_or_release_titles(self) -> None:
        secret_url = "https://prowlarr.example/download?apikey=SECRET"
        queue = self.queue(workers=1)
        self.quick.results[key(17, 1)] = QuickDownloadResult(
            found=True, release_title="Gold.Rush.S17E01.1080p.WEB-SECRETGRP", grab=SENT
        )
        self.quick.errors[key(17, 2)] = GrabError("No torrent program is set up on this PC.")
        self.quick.errors[key(17, 3)] = ValueError(f"bad input {secret_url}")
        with self.assertLogs(LOGGER, "INFO") as logs:
            queue.add(item(1))
            queue.add(item(2))
            queue.add(item(3))
            await self.until(lambda: not queue.running and not queue.waiting)
        text = "\n".join(logs.output)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("http", text)
        self.assertIn("Gold Rush S17E03 failed unexpectedly (ValueError)", text)
        failed = queue.event(key(17, 3))
        assert failed is not None
        self.assertEqual(failed.message, "Something went wrong with Gold Rush S17E03 (ValueError)")
        for event in self.events:
            self.assertNotIn("SECRET", event.message)
            self.assertNotIn("http", event.label + event.message)


if __name__ == "__main__":
    unittest.main()
