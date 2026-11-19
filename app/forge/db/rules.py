"""The rules for watched folders (`rules`, schema v5), in their order (`position`).

The whole list is stored at once (`replace_all`, a change in Settings): rules left out are deleted, new
ones (id 0) get an id, positions follow the list. At most one row is AirGrabber's rule (the
partial unique index `rules_airgrabber`). A rule's extras are JSON (only the ticks it changed). Renamed presets are followed in one
statement (`rename_presets`, swaps included).
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from app.forge.db.database import Database
from app.forge.models.rules import Rule, RuleTarget

_COLUMNS = (
    "id, enabled, name, folder, airgrabber, subfolders, take, quality, stays, "
    "shows_preset, shows_extras, shows_to, shows_folder, "
    "films_preset, films_extras, films_to, films_folder, mode, fresh"
)

_UPSERT = (
    "INSERT INTO rules (id, position, enabled, name, folder, airgrabber, subfolders, take, "
    "quality, stays, shows_preset, shows_extras, shows_to, shows_folder, films_preset, "
    "films_extras, films_to, films_folder, mode, fresh) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET position = excluded.position, enabled = excluded.enabled, "
    "name = excluded.name, folder = excluded.folder, airgrabber = excluded.airgrabber, "
    "subfolders = excluded.subfolders, take = excluded.take, quality = excluded.quality, "
    "stays = excluded.stays, shows_preset = excluded.shows_preset, "
    "shows_extras = excluded.shows_extras, shows_to = excluded.shows_to, "
    "shows_folder = excluded.shows_folder, films_preset = excluded.films_preset, "
    "films_extras = excluded.films_extras, films_to = excluded.films_to, "
    "films_folder = excluded.films_folder, mode = excluded.mode, fresh = excluded.fresh"
)


def _target(preset: str, extras: str, to: str, folder: str) -> dict[str, Any]:
    return {"preset": preset, "extras": json.loads(extras), "to": to, "folder": folder}


def _rule(row: Sequence[Any]) -> Rule:
    (id_, enabled, name, folder, airgrabber, subfolders, take, quality, stays,
     s_preset, s_extras, s_to, s_folder, f_preset, f_extras, f_to, f_folder, mode, fresh) = row
    return Rule.model_validate(
        {
            "id": id_,
            "on": bool(enabled),
            "name": name,
            "folder": folder,
            "airgrabber": bool(airgrabber),
            "subfolders": bool(subfolders),
            "take": take,
            "quality": quality,
            "stays": bool(stays),
            "shows": _target(s_preset, s_extras, s_to, s_folder),
            "films": _target(f_preset, f_extras, f_to, f_folder),
            "mode": mode,
            "fresh": bool(fresh),
        }
    )


def _target_values(target: RuleTarget) -> tuple[str, str, str, str]:
    return (target.preset, json.dumps(target.extras, sort_keys=True), target.to, target.folder)


def _values(rule: Rule, position: int) -> tuple[Any, ...]:
    return (
        rule.id or None,  # NULL: a new id
        position,
        int(rule.on),
        rule.name,
        rule.folder,
        int(rule.airgrabber),
        int(rule.subfolders),
        rule.take,
        rule.quality,
        int(rule.stays),
        *_target_values(rule.shows),
        *_target_values(rule.films),
        rule.mode,
        int(rule.fresh),
    )


class RuleRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> builtins.list[Rule]:
        """Every rule, in its order."""
        rows = await self._db.fetch_all(f"SELECT {_COLUMNS} FROM rules ORDER BY position, id")
        return [_rule(row) for row in rows]

    async def replace_all(self, rules: Sequence[Rule]) -> builtins.list[Rule]:
        """Store `rules` as the whole list, in one transaction: a stored rule whose id isn't
        among them goes, one with id 0 is added, the rest are written over; the positions are
        the list's. Returns the rules with their ids. Two of AirGrabber's rules raise
        sqlite3.IntegrityError (nothing is stored then)."""
        kept = [rule.id for rule in rules if rule.id]
        stored: builtins.list[Rule] = []
        async with self._db.transaction() as conn:
            marks = ", ".join("?" * len(kept))
            await conn.execute(f"DELETE FROM rules WHERE id NOT IN ({marks})", kept)
            for position, rule in enumerate(rules):
                cursor = await conn.execute(_UPSERT, _values(rule, position))
                rule_id = rule.id or cursor.lastrowid
                assert rule_id is not None
                stored.append(rule if rule.id else rule.model_copy(update={"id": rule_id}))
        return stored

    async def set_fresh(self, ids: Collection[int], fresh: bool) -> None:
        """Set the fresh flag of the rules `ids` (unknown ids are passed over)."""
        if not ids:
            return
        marks = ", ".join("?" * len(ids))
        async with self._db.transaction() as conn:
            await conn.execute(
                f"UPDATE rules SET fresh = ? WHERE id IN ({marks})", (int(fresh), *ids)
            )

    async def rename_preset(self, old: str, new: str) -> int:
        """The rules using the preset `old` (for either kind) use `new`; how many rules changed."""
        return await self.rename_presets({old: new})

    async def rename_presets(self, renamed: Mapping[str, str]) -> int:
        """The rules follow every rename in `renamed` (old name → new name) at once, in one
        statement: each row's old names are read before any is written, so two presets that
        swapped names swap here too. How many rules changed."""
        pairs = [(old, new) for old, new in renamed.items() if old != new]
        if not pairs:
            return 0
        whens = " ".join("WHEN ? THEN ?" for _ in pairs)
        mapped = [value for pair in pairs for value in pair]
        olds = [old for old, _ in pairs]
        marks = ", ".join("?" * len(olds))
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE rules SET "
                f"shows_preset = CASE shows_preset {whens} ELSE shows_preset END, "
                f"films_preset = CASE films_preset {whens} ELSE films_preset END "
                f"WHERE shows_preset IN ({marks}) OR films_preset IN ({marks})",
                (*mapped, *mapped, *olds, *olds),
            )
            return cursor.rowcount
