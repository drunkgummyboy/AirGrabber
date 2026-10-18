"""QuickDownloadService: search to completion (30 s cap), pick, grab; nothing found; failures."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from unittest import mock

from app.models import (
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    QuickDownloadResult,
    SearchKind,
    SearchRequest,
    TorrentRelease,
)
from app.services.downloads import DownloadService, GrabError
from app.services.quick_download import QuickDownloadService, request_label
from app.services.search import TorrentSearchService
from tests.test_search import MIB, fake, release, running_searches, settings_service

LOGGER = "app.services.quick_download"
EPISODE = SearchRequest(
    kind=SearchKind.EPISODE,
    title="Severance",
    year=2022,
    season=2,
    episode=1,
    queries=("Severance S02E01",),
)
TARGET = GrabTarget(media_type=MediaType.TV, media_id=44933, season=2, episode=1)
SAVED = GrabResult(outcome=GrabOutcome.SAVED, path="C:/watch/x.torrent", message="Saved x.torrent")
LINK = "https://prowlarr.example/download?apikey=SECRET"


def downloads(result: GrabResult = SAVED) -> mock.Mock:
    """A DownloadService whose history starts empty and records what `grab` grabbed."""
    service = mock.Mock(spec=DownloadService)
    grabbed: set[GrabTarget] = set()

    async def grab(release: TorrentRelease, target: GrabTarget) -> GrabResult:
        grabbed.add(target)
        return result

    async def has_grabbed(target: GrabTarget) -> bool:
        return target in grabbed

    service.grab = mock.AsyncMock(side_effect=grab)
    service.has_grabbed = mock.AsyncMock(side_effect=has_grabbed)
    return service


class QuickDownloadTests(unittest.IsolatedAsyncioTestCase):
    def service(
        self,
        indexers: list[object],
        grabber: mock.Mock,
        *,
        timeout: float = 30.0,
        **settings: object,
    ) -> QuickDownloadService:
        prefs = settings_service(**settings)
        search = TorrentSearchService(indexers, prefs)  # type: ignore[arg-type]
        return QuickDownloadService(search, grabber, prefs, timeout=timeout)

    async def test_picks_the_best_release_and_grabs_it(self) -> None:
        small = release("Severance S02E01 1080p WEB-A", h=1, seeders=900, size=10 * MIB)
        hd = release("Severance S02E01 720p WEB-B", h=2, seeders=500, size=600 * MIB)
        full_hd = release(
            "Severance S02E01 1080p WEB-C", h=3, seeders=40, size=1200 * MIB, download_url=LINK
        )
        grabber = downloads()
        service = self.service(
            [fake("a", result=[small, hd]), fake("b", result=[full_hd], delay=0.05)], grabber
        )
        with self.assertLogs(LOGGER, "INFO") as logs:
            result = await service.download(EPISODE, TARGET)
        self.assertEqual(
            result,
            QuickDownloadResult(
                found=True,
                release_title=full_hd.title,
                quality="1080p",
                seeders=40,
                grab=SAVED,
            ),
        )
        grabber.grab.assert_awaited_once()
        picked, target = grabber.grab.await_args.args
        self.assertEqual(picked.title, full_hd.title)
        self.assertEqual(target, TARGET)
        text = "\n".join(logs.output)
        self.assertIn("Severance S02E01", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn(full_hd.title, text)  # no release objects or titles in the log
        self.assertNotIn("http", text)

    async def test_nothing_found_grabs_nothing(self) -> None:
        fake_release = release("Severance S02E01 1080p WEB.exe", h=1)
        wrong = release("Severance S02E02 1080p WEB", h=2)
        grabber = downloads()
        service = self.service([fake("a", result=[fake_release, wrong])], grabber)
        result = await service.download(EPISODE, TARGET)
        self.assertEqual(result, QuickDownloadResult(found=False))
        grabber.grab.assert_not_awaited()

    async def test_no_sources_at_all(self) -> None:
        grabber = downloads()
        service = self.service([fake("a", kinds=[SearchKind.MOVIE])], grabber)
        self.assertFalse((await service.download(EPISODE, TARGET)).found)
        grabber.grab.assert_not_awaited()

    async def test_stops_waiting_after_the_timeout_and_picks_from_what_arrived(self) -> None:
        arrived = release("Severance S02E01 720p WEB-A", h=1, size=600 * MIB)
        slow = fake("slow", gate=asyncio.Event())  # never answers
        grabber = downloads()
        service = self.service([fake("fast", result=[arrived]), slow], grabber, timeout=0.2)
        with self.assertLogs(LOGGER, "INFO") as logs:
            result = await service.download(EPISODE, TARGET)
        self.assertEqual(result.release_title, arrived.title)
        self.assertTrue(slow.cancelled)
        self.assertEqual(running_searches(), [])
        self.assertTrue(any("stopped waiting" in line for line in logs.output))

    async def test_timeout_with_nothing_arrived(self) -> None:
        grabber = downloads()
        service = self.service([fake("slow", gate=asyncio.Event())], grabber, timeout=0.05)
        self.assertEqual(await service.download(EPISODE, TARGET), QuickDownloadResult(found=False))
        grabber.grab.assert_not_awaited()

    async def test_grab_errors_propagate(self) -> None:
        grabber = downloads()
        grabber.grab.side_effect = GrabError("No torrent program is set up on this PC.")
        hit = release("Severance S02E01 1080p WEB-A", h=1)
        service = self.service([fake("a", result=[hit])], grabber)
        with self.assertRaisesRegex(GrabError, "No torrent program"):
            await service.download(EPISODE, TARGET)

    async def test_uses_the_settings_for_the_pick(self) -> None:
        hd = release("Severance S02E01 720p WEB-A", h=1, size=600 * MIB, seeders=5)
        full_hd = release("Severance S02E01 1080p WEB-B", h=2, size=1200 * MIB, seeders=50)
        grabber = downloads()
        service = self.service([fake("a", result=[full_hd, hd])], grabber, quality="720p")
        result = await service.download(EPISODE, TARGET)
        self.assertEqual(result.release_title, hd.title)
        self.assertEqual(result.quality, "720p")

    async def test_progress(self) -> None:
        hit = release("Severance S02E01 1080p WEB-A", h=1)
        grabber = downloads(GrabResult(outcome=GrabOutcome.OPENED, message="Opened in qBittorrent"))
        service = self.service([fake("a", result=[hit]), fake("b", delay=0.05)], grabber)
        messages: list[str] = []
        result = await service.download(EPISODE, TARGET, progress=messages.append)
        self.assertEqual(result.grab and result.grab.outcome, GrabOutcome.OPENED)
        self.assertEqual(messages[0], "Searching… 0 found, 2 sources still searching")
        self.assertIn("Searching… 1 found, 1 source still searching", messages)
        self.assertEqual(messages[-2:], ["Searching… 1 found", "Grabbing the best one…"])

    async def test_quality_is_none_without_a_resolution(self) -> None:
        hit = TorrentRelease(
            title="Severance S02E01 WEB-A", source="a", seeders=3, size_bytes=0, info_hash="a" * 40
        )
        service = self.service([fake("a", result=[hit])], downloads())
        result = await service.download(EPISODE, TARGET)
        self.assertTrue(result.found)
        self.assertIsNone(result.quality)

    # One download per target at a time.

    async def test_concurrent_downloads_of_one_target_grab_once(self) -> None:
        hit = release("Severance S02E01 1080p WEB-A", h=1)
        indexer = fake("a", result=[hit], delay=0.05)
        grabber = downloads()
        service = self.service([indexer], grabber)
        with self.assertLogs(LOGGER, "INFO") as logs:
            first, second = await asyncio.gather(
                service.download(EPISODE, TARGET), service.download(EPISODE, TARGET)
            )
        self.assertTrue(first.found)
        self.assertEqual(second, QuickDownloadResult(found=False, already_downloaded=True))
        grabber.grab.assert_awaited_once()
        self.assertEqual(len(indexer.requests), 1)  # the second call never searched
        self.assertTrue(any("already downloaded" in line for line in logs.output))
        self.assertEqual(service._target_locks, {})  # dropped once free

    async def test_already_downloaded_target_never_searches(self) -> None:
        indexer = fake("a", result=[release("Severance S02E01 1080p WEB-A", h=1)])
        grabber = downloads()
        grabber.has_grabbed.side_effect = None
        grabber.has_grabbed.return_value = True
        service = self.service([indexer], grabber)
        result = await service.download(EPISODE, TARGET)
        self.assertEqual(result, QuickDownloadResult(found=False, already_downloaded=True))
        self.assertEqual(indexer.requests, [])
        grabber.grab.assert_not_awaited()
        grabber.has_grabbed.assert_awaited_once_with(TARGET)

    async def test_different_targets_download_side_by_side(self) -> None:
        gate = asyncio.Event()
        indexer = fake("a", gate=gate)
        service = self.service([indexer], downloads())
        other = TARGET.model_copy(update={"episode": 2})
        both = asyncio.gather(service.download(EPISODE, TARGET), service.download(EPISODE, other))
        await asyncio.wait_for(_until(lambda: len(indexer.requests) == 2), timeout=5)
        gate.set()
        results = await both
        self.assertEqual([r.found for r in results], [False, False])

    async def test_a_cancelled_waiter_releases_its_lock_entry(self) -> None:
        gate = asyncio.Event()
        service = self.service([fake("a", gate=gate)], downloads())
        first = asyncio.create_task(service.download(EPISODE, TARGET))
        second = asyncio.create_task(service.download(EPISODE, TARGET))
        await asyncio.sleep(0.01)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        gate.set()
        await first
        self.assertEqual(service._target_locks, {})


async def _until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0.001)


class RequestLabelTests(unittest.TestCase):
    def test_labels(self) -> None:
        self.assertEqual(request_label(EPISODE), "Severance S02E01")
        season = SearchRequest(kind=SearchKind.SEASON, title="Severance", season=2)
        self.assertEqual(request_label(season), "Severance S02")
        movie = SearchRequest(kind=SearchKind.MOVIE, title="Dune", year=2021)
        self.assertEqual(request_label(movie), "Dune (2021)")
        self.assertEqual(request_label(movie.model_copy(update={"year": None})), "Dune")


if __name__ == "__main__":
    unittest.main()
