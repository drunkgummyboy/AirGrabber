"""SQLite persistence via aiosqlite.

Owns the schema (versioned with PRAGMA user_version) and exposes repositories that accept and
return domain models only. All SQL in the application lives in this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ValidationError

from app.models import (
    FIRST_PROFILE_ID,
    CalendarEntry,
    DownloadRecord,
    Episode,
    FollowRecord,
    LibraryEntry,
    LibraryStats,
    MediaType,
    Movie,
    Profile,
    ProfileColour,
    Settings,
    Show,
    WatchlistEntry,
)
from app.models.plex import PlexCounts, PlexFilms, PlexLibrary, PlexScan, PlexTitle
from app.models.backup import (
    BackupData,
    BackupFilm,
    BackupGrab,
    BackupProfile,
    BackupShow,
    RestoreCounts,
)

log = logging.getLogger(__name__)

# v6: the old subtitles' bookkeeping, no longer read or written (subtitles moved to MediaForge,
# app/forge/core/subtitles.py); the tables stay in the file: no drop, no migration.
SUBTITLES_MIGRATION = """
    CREATE TABLE subtitle_files (
        video_path    TEXT    NOT NULL,
        language      TEXT    NOT NULL,                     -- OpenSubtitles code: 'en', 'pt-BR'
        history_id    INTEGER REFERENCES download_history (id) ON DELETE SET NULL,
        media_type    TEXT    NOT NULL CHECK (media_type IN ('tv', 'movie')),
        media_id      INTEGER NOT NULL,                     -- TVMaze show id / TMDB movie id
        season        INTEGER,
        episode       INTEGER,
        tries         INTEGER NOT NULL DEFAULT 0,
        first_try     TEXT,                                 -- UTC ISO-8601
        last_try      TEXT,
        gave_up       INTEGER NOT NULL DEFAULT 0 CHECK (gave_up IN (0, 1)),
        saved_at      TEXT,                                 -- NULL until a subtitle is there
        subtitle_path TEXT,
        PRIMARY KEY (video_path, language)
    ) STRICT;
    CREATE INDEX ix_subtitle_files_media ON subtitle_files (media_type, media_id, season, episode);

    CREATE TABLE subtitle_grabs (
        history_id INTEGER PRIMARY KEY REFERENCES download_history (id) ON DELETE CASCADE,
        languages  TEXT    NOT NULL,                        -- JSON array the grab is complete for
        done_at    TEXT    NOT NULL                         -- UTC ISO-8601
    ) STRICT;
"""

# v7: the Downloads page's "Clear finished" and "Remove from list": a grab taken off the page. Only
# the page reads it: the grab stays in the history (downloaded marks, "Download
# missing") and its files are never touched. A mark goes with its grab. Self-contained.
CLEARED_MIGRATION = """
    CREATE TABLE cleared_downloads (
        history_id INTEGER PRIMARY KEY REFERENCES download_history (id) ON DELETE CASCADE,
        cleared_at TEXT    NOT NULL                         -- UTC ISO-8601
    ) STRICT;
"""

# v8: people (app/services/profiles.py). Each has their own followed series and film watchlist;
# the rest stays shared. The first person (no name: shown as "Me") gets every row there was.
# tracked_shows and watchlist are rebuilt with the person in their primary key (SQLite can't
# change a key in place); removing a person removes their rows. Per show, `synced_at` stays the
# same in every person's row (sync stamps them all); per film, the release dates and the
# automatic tries likewise (app.database.WatchlistRepository): they are the show's and the film's,
# not a person's. Nothing references the two tables, so dropping the old ones is safe with the
# foreign keys on.
PROFILES_MIGRATION = """
    CREATE TABLE profiles (
        id         INTEGER PRIMARY KEY,
        name       TEXT,                                    -- NULL: never named ("Me")
        colour     TEXT    NOT NULL,                        -- app.models.ProfileColour
        created_at TEXT    NOT NULL                         -- UTC ISO-8601
    ) STRICT;
    INSERT INTO profiles (id, name, colour, created_at)
    VALUES (1, NULL, 'blue', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

    CREATE TABLE tracked_shows_v8 (
        profile_id    INTEGER NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
        show_id       INTEGER NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
        tracked_at    TEXT    NOT NULL,
        auto_download INTEGER NOT NULL DEFAULT 0 CHECK (auto_download IN (0, 1)),
        synced_at     TEXT,                                 -- NULL = never synced from TVMaze
        auto_since    TEXT,                                 -- UTC ISO-8601; NULL while off
        PRIMARY KEY (profile_id, show_id)
    ) STRICT;
    INSERT INTO tracked_shows_v8
        (profile_id, show_id, tracked_at, auto_download, synced_at, auto_since)
    SELECT 1, show_id, tracked_at, auto_download, synced_at, auto_since FROM tracked_shows;
    DROP TABLE tracked_shows;
    ALTER TABLE tracked_shows_v8 RENAME TO tracked_shows;
    CREATE INDEX ix_tracked_shows_show ON tracked_shows (show_id);

    CREATE TABLE watchlist_v8 (
        profile_id         INTEGER NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
        movie_id           INTEGER NOT NULL,                -- TMDB movie id
        title              TEXT    NOT NULL,
        movie              TEXT    NOT NULL,                -- JSON of the Movie model
        added_at           TEXT    NOT NULL,                -- UTC ISO-8601
        auto_download      INTEGER NOT NULL DEFAULT 1 CHECK (auto_download IN (0, 1)),
        imdb_id            TEXT,
        digital_release    TEXT,                            -- YYYY-MM-DD, TMDB client's region
        theatrical_release TEXT,                            -- YYYY-MM-DD
        details_at         TEXT,                            -- UTC; NULL = never read from TMDB
        tries              INTEGER NOT NULL DEFAULT 0,      -- automatic searches without a grab
        first_try          TEXT,                            -- UTC
        last_try           TEXT,                            -- UTC
        gave_up            INTEGER NOT NULL DEFAULT 0 CHECK (gave_up IN (0, 1)),
        PRIMARY KEY (profile_id, movie_id)
    ) STRICT;
    INSERT INTO watchlist_v8
        (profile_id, movie_id, title, movie, added_at, auto_download, imdb_id, digital_release,
         theatrical_release, details_at, tries, first_try, last_try, gave_up)
    SELECT 1, movie_id, title, movie, added_at, auto_download, imdb_id, digital_release,
           theatrical_release, details_at, tries, first_try, last_try, gave_up
    FROM watchlist;
    DROP TABLE watchlist;
    ALTER TABLE watchlist_v8 RENAME TO watchlist;
    CREATE INDEX ix_watchlist_movie ON watchlist (movie_id);
"""

# v9: the Downloads page in sync with the torrent program (app/services/download_status.py): a
# grab its program has had at least once. Never had and less than an hour old: waiting for the
# program to pick it up; never had and older: it never did. Had once and gone now: removed in
# the program, so it leaves the page (a cleared_downloads mark). The grabs older than an hour at
# the upgrade count as had: those the program no longer has leave the page at its first check
# instead of turning into problems. A mark goes with its grab. Self-contained.
SEEN_MIGRATION = """
    CREATE TABLE seen_downloads (
        history_id INTEGER PRIMARY KEY REFERENCES download_history (id) ON DELETE CASCADE,
        seen_at    TEXT    NOT NULL                         -- UTC ISO-8601
    ) STRICT;
    INSERT INTO seen_downloads (history_id, seen_at)
    SELECT id, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') FROM download_history
    WHERE julianday(downloaded_at) <= julianday('now', '-1 hour');
"""

# v10: Plex (app/services/plex.py): what is in the user's Plex libraries counts as had (since
# 3.1.0 it is all that does: `have_episodes`, the "I already have this" marks of v4 / v5, is kept
# as it is but no longer read). Each check replaces the three tables' contents in one transaction
# (PlexRepository.replace); Disconnect empties them. Episodes by TVMaze show id, season and number
# (they outlive a refresh of the show's episodes; a show that leaves the cache takes its rows
# along), films by TMDB id and / or IMDb id (Plex's old IMDb agent knows only the second). Plex's
# shows match TVMaze's by TheTVDB id, else IMDb id: the shows cache keeps TheTVDB's id from now
# on, and the shows anyone follows are read from TVMaze once more at the next sync (their
# `synced_at` cleared) to learn it.
PLEX_MIGRATION = """
    ALTER TABLE shows ADD COLUMN tvdb_id INTEGER;          -- TheTVDB id (TVMaze's externals)
    UPDATE tracked_shows SET synced_at = NULL;

    CREATE TABLE plex_episodes (
        show_id INTEGER NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
        season  INTEGER NOT NULL,
        episode INTEGER NOT NULL,
        PRIMARY KEY (show_id, season, episode)
    ) STRICT;

    CREATE TABLE plex_movies (
        tmdb_id INTEGER,                                    -- TMDB movie id
        imdb_id TEXT,                                       -- "tt0111161"
        CHECK (tmdb_id IS NOT NULL OR imdb_id IS NOT NULL)
    ) STRICT;
    CREATE INDEX ix_plex_movies_tmdb ON plex_movies (tmdb_id);
    CREATE INDEX ix_plex_movies_imdb ON plex_movies (imdb_id);

    CREATE TABLE plex_state (                               -- one row once a check succeeded
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        checked_at      TEXT    NOT NULL,                   -- UTC ISO-8601
        shows           INTEGER NOT NULL,                   -- Plex shows read
        unmatched_shows INTEGER NOT NULL,                   -- ... AirGrabber doesn't know
        unmatched_films INTEGER NOT NULL,                   -- films without a TMDB or IMDb id
        libraries       TEXT    NOT NULL DEFAULT '[]'       -- JSON: the show and film libraries
    ) STRICT;
"""

# The Library page's titles: every show and film the last Plex check read (replaced per check, like
# plex_episodes). A version before it never reads the table.
LIBRARY_MIGRATION = """
    CREATE TABLE plex_titles (
        kind       TEXT    NOT NULL,                        -- 'show' or 'movie'
        rating_key TEXT    NOT NULL,                        -- Plex's own key
        title      TEXT    NOT NULL,
        year       INTEGER,
        thumb      TEXT,                                    -- the server's picture path
        added_at   TEXT,                                    -- UTC ISO-8601
        seasons    INTEGER,
        episodes   INTEGER,
        resolution TEXT,                                    -- "4K", "1080p", "720p", "SD"
        show_id    INTEGER,                                 -- TVMaze id it was matched to
        tvdb_id    INTEGER,
        imdb_id    TEXT,
        tmdb_id    INTEGER,
        PRIMARY KEY (kind, rating_key)
    ) STRICT;
"""

# Entry i upgrades the schema from version i to i + 1. Append only — never edit a shipped entry.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE shows (
        id                  INTEGER PRIMARY KEY,            -- TVMaze show id
        name                TEXT    NOT NULL,
        status              TEXT,
        type                TEXT,
        language            TEXT,
        genres              TEXT    NOT NULL DEFAULT '[]',  -- JSON array
        premiered           TEXT,                           -- YYYY-MM-DD
        ended               TEXT,
        network             TEXT,
        runtime             INTEGER,
        rating              REAL,
        weight              INTEGER NOT NULL DEFAULT 0,
        imdb_id             TEXT,
        poster_url          TEXT,
        poster_url_original TEXT,
        summary             TEXT    NOT NULL DEFAULT '',
        updated             INTEGER                         -- TVMaze change stamp
    ) STRICT;

    CREATE TABLE episodes (
        id         INTEGER PRIMARY KEY,                     -- TVMaze episode id
        show_id    INTEGER NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
        season     INTEGER NOT NULL,
        number     INTEGER,                                 -- NULL for specials
        name       TEXT    NOT NULL DEFAULT '',
        type       TEXT    NOT NULL DEFAULT 'regular',
        air_date   TEXT,                                    -- network-local YYYY-MM-DD
        air_time   TEXT,                                    -- network-local HH:MM:SS
        airstamp   TEXT,                                    -- UTC instant, ISO-8601
        runtime    INTEGER,
        rating     REAL,
        image_url  TEXT,
        summary    TEXT    NOT NULL DEFAULT ''
    ) STRICT;
    CREATE INDEX ix_episodes_air_date ON episodes (air_date);
    CREATE INDEX ix_episodes_show ON episodes (show_id, season, number);

    CREATE TABLE tracked_shows (
        show_id       INTEGER PRIMARY KEY REFERENCES shows (id) ON DELETE CASCADE,
        tracked_at    TEXT    NOT NULL,
        auto_download INTEGER NOT NULL DEFAULT 0 CHECK (auto_download IN (0, 1)),
        synced_at     TEXT                                  -- NULL = never synced from TVMaze
    ) STRICT;

    CREATE TABLE download_history (
        id            INTEGER PRIMARY KEY,
        media_type    TEXT    NOT NULL CHECK (media_type IN ('tv', 'movie')),
        media_id      INTEGER NOT NULL,                     -- TVMaze show id / TMDB movie id
        season        INTEGER,
        episode       INTEGER,                              -- NULL with a season = season pack
        release_title TEXT    NOT NULL,
        info_hash     TEXT    UNIQUE,
        source        TEXT,
        size_bytes    INTEGER,
        saved_path    TEXT,
        downloaded_at TEXT    NOT NULL,
        CHECK (media_type = 'movie' OR season IS NOT NULL)
    ) STRICT;
    CREATE INDEX ix_history_media ON download_history (media_type, media_id, season, episode);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                                 -- JSON-encoded
    ) STRICT;

    CREATE TABLE schedule_fetches (                         -- days pulled from TVMaze /schedule
        air_date   TEXT PRIMARY KEY,
        fetched_at TEXT NOT NULL
    ) STRICT;
    """,
    # v2: automatic downloads of new episodes. `auto_download` already exists (v1, filled by the
    # 1.x import); `auto_since` says from when it applies. Every switch starts off after this
    # upgrade (the user turns the wanted shows on again in My shows), so the 1.x values are reset
    # and `auto_since` stays NULL.
    """
    ALTER TABLE tracked_shows ADD COLUMN auto_since TEXT;   -- UTC ISO-8601; NULL while switched off
    UPDATE tracked_shows SET auto_download = 0;

    CREATE TABLE auto_attempts (                            -- the automatic downloads' tries
        episode_id INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
        tries      INTEGER NOT NULL,                        -- searches that found nothing / failed
        last_try   TEXT    NOT NULL,                        -- UTC ISO-8601
        gave_up    INTEGER NOT NULL DEFAULT 0 CHECK (gave_up IN (0, 1))
    ) STRICT;
    """,
    # v3: the movie watchlist. A film is kept as the JSON of the TMDB list entry it was saved
    # with (the `Movie` model), so its card draws without TMDB; the release dates come from
    # TMDB's details and are refreshed now and then. A film leaves the list once it is
    # downloaded (HistoryRepository.add).
    """
    CREATE TABLE watchlist (
        movie_id           INTEGER PRIMARY KEY,             -- TMDB movie id
        title              TEXT    NOT NULL,
        movie              TEXT    NOT NULL,                -- JSON of the Movie model
        added_at           TEXT    NOT NULL,                -- UTC ISO-8601
        auto_download      INTEGER NOT NULL DEFAULT 1 CHECK (auto_download IN (0, 1)),
        imdb_id            TEXT,
        digital_release    TEXT,                            -- YYYY-MM-DD, TMDB client's region
        theatrical_release TEXT,                            -- YYYY-MM-DD
        details_at         TEXT,                            -- UTC; NULL = never read from TMDB
        tries              INTEGER NOT NULL DEFAULT 0,      -- automatic searches without a grab
        first_try          TEXT,                            -- UTC
        last_try           TEXT,                            -- UTC
        gave_up            INTEGER NOT NULL DEFAULT 0 CHECK (gave_up IN (0, 1))
    ) STRICT;
    """,
    # v4: episodes the user marked "I already have this": no longer counted as missing (the
    # latest season's aired episodes that aren't downloaded; app.services.missing). A mark
    # goes with its episode. Not read since 3.1.0 (only Plex counts as had); kept, and still
    # carried by backups.
    """
    CREATE TABLE have_episodes (
        episode_id INTEGER PRIMARY KEY REFERENCES episodes (id) ON DELETE CASCADE,
        marked_at  TEXT    NOT NULL                         -- UTC ISO-8601
    ) STRICT;
    """,
    # v5: AirGrabber never saw what the user got before (or elsewhere), so the episodes of the
    # tracked shows that aired before this upgrade count as had: only later ones can become
    # missing. Newly tracked shows got the same start until 3.1.0, which reads only Plex as
    # had and leaves these marks as they are, unread (app/services/plex.py).
    """
    INSERT INTO have_episodes (episode_id, marked_at)
    SELECT e.id, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    FROM episodes AS e
    JOIN tracked_shows AS t ON t.show_id = e.show_id
    WHERE e.season > 0 AND e.number IS NOT NULL AND e.type = 'regular'
      AND (
          (e.airstamp IS NOT NULL AND julianday(e.airstamp) <= julianday('now'))
          OR (e.airstamp IS NULL AND e.air_date <= date('now', 'localtime'))
      )
      AND NOT EXISTS (
          SELECT 1 FROM download_history AS h
          WHERE h.media_type = 'tv' AND h.media_id = e.show_id AND h.season = e.season
            AND (h.episode IS NULL OR h.episode = e.number)
      )
    ON CONFLICT (episode_id) DO NOTHING;
    """,
    # v6: subtitles; see SUBTITLES_MIGRATION above.
    SUBTITLES_MIGRATION,
    # v7: grabs taken off the Downloads page; see CLEARED_MIGRATION above.
    CLEARED_MIGRATION,
    # v8: people, each with their own followed series and watchlist; see PROFILES_MIGRATION.
    PROFILES_MIGRATION,
    # v9: grabs the torrent program has had (the Downloads page in sync); see SEEN_MIGRATION.
    SEEN_MIGRATION,
    # v10: what is in Plex; see PLEX_MIGRATION.
    PLEX_MIGRATION,
    # v11: Plex's titles for the Library page; see LIBRARY_MIGRATION.
    LIBRARY_MIGRATION,
)

SHOW_COLUMNS = tuple(Show.model_fields)
EPISODE_COLUMNS = tuple(Episode.model_fields)
HISTORY_COLUMNS = tuple(DownloadRecord.model_fields)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc(instant: datetime) -> str:
    """An aware datetime as stored: UTC ISO-8601."""
    return instant.astimezone(UTC).isoformat()


def _upsert_sql(table: str, columns: Sequence[str], key: str = "id") -> str:
    names = ", ".join(columns)
    values = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != key)
    return f"INSERT INTO {table} ({names}) VALUES ({values}) ON CONFLICT ({key}) DO UPDATE SET {updates}"


