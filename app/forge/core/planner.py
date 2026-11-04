"""Work out every file's final name, plus the posters, trailers, subtitles and cleanup a run
needs."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

from app.forge.core import fsops
from app.forge.core.formatter import render, sanitize_segment
from app.forge.core.parser import is_video
from app.forge.models.media import MediaKind, ResolvedMedia
from app.forge.models.plan import (
    Plan,
    PlanInput,
    PlannedFile,
    PlannedPoster,
    PlannedSubtitles,
    PlannedTrailer,
)
from app.forge.models.preset import PresetOptions
from app.forge.models.subtitles import SubtitleIdentity
from app.i18n import N_, t, tn

ALREADY_NAMED = N_("already correctly named")  # a skip reason: t() it where it is shown

type PosterKind = Literal["movie", "series", "season"]

# What follows a video's stem in the name of a subtitle that goes with it: ".<lang>.srt",
# ".<lang>.forced.srt", ".<lang>.sdh.srt" (AirGrabber saves "<stem>.<lang>.srt", the language
# as OpenSubtitles spells it: "en", "pt-BR").
_SUBTITLE_TAIL = re.compile(
    r"\.[a-z]{2,3}(?:-[a-z0-9]{2,4})?(?:\.(?:forced|sdh))?\.srt", re.IGNORECASE
)


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _free_target(
    wanted: Path, source: Path, taken: set[str], exists: Callable[[Path], bool]
) -> Path:
    """The wanted name, or "name (n).ext" if something else already has it."""
    n = 0
    while True:
        candidate = wanted if n == 0 else wanted.with_name(f"{wanted.stem} ({n}){wanted.suffix}")
        is_source = _key(candidate) == _key(source)  # case-only rename of the same file
        if _key(candidate) not in taken and (is_source or not exists(candidate)):
            return candidate
        n += 1


def subtitle_of(video: Path, other: Path) -> bool:
    """Whether `other` is `video`'s subtitle: beside it, "<its stem>.<lang>[.forced|.sdh].srt"."""
    stem = video.stem
    name = other.name
    return (
        _key(video.parent) == _key(other.parent)
        and name[: len(stem)].casefold() == stem.casefold()
        and _SUBTITLE_TAIL.fullmatch(name[len(stem) :]) is not None
    )


def identity_of(media: ResolvedMedia) -> SubtitleIdentity:
    """What OpenSubtitles finds a video by, from what the queue knows of it."""
    return SubtitleIdentity(
        kind=media.kind,
        title=media.title,
        year=media.year,
        tmdb_id=media.tmdb_id,
        season=media.season,
        episode=media.episode,
        release=media.parsed.source_name,
    )


def subtitle_moves(
    source: Path,
    target: Path,
    names: Iterable[str],
    exists: Callable[[Path], bool] = fsops.exists,
) -> list[tuple[Path, Path]]:
    """The subtitles beside `source` that go along when it becomes `target`, each with its new
    name beside `target`: "<stem>.<lang>.srt" (also ".<lang>.forced.srt" / ".<lang>.sdh.srt",
    the stem compared without case) → "<target stem>.<lang>….srt". `names`: the file names in
    `source`'s folder. Left where they are: "<another video's stem>.srt" (that video's own
    subtitle, e.g. "Film.en.srt" beside "Film.en.mkv"), and one whose new name something else
    has already (`exists`; a change of case only is no clash)."""
    listed = list(names)
    stem, new_stem = source.stem, target.stem
    others = {Path(n).stem.casefold() for n in listed if is_video(Path(n))}
    moves: list[tuple[Path, Path]] = []
    for name in sorted(listed):
        if name[: len(stem)].casefold() != stem.casefold():
            continue
        tail = name[len(stem) :]
        if not _SUBTITLE_TAIL.fullmatch(tail) or name[: -len(".srt")].casefold() in others:
            continue
        src = source.parent / name
        dst = target.parent / f"{new_stem}{tail}"
        if str(src) == str(dst):
            continue
        if _key(src) != _key(dst) and exists(dst):
            continue
        moves.append((src, dst))
    return moves


def _series_root(item: PlanInput, folders: tuple[str, ...], target: Path) -> Path:
    """The first destination folder holding the series title; like the legacy app, the
    first folder when none does, and the file's own folder in a flat layout."""
    if not folders:
        return target.parent
    title = sanitize_segment(item.media.title).casefold()  # folders are sanitized too
    for i, folder in enumerate(folders):
        if title in folder.casefold():
            return item.root.joinpath(*folders[: i + 1])
    return item.root / folders[0]


