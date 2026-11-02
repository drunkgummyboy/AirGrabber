"""MediaForge watches AirGrabber's downloads (docs/forge/INTEGRATION.md 3.9 bridge 1, B-01).

AirGrabber's `download_dir` / `movie_download_dir` hold **.torrent files** (FOLDER mode), never
videos: they are never watched. The folders on this PC where finished videos land, as far as
AirGrabber's settings say:

- `subtitles_folder` ("Where finished downloads end up", a UNC path allowed), in every mode;
- the qBittorrent on this PC that AirGrabber set up (`qbittorrent_local.set_up_by_airgrabber`):
  its save folder (`QBittorrentState.save_path`);
- a torrent program on this PC: the download folders AirGrabber gives it
  (`remote_destination`, the films' `movie_remote_destination`) when they are full Windows paths;
- Download Station (a NAS): only a *suggestion* `\\\\<host of remote_url>\\<destination>` (and the
  films one), checked for reachability in a thread with a time limit, never watched until the
  owner adds it to their own folders;
- qBittorrent / Transmission on another computer: the owner is asked (`AirGrabberAsk`).

The remote program counts in REMOTE mode, and in FOLDER mode when one is set up (releases
without a .torrent file go there). `derived_folders` is pure; `AirGrabberFolderSync` keeps
`ForgeSettings.airgrabber` up to date on every change of AirGrabber's settings
(`SettingsService.subscribe`). `watched_folders` is what the renamer watches: the owner's own
folders, plus AirGrabber's (not the suggestions) while "Use AirGrabber's downloads" is on, minus
those that would clash with the owner's or the library folders (`airgrabber_in_use` says why).

Nothing moves from a folder that starts being watched behind the owner's back: renaming on its
own stays as the owner set it, and when AirGrabber's settings add a folder while it is On, it
goes back to Trial (said in the Activity strip), as On straight from Off does.
"""

from __future__ import annotations

import asyncio
import logging
import ntpath
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol
from urllib.parse import urlsplit

from app.forge.core.activity import ActivityFeed
from app.forge.core.library import inside
from app.forge.models.settings import (
    AirGrabberAsk,
    AirGrabberDownloads,
    AirGrabberFolder,
    AirGrabberWhy,
    ForgeSettings,
)
from app.i18n import N_, t
from app.models import DownloadMode, QBittorrentState, RemoteKind, Settings
from app.services.qbittorrent_local import local_port, set_up_by_airgrabber

log = logging.getLogger(__name__)

REACH_TIMEOUT_S = 5.0  # a suggested NAS folder that doesn't answer in this long counts as away

# -- texts (shown by MediaForge Settings › Library & watching and the Forge window's Library) --

REASONS: dict[AirGrabberWhy, str] = {
    "finished": N_("“Where finished downloads end up” in AirGrabber's Settings"),
    "qbittorrent": N_("qBittorrent on this PC saves AirGrabber's downloads here"),
    "shows": N_("{program} saves AirGrabber's downloads here"),
    "films": N_("{program} saves AirGrabber's films here"),
}
ASKS: dict[AirGrabberAsk, str] = {
    "": "",
    "finished": N_(
        "AirGrabber doesn't know where its finished downloads end up on this PC. Fill in "
        "“Where finished downloads end up” in AirGrabber's Settings › Subtitles, or add the "
        "folder to the download folders to watch."
    ),
    "other_computer": N_(
        "AirGrabber sends downloads to {program} on another computer. Add the folder where they "
        "show up on this PC (a shared network folder) to the download folders to watch, or fill "
        "in “Where finished downloads end up” in AirGrabber's Settings › Subtitles."
    ),
    "nas_folder": N_(
        "Download Station keeps AirGrabber's downloads in its own download folder. Add the "
        "shared folder where they show up on this PC to the download folders to watch, or fill "
        "in “Where finished downloads end up” in AirGrabber's Settings › Subtitles."
    ),
}
SUGGESTED = N_("Suggested: {reason}")
REACHABLE = N_("This PC can reach it.")
UNREACHABLE = N_("This PC can't reach it right now.")
NOT_CHECKED = N_("Not checked yet.")
COVERED = N_("Watched already: it is in a folder that is watched.")
HOLDS_OWN = N_("Not watched: a folder you added is inside it.")
OVERLAP = N_("Not watched: it overlaps a library folder.")
BACK_TO_TRIAL = N_(
    "AirGrabber's downloads now also end up in {folder}. Renaming on its own is on Trial again: "
    "switch it to On when you're happy."
)

