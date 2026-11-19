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
from app.forge.core.replace_facts import ReplaceFacts
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.models.tmdb import TMDBTitle
from app.forge.ui import queue_rows
from app.forge.ui.queue_rows import (
    ACCEPT_ACTION,
    DONE_TODAY,
    EXPLORER_ACTION,
    REPLACE_ACTION,
    QueueRows,
    WatchedLook,
    build_queue_rows,
    build_rows,
    empty_texts,
    episode_code,
    item_kind,
    item_state,
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
    """TMDB isn't sure, and has no guess."""
    item.status = ItemStatus.NEEDS_CHOICE
    item.resolution = Resolution(media=ResolvedMedia.offline(item.parsed, MatchState.UNCERTAIN))
    return item


def guessed(item: QueueItem, title: str, year: int | None, tmdb_id: int = 7) -> QueueItem:
    """TMDB isn't sure; its top result is `title` (`year`)."""
    item.status = ItemStatus.NEEDS_CHOICE
    media = ResolvedMedia.offline(item.parsed, MatchState.UNCERTAIN).model_copy(
        update={"title": title, "year": year, "tmdb_id": tmdb_id}
    )
    result = TMDBTitle(id=tmdb_id, title=title, date=f"{year}-01-01" if year else None)
    item.resolution = Resolution(media=media, candidates=(result,))
    return item


READY_ACTIONS = (
    ("explorer", "📂 Show file"),
    ("choose", "Wrong show?"),
    ("ignore", "Leave it alone ▾"),
    ("rename", "Forge this"),
)


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
        self.assertEqual(ted.dest, "📁 Stays here, in Ted Lasso › Ted Lasso - Season 04")
        self.assertEqual((ted.state, ted.state_text), ("ready", "Ready to forge"))
        lines = rows.rows[rows.rows.index(ted) + 1 :]
        self.assertEqual([r.code for r in lines], ["S04E03", "S04E05"])  # by episode
        self.assertEqual(lines[0].faint, "Ted.Lasso.S04E03.1080p.WEB.mkv")
        # The current name says so, so it isn't taken for the new one.
        self.assertEqual({r.faint_label for r in lines}, {"Current name:"})
        self.assertEqual({r.new for r in lines}, {False})  # an episode's title is its name
        self.assertEqual({r.band for r in lines}, {ted.key})  # hovered with their band
        self.assertEqual(ted.actions, READY_ACTIONS)
        self.assertEqual(ted.quiet, ("explorer", "choose", "ignore"))  # links; Forge this a button
        self.assertEqual(
            ted.tips,
            (
                ("explorer", "Open in Explorer"),
                ("ignore", "Takes it off the list. Sorting won't touch it again."),
            ),
        )
        self.assertEqual(ted.ask, "")  # nothing to ask
        self.assertEqual(rows.bands[ted.key], [items[2].id, items[0].id])
        self.assertEqual(rows.targets[items[2].id].parent.name, "Ted Lasso - Season 04")
        dune = bands(rows)[0]
        self.assertEqual((dune.subtitle, dune.dest), ("Film · 2021", "📁 Stays here, in Dune (2021)"))
        film_line = rows.rows[rows.rows.index(dune) + 1]
        self.assertEqual((film_line.title, film_line.new), ("Dune (2021).mkv", True))
        self.assertEqual((film_line.faint, film_line.faint_label), ("Dune.2021.mkv", "Current name:"))
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
        # Not one guess: which show is it?
        self.assertEqual(shrinking.title, "Shrinking.S03E01.1080p.mkv")  # what the file says
        self.assertEqual((shrinking.state, shrinking.state_text), ("needs", "Not sure which show"))
        self.assertEqual((shrinking.ask, shrinking.ask_bold), ("Which show is this?", None))
        self.assertEqual(
            shrinking.actions,
            (
                ("explorer", "📂 Show file"),
                ("ignore", "Leave it alone ▾"),
                ("choose", "Pick the show…"),
            ),
        )
        self.assertEqual(shrinking.quiet, ("explorer", "ignore"))
        self.assertEqual(shrinking.dest, "")
        self.assertEqual(arrival.title, "Arrival")
        self.assertEqual(rows.needs, 1)

    def test_an_unsure_film_asks_whether_it_is_the_guess(self) -> None:
        item = guessed(movie(self.folder, "Heart.of.the.Beast.2026.2160p.mkv", "Heart", 2026),
                       "Heart of the Beast", 2026)  # fmt: skip
        [band] = bands(build([item]))
        self.assertEqual(band.title, "Heart.of.the.Beast.2026.2160p.mkv")
        self.assertEqual(band.state_text, "Not sure which film")
        self.assertEqual(band.ask, "Is this Heart of the Beast (2026)?")
        self.assertEqual(band.ask[slice(*band.ask_bold or (0, 0))], "Heart of the Beast (2026)")
        self.assertEqual(band.ask_more, "The file name and the film database don't fully agree.")
        self.assertEqual(
            band.actions,
            (
                ("explorer", "📂 Show file"),
                ("ignore", "Leave it alone ▾"),
                ("choose", "No, pick the right film…"),
                (ACCEPT_ACTION, "Yes, forge it"),
            ),
        )
        self.assertEqual(band.quiet, ("explorer", "ignore"))  # the answers are buttons
        rows = build([item])
        self.assertEqual(rows.guesses, {bands(rows)[0].key: 7})  # the id Yes takes

    def test_an_unsure_show_asks_by_its_name(self) -> None:
        item = guessed(episode(self.folder, "Shrinking.S03E01.mkv", "Shrinking", 3, 1),
                       "Shrinking", 2023)  # fmt: skip
        [band] = bands(build([item]))
        self.assertEqual((band.state_text, band.ask), ("Not sure which show", "Is this Shrinking?"))
        self.assertEqual(band.ask_more, "The file name and the show database don't fully agree.")
        self.assertEqual(band.actions[-2:], (
            ("choose", "No, pick the right show…"), (ACCEPT_ACTION, "Yes, forge it"),
        ))  # fmt: skip

    def test_a_download_held_for_its_match_asks_too(self) -> None:
        downloads = self.tmp / "dl"
        item = guessed(movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads),
                       "Dune: Part One", 2021)  # fmt: skip
        item.status, item.message = ItemStatus.HELD, "TMDB isn't sure which one it is."
        [band] = bands(build([item]))
        self.assertEqual(band.ask, "Is this Dune: Part One (2021)?")
        self.assertEqual(band.actions[-1], (ACCEPT_ACTION, "Yes, forge it"))
        missing = movie(downloads, "Home.Video.mkv", "Home Video", None, watched=downloads)
        missing.status, missing.message = ItemStatus.HELD, "Not found at TMDB."
        missing.match_problem = True
        [band] = bands(build([missing]))
        self.assertEqual((band.ask, band.ask_more), ("Which film is this?", "Not found at TMDB."))
        self.assertEqual(band.actions[-1], ("choose", "Pick the film…"))

    def test_the_filter_shows_what_it_says_and_counts_everything(self) -> None:
        items = [
            unsure(movie(self.folder, "Dune.2021.mkv", "Dune", 2021)),
            movie(self.folder, "Heat.1995.mkv", "Heat", 1995),
            movie(self.folder, "Ran.1985.mkv", "Ran", 1985, status=DONE),
        ]
        for show, titles in (
            ("all", ["Dune.2021.mkv", "Heat", "Ran"]),
            ("needs", ["Dune.2021.mkv"]),
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
        self.assertEqual([b.chip_caption for b in bands(rows)], ["Name style", ""])
        tips = [dict(b.tips).get("preset") for b in bands(rows)]
        self.assertEqual(tips, ["Ran (1985).mkv", None])  # the name this band's file gets
        self.assertEqual([b.chip for b in bands(build(items))], ["", ""])  # not asked

    def test_the_chip_tip_shows_the_whole_new_name(self) -> None:
        item = movie(self.folder, "Heart.of.the.Beast.2026.mkv", "Heart of the Beast", 2026)
        [band] = bands(build([item], rule=rule(movies=MOVIE_FOLDER), chip=lambda _i: "Movie ▾"))
        self.assertEqual(
            dict(band.tips)["preset"], "Heart of the Beast (2026)\\Heart of the Beast (2026).mkv"
        )

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
        self.assertTrue(
            all(b.actions == (("explorer", "📂 Show file"), ("undo", "Undo")) for b in done)
        )
        self.assertTrue(all(b.quiet == ("explorer",) for b in done))
        self.assertTrue(all(b.faded and b.state == "done" for b in done))
        self.assertEqual([b.state_text for b in done], ["Forged 14:05", "Forged"])
        self.assertEqual([b.note for b in done], ["", ""])  # the time is in the status
        lines = [r for r in rows.rows if r.kind == "item" and r.faded]
        self.assertEqual({r.faint for r in lines}, {"Dune.2021.mkv", "Heat.1995.1080p.mkv"})
        self.assertEqual({r.status for r in lines}, {"done", "skipped"})
        self.assertEqual(rows.needs, 0)
        self.assertEqual(rows.waiting, 1)  # the one still to rename: renamed files wait for nothing

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
        self.assertEqual(band.dest, "📁 Stays here, in VisionQuest › …")

    def test_a_new_season_starts_under_a_divider(self) -> None:
        items = [
            episode(self.folder, f"Show.S0{s}E0{e}.mkv", "Show", s, e)
            for s, e in ((2, 1), (1, 2), (1, 1), (3, 1))
        ]
        lines = [r for r in build(items).rows if r.kind == "item"]
        self.assertEqual([r.code for r in lines], ["S01E01", "S01E02", "S02E01", "S03E01"])
        self.assertEqual([r.divider for r in lines], ["", "", "Season 2", "Season 3"])
        # One season: no divider; films never.
        one = [episode(self.folder, f"Show.S01E0{e}.mkv", "Show", 1, e) for e in (1, 2)]
        self.assertFalse(any(r.divider for r in build(one).rows))
        self.assertFalse(any(r.divider for r in build([movie(self.folder, "Ran.mkv", "Ran", None)]).rows))

    def test_seasons_in_a_row_are_a_range(self) -> None:
        items = [episode(self.folder, f"Show.S0{s}E01.mkv", "Show", s, 1) for s in (1, 2, 3)]
        [band] = bands(build(items))
        self.assertEqual(band.subtitle, "Seasons 1–3 · 3 episodes")
        self.assertEqual(band.dest, "📁 Stays in its folder")

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
        # The rule doesn't say which episode: the name says it anyway (formatter.episode_rule).
        self.assertEqual(line.title, "Gold Rush - S17E03 - Episode 03.mkv")
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
        rows = build([item], naming=lambda _item: WatchedLook(SERIES_FOLDER, series, True, "", ""))
        [band] = bands(rows)
        self.assertEqual(band.dest, "📦 Moves to Series › VisionQuest › VisionQuest - Season 01")
        self.assertEqual(rows.targets[item.id].parents[1], series / "VisionQuest")
        self.assertEqual((band.state, band.state_text), ("ready", "Ready to forge"))
        self.assertEqual(rows.needs, 0)
        self.assertEqual(rows.waiting, 1)  # no decision to make, but Forge still to click

    def test_the_band_says_which_rule_took_it(self) -> None:
        downloads = self.tmp / "dl"
        film = movie(downloads, "Heart.of.the.Beast.2026.2160p.mkv", "Heart of the Beast", 2026,
                     watched=downloads)  # fmt: skip
        nas = Path(r"\\NAS\Movies 4K")  # a share root: named by its share
        rows = build(
            [film], naming=lambda i: WatchedLook("{ny}/{ny}", nas, True, "4K films", "")
        )
        [band] = bands(rows)
        self.assertEqual(band.dest, "📦 Moves to Movies 4K › Heart of the Beast (2026)")
        self.assertEqual(band.rule, "Rule: 4K films")
        self.assertEqual(band.state_text, "Ready to forge")
        self.assertEqual(rows.targets[film.id].parent, nas / "Heart of the Beast (2026)")
        flat = build([film], naming=lambda i: WatchedLook("{ny}", nas, True, "4K films", ""))
        self.assertEqual(bands(flat)[0].dest, "📦 Moves to Movies 4K")
        dropped = movie(self.folder, "Heat.1995.mkv", "Heat", 1995)
        [band] = bands(build([dropped], naming=lambda i: WatchedLook("{ny}", nas, True, "X", "")))
        self.assertEqual(band.rule, "")  # a dropped file has no rule
        [band] = bands(build([film], naming=lambda i: WatchedLook("{ny}", nas, True, "", "")))
        self.assertEqual(band.rule, "")  # no rule takes it

    def test_a_manual_rule_waits_for_you(self) -> None:
        downloads = self.tmp / "dl"
        item = episode(downloads, "The.Long.Road.S02E05.mkv", "The Long Road", 2, 5,
                       watched=downloads)  # fmt: skip
        look = WatchedLook(SERIES_FLAT, self.tmp / "Series", True, "Shows", "manual")
        rows = build([item], naming=lambda i: look)
        [band] = bands(rows)
        self.assertEqual((band.state, band.state_text),
                         ("ready", "Waits for you: rule “Shows” is Manual"))  # fmt: skip
        self.assertEqual(band.rule, "Rule: Shows")
        self.assertEqual(rows.counts["ready"], 1)  # still ready: Forge this does it
        item.status, item.message = ItemStatus.HELD, "No year in the name: is it a movie?"
        [band] = bands(build([item], naming=lambda i: look))
        self.assertEqual(band.state_text, "Waiting for you")  # one that needs you says why

    def test_a_changed_rule_says_so(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads)
        look = WatchedLook(MOVIE_FLAT, self.tmp / "Movies", True, "Films", "changed")
        [band] = bands(build([item], naming=lambda i: look))
        self.assertEqual((band.state, band.state_text),
                         ("ready", "Its rule was changed: check it"))  # fmt: skip

    def test_a_stays_rule_is_sorted_in_its_folder(self) -> None:
        courses = Path(r"D:\Courses")
        item = movie(courses, "Intro.to.Watercolour.2024.mp4", "Intro to Watercolour", 2024,
                     watched=courses)  # fmt: skip
        look = WatchedLook(MOVIE_FOLDER, courses, False, "Courses", "")
        rows = build([item], naming=lambda i: look)
        [band] = bands(rows)
        self.assertEqual((band.dest, band.dest_look), ("📁 Sorted in Courses", "stays"))
        self.assertEqual(band.rule, "Rule: Courses")
        named = "Intro to Watercolour (2024)"
        self.assertEqual(rows.targets[item.id], courses / named / f"{named}.mp4")
        item.status = DONE
        item.finished_at = time.time()
        [band] = bands(build([item], naming=lambda i: look, today=date.today()))
        self.assertEqual(band.dest, "📁 Was sorted in Courses")  # done: past

    def test_a_rule_whose_preset_is_gone_says_so(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads)
        look = WatchedLook(MOVIE_FLAT, self.tmp / "Movies", True, "Films", "no_preset")
        [band] = bands(build([item], naming=lambda i: look))
        self.assertEqual((band.state, band.state_text),
                         ("ready", "⚠ The preset of rule “Films” is gone: choose another"))  # fmt: skip

    def test_a_file_no_rule_takes_says_so_and_names_no_destination(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads)
        look = WatchedLook(MOVIE_FLAT, downloads, False, "", "unwatched")
        [band] = bands(build([item], naming=lambda i: look))
        self.assertEqual(band.state_text, "It isn't in a watched folder any more.")
        self.assertEqual((band.dest, band.rule), ("", ""))  # never "Choose your Movies folder"
        # Still in a watched folder, but no rule fits it: its own plain words.
        look = WatchedLook(MOVIE_FLAT, downloads, False, "", "no_rule")
        [band] = bands(build([item], naming=lambda i: look))
        self.assertEqual(band.state_text, "No rule takes this file.")
        self.assertEqual((band.dest, band.rule), ("", ""))

    def test_watched_files_are_ready_like_any_other(self) -> None:
        downloads = self.tmp / "dl"
        watched = movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads)
        dropped = movie(self.folder, "Heat.1995.mkv", "Heat", 1995)
        rows = build([watched, dropped])
        dune, heat = bands(rows)
        self.assertEqual((dune.state, dune.state_text), ("ready", "Ready to forge"))
        self.assertEqual(heat.state, "ready")
        self.assertEqual(rows.counts, {"all": 2, "needs": 0, "ready": 2})
        self.assertEqual([b.title for b in bands(build([watched], show="ready"))], ["Dune"])
        self.assertEqual(item_state(watched), "ready")

    def test_held_files_need_you_with_their_reason(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.mkv", "Dune", None, watched=downloads)
        item.status, item.message = ItemStatus.HELD, "No year in the name: is it a movie?"
        rows = build([item])
        [band] = bands(rows)
        self.assertEqual((band.state, band.state_text), ("needs", "Waiting for you"))
        self.assertEqual(band.subtitle, "No year in the name: is it a movie? · 1 file")
        self.assertEqual(band.ask, "")  # the reason is its subtitle
        self.assertEqual(
            band.actions,
            (
                ("explorer", "📂 Show file"),
                ("choose", "Wrong film?"),
                ("ignore", "Leave it alone ▾"),
                ("rename", "Forge this"),
            ),
        )
        self.assertEqual(band.quiet, ("explorer", "choose"))
        item.message = ""
        [band] = bands(build([item]))
        self.assertTrue(band.subtitle.startswith("Waiting for you · "))

    def test_only_a_band_already_in_the_library_offers_to_replace_it(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.2021.mkv", "Dune", 2021, watched=downloads)
        item.status, item.message = ItemStatus.HELD, "Already in the library."
        item.in_library = True
        [band] = bands(build([item]))
        self.assertEqual((band.state, band.state_text), ("needs", "Already in your library"))
        self.assertEqual((band.ask, band.ask_more), ("You already have this film.", ""))
        self.assertEqual(
            band.actions,
            (
                (EXPLORER_ACTION, "📂 Show file"),
                ("choose", "Wrong film?"),
                ("ignore", "Keep the one I have ▾"),
                (REPLACE_ACTION, "Use this one instead"),
            ),
        )  # no Forge this: it would make a second copy
        self.assertEqual(band.quiet, (EXPLORER_ACTION, "choose"))
        self.assertEqual(
            dict(band.tips)[REPLACE_ACTION],
            "The one in your library moves to the Duplicates folder. Nothing is deleted; Undo "
            "puts both back.",
        )
        item.message, item.in_library = "No year in the name: is it a movie?", False
        [band] = bands(build([item]))
        self.assertNotIn(REPLACE_ACTION, [key for key, _label in band.actions])

    def test_a_band_already_in_the_library_tells_both_copies(self) -> None:
        downloads = self.tmp / "dl"
        item = movie(downloads, "Dune.2021.2160p.mkv", "Dune", 2021, watched=downloads)
        item.status, item.in_library = ItemStatus.HELD, True
        facts = ReplaceFacts(
            library_name="Dune (2021).mkv",
            library_size=round(4.2 * 1024**3),
            library_quality="1080p",
            download_size=round(18.6 * 1024**3),
            download_quality="2160p",
        )
        [band] = bands(build([item], facts=lambda _item: facts))
        self.assertEqual(
            band.ask_more, "In your library: 1080p · 4.2 GB — this download: 2160p · 18.6 GB"
        )
        [band] = bands(build([item], facts=lambda _item: None))  # not read yet
        self.assertEqual(band.ask_more, "")

    def test_a_show_with_several_episodes_in_the_library_counts_them(self) -> None:
        downloads = self.tmp / "dl"
        items = [
            episode(downloads, f"Show.S01E0{n}.mkv", "Show", 1, n, watched=downloads)
            for n in (1, 2, 3)
        ]
        for item in items[:2]:
            item.status, item.in_library = ItemStatus.HELD, True
        items[2].status, items[2].message = ItemStatus.HELD, "TMDB doesn't know this episode."
        rows = build(items, facts=lambda _item: ReplaceFacts(download_size=1))
        [band] = bands(rows)
        self.assertEqual((band.ask, band.ask_more), ("You already have 2 of these episodes.", ""))
        # Keep the one I have leaves alone only the files in the library; no facts for two.
        self.assertEqual(rows.keeps, {band.key: [items[0].id, items[1].id]})
        self.assertEqual(rows.held, {})
        self.assertEqual(band.actions[-2][1], "Keep the one I have ▾")
        items[1].status, items[1].in_library = ItemStatus.HELD, False
        rows = build(items, facts=lambda _item: ReplaceFacts(download_size=5 * 1024**2))
        [band] = bands(rows)
        self.assertEqual(band.ask, "You already have this episode.")
        self.assertEqual(rows.keeps, {band.key: [items[0].id]})
        self.assertEqual(rows.held, {items[0].id: (items[0], rows.targets[items[0].id])})
        self.assertEqual(band.ask_more, "This download: 5 MB")

    def test_a_failed_rename_needs_you(self) -> None:
        item = movie(self.folder, "Dune.2021.mkv", "Dune", 2021, status=ItemStatus.ERROR)
        item.message = "Access is denied"
        [band] = bands(build([item]))
        self.assertEqual((band.state, band.subtitle), ("needs", "Access is denied · 1 file"))
        self.assertEqual(band.state_text, "Couldn't forge")
        self.assertEqual(band.actions[-2:], (("ignore", "Leave it alone ▾"), ("rename", "Try again")))

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
        self.assertEqual(shared([(), ()]), "")
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
        self.assertEqual(heat.state_text, "Niet zeker welke film")
        self.assertEqual(heat.ask, "Welke film is dit?")
        self.assertEqual(show.state_text, "Klaar om te forgen")
        self.assertEqual(dune.state_text, "Geforged")
        self.assertEqual(show.subtitle, "Seizoenen 1–2 · 2 afleveringen")
        self.assertEqual(show.dest, "📁 Blijft in zijn map")
        self.assertEqual(
            show.actions,
            (
                ("explorer", "📂 Bestand tonen"),
                ("choose", "Verkeerde serie?"),
                ("ignore", "Met rust laten ▾"),
                ("rename", "Dit forgen"),
            ),
        )
        self.assertEqual(dune.actions, (("explorer", "📂 Bestand tonen"), ("undo", "Ongedaan maken")))
        self.assertEqual(dune.tips, (("explorer", "Openen in Verkenner"),))
        self.assertEqual(dune.subtitle, "Film · 2021")
        self.assertEqual({r.faint_label for r in rows.rows if r.kind == "item"}, {"Huidige naam:"})


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
