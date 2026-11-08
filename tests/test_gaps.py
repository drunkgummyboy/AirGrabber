"""The gap finder: episodes Plex lacks in the seasons it has some of (My shows ⋯)."""

from __future__ import annotations

import unittest
from datetime import date

from app.models import CalendarEntry, Episode, Show
from app.models.plex import PlexScan
from app.ui.gaps_window import GapsWindow, coming_text, group_gaps, seasons_text
from tests.test_backup_dialog import DialogTestCase
from tests.test_missing import DAY, NOW, ServiceTestCase, ep


class GapQueryTests(ServiceTestCase):
    async def test_the_gaps(self) -> None:
        # Season 1 (Plex has E1, E3), season 2 (none), season 3 (E1; E2 aired, E3 to come),
        # season 4 (aired, after Plex's last season: "missing" covers it, not a gap).
        episodes = [ep(10 + n, 1, n, aired=NOW - 900 * DAY) for n in (1, 2, 3, 4)]
        episodes += [ep(20 + n, 2, n, aired=NOW - 500 * DAY) for n in (1, 2)]
        episodes += [ep(31, 3, 1, aired=NOW - 9 * DAY), ep(32, 3, 2, aired=NOW - 2 * DAY)]
        episodes += [ep(33, 3, 3, aired=NOW + 5 * DAY), ep(34, 3, None, aired=NOW - DAY)]
        episodes += [ep(41, 4, 1, aired=NOW - DAY)]
        await self.add_show(1, episodes, name="Severance")
        await self.add_show(2, [ep(51, 1, 1, aired=NOW - DAY, show_id=2)], name="Nothing in Plex")
        await self.db.plex.replace(
            PlexScan(episodes=frozenset({(1, 1, 1), (1, 1, 3), (1, 3, 1)})), NOW
        )
        await self.grab(1, 2, 1)
        gaps = await self.library.gaps(profile_id=1, now=NOW)
        found = [(entry.episode.season, entry.episode.number) for entry in gaps]
        self.assertEqual(found, [(1, 2), (1, 4), (2, 1), (2, 2), (3, 2)])
        self.assertEqual([entry.is_downloaded for entry in gaps], [False, False, True, False, False])
        self.assertEqual(await self.library.gaps(profile_id=2, now=NOW), [])  # another person

    async def test_none_without_plex(self) -> None:
        await self.severance()
        await self.in_plex((1, 1))
        await self.settings.update(plex_token="")
        self.assertEqual(await self.library.gaps(profile_id=1, now=NOW), [])


def entry(show: Show, season: int, number: int, *, downloaded: bool = False) -> CalendarEntry:
    episode = Episode(
        id=show.id * 1000 + season * 100 + number,
        show_id=show.id,
        season=season,
        number=number,
        air_date=date(2026, 1, 1),
    )
    return CalendarEntry(show=show, episode=episode, is_tracked=True, is_downloaded=downloaded)


class TextTests(unittest.TestCase):
    def test_grouped_per_show(self) -> None:
        ark, lost = Show(id=1, name="The Ark"), Show(id=2, name="Lost")
        gaps = group_gaps(
            [entry(ark, 1, 3), entry(ark, 1, 5, downloaded=True), entry(lost, 2, 1)]
        )
        self.assertEqual([g.show.name for g in gaps], ["The Ark", "Lost"])
        self.assertEqual(len(gaps[0].wanted), 1)
        self.assertEqual(gaps[0].coming, 1)
        self.assertEqual(coming_text(gaps[0].coming), "1 on its way")
        self.assertEqual(coming_text(0), "")

    def test_the_seasons(self) -> None:
        show = Show(id=1, name="The Ark")
        entries = [entry(show, 1, n) for n in (3, 5, 6, 7)] + [entry(show, 2, 1)]
        self.assertEqual(seasons_text(entries), "S01: E03, E05–E07 · S02: E01")


class WindowTests(DialogTestCase):
    async def test_the_buttons_queue_each_show_once(self) -> None:
        ark, lost = Show(id=1, name="The Ark"), Show(id=2, name="Lost")
        queued: list[list[int]] = []
        gaps = group_gaps(
            [entry(ark, 1, 3), entry(ark, 1, 4), entry(lost, 2, 1), entry(lost, 2, 2, downloaded=True)]
        )
        window = GapsWindow(
            self.root,
            gaps,
            on_download=lambda entries: queued.append([e.episode.number or 0 for e in entries]),
        )
        self.keep(window)
        await self.settle()
        assert window.all_button is not None
        self.assertEqual(window.all_button.cget("text"), "⬇ Download all 3")
        window.row_buttons[1].invoke()
        self.assertEqual(queued, [[1]])
        window.all_button.invoke()
        self.assertEqual(queued, [[1], [3, 4]])  # Lost's row was queued already
        self.assertEqual(window.all_button.cget("state"), "disabled")

    async def test_no_gaps(self) -> None:
        window = GapsWindow(self.root, [], on_download=lambda entries: None)
        self.keep(window)
        await self.settle()
        self.assertIsNone(window.all_button)


if __name__ == "__main__":
    unittest.main()
