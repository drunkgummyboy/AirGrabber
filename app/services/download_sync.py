"""The Downloads page in sync with the torrent program (3.0.2): the pure rules behind
DownloadStatusService (app/services/download_status.py).

Whenever the program's task list can be read (REMOTE mode, and FOLDER mode with a program set
up), the page lists every task it has, plus AirGrabber's grabs it doesn't have (yet):

- a task matched to a grab is that grab's row; a task no grab matches was added in the program
  itself (a TaskEntry), recognised when cheap (`Recognition`): a release name with SxxEyy of a
  show AirGrabber knows (anyone's followed shows, the recent grabs' shows), or with a year of a
  film on anyone's watchlist or in the history; recognised, it gets that poster and a clean
  title ("Severance S02E04"), else its own name;
- a grab the program has had once (`seen_downloads`, HistoryRepository.mark_seen) and doesn't
  have now was removed there: it leaves the page (a cleared_downloads mark), the history row
  stays ("✓ Grabbed" marks); `GoneGrabs` waits for a second check a little later before the
  mark is written, so a program that answers with a half list while it starts up costs nothing;
- a grab it has never had is waiting for it (PENDING) for PICKUP_GRACE, then a problem: it
  never picked it up (NOT_FOUND, with "Download again").

Recognition caches its answer per task name (a NAS can hold hundreds of finished tasks; the
page checks every 10 s): `Recognition.of` is a dictionary lookup after the first time.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import DownloadRecord, DownloadState, MediaType, Movie, Recognised, Show
from app.services.subtitle_files import movie_identity, normalize, split_episode

PICKUP_GRACE = timedelta(hours=1)  # a grab the program hasn't picked up: waiting this long
MIRROR_MISSES = 2  # checks in a row a seen grab must be missing before it is marked cleared
MIRROR_CONFIRM_S = 30.0  # … at least this far apart (seconds)

_YEAR_OR_COUNTRY = re.compile(r" (?:(?:19|20)\d\d|us|uk|au|nz|ca)$")


def pickup_state(downloaded_at: datetime, now: datetime) -> DownloadState:
    """A grab the program has never had: still waiting for it within PICKUP_GRACE, else
    never picked up."""
    return DownloadState.PENDING if now - downloaded_at < PICKUP_GRACE else DownloadState.NOT_FOUND


def title_key(text: str) -> str:
    """A show's or film's name as release names spell it: accents folded ("Shōgun" →
    "shogun"), then subtitle_files' `normalize`."""
    folded = unicodedata.normalize("NFKD", text)
    return normalize("".join(char for char in folded if not unicodedata.combining(char)))


def _shorter(key: str) -> str:
    """A key without a year or country at its end ("doctor who 2005" → "doctor who")."""
    return _YEAR_OR_COUNTRY.sub("", key)


@dataclass(frozen=True, slots=True)
class KnownFilm:
    movie_id: int
    name: str  # "Dune: Part Two"
    year: str
    poster_url: str | None = None


