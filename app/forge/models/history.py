"""The journal: every change MediaForge makes on disk, grouped into runs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from app.forge.models.media import MediaKind

HISTORY_LIMIT = 50


class OpKind(StrEnum):
    RENAME = "rename"
    CREATE_FILE = "create_file"
    CREATE_DIR = "create_dir"
    REMOVE_DIR = "remove_dir"
    DELETE_FILE = "delete_file"


class OpState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class UndoState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class RunSource(StrEnum):
    APP = "app"
    RIGHT_CLICK = "right-click"
    IMPORT = "import"
    AUTOMATIC = "automatic"  # a watched download, renamed and moved on its own


class Operation(BaseModel, frozen=True):
    id: int
    run_id: int
    seq: int
    kind: OpKind
    src: str | None
    dst: str | None
    size: int | None
    mtime_ns: int | None
    state: OpState
    error: str | None
    undo_state: UndoState | None
    undo_note: str | None
    undone_at: datetime | None

    @property
    def undoable(self) -> bool:
        return (
            self.state is OpState.DONE
            and self.kind is not OpKind.DELETE_FILE
            and self.undo_state is not UndoState.DONE
        )


class RunSummary(BaseModel, frozen=True):
    id: int
    started_at: datetime
    finished_at: datetime | None
    source: RunSource
    preset_name: str | None
    file_count: int
    created_count: int
    undoable_count: int
    undone_count: int
    kinds: frozenset[MediaKind] = frozenset()  # empty for runs from before 1.3

    @property
    def status(self) -> Literal["active", "partly-undone", "undone"]:
        if self.undone_count == 0:
            return "active"
        return "undone" if self.undoable_count == 0 else "partly-undone"
