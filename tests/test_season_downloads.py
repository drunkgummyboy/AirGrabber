"""Season downloads in the show window (3.0.1): per season its download control in the
season bar, in the rows' style: "⬇ Whole season" + "☰ Choose…" once it has finished airing
(the season pack; the episodes still needed when there is none), with Plex "⬇ All missing (n)"
while it airs (hidden at 0; without Plex none), their looks following the download queue ("Queued", the batch's progress,
the outcome). The rules (pure), the window on fake services, and the whole path through the
real DownloadQueue + QueueLink with a fake one-click download. No network."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from collections.abc import Sequence
from typing import Any, cast

from app.models import (
    TitleExtras,
    TitleInfo,
    Video,
    CalendarEntry,
    Episode,
    GrabOutcome,
    MediaType,
    SearchKind,
    Show,
    ShowDetails,
)
from app.services.download_queue import DownloadQueue
from app.services.search import TorrentSearchService
from app.ui.download_control import (
    BatchKey,
    BatchState,
    DownloadActions,
    DownloadStates,
    Phase,
    RunState,
    episode_key,
)
from app.ui.main_window import MainWindow
from app.ui.queue_link import NO_PACK_TEXT, QueueLink
from app.ui.release_panel import TvTarget
from app.ui.show_details import (
    SeasonActions,
    ShowDetailsWindow,
    ShowWindows,
    season_kind,
    season_look,
    season_needed,
)
from tests.test_download_queue import NOTHING, FakeQuick
from tests.test_info_layouts import WindowDiscovery, no_subscribe
from tests.test_info_window import FakeImages
from tests.test_release_panel import TkTestCase, no_status
from tests.test_search import settings_service

NOW = datetime.now(UTC)
SHOW = Show(id=61, name="Harbour Lights")


def episode(season: int, number: int | None, *, days_ago: float) -> Episode:
    airs = NOW - timedelta(days=days_ago)
    return Episode(
        id=season * 100 + (number or 50),
        show_id=SHOW.id,
        season=season,
        number=number,
        name=f"Part {number}",
        airstamp=airs,
        air_date=airs.date(),
    )


S1 = (episode(1, 1, days_ago=90), episode(1, 2, days_ago=83), episode(1, 3, days_ago=76))
S2 = (episode(2, 1, days_ago=14), episode(2, 2, days_ago=7), episode(2, 3, days_ago=-7))
S3 = (episode(3, 1, days_ago=-60),)
SPECIAL = episode(1, None, days_ago=70)


def details(**fields: Any) -> ShowDetails:
    values: dict[str, Any] = {
        "show": SHOW,
        "episodes": (*S1, SPECIAL, *S2, *S3),
        "tracked": True,
        "plex_connected": True,
        "latest_season": 2,
        "downloaded": frozenset({S1[0].id}),
        "missing": frozenset({S2[1].id}),
        "in_plex": frozenset({S2[0].id}),
    }
    values.update(fields)
    return ShowDetails(**values)


def key(season: int, number: int | None) -> tuple[MediaType, int, int | None, int | None]:
    return episode_key(SHOW.id, season, number)


# -- the rules ---------------------------------------------------------------------------------


class SeasonRuleTests(unittest.TestCase):
    def test_finished_airing_or_not_aired(self) -> None:
        episodes = details().episodes
        now = datetime.now(UTC)
        self.assertEqual(season_kind(episodes, 1, now), "whole")
        self.assertEqual(season_kind(episodes, 2, now), "missing")
        self.assertIsNone(season_kind(episodes, 3, now))
        self.assertIsNone(season_kind(episodes, 9, now))
        self.assertIsNone(season_kind((SPECIAL,), 1, now))  # only a special: nothing to fetch

    def test_still_needed_is_the_calendar_stacks_rule(self) -> None:
        states = DownloadStates()
        info = details()
        codes = [e.episode.code for e in season_needed(info, 1, states.get)]
        self.assertEqual(codes, ["S01E02", "S01E03"])  # E01 downloaded, the special unnumbered
        self.assertEqual([e.episode.code for e in season_needed(info, 2, states.get)], ["S02E02"])
        self.assertEqual(season_needed(info, 3, states.get), ())
        states.set(key(1, 2), RunState(Phase.QUEUED))  # queued: still needed
        states.set(key(1, 3), RunState(Phase.DONE, GrabOutcome.SENT))  # just grabbed
        self.assertEqual([e.episode.code for e in season_needed(info, 1, states.get)], ["S01E02"])

    def test_the_looks(self) -> None:
        whole = season_look("whole", None, None)
        self.assertEqual((whole.kind, whole.buttons), ("download", ("auto", "choose")))
        missing = season_look("missing", None, None)
        self.assertEqual((missing.kind, missing.buttons), ("download", ("auto",)))
        queued = season_look("whole", RunState(Phase.QUEUED), None)
        self.assertEqual((queued.kind, queued.status, queued.buttons), ("queued", ("Queued",), ()))
        working = season_look("whole", RunState(Phase.WORKING), None)
        self.assertEqual(working.status[0], "Finding the best one…")
        batch = season_look("missing", None, BatchState(1, 3, queued=True))
        self.assertEqual(batch.status, ("Queued",))
        progress = season_look("missing", None, BatchState(2, 3))
        self.assertEqual(progress.status[0], "Downloading 2 of 3…")
        done = season_look("whole", RunState(Phase.DONE, GrabOutcome.SENT), None)
        self.assertEqual((done.kind, done.status, done.buttons), ("done", ("✓ Sent",), ()))
        nothing = season_look("whole", RunState(Phase.NOTHING), None)
        self.assertEqual(
            (nothing.status[0], nothing.buttons, nothing.alert),
            ("No good release found", ("choose",), True),
        )


# -- the window --------------------------------------------------------------------------------


class SeasonLibrary:
    """What the show window asks: the details (changeable), the listeners."""

    def __init__(self, info: ShowDetails) -> None:
        self.info = info

    async def show_details(self, show_id: int, *, profile_id: int) -> ShowDetails:
        return self.info

    async def set_auto_download(self, show_id: int, on: bool, *, profile_id: int) -> None:
        return None

    def subscribe(self, listener: Any) -> Any:
        return lambda: None


class SeasonWindowTestCase(TkTestCase):
    def open(
        self, actions: DownloadActions[CalendarEntry], info: ShowDetails | None = None
    ) -> ShowDetailsWindow:
        self.library = SeasonLibrary(info or details())
        shows = ShowWindows(
            self.root,
            library=cast(Any, self.library),
            discovery=cast(Any, WindowDiscovery()),
            images=cast(Any, FakeImages()),
            status=no_status,
            episode_actions=actions,
            downloads_changed=no_subscribe,
            profile=lambda: 1,
        )
        self.addCleanup(shows.close)
        self.shows = shows
        shows.open_show(SHOW)
        window = shows._live()[SHOW.id]
        assert isinstance(window, ShowDetailsWindow)
        return window

    async def ready(self, window: ShowDetailsWindow, season: int | None = None) -> None:
        for _ in range(4):
            await self.settle()
        if season is not None:
            window._select_season(season)
            for _ in range(3):
                await self.settle()

    def control(self, window: ShowDetailsWindow) -> SeasonActions | None:
        control = window._season_actions
        if control is None or not control.winfo_ismapped():
            return None
        return control


class SeasonControlTests(SeasonWindowTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.states = DownloadStates()
        self.seasons: list[tuple[int, int, list[str]]] = []
        self.aired: int | None = None
        self.batches: list[tuple[BatchKey, list[str]]] = []
        self.actions = DownloadActions[CalendarEntry](
            download=lambda entry: None,
            choose=lambda entry: None,
            states=self.states,
            download_all=lambda batch, entries: self.batches.append(
                (batch, [e.episode.code for e in entries])
            ),
            download_season=self._season_asked,
        )

    def _season_asked(
        self, show: Show, season: int, entries: Sequence[CalendarEntry], aired: int | None
    ) -> None:
        self.seasons.append((show.id, season, [e.episode.code for e in entries]))
        self.aired = aired

    async def test_a_finished_season_offers_the_whole_season(self) -> None:
        window = self.open(self.actions)
        await self.ready(window, 1)
        control = self.control(window)
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        self.assertTrue(control.choose.winfo_ismapped())
        self.assertIsNone(window._missing_label)  # not the latest season
        control.auto.invoke()
        self.assertEqual(self.seasons, [(SHOW.id, 1, ["S01E02", "S01E03"])])
        self.assertEqual(self.aired, 3)  # what the pack's size is judged by
        chosen: list[TvTarget] = []
        window.choose = chosen.append  # type: ignore[method-assign,assignment]
        control.choose.invoke()
        self.assertEqual(chosen, [TvTarget(SHOW, 1, None, 3)])  # the pack's releases

    async def test_with_plex_the_whole_season_shows_after_an_earlier_grab(self) -> None:
        info = details(downloaded=frozenset(e.id for e in S1))
        window = self.open(self.actions, info)
        await self.ready(window, 1)
        control = self.control(window)  # grabbed isn't had: only what's in Plex counts
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        control.auto.invoke()
        # The pack again; its fallback still leaves out what was grabbed before.
        self.assertEqual(self.seasons, [(SHOW.id, 1, [])])

    async def test_with_plex_the_whole_season_shows_while_part_is_in_plex(self) -> None:
        info = details(in_plex=frozenset({S1[0].id, S1[1].id}), downloaded=frozenset({S1[2].id}))
        window = self.open(self.actions, info)
        await self.ready(window, 1)
        self.assertIsNotNone(self.control(window))

    async def test_with_plex_the_whole_season_shows_when_plex_has_it_all(self) -> None:
        info = details(in_plex=frozenset(e.id for e in S1))
        window = self.open(self.actions, info)
        await self.ready(window, 1)
        control = self.control(window)
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        control.auto.invoke()
        self.assertEqual(self.seasons, [(SHOW.id, 1, [])])  # the pack; no episode as fallback

    async def test_an_airing_season_offers_all_missing(self) -> None:
        window = self.open(self.actions)
        await self.ready(window)
        self.assertEqual(window._season, 2)
        control = self.control(window)
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ All missing (1)")
        self.assertFalse(control.choose.winfo_ismapped())
        assert window._missing_label is not None
        self.assertEqual(window._missing_label.cget("text"), "1 missing")
        control.auto.invoke()
        self.assertEqual(self.batches, [((key(2, 2),), ["S02E02"])])

    async def test_nothing_missing_the_whole_season_and_nothing_aired_no_control(self) -> None:
        info = details(in_plex=frozenset({S2[0].id, S2[1].id}), missing=frozenset())
        window = self.open(self.actions, info)
        await self.ready(window, 2)
        control = self.control(window)  # All missing (0) gives way
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        assert window._missing_label is not None
        self.assertEqual(window._missing_label.cget("text"), "Nothing missing")
        window._select_season(3)
        await self.settle()
        self.assertIsNone(self.control(window))
        self.assertFalse(window._season_bar.winfo_manager())  # nothing aired: no bar

    async def test_the_trailer_button_follows_the_season(self) -> None:
        show_video = Video(key="show", site="YouTube", type="Trailer")
        season_video = Video(key="three", site="YouTube", type="Trailer")

        class Discovery:
            has_tmdb_key = True

            async def season_trailer(self, show: Any, season: int) -> Video | None:
                return season_video if season == 3 else None

        window = self.open(self.actions, details())
        await self.ready(window)
        window._discovery = cast(Any, Discovery())
        window._extras = TitleExtras(info=TitleInfo(id=1), trailer=show_video)
        calls: list[tuple[Any, ...]] = []
        real = window.masthead.set_trailer

        def spy(url: str | None, label: str | None = None) -> None:
            calls.append((url, label))
            real(url, label)

        window.masthead.set_trailer = spy  # type: ignore[method-assign]
        window._select_season(3)
        await self.settle()
        self.assertEqual(calls[-1], (season_video.watch_url, "Season 3 trailer"))
        self.assertEqual(window.masthead._pill_words(), "Season 3 trailer")
        window._select_season(2)  # none of its own: the show's
        await self.settle()
        self.assertEqual(calls[-1], (show_video.watch_url, None))
        self.assertEqual(window.masthead._pill_words(), "Watch trailer")

    async def test_all_missing_counts_exactly_the_missing_ones_not_the_new(self) -> None:
        # S02E01 isn't in Plex here and counts as new: "1 missing" and "All missing (1)" agree.
        info = details(in_plex=frozenset(), new=frozenset({S2[0].id}))
        window = self.open(self.actions, info)
        await self.ready(window, 2)
        control = self.control(window)
        assert control is not None and window._missing_label is not None
        self.assertEqual(window._missing_label.cget("text"), "1 missing")
        self.assertEqual(control.auto.cget("text"), "⬇ All missing (1)")
        control.auto.invoke()
        self.assertEqual(self.batches, [((key(2, 2),), ["S02E02"])])

    async def test_untracked_show_has_nothing_missing_so_no_all_missing(self) -> None:
        info = details(tracked=False, latest_season=None, missing=frozenset(), in_plex=frozenset())
        window = self.open(self.actions, info)
        await self.ready(window, 2)
        control = self.control(window)
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        self.assertIsNone(window._missing_label)

    async def test_without_plex_only_the_whole_season(self) -> None:
        info = details(
            plex_connected=False, latest_season=None, missing=frozenset(), in_plex=frozenset()
        )
        window = self.open(self.actions, info)
        await self.ready(window, 2)
        airing = self.control(window)  # no "All missing" without Plex
        assert airing is not None
        self.assertEqual(airing.auto.cget("text"), "⬇ Whole season")
        self.assertIsNone(window._missing_label)
        self.assertTrue(all(row._mark_slot is None for row in window._rows.values()))
        window._select_season(1)
        for _ in range(3):
            await self.settle()
        control = self.control(window)
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")
        control.auto.invoke()
        # Its fallback leaves out what was grabbed before: nothing is fetched twice.
        self.assertEqual(self.seasons, [(SHOW.id, 1, ["S01E02", "S01E03"])])
        self.assertIsNone(window._missing_label)

    async def test_without_plex_the_whole_season_shows_even_when_all_were_grabbed(self) -> None:
        info = details(
            plex_connected=False,
            latest_season=None,
            missing=frozenset(),
            in_plex=frozenset(),
            downloaded=frozenset(e.id for e in S1),
        )
        window = self.open(self.actions, info)
        await self.ready(window, 1)
        control = self.control(window)  # nothing tells it was all downloaded
        assert control is not None
        self.assertEqual(control.auto.cget("text"), "⬇ Whole season")

    async def test_its_look_follows_the_queue(self) -> None:
        window = self.open(self.actions)
        await self.ready(window, 1)
        control = self.control(window)
        assert control is not None
        self.states.set(key(1, None), RunState(Phase.QUEUED))
        await self.settle()
        self.assertEqual(control.status.cget("text"), "Queued")
        self.assertFalse(control.auto.winfo_ismapped())
        self.states.set(key(1, None), None)
        self.states.set_batch((key(1, 2), key(1, 3)), BatchState(2, 2))
        await self.settle()
        self.assertEqual(control.status.cget("text"), "Downloading 2 of 2…")
        self.states.set_batch((key(1, 2), key(1, 3)), None)
        await self.settle()
        self.assertTrue(control.auto.winfo_ismapped())

    async def test_without_season_packs_a_finished_season_has_no_control(self) -> None:
        actions = DownloadActions[CalendarEntry](
            download=lambda entry: None, choose=lambda entry: None, states=self.states
        )
        window = self.open(actions)
        await self.ready(window, 1)
        self.assertIsNone(self.control(window))


# -- through the queue -------------------------------------------------------------------------


class ThroughTheQueueTests(SeasonWindowTestCase):
    """MainWindow's season and batch downloads (bound to a stand-in) into a real queue."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.quick = FakeQuick()
        self.queue = DownloadQueue(self.quick, workers=1)  # type: ignore[arg-type]
        self.addCleanup(self.queue.close)
        self.states = DownloadStates()
        self.reports: list[str] = []
        link = QueueLink(self.queue, self.states, report=self.reports.append)
        self.addCleanup(link.close)
        main = SimpleNamespace(
            _queue_link=link,
            _services=SimpleNamespace(search=TorrentSearchService([], settings_service())),
        )
        names = (
            "_tv_item",
            "_episode_item",
            "_download_episode",
            "_download_episodes",
            "_download_season",
            "_search_episode",
        )
        for name in names:
            setattr(main, name, getattr(MainWindow, name).__get__(main))
        self.main: Any = main
        self.actions = DownloadActions[CalendarEntry](
            download=main._download_episode,
            choose=lambda entry: None,
            states=self.states,
            download_all=main._download_episodes,
            download_season=main._download_season,
        )

    async def idle(self) -> None:
        async with asyncio.timeout(3):
            while self.queue.running or self.queue.waiting:
                await asyncio.sleep(0.005)
        await self.settle()

    def row_kinds(self, window: ShowDetailsWindow) -> dict[str, str]:
        kinds: dict[str, str] = {}
        for row in window._rows.values():
            actions = row.row_actions
            if actions is not None and actions.look is not None:
                kinds[row._episode.code] = actions.look.kind
        return kinds

    async def test_whole_season_without_a_pack_queues_the_episodes(self) -> None:
        self.quick.results[key(1, None)] = NOTHING
        self.quick.gate(key(1, None), key(1, 2))
        window = self.open(self.actions)
        await self.ready(window, 1)
        control = self.control(window)
        assert control is not None
        control.auto.invoke()
        await self.settle()
        self.assertEqual(control.status.cget("text"), "Finding the best one…")
        self.assertEqual(self.quick.requests[0].kind, SearchKind.SEASON)
        self.quick.open(key(1, None))  # no pack
        await self.settle()
        self.assertEqual(self.reports, [NO_PACK_TEXT])
        self.assertEqual(self.row_kinds(window)["S01E02"], "working")
        self.assertEqual(self.row_kinds(window)["S01E03"], "queued")
        self.assertEqual(control.status.cget("text"), "Downloading 1 of 2…")
        window.destroy()  # closed mid-run
        self.quick.open(key(1, 2))
        await self.idle()
        self.assertEqual(self.quick.started, [key(1, None), key(1, 2), key(1, 3)])
        self.shows.open_show(SHOW)  # opened again: the outcomes show
        again = self.shows._live()[SHOW.id]
        await self.ready(again, 1)
        kinds = self.row_kinds(again)
        self.assertEqual((kinds["S01E02"], kinds["S01E03"]), ("done", "done"))

    async def test_whole_season_with_a_pack(self) -> None:
        window = self.open(self.actions)
        await self.ready(window, 1)
        control = self.control(window)
        assert control is not None
        control.auto.invoke()
        await self.idle()
        self.assertEqual(self.quick.started, [key(1, None)])
        self.assertEqual(control.status.cget("text"), "✓ Sent")
        report = "Harbour Lights S01 · 1080p · 40 seeders · Sent to qBittorrent"
        self.assertEqual(self.reports, [report])

    async def test_all_missing_queues_the_episodes_as_a_batch(self) -> None:
        info = details(in_plex=frozenset(), missing=frozenset({S2[0].id, S2[1].id}))
        self.quick.gate(key(2, 1))
        window = self.open(self.actions, info)
        await self.ready(window, 2)
        control = self.control(window)
        assert control is not None
        control.auto.invoke()
        control.auto.invoke()  # twice: queued once
        await self.settle()
        self.assertEqual(control.status.cget("text"), "Downloading 1 of 2…")
        self.assertEqual(self.row_kinds(window)["S02E02"], "queued")
        self.assertEqual(len(self.queue.waiting), 1)
        self.quick.open(key(2, 1))
        await self.idle()
        self.assertEqual(self.quick.started, [key(2, 1), key(2, 2)])
        # What is missing skips what was grabbed before (never by hand: see below).
        self.assertEqual(self.quick.again, [False, False])

    async def test_a_click_by_hand_downloads_again_the_batches_skip(self) -> None:
        main = self.main
        entry = CalendarEntry(show=SHOW, episode=S1[0], is_tracked=True, is_downloaded=True)
        main._download_episode(entry)  # ⬇ / ⬇ Auto on an episode grabbed before
        await self.idle()
        self.assertEqual((self.quick.started, self.quick.again), ([key(1, 1)], [True]))
        self.quick.results[key(1, None)] = NOTHING  # no pack: the fallback episodes
        main._download_season(SHOW, 1, [CalendarEntry(show=SHOW, episode=S1[1], is_tracked=True)])
        await self.idle()
        self.assertEqual(self.quick.started[1:], [key(1, None), key(1, 2)])
        self.assertEqual(self.quick.again[1:], [True, False])  # the pack by hand, its fallback not


if __name__ == "__main__":
    unittest.main()
