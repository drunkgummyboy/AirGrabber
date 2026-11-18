"""Runs planned per rule (app/forge/core/auto.py's `library_runs` / `owner_runs` / `replace_runs`
with `rules=`, app/forge/core/rules.py's `route_for_item` / `route_problem`): one run per rule and
kind, a rule's own destination, a Stays rule sorting inside its watched folder (never cleaning the
folders it built), "already there" and "Use this one instead" at any destination, a rule's extras,
a library folder not chosen holding only its rule's files, a deleted rule's snapshot, and a gone
preset without a choice. Pure: `exists` fakes, no disk."""

from __future__ import annotations

from pathlib import Path

from app.forge.core.auto import (
    ALREADY_THERE,
    PRESET_GONE_HOLD,
    NamingChoice,
    library_runs,
    owner_runs,
    replace_runs,
)
from app.forge.core.matcher import Resolution
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.core.rules import (
    NO_MOVIES_HOLD,
    NO_RULE_TAKES,
    NOT_WATCHED,
    route_for_item,
    route_problem,
)
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.i18n import t
from tests.forge.base import ForgeTestCase

D = r"C:\dl"
SERIES = Path(r"C:\lib\Series")
MOVIES = Path(r"C:\lib\Movies")
BIG = r"C:\lib\Movies 4K"
SETTINGS = ForgeSettings(series_folder=str(SERIES), movies_folder=str(MOVIES))


def movie(
    item_id: int,
    *,
    rule: Rule | None = None,
    path: Path | None = None,
    watched: Path | None = Path(D),
) -> QueueItem:
    name = "Dune.2021.2160p.mkv"
    parsed = ParsedMedia(source_name=name, kind=MediaKind.MOVIE, title="Dune", year=2021)
    media = ResolvedMedia(
        parsed=parsed,
        title="Dune",
        year=2021,
        episode_title=None,
        match_state=MatchState.CONFIDENT,
        tmdb_id=2,
    )
    return QueueItem(
        item_id,
        path or Path(D, name),
        None,
        parsed,
        Resolution(media=media),
        ItemStatus.READY,
        watched=watched,
        rule=rule,
    )


def episode(
    item_id: int,
    *,
    rule: Rule | None = None,
    path: Path | None = None,
    watched: Path | None = Path(D),
) -> QueueItem:
    name = "VisionQuest.S01E02.mkv"
    parsed = ParsedMedia(
        source_name=name, kind=MediaKind.EPISODE, title="VisionQuest", season=1, episode=2
    )
    media = ResolvedMedia(
        parsed=parsed,
        title="VisionQuest",
        year=2025,
        episode_title="Theseus",
        episode_known=True,
        match_state=MatchState.CONFIDENT,
        tmdb_id=1,
    )
    return QueueItem(
        item_id,
        path or Path(D, "VisionQuest.S01E02", name),
        None,
        parsed,
        Resolution(media=media),
        ItemStatus.READY,
        watched=watched,
        rule=rule,
    )


def never(path: Path) -> bool:
    return False


SORTED = Path(D, "VisionQuest (2025)", "VisionQuest (2025) - Season 01")
SORTED_FILE = SORTED / "VisionQuest (2025) - S01E02 - Theseus.mkv"
COURSES = Rule(id=5, folder=D, stays=True, shows=RuleTarget(preset="Series (Folder)"))


