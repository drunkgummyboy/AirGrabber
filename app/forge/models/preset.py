"""Presets: a media type, a format string and the extras to apply."""

from __future__ import annotations

from pydantic import BaseModel

from app.forge.models.media import TypeHint


class LastPresetError(Exception):
    """The last remaining preset can't be deleted."""


class PresetOptions(BaseModel, frozen=True):
    move_to_library: bool = False  # a file added by hand goes to its Series / Movies folder
    movie_poster: bool = False
    series_poster: bool = False
    season_poster: bool = False
    trailer: bool = False
    clean_junk: bool = False
    prune_empty: bool = False
    subtitles: bool = False  # one subtitle per chosen language, right after the rename


class Preset(BaseModel, frozen=True):
    name: str
    type: TypeHint
    format: str
    options: PresetOptions = PresetOptions()


DEFAULT_PRESETS: tuple[Preset, ...] = (
    Preset(name="Auto", type=TypeHint.AUTO, format="{ny}"),
    Preset(
        name="Movie (Folder)",
        type=TypeHint.MOVIE,
        format="{ny}/{ny}",
        options=PresetOptions(move_to_library=True, movie_poster=True, trailer=True),
    ),
    Preset(name="Movie (Flat)", type=TypeHint.MOVIE, format="{ny}"),
    Preset(
        name="Series (Folder)",
        type=TypeHint.TV,
        format="{ny}/{ny} - Season {s}/{ny} - {s00e00} - {en}",
        options=PresetOptions(move_to_library=True, series_poster=True, season_poster=True),
    ),
    Preset(name="Series (Flat)", type=TypeHint.TV, format="{ny} - {s00e00} - {en}"),
)

# Checkbox ids used by the legacy mediaforge_config.json. "chk_recursive" is deliberately
# absent: scanning subfolders is now a global setting, not part of a preset.
LEGACY_OPTION_KEYS: dict[str, str] = {
    "chk_poster": "movie_poster",
    "chk_series_poster": "series_poster",
    "chk_season_poster": "season_poster",
    "chk_trailer": "trailer",
    "chk_clean": "clean_junk",
    "chk_prune": "prune_empty",
}