def _select(alias: str, columns: Sequence[str], prefix: str) -> str:
    """`s.id AS show__id, s.name AS show__name, ...` — disambiguates columns across a JOIN."""
    return ", ".join(f"{alias}.{column} AS {prefix}{column}" for column in columns)


def _section(row: sqlite3.Row, prefix: str) -> dict[str, Any]:
    return {key.removeprefix(prefix): row[key] for key in row.keys() if key.startswith(prefix)}


def _show_to_row(show: Show) -> dict[str, Any]:
    row = show.model_dump(mode="json")
    row["genres"] = json.dumps(row["genres"])
    return row


def _show_from_row(row: Mapping[str, Any]) -> Show:
    return Show.model_validate({**row, "genres": json.loads(row["genres"])})


def _episode_to_row(episode: Episode) -> dict[str, Any]:
    return episode.model_dump(mode="json")


_SHOW_UPSERT = _upsert_sql("shows", SHOW_COLUMNS)
_EPISODE_UPSERT = _upsert_sql("episodes", EPISODE_COLUMNS)
_SHOW_FIELDS = _select("s", SHOW_COLUMNS, "show__")
_EPISODE_FIELDS = _select("e", EPISODE_COLUMNS, "ep__")

# The season pack grab `h` can hold the episode `e`: `e` had aired when it was grabbed (what came
# out later isn't in it). An episode without a date hadn't. The SQL twin of
# app.services.library.downloaded_ids; tests keep them in step.
_PACK_HOLDS = """
    (
        (e.airstamp IS NOT NULL AND julianday(e.airstamp) <= julianday(h.downloaded_at))
        OR (e.airstamp IS NULL AND e.air_date <= substr(h.downloaded_at, 1, 10))
    )"""

_IS_DOWNLOADED = f"""
    EXISTS (
        SELECT 1 FROM download_history AS h
        WHERE h.media_type = 'tv' AND h.media_id = e.show_id AND h.season = e.season
          AND (h.episode = e.number OR (h.episode IS NULL AND {_PACK_HOLDS}))
    )"""

# The episode `e` is in Plex (app/services/plex.py): the only thing that counts as had.
_IN_PLEX = """
    EXISTS (
        SELECT 1 FROM plex_episodes AS p
        WHERE p.show_id = e.show_id AND p.season = e.season AND p.episode = e.number
    )"""

_CALENDAR_SQL = f"""
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, {_IS_DOWNLOADED} AS is_downloaded
    FROM episodes AS e
    JOIN tracked_shows AS t ON t.show_id = e.show_id AND t.profile_id = :profile
    JOIN shows AS s ON s.id = e.show_id
    WHERE e.air_date BETWEEN :start AND :end
    ORDER BY e.air_date, e.airstamp, s.name COLLATE NOCASE, e.season, e.number
"""

# Popular shows the person (:profile) doesn't follow airing in the range: one episode per show per
# day, best `per_day` per day.
# Shows of :types rank first; shows of :fallback_types only after them, so they fill the days with
# few of the first kind (US networks air hardly any new scripted episodes on a Saturday).
_SUGGESTIONS_SQL = f"""
    WITH candidates AS (
        SELECT e.id AS episode_id, e.show_id, e.air_date, s.weight,
               s.type NOT IN (SELECT value FROM json_each(:types)) AS is_fallback,
               ROW_NUMBER() OVER (
                   PARTITION BY e.air_date, e.show_id ORDER BY e.season, e.number
               ) AS nth_of_show
        FROM episodes AS e
        JOIN shows AS s ON s.id = e.show_id
        WHERE e.air_date BETWEEN :start AND :end
          AND e.show_id NOT IN (SELECT show_id FROM tracked_shows WHERE profile_id = :profile)
          AND s.language = :language
          AND s.weight >= :min_weight
          AND (
              s.type IN (SELECT value FROM json_each(:types))
              OR s.type IN (SELECT value FROM json_each(:fallback_types))
          )
    ),
    ranked AS (
        SELECT episode_id, air_date,
               ROW_NUMBER() OVER (
                   PARTITION BY air_date ORDER BY is_fallback, weight DESC, show_id
               ) AS day_rank
        FROM candidates
        WHERE nth_of_show = 1
    )
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, 0 AS is_downloaded
    FROM ranked AS r
    JOIN episodes AS e ON e.id = r.episode_id
    JOIN shows AS s ON s.id = e.show_id
    WHERE r.day_rank <= :per_day
    ORDER BY r.air_date, r.day_rank
"""

