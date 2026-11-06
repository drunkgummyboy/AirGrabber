"""Quality in the name, NFO files and the background picture: off unless a preset asks for them."""

from __future__ import annotations

import unittest
from pathlib import Path
from xml.etree import ElementTree

from app.forge.core.formatter import render
from app.forge.core.nfo import episode_nfo, movie_nfo, show_nfo
from app.forge.core.planner import build_mixed_plan, build_plan, describe
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia, TypeHint
from app.forge.models.plan import PlanInput
from app.forge.models.preset import (
    DEFAULT_PRESETS,
    Preset,
    PresetOptions,
    has_quality,
    with_options,
    without_quality,
)

ROOT = Path("C:/lib")


def show(quality: str | None = "1080p H.265", tmdb_id: int | None = 7, episode: int = 1) -> ResolvedMedia:
    return ResolvedMedia(
        parsed=ParsedMedia(
            source_name="a.mkv",
            kind=MediaKind.EPISODE,
            title="Severance",
            season=2,
            episode=episode,
            quality=quality,
        ),
        title="Severance",
        year=2022,
        episode_title="Hello & Goodbye",
        match_state=MatchState.CONFIDENT,
        tmdb_id=tmdb_id,
        backdrop_path="/back.jpg",
        overview="A <strange> office.",
        episode_overview="Mark returns.",
    )


def film() -> ResolvedMedia:
    return ResolvedMedia(
        parsed=ParsedMedia(source_name="d.mkv", kind=MediaKind.MOVIE, title="Dune", quality="2160p"),
        title="Dune",
        year=2021,
        episode_title=None,
        match_state=MatchState.CONFIDENT,
        tmdb_id=9,
        backdrop_path="/dune.jpg",
        overview="Spice.",
    )


def item(media: ResolvedMedia, item_id: int = 1) -> PlanInput:
    return PlanInput(
        item_id=item_id,
        source=Path(f"C:/in/{item_id}.mkv"),
        root=ROOT,
        dropped_folder=None,
        media=media,
    )


def nothing(_path: Path) -> bool:
    return False


SHOW_RULE = "{ny}/Season {s}/{ny} - {s00e00}"


class OffByDefaultTest(unittest.TestCase):
    def test_a_new_option_set_has_them_off(self) -> None:
        options = PresetOptions()
        self.assertFalse(options.quality_in_name or options.nfo or options.backdrop)

    def test_no_default_preset_turns_them_on(self) -> None:
        for preset in DEFAULT_PRESETS:
            self.assertFalse(
                preset.options.quality_in_name or preset.options.nfo or preset.options.backdrop
            )
            self.assertFalse(has_quality(preset.format))
            self.assertEqual(preset.rule, preset.format)

    def test_a_preset_saved_before_them_reads_them_as_off(self) -> None:
        options = PresetOptions.model_validate_json('{"trailer": true}')
        self.assertFalse(options.quality_in_name or options.nfo or options.backdrop)

    def test_a_plan_without_them_has_none_of_their_files(self) -> None:
        plan = build_plan([item(show()), item(film(), 2)], "{ny}/{ny}", PresetOptions(), nothing)
        self.assertEqual((plan.nfos, plan.posters), ((), ()))


class QualityTest(unittest.TestCase):
    def test_the_option_adds_the_code_to_the_file_name_only(self) -> None:
        rule = with_options(SHOW_RULE, PresetOptions(quality_in_name=True))
        self.assertEqual(rule, SHOW_RULE + " - {quality}")
        self.assertEqual(
            render(rule, show(), ".mkv").parts,
            (
                "Severance (2022)",
                "Season 02",
                "Severance (2022) - S02E01 - 1080p H.265.mkv",
            ),
        )

    def test_a_rule_that_has_the_code_keeps_its_place(self) -> None:
        rule = "{ny} [{quality}] - {s00e00}"
        self.assertEqual(with_options(rule, PresetOptions(quality_in_name=True)), rule)
        self.assertEqual(
            render(rule, show(), ".mkv").parts, ("Severance (2022) [1080p H.265] - S02E01.mkv",)
        )

    def test_a_file_that_says_nothing_of_its_quality_leaves_no_dash(self) -> None:
        for rule, name in (
            ("{ny} - {quality}", "Severance (2022).mkv"),
            ("{ny} [{quality}]", "Severance (2022).mkv"),
            ("{ny} ({quality}) - {s00e00}", "Severance (2022) - S02E01.mkv"),
        ):
            with self.subTest(rule):
                rendered = render(rule, show(quality=None), ".mkv")
                self.assertEqual(rendered.parts, (name,))
                self.assertEqual(rendered.unknown, ())

    def test_taking_the_code_out(self) -> None:
        self.assertEqual(without_quality("{ny} - {quality}"), "{ny}")
        self.assertEqual(without_quality("{ny}/{ny} - {QUALITY}"), "{ny}/{ny}")
        self.assertEqual(without_quality("{ny}"), "{ny}")

    def test_a_preset_says_its_rule(self) -> None:
        preset = Preset(
            name="P",
            type=TypeHint.MOVIE,
            format="{ny}",
            options=PresetOptions(quality_in_name=True),
        )
        self.assertEqual(preset.rule, "{ny} - {quality}")
        self.assertEqual(preset.format, "{ny}")