@dataclass(slots=True)
class KnownTitles:
    """The shows and films a task's name is looked up in, by `title_key`."""

    shows: dict[str, Show] = field(default_factory=dict)
    shows_short: dict[str, Show] = field(default_factory=dict)  # year / country left out
    films: dict[tuple[str, str], KnownFilm] = field(default_factory=dict)

    @classmethod
    def build(
        cls, shows: Iterable[Show], films: Iterable[Movie], grabs: Iterable[DownloadRecord]
    ) -> KnownTitles:
        """`shows` first come first (followed shows before the grabs' shows); `films`: the
        watchlist films (with their posters), then the films in the history (`grabs`)."""
        known = cls()
        for show in shows:
            key = title_key(show.name)
            if key:
                known.shows.setdefault(key, show)
                known.shows_short.setdefault(_shorter(key), show)
        for movie in films:
            year = str(movie.year) if movie.year else ""
            if not year:
                continue
            film = KnownFilm(movie.id, movie.title, year, movie.poster_url("w185"))
            for title in (movie.title, movie.original_title or ""):
                if key := title_key(title):
                    known.films.setdefault((key, year), film)
        for grab in grabs:
            if grab.media_type is not MediaType.MOVIE:
                continue
            identity = movie_identity(grab.release_title)
            if identity is None:
                continue
            title, year = identity
            words = _film_name(grab.release_title, year)
            known.films.setdefault((title, year), KnownFilm(grab.media_id, words, year))
        return known

    def show(self, words: str) -> Show | None:
        """The show whose name `words` (a release name's words before SxxEyy) spells."""
        for table in (self.shows, self.shows_short):
            for key in (words, _shorter(words)):
                if key and (found := table.get(key)) is not None:
                    return found
        return None

    def film(self, title: str, year: str) -> KnownFilm | None:
        """A known film of this (normalised) title and year (a year either side too: release
        years drift)."""
        found = self.films.get((title, year))
        if found is not None or not year.isdigit():
            return found
        for other in (int(year) - 1, int(year) + 1):
            if (found := self.films.get((title, str(other)))) is not None:
                return found
        return None


def _film_name(release_title: str, year: str) -> str:
    """"Gladiator II" from "Gladiator.II.2024.1080p" (the words before the year, as written)."""
    text = re.sub(r"[._]+", " ", release_title)
    match = re.search(rf"(?<!\d){year}(?!\d)", text)
    words = text[: match.start()] if match else text
    return " ".join(words.strip(" -([").split()) or release_title


def recognise(name: str, known: KnownTitles) -> tuple[str, Recognised] | None:
    """(clean title, what it is) of a task named `name`: an episode of a known show
    ("Severance S02E04"), or a known film ("Dune: Part Two (2024)"); None when neither."""
    episode = split_episode(name)
    if episode is not None:
        words, season, number = episode
        show = known.show(words)
        if show is None:
            return None
        title = f"{show.name} S{season:02d}E{number:02d}"
        return title, Recognised(name=show.name, show=show)
    identity = movie_identity(name)
    if identity is None:
        return None
    film = known.film(*identity)
    if film is None:
        return None
    return (
        f"{film.name} ({film.year})",
        Recognised(name=film.name, movie_id=film.movie_id, poster_url=film.poster_url),
    )


class Recognition:
    """`recognise` with its answers kept per task name, until the known titles change."""

    def __init__(self) -> None:
        self._known: KnownTitles = KnownTitles()
        self._answers: dict[str, tuple[str, Recognised] | None] = {}

    def update(self, known: KnownTitles) -> None:
        self._known = known
        self._answers = {}

    def of(self, name: str) -> tuple[str, Recognised] | None:
        if name not in self._answers:
            self._answers[name] = recognise(name, self._known)
        return self._answers[name]


class GoneGrabs:
    """The grabs the program had and doesn't have now: each leaves the page at once, and is
    marked cleared (`confirm`) once it was missing MIRROR_MISSES checks in a row, at least
    MIRROR_CONFIRM_S apart."""

    def __init__(self) -> None:
        self._missing: dict[int, tuple[float, int]] = {}  # history id -> (first miss, misses)

    def confirm(self, gone: Iterable[int], now: float) -> list[int]:
        """The grabs missing this check (`gone`): those to mark cleared now."""
        current = set(gone)
        for history_id in [known for known in self._missing if known not in current]:
            del self._missing[history_id]  # back in the program, or no longer asked about
        ready: list[int] = []
        for history_id in sorted(current):
            first, misses = self._missing.get(history_id, (now, 0))
            misses += 1
            if misses >= MIRROR_MISSES and now - first >= MIRROR_CONFIRM_S:
                ready.append(history_id)
                self._missing.pop(history_id, None)
            else:
                self._missing[history_id] = (first, misses)
        return ready

    @property
    def waiting(self) -> Mapping[int, tuple[float, int]]:
        return self._missing