FolderUseKind = Literal["watched", "suggested", "covered", "holds_own", "overlap"]
_NOTES: dict[FolderUseKind, str] = {
    "watched": "",
    "suggested": "",
    "covered": COVERED,
    "holds_own": HOLDS_OWN,
    "overlap": OVERLAP,
}


class FolderUse(NamedTuple):
    """One of AirGrabber's folders and what MediaForge does with it."""

    folder: AirGrabberFolder
    use: FolderUseKind


def reason_text(folder: AirGrabberFolder) -> str:
    """Why the folder comes from AirGrabber (translated)."""
    return t(REASONS[folder.why], program=folder.program)


def use_text(use: FolderUse) -> str:
    """The reason, and for a suggestion whether this PC reaches it, or why it isn't watched
    (translated)."""
    reason = reason_text(use.folder)
    if use.use == "suggested":
        reach = (
            NOT_CHECKED
            if use.folder.reachable is None
            else REACHABLE if use.folder.reachable else UNREACHABLE
        )
        return f"{t(SUGGESTED, reason=reason)}. {t(reach)}"
    note = _NOTES[use.use]
    return f"{reason}. {t(note)}" if note else reason


def ask_text(downloads: AirGrabberDownloads) -> str:
    """What the owner should choose (translated); empty when AirGrabber named a folder."""
    text = ASKS[downloads.ask]
    return t(text, program=downloads.program) if text else ""


# -- the pure part ----------------------------------------------------------------------------------


def _same(a: str, b: str) -> bool:
    return os.path.normcase(a.rstrip("\\/")) == os.path.normcase(b.rstrip("\\/"))


def pc_path(value: str) -> str:
    """`value` as a full Windows path (a drive letter, or `\\\\server\\share`), normalised; ""
    when it is empty or isn't one (a NAS path such as "/downloads/tv", or a relative one)."""
    value = value.strip()
    drive, rest = ntpath.splitdrive(value)
    if drive[:2] in ("\\\\", "//"):
        if len([p for p in re.split(r"[\\/]+", drive) if p]) < 2:  # a server without a share
            return ""
        return ntpath.normpath(value).rstrip("\\")  # `\\NAS\video\` is `\\NAS\video`
    if not drive or rest[:1] not in ("\\", "/"):
        return ""
    return ntpath.normpath(value)


def nas_path(remote_url: str, destination: str) -> str:
    """Download Station's download folder ("video/tv": a shared folder and below) as the NAS
    shows it to this PC (`\\\\<host>\\video\\tv`); "" without a host or a folder."""
    try:
        host = urlsplit(remote_url).hostname or ""
    except ValueError:
        return ""
    parts = [p for p in re.split(r"[\\/]+", destination.strip()) if p]
    if not host or ":" in host or not parts:
        return ""
    return "\\\\" + host + "\\" + "\\".join(parts)


def derived_folders(
    settings: Settings, qbittorrent_state: QBittorrentState | None
) -> AirGrabberDownloads:
    """Where AirGrabber's downloads end up on this PC, each folder with its reason, and what to
    ask when its settings name none. `qbittorrent_state` is the qBittorrent on this PC's
    settings file (None when unknown); it counts only when AirGrabber set that qBittorrent up."""
    folders: list[AirGrabberFolder] = []

    def add(path: str, why: AirGrabberWhy, program: str = "", suggested: bool = False) -> None:
        if path and not any(_same(path, f.path) for f in folders):
            folders.append(
                AirGrabberFolder(path=path, why=why, program=program, suggested=suggested)
            )

    add(pc_path(settings.subtitles_folder), "finished")
    other_computer = ""
    nas_unknown = False
    mode = settings.download_mode
    remote_used = mode is DownloadMode.REMOTE or (
        mode is DownloadMode.FOLDER and settings.remote_configured
    )
    if remote_used and settings.remote_url:
        program = settings.remote_kind.program
        if local_port(settings.remote_url) is not None:  # the program runs on this PC
            if set_up_by_airgrabber(settings) and qbittorrent_state is not None:
                add(pc_path(qbittorrent_state.save_path), "qbittorrent", program)
            add(pc_path(settings.remote_destination), "shows", program)
            add(pc_path(settings.movie_remote_destination), "films", program)
        elif settings.remote_kind is RemoteKind.SYNOLOGY:
            shows = nas_path(settings.remote_url, settings.remote_destination)
            nas_unknown = not shows
            add(shows, "shows", program, suggested=True)
            add(nas_path(settings.remote_url, settings.movie_remote_destination), "films",
                program, suggested=True)
        else:
            other_computer = program
    ask: AirGrabberAsk = ""
    if not any(not f.suggested for f in folders):
        if other_computer:
            ask = "other_computer"
        elif nas_unknown:
            ask = "nas_folder"
        elif not folders:
            ask = "finished"
    return AirGrabberDownloads(folders=folders, ask=ask, program=other_computer)