class NfoTest(unittest.TestCase):
    def test_a_film_gets_one_beside_it(self) -> None:
        plan = build_plan([item(film())], "{ny}/{ny}", PresetOptions(nfo=True), nothing)
        [nfo] = plan.nfos
        self.assertEqual(nfo.target, ROOT / "Dune (2021)" / "Dune (2021).nfo")
        root = ElementTree.fromstring(nfo.content)
        self.assertEqual(root.tag, "movie")
        self.assertEqual(root.findtext("title"), "Dune")
        self.assertEqual(root.findtext("year"), "2021")
        self.assertEqual(root.findtext("plot"), "Spice.")
        self.assertEqual(root.find("uniqueid").attrib["type"], "tmdb")  # type: ignore[union-attr]

    def test_episodes_get_one_each_and_their_show_one_together(self) -> None:
        plan = build_plan(
            [item(show()), item(show(episode=2), 2)],
            SHOW_RULE,
            PresetOptions(nfo=True),
            nothing,
        )
        names = {n.target.name: n for n in plan.nfos}
        self.assertEqual(
            set(names),
            {"Severance (2022) - S02E01.nfo", "Severance (2022) - S02E02.nfo", "tvshow.nfo"},
        )
        shared = names["tvshow.nfo"]
        self.assertEqual(shared.target, ROOT / "Severance (2022)" / "tvshow.nfo")
        self.assertEqual(shared.item_ids, (1, 2))
        self.assertEqual(ElementTree.fromstring(shared.content).tag, "tvshow")

    def test_a_flat_layout_has_no_folder_for_a_tvshow_nfo(self) -> None:
        plan = build_plan([item(show())], "{ny} - {s00e00}", PresetOptions(nfo=True), nothing)
        self.assertEqual([n.target.name for n in plan.nfos], ["Severance (2022) - S02E01.nfo"])

    def test_an_existing_one_stays(self) -> None:
        plan = build_plan([item(film())], "{ny}", PresetOptions(nfo=True), lambda p: p.suffix == ".nfo")
        self.assertEqual(plan.nfos, ())

    def test_a_file_tmdb_does_not_know_gets_none(self) -> None:
        plan = build_plan([item(show(tmdb_id=None))], SHOW_RULE, PresetOptions(nfo=True), nothing)
        self.assertEqual(plan.nfos, ())

    def test_special_characters_stay_valid_xml(self) -> None:
        self.assertEqual(
            ElementTree.fromstring(show_nfo(show())).findtext("plot"), "A <strange> office."
        )
        self.assertEqual(
            ElementTree.fromstring(episode_nfo(show())).findtext("title"), "Hello & Goodbye"
        )
        self.assertEqual(ElementTree.fromstring(movie_nfo(film())).tag, "movie")

    def test_a_mixed_plan_and_the_preview_list_them(self) -> None:
        options = PresetOptions(nfo=True, backdrop=True)
        plan = build_mixed_plan(
            [([item(film())], "{ny}", options), ([item(show(), 2)], SHOW_RULE, options)], nothing
        )
        self.assertEqual(len(plan.nfos), 3)
        lines = describe(plan)
        self.assertTrue(any(line.startswith("NFO: ") for line in lines))
        self.assertTrue(any(line.startswith("Background: ") for line in lines))


class BackgroundTest(unittest.TestCase):
    def options(self) -> PresetOptions:
        return PresetOptions(backdrop=True)

    def test_a_film_has_its_own_named_after_it(self) -> None:
        [picture] = build_plan([item(film())], "{ny}", self.options(), nothing).posters
        self.assertEqual(picture.kind, "backdrop")
        self.assertEqual(picture.tmdb_path, "/dune.jpg")
        self.assertEqual(picture.target, ROOT / "Dune (2021)-fanart.jpg")

    def test_a_show_has_one_in_its_folder(self) -> None:
        [picture] = build_plan(
            [item(show()), item(show(episode=2), 2)], SHOW_RULE, self.options(), nothing
        ).posters
        self.assertEqual(picture.target, ROOT / "Severance (2022)" / "fanart.jpg")
        self.assertEqual(picture.item_ids, (1, 2))

    def test_a_flat_show_keeps_its_name_on_it(self) -> None:
        [picture] = build_plan([item(show())], "{ny} - {s00e00}", self.options(), nothing).posters
        self.assertEqual(picture.target, ROOT / "Severance (2022) - Fanart.jpg")

    def test_no_picture_at_tmdb_means_none(self) -> None:
        media = film().model_copy(update={"backdrop_path": None})
        self.assertEqual(build_plan([item(media)], "{ny}", self.options(), nothing).posters, ())


if __name__ == "__main__":
    unittest.main()
