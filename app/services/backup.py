"""Backup & move (Settings → Backup; the welcome's "Moving from another PC?"): the people, their
followed shows and film watchlists, the download history and the settings in one file
(app/models/backup.py), and loading such a file here or on another PC.

What goes in, and why:
- the people (name and colour; app/services/profiles.py);
- each person's followed shows with their automatic-download switch, the shows' "I already have
  this" marks, and their episodes (the marks belong to episodes, and the calendar and My shows
  can draw before the first sync; a loaded show is refreshed from TVMaze at the next sync);
- each person's watchlist films with their "Download when it's out" switch (not the automatic
  tries: they start afresh, and the release dates are read from TMDB again);
- the whole download history, and which grabs were taken off the Downloads page. It is small (a
  few hundred bytes a grab) and it is what stops downloading twice: without it every episode
  downloaded before would count as missing on the new PC, and "Download missing" and the
  automatic downloads would fetch them again;
- the settings, folders and addresses included (the load says to check them), except this PC's
  own markers (MACHINE_FIELDS) and, unless the user ticks "Include keys and passwords", every
  key and password (SECRET_FIELDS, the one list of them).
What stays out: caches (posters, TMDB's answers, TVMaze's schedule of shows nobody follows),
the subtitles' bookkeeping (it is about files on this PC) and the automatic downloads' tries.

Loading: `open` reads and checks a file (another kind of file, a newer version or a damaged
one: BackupError, nothing changed), `summarize` says what it holds, and `load` writes it:
REPLACE (the people, their shows and watchlists and the settings become the backup's) or ADD
(its people's shows and films join theirs here, what is here wins; the settings stay). The
backup's people are matched to the people here by name, then in order; the ones left over are
added (and with REPLACE, people here the backup lacks are removed). A backup from before people
is one person's lists: they go to the selected person (BackupRepository.restore). The download
history is added either way. A key or password the file doesn't hold keeps its value: a load never wipes one.
Where downloads go (LOCAL_DOWNLOAD_FIELDS) stays as it is on this PC when either PC sends its
downloads to its own qBittorrent, the one AirGrabber set up (app/services/qbittorrent_local.py):
that one's port and password only work on its own PC (`kept_downloads`). A NAS or another
computer the user chose carries over. Listeners (`subscribe`) hear every load, so the pages
reload.

Log hygiene: only counts and outcomes are logged; never paths, settings or secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from app import __version__
from app.database import BackupRepository
from app.i18n import t
from app.models import Settings
from app.models.backup import (
    FILE_SUFFIX,
    FORMAT_NAME,
    FORMAT_VERSION,
    BackupFile,
    BackupLoadResult,
    BackupSummary,
    KeptDownloads,
    LoadMode,
)
from app.services.qbittorrent_local import set_up_by_airgrabber
from app.services.profiles import ProfileService
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

BackupListener = Callable[[], None]

# Every key and password in the settings (app/models/settings.py). They go into a backup only
# when the user asks for it, and a load never wipes one the file doesn't hold.
# tests/test_backup.py fails for a new setting whose name says key / password / token / secret
# until it is listed here or in NOT_SECRET_FIELDS.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "tmdb_api_key",
        "prowlarr_api_key",
        # The NAS's (Download Station), qBittorrent's or Transmission's; also the one AirGrabber
        # made for the qBittorrent on this PC.
        "remote_password",
        "opensubtitles_password",  # the user's OpenSubtitles account (its token is never stored)
        "opensubtitles_own_key",
    }
)
# Settings whose names look like a secret's but aren't one.
NOT_SECRET_FIELDS: frozenset[str] = frozenset({"excluded_keywords"})
SECRET_NAME = re.compile(r"key|password|token|secret", re.IGNORECASE)
# This PC's own markers, never in a backup (a load keeps this PC's values).
MACHINE_FIELDS: frozenset[str] = frozenset(
    {
        "setup_done",  # the welcome ran on this PC
        "last_run_version",  # the "Updated to v…" notice
        "tray_notice_shown",
        "start_with_windows",  # mirrors the Startup-folder shortcut on this PC
        "prowlarr_seeded_url",  # the Prowlarr on this PC that got AirGrabber's sites
        "current_profile",  # who is selected on this PC (the people themselves are in it)
    }
)
# Where downloads go: kept as on this PC when either PC uses its own qBittorrent that AirGrabber
# set up (127.0.0.1, its port, the `airgrabber` user and its password; `kept_downloads`). Films'
# own folder there (`movie_remote_destination`) still comes from the backup.
LOCAL_DOWNLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "download_mode",
        "remote_kind",
        "remote_url",  # the Web UI's address and port
        "remote_username",
        "remote_password",
        "remote_verify_tls",
        "remote_destination",
    }
)
MAX_FILE_BYTES = 256 * 1024 * 1024  # a bigger file isn't a backup


class BackupProblem(StrEnum):
    UNREADABLE = "unreadable"  # the file couldn't be read
    NOT_A_BACKUP = "not_a_backup"  # another kind of file
    NEWER = "newer"  # made by a newer AirGrabber, with a newer format
    DAMAGED = "damaged"  # an AirGrabber backup that doesn't read
    UNWRITABLE = "unwritable"  # the backup couldn't be saved there


class BackupError(Exception):
    """A backup that couldn't be saved or read; `str()` says why in plain words (no path)."""

    def __init__(self, problem: BackupProblem, message: str) -> None:
        super().__init__(message)
        self.problem = problem


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- pure parts ------------------------------------------------------------------------------------


