"""Subtitles from OpenSubtitles.com, saved next to a video MediaForge knows.

A rename whose preset has the Subtitles extra asks here right after it (Executor), and so does
CC ▾ on the queue's done bands and History's rows. There are no background checks and no second
tries: one request is one search, and the disk is the only memory. A language whose subtitle is
next to the video already ("<stem>.<lang>.srt", or a variant such as ".<lang>.forced.srt") is
skipped, so no download is spent on it.

`fetch` looks by the file's moviehash first (the best sync), then by what MediaForge knows of the
file (`SubtitleIdentity`: a film's or a series' TMDB id with season and episode, else title and
year). Machine- and AI-translated subtitles are never taken; a moviehash match wins, then the
hearing-impaired choice (Settings › Subtitles: avoid (the default), prefer or either), then the one
made for the same release as the file's original name (same group, then same source), then the most
downloads. It never raises for OpenSubtitles or the folder: the outcome says.

Undo: with a `run_id` (the run that renamed the video) a saved subtitle is journaled in that run
as a created file, so Undo removes it with the run or with its video (app/forge/core/undo.py).

The settings live in AirGrabber's `Settings` (`subtitle_languages`, `subtitles_hearing_impaired`,
`opensubtitles_*`). The user's own free OpenSubtitles account is optional: signed in, the downloads
count against it (20 a day for a free account), without one against this PC's address (5 a day).

Log hygiene: ids and counts only; never a URL, key, password, token or a file's path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from app.forge.clients.opensubtitles import (
    KEY_REJECTED_TEXT,
    LOGIN_REJECTED_TEXT,
    OWN_KEY_REJECTED_TEXT,
    QUOTA_TEXT,
    UNREACHABLE_TEXT,
    OpenSubtitlesClient,
    movie_hash,
)
from app.forge.core import fsops
from app.forge.core.parser import parse_filename
from app.forge.db.history import HistoryRepo
from app.forge.models.history import OpKind
from app.forge.models.media import MediaKind, TypeHint
from app.forge.models.moves import MoveRecord
from app.forge.models.opensubtitles import SubtitleCandidate
from app.forge.models.subtitles import (
    KeyRejected,
    LoginRejected,
    OpenSubtitlesError,
    QuotaExceeded,
    SubtitleAccount,
    SubtitleFetch,
    SubtitleFetchOutcome,
    SubtitleIdentity,
    SubtitleQuota,
    Unreachable,
)
from app.i18n import N_, t
from app.models import HearingImpaired, Settings, SubtitlesFor, SubtitlesMode

if TYPE_CHECKING:
    from app.forge.core.presets_service import PresetService
    from app.forge.core.settings import ForgeSettingsService
    from app.services.settings import SettingsService

log = logging.getLogger(__name__)

T = TypeVar("T")

SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".sub", ".vtt"})

# Journal notes stay in English (the language can change between runs): t() them where shown.
PROBLEM_TEXT = N_("Couldn't get subtitles")
WRITE_FAILED_TEXT = N_(
    "Couldn't save subtitles next to the video. Check that the folder is writable"
)


def quota_text(reset_at: datetime | None) -> str:
    """"Today's subtitle downloads are used up; more after 02:00" (local time)."""
    if reset_at is None:
        return t("Today's subtitle downloads are used up; more tomorrow")
    return t(
        "Today's subtitle downloads are used up; more after {time}",
        time=f"{reset_at.astimezone():%H:%M}",
    )


# A release's source: families that share a sync, and the words for them in a name.
_SOURCE_FAMILIES = {
    "web": frozenset({"web", "webdl", "webrip"}),
    "bluray": frozenset({"bluray", "bdrip", "brrip", "bdremux", "remux"}),
    "tv": frozenset({"hdtv", "pdtv", "dsr"}),
    "dvd": frozenset({"dvd", "dvdrip"}),
}
_SOURCE = re.compile(
    r"(?<![0-9a-z])(web[ ._-]?dl|web[ ._-]?rip|web|blu[ ._-]?ray|bdrip|brrip|bdremux|remux|hdtv"
    r"|pdtv|dsr|dvd[ ._-]?rip|dvd)(?![0-9a-z])",
    re.IGNORECASE,
)
_TECHNICAL = re.compile(
    r"(?<![0-9a-z])(\d{3,4}p|[xh][ .]?26[45]|hevc|avc|xvid|av1)(?![0-9a-z])", re.IGNORECASE
)
_VIDEO_END = re.compile(r"\.[0-9a-z]{2,4}$", re.IGNORECASE)
_BRACKETS_AT_END = re.compile(r"\s*\[[^\]]*\]$")
_GROUP_AT_END = re.compile(r"-([0-9a-z]+)$", re.IGNORECASE)
_NOT_A_GROUP = re.compile(r"dl|rip|ray|web|\d{3,4}p|[xh]26[45]|hevc|avc|xvid|av1", re.IGNORECASE)


def _source_of(name: str) -> str | None:
    """The family of where a release came from (they sync alike), or None."""
    found = _SOURCE.search(name)
    if found is None:
        return None
    word = re.sub(r"[ ._-]", "", found.group(1).casefold())
    return next(family for family, words in _SOURCE_FAMILIES.items() if word in words)


def release_marks(name: str) -> tuple[str | None, str | None]:
    """The (group, source) in a release-style name, casefolded: "…1080p.WEB-DL.x265-NTb.mkv" ->
    ("ntb", "web"). A name without release words has no group ("Spider-Man.mkv" isn't by "Man")."""
    stem = _BRACKETS_AT_END.sub("", _VIDEO_END.sub("", name.strip()))
    source = _source_of(stem)
    if source is None and _TECHNICAL.search(stem) is None:
        return None, None
    last = _GROUP_AT_END.search(stem)
    group = last.group(1).casefold() if last else None
    if group is not None and (group.isdigit() or _NOT_A_GROUP.fullmatch(group)):
        group = None
    return group, source


def _release_fit(release: str, marks: tuple[str | None, str | None]) -> tuple[bool, bool]:
    """How a subtitle's release (free text at OpenSubtitles) fits the video's: (group, source)."""
    group, source = marks
    words = re.split(r"[^0-9a-z]+", release.casefold())
    return (
        group is not None and group in words,
        source is not None and _source_of(release) == source,
    )


def pick(
    found: Iterable[SubtitleCandidate],
    language: str,
    hearing_impaired: HearingImpaired = HearingImpaired.AVOID,
    release: str = "",
) -> SubtitleCandidate | None:
    """The best subtitle in `language`: a moviehash match first, then the hearing-impaired
    choice (AVOID: one without; PREFER: one with; EITHER: no matter), then the one made for the
    same release as `release` (the video's original name), then the most downloads.
    (Machine- and AI-translated ones never reach this.)"""
    wanted = language.lower()
    matches = [c for c in found if c.language.lower() == wanted]
    if not matches:
        return None

    def fits(candidate: SubtitleCandidate) -> bool:
        if hearing_impaired is HearingImpaired.AVOID:
            return not candidate.hearing_impaired
        if hearing_impaired is HearingImpaired.PREFER:
            return candidate.hearing_impaired
        return True

    marks = release_marks(release)

    def rank(candidate: SubtitleCandidate) -> tuple[bool, bool, tuple[bool, bool], int]:
        fit = _release_fit(candidate.release, marks)
        return (candidate.moviehash_match, fits(candidate), fit, candidate.download_count)

    return max(matches, key=rank)


def subtitle_path(video: Path, language: str) -> Path:
    """"…/Show.S01E02.mkv" + "en" -> "…/Show.S01E02.en.srt" (next to the video)."""
    return video.with_name(f"{video.stem}.{language}.srt")


def has_subtitle(video: Path, language: str, names: Iterable[str]) -> bool:
    """A subtitle for this language is next to the video already: "<stem>.<lang>.srt", or a
    variant such as "<stem>.<lang>.forced.srt" / ".<lang>.ass" (`names`: its folder's files)."""
    prefix = f"{video.stem}.{language}.".casefold()
    return any(
        name.casefold().startswith(prefix)
        and os.path.splitext(name)[1].lower() in SUBTITLE_EXTENSIONS
        for name in names
    )


def write_new_file(path: Path, content: bytes) -> tuple[int, int] | None:
    """Write `content` to `path` atomically, never over an existing file: written to a temporary
    file next to it, then linked (or, where links aren't possible, renamed, which Windows refuses
    over an existing file) into place. Returns its (size, mtime_ns); None when `path` exists
    already. Blocking; raises OSError when the folder can't be written."""
    target = fsops.long_path(path)
    if os.path.exists(target):
        return None
    temporary = f"{target}.airgrabber-{os.getpid()}.tmp"
    with open(temporary, "xb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            return None
        except OSError:  # no hard links here (FAT, some network shares)
            if os.path.exists(target):
                return None
            try:
                os.rename(temporary, target)
            except FileExistsError:
                return None
        return fsops.stamp(path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def file_identity(
    name: str, record: MoveRecord | None = None, original_name: str = ""
) -> SubtitleIdentity:
    """The identity of a file known only by its name and maybe a move record (History's rows):
    the record's kind and TMDB id, the rest read from the name; `original_name`: its name before
    the rename."""
    parsed = parse_filename(name, TypeHint.AUTO)
    return SubtitleIdentity(
        kind=record.kind if record is not None else parsed.kind,
        title=parsed.title,
        year=parsed.year,
        tmdb_id=record.tmdb_id if record is not None else None,
        season=parsed.season,
        episode=parsed.episode,
        release=original_name,
    )


def _reason_of(exc: OpenSubtitlesError, *, own_key: bool) -> str:
    """The English note of a stop (a journal note, t() where shown)."""
    if isinstance(exc, KeyRejected):
        return OWN_KEY_REJECTED_TEXT if own_key else KEY_REJECTED_TEXT
    if isinstance(exc, LoginRejected):
        return LOGIN_REJECTED_TEXT
    if isinstance(exc, Unreachable):
        return UNREACHABLE_TEXT
    return PROBLEM_TEXT


def _is_one_off(exc: OpenSubtitlesError) -> bool:
    """An error about one subtitle (an odd answer), not about the key, account or connection."""
    return not isinstance(exc, (KeyRejected, LoginRejected, Unreachable))


def _next_midnight_utc(now: datetime) -> datetime:
    """OpenSubtitles' download counters start again at midnight UTC."""
    tomorrow = (now.astimezone(UTC) + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _finish_even_if_cancelled(work: Awaitable[T]) -> T:
    """Await `work` to its end; a cancellation meanwhile goes through afterwards (a file being
    saved and journaled is never left halfway)."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - task.result() raises it below
            break
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


def combine_fetches(results: Sequence[SubtitleFetch]) -> SubtitleFetch:
    """One outcome for several videos' requests (a band's): what stopped them, else what the
    languages add up to."""
    saved = tuple(dict.fromkeys(code for r in results for code in r.saved))
    already = tuple(dict.fromkeys(code for r in results for code in r.already))
    missing = tuple(dict.fromkeys(code for r in results for code in r.not_found))
    for stopping in (SubtitleFetchOutcome.QUOTA, SubtitleFetchOutcome.PROBLEM):
        stop = next((r for r in results if r.outcome is stopping), None)
        if stop is not None:
            return SubtitleFetch(
                outcome=stopping,
                saved=saved,
                already=already,
                not_found=missing,
                message=stop.message,
                reason=stop.reason,
            )
    outcomes = {r.outcome for r in results}
    if outcomes and outcomes <= {SubtitleFetchOutcome.NO_VIDEO}:
        outcome = SubtitleFetchOutcome.NO_VIDEO
    elif outcomes and outcomes <= {SubtitleFetchOutcome.UNAVAILABLE}:
        outcome = SubtitleFetchOutcome.UNAVAILABLE
    elif missing:
        outcome = (
            SubtitleFetchOutcome.PARTLY if saved or already else SubtitleFetchOutcome.NOT_FOUND
        )
    else:
        outcome = SubtitleFetchOutcome.SAVED if saved else SubtitleFetchOutcome.ALREADY
    return SubtitleFetch(outcome=outcome, saved=saved, already=already, not_found=missing)


class _Stop(Exception):
    """Ends a request: `outcome` with its message and English reason."""

    def __init__(self, outcome: SubtitleFetchOutcome, message: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.message = message
        self.reason = reason


class AirGrabberSettings(Protocol):
    """What this reads of AirGrabber's SettingsService (where the subtitle settings live)."""

    @property
    def current(self) -> Settings: ...


class ForgeSubtitles:
    """Finds and saves subtitles (see the module docstring); Settings › Subtitles checks the key
    and the account through it."""

    def __init__(
        self,
        client: OpenSubtitlesClient,
        settings: AirGrabberSettings,
        history: HistoryRepo | None = None,
        *,
        own_key: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client = client
        self._settings = settings
        self._history = history
        self._own_key = own_key
        self._clock = clock
        self._quota = SubtitleQuota()

    # --- what can be asked ---------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """This version ships an OpenSubtitles API key (or the user has one of their own):
        subtitles can be offered at all."""
        return self._client.configured

    @property
    def languages(self) -> tuple[str, ...]:
        """The languages chosen in Settings › Subtitles, in order."""
        return self._settings.current.subtitle_languages

    @property
    def quota(self) -> SubtitleQuota:
        """Downloads left today, as OpenSubtitles last said (nothing known before a download or
        a sign-in)."""
        return self._quota

    @property
    def used_up(self) -> bool:
        """Today's allowance is known to be used up, until the count starts again."""
        quota = self._quota
        return (
            quota.remaining == 0 and quota.reset_at is not None and self._clock() < quota.reset_at
        )

    # --- Settings' checks ----------------------------------------------------------------------

    async def sign_in(self, username: str, password: str) -> SubtitleAccount:
        """Sign in with these (unsaved) details; raises LoginRejected / KeyRejected /
        Unreachable. The downloads left today become the known quota."""
        account = await self._client.sign_in(username, password)
        self._quota = SubtitleQuota(
            remaining=account.remaining_downloads,
            allowed=account.allowed_downloads,
            reset_at=self._quota.reset_at,
        )
        return account

    async def check_key(self, key: str) -> None:
        """Check the user's own API key (unsaved) with OpenSubtitles; raises KeyRejected /
        Unreachable / OpenSubtitlesError in plain words, without the key."""
        await self._client.check_key(key)

    # --- requests ------------------------------------------------------------------------------

    async def missing(self, video: Path, languages: Sequence[str] | None = None) -> tuple[str, ...]:
        """Which of `languages` (default: the chosen ones) have no subtitle next to `video`."""
        wanted = tuple(dict.fromkeys(self.languages if languages is None else languages))
        names = await asyncio.to_thread(fsops.list_names, video.parent)
        return tuple(code for code in wanted if not has_subtitle(video, code, names))

    async def fetch(
        self,
        video: Path,
        identity: SubtitleIdentity,
        languages: Sequence[str] | None = None,
        *,
        run_id: int | None = None,
    ) -> SubtitleFetch:
        """One try for `languages` (default: the chosen ones) of `video`. `run_id`: the run that
        renamed it, where a saved subtitle is journaled for Undo. Never raises for OpenSubtitles
        or the folder."""
        wanted = tuple(dict.fromkeys(self.languages if languages is None else languages))
        if not self._client.configured or not wanted:
            return SubtitleFetch(outcome=SubtitleFetchOutcome.UNAVAILABLE)
        if not await asyncio.to_thread(fsops.exists, video):
            log.info("Subtitles: the video is gone")
            return SubtitleFetch(outcome=SubtitleFetchOutcome.NO_VIDEO, not_found=wanted)
        needed = await self.missing(video, wanted)
        already = tuple(code for code in wanted if code not in needed)
        if not needed:
            return SubtitleFetch(outcome=SubtitleFetchOutcome.ALREADY, already=already)
        saved: list[str] = []
        stop: _Stop | None = None
        try:
            self._check_quota()
            await self._sign_in_saved()
            found = await self._search(video, identity, needed)
            hearing = self._settings.current.subtitles_hearing_impaired
            for code in needed:
                choice = pick(found, code, hearing, identity.release)
                if choice is None:
                    continue
                self._check_quota()
                if await self._get(video, code, choice, run_id):
                    saved.append(code)
        except _Stop as stopped:
            stop = stopped
        except OpenSubtitlesError as exc:
            stop = self._stop_for(exc)
        except OSError:
            log.warning("Subtitles: the video's folder can't be written")
            stop = _Stop(SubtitleFetchOutcome.PROBLEM, t(WRITE_FAILED_TEXT), WRITE_FAILED_TEXT)
        missing = tuple(code for code in needed if code not in saved)
        log.info("Subtitles: %d saved, %d not there", len(saved), len(missing))
        if stop is not None and missing:
            return SubtitleFetch(
                outcome=stop.outcome,
                saved=tuple(saved),
                already=already,
                not_found=missing,
                message=stop.message,
                reason=stop.reason,
            )
        if missing:
            outcome = (
                SubtitleFetchOutcome.PARTLY if saved or already else SubtitleFetchOutcome.NOT_FOUND
            )
        else:
            outcome = SubtitleFetchOutcome.SAVED
        return SubtitleFetch(
            outcome=outcome, saved=tuple(saved), already=already, not_found=missing
        )

    def _check_quota(self) -> None:
        if self.used_up:
            raise _Stop(SubtitleFetchOutcome.QUOTA, quota_text(self._quota.reset_at), QUOTA_TEXT)

    def _stop_for(self, exc: OpenSubtitlesError) -> _Stop:
        if isinstance(exc, QuotaExceeded):
            self._quota_used_up(exc.reset_at)
            return _Stop(SubtitleFetchOutcome.QUOTA, quota_text(self._quota.reset_at), QUOTA_TEXT)
        return _Stop(
            SubtitleFetchOutcome.PROBLEM, str(exc), _reason_of(exc, own_key=self._own_key())
        )

    async def _sign_in_saved(self) -> None:
        settings = self._settings.current
        await self._client.ensure_session(
            settings.opensubtitles_username, settings.opensubtitles_password.get_secret_value()
        )

    async def _search(
        self, video: Path, identity: SubtitleIdentity, languages: Sequence[str]
    ) -> list[SubtitleCandidate]:
        """Moviehash first; then the identity for the languages still missing."""
        wanted = ",".join(sorted(code.lower() for code in languages))
        found: list[SubtitleCandidate] = []
        try:
            digest = await asyncio.to_thread(movie_hash, fsops.long_path(video))
        except OSError:
            digest = None
        if digest is not None:
            found += await self._client.search(
                {"moviehash": digest, "languages": wanted, "ai_translated": "exclude"}
            )
        left = [code for code in languages if pick(found, code) is None]
        if not left:
            return found
        params: dict[str, str | int] = {
            "languages": ",".join(sorted(code.lower() for code in left)),
            "ai_translated": "exclude",
        }
        name = identity.title or video.stem
        if identity.kind is MediaKind.EPISODE:
            if identity.tmdb_id is not None:
                params["parent_tmdb_id"] = identity.tmdb_id
            else:
                params["query"] = name
            if identity.season is not None and identity.episode is not None:
                params["season_number"] = identity.season
                params["episode_number"] = identity.episode
            params["type"] = "episode"
        else:
            if identity.tmdb_id is not None:
                params["tmdb_id"] = identity.tmdb_id
            else:
                params["query"] = name
                if identity.year is not None:
                    params["year"] = identity.year
            params["type"] = "movie"
        return found + await self._client.search(params)

    async def _get(
        self, video: Path, language: str, choice: SubtitleCandidate, run_id: int | None
    ) -> bool:
        """Download one subtitle and save it next to the video. False when OpenSubtitles gave
        nothing usable for this one (the request goes on)."""
        try:
            link = await self._client.download_link(choice.file_id)
            self._quota = SubtitleQuota(
                remaining=link.remaining,
                allowed=self._quota.allowed,
                reset_at=link.reset_at or self._quota.reset_at,
            )
            if link.remaining is not None and link.remaining <= 0:
                self._quota_used_up(link.reset_at)
            content = await self._client.fetch(link.link)
        except OpenSubtitlesError as exc:
            if isinstance(exc, QuotaExceeded) or not _is_one_off(exc):
                raise
            log.info("Subtitles: file %d: %s", choice.file_id, type(exc).__name__)
            return False
        if not await asyncio.to_thread(fsops.exists, video):  # undone or moved meanwhile
            log.info("Subtitles: file %d: its video is gone", choice.file_id)
            return False
        return await _finish_even_if_cancelled(
            self._save(subtitle_path(video, language), content, run_id)
        )

    async def _save(self, target: Path, content: bytes, run_id: int | None) -> bool:
        """Write it (journaled in `run_id` when that run is still kept). True when it is there:
        written now, or one appeared there meanwhile (kept as it is, not journaled as ours)."""
        op = await self._pending(run_id, target)
        history = self._history
        try:
            stamp = await asyncio.to_thread(write_new_file, target, content)
        except OSError as exc:
            if op is not None and history is not None:
                await history.mark_failed(op, exc.strerror or type(exc).__name__)
            raise
        if op is not None and history is not None:
            if stamp is None:
                await history.mark_failed(op, "already there")
            else:
                await history.mark_done(op, size=stamp[0], mtime_ns=stamp[1])
        return True

    async def _pending(self, run_id: int | None, target: Path) -> int | None:
        if run_id is None or self._history is None:
            return None
        try:
            if not await self._history.operations(run_id):
                return None  # the run left the history: nothing can undo it anyway
            return await self._history.record_pending(run_id, OpKind.CREATE_FILE, None, target)
        except Exception as exc:  # noqa: BLE001 - the journal never stops a subtitle
            log.warning("Subtitles: couldn't journal one (%s)", type(exc).__name__)
            return None

    def _quota_used_up(self, reset_at: datetime | None) -> None:
        self._quota = SubtitleQuota(
            remaining=0,
            allowed=self._quota.allowed,
            reset_at=reset_at or self._quota.reset_at or _next_midnight_utc(self._clock()),
        )


# --- the one-time carry ----------------------------------------------------------------------


def carried_types(for_what: SubtitlesFor) -> frozenset[TypeHint]:
    """The presets AirGrabber's "For" choice applies to."""
    if for_what is SubtitlesFor.SERIES:
        return frozenset({TypeHint.TV, TypeHint.AUTO})
    if for_what is SubtitlesFor.FILMS:
        return frozenset({TypeHint.MOVIE, TypeHint.AUTO})
    return frozenset(TypeHint)


async def carry_subtitles(
    settings: ForgeSettingsService, presets: PresetService, airgrabber: SettingsService
) -> None:
    """At Forge's start, once: whoever had automatic subtitles in AirGrabber keeps them, as the
    Subtitles extra on the presets it applied to. Marked as carried, so turning the extra off
    stays off. A failure is logged and tried at the next start; it never stops Forge."""
    if settings.current.subtitles_carried:
        return
    try:
        old = airgrabber.current
        if old.subtitles_enabled and old.subtitles_mode is SubtitlesMode.AUTO:
            types = carried_types(old.subtitles_for)
            for preset in await presets.list():
                if preset.type in types and not preset.options.subtitles:
                    options = preset.options.model_copy(update={"subtitles": True})
                    await presets.save(preset.model_copy(update={"options": options}))
            log.info("Automatic subtitles carried over to the presets")
        await settings.update(subtitles_carried=True)
    except Exception as exc:  # noqa: BLE001 - forge.db: tried again at the next start
        log.warning("Couldn't carry over the subtitle settings (%s)", type(exc).__name__)
