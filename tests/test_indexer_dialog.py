"""The indexer dialog's pure helpers: table text, row ids, sorting, chips and header lines."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.models import IndexerState, IndexerStatus, Movie, MovieDetails, Show, TorrentRelease
from app.ui import theme
from app.ui.indexer_dialog import (
    SortColumn,
    action_text,
    chip_style,
    episode_title,
    failure_text,
    format_age,
    format_size,
    hidden_text,
    movie_meta,
    movie_title,
    next_sort,
    quality_button_text,
    release_id,
    searching_text,
    show_meta,
    sort_releases,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
GIB = 1024**3


def release(title: str = "Show.S01E01.1080p-GRP", **fields: object) -> TorrentRelease:
    data: dict[str, object] = {"title": title, "source": "apibay", "magnet_uri": None}
    data.update(fields)
    if not (data.get("info_hash") or data.get("magnet_uri") or data.get("download_url")):
        data["download_url"] = "https://example.org/x.torrent"
    return TorrentRelease.model_validate(data)


def status(state: IndexerState, **fields: object) -> IndexerStatus:
    return IndexerStatus.model_validate({"name": "eztv", "label": "EZTV", "state": state, **fields})


class FormatSizeTests(unittest.TestCase):
    def test_binary_units_with_two_decimals(self) -> None:
        self.assertEqual(format_size(round(3.69 * GIB)), "3.69 GB")
        self.assertEqual(format_size(1536), "1.50 KB")
        self.assertEqual(format_size(700 * 1024 * 1024), "700.00 MB")
        self.assertEqual(format_size(2 * 1024 * GIB), "2.00 TB")

    def test_bytes_and_unknown(self) -> None:
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(0), "—")


class FormatAgeTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(format_age(NOW - timedelta(minutes=45), NOW), "45m")
        self.assertEqual(format_age(NOW - timedelta(hours=5, minutes=59), NOW), "5h")
        self.assertEqual(format_age(NOW - timedelta(days=3, hours=2), NOW), "3d")
        self.assertEqual(format_age(NOW - timedelta(days=364), NOW), "364d")
        self.assertEqual(format_age(NOW - timedelta(days=800), NOW), "2y")

    def test_unknown_and_future(self) -> None:
        self.assertEqual(format_age(None, NOW), "—")
        self.assertEqual(format_age(NOW + timedelta(hours=2), NOW), "0m")  # clock skew


class ReleaseIdTests(unittest.TestCase):
    def test_info_hash_when_known(self) -> None:
        self.assertEqual(release_id(release(info_hash="AB" * 20)), "ab" * 20)

    def test_title_and_size_otherwise(self) -> None:
        self.assertEqual(release_id(release("A b", size_bytes=10)), "A b|10")

    def test_stable_across_merged_copies(self) -> None:
        first = release(info_hash="cd" * 20, seeders=3)
        merged = first.model_copy(update={"seeders": 9, "source": "apibay+eztv"})
        self.assertEqual(release_id(first), release_id(merged))


class SortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.small = release("b small", size_bytes=1 * GIB, seeders=50, leechers=1,
                             published_at=NOW - timedelta(days=2))
        self.big = release("A big", size_bytes=4 * GIB, seeders=5, leechers=9,
                           published_at=NOW - timedelta(hours=1))
        self.unknown = release("c unknown", size_bytes=0, seeders=20, source="eztv")

    def titles(self, column: SortColumn, descending: bool) -> list[str]:
        items = [self.small, self.big, self.unknown]
        return [r.title for r in sort_releases(items, column, descending=descending)]

    def test_size_unknown_last_both_ways(self) -> None:
        self.assertEqual(self.titles("size", True), ["A big", "b small", "c unknown"])
        self.assertEqual(self.titles("size", False), ["b small", "A big", "c unknown"])

    def test_counts(self) -> None:
        self.assertEqual(self.titles("seeds", True), ["b small", "c unknown", "A big"])
        self.assertEqual(self.titles("leech", True), ["A big", "b small", "c unknown"])

    def test_name_ignores_case(self) -> None:
        self.assertEqual(self.titles("name", False), ["A big", "b small", "c unknown"])

    def test_age_ascending_is_newest_first_unknown_last(self) -> None:
        self.assertEqual(self.titles("age", False), ["A big", "b small", "c unknown"])
        self.assertEqual(self.titles("age", True), ["b small", "A big", "c unknown"])

    def test_source_and_stability(self) -> None:
        self.assertEqual(self.titles("source", False), ["b small", "A big", "c unknown"])
        twins = [release("x", seeders=1), release("y", seeders=1), release("z", seeders=1)]
        self.assertEqual(
            [r.title for r in sort_releases(twins, "seeds", descending=True)], ["x", "y", "z"]
        )

    def test_next_sort(self) -> None:
        self.assertEqual(next_sort("size", True, "size"), ("size", False))
        self.assertEqual(next_sort("size", False, "size"), ("size", True))
        self.assertEqual(next_sort("size", True, "seeds"), ("seeds", True))
        self.assertEqual(next_sort("size", True, "name"), ("name", False))
        self.assertEqual(next_sort("name", False, "age"), ("age", False))


class LabelTests(unittest.TestCase):
    def test_quality_buttons(self) -> None:
        self.assertEqual(quality_button_text("1080p", 12), "1080P (12)")
        self.assertEqual(quality_button_text("2160p", 0), "4K")
        self.assertEqual(quality_button_text("any", 3), "ANY (3)")

    def test_chips(self) -> None:
        self.assertEqual(chip_style(status(IndexerState.SEARCHING)), ("EZTV", theme.PENDING))
        self.assertEqual(
            chip_style(status(IndexerState.FOUND, count=7)), ("EZTV (7)", theme.SUCCESS)
        )
        self.assertEqual(chip_style(status(IndexerState.EMPTY)), ("EZTV", theme.DANGER_BUTTON))
        self.assertEqual(chip_style(status(IndexerState.FAILED)), ("EZTV ✗", theme.DANGER))
        self.assertEqual(chip_style(status(IndexerState.SKIPPED)), ("EZTV", theme.TEXT_FAINT))

    def test_failures(self) -> None:
        statuses = [
            status(IndexerState.FAILED, message="timed out"),
            status(IndexerState.FOUND, name="apibay", label="APIBay", count=2),
            status(
                IndexerState.FAILED,
                name="yts",
                label="YTS",
                message="YTS: /api/v2/list_movies.json answered HTTP 503",
            ),
        ]
        self.assertEqual(
            failure_text(statuses),
            "EZTV: timed out · YTS: /api/v2/list_movies.json answered HTTP 503",
        )
        self.assertEqual(failure_text([status(IndexerState.EMPTY)]), "")

    def test_actions(self) -> None:
        self.assertEqual(action_text(None, grabbed=False), "⬇ Grab")
        self.assertEqual(action_text(None, grabbed=True), "✓ Grabbed")
        self.assertEqual(action_text("grabbing", grabbed=True), "⏳ Grabbing…")
        self.assertEqual(action_text("saved", grabbed=False), "✓ Saved")
        self.assertEqual(action_text("sent", grabbed=False), "📨 Sent to DS")
        self.assertEqual(action_text("failed", grabbed=True), "✗ Failed")

    def test_hidden_hint(self) -> None:
        self.assertEqual(
            hidden_text(4, showing=False),
            "4 hidden by name/episode matching or your filters — Show",
        )
        self.assertTrue(hidden_text(4, showing=True).endswith("— Hide"))

    def test_searching(self) -> None:
        self.assertEqual(searching_text(2, 1), "Searching indexers for S02E01…")
        self.assertEqual(searching_text(2, None), "Searching indexers for Season 02 pack…")
        self.assertEqual(searching_text(None, None), "Searching indexers…")


class HeaderTests(unittest.TestCase):
    show = Show.model_validate(
        {
            "id": 1,
            "name": "Severance",
            "premiered": "2022-02-18",
            "genres": ["Drama", "Mystery"],
            "status": "Running",
        }
    )
    movie = Movie.model_validate(
        {
            "id": 2,
            "title": "Dune",
            "release_date": "2021-09-15",
            "vote_average": 7.8,
            "vote_count": 9,
        }
    )

    def test_titles(self) -> None:
        self.assertEqual(episode_title(self.show, 1, 5), "Severance: S01E05")
        self.assertEqual(episode_title(self.show, 1, None), "Severance: Season 01 (Pack)")
        self.assertEqual(movie_title(self.movie), "Dune (2021)")
        self.assertEqual(movie_title(Movie(id=3, title="Untitled")), "Untitled")

    def test_show_meta(self) -> None:
        self.assertEqual(show_meta(self.show), "2022 • Drama, Mystery • RUNNING")
        self.assertEqual(show_meta(Show(id=4, name="Bare")), "")

    def test_movie_meta(self) -> None:
        self.assertEqual(movie_meta(self.movie, None), "2021 • ★ 7.8")
        details = MovieDetails(id=2, certification="PG-13", runtime=155)
        self.assertEqual(
            movie_meta(self.movie, details), "2021 • ★ 7.8 • Rated PG-13 • 2h 35m"
        )
        self.assertEqual(movie_meta(Movie(id=5, title="New"), MovieDetails(id=5)), "")


if __name__ == "__main__":
    unittest.main()
