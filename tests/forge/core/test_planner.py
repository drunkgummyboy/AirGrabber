"""Every file's final name, plus the posters, trailers and cleanup a run needs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from app.forge.core.planner import (
    ALREADY_NAMED,
    build_mixed_plan,
    build_plan,
    describe,
    subtitle_moves,
)
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.plan import PlanInput
from app.forge.models.preset import PresetOptions
from app.i18n import Language, set_language
from tests.forge.base import ForgeTestCase

R = Path(r"C:\media")
SERIES = "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {en}"
MOVIE_FOLDER = "{ny}/{ny}"


def fake_exists(paths: Iterable[Path]) -> Callable[[Path], bool]:
    known = {str(p).lower() for p in paths}
    return lambda p: str(p).lower() in known


def episode(
    n: int, title: str = "VisionQuest", year: int | None = 2026, **kw: object
) -> ResolvedMedia:
    parsed = ParsedMedia(
        source_name=f"e{n}.mkv", kind=MediaKind.EPISODE, title=title, season=1, episode=n
    )
    fields: dict[str, object] = {
        "parsed": parsed,
        "title": title,
        "year": year,
        "episode_title": f"Ep {n}",
        "match_state": MatchState.CONFIDENT,
        "poster_path": "/series.jpg",
        "season_poster_path": "/season1.jpg",
    }
    fields.update(kw)
    return ResolvedMedia.model_validate(fields)


def film(title: str = "Dune Part Two", year: int | None = 2024, **kw: object) -> ResolvedMedia:
    parsed = ParsedMedia(source_name="m.mkv", kind=MediaKind.MOVIE, title=title)
    fields: dict[str, object] = {
        "parsed": parsed,
        "title": title,
        "year": year,
        "episode_title": None,
        "match_state": MatchState.CONFIDENT,
        "poster_path": "/movie.jpg",
    }
    fields.update(kw)
    return ResolvedMedia.model_validate(fields)


def inp(i: int, source: Path, media: ResolvedMedia, dropped: Path | None = R) -> PlanInput:
    root = dropped or source.parent
    return PlanInput(item_id=i, source=source, root=root, dropped_folder=dropped, media=media)


class PlannerTest(ForgeTestCase):
    def test_target_under_root(self) -> None:
        plan = build_plan([inp(1, R / "x.mkv", episode(2))], SERIES, PresetOptions(), fake_exists([]))
        [f] = plan.files
        assert f.target == (
            R
            / "VisionQuest (2026)"
            / "VisionQuest (2026) - Season 01"
            / "VisionQuest (2026) - S01E02 - Ep 2.mkv"
        )
        assert f.skip_reason is None

    def test_each_dropped_folder_is_its_own_root(self) -> None:
        a, b = Path(r"C:\one"), Path(r"\\nas\share\two")
        plan = build_plan(
            [inp(1, a / "x.mkv", film(), a), inp(2, b / "y.mkv", film("Alien", 1979), b)],
            "{ny}",
            PresetOptions(),
            fake_exists([]),
        )
        assert [f.target for f in plan.files] == [
            a / "Dune Part Two (2024).mkv",
            b / "Alien (1979).mkv",
        ]

    def test_single_file_uses_its_own_folder(self) -> None:
        folder = Path(r"C:\downloads\sub")
        plan = build_plan(
            [inp(1, folder / "x.mkv", film(), None)], "{ny}", PresetOptions(), fake_exists([])
        )
        assert plan.files[0].target == folder / "Dune Part Two (2024).mkv"

    def test_collision_with_existing_file_gets_suffix(self) -> None:
        base = R / "Dune Part Two (2024).mkv"
        one = R / "Dune Part Two (2024) (1).mkv"
        plan = build_plan([inp(1, R / "x.mkv", film())], "{ny}", PresetOptions(), fake_exists([base]))
        assert plan.files[0].target == one
        plan = build_plan(
            [inp(1, R / "x.mkv", film())], "{ny}", PresetOptions(), fake_exists([base, one])
        )
        assert plan.files[0].target == R / "Dune Part Two (2024) (2).mkv"

    def test_two_queue_items_same_target(self) -> None:
        plan = build_plan(
            [inp(1, R / "a.mkv", film()), inp(2, R / "b.mkv", film())],
            "{ny}",
            PresetOptions(),
            fake_exists([]),
        )
        assert [f.target.name for f in plan.files] == [
            "Dune Part Two (2024).mkv",
            "Dune Part Two (2024) (1).mkv",
        ]

    def test_already_named_is_skipped(self) -> None:
        src = R / "Dune Part Two (2024).mkv"
        plan = build_plan([inp(1, src, film())], "{ny}", PresetOptions(), fake_exists([src]))
        [f] = plan.files
        assert f.skip_reason == ALREADY_NAMED
        assert f.target == src

    def test_case_only_rename_is_not_a_collision(self) -> None:
        # Review focus 1: Windows says the target "exists" because it is the same file.
        src = R / "the office (2005).mkv"
        plan = build_plan(
            [inp(1, src, film("The Office", 2005))], "{ny}", PresetOptions(), fake_exists([src])
        )
        [f] = plan.files
        assert f.target.name == "The Office (2005).mkv"
        assert f.skip_reason is None

    def test_movie_poster(self) -> None:
        opts = PresetOptions(movie_poster=True)
        plan = build_plan([inp(1, R / "x.mkv", film())], MOVIE_FOLDER, opts, fake_exists([]))
        [poster] = plan.posters
        assert poster.kind == "movie"
        assert poster.tmdb_path == "/movie.jpg"
        assert poster.target == R / "Dune Part Two (2024)" / "Dune Part Two (2024) - Poster.jpg"

    def test_series_and_season_posters_deduplicated(self) -> None:
        opts = PresetOptions(series_poster=True, season_poster=True)
        items = [inp(1, R / "1.mkv", episode(1)), inp(2, R / "2.mkv", episode(2))]
        plan = build_plan(items, SERIES, opts, fake_exists([]))
        assert [(p.kind, p.target) for p in plan.posters] == [
            ("series", R / "VisionQuest (2026)" / "VisionQuest (2026) - Series Poster.jpg"),
            (
                "season",
                R / "VisionQuest (2026)" / "VisionQuest (2026) - Season 01" / "Season 01 Poster.jpg",
            ),
        ]

    def test_series_poster_falls_back_to_file_folder_in_flat_layout(self) -> None:
        opts = PresetOptions(series_poster=True)
        plan = build_plan([inp(1, R / "1.mkv", episode(1))], "{ny} - {s00e00}", opts, fake_exists([]))
        assert plan.posters[0].target == R / "VisionQuest (2026) - Series Poster.jpg"

    def test_posters_need_option_and_path_and_free_target(self) -> None:
        no_path = film(poster_path=None)
        plan = build_plan(
            [inp(1, R / "x.mkv", no_path)], "{ny}", PresetOptions(movie_poster=True), fake_exists([])
        )
        assert plan.posters == ()
        plan = build_plan([inp(1, R / "x.mkv", film())], "{ny}", PresetOptions(), fake_exists([]))
        assert plan.posters == ()
        existing = R / "Dune Part Two (2024) - Poster.jpg"
        plan = build_plan(
            [inp(1, R / "x.mkv", film())],
            "{ny}",
            PresetOptions(movie_poster=True),
            fake_exists([existing]),
        )
        assert plan.posters == ()

    def test_a_shows_trailer_knows_its_season(self) -> None:
        media = episode(2).model_copy(update={"tmdb_id": 7})
        plan = build_plan(
            [inp(1, R / "1.mkv", media)], "{ny}/{s}/{ny}", PresetOptions(trailer=True), fake_exists([])
        )
        [planned] = plan.trailers
        self.assertEqual((planned.tmdb_id, planned.season), (7, media.season))
        plan = build_plan(
            [inp(1, R / "x.mkv", film(tmdb_id=9))], "{ny}", PresetOptions(trailer=True), fake_exists([])
        )
        self.assertIsNone(plan.trailers[0].season)

    def test_trailer_query_and_dedupe(self) -> None:
        opts = PresetOptions(trailer=True)
        plan = build_plan([inp(1, R / "x.mkv", film())], MOVIE_FOLDER, opts, fake_exists([]))
        [t] = plan.trailers
        assert t.query == "ytsearch1:Dune Part Two 2024 official trailer"
        assert (t.title, t.year) == ("Dune Part Two", 2024)
        assert t.target_stem == R / "Dune Part Two (2024)" / "Dune Part Two (2024) - Trailer"

        eps = [inp(1, R / "1.mkv", episode(1)), inp(2, R / "2.mkv", episode(2))]
        assert len(build_plan(eps, "{ny} - {s00e00}", opts, fake_exists([])).trailers) == 1

    def test_posters_and_trailers_carry_item_ids(self) -> None:
        opts = PresetOptions(series_poster=True, trailer=True)
        items = [inp(1, R / "1.mkv", episode(1)), inp(2, R / "2.mkv", episode(2))]
        plan = build_plan(items, "{ny} - {s00e00}", opts, fake_exists([]))
        assert plan.posters[0].item_ids == (1, 2)
        assert plan.trailers[0].item_ids == (1, 2)

    def test_trailer_without_year(self) -> None:
        plan = build_plan(
            [inp(1, R / "x.mkv", film(year=None))], "{ny}", PresetOptions(trailer=True), fake_exists([])
        )
        assert plan.trailers[0].query == "ytsearch1:Dune Part Two official trailer"

    def test_prune_dirs_deepest_first_and_clean_roots_only_dropped_folders(self) -> None:
        loose = Path(r"C:\loose")
        items = [
            inp(1, R / "a" / "b" / "x.mkv", film()),
            inp(2, R / "a" / "y.mkv", film("Alien", 1979)),
            inp(3, loose / "z.mkv", film("Heat", 1995), None),
        ]
        opts = PresetOptions(clean_junk=True, prune_empty=True)
        plan = build_plan(items, "{ny}", opts, fake_exists([]))
        assert plan.clean_roots == (R,)
        assert plan.prune_dirs[0] == R / "a" / "b"
        assert set(plan.prune_dirs) == {R / "a" / "b", R / "a", R, loose}
        depths = [len(p.parts) for p in plan.prune_dirs]
        assert depths == sorted(depths, reverse=True)

        plain = build_plan(items, "{ny}", PresetOptions(), fake_exists([]))
        assert plain.clean_roots == () and plain.prune_dirs == ()

    def test_describe_lines(self) -> None:
        src = R / "Alien (1979).mkv"
        opts = PresetOptions(movie_poster=True)
        plan = build_plan(
            [inp(1, R / "x.mkv", film()), inp(2, src, film("Alien", 1979, poster_path=None))],
            "{ny}",
            opts,
            fake_exists([src]),
        )
        lines = describe(plan)
        assert f"Rename: x.mkv → {R / 'Dune Part Two (2024).mkv'}" in lines
        assert "Skip: Alien (1979).mkv (already correctly named)" in lines
        assert f"Poster: {R / 'Dune Part Two (2024) - Poster.jpg'}" in lines

    def test_describe_lines_in_dutch(self) -> None:
        src = R / "Alien (1979).mkv"
        opts = PresetOptions(prune_empty=True)
        plan = build_plan(
            [inp(1, R / "x.mkv", film()), inp(2, src, film("Alien", 1979))],
            "{ny}",
            opts,
            fake_exists([src]),
        )
        set_language(Language.NL)
        self.addCleanup(set_language, Language.EN)
        lines = describe(plan)
        assert f"Hernoemen: x.mkv → {R / 'Dune Part Two (2024).mkv'}" in lines
        assert "Overslaan: Alien (1979).mkv (heeft al de juiste naam)" in lines
        assert "Lege mappen weghalen: controleert 1 map" in lines

    def test_series_poster_root_for_titles_with_punctuation(self) -> None:
        # Review I9: the folder name is sanitized ("Star Trek Discovery"), the title isn't.
        opts = PresetOptions(series_poster=True)
        media = episode(1, title="Star Trek: Discovery", year=2017)
        plan = build_plan([inp(1, R / "1.mkv", media)], SERIES, opts, fake_exists([]))
        assert plan.posters[0].target == (
            R / "Star Trek Discovery (2017)" / "Star Trek Discovery (2017) - Series Poster.jpg"
        )

    def test_series_poster_falls_back_to_first_folder_like_legacy(self) -> None:
        opts = PresetOptions(series_poster=True)
        fmt = "TV/Season {s}/{t} - {s00e00}"
        plan = build_plan([inp(1, R / "1.mkv", episode(1))], fmt, opts, fake_exists([]))
        assert plan.posters[0].target == R / "TV" / "VisionQuest (2026) - Series Poster.jpg"


class MixedPlanTest(ForgeTestCase):
    """The queue's Forge: shows and films, each named by its own preset, in one plan."""

    def test_each_part_with_its_own_rule_and_options_in_one_plan(self) -> None:
        shows = [inp(1, R / "e1.mkv", episode(1))]
        films = [inp(2, R / "m.mkv", film())]
        plan = build_mixed_plan(
            [
                (shows, "{ny} - {s00e00}", PresetOptions(season_poster=True)),
                (films, MOVIE_FOLDER, PresetOptions(movie_poster=True, trailer=True)),
            ],
            exists=fake_exists([]),
        )
        targets = {f.item_id: f.target for f in plan.files}
        self.assertEqual(targets[1], R / "VisionQuest (2026) - S01E01.mkv")
        self.assertEqual(targets[2], R / "Dune Part Two (2024)" / "Dune Part Two (2024).mkv")
        self.assertEqual({p.kind for p in plan.posters}, {"season", "movie"})
        self.assertEqual([t.title for t in plan.trailers], ["Dune Part Two"])  # films only
        self.assertTrue(plan.options.trailer and plan.options.season_poster)

    def test_a_later_part_never_takes_an_earlier_ones_name(self) -> None:
        shows = [inp(1, R / "e1.mkv", episode(1, title="Same", year=None))]
        films = [inp(2, R / "m.mkv", film(title="Same", year=None))]
        plan = build_mixed_plan(
            [(shows, "{t} - {s00e00}", PresetOptions()), (films, "{t} - S01E01", PresetOptions())],
            exists=fake_exists([]),
        )
        names = [f.target.name for f in plan.files]
        self.assertEqual(names, ["Same - S01E01.mkv", "Same - S01E01 (1).mkv"])

    def test_tidying_up_where_a_part_asks_for_it(self) -> None:
        other = Path(r"C:\other")
        shows = [inp(1, R / "e1.mkv", episode(1))]
        films = [inp(2, other / "m.mkv", film(), dropped=other)]
        plan = build_mixed_plan(
            [
                (shows, SERIES, PresetOptions(clean_junk=True)),
                (films, MOVIE_FOLDER, PresetOptions(prune_empty=True)),
            ],
            exists=fake_exists([]),
        )
        self.assertEqual(plan.clean_roots, (R,))
        self.assertEqual(plan.prune_dirs, (other,))

    def test_one_part_is_that_parts_plan_and_none_is_empty(self) -> None:
        films = [inp(2, R / "m.mkv", film())]
        options = PresetOptions(movie_poster=True)
        exists = fake_exists([])
        self.assertEqual(
            build_mixed_plan(
                [([], SERIES, PresetOptions()), (films, MOVIE_FOLDER, options)], exists
            ),
            build_plan(films, MOVIE_FOLDER, options, exists),
        )
        self.assertEqual(build_mixed_plan([], exists).files, ())


