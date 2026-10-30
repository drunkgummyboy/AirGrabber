"""Subtitles in 2.0.7: the settings (When / Hearing impaired / For), the pick rule that follows
them, and a request by hand for one episode or film (`SubtitleService.fetch_now`)."""

from __future__ import annotations

import unittest
from datetime import timedelta

from app.models import (
    HearingImpaired,
    MediaType,
    Settings,
    SubtitleFetchOutcome,
    SubtitleOffer,
    SubtitlesFor,
    SubtitlesMode,
    SubtitlesSaved,
)
from app.services.subtitles import NO_FOLDER_TEXT, pick, quota_text
from tests.test_subtitles import (
    FILM,
    FOLDER_NAME,
    PACK,
    RESET,
    SubtitleRunTestCase,
    candidate,
)


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = Settings()
        self.assertIs(settings.subtitles_mode, SubtitlesMode.AUTO)
        self.assertIs(settings.subtitles_hearing_impaired, HearingImpaired.AVOID)
        self.assertIs(settings.subtitles_for, SubtitlesFor.BOTH)

    def test_stored_values_and_unknown_ones(self) -> None:
        stored = Settings.model_validate(
            {"subtitles_mode": "ask", "subtitles_hearing_impaired": "PREFER", "subtitles_for": "films"}
        )
        self.assertIs(stored.subtitles_mode, SubtitlesMode.ASK)
        self.assertIs(stored.subtitles_hearing_impaired, HearingImpaired.PREFER)
        self.assertIs(stored.subtitles_for, SubtitlesFor.FILMS)
        newer = Settings.model_validate(
            {"subtitles_mode": "hourly", "subtitles_hearing_impaired": "x", "subtitles_for": "anime"}
        )
        self.assertEqual(
            (newer.subtitles_mode, newer.subtitles_hearing_impaired, newer.subtitles_for),
            (SubtitlesMode.AUTO, HearingImpaired.AVOID, SubtitlesFor.BOTH),
        )
        dumped = stored.model_dump(mode="json")
        self.assertEqual(
            (dumped["subtitles_mode"], dumped["subtitles_hearing_impaired"], dumped["subtitles_for"]),
            ("ask", "prefer", "films"),
        )


class SettingsChoiceTests(unittest.TestCase):
    """Settings → Subtitles' three boxes: names shown, values saved."""

    def test_names_round_trip(self) -> None:
        from app.ui.settings_dialog import (
            HEARING_NAMES,
            SUBTITLES_FOR_NAMES,
            SUBTITLES_MODE_NAMES,
            choice_from_name,
            choice_name,
        )

        for mode in SUBTITLES_MODE_NAMES:
            name = choice_name(SUBTITLES_MODE_NAMES, mode)
            self.assertIs(choice_from_name(SUBTITLES_MODE_NAMES, name, SubtitlesMode.AUTO), mode)
        for hearing in HEARING_NAMES:
            name = choice_name(HEARING_NAMES, hearing)
            self.assertIs(choice_from_name(HEARING_NAMES, name, HearingImpaired.AVOID), hearing)
        for kind in SUBTITLES_FOR_NAMES:
            name = choice_name(SUBTITLES_FOR_NAMES, kind)
            self.assertIs(choice_from_name(SUBTITLES_FOR_NAMES, name, SubtitlesFor.BOTH), kind)
        self.assertIs(choice_from_name(HEARING_NAMES, "?", HearingImpaired.AVOID), HearingImpaired.AVOID)
        self.assertEqual(choice_name(SUBTITLES_MODE_NAMES, SubtitlesMode.ASK), "Only when I ask")
        self.assertEqual(list(HEARING_NAMES.values()), ["Avoid", "Prefer", "Either"])

    def test_segment_width_fits_the_names(self) -> None:
        from app.ui.settings_dialog import segment_width

        self.assertEqual(segment_width(["a", "b"], lambda text: 10.0), 260)
        self.assertEqual(segment_width(["x" * 30, "y" * 30], lambda text: 6.0 * len(text)), 2 * (180 + 36))


