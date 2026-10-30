"""The calendar's amber marks: which episodes in a date range are missing (the SQL query
`missing_between`), in step with app.services.missing's rule, and the episodes marked "I
already have this" in a range (`have_between`, the stack cards)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.library import downloaded_ids
from app.services.missing import missing_episodes, new_episodes
from tests.test_missing import DAY, NOW, DatabaseTestCase, ServiceTestCase, cutoffs, ep


class MissingBetweenTests(DatabaseTestCase):
    async def between(self, start: date, end: date, now: datetime = NOW) -> frozenset[int]:
        return await self.db.library.missing_between(
            start, end, now=now, today=now.date(), **cutoffs(now)
        )

    async def new_between(self, start: date, end: date, now: datetime = NOW) -> frozenset[int]:
        return await self.db.library.new_between(
            start, end, now=now, today=now.date(), **cutoffs(now)
        )

    async def python_between(
        self, start: date, end: date, now: datetime = NOW, *, new: bool = False
    ) -> frozenset[int]:
        rule = new_episodes if new else missing_episodes
        found: set[int] = set()
        for show_id in await self.db.library.tracked_ids():
            episodes = await self.db.library.episodes(show_id)
            downloaded = downloaded_ids(episodes, await self.db.history.show_grabs(show_id))
            have = await self.db.library.have_ids(show_id)
            for episode in rule(episodes, downloaded=downloaded, have=have, now=now):
                if episode.air_date is not None and start <= episode.air_date <= end:
                    found.add(episode.id)
        return frozenset(found)

    async def test_matches_the_rule_on_every_edge_case(self) -> None:
        # 1: latest season 2 (season 3 not aired yet), one episode grabbed, one marked, a special.
        await self.add_show(
            1,
            [
                ep(11, 1, 1, aired=NOW - 3 * DAY),  # an older season: never marked
                ep(21, 2, 1, aired=NOW - 6 * DAY),
                ep(22, 2, 2, aired=NOW - 5 * DAY),
                ep(23, 2, 3, aired=NOW - 4 * DAY),
                ep(24, 2, 4, aired=NOW - 1 * DAY),
                ep(25, 2, None, aired=NOW - 2 * DAY),
                ep(31, 3, 1, aired=NOW + 3 * DAY),
            ],
        )
        await self.grab(1, 2, 2)
        await self.db.library.mark_have([23], NOW)
        # 2: a season pack: nothing missing.
        await self.add_show(2, [ep(41, 1, 1, aired=NOW - DAY, show_id=2)])
        await self.grab(2, 1, None)
        # 3: an air date only, today; and tomorrow.
        await self.add_show(
            3,
            [
                ep(61, 1, 1, air_date=NOW.date(), show_id=3),
                ep(62, 1, 2, air_date=NOW.date() + DAY, show_id=3),
            ],
        )
        # 4: untracked: never marked.
        await self.add_show(4, [ep(71, 1, 1, aired=NOW - DAY, show_id=4)], tracked=False)

        start, end = NOW.date() - 7 * DAY, NOW.date() + 7 * DAY
        # 61 aired today (an air date only): new, not missing yet.
        self.assertEqual(await self.between(start, end), {21, 24})
        self.assertEqual(await self.between(start, end), await self.python_between(start, end))
        self.assertEqual(await self.new_between(start, end), {61})
        self.assertEqual(
            await self.new_between(start, end), await self.python_between(start, end, new=True)
        )
        # Only the range's days.
        self.assertEqual(await self.between(NOW.date() - DAY, NOW.date()), {24})
        later = NOW + 4 * DAY  # season 3 has aired: season 2 is no longer the latest
        self.assertEqual(await self.between(start, end, later), {31, 61, 62})
        self.assertEqual(await self.new_between(start, end, later), frozenset())
        just_aired = NOW + 3 * DAY + timedelta(hours=2)  # season 3's first: new
        self.assertEqual(await self.between(start, end, just_aired), {61, 62})
        self.assertEqual(await self.new_between(start, end, just_aired), {31})
        for when in (later, just_aired, NOW + DAY, NOW + 5 * DAY):
            self.assertEqual(
                await self.between(start, end, when), await self.python_between(start, end, when)
            )
            self.assertEqual(
                await self.new_between(start, end, when),
                await self.python_between(start, end, when, new=True),
            )

    async def test_new_turns_missing_after_a_day(self) -> None:
        aired = NOW - timedelta(hours=20)
        await self.add_show(1, [ep(1, 1, 1, aired=aired)])
        start, end = NOW.date() - 7 * DAY, NOW.date() + 7 * DAY
        self.assertEqual(await self.between(start, end), frozenset())
        self.assertEqual(await self.new_between(start, end), {1})
        four_hours_later = NOW + timedelta(hours=4)
        self.assertEqual(await self.between(start, end, four_hours_later), {1})
        self.assertEqual(await self.new_between(start, end, four_hours_later), frozenset())


class LibraryMissingBetweenTests(ServiceTestCase):
    async def test_marks_and_grabs_take_episodes_off(self) -> None:
        await self.severance()  # season 2: episodes 21-23 aired, 99 still to come
        start, end = NOW.date() - 7 * DAY, NOW.date() + 7 * DAY
        self.assertEqual(await self.library.missing_between(start, end, now=NOW), {21, 22, 23})
        await self.library.mark_have([21])
        await self.grab(1, 2, 2)
        self.assertEqual(await self.library.missing_between(start, end, now=NOW), {23})

    async def test_have_between_lists_the_marked_episodes_of_the_range(self) -> None:
        await self.severance()  # season 1 long ago; season 2: 21-23 aired, 99 to come
        start, end = NOW.date() - 7 * DAY, NOW.date() + 7 * DAY
        self.assertEqual(await self.library.have_between(start, end), frozenset())
        await self.library.mark_have([10, 22, 99])
        # Episode 10 aired long before the range: left out.
        self.assertEqual(await self.library.have_between(start, end), {22, 99})
        self.assertEqual(await self.library.have_between(start, NOW.date()), {22})
        await self.library.unmark_have([22])
        self.assertEqual(await self.library.have_between(start, end), {99})
