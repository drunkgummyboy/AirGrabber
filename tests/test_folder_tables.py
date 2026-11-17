"""Two shelves: the folders' own tables beside Plex's (`folder_*`, FolderRepository), the have /
missing SQL counting both, and the 3-day grace for a file that just left the folders
(`folder_gone`, GONE_GRACE_DAYS; docs/folder-library/2026-11-16-alongside-plex-design.md)."""

from __future__ import annotations

import unittest
from collections.abc import Iterable
from datetime import UTC, datetime

from app.database import GONE_GRACE_DAYS
from app.models import Show
from app.models.plex import PlexLibrary, PlexScan, PlexTitle
from tests.test_missing import DAY, NOW, DatabaseTestCase, cutoffs, ep


def scan(
    episodes: Iterable[tuple[int, int, int]] = (),
    films: Iterable[tuple[int | None, str | None]] = (),
    titles: tuple[PlexTitle, ...] = (),
    **counts: object,
) -> PlexScan:
    return PlexScan.model_validate(
        {"episodes": frozenset(episodes), "films": frozenset(films), "titles": titles, **counts}
    )


def title(kind: str, key: str, name: str) -> PlexTitle:
    return PlexTitle.model_validate({"kind": kind, "key": key, "title": name})


class FolderTablesTestCase(DatabaseTestCase):
    async def missing(self, now: datetime = NOW) -> frozenset[int]:
        return await self.db.library.missing_between(
            NOW.date() - 30 * DAY, NOW.date() + 30 * DAY, now=now, today=now.date(),
            **cutoffs(now),
        )

    async def gone_rows(self) -> list[tuple[object, ...]]:
        rows = await self.db.fetch_all(
            "SELECT kind, show_id, season, episode, tmdb_id, imdb_id FROM folder_gone "
            "ORDER BY kind, show_id, season, episode, tmdb_id"
        )
        return [tuple(row) for row in rows]

    async def left_days_ago(self, days: float) -> None:
        """Every row of folder_gone left `days` days before the real now (the grace is
        measured against SQLite's 'now')."""
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE folder_gone SET left_at = "
                "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', ?)",
                (f"-{days} days",),
            )


class ReplaceTests(FolderTablesTestCase):
    async def test_replace_keeps_to_its_own_tables(self) -> None:
        await self.add_show(1, [ep(11, 1, 1, aired=NOW - 9 * DAY)])
        await self.add_show(2, [ep(21, 1, 1, aired=NOW - 9 * DAY, show_id=2)])
        plex_scan = scan(
            {(1, 1, 1)}, {(1, "tt1")}, (title("show", "4711", "Show 1"),),
            shows=1, unmatched_shows=2, unmatched_films=3,
            libraries=(PlexLibrary(key="1", title="TV", kind="show"),),
        )
        folder_scan = scan(
            {(2, 1, 1)}, {(2, None)}, (title("show", "folder:show:2", "Show 2"),),
            shows=1, unmatched_shows=0, unmatched_films=1,
        )
        plex_when, folder_when = NOW - DAY, NOW
        await self.db.plex.replace(plex_scan, plex_when)
        await self.db.folders.replace(folder_scan, folder_when)

        async def plex_side() -> object:
            return (
                await self.db.plex.contents(),
                await self.db.plex.titles(),
                await self.db.plex.libraries(),
                await self.db.plex.counts(),
            )

        async def folder_side() -> object:
            return (
                await self.db.folders.contents(),
                await self.db.folders.titles(),
                await self.db.folders.checked_at(),
            )

        plex_before, folder_before = await plex_side(), await folder_side()
        self.assertEqual(
            folder_before,
            (
                (frozenset({(2, 1, 1)}), frozenset({(2, None)})),
                [title("show", "folder:show:2", "Show 2")],
                folder_when,
            ),
        )
        self.assertEqual(
            await self.db.plex.contents(), (frozenset({(1, 1, 1)}), frozenset({(1, "tt1")}))
        )
        self.assertEqual([t.key for t in await self.db.plex.titles()], ["4711"])
        self.assertEqual(len(await self.db.plex.libraries()), 1)
        counts = await self.db.plex.counts()
        self.assertEqual((counts.episodes, counts.films, counts.checked_at), (1, 1, plex_when))

        # Plex's check again: the folders' rows and state stay.
        await self.db.plex.replace(scan(), NOW + DAY)
        self.assertEqual(await folder_side(), folder_before)
        # The folders' check again: Plex's rows, libraries and counts stay.
        await self.db.plex.replace(plex_scan, plex_when)
        await self.db.folders.replace(scan(), NOW + DAY)
        self.assertEqual(await plex_side(), plex_before)
        # Each clear is its own.
        await self.db.folders.replace(folder_scan, folder_when)
        await self.db.plex.clear()
        self.assertEqual(await folder_side(), folder_before)
        await self.db.plex.replace(plex_scan, plex_when)
        await self.db.folders.clear()
        self.assertEqual(await plex_side(), plex_before)
        self.assertEqual(
            await folder_side(), ((frozenset(), frozenset()), [], None)
        )
        self.assertEqual(await self.gone_rows(), [])

    async def test_titles_by_key(self) -> None:
        await self.db.folders.replace(
            scan(
                titles=(title("show", "folder:show:2", "B"), title("movie", "folder:movie:3", "A"))
            ),
            NOW,
        )
        found = await self.db.folders.titles_by_key(
            [("movie", "folder:movie:3"), ("show", "folder:movie:3"), ("show", "nope")]
        )
        self.assertEqual(list(found), [("movie", "folder:movie:3")])
        self.assertEqual(await self.db.folders.titles_by_key([]), {})
        self.assertEqual(
            [t.title for t in await self.db.folders.titles()], ["A", "B"]
        )

    async def test_episodes_of_a_show_not_in_the_cache_are_left_out(self) -> None:
        await self.add_show(1, [ep(11, 1, 1, aired=NOW - 9 * DAY)])
        await self.db.folders.replace(scan({(1, 1, 1), (9, 1, 1)}), NOW)
        self.assertEqual((await self.db.folders.contents())[0], frozenset({(1, 1, 1)}))