def default_file_name(day: date) -> str:
    """The name the save dialog suggests: "AirGrabber backup 2026-10-31.airgrabber"."""
    return t("AirGrabber backup {date}", date=day.isoformat()) + FILE_SUFFIX


def _filled(value: Any) -> bool:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return isinstance(value, str) and bool(value.strip())


def settings_for_backup(settings: Settings, *, include_secrets: bool) -> dict[str, Any]:
    """The settings as a backup holds them: as stored (JSON values), without MACHINE_FIELDS
    and, unless `include_secrets`, without any of SECRET_FIELDS (empty ones never)."""
    kept: dict[str, Any] = {}
    for name, value in settings.model_dump(mode="json").items():
        if name in MACHINE_FIELDS:
            continue
        if name in SECRET_FIELDS and not (include_secrets and _filled(value)):
            continue
        kept[name] = value
    return kept


def has_secrets(stored: Mapping[str, Any]) -> bool:
    """Whether a backup's settings hold a key or password."""
    return any(_filled(stored.get(name)) for name in SECRET_FIELDS)


def valid_settings(stored: Mapping[str, Any]) -> Settings:
    """A backup's settings as a Settings: a value that doesn't validate (a newer version's,
    a damaged one) is its default, as SettingsRepository.load does."""
    try:
        return Settings.model_validate(dict(stored))
    except ValidationError as exc:
        invalid = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        log.warning("Ignoring invalid settings in the backup: %s", ", ".join(sorted(invalid)) or "?")
        try:
            return Settings.model_validate({k: v for k, v in stored.items() if k not in invalid})
        except ValidationError:
            return Settings()


def kept_downloads(loaded: Settings, current: Settings) -> KeptDownloads:
    """Whether REPLACE keeps this PC's download connection (LOCAL_DOWNLOAD_FIELDS): LOCAL
    when this PC sends its downloads to its own qBittorrent that AirGrabber set up, THIS_PC when
    the backup's (`loaded`) went to the other PC's own one (its port and password don't work
    here), else NONE (a NAS or another computer carries over)."""
    if set_up_by_airgrabber(current):
        return KeptDownloads.LOCAL
    if set_up_by_airgrabber(loaded):
        return KeptDownloads.THIS_PC
    return KeptDownloads.NONE


