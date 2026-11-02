"""A Forge test with a fresh forge.db in its temporary folder (MediaForge's `db` fixture)."""

from __future__ import annotations

from app.forge.db.database import Database
from tests.forge.base import ForgeAsyncTestCase


class ForgeDbTestCase(ForgeAsyncTestCase):
    db: Database

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db = await Database.open(self.tmp / "t.db")
        self.addAsyncCleanup(self.db.close)

    async def scalar(self, sql: str) -> object:
        row = await self.db.fetch_one(sql)
        assert row is not None
        return row[0]
