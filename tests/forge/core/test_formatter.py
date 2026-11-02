"""The format string → safe Windows path segments; the default presets and the media models."""

from __future__ import annotations

from typing import Any

from app.forge.core.formatter import render, sanitize_segment
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia, TypeHint
from app.forge.models.preset import DEFAULT_PRESETS, LEGACY_OPTION_KEYS
from tests.forge.base import ForgeTestCase

SERIES_FOLDER = "{ny}/{ny} - Season {s}/{ny} - {s00e00} - {en}"


def ep(**kw: Any) -> ResolvedMedia:
    parsed_fields: dict[str, Any] = {
        "source_name": "x.mkv",
        "kind": MediaKind.EPISODE,
        "title": "VisionQuest",
        "season": kw.pop("season", 1),
        "episode": kw.pop("episode", 2),
        "episode_end": kw.pop("episode_end", None),
    }
    parsed = ParsedMedia(**parsed_fields)
    return ResolvedMedia(
        parsed=parsed,
        title=kw.pop("title", "VisionQuest"),
        year=kw.pop("year", 2026),
        episode_title=kw.pop("episode_title", "Theseus"),
        match_state=MatchState.CONFIDENT,
    )


def movie(**kw: Any) -> ResolvedMedia:
    parsed = ParsedMedia(source_name="x.mkv", kind=MediaKind.MOVIE, title="Dune Part Two")
    return ResolvedMedia(
        parsed=parsed,
        title=kw.pop("title", "Dune Part Two"),
        year=kw.pop("year", 2024),
        episode_title=None,
        match_state=MatchState.CONFIDENT,
    )


class RenderTest(ForgeTestCase):
    def test_series_folder_preset(self) -> None:
        r = render(SERIES_FOLDER, ep(), ".MKV")
        assert r.parts == (
            "VisionQuest (2026)",
            "VisionQuest (2026) - Season 01",
            "VisionQuest (2026) - S01E02 - Theseus.mkv",
        )
        assert str(r.relative) == "\\".join(r.parts)

    def test_variables_case_insensitive(self) -> None:
        assert render("{NY}", movie(), ".mp4").parts == ("Dune Part Two (2024).mp4",)

    def test_t_s_e_variables(self) -> None:
        assert render("{t} s{s} e{e}", ep(), ".mkv").parts == ("VisionQuest s01 e02.mkv",)

    def test_multi_episode(self) -> None:
        r = render("{s00e00}", ep(episode=1, episode_end=2), ".mkv")
        assert r.parts == ("S01E01-E02.mkv",)

    def test_missing_values(self) -> None:
        r = render("{t} {s00e00} {en}", ep(season=None, episode=None, episode_title=None), ".mkv")
        assert r.parts == ("VisionQuest S00E00 Episode 00.mkv",)

    def test_no_year(self) -> None:
        assert render("{ny}", movie(year=None), ".mkv").parts == ("Dune Part Two.mkv",)

    def test_values_are_not_rescanned(self) -> None:
        r = render("{en}", ep(episode_title="{t}"), ".mkv")
        assert r.parts == ("{t}.mkv",)
        assert r.unknown == ()

    def test_unknown_variable_kept_and_reported(self) -> None:
        r = render("{ny} {foo}", movie(), ".mkv")
        assert r.parts == ("Dune Part Two (2024) {foo}.mkv",)
        assert r.unknown == ("foo",)

    def test_backslash_and_slash_both_split(self) -> None:
        assert len(render("{t}\\{t}/{t}", movie(), ".mkv").parts) == 3

    def test_empty_segments_dropped(self) -> None:
        assert render("/{t}//", movie(), ".mkv").parts == ("Dune Part Two.mkv",)

    def test_empty_format_gives_placeholder_name(self) -> None:
        assert render("", movie(), ".mkv").parts == ("_.mkv",)

    def test_sanitize_segment(self) -> None:
        cases = [
            ('Who: Are "You"?', "Who Are You"),
            ("Part 1...", "Part 1"),
            ("  a   b  ", "a b"),
            ("CON", "CON_"),
            ("nul", "nul_"),
            ("COM1", "COM1_"),
            ("", "_"),
            ("a\x07b", "ab"),
            ("ｆｕｌｌ", "full"),
            ("a<b>c|d*e", "abcde"),
        ]
        for raw, clean in cases:
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_segment(raw), clean)

    def test_reserved_name_check_applies_to_whole_segment_only(self) -> None:
        assert render("{t}", movie(title="Con Air", year=None), ".mkv").parts == ("Con Air.mkv",)
        assert render("{t}", movie(title="CON", year=None), ".mkv").parts == ("CON_.mkv",)

    def test_episode_title_with_slash_does_not_create_folder(self) -> None:
        assert render("{en}", ep(episode_title="AC/DC"), ".mkv").parts == ("ACDC.mkv",)

    def test_trailing_dot_in_episode_title_is_stripped(self) -> None:
        assert render("{en}", ep(episode_title="To Be Continued..."), ".mkv").parts == (
            "To Be Continued.mkv",
        )


class PresetAndModelTest(ForgeTestCase):
    def test_default_presets(self) -> None:
        assert [p.name for p in DEFAULT_PRESETS] == [
            "Auto",
            "Movie (Folder)",
            "Movie (Flat)",
            "Series (Folder)",
            "Series (Flat)",
        ]
        by_name = {p.name: p for p in DEFAULT_PRESETS}
        assert by_name["Series (Folder)"].format == SERIES_FOLDER
        assert by_name["Series (Folder)"].type is TypeHint.TV
        assert by_name["Series (Folder)"].options.series_poster
        assert by_name["Series (Folder)"].options.season_poster
        assert by_name["Movie (Folder)"].options.movie_poster
        assert by_name["Movie (Folder)"].options.trailer
        assert by_name["Auto"].format == "{ny}"

    def test_legacy_option_keys(self) -> None:
        assert LEGACY_OPTION_KEYS["chk_poster"] == "movie_poster"
        assert LEGACY_OPTION_KEYS["chk_prune"] == "prune_empty"
        assert "chk_recursive" not in LEGACY_OPTION_KEYS

    def test_resolved_media_offline_copies_parsed(self) -> None:
        parsed = ParsedMedia(
            source_name="a.mkv",
            kind=MediaKind.EPISODE,
            title="Show",
            year=2001,
            season=2,
            episode=3,
            episode_title="Pilot",
        )
        r = ResolvedMedia.offline(parsed)
        assert (r.title, r.year, r.episode_title, r.match_state) == (
            "Show",
            2001,
            "Pilot",
            MatchState.OFFLINE,
        )
        assert (r.kind, r.season, r.episode, r.ny) == (MediaKind.EPISODE, 2, 3, "Show (2001)")