def _depth(folder: AirGrabberFolder) -> int:
    return len(Path(folder.path).parts)


def airgrabber_in_use(settings: ForgeSettings) -> list[FolderUse]:
    """What MediaForge does with each of AirGrabber's folders, in their order: watch it, or
    not (a suggestion; in a folder that is watched already; holding one of the owner's own
    folders; overlapping a library folder, which would stop all watching). Whether "Use
    AirGrabber's downloads" is on is not asked here."""
    own = [Path(f) for f in settings.watch_folders]
    libraries = [Path(f) for f in (settings.series_folder, settings.movies_folder) if f]
    taken: list[Path] = []
    uses: dict[int, FolderUseKind] = {}
    folders = settings.airgrabber.folders
    # The outer folders first, so a folder inside another one of AirGrabber's is "covered".
    for index in sorted(range(len(folders)), key=lambda i: _depth(folders[i])):
        folder = folders[index]
        path = Path(folder.path)
        use: FolderUseKind
        if any(inside(path, o) for o in [*own, *taken]):
            use = "covered"
        elif folder.suggested:
            use = "suggested"
        elif any(inside(path, lib) or inside(lib, path) for lib in libraries):
            use = "overlap"
        elif any(inside(o, path) for o in own):
            use = "holds_own"
        else:
            use = "watched"
            taken.append(path)
        uses[index] = use
    return [FolderUse(folder, uses[i]) for i, folder in enumerate(folders)]


def watched_folders(settings: ForgeSettings) -> list[str]:
    """The folders the renamer watches: the owner's own, then AirGrabber's in use while "Use
    AirGrabber's downloads" is on."""
    folders = list(settings.watch_folders)
    if settings.airgrabber_on:
        folders += [u.folder.path for u in airgrabber_in_use(settings) if u.use == "watched"]
    return folders


def newly_watched(before: ForgeSettings, after: ForgeSettings) -> list[str]:
    """The folders `after` watches that `before` didn't."""
    old = watched_folders(before)
    return [f for f in watched_folders(after) if not any(_same(f, o) for o in old)]


# -- keeping it up to date ------------------------------------------------------------------------


class _ForgeSettingsSource(Protocol):
    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]: ...


class _AirGrabberSettingsSource(Protocol):
    @property
    def current(self) -> Settings: ...

    def subscribe(self, listener: Callable[[Settings], None]) -> Callable[[], None]: ...


def _read_qbittorrent_state() -> QBittorrentState:
    """qBittorrent's settings file on this PC, read only (blocking: run it in a thread)."""
    from app.services.qbittorrent_setup import QBittorrentSetup

    return QBittorrentSetup().state()


def _view(settings: Settings) -> tuple[object, ...]:
    """What `derived_folders` reads of AirGrabber's settings (a change elsewhere is ignored)."""
    return (
        settings.download_mode,
        settings.remote_kind,
        settings.remote_url,
        settings.remote_username,
        settings.remote_configured,
        settings.remote_destination,
        settings.movie_remote_destination,
        settings.subtitles_folder,
    )


