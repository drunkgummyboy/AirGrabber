"""The queue's rows (app/forge/ui/queue_rows.py): no Tk, files made up by hand (converted from
the row-building checks of MediaForge's tests/ui/test_queue_view.py): shows and films in one
list, each band's status, the All / Needs you / Ready filter and its counts, the done ones at the
end, each kind named by its own rule."""

from __future__ import annotations

import itertools
import time
from datetime import date
from pathlib import Path

from app import i18n
from app.forge.core.matcher import Resolution
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.ui import queue_rows
from app.forge.ui.queue_rows import (
    DONE_TODAY,
    QueueRows,
    build_queue_rows,
    build_rows,
    empty_texts,
    episode_code,
    item_kind,
    item_state,
    needs_you,
)
from app.forge.ui.widgets.virtual_list import Row
from tests.forge.base import ForgeTestCase

SERIES_FOLDER = "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {en}"
SERIES_FLAT = "{ny} - {s00e00} - {en}"
MOVIE_FLAT = "{ny}"
MOVIE_FOLDER = "{ny}/{ny}"
DONE = ItemStatus.DONE
_ids = itertools.count(1)


def episode(
    folder: Path,
    name: str,
    title: str,
    season: int,
    number: int,
    *,
    end: int | None = None,
    episode_title: str | None = None,
    **fields: object,
) -> QueueItem:
    parsed = ParsedMedia(
        source_name=name,
        kind=MediaKind.EPISODE,
        title=title,
        season=season,
        episode=number,
        episode_end=end,
        episode_title=episode_title,
    )
    fields.setdefault("status", ItemStatus.READY)
    return QueueItem(next(_ids), folder / name, folder, parsed, **fields)  # type: ignore[arg-type]


def movie(folder: Path, name: str, title: str, year: int | None, **fields: object) -> QueueItem:
    parsed = ParsedMedia(source_name=name, kind=MediaKind.MOVIE, title=title, year=year)
    fields.setdefault("status", ItemStatus.READY)
    return QueueItem(next(_ids), folder / name, folder, parsed, **fields)  # type: ignore[arg-type]


def rule(series: str = SERIES_FLAT, movies: str = MOVIE_FLAT) -> object:
    return lambda item: series if item.media.kind is MediaKind.EPISODE else movies


def build(items: list[QueueItem], **kwargs: object) -> QueueRows:
    kwargs.setdefault("show", "all")
    fmt = kwargs.pop("rule", rule())
    return build_queue_rows(items, fmt, **kwargs)  # type: ignore[arg-type]


def bands(rows: QueueRows) -> list[Row]:
    return [r for r in rows.rows if r.kind == "band"]


def sections(rows: QueueRows) -> list[str]:
    return [r.title for r in rows.rows if r.kind == "section"]


def unsure(item: QueueItem) -> QueueItem:
    item.status = ItemStatus.NEEDS_CHOICE
    item.resolution = Resolution(media=ResolvedMedia.offline(item.parsed, MatchState.UNCERTAIN))
    return item


class QueueRowsTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / "in"

    def test_shows_and_films_are_one_list_by_name(self) -> None:
        items = [
            episode(self.folder, "Ted.Lasso.S04E05.1080p.WEB.mkv", "Ted Lasso", 4, 5),
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021),
            episode(self.folder, "Ted.Lasso.S04E03.1080p.WEB.mkv", "Ted Lasso", 4, 3),
            episode(self.folder, "Severance.S03E02.2160p.WEB-DL.mkv", "Severance", 3, 2),
        ]
        rows = build(items, rule=rule(SERIES_FOLDER, MOVIE_FOLDER))
        self.assertEqual(sections(rows), [])  # no headings: the filter says what is shown
        self.assertEqual([b.title for b in bands(rows)], ["Dune", "Severance", "Ted Lasso"])
        ted = next(b for b in bands(rows) if b.title == "Ted Lasso")
        self.assertEqual(ted.subtitle, "Season 4 · 2 episodes")
        self.assertEqual(ted.dest, "→ Ted Lasso › Ted Lasso - Season 04")
        self.assertEqual((ted.state, ted.state_text), ("ready", "Ready"))
        lines = rows.rows[rows.rows.index(ted) + 1 :]
        self.assertEqual([r.code for r in lines], ["S04E03", "S04E05"])  # by episode
        self.assertEqual(lines[0].faint, "Ted.Lasso.S04E03.1080p.WEB.mkv")
        # The current name says so, so it isn't taken for the new one.
        self.assertEqual({r.faint_label for r in lines}, {"Now:"})
        self.assertEqual({r.new for r in lines}, {False})  # an episode's title is its name
        self.assertEqual({r.band for r in lines}, {ted.key})  # hovered with their band
        self.assertEqual(ted.actions, (("ignore", "Ignore"), ("rename", "Rename")))
        self.assertEqual(rows.bands[ted.key], [items[2].id, items[0].id])
        self.assertEqual(rows.targets[items[2].id].parent.name, "Ted Lasso - Season 04")
        dune = bands(rows)[0]
        self.assertEqual((dune.subtitle, dune.dest), ("Film · 2021", "→ Dune (2021)"))
        film_line = rows.rows[rows.rows.index(dune) + 1]
        self.assertEqual((film_line.title, film_line.new), ("Dune (2021).mkv", True))
        self.assertEqual((film_line.faint, film_line.faint_label), ("Dune.2021.mkv", "Now:"))
        self.assertEqual(rows.targets[items[1].id], self.folder / "Dune (2021)" / "Dune (2021).mkv")
        self.assertEqual(rows.kinds, {"series": 3, "movies": 1})
        self.assertEqual(rows.counts, {"all": 4, "needs": 0, "ready": 4})

    def test_each_kind_has_its_own_rule(self) -> None:
        items = [
            episode(self.folder, "Ted.Lasso.S04E03.mkv", "Ted Lasso", 4, 3),
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021),
        ]
        rows = build(items, rule=rule("{t} {s00e00}", "{t} [{ny}]"))
        self.assertEqual(rows.targets[items[0].id].name, "Ted Lasso S04E03.mkv")
        self.assertEqual(rows.targets[items[1].id].name, "Dune [Dune (2021)].mkv")
        self.assertEqual([item_kind(i) for i in items], ["series", "movies"])

    def test_needs_you_comes_first_and_always_shows_its_buttons(self) -> None:
        items = [
            movie(self.folder, "Arrival.2016.mkv", "Arrival", 2016),
            unsure(episode(self.folder, "Shrinking.S03E01.1080p.mkv", "Shrinking", 3, 1)),
        ]
        rows = build(items)
        shrinking, arrival = bands(rows)
        self.assertEqual(shrinking.title, "Shrinking")
        self.assertEqual((shrinking.state, shrinking.state_text), ("needs", "Needs you"))
        self.assertEqual(
            shrinking.actions, (("ignore", "Ignore"), ("choose", "Choose…"), ("rename", "Rename"))
        )
        self.assertEqual((shrinking.subtitle, shrinking.dest), ("TMDB isn't sure · 1 file", ""))
        self.assertEqual(arrival.title, "Arrival")
        self.assertEqual(rows.needs, 1)
        self.assertTrue(needs_you(items[1]))
        self.assertFalse(needs_you(items[0]))

    def test_an_unsure_film_names_its_year(self) -> None:
        [band] = bands(build([unsure(movie(self.folder, "Dune.2021.mkv", "Dune", 2021))]))
        self.assertEqual(band.title, "Dune (2021)")

    def test_the_filter_shows_what_it_says_and_counts_everything(self) -> None:
        items = [
            unsure(movie(self.folder, "Dune.2021.mkv", "Dune", 2021)),
            movie(self.folder, "Heat.1995.mkv", "Heat", 1995),
            movie(self.folder, "Ran.1985.mkv", "Ran", 1985, status=DONE),
        ]
        for show, titles in (
            ("all", ["Dune (2021)", "Heat", "Ran"]),
            ("needs", ["Dune (2021)"]),
            ("ready", ["Heat"]),
        ):
            with self.subTest(show=show):
                rows = build(items, show=show)
                self.assertEqual([b.title for b in bands(rows)], titles)
                self.assertEqual(rows.counts, {"all": 3, "needs": 1, "ready": 1})
                self.assertEqual(rows.kinds, {"series": 0, "movies": 2})  # not renamed yet
        self.assertEqual(sections(build(items, show="needs")), [])  # done ones: only in All

    def test_a_band_not_done_shows_its_preset_chip(self) -> None:
        at = time.mktime((2026, 11, 1, 14, 5, 0, 0, 0, -1))
        items = [
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021, status=DONE, finished_at=at),
            movie(self.folder, "Ran.1985.mkv", "Ran", 1985),
        ]
        rows = build(items, chip=lambda item: f"{item.media.title} preset ▾")
        self.assertEqual([b.chip for b in bands(rows)], ["Ran preset ▾", ""])  # none when done
        self.assertEqual([b.chip for b in bands(build(items))], ["", ""])  # not asked

    def test_done_today_is_at_the_end_faded_with_undo(self) -> None:
        at = time.mktime((2026, 11, 1, 14, 5, 0, 0, 0, -1))
        items = [
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021, status=DONE, finished_at=at),
            movie(self.folder, "Heat.1995.1080p.mkv", "Heat", 1995, status=ItemStatus.SKIPPED),
            movie(self.folder, "Ran.1985.mkv", "Ran", 1985),
        ]
        rows = build(items)
        self.assertEqual([b.title for b in bands(rows)], ["Ran", "Dune", "Heat"])
        self.assertEqual(sections(rows), ["Done today"])
        heading = next(r for r in rows.rows if r.kind == "section")
        self.assertEqual(heading.subtitle, "2")
        self.assertLess(rows.rows.index(bands(rows)[0]), rows.rows.index(heading))
        done = bands(rows)[1:]
        self.assertTrue(all(b.actions == (("undo", "Undo"),) for b in done))
        self.assertTrue(all(b.faded and b.state == "done" for b in done))
        self.assertEqual([b.state_text for b in done], ["Renamed", "Renamed"])
        self.assertEqual([b.note for b in done], ["14:05", ""])
        lines = [r for r in rows.rows if r.kind == "item" and r.faded]
        self.assertEqual({r.faint for r in lines}, {"Dune.2021.mkv", "Heat.1995.1080p.mkv"})
        self.assertEqual({r.status for r in lines}, {"done", "skipped"})
        self.assertEqual(rows.needs, 0)

    def test_done_today_hides_yesterday(self) -> None:
        now = time.time()
        items = [
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021, status=DONE, finished_at=now),
            movie(
                self.folder, "Heat.1995.mkv", "Heat", 1995,
                status=DONE, finished_at=now - 2 * 24 * 3600,
            ),
        ]  # fmt: skip
        rows = build(items, today=date.today())
        self.assertEqual([b.title for b in bands(rows)], ["Dune"])
        self.assertEqual(rows.counts["all"], 1)
        self.assertEqual(len(bands(build(items))), 2)  # no day: all of them

    def test_a_band_with_several_seasons_names_them_and_their_shared_folder(self) -> None:
        items = [
            episode(self.folder, "VisionQuest.S01E01.mkv", "VisionQuest", 1, 1),
            episode(self.folder, "VisionQuest.S03E02.mkv", "VisionQuest", 3, 2),
        ]
        [band] = bands(build(items, rule=rule(SERIES_FOLDER)))
        self.assertEqual(band.subtitle, "Seasons 1, 3 · 2 episodes")
        self.assertEqual(band.dest, "→ VisionQuest › …")

    def test_seasons_in_a_row_are_a_range(self) -> None:
        items = [episode(self.folder, f"Show.S0{s}E01.mkv", "Show", s, 1) for s in (1, 2, 3)]
        [band] = bands(build(items))
        self.assertEqual(band.subtitle, "Seasons 1–3 · 3 episodes")
        self.assertEqual(band.dest, "→ the same folder")

    def test_a_film_band(self) -> None:
        two = [
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021),
            movie(self.folder, "Dune.2021.sample.mkv", "Dune", 2021),
        ]
        [band] = bands(build(two, rule=rule(movies=MOVIE_FOLDER)))
        self.assertEqual(band.subtitle, "Film · 2021 · 2 files")
        self.assertEqual(band.badge, "Dune")
        [band] = bands(build([movie(self.folder, "Ran.mkv", "Ran", None)]))
        self.assertEqual(band.subtitle, "Film")

    def test_episode_lines_use_the_episode_title_and_keep_their_code(self) -> None:
        # Found in a real-window screenshot: with "{ny}" (Auto) the lines lost their S17E03.
        name = "Gold Rush S17E03 Diagnosing Rick 1080p AMZN WEB-DL.mkv"
        item = episode(self.folder, name, "Gold Rush", 17, 3)
        [line] = [r for r in build([item], rule=rule("{ny}")).rows if r.kind == "item"]
        self.assertEqual(line.code, "S17E03")
        self.assertEqual(line.title, "Gold Rush.mkv")
        named = episode(
            self.folder, "Gold.Rush.S17E04.mkv", "Gold Rush", 17, 4, episode_title="Gold"
        )
        [line] = [r for r in build([named]).rows if r.kind == "item"]
        self.assertEqual(line.title, "Gold")

    def test_watched_files_go_to_their_library(self) -> None:
        downloads, series = self.tmp / "dl", self.tmp / "lib" / "Series"
        item = episode(
            downloads, "VisionQuest.S01E02.Theseus.mkv", "VisionQuest", 1, 2, watched=downloads
        )
        rows = build([item], naming=lambda _item: (SERIES_FOLDER, series))
        [band] = bands(rows)
        self.assertEqual(band.dest, "→ Series › VisionQuest › VisionQuest - Season 01")
        self.assertEqual(rows.targets[item.id].parents[1], series / "VisionQuest")
        self.assertEqual((band.state, band.state_text), ("ready", "Ready"))
        self.assertEqual(rows.needs, 0)

    def test_on_trial_watched_files_would_rename(self) -> None:
        downloads = self.tmp / "dl"
        watched = movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads)
        dropped = movie(self.folder, "Heat.1995.mkv", "Heat", 1995)
        rows = build([watched, dropped], trial=True)
        dune, heat = bands(rows)
        self.assertEqual((dune.state, dune.state_text), ("would", "Would rename"))
        self.assertEqual(heat.state, "ready")  # a dropped file is renamed with Forge
        self.assertEqual(rows.counts, {"all": 2, "needs": 0, "ready": 1})
        self.assertEqual([b.title for b in bands(build([watched], show="ready", trial=True))], [])
        self.assertEqual(item_state(watched, trial=False), "ready")

    def test_held_files_need_you_with_their_reason(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.mkv", "Dune", None, watched=downloads)
        item.status, item.message = ItemStatus.HELD, "No year in the name: is it a movie?"
        rows = build([item])
        [band] = bands(rows)
        self.assertEqual(band.state, "needs")
        self.assertEqual(band.subtitle, "No year in the name: is it a movie? · 1 file")
        self.assertEqual(band.actions[-1], ("rename", "Rename"))
        item.message = ""
        [band] = bands(build([item]))
        self.assertTrue(band.subtitle.startswith("Waiting for you · "))

    def test_a_failed_rename_needs_you(self) -> None:
        item = movie(self.folder, "Dune.2021.mkv", "Dune", 2021, status=ItemStatus.ERROR)
        item.message = "Access is denied"
        [band] = bands(build([item]))
        self.assertEqual((band.state, band.subtitle), ("needs", "Access is denied · 1 file"))

    def test_keys_never_clash_with_item_ids(self) -> None:
        items = [
            episode(self.folder, "A.S01E01.mkv", "A", 1, 1),
            movie(self.folder, "B.2001.mkv", "B", 2001, status=DONE),
        ]
        rows = build(items)
        heads = [r.key for r in rows.rows if r.kind in ("section", "band")]
        self.assertTrue(all(k < 0 for k in heads))
        self.assertEqual(len({r.key for r in rows.rows}), len(rows.rows))
        self.assertEqual(sorted(rows.bands), sorted(r.key for r in bands(rows)))

    def test_episode_code(self) -> None:
        cases = (
            (episode(self.folder, "a.mkv", "A", 4, 3), "S04E03"),
            (episode(self.folder, "b.mkv", "A", 1, 2, end=3), "S01E02-E03"),
            (episode(self.folder, "c.mkv", "A", 1, 2, end=2), "S01E02"),
            (movie(self.folder, "d.mkv", "D", 2001), ""),
        )
        for item, code in cases:
            with self.subTest(name=item.path.name):
                self.assertEqual(episode_code(item), code)

    def test_empty_texts(self) -> None:
        self.assertEqual(
            empty_texts("needs", others=True),
            ("Nothing needs you", "Everything in the queue can be renamed."),
        )
        self.assertEqual(
            empty_texts("ready", others=True),
            ("Nothing is ready yet", "The files in the queue need you first."),
        )
        drop = "Drop video files or folders here"
        self.assertEqual(empty_texts("needs", others=False)[0], drop)
        self.assertEqual(empty_texts("all", others=True)[0], drop)
        self.assertIn("buttons above", empty_texts("all", others=False)[1])

    def test_shared_folder(self) -> None:
        shared = queue_rows._shared_folder  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(shared([("A", "B"), ("A", "B")]), "A › B")
        self.assertEqual(shared([(), ()]), "the same folder")
        self.assertEqual(shared([("A", "B"), ("A", "C")]), "A › …")
        self.assertEqual(shared([("A",), ("C",)]), "several folders")

    def test_dutch(self) -> None:
        i18n.set_language("nl")
        self.addCleanup(i18n.set_language, "en")
        items = [
            episode(self.folder, "Show.S01E01.mkv", "Show", 1, 1),
            episode(self.folder, "Show.S02E01.mkv", "Show", 2, 1),
            movie(self.folder, "Dune.2021.mkv", "Dune", 2021, status=DONE),
            unsure(movie(self.folder, "Heat.1995.mkv", "Heat", 1995)),
        ]
        rows = build(items)
        self.assertEqual(sections(rows), [i18n.t(DONE_TODAY)])
        self.assertEqual(i18n.t(DONE_TODAY), "Vandaag klaar")
        heat, show, dune = bands(rows)
        self.assertEqual(heat.state_text, "Wacht op jou")
        self.assertEqual(show.state_text, "Klaar")
        self.assertEqual(dune.state_text, "Hernoemd")
        self.assertEqual(show.subtitle, "Seizoenen 1–2 · 2 afleveringen")
        self.assertEqual(show.dest, "→ dezelfde map")
        self.assertEqual(show.actions, (("ignore", "Negeren"), ("rename", "Hernoemen")))
        self.assertEqual(dune.actions, (("undo", "Ongedaan maken"),))
        self.assertEqual(dune.subtitle, "Film · 2021")
        self.assertEqual({r.faint_label for r in rows.rows if r.kind == "item"}, {"Nu:"})


class BuildRowsTests(ForgeTestCase):
    """The right-click confirm window's list: rows grouped by destination folder."""

    def test_groups_by_destination_folder(self) -> None:
        folder = self.tmp / "in"
        items = [
            episode(folder, "Ted.Lasso.S04E03.mkv", "Ted Lasso", 4, 3),
            episode(folder, "Ted.Lasso.S04E05.mkv", "Ted Lasso", 4, 5),
            movie(folder, "Dune.2021.mkv", "Dune", 2021),
        ]
        rows, targets = build_rows(items, "{ny}")
        groups = [r for r in rows if r.kind == "group"]
        self.assertEqual(
            [(g.title, g.subtitle) for g in groups],
            [("📺 Ted Lasso", "2 episodes"), ("📁 Same folder", "1 file")],
        )
        self.assertTrue(all(g.key < 0 for g in groups))
        self.assertEqual(targets[items[2].id], folder / "Dune (2021).mkv")
        first = rows[1]
        self.assertEqual(
            (first.kind, first.subtitle, first.badge), ("item", items[0].path.name, "Ted Lasso")
        )

    def test_highlights_the_episode_code(self) -> None:
        folder = self.tmp / "in"
        items = [episode(folder, "Ted.Lasso.S04E03.mkv", "Ted Lasso", 4, 3)]
        rows, _ = build_rows(items, SERIES_FOLDER)
        group, line = rows
        self.assertEqual(group.title, "📺 Ted Lasso - Season 04")
        self.assertEqual(group.subtitle, "1 episode")
        self.assertIsNotNone(line.highlight)
        start, end = line.highlight or (0, 0)
        self.assertEqual(line.title[start:end], "S04E03")