# Episodes the automatic downloads look after: numbered, non-special episodes with an airstamp,
# of a show anyone follows with the switch on, aired after the earliest moment one of them
# switched it on, never grabbed (an episode or its season pack), not in Plex (empty without Plex)
# and not given up: everyone's lists at once, each episode once (its tries are the episode's,
# not a person's). Instants are compared through julianday(), which reads both the 'Z' and the
# '+00:00' spelling of UTC.
_AUTO_ELIGIBLE = f"""
    FROM episodes AS e
    JOIN (
        SELECT show_id, MIN(julianday(auto_since)) AS since FROM tracked_shows
        WHERE auto_download = 1 AND auto_since IS NOT NULL
        GROUP BY show_id
    ) AS t ON t.show_id = e.show_id
    JOIN shows AS s ON s.id = e.show_id
    LEFT JOIN auto_attempts AS a ON a.episode_id = e.id
    WHERE e.season > 0 AND e.number IS NOT NULL AND e.airstamp IS NOT NULL
      AND julianday(e.airstamp) > t.since
      AND COALESCE(a.gave_up, 0) = 0
      AND NOT {_IS_DOWNLOADED}
      AND NOT {_IN_PLEX}
"""
_AUTO_ORDER = "ORDER BY julianday(e.airstamp), s.name COLLATE NOCASE, e.season, e.number"

# Untried and least-tried episodes first (then the one tried longest ago), so episodes that keep
# coming back with nothing found can't crowd newer ones out of a run.
_AUTO_DUE_SQL = f"""
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, 0 AS is_downloaded
    {_AUTO_ELIGIBLE}
      AND julianday(e.airstamp) BETWEEN julianday(:aired_from) AND julianday(:aired_until)
    ORDER BY COALESCE(a.tries, 0), COALESCE(a.last_try, ''),
             julianday(e.airstamp), s.name COLLATE NOCASE, e.season, e.number
    LIMIT :limit
"""

_AUTO_OVERDUE_SQL = f"""
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, 0 AS is_downloaded
    {_AUTO_ELIGIBLE}
      AND julianday(e.airstamp) < julianday(:aired_before)
    {_AUTO_ORDER}
"""


# Per show the person (:profile) follows, its first numbered episode still to air (the Tracked
# cards' "Next: …").
_NEXT_EPISODES_SQL = f"""
    SELECT {_EPISODE_FIELDS} FROM (
        SELECT e.id,
               ROW_NUMBER() OVER (
                   PARTITION BY e.show_id
                   ORDER BY e.air_date, julianday(e.airstamp), e.season, e.number
               ) AS nth
        FROM episodes AS e
        JOIN tracked_shows AS t ON t.show_id = e.show_id AND t.profile_id = :profile
        WHERE e.season > 0 AND e.number IS NOT NULL
          AND (
              (e.airstamp IS NOT NULL AND julianday(e.airstamp) > julianday(:now))
              OR (e.airstamp IS NULL AND e.air_date >= :today)
          )
    ) AS upcoming
    JOIN episodes AS e ON e.id = upcoming.id
    WHERE upcoming.nth = 1
"""


# The missing episodes of the shows one person (:profile) follows, as the CTE `missing` (id,
# show_id, air_date): the regular
# numbered episodes (seasons from 1) of each show's latest season — the highest with such an
# episode aired (an airstamp up to :now, or without one an air date up to :today) — that aren't
# downloaded (the episode or its season pack) nor in Plex (together the CTE `pending`) and aired
# long enough ago (an airstamp up to :missing_by, or without one an air date up to :missing_day;
# app.services.missing.missing_cutoffs). `pending` minus `missing` are the "new" episodes. The
# SQL twin of app.services.missing.missing_episodes / new_episodes; tests keep them in step.
# Only run while Plex is connected (LibraryService): without Plex nothing is missing.
_MISSING_CTE = f"""
    WITH aired AS (
        SELECT e.id, e.show_id, e.season, e.number, e.air_date, e.airstamp
        FROM episodes AS e
        JOIN tracked_shows AS t ON t.show_id = e.show_id AND t.profile_id = :profile
        WHERE e.season > 0 AND e.number IS NOT NULL AND e.type = 'regular'
          AND (
              (e.airstamp IS NOT NULL AND julianday(e.airstamp) <= julianday(:now))
              OR (e.airstamp IS NULL AND e.air_date <= :today)
          )
    ),
    latest AS (
        SELECT show_id, MAX(season) AS season FROM aired GROUP BY show_id
    ),
    pending AS (
        SELECT e.id, e.show_id, e.air_date, e.airstamp
        FROM aired AS e
        JOIN latest AS l ON l.show_id = e.show_id AND l.season = e.season
        WHERE NOT {_IN_PLEX}
          AND NOT {_IS_DOWNLOADED}
    ),
    missing AS (
        SELECT id, show_id, air_date
        FROM pending
        WHERE (airstamp IS NOT NULL AND julianday(airstamp) <= julianday(:missing_by))
           OR (airstamp IS NULL AND air_date <= :missing_day)
    )
"""
# Per tracked show, how many episodes are missing (My shows' badges).
_MISSING_COUNTS_SQL = f"""{_MISSING_CTE}
    SELECT show_id, COUNT(*) AS missing FROM missing GROUP BY show_id
"""
# The gaps (My shows ⋯ → Find gaps in Plex…): aired episodes of the shows a person follows
# that Plex doesn't have, in the seasons from the first to the last one Plex has some of.
_GAPS_SQL = f"""
    WITH have AS (
        SELECT show_id, MIN(season) AS first, MAX(season) AS last
        FROM plex_episodes GROUP BY show_id
    )
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, {_IS_DOWNLOADED} AS is_downloaded
    FROM episodes AS e
    JOIN tracked_shows AS t ON t.show_id = e.show_id AND t.profile_id = :profile
    JOIN have AS h ON h.show_id = e.show_id
    JOIN shows AS s ON s.id = e.show_id
    WHERE e.season > 0 AND e.number IS NOT NULL AND e.type = 'regular'
      AND e.season BETWEEN h.first AND h.last
      AND (
          (e.airstamp IS NOT NULL AND julianday(e.airstamp) <= julianday(:now))
          OR (e.airstamp IS NULL AND e.air_date <= :today)
      )
      AND NOT {_IN_PLEX}
    ORDER BY s.name COLLATE NOCASE, s.id, e.season, e.number
"""
# The missing episodes airing between :start and :end (the calendar's amber marks).
_MISSING_BETWEEN_SQL = f"""{_MISSING_CTE}
    SELECT id FROM missing WHERE air_date BETWEEN :start AND :end
"""
# The new episodes (aired, not yet missing) airing between :start and :end (the calendar's blue
# "New" notes).
_NEW_BETWEEN_SQL = f"""{_MISSING_CTE}
    SELECT id FROM pending
    WHERE air_date BETWEEN :start AND :end AND id NOT IN (SELECT id FROM missing)
"""


def _missing_params(
    profile_id: int, now: datetime, today: date, missing_by: datetime, missing_day: date
) -> dict[str, str | int]:
    return {
        "profile": profile_id,
        "now": _utc(now),
        "today": today.isoformat(),
        "missing_by": _utc(missing_by),
        "missing_day": missing_day.isoformat(),
    }


_TRACKED_SQL = f"""
    SELECT {_SHOW_FIELDS}, t.tracked_at, t.auto_download, t.synced_at
    FROM tracked_shows AS t JOIN shows AS s ON s.id = t.show_id
    WHERE t.profile_id = :profile
"""

# Every show anyone follows, once (the sync): its earliest follow, whether anyone has its switch on,
# and when it was synced (NULL while any person's row was never synced; sync stamps them all).
_FOLLOWED_SQL = f"""
    SELECT {_SHOW_FIELDS}, MIN(t.tracked_at) AS tracked_at, MAX(t.auto_download) AS auto_download,
           CASE WHEN COUNT(t.synced_at) < COUNT(*) THEN NULL ELSE MIN(t.synced_at) END AS synced_at
    FROM tracked_shows AS t JOIN shows AS s ON s.id = t.show_id
    GROUP BY t.show_id
    ORDER BY s.name COLLATE NOCASE
"""


def _library_entry(row: sqlite3.Row) -> LibraryEntry:
    return LibraryEntry(
        show=_show_from_row(_section(row, "show__")),
        tracked_at=row["tracked_at"],
        auto_download=row["auto_download"],
        synced_at=row["synced_at"],
    )


def _calendar_entry(row: sqlite3.Row, *, is_tracked: bool) -> CalendarEntry:
    return CalendarEntry(
        show=_show_from_row(_section(row, "show__")),
        episode=Episode.model_validate(_section(row, "ep__")),
        is_tracked=is_tracked,
        is_downloaded=bool(row["is_downloaded"]),
    )


class Database:
    """One aiosqlite connection shared by the whole app.

    Every statement goes through an asyncio lock, so a coroutine can never observe (or write
    into) another coroutine's half-finished transaction on the shared connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self.profiles = ProfileRepository(self)
        self.library = LibraryRepository(self)
        self.history = HistoryRepository(self)
        self.settings = SettingsRepository(self)
        self.watchlist = WatchlistRepository(self)
        self.plex = PlexRepository(self)

    @classmethod
    async def open(cls, path: Path | str) -> Database:
        # Autocommit mode: transactions are opened explicitly by `transaction()`.
        conn = await aiosqlite.connect(path, isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            for pragma in ("foreign_keys = ON", "journal_mode = WAL", "synchronous = NORMAL"):
                await conn.execute(f"PRAGMA {pragma}")
            db = cls(conn)
            await db._migrate()
            await db._check_columns()
        except BaseException:
            await conn.close()
            raise
        return db

    async def close(self) -> None:
        async with self._lock:
            await self._conn.close()

    async def fetch_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[sqlite3.Row]:
        async with self._lock:
            return list(await self._conn.execute_fetchall(sql, params))

    async def fetch_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Row | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Atomic unit of work. Use the yielded connection — not `fetch_*` — inside the block."""
        async with self._lock:
            try:
                # Inside the try: a task cancelled here leaves BEGIN on its way to the worker
                # thread, and the ROLLBACK behind it closes what it opens.
                await self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
            except BaseException:
                with contextlib.suppress(sqlite3.OperationalError):  # BEGIN itself failed
                    await self._conn.execute("ROLLBACK")
                raise
            else:
                await self._conn.execute("COMMIT")

    async def _migrate(self) -> None:
        row = await self.fetch_one("PRAGMA user_version")
        current = row[0] if row else 0
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"Database schema v{current} is newer than this build supports (v{len(MIGRATIONS)})"
            )
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            log.info("Migrating database schema to v%d", version)
            async with self._lock:
                try:
                    await self._conn.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except BaseException:
                    with contextlib.suppress(sqlite3.OperationalError):
                        await self._conn.execute("ROLLBACK")
                    raise

    async def _check_columns(self) -> None:
        """Fail fast if a table drifts from the model it is mapped to."""
        mapped: tuple[tuple[str, type[BaseModel]], ...] = (
            ("shows", Show),
            ("episodes", Episode),
            ("download_history", DownloadRecord),
        )
        for table, model in mapped:
            rows = await self.fetch_all(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in rows}
            if columns != set(model.model_fields):
                raise RuntimeError(
                    f"Table {table!r} columns {sorted(columns)} don't match "
                    f"{model.__name__} fields {sorted(model.model_fields)}"
                )


def _profile(row: sqlite3.Row) -> Profile:
    try:
        colour = ProfileColour(row["colour"])
    except ValueError:  # a newer version's colour
        colour = ProfileColour.BLUE
    return Profile(id=row["id"], name=row["name"], colour=colour, created_at=row["created_at"])


