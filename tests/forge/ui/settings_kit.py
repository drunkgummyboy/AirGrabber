"""The one Settings window (app/ui/settings_dialog.py) for tests of its MediaForge pages:
AirGrabber's side on fakes (tests/test_settings_dialog.py's), `forge` the test's own fake or a real
ForgeRuntime."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.models import Settings
from app.services import SettingsService
from app.ui.settings_dialog import SettingsDialog
from tests.test_settings_dialog import FakeProwlarr, FakeRepository, FakeShortcut


def airgrabber_settings() -> SettingsService:
    return SettingsService(
        FakeRepository(),  # type: ignore[arg-type]
        Settings(download_dir=Path(tempfile.gettempdir())),
    )


def forge_settings(
    master: Any,
    forge: Any,
    *,
    section: str | None = None,
    settings: SettingsService | None = None,
) -> SettingsDialog:
    """Settings over `master` with the MediaForge group; `forge` isn't asked for anything before
    a MediaForge page shows."""
    return SettingsDialog(
        master,
        airgrabber_settings() if settings is None else settings,
        search=mock.MagicMock(),
        station=mock.MagicMock(),
        updates=SimpleNamespace(current="3.0.0"),  # type: ignore[arg-type]
        torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
        discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
        prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
        startup_shortcut=FakeShortcut(),
        forge=forge,
        section=section,
    )