class HearingImpairedPickTests(unittest.TestCase):
    FOUND = [
        candidate(1, "en", download_count=900, hearing_impaired=True),
        candidate(2, "en", download_count=500),
        candidate(3, "nl", download_count=10, hearing_impaired=True),
    ]

    def test_avoid_is_the_default(self) -> None:
        self.assertEqual(pick(self.FOUND, "en").file_id, 2)  # type: ignore[union-attr]
        self.assertEqual(pick(self.FOUND, "en", HearingImpaired.AVOID).file_id, 2)  # type: ignore[union-attr]

    def test_prefer(self) -> None:
        self.assertEqual(pick(self.FOUND, "en", HearingImpaired.PREFER).file_id, 1)  # type: ignore[union-attr]

    def test_either_takes_the_most_downloaded(self) -> None:
        either = [candidate(1, "en", download_count=400, hearing_impaired=True), candidate(2, "en", download_count=500)]
        self.assertEqual(pick(either, "en", HearingImpaired.EITHER).file_id, 2)  # type: ignore[union-attr]
        self.assertEqual(pick(self.FOUND, "en", HearingImpaired.EITHER).file_id, 1)  # type: ignore[union-attr]

    def test_a_hash_match_still_wins(self) -> None:
        found = [*self.FOUND, candidate(9, "en", moviehash_match=True)]
        self.assertEqual(pick(found, "en", HearingImpaired.PREFER).file_id, 9)  # type: ignore[union-attr]

    def test_the_only_one_in_a_language_is_taken_anyway(self) -> None:
        self.assertEqual(pick(self.FOUND, "nl", HearingImpaired.AVOID).file_id, 3)  # type: ignore[union-attr]


class RunSettingsTests(SubtitleRunTestCase):
    async def test_prefer_hearing_impaired_in_the_runs(self) -> None:
        await self.settings.update(
            subtitle_languages=("en",), subtitles_hearing_impaired=HearingImpaired.PREFER
        )
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)  # no hash
        await self.service.run_once()
        self.assertEqual(video.with_name("severance.s02e04.en.srt").read_bytes(), b"subtitle 3")

    async def test_only_when_asked_fetches_nothing_but_notes_what_is_there(self) -> None:
        await self.settings.update(subtitles_mode=SubtitlesMode.ASK)
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        video.with_name("severance.s02e04.en.srt").write_bytes(b"mine")
        report = await self.service.run_once()
        self.assertEqual((self.client.searches, self.client.downloads), ([], []))
        self.assertEqual(self.client.sessions, [])  # nothing to sign in for
        self.assertIsNone(report.problem)
        self.assertEqual(await self.service.show_languages(7), {(2, 4): frozenset({"en"})})
        # Asked by hand, it fetches.
        result = await self.service.fetch_now(MediaType.TV, 7, 2, 4, ("nl",), force=True)
        self.assertIs(result.outcome, SubtitleFetchOutcome.SAVED)
        # Back to automatic: the run carries on where it is.
        await self.settings.update(subtitles_mode=SubtitlesMode.AUTO)
        self.client.searches.clear()
        await self.service.run_once()
        self.assertEqual(self.client.searches, [])  # complete now: EN there, NL saved

    async def test_for_only_films_leaves_episodes_alone(self) -> None:
        await self.settings.update(subtitle_languages=("en",), subtitles_for=SubtitlesFor.FILMS)
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)
        await self.grab(FILM, media_type=MediaType.MOVIE, media_id=9)
        film = self.video("Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv", size=1000)
        report = await self.service.run_once()
        self.assertEqual(report.saved, ("Dune Part Two (2024) (EN)",))
        self.assertTrue(film.with_name("Dune Part Two (2024).en.srt").exists())
        self.assertEqual(await self.service.show_languages(7), {})

    async def test_for_only_series_leaves_films_alone(self) -> None:
        await self.settings.update(subtitle_languages=("en",), subtitles_for=SubtitlesFor.SERIES)
        await self.grab(FILM, media_type=MediaType.MOVIE, media_id=9)
        self.video("Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv", size=1000)
        await self.service.run_once()
        self.assertEqual(self.client.searches, [])

    async def test_a_new_setting_wakes_the_runs(self) -> None:
        await self.settings.update(subtitles_for=SubtitlesFor.FILMS)
        self.assertTrue(self.service._wake.is_set())