class ProfileRepository:
    """The people who share AirGrabber (app/services/profiles.py). Removing a person removes
    their followed shows and watchlist (the foreign keys cascade); the last one is never
    removed."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def all(self) -> list[Profile]:
        """Every person, oldest first."""
        rows = await self._db.fetch_all("SELECT * FROM profiles ORDER BY id")
        return [_profile(row) for row in rows]

    async def add(self, name: str | None, colour: ProfileColour, when: datetime) -> Profile:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO profiles (name, colour, created_at) VALUES (?, ?, ?)",
                (name, colour.value, _utc(when)),
            )
            row_id = cursor.lastrowid
            rows = await conn.execute_fetchall("SELECT * FROM profiles WHERE id = ?", (row_id,))
        return _profile(list(rows)[0])

    async def update(self, profile_id: int, *, name: str | None, colour: ProfileColour) -> bool:
        """A person's name (None: unnamed, "Me") and colour; False when they are gone."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE profiles SET name = ?, colour = ? WHERE id = ?",
                (name, colour.value, profile_id),
            )
            return cursor.rowcount > 0

    async def remove(self, profile_id: int) -> bool:
        """Remove a person with their lists; never the last one. Returns whether they went."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM profiles WHERE id = ? AND (SELECT COUNT(*) FROM profiles) > 1",
                (profile_id,),
            )
            return cursor.rowcount > 0


class LibraryRepository:
    """Shows and their episodes (shared), and which of them each person follows (per person:
    every method that takes `profile_id`; the automatic downloads, the sync and the cleanup of
    the schedule cache look at everyone's)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_show(self, show: Show, episodes: Iterable[Episode] | None = None) -> None:
        """Upsert a show; when `episodes` is given it becomes the show's complete episode list."""
        episode_rows = None if episodes is None else [_episode_to_row(e) for e in episodes]
        if episode_rows and any(row["show_id"] != show.id for row in episode_rows):
            raise ValueError(f"Episodes passed to save_show must all belong to show {show.id}")
        async with self._db.transaction() as conn:
            await conn.execute(_SHOW_UPSERT, _show_to_row(show))
            if episode_rows is not None:
                # Only episodes that are gone are deleted (the rest are upserted), so rows that
                # cascade from an episode, like its auto_attempts, survive a refresh.
                await conn.execute(
                    "DELETE FROM episodes WHERE show_id = ? "
                    "AND id NOT IN (SELECT value FROM json_each(?))",
                    (show.id, json.dumps([row["id"] for row in episode_rows])),
                )
                await conn.executemany(_EPISODE_UPSERT, episode_rows)

    async def save_schedule_day(
        self, day: date, shows: Iterable[Show], episodes: Iterable[Episode], *, whole: bool = True
    ) -> None:
        """Cache one day of TVMaze's schedules (feeds the calendar's suggestions). `whole`: all of
        them came in; otherwise the day counts as not fetched, and is tried again at the next look."""
        async with self._db.transaction() as conn:
            await conn.executemany(_SHOW_UPSERT, [_show_to_row(s) for s in shows])
            await conn.executemany(_EPISODE_UPSERT, [_episode_to_row(e) for e in episodes])
            if whole:
                await conn.execute(
                    "INSERT INTO schedule_fetches (air_date, fetched_at) VALUES (?, ?) "
                    "ON CONFLICT (air_date) DO UPDATE SET fetched_at = excluded.fetched_at",
                    (day.isoformat(), _now()),
                )

    async def track(self, show_id: int, *, profile_id: int, auto_download: bool = False) -> None:
        """Add a show to a person's library. The show must already be saved (`save_show`).

        `auto_download` switches its automatic downloads on from now. A show the person
        already follows keeps its switch. A show another person follows is synced already:
        this follow starts with the same `synced_at`.
        """
        now = _now()
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO tracked_shows "
                "(profile_id, show_id, tracked_at, auto_download, auto_since, synced_at) "
                "VALUES (:profile, :show, :now, :auto, :since, "
                "        (SELECT MIN(synced_at) FROM tracked_shows WHERE show_id = :show)) "
                "ON CONFLICT (profile_id, show_id) DO NOTHING",
                {
                    "profile": profile_id,
                    "show": show_id,
                    "now": now,
                    "auto": auto_download,
                    "since": now if auto_download else None,
                },
            )

    async def set_auto_download(
        self, show_id: int, on: bool, *, profile_id: int, since: datetime | None = None
    ) -> None:
        """Switch a person's automatic downloads of a show they follow on (from `since`,
        default now) or off.

        Switching on a switch that is already on keeps the moment it went on; switching off
        forgets it. A show the person doesn't follow is ignored.
        """
        async with self._db.transaction() as conn:
            if on:
                await conn.execute(
                    "UPDATE tracked_shows SET auto_download = 1, auto_since = ? "
                    "WHERE profile_id = ? AND show_id = ? "
                    "AND (auto_download = 0 OR auto_since IS NULL)",
                    (_now() if since is None else _utc(since), profile_id, show_id),
                )
            else:
                await conn.execute(
                    "UPDATE tracked_shows SET auto_download = 0, auto_since = NULL "
                    "WHERE profile_id = ? AND show_id = ?",
                    (profile_id, show_id),
                )

    async def auto_due(
        self, *, aired_from: datetime, aired_until: datetime, limit: int
    ) -> list[CalendarEntry]:
        """Episodes the automatic downloads should fetch now, at most `limit`: least tried
        first, then the one tried longest ago, then oldest first.

        Those of a show anyone follows with the switch on, numbered and not specials (season
        0), with an airstamp after the earliest moment one of them switched it on and within
        [aired_from, aired_until], not in the download history (nor their season pack), not
        marked "I already have this", not in Plex and not given up. Each episode once, however
        many people follow its show.
        """
        rows = await self._db.fetch_all(
            _AUTO_DUE_SQL,
            {"aired_from": _utc(aired_from), "aired_until": _utc(aired_until), "limit": limit},
        )
        return [_calendar_entry(row, is_tracked=True) for row in rows]

    async def record_auto_attempt(self, episode_id: int, when: datetime) -> None:
        """Count one more automatic try at an episode (ignored if the episode is gone)."""
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO auto_attempts (episode_id, tries, last_try) "
                "SELECT id, 1, :when FROM episodes WHERE id = :id "
                "ON CONFLICT (episode_id) DO UPDATE SET "
                "tries = auto_attempts.tries + 1, last_try = excluded.last_try",
                {"id": episode_id, "when": _utc(when)},
            )

    async def auto_give_up(self, *, aired_before: datetime, when: datetime) -> list[CalendarEntry]:
        """Mark as given up every episode the automatic downloads look after that aired before
        `aired_before` and still isn't downloaded; returns them, oldest first.

        Each episode is given up once: later calls skip it. An episode never tried gets an
        attempts row with no tries, stamped `when`.
        """
        async with self._db.transaction() as conn:
            rows = await conn.execute_fetchall(
                _AUTO_OVERDUE_SQL, {"aired_before": _utc(aired_before)}
            )
            entries = [_calendar_entry(row, is_tracked=True) for row in rows]
            await conn.executemany(
                "INSERT INTO auto_attempts (episode_id, tries, last_try, gave_up) "
                "VALUES (?, 0, ?, 1) ON CONFLICT (episode_id) DO UPDATE SET gave_up = 1",
                [(entry.episode.id, _utc(when)) for entry in entries],
            )
        return entries

    async def untrack(self, show_id: int, *, profile_id: int) -> FollowRecord | None:
        """Take a show off a person's library: the follow as it was (for Undo), or None when
        they didn't follow it. The show, its episodes and their "I already have this" marks
        stay: they are shared (another person may follow it; `prune_schedule` forgets what
        nobody follows)."""
        async with self._db.transaction() as conn:
            rows = await conn.execute_fetchall(
                "SELECT show_id, profile_id, tracked_at, auto_download, synced_at, auto_since "
                "FROM tracked_shows WHERE profile_id = ? AND show_id = ?",
                (profile_id, show_id),
            )
            await conn.execute(
                "DELETE FROM tracked_shows WHERE profile_id = ? AND show_id = ?",
                (profile_id, show_id),
            )
        for row in rows:
            return FollowRecord(
                show_id=row[0],
                profile_id=row[1],
                tracked_at=row[2],
                auto_download=bool(row[3]),
                synced_at=row[4],
                auto_since=row[5],
            )
        return None

    async def restore_follow(self, record: FollowRecord) -> bool:
        """Put a removed follow back as it was (Undo); False when the show left the cache
        meanwhile, or is followed again already."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO tracked_shows "
                "(profile_id, show_id, tracked_at, auto_download, synced_at, auto_since) "
                "SELECT ?, id, ?, ?, ?, ? FROM shows WHERE id = ?",
                (
                    record.profile_id,
                    record.tracked_at,
                    int(record.auto_download),
                    record.synced_at,
                    record.auto_since,
                    record.show_id,
                ),
            )
            return cursor.rowcount > 0

    async def untrack_ended(self, *, profile_id: int) -> int:
        """Drop every show a person follows whose status is Ended (other people keep theirs).
        Returns how many were removed."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM tracked_shows WHERE profile_id = ? "
                "AND show_id IN (SELECT id FROM shows WHERE status = 'Ended')",
                (profile_id,),
            )
            return cursor.rowcount

    async def mark_synced(self, show_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE tracked_shows SET synced_at = ? WHERE show_id = ?", (_now(), show_id)
            )

    async def mark_synced_many(self, show_ids: Iterable[int]) -> None:
        now = _now()
        rows = [(now, show_id) for show_id in show_ids]
        if not rows:
            return
        async with self._db.transaction() as conn:
            await conn.executemany("UPDATE tracked_shows SET synced_at = ? WHERE show_id = ?", rows)

    async def prune_schedule(self, before: date) -> int:
        """Forget cached schedule data from before `before` for shows nobody follows.

        Deletes those shows' older episodes, shows nobody follows left without any episode,
        and the matching `schedule_fetches` rows. A show anyone follows keeps every episode
        (and so their "I already have this" marks). Returns how many episodes were deleted.
        """
        cutoff = before.isoformat()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM episodes WHERE air_date < ? "
                "AND show_id NOT IN (SELECT show_id FROM tracked_shows)",
                (cutoff,),
            )
            deleted = cursor.rowcount
            await conn.execute(
                "DELETE FROM shows WHERE id NOT IN (SELECT show_id FROM tracked_shows) "
                "AND NOT EXISTS (SELECT 1 FROM episodes AS e WHERE e.show_id = shows.id)"
            )
            await conn.execute("DELETE FROM schedule_fetches WHERE air_date < ?", (cutoff,))
        return deleted

    async def get_show(self, show_id: int) -> Show | None:
        row = await self._db.fetch_one("SELECT * FROM shows WHERE id = ?", (show_id,))
        return _show_from_row(dict(row)) if row else None

    async def tracked(self, *, profile_id: int) -> list[LibraryEntry]:
        """The shows a person follows, by name."""
        rows = await self._db.fetch_all(
            f"{_TRACKED_SQL} ORDER BY s.name COLLATE NOCASE", {"profile": profile_id}
        )
        return [_library_entry(row) for row in rows]

    async def tracked_entry(self, show_id: int, *, profile_id: int) -> LibraryEntry | None:
        """A person's library entry of one show; None when they don't follow it."""
        row = await self._db.fetch_one(
            f"{_TRACKED_SQL} AND t.show_id = :show", {"profile": profile_id, "show": show_id}
        )
        return _library_entry(row) if row else None

    async def tracked_ids(self, *, profile_id: int) -> frozenset[int]:
        """The shows a person follows."""
        rows = await self._db.fetch_all(
            "SELECT show_id FROM tracked_shows WHERE profile_id = ?", (profile_id,)
        )
        return frozenset(row["show_id"] for row in rows)

    async def followed(self) -> list[LibraryEntry]:
        """Every show anyone follows, once, by name (the sync): see _FOLLOWED_SQL."""
        rows = await self._db.fetch_all(_FOLLOWED_SQL)
        return [_library_entry(row) for row in rows]

    async def followed_ids(self) -> frozenset[int]:
        """The shows anyone follows."""
        rows = await self._db.fetch_all("SELECT DISTINCT show_id FROM tracked_shows")
        return frozenset(row["show_id"] for row in rows)

    async def episodes(self, show_id: int) -> list[Episode]:
        rows = await self._db.fetch_all(
            "SELECT * FROM episodes WHERE show_id = ? ORDER BY season, number", (show_id,)
        )
        return [Episode.model_validate(dict(row)) for row in rows]

    async def next_episodes(
        self, *, profile_id: int, now: datetime, today: date
    ) -> list[Episode]:
        """Per show a person follows, its first numbered episode still to air: an airstamp
        after `now`, or (without one) an air date from `today` on. Shows with nothing upcoming
        are left out."""
        rows = await self._db.fetch_all(
            _NEXT_EPISODES_SQL,
            {"profile": profile_id, "now": _utc(now), "today": today.isoformat()},
        )
        return [Episode.model_validate(_section(row, "ep__")) for row in rows]

    async def missing_counts(
        self,
        *,
        profile_id: int,
        now: datetime,
        today: date,
        missing_by: datetime,
        missing_day: date,
    ) -> dict[int, int]:
        """Per id of a show the person follows, how many episodes of its latest season are
        missing (see
        _MISSING_CTE): the latest season by what aired by `now` (without an airstamp: by
        `today`), missing when aired by `missing_by` (without an airstamp: by `missing_day`).
        Shows with none are left out."""
        rows = await self._db.fetch_all(
            _MISSING_COUNTS_SQL, _missing_params(profile_id, now, today, missing_by, missing_day)
        )
        return {row["show_id"]: row["missing"] for row in rows}

    async def missing_between(
        self,
        start: date,
        end: date,
        *,
        profile_id: int,
        now: datetime,
        today: date,
        missing_by: datetime,
        missing_day: date,
    ) -> frozenset[int]:
        """Ids of the missing episodes of the shows a person follows (see _MISSING_CTE,
        `missing_counts`) airing between `start` and `end` (inclusive)."""
        rows = await self._db.fetch_all(
            _MISSING_BETWEEN_SQL,
            {
                **_missing_params(profile_id, now, today, missing_by, missing_day),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        return frozenset(row["id"] for row in rows)

    async def new_between(
        self,
        start: date,
        end: date,
        *,
        profile_id: int,
        now: datetime,
        today: date,
        missing_by: datetime,
        missing_day: date,
    ) -> frozenset[int]:
        """Ids of the new episodes of the shows a person follows (aired, neither downloaded nor
        in Plex, not yet missing; see _MISSING_CTE) airing between `start` and `end`
        (inclusive)."""
        rows = await self._db.fetch_all(
            _NEW_BETWEEN_SQL,
            {
                **_missing_params(profile_id, now, today, missing_by, missing_day),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        return frozenset(row["id"] for row in rows)

    async def plex_between(self, start: date, end: date) -> frozenset[int]:
        """Ids of the episodes airing between `start` and `end` (inclusive) that are in Plex
        (the calendar's stack cards)."""
        rows = await self._db.fetch_all(
            f"SELECT e.id FROM episodes AS e WHERE e.air_date BETWEEN ? AND ? AND {_IN_PLEX}",
            (start.isoformat(), end.isoformat()),
        )
        return frozenset(row["id"] for row in rows)

    async def downloaded_between(
        self, start: date, end: date, today: date, since: datetime
    ) -> frozenset[int]:
        """Ids of the episodes airing between `start` and `end` (inclusive) that AirGrabber
        downloaded since `since` and Plex doesn't have (the calendar's "✓ Downloaded": on its
        way to Plex). Only an episode that has aired, and only a recent grab: an older one Plex
        doesn't have was deleted there."""
        rows = await self._db.fetch_all(
            f"""
            SELECT e.id FROM episodes AS e
            WHERE e.air_date BETWEEN ? AND ? AND e.air_date <= ?
              AND EXISTS (
                  SELECT 1 FROM download_history AS h
                  WHERE h.media_type = 'tv' AND h.media_id = e.show_id AND h.season = e.season
                    AND julianday(h.downloaded_at) >= julianday(?)
                    AND (h.episode = e.number OR (h.episode IS NULL AND {_PACK_HOLDS}))
              )
              AND NOT {_IN_PLEX}
            """,
            (start.isoformat(), end.isoformat(), today.isoformat(), since.isoformat()),
        )
        return frozenset(row["id"] for row in rows)

    async def plex_ids(self, show_id: int) -> frozenset[int]:
        """Ids of the show's episodes that are in Plex."""
        rows = await self._db.fetch_all(
            f"SELECT e.id FROM episodes AS e WHERE e.show_id = ? AND {_IN_PLEX}", (show_id,)
        )
        return frozenset(row["id"] for row in rows)

    async def plex_films(self) -> PlexFilms:
        """The films in Plex, by TMDB id and IMDb id."""
        rows = await self._db.fetch_all("SELECT tmdb_id, imdb_id FROM plex_movies")
        return PlexFilms(
            tmdb=frozenset(row["tmdb_id"] for row in rows if row["tmdb_id"] is not None),
            imdb=frozenset(row["imdb_id"] for row in rows if row["imdb_id"] is not None),
        )

    async def calendar(self, start: date, end: date, *, profile_id: int) -> list[CalendarEntry]:
        """The episodes of the shows a person follows airing between `start` and `end`
        (inclusive), in airing order."""
        rows = await self._db.fetch_all(
            _CALENDAR_SQL,
            {"profile": profile_id, "start": start.isoformat(), "end": end.isoformat()},
        )
        return [_calendar_entry(row, is_tracked=True) for row in rows]

    async def gaps(self, *, profile_id: int, now: datetime, today: date) -> list[CalendarEntry]:
        """The aired episodes (by `now`; without an airstamp by `today`) of the shows a person
        follows that Plex doesn't have, in the seasons from the first to the last one Plex has
        some of; by show name, season and number. `is_downloaded`: grabbed already."""
        rows = await self._db.fetch_all(
            _GAPS_SQL, {"profile": profile_id, "now": _utc(now), "today": today.isoformat()}
        )
        return [_calendar_entry(row, is_tracked=True) for row in rows]

    async def coming_up(
        self, after: date, *, profile_id: int, limit: int
    ) -> list[CalendarEntry]:
        """The next numbered episode after `after` of each show a person follows, soonest first,
        at most `limit` (the rolling week's "Coming up" row)."""
        rows = await self._db.fetch_all(
            f"""
            SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, 0 AS is_downloaded
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY show_id ORDER BY air_date, season, number
                ) AS place
                FROM episodes
                WHERE air_date > :after AND number IS NOT NULL
                  AND show_id IN (SELECT show_id FROM tracked_shows WHERE profile_id = :profile)
            ) AS e
            JOIN shows AS s ON s.id = e.show_id
            WHERE e.place = 1
            ORDER BY e.air_date, e.airstamp, s.name COLLATE NOCASE
            LIMIT :limit
            """,
            {"after": after.isoformat(), "profile": profile_id, "limit": limit},
        )
        return [_calendar_entry(row, is_tracked=True) for row in rows]

    async def suggestions(
        self,
        start: date,
        end: date,
        *,
        profile_id: int,
        per_day: int,
        language: str = "English",
        types: Sequence[str] = ("Scripted", "Animation"),
        fallback_types: Sequence[str] = (
            "Reality",
            "Variety",
            "Documentary",
            "Panel Show",
            "Game Show",
        ),
        min_weight: int = 40,
    ) -> list[CalendarEntry]:
        """The most popular shows the person doesn't follow airing each day, from the cached
        TVMaze schedule, best first. A day's shows of `types` come first, its shows of
        `fallback_types` after them: those fill the days with few of the first kind, such as
        Saturdays, when the US networks air hardly any new scripted episodes. (News, talk shows,
        sports and award shows are never suggested.)"""
        rows = await self._db.fetch_all(
            _SUGGESTIONS_SQL,
            {
                "profile": profile_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "language": language,
                "types": json.dumps(list(types)),
                "fallback_types": json.dumps(list(fallback_types)),
                "min_weight": min_weight,
                "per_day": per_day,
            },
        )
        return [_calendar_entry(row, is_tracked=False) for row in rows]

    async def schedule_fetches(self, start: date, end: date) -> dict[date, datetime]:
        """When each day in the range was last pulled from TVMaze's schedule."""
        rows = await self._db.fetch_all(
            "SELECT air_date, fetched_at FROM schedule_fetches WHERE air_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        return {
            date.fromisoformat(row["air_date"]): datetime.fromisoformat(row["fetched_at"])
            for row in rows
        }

    async def stats(self, *, profile_id: int) -> LibraryStats:
        """How many shows a person follows, their episodes, and the latest sync (anyone's: the
        sync covers everyone's shows)."""
        row = await self._db.fetch_one(
            """
            SELECT (SELECT COUNT(*) FROM tracked_shows WHERE profile_id = :profile)
                       AS tracked_shows,
                   (SELECT COUNT(*) FROM episodes AS e
                      JOIN tracked_shows AS t ON t.show_id = e.show_id
                      WHERE t.profile_id = :profile) AS episodes,
                   (SELECT MAX(synced_at) FROM tracked_shows) AS last_synced
            """,
            {"profile": profile_id},
        )
        assert row is not None  # an aggregate-only SELECT always yields one row
        return LibraryStats.model_validate(dict(row))


class HistoryRepository:
    """Grabbed releases, used to flag what was already downloaded."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, record: DownloadRecord) -> bool:
        """Record a grab. Returns False if this exact torrent (info hash) was already recorded.

        A film's grab also takes it off every person's watchlist, in the same transaction and
        however it was grabbed: a downloaded film is on the shared PC, it leaves every list.
        """
        columns = [column for column in HISTORY_COLUMNS if column != "id"]
        sql = (
            f"INSERT INTO download_history ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + column for column in columns)}) "
            "ON CONFLICT (info_hash) DO NOTHING"
        )
        async with self._db.transaction() as conn:
            cursor = await conn.execute(sql, record.model_dump(mode="json", exclude={"id"}))
            if record.media_type is MediaType.MOVIE:
                await conn.execute("DELETE FROM watchlist WHERE movie_id = ?", (record.media_id,))
            return cursor.rowcount > 0

    async def has_episode(self, show_id: int, season: int, episode: int | None) -> bool:
        """True if the episode was grabbed, or a pack of its whole season that can hold it: one
        grabbed after it aired. An episode the cache doesn't know gets the pack's benefit of the
        doubt (better than fetching it twice). `episode` None: the season's pack itself."""
        row = await self._db.fetch_one(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM download_history AS h
                WHERE h.media_type = :tv AND h.media_id = :show AND h.season = :season
                  AND (
                      h.episode IS :episode
                      OR (
                          h.episode IS NULL
                          AND (
                              EXISTS (
                                  SELECT 1 FROM episodes AS e
                                  WHERE e.show_id = h.media_id AND e.season = h.season
                                    AND e.number = :episode AND {_PACK_HOLDS}
                              )
                              OR NOT EXISTS (
                                  SELECT 1 FROM episodes AS e
                                  WHERE e.show_id = h.media_id AND e.season = h.season
                                    AND e.number = :episode
                              )
                          )
                      )
                  )
            )
            """,
            {"tv": MediaType.TV, "show": show_id, "season": season, "episode": episode},
        )
        return bool(row and row[0])

    async def show_grabs(self, show_id: int) -> frozenset[tuple[int, int | None, datetime]]:
        """(season, episode, when: its latest) of every grab for a TV show; episode None = a
        season pack."""
        rows = await self._db.fetch_all(
            "SELECT season, episode, MAX(downloaded_at) AS downloaded_at FROM download_history "
            "WHERE media_type = ? AND media_id = ? GROUP BY season, episode",
            (MediaType.TV, show_id),
        )
        return frozenset(
            (row["season"], row["episode"], datetime.fromisoformat(row["downloaded_at"]))
            for row in rows
        )

    async def has_movie(self, movie_id: int) -> bool:
        row = await self._db.fetch_one(
            "SELECT EXISTS (SELECT 1 FROM download_history WHERE media_type = ? AND media_id = ?)",
            (MediaType.MOVIE, movie_id),
        )
        return bool(row and row[0])

    async def recent(self, limit: int = 50) -> list[DownloadRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM download_history ORDER BY downloaded_at DESC LIMIT ?", (limit,)
        )
        return [DownloadRecord.model_validate(dict(row)) for row in rows]

    async def listed(self, limit: int = 50) -> list[DownloadRecord]:
        """The newest grabs not taken off the Downloads page (`clear`), newest first."""
        rows = await self._db.fetch_all(
            "SELECT h.* FROM download_history AS h "
            "WHERE NOT EXISTS (SELECT 1 FROM cleared_downloads AS c WHERE c.history_id = h.id) "
            "ORDER BY h.downloaded_at DESC LIMIT ?",
            (limit,),
        )
        return [DownloadRecord.model_validate(dict(row)) for row in rows]

    async def clear(self, history_ids: Iterable[int], when: datetime) -> int:
        """Take grabs off the Downloads page ("Clear finished", "Remove from list"): they stay
        in the history. Unknown ids are ignored. Returns how many were newly taken off."""
        ids = json.dumps(sorted(set(history_ids)))
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO cleared_downloads (history_id, cleared_at) "
                "SELECT id, ? FROM download_history WHERE id IN (SELECT value FROM json_each(?)) "
                "ON CONFLICT (history_id) DO NOTHING",
                (_utc(when), ids),
            )
            return cursor.rowcount

    async def every_grab(self) -> list[DownloadRecord]:
        """The whole history, newest first (the Downloads page matches the torrent program's
        older tasks to it; it reads it again only when `stamp` changes)."""
        rows = await self._db.fetch_all(
            "SELECT * FROM download_history ORDER BY downloaded_at DESC, id DESC"
        )
        return [DownloadRecord.model_validate(dict(row)) for row in rows]

    async def stamp(self) -> tuple[int, int]:
        """(how many grabs, the highest id): changes whenever a grab is added or removed."""
        row = await self._db.fetch_one("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM download_history")
        return (int(row[0]), int(row[1])) if row is not None else (0, 0)

    async def seen(self, history_ids: Iterable[int]) -> frozenset[int]:
        """Of these grabs, the ones their torrent program has had (`mark_seen`)."""
        ids = json.dumps(sorted(set(history_ids)))
        rows = await self._db.fetch_all(
            "SELECT history_id FROM seen_downloads "
            "WHERE history_id IN (SELECT value FROM json_each(?))",
            (ids,),
        )
        return frozenset(row["history_id"] for row in rows)

    async def mark_seen(self, history_ids: Iterable[int], when: datetime) -> int:
        """Note that the torrent program has these grabs (the Downloads page). Unknown ids are
        ignored. Returns how many were newly marked."""
        ids = json.dumps(sorted(set(history_ids)))
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO seen_downloads (history_id, seen_at) "
                "SELECT id, ? FROM download_history WHERE id IN (SELECT value FROM json_each(?)) "
                "ON CONFLICT (history_id) DO NOTHING",
                (_utc(when), ids),
            )
            return cursor.rowcount

    async def latest_tv_grabs(self, since: datetime) -> list[DownloadRecord]:
        """Per TV show, its latest grab at or after `since` (episodes and season packs)."""
        rows = await self._db.fetch_all(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY media_id ORDER BY julianday(downloaded_at) DESC, id DESC
                ) AS nth
                FROM download_history
                WHERE media_type = 'tv' AND julianday(downloaded_at) >= julianday(?)
            )
            WHERE nth = 1
            """,
            (_utc(since),),
        )
        return [
            DownloadRecord.model_validate({k: row[k] for k in row.keys() if k != "nth"})
            for row in rows
        ]


_WATCHLIST_SQL = """
    SELECT movie_id, movie, added_at, auto_download, imdb_id, digital_release,
           theatrical_release, details_at, tries, first_try, last_try, gave_up
    FROM watchlist
    WHERE profile_id = :profile
"""

# Every film anyone has on their list, once (the automatic downloads): its switch is on while
# anyone's is. The release dates and the tries are the film's (kept the same in every row).
def _films_sql(where: str = "") -> str:
    return f"""
    SELECT movie_id, movie, MIN(added_at) AS added_at, MAX(auto_download) AS auto_download,
           imdb_id, digital_release, theatrical_release, MAX(details_at) AS details_at,
           MAX(tries) AS tries, MIN(first_try) AS first_try, MAX(last_try) AS last_try,
           MAX(gave_up) AS gave_up
    FROM watchlist
    {where}
    GROUP BY movie_id
    ORDER BY movie_id
"""


_FILMS_SQL = _films_sql()
_FILM_SQL = _films_sql("WHERE movie_id = :id")

# A film onto a person's list (:profile). When someone else has it already, the film's own
# columns (release dates, the automatic tries, given up) come from their row: they are the
# film's, not a person's; a film given up starts with its switch off, as given-up films have
# it. (The WHERE keeps SQLite from reading ON CONFLICT as the join's ON.)
_ADD_FILM_SQL = """
    INSERT INTO watchlist (profile_id, movie_id, title, movie, added_at, auto_download, imdb_id,
                           digital_release, theatrical_release, details_at, tries, first_try,
                           last_try, gave_up)
    SELECT :profile, :id, :title, :movie, :when,
           CASE WHEN COALESCE(o.gave_up, 0) THEN 0 ELSE :auto END,
           COALESCE(o.imdb_id, :imdb_id), o.digital_release, o.theatrical_release, o.details_at,
           COALESCE(o.tries, 0), o.first_try, o.last_try, COALESCE(o.gave_up, 0)
    FROM (SELECT 1) AS one
    LEFT JOIN (
        SELECT * FROM watchlist WHERE movie_id = :id
        ORDER BY details_at IS NULL, julianday(details_at) DESC LIMIT 1
    ) AS o ON 1
    WHERE 1
    ON CONFLICT (profile_id, movie_id) DO NOTHING
"""


def _watchlist_entry(row: sqlite3.Row) -> WatchlistEntry:
    return WatchlistEntry(
        movie=Movie.model_validate_json(row["movie"]),
        added_at=row["added_at"],
        auto_download=bool(row["auto_download"]),
        imdb_id=row["imdb_id"],
        digital_release=row["digital_release"],
        theatrical_release=row["theatrical_release"],
        details_at=row["details_at"],
        tries=row["tries"],
        first_try=row["first_try"],
        last_try=row["last_try"],
        gave_up=bool(row["gave_up"]),
    )


def _iso_date(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


def _readable(rows: Iterable[sqlite3.Row]) -> list[WatchlistEntry]:
    """The rows as entries; a row whose stored film no longer reads as a `Movie` is skipped
    (and logged) rather than hiding the whole list."""
    entries: list[WatchlistEntry] = []
    for row in rows:
        try:
            entries.append(_watchlist_entry(row))
        except ValidationError:
            log.warning("Skipping watchlist film %d: its stored data no longer reads", row[0])
    return entries


class WatchlistRepository:
    """The film watchlists, a person's each (every method that takes `profile_id`): films by
    TMDB id, with their release dates and automatic tries. Those two are the film's: they are
    written to every row of the film (whoever's list it is on) and come along when another
    person adds it, so a film is searched for (and given up) once, not once per person."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def entries(self, *, profile_id: int) -> list[WatchlistEntry]:
        """Every film on a person's list, newest first (see `_readable`)."""
        rows = await self._db.fetch_all(
            f"{_WATCHLIST_SQL} ORDER BY julianday(added_at) DESC, movie_id DESC",
            {"profile": profile_id},
        )
        return _readable(rows)

    async def entry(self, movie_id: int, *, profile_id: int) -> WatchlistEntry | None:
        row = await self._db.fetch_one(
            f"{_WATCHLIST_SQL} AND movie_id = :id", {"profile": profile_id, "id": movie_id}
        )
        return _watchlist_entry(row) if row else None

    async def films(self) -> list[WatchlistEntry]:
        """Every film on anyone's list, once (see _FILMS_SQL)."""
        return _readable(await self._db.fetch_all(_FILMS_SQL))

    async def film(self, movie_id: int) -> WatchlistEntry | None:
        """One film as `films` has it; None when it is on nobody's list."""
        row = await self._db.fetch_one(_FILM_SQL, {"id": movie_id})
        return _watchlist_entry(row) if row else None

    async def add(
        self, movie: Movie, *, profile_id: int, auto_download: bool, when: datetime
    ) -> bool:
        """Put a film on a person's list. A film already on it keeps its row (and switch);
        returns whether it was added. See _ADD_FILM_SQL."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                _ADD_FILM_SQL,
                {
                    "profile": profile_id,
                    "id": movie.id,
                    "title": movie.title,
                    "movie": movie.model_dump_json(),
                    "when": _utc(when),
                    "auto": auto_download,
                    "imdb_id": movie.imdb_id,
                },
            )
            return cursor.rowcount > 0

    async def remove(self, movie_id: int, *, profile_id: int) -> bool:
        """Take a film off a person's list; returns whether it was on it."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM watchlist WHERE profile_id = ? AND movie_id = ?",
                (profile_id, movie_id),
            )
            return cursor.rowcount > 0

    async def set_auto_download(self, movie_id: int, on: bool, *, profile_id: int) -> None:
        """Switch a person's "Download when it's out" on or off. Switching it on starts
        afresh: the film's tries and a given-up mark are forgotten (on every list)."""
        async with self._db.transaction() as conn:
            if on:
                cursor = await conn.execute(
                    "UPDATE watchlist SET auto_download = 1 "
                    "WHERE profile_id = ? AND movie_id = ? AND auto_download = 0",
                    (profile_id, movie_id),
                )
                if cursor.rowcount > 0:
                    await conn.execute(
                        "UPDATE watchlist SET tries = 0, first_try = NULL, last_try = NULL, "
                        "gave_up = 0 WHERE movie_id = ?",
                        (movie_id,),
                    )
            else:
                await conn.execute(
                    "UPDATE watchlist SET auto_download = 0 WHERE profile_id = ? AND movie_id = ?",
                    (profile_id, movie_id),
                )

    async def switch_off(self, movie_id: int) -> None:
        """Switch "Download when it's out" off on every list that has the film (the automatic
        downloads found it downloaded already)."""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE watchlist SET auto_download = 0 WHERE movie_id = ?", (movie_id,)
            )

    async def set_release_dates(
        self,
        movie_id: int,
        *,
        imdb_id: str | None,
        digital: date | None,
        theatrical: date | None,
        when: datetime,
    ) -> None:
        """Store what TMDB's details say about a film (on every list that has it); an IMDb id
        already known is kept when the details name none."""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE watchlist SET imdb_id = COALESCE(?, imdb_id), digital_release = ?, "
                "theatrical_release = ?, details_at = ? WHERE movie_id = ?",
                (imdb_id, _iso_date(digital), _iso_date(theatrical), _utc(when), movie_id),
            )

    async def record_try(self, movie_id: int, when: datetime, *, give_up: bool) -> None:
        """Count one more automatic try at a film (on every list that has it); with `give_up`
        the automatic downloads stop looking and every switch of it goes off. Ignored for a
        film that is on nobody's list."""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE watchlist SET tries = tries + 1, first_try = COALESCE(first_try, :when), "
                "last_try = :when, gave_up = :give_up, "
                "auto_download = CASE WHEN :give_up THEN 0 ELSE auto_download END "
                "WHERE movie_id = :id",
                {"id": movie_id, "when": _utc(when), "give_up": give_up},
            )


