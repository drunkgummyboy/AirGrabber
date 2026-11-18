"""The rules for watched folders (forge.db `rules`, schema v5).

Design: docs/forge/2026-11-18-rules-design.md. A rule is one line of Settings › MediaForge ›
Auto-rename: the folder it watches (or AirGrabber's downloads), whether it looks in subfolders,
what it takes (kind and quality), the preset and extras per kind, where the files go (a library
folder, another folder, or sorted inside the watched folder: `stays`) and Automatic / Manual.
The list's order is the rule order: the first rule that fits a file takes it (there is no
position field). Filters added later are new fields with a default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

from app.forge.models.media import MediaKind

type RuleTake = Literal["anything", "shows", "films"]
type RuleQuality = Literal["any", "4k", "not_4k"]
type RuleMode = Literal["auto", "manual"]
type TargetTo = Literal["library", "folder"]

# The preset extras (PresetOptions' fields) a rule may change for itself. Never
# `move_to_library` (the rule's destination says where), `clean_junk` or `prune_empty` (the
# download folder's clean-up stays global).
RULE_EXTRAS: frozenset[str] = frozenset(
    {
        "subtitles",
        "movie_poster",
        "series_poster",
        "season_poster",
        "backdrop",
        "trailer",
        "quality_in_name",
        "nfo",
    }
)


class RuleTarget(BaseModel, frozen=True):
    """What a rule does with one kind: the preset, the extras it changed, and where it goes."""

    preset: str
    # Only the ticks this rule changed from its preset's (the rest follow the preset).
    extras: dict[str, bool] = {}
    to: TargetTo = "library"  # the kind's library folder, or `folder`
    folder: str = ""  # for `to == "folder"`

    @field_validator("extras")
    @classmethod
    def _known_extras(cls, extras: dict[str, bool]) -> dict[str, bool]:
        return {key: value for key, value in extras.items() if key in RULE_EXTRAS}


class Rule(BaseModel, frozen=True):
    """One rule for watched folders."""

    id: int = 0  # 0 = not stored yet
    on: bool = True  # AirGrabber's rule: `ForgeSettings.airgrabber_on`, as RuleService shows it
    name: str = ""  # "" = made from its choices where it is shown
    folder: str = ""  # "" for AirGrabber's rule (its folders come from AirGrabber's settings)
    airgrabber: bool = False  # the ready-made rule for AirGrabber's downloads (exactly one)
    subfolders: bool = True
    take: RuleTake = "anything"
    quality: RuleQuality = "any"
    stays: bool = False  # sorted inside the watched folder instead of moved
    shows: RuleTarget = RuleTarget(preset="Series (Folder)")
    films: RuleTarget = RuleTarget(preset="Movie (Folder)")
    mode: RuleMode = "manual"
    # New, moved or switched back on: its first look queues the files already there as Manual.
    fresh: bool = False

    def target(self, kind: MediaKind) -> RuleTarget:
        """What this rule does with `kind`."""
        return self.shows if kind is MediaKind.EPISODE else self.films

    def kinds(self) -> tuple[MediaKind, ...]:
        """The kinds this rule takes."""
        if self.take == "shows":
            return (MediaKind.EPISODE,)
        if self.take == "films":
            return (MediaKind.MOVIE,)
        return (MediaKind.EPISODE, MediaKind.MOVIE)

    def same_routing(self, other: Rule) -> bool:
        """Whether `other` does the same with the same files (its id, name and fresh flag aside)."""
        ignored = {"id", "name", "fresh"}
        return self.model_dump(exclude=ignored) == other.model_dump(exclude=ignored)