def replace_changes(loaded: Settings, current: Settings) -> dict[str, Any]:
    """What REPLACE stores of a backup's settings (`loaded`, see `valid_settings`): every
    setting as the backup has it (one it lacks: the default), except this PC's own
    (MACHINE_FIELDS), a key or password the backup doesn't hold (left out or empty), and where
    downloads go when either PC uses its own qBittorrent (`kept_downloads`); those keep their
    current values."""
    kept = MACHINE_FIELDS
    if kept_downloads(loaded, current) is not KeptDownloads.NONE:
        kept = kept | LOCAL_DOWNLOAD_FIELDS
    changes: dict[str, Any] = {}
    for name in Settings.model_fields:
        if name in kept:
            continue
        value = getattr(loaded, name)
        if name in SECRET_FIELDS and not _filled(value):
            continue
        if value != getattr(current, name):
            changes[name] = value
    return changes


def summarize(backup: BackupFile) -> BackupSummary:
    return BackupSummary(
        people=max(1, len(backup.profiles)),
        shows=len(backup.shows),
        films=len(backup.watchlist),
        grabs=len(backup.history),
        settings=bool(backup.settings),
        secrets=has_secrets(backup.settings),
        created=backup.created,
        app_version=backup.app_version,
    )


def encode(backup: BackupFile) -> bytes:
    """The file's bytes: indented JSON, UTF-8."""
    return json.dumps(backup.model_dump(mode="json"), ensure_ascii=False, indent=1).encode("utf-8")


def decode(data: bytes) -> BackupFile:
    """A file's bytes as a backup. Raises BackupError: NOT_A_BACKUP (not JSON, or another
    format), NEWER (a newer format version) or DAMAGED (an AirGrabber backup that doesn't
    read)."""
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        if FORMAT_NAME.encode("ascii") in data[:4096]:  # the header is at the start
            raise _damaged() from None
        raise _not_a_backup() from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT_NAME:
        raise _not_a_backup()
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _damaged()
    if version > FORMAT_VERSION:
        made_by = raw.get("app_version")
        if isinstance(made_by, str) and made_by.strip():
            message = t(
                "This backup was made by a newer AirGrabber ({version}). Update AirGrabber, "
                "then load it again.",
                version=made_by.strip(),
            )
        else:
            message = t(
                "This backup was made by a newer AirGrabber. Update AirGrabber, then load it "
                "again."
            )
        raise BackupError(BackupProblem.NEWER, message)
    try:
        return BackupFile.model_validate(raw)
    except (ValidationError, RecursionError):
        raise _damaged() from None


def _not_a_backup() -> BackupError:
    return BackupError(BackupProblem.NOT_A_BACKUP, t("That file isn't an AirGrabber backup."))


def _damaged() -> BackupError:
    return BackupError(BackupProblem.DAMAGED, t("This backup is damaged and can't be loaded."))