# plex_titles' columns and the PlexTitle fields they hold, in the same order.
_TITLE_COLUMNS = (
    "kind", "rating_key", "title", "year", "thumb", "added_at", "seasons", "episodes",
    "resolution", "show_id", "tvdb_id", "imdb_id", "tmdb_id",
)
_TITLE_FIELDS = (
    "kind", "key", "title", "year", "thumb", "added_at", "seasons", "episodes",
    "resolution", "show_id", "tvdb_id", "imdb_id", "tmdb_id",
)


def _title_row(title: PlexTitle) -> tuple[Any, ...]:
    values = title.model_dump()
    values["added_at"] = _utc(title.added_at) if title.added_at is not None else None
    return tuple(values[field] for field in _TITLE_FIELDS)


def _plex_library(item: Any) -> PlexLibrary | None:
    """A library as plex_state.libraries keeps it; None for one that doesn't read."""
    try:
        return PlexLibrary.model_validate(item)
    except ValidationError:
        return None


class PlexRepository:
    """What is in the user's Plex libraries (app/services/plex.py): the episodes of the shows
    AirGrabber knows, the films, and the last check's state. Shared by everyone."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def known_shows(self) -> list[tuple[int, int | None, str | None]]:
        """Every show in the cache with a TheTVDB or IMDb id: (show id, tvdb id, imdb id)."""
        rows = await self._db.fetch_all(
            "SELECT id, tvdb_id, imdb_id FROM shows "
            "WHERE tvdb_id IS NOT NULL OR imdb_id IS NOT NULL ORDER BY id"
        )
        return [(row["id"], row["tvdb_id"], row["imdb_id"]) for row in rows]

    async def contents(
        self,
    ) -> tuple[frozenset[tuple[int, int, int]], frozenset[tuple[int | None, str | None]]]:
        """What is stored: the episodes (show id, season, episode) and the films (TMDB id,
        IMDb id)."""
        episodes = await self._db.fetch_all("SELECT show_id, season, episode FROM plex_episodes")
        films = await self._db.fetch_all("SELECT tmdb_id, imdb_id FROM plex_movies")
        return (
            frozenset((row["show_id"], row["season"], row["episode"]) for row in episodes),
            frozenset((row["tmdb_id"], row["imdb_id"]) for row in films),
        )

    async def replace(self, scan: PlexScan, when: datetime) -> None:
        """Make `scan` what is stored, in one transaction: its episodes (of shows still in the
        cache), its films and the state row."""
        libraries = json.dumps([library.model_dump(mode="json") for library in scan.libraries])
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM plex_episodes")
            await conn.execute("DELETE FROM plex_movies")
            await conn.executemany(
                "INSERT INTO plex_episodes (show_id, season, episode) "
                "SELECT id, ?, ? FROM shows WHERE id = ? "
                "ON CONFLICT (show_id, season, episode) DO NOTHING",
                [(season, episode, show_id) for show_id, season, episode in sorted(scan.episodes)],
            )
            await conn.executemany(
                "INSERT INTO plex_movies (tmdb_id, imdb_id) VALUES (?, ?)",
                sorted(scan.films, key=lambda film: (film[0] or 0, film[1] or "")),
            )
            await conn.execute("DELETE FROM plex_titles")
            await conn.executemany(
                f"INSERT INTO plex_titles ({', '.join(_TITLE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _TITLE_COLUMNS)}) "
                "ON CONFLICT (kind, rating_key) DO NOTHING",
                [_title_row(title) for title in scan.titles],
            )
            await conn.execute(
                "INSERT INTO plex_state "
                "(id, checked_at, shows, unmatched_shows, unmatched_films, libraries) "
                "VALUES (1, :when, :shows, :unmatched_shows, :unmatched_films, :libraries) "
                "ON CONFLICT (id) DO UPDATE SET checked_at = excluded.checked_at, "
                "shows = excluded.shows, unmatched_shows = excluded.unmatched_shows, "
                "unmatched_films = excluded.unmatched_films, libraries = excluded.libraries",
                {
                    "when": _utc(when),
                    "shows": scan.shows,
                    "unmatched_shows": scan.unmatched_shows,
                    "unmatched_films": scan.unmatched_films,
                    "libraries": libraries,
                },
            )

    async def titles(self) -> list[PlexTitle]:
        """The Library page's shows and films, as the last check read them, by title."""
        rows = await self._db.fetch_all(
            f"SELECT {', '.join(_TITLE_COLUMNS)} FROM plex_titles ORDER BY title COLLATE NOCASE"
        )
        found = []
        for row in rows:
            values = dict(zip(_TITLE_FIELDS, tuple(row), strict=True))
            try:
                found.append(PlexTitle.model_validate(values))
            except ValidationError:
                continue  # a row a newer version wrote differently
        return found

    async def clear(self) -> None:
        """Forget everything (Disconnect)."""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM plex_titles")
            await conn.execute("DELETE FROM plex_episodes")
            await conn.execute("DELETE FROM plex_movies")
            await conn.execute("DELETE FROM plex_state")

    async def counts(self) -> PlexCounts:
        """The Settings box's counts: the stored episodes of the shows anyone follows and how
        many of those shows have any, the films, and the last check."""
        row = await self._db.fetch_one(
            """
            SELECT (SELECT COUNT(*) FROM plex_episodes
                      WHERE show_id IN (SELECT show_id FROM tracked_shows)) AS episodes,
                   (SELECT COUNT(DISTINCT show_id) FROM plex_episodes
                      WHERE show_id IN (SELECT show_id FROM tracked_shows)) AS shows,
                   (SELECT COUNT(*) FROM plex_movies) AS films,
                   (SELECT unmatched_shows FROM plex_state WHERE id = 1) AS unmatched_shows,
                   (SELECT checked_at FROM plex_state WHERE id = 1) AS checked_at
            """
        )
        assert row is not None  # an aggregate-only SELECT always yields one row
        return PlexCounts(
            episodes=row["episodes"],
            shows=row["shows"],
            films=row["films"],
            unmatched_shows=row["unmatched_shows"] or 0,
            checked_at=row["checked_at"],
        )

    async def libraries(self) -> tuple[PlexLibrary, ...]:
        """The show and film libraries the last check saw."""
        row = await self._db.fetch_one("SELECT libraries FROM plex_state WHERE id = 1")
        if row is None:
            return ()
        try:
            items = json.loads(row["libraries"])
        except ValueError:
            return ()
        if not isinstance(items, list):
            return ()
        found = (_plex_library(item) for item in items)
        return tuple(library for library in found if library is not None)