class RuleRunsTest(ForgeTestCase):
    def test_one_run_per_rule_and_kind(self) -> None:
        films4k = Rule(
            id=1,
            folder=D,
            take="films",
            quality="4k",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=BIG),
        )
        films = Rule(id=2, folder=D, take="films")
        runs = library_runs(
            [movie(1, rule=films4k), movie(2, rule=films)],
            SETTINGS,
            DEFAULT_PRESETS,
            never,
            rules=[films4k, films],
        )
        assert sorted((r.rule_id, r.kind) for r in runs) == [
            (1, MediaKind.MOVIE),
            (2, MediaKind.MOVIE),
        ]
        big = next(r for r in runs if r.rule_id == 1)
        assert big.plan.files[0].target == Path(BIG, "Dune (2021)", "Dune (2021).mkv")
        library = next(r for r in runs if r.rule_id == 2)
        assert library.plan.files[0].target == MOVIES / "Dune (2021)" / "Dune (2021).mkv"
        assert all(r.held == {} for r in runs)

    def test_a_stays_rule_sorts_inside_the_watched_folder(self) -> None:
        item = episode(1, path=Path(D, "VisionQuest.S01E02.mkv"), rule=COURSES)
        [run] = library_runs([item], SETTINGS, DEFAULT_PRESETS, never, rules=[COURSES])
        assert run.rule_id == 5 and run.preset.name == "Series (Folder)"
        assert run.plan.files[0].target == SORTED_FILE
        route = route_for_item(item, [COURSES], SETTINGS, DEFAULT_PRESETS)
        assert route is not None and not route.moves and route.root == Path(D)

    def test_a_stays_rule_never_cleans_the_folders_it_built(self) -> None:
        # A release folder named for it is still cleaned away once its file has gone.
        [run] = library_runs(
            [episode(1, rule=COURSES)], SETTINGS, DEFAULT_PRESETS, never, rules=[COURSES]
        )
        assert run.plan.files[0].target == SORTED_FILE
        assert [lo.folder for lo in run.plan.leftovers] == [Path(D, "VisionQuest.S01E02")]
        # A file dropped into a folder it built earlier: that folder is the result, not a leftover.
        inner = Path(D, "VisionQuest (2025)", "VisionQuest.S01E02.mkv")
        built = episode(1, path=inner, rule=COURSES)
        [run] = library_runs([built], SETTINGS, DEFAULT_PRESETS, never, rules=[COURSES])
        assert run.plan.files[0].target == SORTED_FILE
        assert run.plan.leftovers == ()
        # And a folder `keep` names never goes.
        [run] = library_runs(
            [episode(1, rule=COURSES)],
            SETTINGS,
            DEFAULT_PRESETS,
            never,
            rules=[COURSES],
            keep=lambda folder: folder == Path(D, "VisionQuest.S01E02"),
        )
        assert run.plan.files and run.plan.leftovers == ()

    def test_already_there_for_a_stays_rule_means_inside_the_watched_folder(self) -> None:
        item = episode(1, path=Path(D, "VisionQuest.S01E02.mkv"), rule=COURSES)
        [run] = library_runs(
            [item], SETTINGS, DEFAULT_PRESETS, lambda p: p == SORTED_FILE, rules=[COURSES]
        )
        assert run.plan.files == ()
        assert run.held == {1: t(ALREADY_THERE)} and run.in_library == frozenset({1})

    def test_use_this_one_instead_at_a_rule_destination(self) -> None:
        big = self.tmp / "Movies 4K"  # a temporary folder, not a share root
        rule = Rule(
            id=1,
            folder=D,
            take="films",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=str(big)),
        )
        there = big / "Dune (2021)" / "Dune (2021).mkv"
        [run] = replace_runs(
            [movie(1, rule=rule)], SETTINGS, DEFAULT_PRESETS, lambda p: p == there, rules=[rule]
        )
        assert run.plan.files[0].target == there
        [aside] = run.plan.aside
        assert aside.source == there
        aside_folder = self.tmp / "Duplicates" / "Movies 4K" / "Dune (2021)"
        assert aside.target == aside_folder / "Dune (2021).mkv"

    def test_use_this_one_instead_for_a_stays_rule_sets_aside_beside_its_folder(self) -> None:
        watched = self.tmp / "Courses"
        rule = COURSES.model_copy(update={"folder": str(watched)})
        item = episode(1, path=watched / "VisionQuest.S01E02.mkv", watched=watched, rule=rule)
        there = watched / SORTED_FILE.relative_to(D)
        [run] = replace_runs([item], SETTINGS, DEFAULT_PRESETS, lambda p: p == there, rules=[rule])
        [aside] = run.plan.aside
        assert aside.target == self.tmp / "Duplicates" / "Courses" / SORTED_FILE.relative_to(D)

    def test_a_rules_extras_change_only_its_ticks(self) -> None:
        rule = Rule(
            id=1,
            folder=D,
            take="films",
            films=RuleTarget(preset="Movie (Folder)", extras={"trailer": False}),
        )
        [run] = library_runs([movie(1, rule=rule)], SETTINGS, DEFAULT_PRESETS, never, rules=[rule])
        assert run.plan.options.trailer is False  # the rule's change
        assert run.plan.options.movie_poster is True  # the rest as the preset
        quality = rule.model_copy(
            update={"films": RuleTarget(preset="Movie (Folder)", extras={"quality_in_name": True})}
        )
        [run] = library_runs(
            [movie(1, rule=quality)], SETTINGS, DEFAULT_PRESETS, never, rules=[quality]
        )
        assert run.plan.options.quality_in_name is True  # named with the quality code too
        assert run.plan.files[0].target.parent == MOVIES / "Dune (2021)"

    def test_a_library_folder_not_chosen_holds_only_that_rules_files(self) -> None:
        settings = SETTINGS.model_copy(update={"movies_folder": ""})
        library = Rule(id=1, folder=D, take="films", quality="not_4k")
        big = Rule(
            id=2,
            folder=D,
            take="films",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=BIG),
        )
        runs = library_runs(
            [movie(1, rule=library), movie(2, rule=big)],
            settings,
            DEFAULT_PRESETS,
            never,
            rules=[library, big],
        )
        held = next(r for r in runs if r.rule_id == 1)
        assert held.plan.files == () and held.held == {1: t(NO_MOVIES_HOLD)}
        moving = next(r for r in runs if r.rule_id == 2)
        assert moving.held == {} and moving.plan.files[0].target.parent == Path(BIG, "Dune (2021)")
        assert route_problem(None, MediaKind.MOVIE) == t(NOT_WATCHED)

    def test_the_snapshot_routes_a_file_whose_rule_was_deleted(self) -> None:
        gone = Rule(
            id=9,
            folder=D,
            take="films",
            films=RuleTarget(preset="Movie (Folder)", to="folder", folder=BIG),
        )
        item = movie(1, rule=gone)
        route = route_for_item(item, [], SETTINGS, DEFAULT_PRESETS)
        assert route is not None and route.number == 0 and route.rule == gone
        [run] = owner_runs([item], SETTINGS, DEFAULT_PRESETS, never, rules=[])
        assert run.plan.files[0].target == Path(BIG, "Dune (2021)", "Dune (2021).mkv")

    def test_the_rule_as_it_is_now_wins_over_the_snapshot(self) -> None:
        before = Rule(id=3, folder=D, take="films")
        now = before.model_copy(
            update={"films": RuleTarget(preset="Movie (Folder)", to="folder", folder=BIG)}
        )
        rules = [Rule(id=1, folder=D), now]
        route = route_for_item(movie(1, rule=before), rules, SETTINGS, DEFAULT_PRESETS)
        assert route is not None and route.number == 2 and route.root == Path(BIG)

    def test_without_a_snapshot_the_rule_that_fits_now_routes_it(self) -> None:
        shows = Rule(id=1, folder=D, take="shows")
        films = Rule(id=2, folder=D, take="films")
        route = route_for_item(movie(1), [shows, films], SETTINGS, DEFAULT_PRESETS)
        assert route is not None and route.rule.id == 2 and route.number == 2
        stray = movie(1, watched=Path(r"C:\elsewhere"), path=Path(r"C:\elsewhere\x.mkv"))
        [run] = library_runs([stray], SETTINGS, DEFAULT_PRESETS, never, rules=[shows, films])
        assert run.held == {1: t(NOT_WATCHED)}
        # In a watched folder, but no rule takes it (a shows rule, a film): it says just that.
        [run] = library_runs([movie(1)], SETTINGS, DEFAULT_PRESETS, never, rules=[shows])
        assert run.held == {1: t(NO_RULE_TAKES)}
        assert route_problem(None, MediaKind.MOVIE, watched=True) == "No rule takes this file."

    def test_a_gone_preset_without_a_choice_holds(self) -> None:
        rule = Rule(id=1, folder=D, take="films", films=RuleTarget(preset="Gone"))
        [run] = library_runs([movie(1, rule=rule)], SETTINGS, DEFAULT_PRESETS, never, rules=[rule])
        assert run.plan.files == () and run.held == {1: t(PRESET_GONE_HOLD)}
        route = route_for_item(movie(1, rule=rule), [rule], SETTINGS, DEFAULT_PRESETS)
        assert route is not None and route.preset is None
        # The owner's naming choice still names it.
        flat = next(p for p in DEFAULT_PRESETS if p.name == "Movie (Flat)")
        choice = {1: NamingChoice(flat, flat.rule, flat.options)}
        [run] = owner_runs(
            [movie(1, rule=rule)], SETTINGS, DEFAULT_PRESETS, never, choice, rules=[rule]
        )
        assert run.held == {} and run.plan.files[0].target == MOVIES / "Dune (2021).mkv"