class AirGrabberFolderSync:
    """Keeps `ForgeSettings.airgrabber` in step with AirGrabber's settings: a look at `start`
    and after every change that matters (looks never overlap; a change during one makes one
    more), and again when Forge's settings were replaced without it (the old MediaForge's
    import stores a whole document). A suggestion's reachability is checked in a thread,
    `timeout_s` at most."""

    def __init__(
        self,
        forge_settings: _ForgeSettingsSource,
        airgrabber_settings: _AirGrabberSettingsSource,
        *,
        qbittorrent_state: Callable[[], QBittorrentState] = _read_qbittorrent_state,
        is_dir: Callable[[str], bool] = os.path.isdir,
        timeout_s: float = REACH_TIMEOUT_S,
        activity: ActivityFeed | None = None,
    ) -> None:
        self._forge = forge_settings
        self._airgrabber = airgrabber_settings
        self._qbittorrent_state = qbittorrent_state
        self._is_dir = is_dir
        self._timeout_s = timeout_s
        self._activity = activity
        self._seen: tuple[object, ...] | None = None
        self._stored: AirGrabberDownloads | None = None  # what the last look stored
        self._unsubscribe: list[Callable[[], None]] = []
        self._started = False
        self._task: asyncio.Task[None] | None = None
        self._again = False
        self._closed = False

    def start(self) -> None:
        """Listen to both settings and look once (on the running loop). Never raises: Forge
        starts even when this can't listen."""
        if self._closed or self._started:
            return
        self._started = True
        for source, listener in (
            (self._airgrabber, self._changed),
            (self._forge, self._forge_changed),
        ):
            subscribe = getattr(source, "subscribe", None)
            if subscribe is None:  # a stand-in that can't be followed: the first look only
                log.debug("Settings without subscribe: AirGrabber's folders looked at once")
                continue
            try:
                self._unsubscribe.append(subscribe(listener))
            except Exception as exc:
                log.error("Can't follow the settings (%s)", type(exc).__name__)
        self.poke()

    def _changed(self, settings: Settings) -> None:
        if _view(settings) != self._seen:
            self.poke()

    def _forge_changed(self, settings: ForgeSettings) -> None:
        if self._stored is not None and settings.airgrabber != self._stored:
            self.poke()  # replaced by something else (an import): derive it again

    def poke(self) -> None:
        """Look again soon."""
        if self._closed:
            return
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="forge-airgrabber-folders"
        )

    async def settled(self) -> None:
        """Wait until no look is running or due."""
        while self._task is not None and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                await self.refresh()
            except Exception as exc:  # the type only: a message could name a folder
                log.error("Looking at AirGrabber's download folders failed (%s)", type(exc).__name__)
            if not self._again or self._closed:
                return

    async def refresh(self) -> AirGrabberDownloads:
        """One look: derive, check the suggestions, store when something changed. Without
        AirGrabber's settings to read (a stand-in that holds none) nothing changes."""
        airgrabber = self._airgrabber.current
        if not isinstance(airgrabber, Settings):
            log.debug("No AirGrabber settings to derive download folders from")
            return self._forge.current.airgrabber
        self._seen = _view(airgrabber)
        state: QBittorrentState | None = None
        if set_up_by_airgrabber(airgrabber):
            try:
                state = await asyncio.to_thread(self._qbittorrent_state)
            except OSError as exc:
                log.warning("qBittorrent's settings file couldn't be read (%s)", type(exc).__name__)
        derived = derived_folders(airgrabber, state)
        derived = derived.model_copy(
            update={"folders": [await self._checked(f) for f in derived.folders]}
        )
        if self._closed:
            return derived
        current = self._forge.current
        self._stored = derived
        if current.airgrabber == derived:
            return derived
        changes: dict[str, Any] = {"airgrabber": derived}
        added = newly_watched(current, current.model_copy(update=changes))
        if added and current.automatic == "on":
            # Never move files from a folder that started being watched behind the owner's back.
            changes["automatic"] = "trial"
            log.info("A new folder of AirGrabber's downloads is watched: Trial again")
            if self._activity is not None:
                self._activity.say(t(BACK_TO_TRIAL, folder=added[0]), level=logging.WARNING)
        await self._forge.update(**changes)
        return derived

    async def _checked(self, folder: AirGrabberFolder) -> AirGrabberFolder:
        if not folder.suggested:
            return folder
        return folder.model_copy(update={"reachable": await self.reachable(folder.path)})

    async def reachable(self, path: str) -> bool:
        """Whether this PC reaches `path` within the time limit (asked in a thread)."""
        try:
            return bool(
                await asyncio.wait_for(asyncio.to_thread(self._is_dir, path), self._timeout_s)
            )
        except (TimeoutError, OSError):
            return False

    async def close(self) -> None:
        """Stop listening; a look in progress is cancelled."""
        self._closed = True
        unsubscribe, self._unsubscribe = self._unsubscribe, []
        for stop in unsubscribe:
            stop()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