class MarksTests(FolderTablesTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Latest season 2: E1 in the folders only, E2 in Plex only, E3 nowhere.
        await self.add_show(
            1,
            [
                ep(21, 2, 1, aired=NOW - 9 * DAY),
                ep(22, 2, 2, aired=NOW - 8 * DAY),
                ep(23, 2, 3, aired=NOW - 7 * DAY),
            ],
        )
        await self.db.library.set_auto_download(1, True, profile_id=1, since=NOW - 30 * DAY)
        await self.db.folders.replace(scan({(1, 2, 1)}), NOW)
        await self.db.plex.replace(scan({(1, 2, 2)}), NOW)

    async def test_marks_count_both(self) -> None:
        self.assertEqual(await self.missing(), {23})
        self.assertEqual(
            await self.db.library.missing_counts(now=NOW, today=NOW.date(), **cutoffs(NOW)),
            {1: 1},
        )
        due = await self.db.library.auto_due(
            aired_from=NOW - 30 * DAY, aired_until=NOW, limit=10
        )
        self.assertEqual([entry.episode.id for entry in due], [23])
        for episode_id in (21, 22, 23):
            await self.db.library.record_auto_attempt(episode_id, NOW)
        given_up = await self.db.library.auto_give_up(aired_before=NOW)
        self.assertEqual([entry.episode.id for entry in given_up], [23])
        self.assertEqual(
            [entry.episode.id for entry, _ in await self.db.library.auto_given_up(limit=10)],
            [23],
        )
        # Just aired (two hours ago): E4 in the folders isn't "new", E5 is.
        await self.db.library.save_show(
            Show(id=1, name="Show 1"),
            [
                ep(21, 2, 1, aired=NOW - 9 * DAY),
                ep(22, 2, 2, aired=NOW - 8 * DAY),
                ep(23, 2, 3, aired=NOW - 7 * DAY),
                ep(24, 2, 4, aired=NOW - DAY / 12),
                ep(25, 2, 5, aired=NOW - DAY / 12),
            ],
        )
        await self.db.folders.replace(scan({(1, 2, 1), (1, 2, 4)}), NOW)
        self.assertEqual(
            await self.db.library.new_between(
                NOW.date() - 30 * DAY, NOW.date(), now=NOW, today=NOW.date(), **cutoffs(NOW)
            ),
            {25},
        )

    async def test_gaps_read_both(self) -> None:
        # Seasons 1 to 3: the folders have S1, Plex has S3, so S2 lies between.
        await self.add_show(
            2,
            [
                ep(31, 1, 1, aired=NOW - 900 * DAY, show_id=2),
                ep(32, 2, 1, aired=NOW - 500 * DAY, show_id=2),
                ep(33, 3, 1, aired=NOW - 100 * DAY, show_id=2),
                ep(34, 3, 2, aired=NOW - 90 * DAY, show_id=2),
            ],
        )
        await self.db.folders.replace(scan({(1, 2, 1), (2, 1, 1)}), NOW)
        await self.db.plex.replace(scan({(1, 2, 2), (2, 3, 1)}), NOW)
        gaps = await self.db.library.gaps(profile_id=1, now=NOW, today=NOW.date())
        self.assertEqual([entry.episode.id for entry in gaps], [23, 32, 34])

    async def test_films_and_shows_count_both(self) -> None:
        await self.add_show(2, [ep(31, 1, 1, aired=NOW - 9 * DAY, show_id=2)])
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE shows SET tvdb_id = id + 100, imdb_id = 'tt' || id")
        await self.db.folders.replace(scan({(1, 2, 1)}, {(7, None)}), NOW)
        await self.db.plex.replace(scan({(2, 1, 1)}, {(None, "tt8")}), NOW)
        films = await self.db.library.plex_films()
        self.assertEqual((films.tmdb, films.imdb), (frozenset({7}), frozenset({"tt8"})))
        shows = await self.db.library.plex_shows()
        self.assertEqual(
            (shows.tvdb, shows.imdb), (frozenset({101, 102}), frozenset({"tt1", "tt2"}))
        )

    async def test_what_only_the_folders_have(self) -> None:
        # Show 1 in the folders only, show 2 in both; film 7 in the folders only, film 8 in
        # both (Plex knows it by its IMDb id), film 9 in Plex only.
        await self.add_show(2, [ep(31, 1, 1, aired=NOW - 9 * DAY, show_id=2)])
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE shows SET tvdb_id = id + 100, imdb_id = 'tt' || id")
        await self.db.folders.replace(
            scan({(1, 2, 1), (2, 1, 1)}, {(7, "tt7"), (8, "tt8")}), NOW
        )
        await self.db.plex.replace(scan({(2, 1, 1)}, {(None, "tt8"), (9, None)}), NOW)
        films = await self.db.library.plex_films()
        self.assertEqual((films.folder_tmdb, films.folder_imdb), ({7}, {"tt7"}))
        self.assertTrue(films.only_in_folders(7) and films.only_in_folders(None, "tt7"))
        self.assertFalse(films.only_in_folders(8) or films.only_in_folders(9))
        self.assertTrue(films.has(8) and films.has(9))
        shows = await self.db.library.plex_shows()
        self.assertEqual((shows.folder_tvdb, shows.folder_imdb), ({101}, {"tt1"}))
        self.assertFalse(shows.only_in_folders(102, "tt2"))

    async def test_downloaded_between_leaves_out_what_the_folders_have(self) -> None:
        for number in (1, 3):
            await self.grab(1, 2, number)
        since = NOW - 14 * DAY
        downloaded = await self.db.library.downloaded_between(
            NOW.date() - 30 * DAY, NOW.date(), NOW.date(), since
        )
        self.assertEqual(downloaded, {23})

    async def test_folder_between_is_folder_only(self) -> None:
        # E1 in the folders only, E2 in Plex only, E3 in both.
        await self.db.folders.replace(scan({(1, 2, 1), (1, 2, 3)}), NOW)
        await self.db.plex.replace(scan({(1, 2, 2), (1, 2, 3)}), NOW)
        start, end = NOW.date() - 30 * DAY, NOW.date()
        self.assertEqual(await self.db.library.plex_between(start, end), {22, 23})
        self.assertEqual(await self.db.library.folder_between(start, end), {21})
        self.assertEqual(await self.db.library.plex_ids(1), {22, 23})
        self.assertEqual(await self.db.library.folder_ids(1), {21, 23})
        # The days' range holds.
        self.assertEqual(
            await self.db.library.folder_between(start, NOW.date() - 10 * DAY), frozenset()
        )


class GoneTests(FolderTablesTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.add_show(
            1, [ep(21, 2, 1, aired=NOW - 9 * DAY), ep(22, 2, 2, aired=NOW - 8 * DAY)]
        )
        self.now = datetime.now(UTC)
        await self.db.folders.replace(scan({(1, 2, 1), (1, 2, 2)}, {(7, "tt7")}), self.now)

    def test_the_grace_is_three_days(self) -> None:
        self.assertEqual(GONE_GRACE_DAYS, 3)

    async def test_a_file_that_left_counts_for_three_days(self) -> None:
        self.assertEqual(await self.missing(), frozenset())
        # E2 and the film leave the folders (moved by hand to Plex's folder, say).
        await self.db.folders.replace(scan({(1, 2, 1)}), self.now)
        self.assertEqual(
            await self.gone_rows(),
            [("episode", 1, 2, 2, None, None), ("movie", None, None, None, 7, "tt7")],
        )
        left = await self.db.fetch_all("SELECT left_at FROM folder_gone")
        self.assertEqual(
            {datetime.fromisoformat(row["left_at"]) for row in left}, {self.now}
        )
        self.assertEqual(await self.db.folders.contents(), (frozenset({(1, 2, 1)}), frozenset()))
        # Still had two days on: not missing, the film and the show still count.
        await self.left_days_ago(2)
        self.assertEqual(await self.missing(), frozenset())
        self.assertTrue((await self.db.library.plex_films()).has(7))
        self.assertTrue((await self.db.library.plex_films()).has(None, "tt7"))
        self.assertEqual(await self.db.library.folder_ids(1), {21, 22})
        self.assertEqual(
            await self.db.library.folder_between(NOW.date() - 30 * DAY, NOW.date()), {21, 22}
        )
        self.assertEqual(await self.db.library.plex_ids(1), frozenset())
        # On day 4 without Plex having it, it is missing again.
        await self.left_days_ago(4)
        self.assertEqual(await self.missing(), {22})
        self.assertFalse((await self.db.library.plex_films()).has(7, "tt7"))
        self.assertEqual(await self.db.library.folder_ids(1), {21})
        # Plex has it by then: had again, from Plex.
        await self.db.plex.replace(scan({(1, 2, 2)}), self.now)
        self.assertEqual(await self.missing(), frozenset())

    async def test_what_comes_back_leaves_folder_gone(self) -> None:
        await self.db.folders.replace(scan({(1, 2, 1)}), self.now)
        self.assertEqual(len(await self.gone_rows()), 2)
        await self.db.folders.replace(scan({(1, 2, 1), (1, 2, 2)}, {(7, "tt7")}), self.now)
        self.assertEqual(await self.gone_rows(), [])
        # A film back under a different spelling of its ids (one of them) also counts as back.
        await self.db.folders.replace(scan({(1, 2, 1), (1, 2, 2)}), self.now)
        await self.db.folders.replace(scan({(1, 2, 1), (1, 2, 2)}, {(7, None)}), self.now)
        self.assertEqual(await self.gone_rows(), [])

    async def test_a_second_check_keeps_when_it_left(self) -> None:
        await self.db.folders.replace(scan({(1, 2, 1)}), self.now)
        await self.left_days_ago(1)
        before = await self.db.fetch_all("SELECT left_at FROM folder_gone")
        await self.db.folders.replace(scan({(1, 2, 1)}), self.now)
        after = await self.db.fetch_all("SELECT left_at FROM folder_gone")
        self.assertEqual([tuple(r) for r in after], [tuple(r) for r in before])

    async def test_rows_older_than_the_grace_are_dropped(self) -> None:
        await self.db.folders.replace(scan({(1, 2, 1)}), self.now)
        await self.left_days_ago(GONE_GRACE_DAYS + 1)
        await self.db.folders.replace(scan({(1, 2, 1)}), self.now)
        self.assertEqual(await self.gone_rows(), [])

    async def test_a_show_that_leaves_the_cache_takes_its_rows_along(self) -> None:
        await self.db.folders.replace(scan(), self.now)
        self.assertEqual(len(await self.gone_rows()), 3)
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM shows WHERE id = 1")
        self.assertEqual(await self.gone_rows(), [("movie", None, None, None, 7, "tt7")])


if __name__ == "__main__":
    unittest.main()