class SettingsRepository:
    """Preferences as key/value rows, validated as one `Settings` model on the way out."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> Settings:
        rows = await self._db.fetch_all("SELECT key, value FROM settings")
        stored = {row["key"]: json.loads(row["value"]) for row in rows}
        try:
            return Settings.model_validate(stored)
        except ValidationError as exc:
            # Keep the valid preferences rather than resetting everything over one bad value.
            invalid = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            log.warning("Ignoring invalid stored settings: %s", ", ".join(sorted(invalid)) or "?")
            try:
                return Settings.model_validate({k: v for k, v in stored.items() if k not in invalid})
            except ValidationError:
                return Settings()

    async def save(self, settings: Settings) -> None:
        rows = [(key, json.dumps(value)) for key, value in settings.model_dump(mode="json").items()]
        async with self._db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                rows,
            )
            # Drop retired keys (e.g. the old size limits) so they do not linger in the table.
            fields = tuple(Settings.model_fields)
            placeholders = ", ".join("?" * len(fields))
            await conn.execute(f"DELETE FROM settings WHERE key NOT IN ({placeholders})", fields)


# -- backups (app/services/backup.py) -------------------------------------------------------------

_BACKUP_SHOWS_SQL = f"""
    SELECT {_SHOW_FIELDS}, t.profile_id, t.tracked_at, t.auto_download, t.auto_since
    FROM tracked_shows AS t JOIN shows AS s ON s.id = t.show_id
    ORDER BY t.profile_id, s.name COLLATE NOCASE, s.id
