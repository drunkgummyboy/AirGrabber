"""Forge's models: ForgeSettings loads what MediaForge stored, without AirGrabber's own fields."""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.forge.models.history import Operation, OpKind, OpState, RunSource, RunSummary
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.settings import ForgeSettings
from tests.forge.base import ForgeTestCase

# What MediaForge 1.3 stored for AppSettings (made-up folders, an empty key).
OLD_DOCUMENT = {
    "tmdb_key": "",
    "tmdb_language": "nl-NL",
    "theme": "Dark",
    "scan_subfolders": False,
    "window_geometry": "1200x800+10+10",
    "last_preset": "Series (Folder)",
    "legacy_imported": True,
    "context_menu_presets": ["Auto"],
    "trailer_tools_ok": True,
    "ytdlp_checked_at": 12.5,
    "first_start_done": True,
    "auto_update": False,
    "accent": "ocean",
    "watch_folders": [r"\\192.168.1.20\Downloads"],
    "series_folder": r"\\192.168.1.20\Series",
    "movies_folder": r"\\192.168.1.20\Movies",
    "series_preset": "Series (Flat)",
    "movies_preset": "Movie (Flat)",
    "automatic": "trial",
    "clean_downloads": False,
    "notify_needs": False,
    "daily_summary": False,
    "summary_sent_on": "2026-10-30",
    "tray_notice_shown": True,
    "kept_back": [r"\\192.168.1.20\Downloads\Show.S01E01.mkv"],
    "quiet_restart_for": "1.3.0 2026-10-30",
}
AIRGRABBERS_FIELDS = (
    "tmdb_key",
    "theme",
    "accent",
    "auto_update",
    "tray_notice_shown",
    "quiet_restart_for",
    "first_start_done",
)


class ForgeSettingsTest(ForgeTestCase):
    def test_an_old_mediaforge_document_still_loads(self) -> None:
        settings = ForgeSettings.model_validate_json(json.dumps(OLD_DOCUMENT))
        assert settings.tmdb_language == "nl-NL"
        assert settings.window_geometry == "1200x800+10+10"
        assert settings.watch_folders == [r"\\192.168.1.20\Downloads"]
        assert settings.automatic == "trial"
        assert settings.kept_back == [r"\\192.168.1.20\Downloads\Show.S01E01.mkv"]
        assert settings.imported_from == ""
        dumped = settings.model_dump()
        for name in AIRGRABBERS_FIELDS:
            with self.subTest(field=name):
                self.assertNotIn(name, dumped)
                self.assertNotIn(name, ForgeSettings.model_fields)

    def test_defaults_and_the_import_marker(self) -> None:
        settings = ForgeSettings()
        assert settings.automatic == "off" and settings.scan_subfolders
        assert settings.series_preset == "Series (Folder)"
        marked = settings.model_copy(update={"imported_from": "MediaForge 1.3.0"})
        again = ForgeSettings.model_validate_json(marked.model_dump_json())
        assert again.imported_from == "MediaForge 1.3.0"

    def test_frozen(self) -> None:
        with self.assertRaises(ValidationError):
            ForgeSettings().automatic = "on"  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            DEFAULT_PRESETS[0].name = "Other"  # type: ignore[misc]


class HistoryModelTest(ForgeTestCase):
    def _op(self, **changes: object) -> Operation:
        values: dict[str, object] = {
            "id": 1,
            "run_id": 1,
            "seq": 0,
            "kind": OpKind.RENAME,
            "src": "a",
            "dst": "b",
            "size": 1,
            "mtime_ns": 1,
            "state": OpState.DONE,
            "error": None,
            "undo_state": None,
            "undo_note": None,
            "undone_at": None,
        }
        values.update(changes)
        return Operation.model_validate(values)

    def test_undoable(self) -> None:
        assert self._op().undoable
        assert not self._op(kind=OpKind.DELETE_FILE).undoable
        assert not self._op(state=OpState.FAILED).undoable

    def test_run_status(self) -> None:
        def run(undoable: int, undone: int) -> RunSummary:
            return RunSummary(
                id=1,
                started_at="2026-11-01T10:00:00",
                finished_at=None,
                source=RunSource.APP,
                preset_name=None,
                file_count=2,
                created_count=0,
                undoable_count=undoable,
                undone_count=undone,
            )

        assert run(2, 0).status == "active"
        assert run(1, 1).status == "partly-undone"
        assert run(0, 2).status == "undone"
