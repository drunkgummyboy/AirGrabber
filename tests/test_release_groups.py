"""Release groups: learned from the grabs the user kept, blocked from the Downloads page."""

from __future__ import annotations

import unittest
import zlib
from datetime import UTC, datetime, timedelta

from app.models import DownloadRecord, MediaType, SearchKind, SearchRequest, Settings, TorrentRelease
from app.services.picking import group_rank, rank_releases, recommend
from app.services.release_groups import block_group, learned_groups
from app.services.search import passes_settings, release_group
from app.ui.release_panel import recommendation_reason
from app.ui.views.downloads_view import row_menu

START = datetime(2026, 10, 1, tzinfo=UTC)


def grab(group: str, episode: int, day: int, show: int = 1) -> DownloadRecord:
    return DownloadRecord(
        media_type=MediaType.TV,
        media_id=show,
        season=1,
        episode=episode,
        release_title=f"Show.S01E{episode:02d}.1080p.WEB.h264-{group}",
        downloaded_at=START + timedelta(days=day),
    )


def release(group: str, seeders: int = 10) -> TorrentRelease:
    return TorrentRelease(
        title=f"Show.S01E05.1080p.WEB.h264-{group}",
        source="eztv",
        seeders=seeders,
        size_bytes=2 * 1024**3,
        info_hash=f"{zlib.crc32(f'{group} {seeders}'.encode()):040x}",
    )


EPISODE = SearchRequest(kind=SearchKind.EPISODE, title="Show", season=1, episode=5)


class ReleaseGroupTests(unittest.TestCase):
    def test_the_group(self) -> None:
        self.assertEqual(release_group("Show.S01E01.1080p.x265-ELiTE EZTV"), "ELiTE")
        self.assertEqual(release_group("Show.S01E01.h264-successfulcrab[EZTVx.to].mkv"), "successfulcrab")
        self.assertIsNone(release_group("Show.S01E01.1080p.WEB-DL"))
        self.assertIsNone(release_group("Show S01E01 1080p WEB-DL x264"))
        self.assertIsNone(release_group("Show.S01E01.1080p-x264"))
        self.assertIsNone(release_group("Show 2026-1080p"))
        self.assertIsNone(release_group("Show S01E01"))


class LearnedTests(unittest.TestCase):
    def test_three_kept_grabs_make_a_group_trusted(self) -> None:
        grabs = [grab("NTb", 1, 1), grab("NTb", 2, 2)]
        self.assertEqual(learned_groups(grabs), ())
        grabs.append(grab("ntb", 3, 3))
        self.assertEqual(learned_groups(grabs), ("ntb",))  # spelled as its newest grab

    def test_a_grab_fetched_again_counts_against_its_group(self) -> None:
        grabs = [grab("FAKE", 1, 1), grab("FAKE", 2, 2), grab("FAKE", 3, 3), grab("FAKE", 4, 4)]
        grabs.append(grab("NTb", 1, 5))  # FAKE's episode 1 was fetched again
        self.assertEqual(learned_groups(grabs), ("FAKE",))  # 3 kept, 1 redone: still 3 per redo
        grabs.append(grab("NTb", 2, 6))
        self.assertEqual(learned_groups(grabs), ())
        # The later grab is the one kept: an older grab of the same episode doesn't count against it.
        grabs += [grab("NTb", 3, 7), grab("NTb", 4, 8)]
        self.assertEqual(learned_groups(grabs), ("NTb",))

    def test_a_blocked_group_is_never_learned(self) -> None:
        grabs = [grab("FLUX", n, n) for n in range(1, 5)]
        self.assertEqual(learned_groups(grabs, ("flux",)), ())

    def test_titles_without_a_group_are_skipped(self) -> None:
        grabs = [
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=1,
                season=1,
                episode=n,
                release_title=f"Show S01E0{n} 1080p WEB-DL",
                downloaded_at=START,
            )
            for n in range(1, 5)
        ]
        self.assertEqual(learned_groups(grabs), ())


class PickingTests(unittest.TestCase):
    def test_preferred_then_learned_then_the_rest(self) -> None:
        settings = Settings(preferred_groups="ELiTE", learned_groups=("NTb",))
        self.assertEqual(group_rank(release("ELiTE"), settings), 0)
        self.assertEqual(group_rank(release("ntb"), settings), 1)
        self.assertEqual(group_rank(release("OTHER"), settings), 2)
        ranked = rank_releases(
            [release("OTHER", 90), release("NTb", 20), release("ELiTE", 5)], EPISODE, settings
        )
        self.assertEqual([release_group(r.title) for r in ranked], ["ELiTE", "NTb", "OTHER"])

    def test_the_recommendation_says_it(self) -> None:
        settings = Settings(learned_groups=("NTb",))
        picked = recommend([release("OTHER", 90), release("NTb", 20)], EPISODE, settings)
        assert picked is not None
        self.assertTrue(picked.learned)
        self.assertFalse(picked.preferred)
        self.assertIn("a group that worked for you before", recommendation_reason(picked))

    def test_a_blocked_group_is_never_shown_or_picked(self) -> None:
        settings = Settings(blocked_groups="fake")
        self.assertFalse(passes_settings(release("FAKE"), settings))
        self.assertTrue(passes_settings(release("NTb"), settings))
        ranked = rank_releases([release("FAKE", 90), release("NTb", 2)], EPISODE, settings)
        self.assertEqual([release_group(r.title) for r in ranked], ["NTb"])


class FakeSettings:
    def __init__(self, settings: Settings) -> None:
        self.current = settings

    async def update(self, **changes: object) -> Settings:
        self.current = Settings.model_validate({**self.current.model_dump(), **changes})
        return self.current


class BlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_takes_it_off_the_other_lists(self) -> None:
        settings = FakeSettings(
            Settings(preferred_groups="FAKE, ELiTE", learned_groups=("fake", "NTb"), blocked_groups="OLD")
        )
        await block_group(settings, "FAKE")  # type: ignore[arg-type]
        await block_group(settings, "fake")  # type: ignore[arg-type]
        self.assertEqual(settings.current.blocked_groups, ("old", "FAKE"))
        self.assertEqual(settings.current.preferred_groups, ("elite",))
        self.assertEqual(settings.current.learned_groups, ("NTb",))


class MenuTests(unittest.TestCase):
    def test_the_menu_offers_the_block(self) -> None:
        from app.models import DownloadEntry, DownloadState
        from app.services.download_status import Located

        record = grab("FAKE", 1, 1)
        entry = DownloadEntry(record=record, title="Show", state=DownloadState.READY, progress=1.0)
        blocked: list[bool] = []
        items = row_menu(
            entry,
            Located(folder=None, problem="no_folder"),
            open_folder=lambda _folder: None,
            again=None,
            remove=lambda: None,
            block=("FAKE", lambda: blocked.append(True)),
        )
        [item] = [i for i in items if getattr(i, "label", "").startswith("🚫")]
        self.assertEqual(item.label, "🚫 Wrong or fake? Block FAKE…")
        assert item.action is not None
        item.action()
        self.assertEqual(blocked, [True])
        plain = row_menu(
            entry,
            Located(folder=None, problem="no_folder"),
            open_folder=lambda _folder: None,
            again=None,
            remove=lambda: None,
        )
        self.assertFalse([i for i in plain if getattr(i, "label", "").startswith("🚫")])


if __name__ == "__main__":
    unittest.main()