class SubtitleMovesTest(ForgeTestCase):
    """Subtitles beside a video go with it, renamed to its new stem (B-08)."""

    SRC = R / "Downloads" / "Dune.Part.Two.2024.1080p.mkv"
    DST = R / "Movies" / "Dune Part Two (2024)" / "Dune Part Two (2024).mkv"

    def moves(self, names: list[str], taken: Iterable[Path] = ()) -> list[tuple[str, str]]:
        found = subtitle_moves(self.SRC, self.DST, names, fake_exists(taken))
        for src, dst in found:
            self.assertEqual((src.parent, dst.parent), (self.SRC.parent, self.DST.parent))
        return [(src.name, dst.name) for src, dst in found]

    def test_language_forced_and_sdh_subtitles_follow_with_the_new_stem(self) -> None:
        names = [
            "Dune.Part.Two.2024.1080p.mkv",
            "Dune.Part.Two.2024.1080p.en.srt",
            "dune.part.two.2024.1080p.NL.forced.srt",  # the stem compared without case
            "Dune.Part.Two.2024.1080p.pt-BR.sdh.srt",
        ]
        self.assertEqual(
            self.moves(names),
            [
                ("Dune.Part.Two.2024.1080p.en.srt", "Dune Part Two (2024).en.srt"),
                ("Dune.Part.Two.2024.1080p.pt-BR.sdh.srt", "Dune Part Two (2024).pt-BR.sdh.srt"),
                ("dune.part.two.2024.1080p.NL.forced.srt", "Dune Part Two (2024).NL.forced.srt"),
            ],
        )

    def test_other_files_stay(self) -> None:
        names = [
            "Dune.Part.Two.2024.1080p.srt",  # no language
            "Dune.Part.Two.2024.1080p.en.ass",  # not .srt
            "Dune.Part.Two.2024.1080p.english.srt",  # not a language code
            "Dune.Part.Two.2024.1080p.en.hi.forced.srt",
            "Dune.Part.Two.2024.1080p.extended.en.srt",  # another stem's
            "Dune.Part.Two.2024.720p.en.srt",
            "Dune.Part.Two.2024.1080p.nfo",
        ]
        self.assertEqual(self.moves(names), [])

    def test_another_videos_own_subtitle_stays(self) -> None:
        # "<stem>.en.srt" is the plain subtitle of a video named "<stem>.en.mkv" beside it
        names = ["Dune.Part.Two.2024.1080p.en.mkv", "Dune.Part.Two.2024.1080p.en.srt"]
        self.assertEqual(self.moves(names), [])

    def test_a_taken_name_leaves_that_subtitle_where_it_is(self) -> None:
        names = ["Dune.Part.Two.2024.1080p.en.srt", "Dune.Part.Two.2024.1080p.nl.srt"]
        taken = [self.DST.with_name("Dune Part Two (2024).EN.srt")]
        self.assertEqual(
            self.moves(names, taken),
            [("Dune.Part.Two.2024.1080p.nl.srt", "Dune Part Two (2024).nl.srt")],
        )

    def test_a_change_of_case_only_moves_and_the_same_name_doesnt(self) -> None:
        src = R / "Show" / "show - s01e01.mkv"
        names = ["show - s01e01.en.srt"]
        upper = src.with_name("Show - S01E01.mkv")
        taken = fake_exists([src.with_name("show - s01e01.en.srt")])
        [(old, new)] = subtitle_moves(src, upper, names, taken)
        self.assertEqual((old.name, new.name), ("show - s01e01.en.srt", "Show - S01E01.en.srt"))
        self.assertEqual(subtitle_moves(src, src, names, taken), [])
