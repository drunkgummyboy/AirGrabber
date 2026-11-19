"""MediaForge watches AirGrabber's downloads (docs/forge/INTEGRATION.md 3.9 bridge 1, B-01).

AirGrabber's `download_dir` / `movie_download_dir` hold **.torrent files** (FOLDER mode), never
videos: they are never watched. The folders on this PC where finished videos land, as far as
AirGrabber's settings say:

- `subtitles_folder` (Settings › Folders' "Finished downloads", a UNC path allowed), in every mode;
- the qBittorrent on this PC that AirGrabber set up (`qbittorrent_local.set_up_by_airgrabber`):
  its save folder (`QBittorrentState.save_path`);
- a torrent program on this PC: the download folders AirGrabber gives it
  (`remote_destination`, the films' `movie_remote_destination`) when they are full Windows paths;
- Download Station (a NAS): only a *suggestion* `\\\\<host of remote_url>\\<destination>` (and the
  films one), checked for reachability in a thread with a time limit, never watched by
  AirGrabber's rule (the owner may add a rule for it);
- qBittorrent / Transmission on another computer: the owner is asked (`AirGrabberAsk`).

The remote program counts in REMOTE mode, and in FOLDER mode when one is set up (releases
without a .torrent file go there). `derived_folders` is pure; `AirGrabberFolderSync` keeps
`ForgeSettings.airgrabber` up to date on every change of AirGrabber's settings
(`SettingsService.subscribe`). With the rules for watched folders (app/forge/core/rules.py),
AirGrabber's rule watches the folders `airgrabber_uses` marks watched (its switch is "Use
AirGrabber's downloads"): nesting between rules is allowed, so only suggestions and folders
overlapping a library folder or inside a rule's destination are left out. Settings › Sorting
shows each with why (`use_text`), and offers a suggestion "Add a rule for this folder".

Nothing moves from a folder that starts being watched behind the owner's back: when AirGrabber's
settings bring AirGrabber's rule a folder (`rules.newly_watched`), that rule is marked fresh
(`RuleService.mark_fresh`) and the folders are stored in one step no look comes between (the
renamer's `hold_looks`), so what is there waits in the queue once (said in the Activity strip);
after that the rule works as the owner set it. A failure stores nothing; the next change of
either settings tries again. The master switch (`automatic`) is never touched. Logs give no
folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ntpath
import os
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol
from urllib.parse import urlsplit

from app.forge.core.activity import ActivityFeed
from app.forge.core.library import inside, library_folders
from app.forge.models.rules import Rule
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

# -- texts (shown by Settings › Sorting) --

REASONS: dict[AirGrabberWhy, str] = {
    "finished": N_("“Finished downloads” in Settings › Folders"),
    "qbittorrent": N_("qBittorrent on this PC saves AirGrabber's downloads here"),
    "shows": N_("{program} saves AirGrabber's downloads here"),
    "films": N_("{program} saves AirGrabber's films here"),
}
ASKS: dict[AirGrabberAsk, str] = {
    "": "",
    "finished": N_(
        "AirGrabber doesn't know where its finished downloads end up on this PC. Fill in "
        "“Finished downloads” in Settings › Folders, or add a rule for the folder."
    ),
    "other_computer": N_(
        "AirGrabber sends downloads to {program} on another computer. Add a rule for the folder "
        "where they show up on this PC (a shared network folder), or fill in “Finished "
        "downloads” in Settings › Folders."
    ),
    "nas_folder": N_(
        "Download Station keeps AirGrabber's downloads in its own download folder. Add a rule for "
        "the shared folder where they show up on this PC, or fill in “Finished downloads” in "
        "Settings › Folders."
    ),
}
SUGGESTED = N_("Suggested: {reason}")
REACHABLE = N_("This PC can reach it.")
UNREACHABLE = N_("This PC can't reach it right now.")
NOT_CHECKED = N_("Not checked yet.")
OVERLAP = N_(
    "Not watched: it's inside a library folder or a folder a rule moves files to."
)
NEW_FOLDER_ONCE = N_(
    "AirGrabber's downloads now also end up in {folder}. What's there now waits in the queue "
    "once; after that, AirGrabber's rule works as you set it."
)

FolderUseKind = Literal["watched", "suggested", "overlap"]
_NOTES: dict[FolderUseKind, str] = {"watched": "", "suggested": "", "overlap": OVERLAP}


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


def airgrabber_uses(settings: ForgeSettings, destinations: Sequence[Path]) -> list[FolderUse]:
    """What AirGrabber's rule does with each of AirGrabber's folders, in their order (the rules
    for watched folders, app/forge/core/rules.py): a suggestion; "overlap" when it is inside or
    holds a library folder, or is inside a rule's destination (`rules.destinations`); else
    watched. Nesting between rules is allowed: a folder inside another one is watched. A folder that
    holds a rule's own folder destination stays watched (unlike one holding a library folder):
    a look skips every destination, so nothing moved there is found again. Whether AirGrabber's
    rule is on is not asked here."""
    libraries = library_folders(settings)
    uses: list[FolderUse] = []
    for folder in settings.airgrabber.folders:
        path = Path(folder.path)
        use: FolderUseKind
        if folder.suggested:
            use = "suggested"
        elif any(inside(path, lib) or inside(lib, path) for lib in libraries) or any(
            inside(path, dest) for dest in destinations
        ):
            use = "overlap"
        else:
            use = "watched"
        uses.append(FolderUse(folder, use))
    return uses


# -- keeping it up to date ------------------------------------------------------------------------


class _ForgeSettingsSource(Protocol):
    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]: ...


class _RulesSource(Protocol):
    """What the sync needs of RuleService."""

    @property
    def current(self) -> Sequence[Rule]: ...

    @property
    def airgrabber_rule(self) -> Rule: ...

    async def mark_fresh(self, rule_id: int) -> None: ...


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
        rules: _RulesSource | None = None,
        hold_looks: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        qbittorrent_state: Callable[[], QBittorrentState] = _read_qbittorrent_state,
        is_dir: Callable[[str], bool] = os.path.isdir,
        timeout_s: float = REACH_TIMEOUT_S,
        activity: ActivityFeed | None = None,
    ) -> None:
        self._forge = forge_settings
        self._airgrabber = airgrabber_settings
        self._rules = rules  # AirGrabber's rule is marked fresh for a folder it newly watches
        self._hold_looks = hold_looks  # the renamer's: no look between that mark and the store
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
        if self._forge.current.airgrabber == derived:
            self._stored = derived
            return derived
        before = self._stored
        self._stored = derived  # the store's own echo is no import (`_forge_changed`)
        try:
            added = await self._store(derived)
        except BaseException:
            # Nothing stored: the next change of either settings looks again.
            self._stored, self._seen = before, None
            raise
        if added:
            log.info("A new folder of AirGrabber's downloads is watched: its files wait once")
            if self._activity is not None:
                self._activity.say(t(NEW_FOLDER_ONCE, folder=added[0]), level=logging.WARNING)
        return derived

    async def _store(self, derived: AirGrabberDownloads) -> list[str]:
        """Store `derived`; when it brings AirGrabber's rule a folder, mark that rule fresh
        first. Both in one step no look comes between (`hold_looks`): a look sees the new folder
        only with the rule fresh, and can't clear the flag having seen only the old folders.
        Returns the new folders (none without the rules)."""
        rules = self._rules
        hold = self._hold_looks() if self._hold_looks is not None else contextlib.nullcontext()
        async with hold:
            current = self._forge.current
            added: list[str] = []
            if rules is not None:
                from app.forge.core.rules import newly_watched  # it imports this module

                after = current.model_copy(update={"airgrabber": derived})
                added = newly_watched(current, after, rules.current)
            if added and rules is not None:
                # Never move files from a folder that started being watched behind the owner's
                # back (a failure stores nothing).
                await rules.mark_fresh(rules.airgrabber_rule.id)
            await self._forge.update(airgrabber=derived)
        return added

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
