"""The remembered match choices (Settings › Naming lists, forgets and clears them).

A thin wrapper over `app.forge.db.matches.MatchRepo`, so `app/forge/ui` never sees the database
package. Remembering a choice goes through the resolver (`Resolver.apply_choice`).
"""

from __future__ import annotations

import builtins

from app.forge.db.matches import MatchRepo
from app.forge.models.match import RememberedMatch


class MatchService:
    def __init__(self, repository: MatchRepo) -> None:
        self._repository = repository

    async def list(self) -> builtins.list[RememberedMatch]:
        return await self._repository.list()

    async def forget(self, match: RememberedMatch) -> None:
        await self._repository.forget(match.kind, match.norm_title, match.year)

    async def clear(self) -> None:
        await self._repository.clear()
