"""One-shot import of AirGrabber 1.x JSON data into the new SQLite database.

    python -m tools.import_legacy [LEGACY_DIR] [--db PATH]

Reads followed_shows.json, episodes_cache.json, history.json and settings.json from LEGACY_DIR
(default: the current directory), whichever exist. The JSON files are never modified, and
re-running is safe: shows and settings are upserted, history entries are de-duplicated.
The 1.x per-show "auto" flag is not carried over: imported shows start with automatic downloads off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import AppPaths
from app.database import Database
from app.models import DownloadRecord, Episode, MediaType, Settings, Show
from app.models.tvmaze import TVMazeEpisode, TVMazeShow

_HISTORY_KEY = re.compile(r"^(\d+)_S(\d+)E(\d+)$", re.IGNORECASE)


@dataclass
class ImportReport:
    shows: int = 0
    episodes: int = 0
    history: int = 0
    settings: bool = False
    problems: list[str] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _parse_show(
    key: str, entry: dict[str, Any], cached_episodes: list[Any] | None
) -> tuple[Show, list[Episode] | None]:
    if metadata := entry.get("metadata"):
        payload = TVMazeShow.model_validate(metadata)
        show, episodes = payload.to_show(), payload.to_episodes() or None
    else:  # added in 1.x but never synced: only the id and name are known
        show, episodes = Show(id=int(key), name=entry["name"]), None
    if cached_episodes:  # newer 1.x builds kept episodes in a separate cache file
        episodes = [TVMazeEpisode.model_validate(e).to_domain(show.id) for e in cached_episodes]
    return show, episodes


async def _import_settings(raw: dict[str, Any], db: Database, report: ImportReport) -> None:
    merged = {**(await db.settings.load()).model_dump(), **raw}
    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        invalid = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        report.problems.append(f"settings: ignored invalid {', '.join(sorted(invalid))}")
        settings = Settings.model_validate({k: v for k, v in merged.items() if k not in invalid})
    await db.settings.save(settings)
    report.settings = True


async def import_legacy(legacy_dir: Path, db: Database) -> ImportReport:
    report = ImportReport()

    if isinstance(raw_settings := _read_json(legacy_dir / "settings.json"), dict):
        await _import_settings(raw_settings, db, report)

    followed: dict[str, Any] = _read_json(legacy_dir / "followed_shows.json") or {}
    episode_cache: dict[str, Any] = _read_json(legacy_dir / "episodes_cache.json") or {}
    for key, entry in followed.items():
        try:
            show, episodes = _parse_show(key, entry, episode_cache.get(key))
        except (ValidationError, ValueError, KeyError) as exc:
            report.problems.append(f"show {key} ({entry.get('name', '?')}): {exc}")
            continue
        await db.library.save_show(show, episodes)
        await db.library.track(show.id)  # 1.x "auto" flag ignored: every switch starts off in 2.x
        report.shows += 1
        report.episodes += len(episodes or ())

    for item in _read_json(legacy_dir / "history.json") or []:
        if not (match := _HISTORY_KEY.match(str(item))):
            report.problems.append(f"history {item!r}: unrecognised entry")
            continue
        show_id, season, episode = map(int, match.groups())
        if await db.history.has_episode(show_id, season, episode):
            continue
        await db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=show_id,
                season=season,
                episode=episode,
                release_title="(imported from AirGrabber 1.x)",
            )
        )
        report.history += 1

    return report


async def _run(legacy_dir: Path, db_path: Path) -> ImportReport:
    db = await Database.open(db_path)
    try:
        return await import_legacy(legacy_dir, db)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import AirGrabber 1.x JSON data into SQLite.")
    parser.add_argument("legacy_dir", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path, help="target database (default: the app's database)")
    args = parser.parse_args()

    db_path = args.db or AppPaths.default().ensure().database
    report = asyncio.run(_run(args.legacy_dir, db_path))
    print(f"Imported into {db_path}")
    print(f"  shows:    {report.shows} ({report.episodes} episodes)")
    print(f"  history:  {report.history} entries")
    print(f"  settings: {'yes' if report.settings else 'no settings.json found'}")
    for problem in report.problems:
        print(f"  ! {problem}")


if __name__ == "__main__":
    main()
