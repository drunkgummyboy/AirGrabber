"""What "Use this one instead" tells about the two copies (app/forge/core/replace_facts.py): the
library copy found under the download's name, both sizes read in a thread under a time limit (a
fake disk here), the qualities from the names, and the words for it."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from app import i18n
from app.forge.core.queue import ItemStatus, QueueItem
from app.forge.core.replace_facts import (
    ReplaceFacts,
    facts_text,
    place_of,
    read_sizes,
    replace_facts,
    screen_quality,
    size_text,
)
from app.forge.models.media import MediaKind, ParsedMedia
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase

GB = 1024**3
LIBRARY = Path(r"\\NAS\Movies\Dune (2021)\Dune (2021).mkv")


def held(name: str = "Dune.2021.2160p.WEB.mkv", quality: str | None = "2160p H.265") -> QueueItem:
    parsed = ParsedMedia(source_name=name, kind=MediaKind.MOVIE, title="Dune", year=2021,
                         quality=quality)  # fmt: skip
    item = QueueItem(1, Path(r"D:\dl") / name, None, parsed, status=ItemStatus.HELD)
    item.in_library = True
    return item


class FactsTests(ForgeTestCase):
    def test_the_library_copy_and_both_sizes_from_the_disk(self) -> None:
        on_disk = {LIBRARY.with_suffix(".mp4"): 4 * GB, Path(r"D:\dl\Dune.mkv"): 18 * GB}
        facts = read_sizes(
            Path(r"D:\dl\Dune.mkv"), LIBRARY, exists=on_disk.__contains__, size_of=on_disk.get
        )  # another kind of video under the same name counts too
        self.assertEqual(
            facts, ReplaceFacts(library_name="Dune (2021).mp4", library_size=4 * GB, download_size=18 * GB)
        )
        nothing = read_sizes(Path(r"D:\dl\Dune.mkv"), LIBRARY, exists=lambda _p: False,
                             size_of=lambda _p: None)  # fmt: skip
        self.assertEqual(nothing, ReplaceFacts())

    def test_the_real_disk(self) -> None:
        library = self.tmp / "Movies" / "Dune (2021)" / "Dune (2021).mkv"
        library.parent.mkdir(parents=True)
        library.write_bytes(b"x" * 300)
        download = self.tmp / "dl.mkv"
        download.write_bytes(b"x" * 500)
        facts = read_sizes(download, library)
        self.assertEqual((facts.library_name, facts.library_size, facts.download_size),
                         ("Dune (2021).mkv", 300, 500))  # fmt: skip

    def test_the_guard_knows_a_copy_by_its_drive_or_share(self) -> None:
        self.assertEqual(place_of(LIBRARY), place_of(Path(r"\\nas\movies\Heat\Heat.mkv")))
        self.assertNotEqual(place_of(LIBRARY), place_of(Path(r"\\NAS\Series\Show\x.mkv")))
        self.assertEqual(place_of(Path(r"D:\Movies\Dune.mkv")), place_of(Path(r"d:\Films\x.mkv")))

    def test_the_quality_is_the_pictures_height(self) -> None:
        self.assertEqual(screen_quality("2160p H.265"), "2160p")
        self.assertEqual(screen_quality("720P"), "720p")
        self.assertEqual(screen_quality("4k HEVC"), "4K")
        self.assertIsNone(screen_quality("H.264"))
        self.assertIsNone(screen_quality(None))

    def test_the_words(self) -> None:
        both = ReplaceFacts(library_size=round(4.2 * GB), library_quality="1080p",
                            download_size=round(18.6 * GB), download_quality="2160p")  # fmt: skip
        self.assertEqual(
            facts_text(both), "In your library: 1080p · 4.2 GB — this download: 2160p · 18.6 GB"
        )
        self.assertEqual(
            facts_text(ReplaceFacts(library_size=700 * 1024**2, download_quality="720p")),
            "In your library: 700 MB — this download: 720p",
        )  # a part not known is left out
        self.assertEqual(facts_text(ReplaceFacts(library_quality="1080p")), "In your library: 1080p")
        self.assertEqual(facts_text(ReplaceFacts(download_size=GB)), "This download: 1.0 GB")
        self.assertEqual(facts_text(ReplaceFacts(library_name="Dune.mkv")), "")
        i18n.set_language("nl")
        self.addCleanup(i18n.set_language, "en")
        self.assertEqual(size_text(round(4.2 * GB)), "4,2 GB")
        self.assertEqual(
            facts_text(both), "In je bibliotheek: 1080p · 4,2 GB — deze download: 2160p · 18,6 GB"
        )


class ReadInTimeTests(ForgeAsyncTestCase):
    async def test_sizes_through_a_thread_and_qualities_from_the_names(self) -> None:
        threads: list[bool] = []

        def read(download: Path, target: Path) -> ReplaceFacts:
            threads.append(threading.current_thread() is not threading.main_thread())
            return ReplaceFacts(library_name="Dune (2021) 1080p.mkv", library_size=4 * GB,
                                download_size=18 * GB)  # fmt: skip

        names: list[str] = []

        def quality_of(name: str) -> str | None:
            names.append(name)
            return "1080p"

        facts = await replace_facts(held(), LIBRARY, read=read, quality_of=quality_of)
        self.assertEqual(threads, [True])
        self.assertEqual(names, ["Dune (2021) 1080p.mkv"])  # the download's quality was parsed
        self.assertEqual(
            facts,
            ReplaceFacts(
                library_name="Dune (2021) 1080p.mkv",
                library_size=4 * GB,
                library_quality="1080p",
                download_size=18 * GB,
                download_quality="2160p",
            ),
        )

    async def test_a_nas_asleep_leaves_the_sizes_out(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def asleep(download: Path, target: Path) -> ReplaceFacts:
            release.wait(5)
            return ReplaceFacts(library_size=1)

        busy: dict[str, asyncio.Future[Any]] = {}
        with self.assertLogs("app.forge.core.replace_facts", "INFO") as logs:
            facts = await replace_facts(held(), LIBRARY, timeout_s=0.05, read=asleep,
                                        quality_of=lambda _name: None, busy=busy)  # fmt: skip
        self.assertEqual(facts, ReplaceFacts(download_quality="2160p"))
        self.assertNotIn("Dune", "\n".join(logs.output))  # never a path or a name
        self.assertNotIn("NAS", "\n".join(logs.output))
        # Its thread still hangs: no second one under that name, the sizes left out at once.
        calls: list[Path] = []

        def counted(download: Path, target: Path) -> ReplaceFacts:
            calls.append(target)
            return ReplaceFacts(library_size=2)

        again = await replace_facts(held(), LIBRARY, timeout_s=5, read=counted,
                                    quality_of=lambda _name: None, busy=busy)  # fmt: skip
        self.assertEqual((again, calls), (ReplaceFacts(download_quality="2160p"), []))
        same_share = await replace_facts(held(), LIBRARY.parents[1] / "Heat (1995)" / "Heat.mkv",
                                         read=counted, quality_of=lambda _name: None,
                                         busy=busy)  # fmt: skip
        self.assertEqual((same_share.library_size, calls), (None, []))  # one thread per NAS
        self.assertEqual(list(busy), [place_of(LIBRARY)])
        other = await replace_facts(held(), Path(r"E:\Movies\Heat (1995)\Heat.mkv"), read=counted,
                                    quality_of=lambda _name: None, busy=busy)  # fmt: skip
        self.assertEqual(other.library_size, 2)  # another drive isn't held up
        release.set()
        await asyncio.wait_for(asyncio.gather(*busy.values()), 5)
        again = await replace_facts(held(), LIBRARY, read=counted,
                                    quality_of=lambda _name: None, busy=busy)  # fmt: skip
        self.assertEqual(again.library_size, 2)  # once it ended, read again
        self.assertEqual(busy, {})

    async def test_the_downloads_quality_is_what_its_parse_said(self) -> None:
        parsed: list[str] = []

        def quality_of(name: str) -> str | None:
            parsed.append(name)
            return "720p"

        facts = await replace_facts(held(quality=None), LIBRARY, read=lambda _d, _t: ReplaceFacts(),
                                    quality_of=quality_of, busy={})  # fmt: skip
        self.assertEqual((facts.download_quality, parsed), (None, []))  # no parse on the loop

    async def test_a_disk_error_too(self) -> None:
        def broken(download: Path, target: Path) -> ReplaceFacts:
            raise PermissionError(5, "Access is denied", str(target))

        facts = await replace_facts(held(quality=None), LIBRARY, read=broken,
                                    quality_of=lambda _name: None)  # fmt: skip
        self.assertEqual(facts, ReplaceFacts())
