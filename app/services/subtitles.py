"""Subtitles from OpenSubtitles.com for what AirGrabber grabbed, saved next to the videos.

A run (a minute after startup, then every 30 minutes, and soon after the subtitle settings
change; one at a time) does nothing unless this version ships an OpenSubtitles API key
(app.services.opensubtitles.APP_API_KEY) and "Get subtitles for my downloads" is on. It looks
after the kinds of downloads Settings → Subtitles → For names (series, films or both), and with
When = "Only when I ask" (`subtitles_mode`) it only notes the subtitles already next to the
videos and fetches nothing. The user's
own free OpenSubtitles account is optional: signed in (once per run) the downloads count
against it (20 a day for a free account), without one against this PC's address (5 a day).
It takes
the grabs of the last LOOKBACK that aren't complete for the chosen languages, scans the
finished-downloads folder once (app/services/subtitle_files.py, in a thread) and matches their
video files. For every video and language without a subtitle yet:

- a subtitle file already next to it ("<stem>.<lang>.srt" or a variant) counts as saved;
- else, unless tried within RETRY_AFTER, it searches OpenSubtitles: by the file's moviehash
  first (the best sync), then by IMDb id (a show's id with season and episode; a film's from
  TMDB's details) or, without one, by name. Machine- and AI-translated subtitles are never
  taken; a moviehash match wins, then the hearing-impaired choice (Settings: avoid (the
  default), prefer or either), then the most downloads. The pick is saved as
  "<video stem>.<lang>.srt" next to the video (atomic, never over an existing file).
- nothing found counts a try; GIVE_UP_AFTER the first try that video and language are given up.

`fetch_now` does the same for one episode or film at once (the CC menus of the show and film
windows and the Downloads page, offered while the switch is on: `offer`), in whatever mode,
for the languages asked: the newest grab of it with its video in the folder, the day's quota
respected, and the RETRY_AFTER wait and a given-up language only when not `force`d (a request
by hand may try again at once). Its outcome is a SubtitleFetch.

A grab whose videos all have their languages (or were given up), with nothing of it still
downloading, is complete and not looked at again until the languages change. When today's
downloads are used up the run stops and says when they start again (midnight UTC); runs skip
until then. A refused app key, a wrong password, an unreachable OpenSubtitles or folder stop the
run with a SubtitleProblem in plain words (the same one at most every PROBLEM_REPEAT).

Log hygiene: episode labels, file names and outcomes only — never a URL, key, password or token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.database import SubtitleRepository
from app.i18n import N_, t
from app.models import (
    DownloadRecord,
    HearingImpaired,
    MediaType,
    Settings,
    Show,
    SubtitleAccount,
    SubtitleEvent,
    SubtitleFetch,
    SubtitleFetchOutcome,
    SubtitleFile,
    SubtitleOffer,
    SubtitleProblem,
    SubtitleQuota,
    SubtitleRunReport,
    SubtitlesFor,
    SubtitlesMode,
    SubtitlesSaved,
)
from app.models.opensubtitles import SubtitleCandidate
from app.services.opensubtitles import (
    KeyRejected,
    LoginRejected,
    OpenSubtitlesClient,
    OpenSubtitlesError,
    QuotaExceeded,
    Unreachable,
    imdb_number,
    movie_hash,
)
from app.services.settings import SettingsService
from app.services.subtitle_files import (
    FolderScan,
    GrabVideo,
    grab_files,
    has_subtitle,
    movie_identity,
    scan_folder,
    subtitle_path,
    write_new_file,
)

log = logging.getLogger(__name__)

SubtitleListener = Callable[[SubtitleEvent], None]

LOOKBACK = timedelta(days=30)  # grabs this recent are looked after
RETRY_AFTER = timedelta(hours=4)  # a video and language without a subtitle is searched again
GIVE_UP_AFTER = timedelta(days=5)  # after its first try
RUN_INTERVAL_S = 1800.0
START_DELAY_S = 60.0  # let the window and the library settle first
PROBLEM_REPEAT = timedelta(hours=3)
SETTINGS_FIELDS = frozenset(
    {
        "subtitles_enabled",
        "subtitle_languages",
        "subtitles_folder",
        "subtitles_mode",
        "subtitles_hearing_impaired",
        "subtitles_for",
        "opensubtitles_username",
        "opensubtitles_password",
        "opensubtitles_own_key",
    }
)
# The kinds of downloads each "For" choice fetches subtitles for.
FOR_KINDS: dict[SubtitlesFor, frozenset[MediaType]] = {
    SubtitlesFor.BOTH: frozenset({MediaType.TV, MediaType.MOVIE}),
    SubtitlesFor.SERIES: frozenset({MediaType.TV}),
    SubtitlesFor.FILMS: frozenset({MediaType.MOVIE}),
}

# Raised through t().
NO_FOLDER_TEXT = N_("Choose where finished downloads end up (Settings, Subtitles)")
WRITE_FAILED_TEXT = N_(
    "Couldn't save subtitles next to the videos. Check that the folder is writable"
)


def folder_unreachable_text(folder: str) -> str:
    return t(
        "Couldn't open {folder}. Check that the folder (or the NAS) is reachable", folder=folder
    )


def quota_text(reset_at: datetime | None) -> str:
    """"Today's subtitle downloads are used up; more after 02:00" (local time)."""
    if reset_at is None:
        return t("Today's subtitle downloads are used up; more tomorrow")
    return t(
        "Today's subtitle downloads are used up; more after {time}",
        time=f"{reset_at.astimezone():%H:%M}",
    )


def pick(
    found: Iterable[SubtitleCandidate],
    language: str,
    hearing_impaired: HearingImpaired = HearingImpaired.AVOID,
) -> SubtitleCandidate | None:
    """The best subtitle in `language`: a moviehash match first, then the hearing-impaired
    choice (AVOID: one without; PREFER: one with; EITHER: no matter), then the most downloads.
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

    return max(matches, key=lambda c: (c.moviehash_match, fits(c), c.download_count))


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _Run:
    videos: int = 0
    saved: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    gave_up: list[str] = field(default_factory=list)
    problem: str | None = None