def read_file(path: Path) -> bytes:
    """A file's bytes (blocking; a file larger than MAX_FILE_BYTES is no backup)."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise _not_a_backup()
    return path.read_bytes()


def write_file(path: Path, data: bytes) -> None:
    """Write `data` to `path` all at once (blocking): into a file beside it, then swapped in,
    so an earlier backup there is never left half-written."""
    part = path.with_name(f"{path.name}.part")
    try:
        part.write_bytes(data)
        os.replace(part, path)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


def _encode_and_write(path: Path, backup: BackupFile) -> None:
    write_file(path, encode(backup))


def _read_and_decode(path: Path) -> BackupFile:
    try:
        data = read_file(path)
    except OSError:
        raise BackupError(BackupProblem.UNREADABLE, t("Couldn't read that file.")) from None
    return decode(data)


# -- the service ---------------------------------------------------------------------------------


class BackupService:
    """Saves backups and loads them (`services.backup`). The file work runs in a thread (a
    backup may be on a network share); the database through BackupRepository."""

    def __init__(
        self,
        repository: BackupRepository,
        settings: SettingsService,
        profiles: ProfileService,
        *,
        app_version: str = __version__,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`profiles`: read again after a load (people may come or go; the selected person's
        watchlist is kept in memory). `clock`: the current UTC instant (tests)."""
        self._repository = repository
        self._settings = settings
        self._profiles = profiles
        self._app_version = app_version
        self._clock = clock
        self._listeners: list[BackupListener] = []

    def subscribe(self, listener: BackupListener) -> Callable[[], None]:
        """Call `listener` after every load (the library, watchlist and maybe the settings
        changed). Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Backup listener %r failed", listener)

    async def build(self, *, include_secrets: bool) -> BackupFile:
        """A backup of what is here now (the stored settings, not a dialog's unsaved ones)."""
        data = await self._repository.read()
        return BackupFile(
            format=FORMAT_NAME,
            version=FORMAT_VERSION,
            app_version=self._app_version,
            created=self._clock(),
            profiles=data.profiles,
            shows=data.shows,
            watchlist=data.films,
            history=data.grabs,
            settings=settings_for_backup(self._settings.current, include_secrets=include_secrets),
        )

    async def save(self, path: Path, *, include_secrets: bool) -> BackupSummary:
        """Save a backup to `path` (replacing a file there). Raises BackupError (UNWRITABLE)."""
        backup = await self.build(include_secrets=include_secrets)
        try:
            await asyncio.to_thread(_encode_and_write, path, backup)
        except OSError as exc:
            log.warning("Saving a backup failed (%s)", type(exc).__name__)
            raise BackupError(
                BackupProblem.UNWRITABLE, t("Couldn't save the backup there. Choose another folder.")
            ) from None
        summary = summarize(backup)
        log.info(
            "Saved a backup: %d people, %d shows, %d films, %d grabs, %s keys and passwords",
            summary.people, summary.shows, summary.films, summary.grabs,
            "with" if summary.secrets else "without",
        )
        return summary

    async def open(self, path: Path) -> BackupFile:
        """Read and check a backup file; nothing changes. Raises BackupError (UNREADABLE,
        NOT_A_BACKUP, NEWER, DAMAGED)."""
        try:
            backup = await asyncio.to_thread(_read_and_decode, path)
        except BackupError as exc:
            log.info("Not loading that file as a backup (%s)", exc.problem.value)
            raise
        return backup

    async def load(self, backup: BackupFile, mode: LoadMode) -> BackupLoadResult:
        """Write a backup here: REPLACE or ADD (see the module's notes)."""
        replace = mode is LoadMode.REPLACE
        current = self._settings.current
        settings_replaced = replace and bool(backup.settings)
        changes: dict[str, Any] = {}
        downloads_kept = KeptDownloads.NONE
        if settings_replaced:
            loaded = valid_settings(backup.settings)
            changes = replace_changes(loaded, current)
            downloads_kept = kept_downloads(loaded, current)
        if changes:  # would they store? Checked before anything changes.
            Settings.model_validate({**current.model_dump(), **changes})
        counts = await self._repository.restore(
            backup.data, replace=replace, when=self._clock(), selected=self._profiles.current_id
        )
        if changes:
            await self._settings.update(**changes)
        await self._profiles.reload()  # people may have come or gone; the watchlist again
        self._notify()
        updated = self._settings.current
        log.info(
            "Loaded a backup (%s): %d people added, %d removed; %d shows added, %d removed; "
            "%d films added, %d removed; %d grabs added; settings %s; download connection "
            "kept: %s",
            mode.value, counts.people_added, counts.people_removed, counts.shows_added,
            counts.shows_removed, counts.films_added, counts.films_removed, counts.grabs_added,
            "replaced" if settings_replaced else "kept", downloads_kept.value,
        )
        return BackupLoadResult(
            mode=mode,
            counts=counts,
            settings_replaced=settings_replaced,
            secrets_loaded=settings_replaced and has_secrets(backup.settings),
            language=updated.language,
            accent=updated.accent,
            downloads_kept=downloads_kept,
        )
