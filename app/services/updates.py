"""Self-update from GitHub: compare versions, download the branch archive, swap in the new app files.

An installed copy (made by install.ps1) consists of exactly APP_ENTRIES; an update replaces exactly
those, so nothing else in the folder is touched. User data lives in the data directory
(`app.config.AppPaths`), never next to the code.

Two ways in:
- `UpdateService.install` (Settings' "Update to X") swaps the new files in at once; the app then
  restarts itself (see main.py).
- `UpdateService.stage` (automatic updates) only unpacks and validates the new files into
  `<install>/.update/new`; main.py swaps them in with `apply_staged` once AirGrabber has closed.

Either way the launch-time package check installs whatever new requirements the update brought.
`apply_staged` and the file-system helpers below use the standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import shutil
import stat
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from app import __version__
from app.bootstrap import version_tuple
from app.models import UpdateInfo
from app.services.install_info import Registry, ensure_uninstall_entry

log = logging.getLogger(__name__)

GITHUB_REPO = "drunkgummyboy/AirGrabber"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{BRANCH}/app/__init__.py"
ARCHIVE_URL = f"https://codeload.github.com/{GITHUB_REPO}/zip/refs/heads/{BRANCH}"

INSTALL_DIR = Path(__file__).resolve().parents[2]
APP_ENTRIES = ("main.py", "requirements.txt", "app")  # keep in step with install.ps1
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
STAGING_DIR = ".update"
READY_FILE = "ready"  # in STAGING_DIR: written last by stage_archive (holds the staged version)

_VERSION_LINE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_RENAME_ATTEMPTS = 5  # antivirus scanners briefly lock freshly written files on Windows
# Staging, installing and applying run in worker threads, and apply_staged at exit: one at a time.
# A staging thread can outlive the event loop (closing the app cancels the task, not the thread).
_STAGING_LOCK = threading.Lock()
_STAGING_WAIT_S = 120.0


class UpdateError(Exception):
    """An update check or install that failed; the message is shown in the UI."""


def parse_version(source: str) -> str | None:
    """`__version__` from the text of app/__init__.py."""
    match = _VERSION_LINE.search(source)
    return match[1] if match else None


def is_newer(latest: str, current: str) -> bool:
    new, old = version_tuple(latest), version_tuple(current)
    return new is not None and old is not None and new > old


def is_development_copy(install_dir: Path) -> bool:
    """The source folder (it has the tests, or is a git checkout) is never overwritten."""
    return (install_dir / "tests").is_dir() or (install_dir / ".git").exists()


class UpdateService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        install_dir: Path = INSTALL_DIR,
        *,
        current: str = __version__,
        registry: Registry | None = None,
    ) -> None:
        self._http = http
        self._dir = install_dir
        self._current = current
        self._registry = registry  # None: the real Windows registry

    @property
    def current(self) -> str:
        return self._current

    @property
    def installable(self) -> bool:
        return not is_development_copy(self._dir)

    async def check(self) -> UpdateInfo:
        """The newest version on GitHub. Raises UpdateError when GitHub can't tell."""
        try:
            response = await self._http.get(VERSION_URL, headers={"Cache-Control": "no-cache"})
        except httpx.HTTPError as exc:
            raise UpdateError("Could not reach GitHub.") from exc
        if response.status_code == 404:
            raise UpdateError("GitHub doesn't have an AirGrabber 2 version yet.")
        if response.status_code != 200:
            raise UpdateError(f"GitHub answered HTTP {response.status_code}.")
        latest = parse_version(response.text)
        if latest is None:
            raise UpdateError("GitHub doesn't have an AirGrabber 2 version yet.")
        return UpdateInfo(
            current=self._current,
            latest=latest,
            available=is_newer(latest, self._current),
            installable=self.installable,
        )

    async def install(self, progress: Callable[[str], None] | None = None) -> str:
        """Download the latest version and swap it in. Returns the version now installed.

        The running process keeps the old code in memory: restart right after this returns.
        """
        if not self.installable:
            raise UpdateError("This is a development copy; it is never overwritten by an update.")
        report = progress or (lambda _text: None)
        report("Downloading update…")
        data = await self._download()
        report("Installing update…")
        version = await asyncio.to_thread(install_archive, data, self._dir)
        log.info("Updated from %s to %s", self._current, version)
        return version

    async def stage(self) -> str:
        """Download the latest version and unpack it, ready to be swapped in once AirGrabber has
        closed (main.py calls `apply_staged`). Returns the staged version.

        Raises UpdateError, also when the download is not newer than the running version.
        """
        if not self.installable:
            raise UpdateError("This is a development copy; it is never overwritten by an update.")
        data = await self._download()
        try:
            version = await asyncio.to_thread(
                stage_archive, data, self._dir, newer_than=self._current
            )
        except OSError as exc:
            log.warning("Could not unpack the update: %r", exc)
            raise UpdateError("Could not unpack the update.") from exc
        log.info("Update %s is ready; it is installed when AirGrabber closes", version)
        return version

    async def register_install(self) -> None:
        """Create or refresh this copy's entry in Windows' "Installed apps" (installed copies only)."""
        if self.installable:
            await asyncio.to_thread(
                ensure_uninstall_entry, self._dir, self._current, registry=self._registry
            )

    async def _download(self) -> bytes:
        try:
            async with self._http.stream("GET", ARCHIVE_URL, timeout=httpx.Timeout(30.0)) as response:
                if response.status_code != 200:
                    raise UpdateError(f"Download failed: GitHub answered HTTP {response.status_code}.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_ARCHIVE_BYTES:
                        raise UpdateError("Download failed: the archive is unexpectedly large.")
                return bytes(body)
        except httpx.HTTPError as exc:
            raise UpdateError("Download failed: could not reach GitHub.") from exc


# Everything below is plain, blocking file-system work on its arguments: it runs in a worker
# thread, or (apply_staged) in main.py after shutdown.


def install_archive(data: bytes, install_dir: Path) -> str:
    """Unpack a GitHub branch archive and swap its APP_ENTRIES in. Returns the new version.

    The old files are moved aside first and put back if anything fails half-way.
    """
    with _STAGING_LOCK:
        version = _unpack(data, install_dir)
        staging = install_dir / STAGING_DIR
        _swap(staging / "new", staging / "old", install_dir)
        _remove_tree(staging)  # best effort: a leftover is cleared by the next update
        return version


def stage_archive(data: bytes, install_dir: Path, *, newer_than: str | None = None) -> str:
    """Unpack and validate a GitHub branch archive into `<install>/.update/new` without swapping
    it in (that is `apply_staged`'s job). Returns the staged version.

    With `newer_than`, an archive that is not newer than that version is refused (UpdateError).
    The ready marker is written last, so an unpack that was cut short is never applied.
    """
    with _STAGING_LOCK:
        version = _unpack(data, install_dir)
        staging = install_dir / STAGING_DIR
        try:
            if newer_than is not None and not is_newer(version, newer_than):
                raise UpdateError(
                    f"The download is version {version}, which is not newer than {newer_than}."
                )
            (staging / READY_FILE).write_text(version, encoding="utf-8")
        except BaseException:
            _remove_tree(staging)
            raise
        return version


def apply_staged(install_dir: Path) -> str | None:
    """Swap in the update `stage_archive` left in `<install>/.update`. Returns the version
    installed, or None when nothing was installed.

    main.py calls this once AirGrabber has shut down. It never raises: a failed swap is rolled
    back and logged. A staged update that is incomplete, or not newer than the version on disk
    (the installer ran in between), is deleted instead. A development copy is never touched.
    """
    try:
        if is_development_copy(install_dir):
            return None
        if not _STAGING_LOCK.acquire(timeout=_STAGING_WAIT_S):
            log.warning("The update was still being unpacked; it is installed next time")
            return None
        try:
            return _apply_staged(install_dir)
        finally:
            _STAGING_LOCK.release()
    except Exception:
        log.exception("Installing the downloaded update failed")
        return None


def _apply_staged(install_dir: Path) -> str | None:
    staging = install_dir / STAGING_DIR
    if not staging.exists():
        return None
    staged = _staged_version(staging)
    if staged is None:
        log.info("Removing an incomplete downloaded update")
        _remove_tree(staging)
        return None
    installed = _read_version(install_dir / "app" / "__init__.py")
    if installed is None or not is_newer(staged, installed):
        log.info("Removing the downloaded update %s: %s is installed", staged, installed or "?")
        _remove_tree(staging)
        return None
    old = staging / "old"
    try:
        _swap(staging / "new", old, install_dir)
    except UpdateError as exc:
        log.warning("Could not install the downloaded update %s: %s", staged, exc)
        (staging / READY_FILE).unlink(missing_ok=True)  # never tried again
        if not old.exists() or not any(old.iterdir()):  # rolled back: no old file is left there
            _remove_tree(staging)
        return None
    _remove_tree(staging)  # best effort: a leftover is cleared by the next update
    log.info("Installed the downloaded update: %s -> %s", installed, staged)
    return staged


def _unpack(data: bytes, install_dir: Path) -> str:
    """Extract and validate an archive into `<install>/.update/new` (cleared first). Returns its
    version. On failure the staging folder is removed again and nothing else was changed."""
    staging = install_dir / STAGING_DIR
    _remove_tree(staging)
    if staging.exists():
        raise UpdateError(f"Could not clear the folder {staging}; delete it and try again.")
    new = staging / "new"
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                _extract(archive, new)
        except zipfile.BadZipFile as exc:
            raise UpdateError("The download is not a valid archive.") from exc
        for entry in APP_ENTRIES:
            if not (new / entry).exists():
                raise UpdateError(f"The download has no {entry}; nothing was changed.")
        version = _read_version(new / "app" / "__init__.py")
        if version is None:
            raise UpdateError("The download has no version number; nothing was changed.")
    except BaseException:
        _remove_tree(staging)
        raise
    return version


def _staged_version(staging: Path) -> str | None:
    """The version of a complete staged update (ready marker written, every entry there, the
    marker's version is the files' version), else None."""
    try:
        marker = (staging / READY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    new = staging / "new"
    if not all((new / entry).exists() for entry in APP_ENTRIES):
        return None
    version = _read_version(new / "app" / "__init__.py")
    return version if version is not None and version == marker else None


def _read_version(init_file: Path) -> str | None:
    try:
        return parse_version(init_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _extract(archive: zipfile.ZipFile, target: Path) -> None:
    """The APP_ENTRIES part of the archive, without its top folder ("AirGrabber-main/")."""
    names = [info.filename for info in archive.infolist()]
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1:
        raise UpdateError("The download has an unexpected layout; nothing was changed.")
    root = target.resolve()
    for info in archive.infolist():
        parts = PurePosixPath(info.filename).parts[1:]
        if not parts or parts[0] not in APP_ENTRIES or "__pycache__" in parts:
            continue
        destination = root.joinpath(*parts).resolve()
        if not destination.is_relative_to(root) or ".." in parts:
            raise UpdateError("The download contains unsafe paths; nothing was changed.")
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, destination.open("wb") as out:
            shutil.copyfileobj(source, out)


def _swap(new: Path, old: Path, install_dir: Path) -> None:
    old.mkdir(parents=True, exist_ok=True)
    started: list[str] = []
    try:
        for entry in APP_ENTRIES:
            target = install_dir / entry
            started.append(entry)
            if target.exists():
                _rename(target, old / entry)
            _rename(new / entry, target)
    except OSError as exc:
        log.warning("Update could not replace the app files; rolling back: %r", exc)
        try:
            for entry in reversed(started):
                target = install_dir / entry
                if not (new / entry).exists() and target.exists():  # the new copy made it in
                    _remove(target)
                if (old / entry).exists():
                    _rename(old / entry, target)
        except OSError as rollback:
            log.error("Rolling back the update failed: %r", rollback)
            raise UpdateError(
                "The update failed half-way and could not be undone. "
                "Reinstall AirGrabber with the install command."
            ) from exc
        raise UpdateError(
            "Could not replace the app files (is another program using them?). Nothing was changed."
        ) from exc


def _rename(source: Path, destination: Path) -> None:
    for attempt in range(_RENAME_ATTEMPTS):
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, onexc=_clear_read_only)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _remove_tree(path: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=_clear_read_only)


def _clear_read_only(function: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """rmtree helper: Windows refuses to delete read-only files and folders."""
    os.chmod(path, stat.S_IWRITE)
    function(path)