class _StopRun(Exception):
    """Ends the run: `message` is the SubtitleProblem (`quota`: today's downloads are used
    up)."""

    def __init__(self, message: str, *, quota: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.quota = quota


class SubtitleService:
    """Runs the subtitle downloads (see the module docstring); Settings checks the key and the
    account through it."""

    def __init__(
        self,
        repository: SubtitleRepository,
        client: OpenSubtitlesClient,
        settings: SettingsService,
        *,
        show: Callable[[int], Awaitable[Show | None]],
        movie_imdb_id: Callable[[int], Awaitable[str | None]],
        clock: Callable[[], datetime] = _utc_now,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """`show`: a TV show by TVMaze id (its name and IMDb id), None when unknown.
        `movie_imdb_id`: a film's IMDb id by TMDB id, None when it can't be had. `clock`: the
        current UTC instant; `wall_clock`: seconds as file times count them (tests move both)."""
        self._repository = repository
        self._client = client
        self._settings = settings
        self._show = show
        self._movie_imdb_id = movie_imdb_id
        self._clock = clock
        self._wall_clock = wall_clock
        self._listeners: list[SubtitleListener] = []
        self._run_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._quota = SubtitleQuota()
        self._last_problem: tuple[str, datetime] | None = None
        settings.subscribe(self._on_settings)
        self._settings_seen = settings.current

    # -- listeners, state ------------------------------------------------------------------------

    def subscribe(self, listener: SubtitleListener) -> Callable[[], None]:
        """Call `listener` with every SubtitlesSaved / SubtitleProblem. Returns a function that
        unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: SubtitleEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("Subtitle listener %r failed", listener)

    @property
    def available(self) -> bool:
        """This version ships an OpenSubtitles API key: subtitles can be offered at all."""
        return self._client.configured

    @property
    def offer(self) -> SubtitleOffer:
        """Whether subtitles can be asked for by hand: not at all (NONE) without a key in this
        version, a finished-downloads folder or a language; OFF while Settings' switch ("Get
        subtitles for my downloads", the master switch) is off; else ON."""
        settings = self._settings.current
        if not (
            self._client.configured
            and settings.subtitles_folder.strip()
            and settings.subtitle_languages
        ):
            return SubtitleOffer.NONE
        return SubtitleOffer.ON if settings.subtitles_enabled else SubtitleOffer.OFF

    @property
    def can_fetch(self) -> bool:
        """Subtitles can be asked for by hand now (`offer` is ON)."""
        return self.offer is SubtitleOffer.ON

    @property
    def quota(self) -> SubtitleQuota:
        """Downloads left today, as OpenSubtitles last said (nothing known before a download or
        a sign-in)."""
        return self._quota

    def _on_settings(self, updated: Settings) -> None:
        previous, self._settings_seen = self._settings_seen, updated
        if any(getattr(previous, name) != getattr(updated, name) for name in SETTINGS_FIELDS):
            self._last_problem = None  # a fixed setting may fail differently: say so at once
            self._wake.set()

    async def show_languages(self, show_id: int) -> dict[tuple[int, int], frozenset[str]]:
        """Per (season, episode) of a TV show, the languages of the subtitles saved for it."""
        return await self._repository.show_languages(show_id)

    async def movie_languages(self, movie_id: int) -> frozenset[str]:
        """The languages of the subtitles saved for a film (TMDB id)."""
        return await self._repository.movie_languages(movie_id)

    # -- Settings' checks --------------------------------------------------------------------------

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

    # -- runs ------------------------------------------------------------------------------------

    async def run_periodic(
        self, interval_s: float = RUN_INTERVAL_S, start_delay_s: float = START_DELAY_S
    ) -> None:
        """Wait `start_delay_s`, then run every `interval_s` seconds (sooner after a subtitle
        setting changed), until cancelled. A failed run is logged and the loop carries on."""
        await self._sleep_or_wake(start_delay_s)
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("Subtitle run failed")
            await self._sleep_or_wake(interval_s)

    async def _sleep_or_wake(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            return
        await asyncio.sleep(2.0)  # several settings saved together: one run

    async def run_once(self) -> SubtitleRunReport:
        """One run (see the module docstring); runs never overlap."""
        async with self._run_lock:
            run = _Run()
            try:
                await self._run(run)
            except _StopRun as stop:
                run.problem = stop.message
                self._report_problem(stop.message)
            return SubtitleRunReport(
                videos=run.videos,
                saved=tuple(run.saved),
                not_found=tuple(run.not_found),
                gave_up=tuple(run.gave_up),
                problem=run.problem,
            )

    def _report_problem(self, message: str) -> None:
        now = self._clock()
        last = self._last_problem
        if last is not None and last[0] == message and now - last[1] < PROBLEM_REPEAT:
            return
        self._last_problem = (message, now)
        log.warning("Subtitles: %s", message)
        self._emit(SubtitleProblem(message=message))

    async def _run(self, run: _Run) -> None:
        settings = self._settings.current
        languages = settings.subtitle_languages
        if not self._client.configured or not settings.subtitles_enabled or not languages:
            return
        now = self._clock()
        quota = self._quota
        if quota.remaining == 0 and quota.reset_at is not None and now < quota.reset_at:
            return  # nothing can be downloaded before the count starts again
        kinds = FOR_KINDS[settings.subtitles_for]
        grabs = [
            grab
            for grab in await self._repository.grabs_since(now - LOOKBACK)
            if grab.record.media_type in kinds
            and not set(languages) <= set(grab.done_languages)
        ]
        if not grabs:
            return
        folder = settings.subtitles_folder
        if not folder:
            raise _StopRun(t(NO_FOLDER_TEXT))
        try:
            scan = await asyncio.to_thread(scan_folder, Path(folder), now=self._wall_clock())
        except OSError:
            raise _StopRun(folder_unreachable_text(folder)) from None
        if scan.truncated:
            log.info("Subtitles: the downloads folder is large; only part of it was looked at")

        shows: dict[int, Show | None] = {}
        movies: dict[int, str | None] = {}
        work: list[tuple[DownloadRecord, list[GrabVideo], bool]] = []
        for grab in grabs:
            record = grab.record
            show = None
            if record.media_type is MediaType.TV:
                if record.media_id not in shows:
                    shows[record.media_id] = await self._show(record.media_id)
                show = shows[record.media_id]
            files = grab_files(record, scan, show_name=show.name if show else None)
            work.append((record, list(files.videos), files.incomplete))
        states = await self._repository.files(
            video.video.path for _record, videos, _incomplete in work for video in videos
        )
        session_ready = False
        asking = settings.subtitles_mode is SubtitlesMode.ASK  # note what's there, fetch nothing
        for record, videos, incomplete in work:
            complete = bool(videos) and not incomplete
            for video in videos:
                run.videos += 1
                needed = await self._needed(record, video, languages, states, scan, now)
                if needed is None:
                    complete = False
                    continue
                if not needed:
                    continue
                if asking:
                    complete = False
                    continue
                if not session_ready:
                    await self._sign_in_saved(settings)
                    session_ready = True
                _saved, done = await self._fetch(
                    record, video, needed, shows, movies, states, run
                )
                if not done:
                    complete = False
            if complete and record.id is not None:
                await self._repository.mark_grab_done(record.id, languages, self._clock())

    # -- one episode or film, asked for by hand -----------------------------------------------

    async def fetch_now(
        self,
        media_type: MediaType,
        media_id: int,
        season: int | None,
        episode: int | None,
        languages: Sequence[str],
        *,
        force: bool = False,
    ) -> SubtitleFetch:
        """Fetch `languages` for one episode (a TVMaze show id with season and episode) or
        film (a TMDB id) now, whatever the When / For settings say (see the module docstring).
        `force`: try again at once, also a language tried within RETRY_AFTER or given up. Waits
        for a run in progress; never raises for OpenSubtitles or the folder (the outcome
        says)."""
        settings = self._settings.current
        wanted = list(dict.fromkeys(languages))
        if not self._client.configured or not wanted:
            return SubtitleFetch(outcome=SubtitleFetchOutcome.UNAVAILABLE)
        if not settings.subtitles_folder.strip():
            return SubtitleFetch(
                outcome=SubtitleFetchOutcome.UNAVAILABLE, message=t(NO_FOLDER_TEXT)
            )
        async with self._run_lock:
            try:
                return await self._fetch_now(
                    settings, media_type, media_id, season, episode, wanted, force=force
                )
            except _StopRun as stop:
                outcome = (
                    SubtitleFetchOutcome.QUOTA if stop.quota else SubtitleFetchOutcome.PROBLEM
                )
                log.info("Subtitles asked for by hand: %s", stop.message)
                return SubtitleFetch(outcome=outcome, message=stop.message)

    async def _fetch_now(
        self,
        settings: Settings,
        media_type: MediaType,
        media_id: int,
        season: int | None,
        episode: int | None,
        languages: list[str],
        *,
        force: bool,
    ) -> SubtitleFetch:
        now = self._clock()
        quota = self._quota
        if quota.remaining == 0 and quota.reset_at is not None and now < quota.reset_at:
            return SubtitleFetch(
                outcome=SubtitleFetchOutcome.QUOTA, message=quota_text(quota.reset_at)
            )
        records = await self._repository.grabs_of(media_type, media_id, season, episode)
        if not records:
            return SubtitleFetch(outcome=SubtitleFetchOutcome.NO_GRAB)
        folder = settings.subtitles_folder
        try:
            scan = await asyncio.to_thread(scan_folder, Path(folder), now=self._wall_clock())
        except OSError:
            raise _StopRun(folder_unreachable_text(folder)) from None
        shows: dict[int, Show | None] = {}
        if media_type is MediaType.TV:
            shows[media_id] = await self._show(media_id)
        show = shows.get(media_id)
        record: DownloadRecord | None = None
        videos: list[GrabVideo] = []
        incomplete = False
        for candidate in records:  # the newest grab with its video in the folder
            files = grab_files(candidate, scan, show_name=show.name if show else None)
            incomplete = incomplete or files.incomplete
            found = [
                video
                for video in files.videos
                if media_type is MediaType.MOVIE
                or episode is None  # a season pack asked for as a whole (the Downloads page)
                or (video.season, video.episode) == (season, episode)
            ]
            if found:
                record, videos = candidate, found
                break
        if record is None:
            return SubtitleFetch(
                outcome=(
                    SubtitleFetchOutcome.DOWNLOADING
                    if incomplete
                    else SubtitleFetchOutcome.NO_VIDEO
                )
            )

        states = await self._repository.files(video.video.path for video in videos)
        there: set[str] = set()
        saved: set[str] = set()
        waiting = False
        movies: dict[int, str | None] = {}
        run = _Run()
        session_ready = False
        noted: list[str] = []  # found next to the video, new to the bookkeeping
        for video in videos:
            path = video.video.path
            needed: list[str] = []
            for language in languages:
                if has_subtitle(path, language, scan.subtitles):
                    known = states.get((path, language))
                    if known is None or known.saved_at is None:
                        noted.append(language)
                    await self._repository.record_saved(
                        path,
                        language,
                        record=record,
                        season=video.season,
                        episode=video.episode,
                        subtitle_path=subtitle_path(path, language),
                        when=now,
                    )
                    there.add(language)
                    continue
                state = states.get((path, language))
                if not force and state is not None and (
                    state.gave_up
                    or (state.last_try is not None and now - state.last_try < RETRY_AFTER)
                ):
                    waiting = True
                    continue
                needed.append(language)
            if not needed:
                continue
            if not session_ready:
                await self._sign_in_saved(settings)
                session_ready = True
            got, _settled = await self._fetch(record, video, needed, shows, movies, states, run)
            saved.update(got)
        if noted:  # the marks show them now
            label = _label(record, videos[0], show)
            self._emit(SubtitlesSaved(label=label, languages=tuple(noted)))
        have = there | saved
        missing = tuple(language for language in languages if language not in have)
        if not missing:
            outcome = SubtitleFetchOutcome.SAVED if saved else SubtitleFetchOutcome.ALREADY
        elif have:
            outcome = SubtitleFetchOutcome.PARTLY
        elif waiting and not run.not_found and not run.gave_up:
            outcome = SubtitleFetchOutcome.WAIT
        else:
            outcome = SubtitleFetchOutcome.NOT_FOUND
        return SubtitleFetch(
            outcome=outcome,
            saved=tuple(language for language in languages if language in have),
            not_found=missing,
        )

    async def _needed(
        self,
        record: DownloadRecord,
        video: GrabVideo,
        languages: Iterable[str],
        states: dict[tuple[str, str], SubtitleFile],
        scan: FolderScan,
        now: datetime,
    ) -> list[str] | None:
        """The languages to search for this video now; None when some wait for a later try."""
        path = video.video.path
        needed: list[str] = []
        waiting = False
        for language in languages:
            state = states.get((path, language))
            if state is not None and (state.saved_at is not None or state.gave_up):
                continue
            if has_subtitle(path, language, scan.subtitles):
                await self._repository.record_saved(
                    path,
                    language,
                    record=record,
                    season=video.season,
                    episode=video.episode,
                    subtitle_path=subtitle_path(path, language),
                    when=now,
                )
                continue
            if state is not None and state.last_try is not None and now - state.last_try < RETRY_AFTER:
                waiting = True
                continue
            needed.append(language)
        if waiting and not needed:
            return None
        return needed

    async def _sign_in_saved(self, settings: Settings) -> None:
        username = settings.opensubtitles_username
        password = settings.opensubtitles_password.get_secret_value()
        try:
            await self._client.ensure_session(username, password)
        except OpenSubtitlesError as exc:  # LoginRejected, KeyRejected, Unreachable
            raise _StopRun(str(exc)) from None

    async def _fetch(
        self,
        record: DownloadRecord,
        video: GrabVideo,
        languages: list[str],
        shows: dict[int, Show | None],
        movies: dict[int, str | None],
        states: dict[tuple[str, str], SubtitleFile],
        run: _Run,
    ) -> tuple[list[str], bool]:
        """Search and save `languages` for one video. Returns the languages saved and whether
        all of them are settled (saved or given up)."""
        show = shows.get(record.media_id) if record.media_type is MediaType.TV else None
        label = _label(record, video, show)
        path = video.video.path
        try:
            found = await self._search(record, video, languages, show, movies)
        except QuotaExceeded as exc:
            self._quota_used_up(exc.reset_at)
            raise _StopRun(quota_text(exc.reset_at), quota=True) from None
        except OpenSubtitlesError as exc:
            raise _StopRun(str(exc)) from None

        saved: list[str] = []
        settled = True
        hearing_impaired = self._settings.current.subtitles_hearing_impaired
        for language in languages:
            choice = pick(found, language, hearing_impaired)
            if choice is not None and await self._save(path, language, choice):
                await self._repository.record_saved(
                    path,
                    language,
                    record=record,
                    season=video.season,
                    episode=video.episode,
                    subtitle_path=subtitle_path(path, language),
                    when=self._clock(),
                )
                saved.append(language)
                run.saved.append(f"{label} ({language.upper()})")
                if self._quota.remaining is not None and self._quota.remaining <= 0:
                    self._emit_saved(label, saved)
                    raise _StopRun(quota_text(self._quota.reset_at), quota=True)
                continue
            state = states.get((path, language))
            now = self._clock()
            first = state.first_try if state is not None and state.first_try else now
            give_up = now - first >= GIVE_UP_AFTER
            await self._repository.record_try(
                path,
                language,
                record=record,
                season=video.season,
                episode=video.episode,
                when=now,
                give_up=give_up,
            )
            if give_up:
                log.info("Subtitles %s (%s): none found in %d days, giving up", label, language, GIVE_UP_AFTER.days)
                run.gave_up.append(f"{label} ({language.upper()})")
            else:
                log.info("Subtitles %s (%s): none found yet", label, language)
                run.not_found.append(f"{label} ({language.upper()})")
                settled = False
        self._emit_saved(label, saved)
        return saved, settled

    def _emit_saved(self, label: str, languages: list[str]) -> None:
        if languages:
            log.info("Subtitles %s: saved %s", label, ", ".join(languages))
            self._emit(SubtitlesSaved(label=label, languages=tuple(languages)))

    async def _search(
        self,
        record: DownloadRecord,
        video: GrabVideo,
        languages: list[str],
        show: Show | None,
        movies: dict[int, str | None],
    ) -> list[SubtitleCandidate]:
        """Moviehash first; then the IMDb id (or the name) for the languages still missing."""
        wanted = ",".join(sorted(language.lower() for language in languages))
        found: list[SubtitleCandidate] = []
        try:
            digest = await asyncio.to_thread(movie_hash, video.video.path)
        except OSError:
            digest = None
        if digest is not None:
            found += await self._client.search(
                {"moviehash": digest, "languages": wanted, "ai_translated": "exclude"}
            )
        missing = [language for language in languages if pick(found, language) is None]
        if not missing:
            return found
        params: dict[str, str | int] = {
            "languages": ",".join(sorted(language.lower() for language in missing)),
            "ai_translated": "exclude",
        }
        if record.media_type is MediaType.TV:
            parent = imdb_number(show.imdb_id) if show is not None else None
            if parent is not None:
                params["parent_imdb_id"] = parent
            else:
                params["query"] = show.name if show is not None else video.video.stem
            if video.season is not None and video.episode is not None:
                params["season_number"] = video.season
                params["episode_number"] = video.episode
            params["type"] = "episode"
        else:
            if record.media_id not in movies:
                movies[record.media_id] = await self._movie_imdb_id(record.media_id)
            imdb = imdb_number(movies[record.media_id])
            if imdb is not None:
                params["imdb_id"] = imdb
            else:
                identity = movie_identity(record.release_title)
                if identity is not None:
                    params["query"], params["year"] = identity[0], identity[1]
                else:
                    params["query"] = video.video.stem
            params["type"] = "movie"
        return found + await self._client.search(params)

    async def _save(self, path: str, language: str, choice: SubtitleCandidate) -> bool:
        """Download a subtitle and write it next to the video. False when OpenSubtitles gave
        nothing usable for this one (the run goes on)."""
        try:
            link = await self._client.download_link(choice.file_id)
        except QuotaExceeded as exc:
            self._quota_used_up(exc.reset_at)
            raise _StopRun(quota_text(exc.reset_at), quota=True) from None
        except OpenSubtitlesError as exc:
            if not _is_one_off(exc):
                raise _StopRun(str(exc)) from None
            log.info("Subtitles: file %d: %s", choice.file_id, exc)
            return False
        self._quota = SubtitleQuota(
            remaining=link.remaining,
            allowed=self._quota.allowed,
            reset_at=link.reset_at or self._quota.reset_at,
        )
        try:
            content = await self._client.fetch(link.link)
        except OpenSubtitlesError as exc:
            if not _is_one_off(exc):
                raise _StopRun(str(exc)) from None
            log.info("Subtitles: file %d: %s", choice.file_id, exc)
            return False
        target = subtitle_path(path, language)
        try:
            await asyncio.to_thread(write_new_file, target, content)
        except OSError:
            raise _StopRun(t(WRITE_FAILED_TEXT)) from None
        return True  # written now, or a subtitle appeared there meanwhile (kept as it is)

    def _quota_used_up(self, reset_at: datetime | None) -> None:
        self._quota = SubtitleQuota(
            remaining=0, allowed=self._quota.allowed, reset_at=reset_at or _next_midnight_utc(self._clock())
        )


def _is_one_off(exc: OpenSubtitlesError) -> bool:
    """An error about one subtitle (an odd answer), not about the key, account or connection."""
    return not isinstance(exc, (KeyRejected, LoginRejected, Unreachable))


def _next_midnight_utc(now: datetime) -> datetime:
    """OpenSubtitles' download counters start again at midnight UTC."""
    tomorrow = (now.astimezone(UTC) + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


def _label(record: DownloadRecord, video: GrabVideo, show: Show | None) -> str:
    """"Severance S02E04" for an episode; a film's title and year from its release title."""
    if record.media_type is MediaType.TV:
        name = show.name if show is not None else "Episode"
        if video.season is not None and video.episode is not None:
            return f"{name} S{video.season:02d}E{video.episode:02d}"
        return name
    identity = movie_identity(record.release_title)
    if identity is None:
        return record.release_title
    return f"{identity[0].title()} ({identity[1]})"