"""
_BACKUP_EPISODES_SQL = """
    SELECT e.* FROM episodes AS e
    WHERE e.show_id IN (SELECT show_id FROM tracked_shows)
    ORDER BY e.show_id, e.season, e.number, e.id
"""
_BACKUP_HAVE_SQL = """
    SELECT e.show_id, m.episode_id FROM have_episodes AS m
    JOIN episodes AS e ON e.id = m.episode_id
    WHERE e.show_id IN (SELECT show_id FROM tracked_shows)
    ORDER BY m.episode_id
"""
_BACKUP_FILMS_SQL = """
    SELECT movie_id, movie, added_at, auto_download, imdb_id, digital_release,
           theatrical_release, details_at, tries, first_try, last_try, gave_up, profile_id
    FROM watchlist
    ORDER BY profile_id, julianday(added_at), movie_id
"""
_BACKUP_HISTORY_SQL = """
    SELECT h.*, EXISTS (SELECT 1 FROM cleared_downloads AS c WHERE c.history_id = h.id) AS cleared
    FROM download_history AS h
    ORDER BY julianday(h.downloaded_at), h.id
"""
# A grab from a backup, unless the history has it: the same torrent (info hash), or without a
# hash the same release of the same title recorded at the same moment (so loading a backup
# twice adds nothing).
_RESTORE_GRAB_SQL = """
    INSERT INTO download_history (media_type, media_id, season, episode, release_title,
                                  info_hash, source, size_bytes, saved_path, downloaded_at)
    SELECT :media_type, :media_id, :season, :episode, :release_title, :info_hash, :source,
           :size_bytes, :saved_path, :downloaded_at
    WHERE NOT EXISTS (
        SELECT 1 FROM download_history AS h
        WHERE h.info_hash = :info_hash
           OR (:info_hash IS NULL AND h.info_hash IS NULL
               AND h.media_type = :media_type AND h.media_id = :media_id
               AND h.season IS :season AND h.episode IS :episode
               AND h.release_title = :release_title
               AND julianday(h.downloaded_at) = julianday(:downloaded_at))
    )
"""
# A film from a backup onto a person's watchlist, unless it was downloaded (a downloaded film
# leaves every list) or is on their list already.
_RESTORE_FILM_SQL = """
    INSERT INTO watchlist (profile_id, movie_id, title, movie, added_at, auto_download, imdb_id,
                           digital_release, theatrical_release)
    SELECT :profile_id, :movie_id, :title, :movie, :added_at, :auto_download, :imdb_id,
           :digital_release, :theatrical_release
    WHERE NOT EXISTS (
        SELECT 1 FROM download_history WHERE media_type = 'movie' AND media_id = :movie_id
    )
    ON CONFLICT (profile_id, movie_id) DO NOTHING