class FetchNowTests(SubtitleRunTestCase):
    """A request by hand for one episode or film: every outcome."""

    async def fetch(self, *languages: str, force: bool = True, **target: object) -> object:
        media_type = target.get("media_type", MediaType.TV)
        media_id = target.get("media_id", 7)
        season = target.get("season", 2)
        episode = target.get("episode", 4)
        return await self.service.fetch_now(
            media_type,  # type: ignore[arg-type]
            media_id,  # type: ignore[arg-type]
            season,  # type: ignore[arg-type]
            episode,  # type: ignore[arg-type]
            languages or ("en", "nl"),
            force=force,
        )

    async def test_saved(self) -> None:
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        result = await self.fetch()
        self.assertIs(result.outcome, SubtitleFetchOutcome.SAVED)  # type: ignore[attr-defined]
        self.assertEqual(result.saved, ("en", "nl"))  # type: ignore[attr-defined]
        self.assertTrue(video.with_name("severance.s02e04.nl.srt").exists())
        self.assertIn(SubtitlesSaved(label="Severance S02E04", languages=("en", "nl")), self.events)
        self.assertEqual(self.client.sessions, [("beau", "pw")])

    async def test_one_language_only(self) -> None:
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        result = await self.fetch("nl")
        self.assertEqual(result.saved, ("nl",))  # type: ignore[attr-defined]
        self.assertFalse(video.with_name("severance.s02e04.en.srt").exists())

    async def test_already_there(self) -> None:
        await self.grab()
        video = self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        video.with_name("severance.s02e04.en.srt").write_bytes(b"mine")
        result = await self.fetch("en")
        self.assertIs(result.outcome, SubtitleFetchOutcome.ALREADY)  # type: ignore[attr-defined]
        self.assertEqual(self.client.searches, [])
        # Noted, so the marks show it.
        self.assertEqual(self.events, [SubtitlesSaved(label="Severance S02E04", languages=("en",))])
        self.assertEqual(await self.service.show_languages(7), {(2, 4): frozenset({"en"})})

    async def test_partly(self) -> None:
        self.client.answers = [[], [candidate(4, "nl")]]  # the hash finds nothing, then only NL
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        result = await self.fetch()
        self.assertIs(result.outcome, SubtitleFetchOutcome.PARTLY)  # type: ignore[attr-defined]
        self.assertEqual((result.saved, result.not_found), (("nl",), ("en",)))  # type: ignore[attr-defined]

    async def test_not_found_counts_a_try_and_force_tries_again_at_once(self) -> None:
        self.client.answers = []
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        self.assertIs((await self.fetch("en")).outcome, SubtitleFetchOutcome.NOT_FOUND)  # type: ignore[attr-defined]
        searches = len(self.client.searches)
        # Within the retry time: unforced waits, forced asks again.
        self.assertIs(
            (await self.fetch("en", force=False)).outcome,  # type: ignore[attr-defined]
            SubtitleFetchOutcome.WAIT,
        )
        self.assertEqual(len(self.client.searches), searches)
        self.assertIs((await self.fetch("en")).outcome, SubtitleFetchOutcome.NOT_FOUND)  # type: ignore[attr-defined]
        self.assertGreater(len(self.client.searches), searches)

    async def test_no_video_and_still_downloading(self) -> None:
        await self.grab()
        self.assertIs((await self.fetch()).outcome, SubtitleFetchOutcome.NO_VIDEO)  # type: ignore[attr-defined]
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", recent=True)
        self.assertIs((await self.fetch()).outcome, SubtitleFetchOutcome.DOWNLOADING)  # type: ignore[attr-defined]
        self.assertEqual(self.client.searches, [])

    async def test_no_grab(self) -> None:
        self.assertIs((await self.fetch()).outcome, SubtitleFetchOutcome.NO_GRAB)  # type: ignore[attr-defined]

    async def test_the_days_quota(self) -> None:
        self.client.quota_error = True
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv", size=1000)
        result = await self.fetch("en")
        self.assertIs(result.outcome, SubtitleFetchOutcome.QUOTA)  # type: ignore[attr-defined]
        self.assertEqual(result.message, quota_text(RESET))  # type: ignore[attr-defined]
        # Used up: the next request doesn't ask OpenSubtitles before the reset.
        searches = len(self.client.searches)
        self.assertIs((await self.fetch("nl")).outcome, SubtitleFetchOutcome.QUOTA)  # type: ignore[attr-defined]
        self.assertEqual(len(self.client.searches), searches)
        self.now = RESET + timedelta(minutes=1)
        self.client.quota_error = False
        self.assertIs((await self.fetch("en")).outcome, SubtitleFetchOutcome.SAVED)  # type: ignore[attr-defined]

    async def test_problems_in_plain_words(self) -> None:
        self.client.login_error = True
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        result = await self.fetch()
        self.assertIs(result.outcome, SubtitleFetchOutcome.PROBLEM)  # type: ignore[attr-defined]
        self.assertTrue(result.message)  # type: ignore[attr-defined]
        missing = str(self.tmp / "nas-offline")
        await self.settings.update(subtitles_folder=missing)
        self.client.login_error = False
        result = await self.fetch()
        self.assertIs(result.outcome, SubtitleFetchOutcome.PROBLEM)  # type: ignore[attr-defined]
        self.assertIn("nas-offline", result.message)  # type: ignore[attr-defined]

    async def test_unavailable(self) -> None:
        await self.settings.update(subtitles_folder="")
        result = await self.fetch()
        self.assertEqual(
            (result.outcome, result.message),  # type: ignore[attr-defined]
            (SubtitleFetchOutcome.UNAVAILABLE, NO_FOLDER_TEXT),
        )
        self.assertFalse(self.service.can_fetch)
        await self.settings.update(subtitles_folder=str(self.downloads))
        self.assertTrue(self.service.can_fetch)
        self.client.configured = False
        self.assertFalse(self.service.can_fetch)
        self.assertIs((await self.fetch()).outcome, SubtitleFetchOutcome.UNAVAILABLE)  # type: ignore[attr-defined]

    async def test_the_switch_is_the_master(self) -> None:
        """Settings' switch off: the CC buttons show, greyed (OFF); without a folder, a
        language or a key there are none (NONE), whatever the switch."""
        self.assertIs(self.service.offer, SubtitleOffer.ON)
        await self.settings.update(subtitles_enabled=False)
        self.assertIs(self.service.offer, SubtitleOffer.OFF)
        self.assertFalse(self.service.can_fetch)
        await self.settings.update(subtitles_folder="")
        self.assertIs(self.service.offer, SubtitleOffer.NONE)
        await self.settings.update(subtitles_folder=str(self.downloads), subtitles_enabled=True)
        self.assertIs(self.service.offer, SubtitleOffer.ON)
        self.client.configured = False
        self.assertIs(self.service.offer, SubtitleOffer.NONE)

    async def test_whatever_the_when_and_for_settings(self) -> None:
        await self.settings.update(
            subtitles_mode=SubtitlesMode.ASK, subtitles_for=SubtitlesFor.FILMS, subtitles_enabled=False
        )
        await self.grab()
        self.video(f"{FOLDER_NAME}/severance.s02e04.mkv")
        self.assertIs((await self.fetch("en")).outcome, SubtitleFetchOutcome.SAVED)  # type: ignore[attr-defined]

    async def test_a_film_and_a_pack_episode(self) -> None:
        await self.grab(FILM, media_type=MediaType.MOVIE, media_id=9)
        film = self.video("Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv", size=1000)
        result = await self.fetch("en", media_type=MediaType.MOVIE, media_id=9, season=None, episode=None)
        self.assertIs(result.outcome, SubtitleFetchOutcome.SAVED)  # type: ignore[attr-defined]
        self.assertTrue(film.with_name("Dune Part Two (2024).en.srt").exists())
        self.assertEqual(await self.service.movie_languages(9), frozenset({"en"}))

        await self.grab(PACK, episode=None)
        self.video(f"{PACK}/Severance.S02E01.mkv", size=1000)
        two = self.video(f"{PACK}/Severance.S02E02.mkv", size=1000)
        result = await self.fetch("en", episode=2)  # one episode of the pack
        self.assertIs(result.outcome, SubtitleFetchOutcome.SAVED)  # type: ignore[attr-defined]
        self.assertTrue(two.with_name("Severance.S02E02.en.srt").exists())
        self.assertFalse((self.downloads / PACK / "Severance.S02E01.en.srt").exists())
        result = await self.fetch("en", episode=None)  # the whole pack (the Downloads page)
        self.assertIs(result.outcome, SubtitleFetchOutcome.SAVED)  # type: ignore[attr-defined]
        self.assertTrue((self.downloads / PACK / "Severance.S02E01.en.srt").exists())