def build_plan(
    inputs: Sequence[PlanInput],
    fmt: str,
    options: PresetOptions,
    exists: Callable[[Path], bool] = fsops.exists,
) -> Plan:
    files: list[PlannedFile] = []
    posters: dict[str, PlannedPoster] = {}
    trailers: dict[str, PlannedTrailer] = {}
    taken: set[str] = set()

    def add_poster(kind: PosterKind, tmdb_path: str | None, target: Path, item_id: int) -> None:
        key = _key(target)
        if key in posters:
            known = posters[key]
            posters[key] = known.model_copy(update={"item_ids": (*known.item_ids, item_id)})
        elif tmdb_path and not exists(target):
            posters[key] = PlannedPoster(
                kind=kind, tmdb_path=tmdb_path, target=target, item_ids=(item_id,)
            )

    for item in inputs:
        media = item.media
        rendered = render(fmt, media, item.source.suffix)
        wanted = item.root.joinpath(*rendered.parts)
        if str(wanted) == str(item.source):
            files.append(
                PlannedFile(
                    item_id=item.item_id,
                    source=item.source,
                    target=item.source,
                    skip_reason=ALREADY_NAMED,
                )
            )
            taken.add(_key(item.source))
            target = item.source
        else:
            target = _free_target(wanted, item.source, taken, exists)
            taken.add(_key(target))
            files.append(PlannedFile(item_id=item.item_id, source=item.source, target=target))

        ny = sanitize_segment(media.ny)
        item_id = item.item_id
        if media.kind is MediaKind.MOVIE and options.movie_poster:
            add_poster("movie", media.poster_path, target.parent / f"{ny} - Poster.jpg", item_id)
        if media.kind is MediaKind.EPISODE and options.series_poster:
            root = _series_root(item, rendered.parts[:-1], target)
            add_poster("series", media.poster_path, root / f"{ny} - Series Poster.jpg", item_id)
        if media.kind is MediaKind.EPISODE and options.season_poster and media.season is not None:
            name = f"Season {media.season:02d} Poster.jpg"
            add_poster("season", media.season_poster_path, target.parent / name, item_id)
        if options.trailer:
            stem = target.parent / f"{ny} - Trailer"
            key = _key(stem)
            if key in trailers:
                known = trailers[key]
                trailers[key] = known.model_copy(update={"item_ids": (*known.item_ids, item_id)})
            elif not exists(stem.with_name(stem.name + ".mp4")):
                words = [media.title, str(media.year)] if media.year else [media.title]
                trailers[key] = PlannedTrailer(
                    query=f"ytsearch1:{' '.join(words)} official trailer",
                    title=media.title,
                    year=media.year,
                    target_stem=stem,
                    item_ids=(item_id,),
                )

    subtitles: list[PlannedSubtitles] = []
    if options.subtitles:
        media_of = {item.item_id: item.media for item in inputs}
        subtitles = [
            PlannedSubtitles(
                item_id=planned.item_id,
                video=planned.target,
                identity=identity_of(media_of[planned.item_id]),
            )
            for planned in files
        ]

    clean_roots: list[Path] = []
    if options.clean_junk:
        for item in inputs:
            if item.dropped_folder and item.dropped_folder not in clean_roots:
                clean_roots.append(item.dropped_folder)

    prune: list[Path] = []
    if options.prune_empty:
        candidates = {_key(p): p for i in inputs for p in (i.source.parent, i.dropped_folder) if p}
        prune = sorted(candidates.values(), key=lambda p: len(p.parts), reverse=True)

    return Plan(
        files=tuple(files),
        posters=tuple(posters.values()),
        trailers=tuple(trailers.values()),
        subtitles=tuple(subtitles),
        clean_roots=tuple(clean_roots),
        prune_dirs=tuple(prune),
        options=options,
    )


type PlanPart = tuple[Sequence[PlanInput], str, PresetOptions]
"""Files named by one rule: (inputs, naming rule, options)."""


def build_mixed_plan(
    parts: Sequence[PlanPart], exists: Callable[[Path], bool] = fsops.exists
) -> Plan:
    """One plan for files named by different rules (the queue's Forge: shows and films, each with
    its own preset). The parts are planned in turn and a later part never takes a name an earlier
    one took; junk is cleaned and empty folders removed where a part's options ask for it, and
    the plan's options are every part's together."""
    taken: set[str] = set()

    def in_use(path: Path) -> bool:
        return _key(path) in taken or exists(path)

    plans: list[Plan] = []
    for inputs, fmt, options in parts:
        if not inputs:
            continue
        plan = build_plan(inputs, fmt, options, exists=in_use)
        taken.update(_key(f.target) for f in plan.files)
        plans.append(plan)
    if len(plans) == 1:
        return plans[0]
    posters = {_key(p.target): p for plan in plans for p in plan.posters}
    clean = {_key(root): root for plan in plans for root in plan.clean_roots}
    prune = {_key(path): path for plan in plans for path in plan.prune_dirs}
    flags = {
        name: any(getattr(plan.options, name) for plan in plans)
        for name in PresetOptions.model_fields
    }
    return Plan(
        files=tuple(f for plan in plans for f in plan.files),
        posters=tuple(posters.values()),
        trailers=tuple(tr for plan in plans for tr in plan.trailers),
        subtitles=tuple(s for plan in plans for s in plan.subtitles),
        clean_roots=tuple(clean.values()),
        prune_dirs=tuple(sorted(prune.values(), key=lambda p: len(p.parts), reverse=True)),
        options=PresetOptions(**flags),
    )


def describe(plan: Plan) -> list[str]:
    """Plain-language lines for the Preview log (translated)."""
    lines = [t("Clean junk in: {folder}", folder=root) for root in plan.clean_roots]
    for f in plan.files:
        if f.skip_reason:
            lines.append(t("Skip: {name} ({reason})", name=f.source.name, reason=t(f.skip_reason)))
        else:
            lines.append(t("Rename: {name} → {target}", name=f.source.name, target=f.target))
    lines += [t("Poster: {target}", target=p.target) for p in plan.posters]
    lines += [t("Trailer: {target}", target=f"{tr.target_stem}.mp4") for tr in plan.trailers]
    lines += [t("Subtitles for: {name}", name=s.video.name) for s in plan.subtitles]
    if plan.prune_dirs:
        count = len(plan.prune_dirs)
        lines.append(
            tn(
                "Remove empty folders: checks {n} folder",
                "Remove empty folders: checks {n} folders",
                count,
            )
        )
    return lines