"""


def _backup_show(
    row: sqlite3.Row, episodes: Sequence[Episode], have: Sequence[int]
) -> BackupShow:
    return BackupShow(
        profile=row["profile_id"],
        show=_show_from_row(_section(row, "show__")),
        tracked_at=row["tracked_at"],
        auto_download=bool(row["auto_download"]),
        auto_since=row["auto_since"],
        episodes=tuple(episodes),
        have=tuple(have),
    )


def _backup_grab(row: sqlite3.Row) -> BackupGrab:
    fields = {key: row[key] for key in row.keys() if key not in ("id", "cleared")}
    return BackupGrab(record=DownloadRecord.model_validate(fields), cleared=bool(row["cleared"]))


def _film_row(film: BackupFilm, profile_id: int) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "movie_id": film.movie.id,
        "title": film.movie.title,
        "movie": film.movie.model_dump_json(),
        "added_at": _utc(film.added_at),
        "auto_download": film.auto_download,
        "imdb_id": film.imdb_id or film.movie.imdb_id,
        "digital_release": _iso_date(film.digital_release),
        "theatrical_release": _iso_date(film.theatrical_release),
    }


def _backup_colour(person: BackupProfile) -> ProfileColour:
    try:
        return ProfileColour(person.colour)
    except ValueError:  # a newer version's colour
        return ProfileColour.BLUE


def match_people(
    backup: Sequence[BackupProfile], here: Sequence[Profile]
) -> tuple[dict[int, int], list[BackupProfile], list[Profile]]:
    """Which person here each of a backup's people is: first by name (ignoring case, both
    named), then the rest in order, oldest first. Returns ({backup id: id here}, the backup's
    people nobody here matches, the people here nobody in the backup matches)."""
    left = list(here)
    by_name = {person.name.casefold(): person for person in here if person.name}
    mapping: dict[int, int] = {}
    unmatched: list[BackupProfile] = []
    ordered = sorted(backup, key=lambda person: person.id)
    for person in ordered:
        match = by_name.get(person.name.casefold()) if person.name else None
        if match is not None and match in left:
            mapping[person.id] = match.id
            left.remove(match)
        else:
            unmatched.append(person)
    extra: list[BackupProfile] = []
    for person in unmatched:
        if left:
            mapping[person.id] = left.pop(0).id
        else:
            extra.append(person)
    return mapping, extra, left


class BackupRepository:
    """A backup's database part (app/services/backup.py): the people, the shows each follows
    with their episodes, automatic-download switches and "I already have this" marks, each
    person's film watchlist and the download history. Read in one go; written back in one
    transaction."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def read(self) -> BackupData:
        """Everything a backup holds from the database, as one consistent snapshot. A
        watchlist film whose stored data no longer reads is left out (and logged)."""
        async with self._db.transaction() as conn:
            profile_rows = await conn.execute_fetchall("SELECT * FROM profiles ORDER BY id")
            show_rows = await conn.execute_fetchall(_BACKUP_SHOWS_SQL)
            episode_rows = await conn.execute_fetchall(_BACKUP_EPISODES_SQL)
            have_rows = await conn.execute_fetchall(_BACKUP_HAVE_SQL)
            film_rows = await conn.execute_fetchall(_BACKUP_FILMS_SQL)
            grab_rows = await conn.execute_fetchall(_BACKUP_HISTORY_SQL)
        people = tuple(
            BackupProfile(
                id=person.id,
                name=person.name,
                colour=person.colour.value,
                created_at=person.created_at,
            )
            for person in (_profile(row) for row in profile_rows)
        )
        episodes: dict[int, list[Episode]] = {}
        for row in episode_rows:
            episode = Episode.model_validate(dict(row))
            episodes.setdefault(episode.show_id, []).append(episode)
        have: dict[int, list[int]] = {}
        for row in have_rows:
            have.setdefault(row["show_id"], []).append(row["episode_id"])
        films: list[BackupFilm] = []
        for row in film_rows:
            try:
                entry = _watchlist_entry(row)
            except ValidationError:
                log.warning("Leaving watchlist film %d out of the backup: it doesn't read", row[0])
                continue
            films.append(
                BackupFilm(
                    profile=row["profile_id"],
                    movie=entry.movie,
                    added_at=entry.added_at,
                    auto_download=entry.auto_download,
                    imdb_id=entry.imdb_id,
                    digital_release=entry.digital_release,
                    theatrical_release=entry.theatrical_release,
                )
            )
        shows = []
        carried: set[int] = set()  # shows whose episodes and marks are in the file already
        for row in show_rows:
            show_id = row["show__id"]
            first = show_id not in carried
            carried.add(show_id)
            shows.append(
                _backup_show(
                    row,
                    episodes.get(show_id, ()) if first else (),
                    have.get(show_id, ()) if first else (),
                )
            )
        return BackupData(
            profiles=people,
            shows=tuple(shows),
            films=tuple(films),
            grabs=tuple(_backup_grab(r) for r in grab_rows),
        )

    async def restore(
        self, data: BackupData, *, replace: bool, when: datetime, selected: int
    ) -> RestoreCounts:
        """Write a backup's database part, all or nothing.

        People: a backup from before people (no `profiles`) is one person's lists, written to
        the `selected` person; no one is added or removed. Else its people are matched to the
        people here (`match_people`) and those nobody here matches are added. `replace`: the
        people become the backup's (a match takes its name and colour; people here nobody in
        it matches are removed, with their lists).

        Per matched person, as before people: `replace`: their followed shows and watchlist
        become the backup's (shows it lacks are no longer followed, films it lacks leave the
        list; a show followed here too takes the backup's switch). Else the backup's shows and
        films are added and what is here wins (a show followed here keeps its switch). A show's
        "I already have this" marks (shared) become the backup's with `replace`, or when nobody
        here followed it yet; a show anyone here follows keeps the episodes stored here. Either
        way the backup's download history is added to this one (no grab twice); a downloaded
        film leaves every watchlist. A newly followed show is synced from TVMaze at the next
        sync. `when`: the moment marks, cleared grabs and new people are stamped with.
        """
        async with self._db.transaction() as conn:
            rows = await conn.execute_fetchall("SELECT * FROM profiles ORDER BY id")
            here = [_profile(row) for row in rows]
            people_added = people_removed = 0
            if not data.profiles:
                mapping = {FIRST_PROFILE_ID: selected}
            else:
                mapping, extra, unmatched = match_people(data.profiles, here)
                backup_people = {person.id: person for person in data.profiles}
                if replace:
                    for backup_id, here_id in mapping.items():
                        person = backup_people[backup_id]
                        created = _opt_utc(person.created_at)
                        await conn.execute(
                            "UPDATE profiles SET name = ?, colour = ?, "
                            "created_at = COALESCE(?, created_at) WHERE id = ?",
                            (person.name, _backup_colour(person).value, created, here_id),
                        )
                for person in extra:
                    created = _utc(person.created_at or when)
                    cursor = await conn.execute(
                        "INSERT INTO profiles (name, colour, created_at) VALUES (?, ?, ?)",
                        (person.name, _backup_colour(person).value, created),
                    )
                    assert cursor.lastrowid is not None
                    mapping[person.id] = cursor.lastrowid
                    people_added += 1
                if replace and unmatched:
                    gone = json.dumps(sorted(person.id for person in unmatched))
                    cursor = await conn.execute(
                        "DELETE FROM profiles WHERE id IN (SELECT value FROM json_each(?))",
                        (gone,),
                    )
                    people_removed = cursor.rowcount

            rows = await conn.execute_fetchall("SELECT profile_id, show_id FROM tracked_shows")
            tracked = {(row[0], row[1]) for row in rows}
            followed = {show_id for _, show_id in tracked}  # by anyone, before this load
            episodes: dict[int, dict[int, Episode]] = {}
            have: dict[int, set[int]] = {}
            for item in data.shows:
                episodes.setdefault(item.show.id, {}).update((e.id, e) for e in item.episodes)
                have.setdefault(item.show.id, set()).update(item.have)

            shows_removed = 0
            if replace:
                for backup_id, here_id in mapping.items():
                    wanted = json.dumps(
                        sorted({item.show.id for item in data.shows if item.profile == backup_id})
                    )
                    cursor = await conn.execute(
                        "DELETE FROM tracked_shows WHERE profile_id = ? "
                        "AND show_id NOT IN (SELECT value FROM json_each(?))",
                        (here_id, wanted),
                    )
                    shows_removed += cursor.rowcount
            shows_added = 0
            saved: set[int] = set()  # shows (and episodes) written from the backup
            marked: set[int] = set()  # shows whose marks are done
            for item in data.shows:
                local_id = mapping.get(item.profile)
                if local_id is None:
                    continue  # a person the file doesn't list
                show_id = item.show.id
                since = (item.auto_since or when) if item.auto_download else None
                if (local_id, show_id) in tracked:
                    if replace:
                        await conn.execute(
                            "UPDATE tracked_shows SET tracked_at = ?, auto_download = ?, "
                            "auto_since = ? WHERE profile_id = ? AND show_id = ?",
                            (
                                _utc(item.tracked_at),
                                item.auto_download,
                                _opt_utc(since),
                                local_id,
                                show_id,
                            ),
                        )
                else:
                    if show_id not in followed and show_id not in saved:
                        saved.add(show_id)
                        await conn.execute(_SHOW_UPSERT, _show_to_row(item.show))
                        await conn.executemany(
                            _EPISODE_UPSERT,
                            [_episode_to_row(episode) for episode in episodes[show_id].values()],
                        )
                    await conn.execute(
                        "INSERT INTO tracked_shows "
                        "(profile_id, show_id, tracked_at, auto_download, auto_since) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            local_id,
                            show_id,
                            _utc(item.tracked_at),
                            item.auto_download,
                            _opt_utc(since),
                        ),
                    )
                    tracked.add((local_id, show_id))
                    shows_added += 1
                if show_id in marked or (not replace and show_id in followed):
                    continue  # once a show; what is here wins when adding
                marked.add(show_id)
                # Its "I already have this" marks become the backup's.
                await conn.execute(
                    "DELETE FROM have_episodes "
                    "WHERE episode_id IN (SELECT id FROM episodes WHERE show_id = ?)",
                    (show_id,),
                )
                await conn.execute(
                    "INSERT INTO have_episodes (episode_id, marked_at) "
                    "SELECT id, ? FROM episodes "
                    "WHERE show_id = ? AND id IN (SELECT value FROM json_each(?)) "
                    "ON CONFLICT (episode_id) DO NOTHING",
                    (_utc(when), show_id, json.dumps(sorted(have[show_id]))),
                )

            grabs_added = 0
            downloaded_films: set[int] = set()
            for grab in data.grabs:
                record = grab.record
                cursor = await conn.execute(
                    _RESTORE_GRAB_SQL, record.model_dump(mode="json", exclude={"id"})
                )
                if cursor.rowcount <= 0:
                    continue
                grabs_added += 1
                if record.media_type is MediaType.MOVIE:
                    downloaded_films.add(record.media_id)
                if grab.cleared:
                    await conn.execute(
                        "INSERT INTO cleared_downloads (history_id, cleared_at) VALUES (?, ?) "
                        "ON CONFLICT (history_id) DO NOTHING",
                        (cursor.lastrowid, _utc(when)),
                    )
                # As at the v9 upgrade: a restored grab counts as had by its torrent program, so
                # one it no longer has leaves the Downloads page instead of becoming a problem.
                await conn.execute(
                    "INSERT INTO seen_downloads (history_id, seen_at) VALUES (?, ?) "
                    "ON CONFLICT (history_id) DO NOTHING",
                    (cursor.lastrowid, _utc(when)),
                )
            if downloaded_films:  # as HistoryRepository.add: a downloaded film leaves every list
                await conn.execute(
                    "DELETE FROM watchlist WHERE movie_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(downloaded_films)),),
                )

            rows = await conn.execute_fetchall("SELECT profile_id, movie_id FROM watchlist")
            listed = {(row[0], row[1]) for row in rows}
            films_removed = 0
            if replace:
                for backup_id, here_id in mapping.items():
                    wanted_films = {
                        film.movie.id for film in data.films if film.profile == backup_id
                    }
                    had = {movie_id for person, movie_id in listed if person == here_id}
                    await conn.execute("DELETE FROM watchlist WHERE profile_id = ?", (here_id,))
                    films_removed += len(had - wanted_films)
            films_added = 0
            for film in data.films:
                local_id = mapping.get(film.profile)
                if local_id is None:
                    continue
                cursor = await conn.execute(_RESTORE_FILM_SQL, _film_row(film, local_id))
                if cursor.rowcount > 0 and (local_id, film.movie.id) not in listed:
                    films_added += 1
        return RestoreCounts(
            shows_added=shows_added,
            shows_removed=shows_removed,
            films_added=films_added,
            films_removed=films_removed,
            grabs_added=grabs_added,
            people_added=people_added,
            people_removed=people_removed,
        )


def _opt_utc(instant: datetime | None) -> str | None:
    return _utc(instant) if instant is not None else None
