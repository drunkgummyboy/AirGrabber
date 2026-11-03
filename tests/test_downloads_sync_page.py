"""The Downloads page in sync with the torrent program (3.0.2), its widgets on a hidden Tk root:
the rows of a task added in the program ("Added in Download Station", a file glyph or the
recognised poster), "Live from …", the ⋯ menu per kind of row, the two questions, the optimistic
remove and its rollback, and 300 tasks updated in place."""

from __future__ import annotations

import asyncio
import gc
import time
import tkinter as tk
import unittest
from datetime import timedelta
from typing import Any

import customtkinter as ctk

from app.models import (
    DownloadEntry,
    DownloadsOverview,
    DownloadState,
    ListedDownload,
    Recognised,
    RemoteTask,
    SubtitleOffer,
    TaskEntry,
)
from app.services.download_status import Located
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.subtitle_control import SubtitleActions, SubtitleRuns
from app.ui.views.downloads_view import (
    FILE_GLYPH,
    FIRST_ROWS,
    EntryFilter,
    PageActions,
    clear_question,
    live_text,
    remove_question,
    row_key,
    task_meta_text,
    task_title,
)
from tests.test_download_status import FALLOUT, NOW, record
from tests.test_downloads_view import FakeImages, FakeSettings, FakeStatus
from tests.test_downloads_sync import SEVERANCE

PROGRAM = "Download Station"


def remote(
    task_id: str,
    name: str,
    state: DownloadState = DownloadState.DOWNLOADING,
    progress: float = 0.41,
    *,
    hours: float = 1,
    size: int | None = int(2.1 * 1024**3),
) -> RemoteTask:
    return RemoteTask(
        name=name,
        state=state,
        progress=progress,
        task_id=task_id,
        size=size,
        added_at=NOW - timedelta(hours=hours),
    )


def grab(
    history_id: int,
    state: DownloadState = DownloadState.DOWNLOADING,
    *,
    task: RemoteTask | None = None,
    hours: float = 1,
) -> DownloadEntry:
    return DownloadEntry(
        record=record(
            f"Fallout.S02E{history_id:02d}",
            episode=history_id,
            record_id=history_id,
            when=NOW - timedelta(hours=hours),
        ),
        title=f"Fallout S02E{history_id:02d}",
        show=FALLOUT,
        program=PROGRAM,
        state=state,
        progress=task.progress if task is not None else None,
        task=task,
    )


def added(task: RemoteTask, *, recognised: Recognised | None = None, title: str = "") -> TaskEntry:
    return TaskEntry(task=task, title=title or task.name, recognised=recognised)


SEVERANCE_TASK = added(
    remote("dbid_2", "Severance.S02E04.1080p", hours=0.5),
    recognised=Recognised(name="Severance", show=SEVERANCE),
    title="Severance S02E04",
)
ISO_TASK = added(
    remote("dbid_3", "ubuntu-24.04.1-desktop-amd64.iso", DownloadState.STOPPED, 0.63, hours=3,
           size=int(5.7 * 1024**3))
)


def overview(*rows: ListedDownload, **fields: Any) -> DownloadsOverview:
    entries = tuple(row for row in rows if isinstance(row, DownloadEntry))
    tasks = tuple(row for row in rows if isinstance(row, TaskEntry))
    values: dict[str, Any] = {"program": PROGRAM, "synced": True, "checked_at": NOW}
    values.update(fields)
    return DownloadsOverview(entries=entries, tasks=tasks, **values)


class TextTests(unittest.TestCase):
    def test_texts(self) -> None:
        self.assertEqual(task_meta_text(SEVERANCE_TASK), "Recognised as Severance · 2.1 GB")
        self.assertEqual(task_meta_text(ISO_TASK), "5.7 GB")
        long = added(remote("x", "A" * 100))
        self.assertEqual(len(task_title(long)), 64)
        self.assertTrue(task_title(long).endswith("…"))
        rows = overview(grab(1), SEVERANCE_TASK, ISO_TASK)
        self.assertEqual(live_text(rows), "Live from Download Station · 3 downloads")
        self.assertEqual(live_text(overview(grab(1))), "Live from Download Station · 1 download")
        self.assertEqual(live_text(overview(grab(1), problem="Can't reach the NAS.")), "")
        self.assertEqual(live_text(overview(grab(1), synced=False)), "")
        self.assertEqual(
            clear_question(6, PROGRAM),
            "6 finished downloads are removed from Download Station and from this list. Their "
            "files stay where they are.",
        )
        self.assertEqual(row_key(grab(4)), ("grab", 4))
        self.assertEqual(row_key(ISO_TASK), ("task", "dbid_3"))

    def test_the_remove_question_says_what_stays(self) -> None:
        ready = grab(8, DownloadState.READY, task=remote("dbid_8", "a", DownloadState.READY, 1.0))
        nas = overview(ready, removes_partial=True)
        self.assertEqual(
            remove_question(ready, nas),
            "Fallout S02E08 is removed from Download Station and from this list. What it "
            "downloaded so far stays on the NAS.",
        )
        # An unfinished Download Station task: its partial download goes too, and it says so.
        self.assertEqual(
            remove_question(ISO_TASK, nas),
            "ubuntu-24.04.1-desktop-amd64.iso is removed from Download Station and from this "
            "list. Its unfinished download is removed too; finished files stay.",
        )
        here = overview(ISO_TASK, program="qBittorrent", on_this_pc=True)
        self.assertEqual(
            remove_question(ISO_TASK, here),
            "ubuntu-24.04.1-desktop-amd64.iso is removed from qBittorrent and from this list. "
            "What it downloaded so far stays on this PC.",
        )


class FakeSyncStatus(FakeStatus):
    """FakeStatus with the actions: each publishes its change at once, waits for `gate`, and
    on `failure` publishes the overview before it again and returns the reason."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any]] = []
        self.failure: str | None = None
        self.gate = asyncio.Event()
        self.gate.set()

    async def _act(self, name: str, item: Any, changed: DownloadsOverview) -> str | None:
        self.calls.append((name, item))
        before = self.latest
        self.publish(changed)
        await self.gate.wait()
        if self.failure is not None and before is not None:
            self.publish(before)
            return self.failure
        return None

    def _without(self, keys: set[Any]) -> DownloadsOverview:
        assert self.latest is not None
        return overview(*(row for row in self.latest.rows if row_key(row) not in keys))

    async def remove(self, item: ListedDownload) -> str | None:
        return await self._act("remove", item, self._without({row_key(item)}))

    async def pause(self, item: ListedDownload) -> str | None:
        assert self.latest is not None
        return await self._act("pause", item, self.latest)

    async def resume(self, item: ListedDownload) -> str | None:
        assert self.latest is not None
        return await self._act("resume", item, self.latest)

    async def clear_finished(self) -> str | None:
        assert self.latest is not None
        ready = {row_key(row) for row in self.latest.rows if row.state is DownloadState.READY}
        return await self._act("clear_finished", None, self._without(ready))

    async def locate(self, item: ListedDownload) -> Located:
        return Located(None, "not_found")

    def open_folder(self, folder: str) -> bool:
        return True


class SyncPageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 5.0  # building widgets is slow
        self.status = FakeSyncStatus()
        self.opened: list[Any] = []

    def view(self) -> Any:
        from app.ui.views.downloads_view import DownloadsView

        subtitles = SubtitleActions(
            offer=lambda: SubtitleOffer.ON,
            languages=lambda: ("en",),
            fetch=lambda key, languages: None,
            runs=SubtitleRuns(),
        )
        page = DownloadsView(
            self.root,
            downloads=self.status,  # type: ignore[arg-type]
            settings=FakeSettings(),  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            on_open_show=self.opened.append,
            actions=PageActions(again=lambda e, auto: None, subtitles=subtitles),
        )
        page.pack(fill="both", expand=True)
        self.addCleanup(lambda: page.destroy() if page.winfo_exists() else None)
        return page

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0.01)
            self.root.update()

    def row(self, page: Any, key: Any) -> Any:
        return page._by_key[key]

    async def test_rows_tag_glyph_poster_and_the_live_line(self) -> None:
        page = self.view()
        self.status.publish(overview(grab(1, task=remote("dbid_1", "x")), SEVERANCE_TASK, ISO_TASK))
        await self.settle()
        self.assertEqual(
            [row_key(row.entry) for row in page._rows],
            [("task", "dbid_2"), ("grab", 1), ("task", "dbid_3")],  # added 30 min, 1 h, 3 h ago
        )
        self.assertEqual(page.live.cget("text"), "Live from Download Station · 3 downloads")
        self.assertTrue(page.live.winfo_manager())
        self.assertEqual(page.clear_button.cget("text"), "✓ Clear finished…")
        severance, fallout, iso = page._rows
        self.assertIsNone(fallout._tag)
        self.assertIn("sent by AirGrabber", fallout._meta.cget("text"))
        assert severance._tag is not None and iso._tag is not None
        self.assertEqual(severance._tag.cget("text"), " Added in Download Station ")
        self.assertEqual(severance._title.cget("text"), "Severance S02E04")
        self.assertEqual(severance._poster_url, SEVERANCE.poster_url)
        self.assertIsNone(severance._glyph)
        self.assertEqual(iso._title.cget("text"), "ubuntu-24.04.1-desktop-amd64.iso")
        assert iso._glyph is not None
        self.assertEqual((iso._glyph.cget("text"), bool(iso._glyph.winfo_manager())),
                         (FILE_GLYPH, True))
        self.assertEqual(iso._state.cget("text"), "Paused at 63 %")
        self.assertIsNone(iso._poster_url)
        # A recognised task opens its show; one that isn't, nothing.
        severance._clicked()
        iso._clicked()
        self.assertEqual(self.opened, [SEVERANCE])

    async def test_the_menu_per_kind_of_row(self) -> None:
        page = self.view()
        paused = remote("dbid_5", "y", DownloadState.STOPPED, 0.3)
        self.status.publish(overview(
            grab(1, task=remote("dbid_1", "x")),
            grab(5, DownloadState.STOPPED, task=paused, hours=2),
            grab(6, DownloadState.READY, task=remote("dbid_6", "z", DownloadState.READY, 1.0), hours=3),
            grab(7, DownloadState.PENDING, hours=0.1),
            SEVERANCE_TASK,
        ))
        await self.settle()
        nowhere = Located(None, "not_found")

        def labels(key: Any) -> list[str]:
            row = self.row(page, key)
            return [item.label if not item.separator else "—"
                    for item in page.menu_items(row, row.entry, nowhere)]

        remove = "🗑 Remove from Download Station…"
        self.assertEqual(
            labels(("grab", 1)),
            ["Download again…", "⏸ Pause", "Open the folder", "Get subtitles", "—", remove],
        )
        self.assertEqual(labels(("grab", 5))[1], "▶ Resume")
        self.assertEqual(
            labels(("grab", 6)), ["Download again…", "Open the folder", "Get subtitles", "—", remove]
        )
        # Waiting for the program: nothing to remove there, so off the list as before.
        self.assertEqual(
            labels(("grab", 7)),
            ["Download again…", "Open the folder", "Get subtitles", "—", "Remove from list"],
        )
        self.assertEqual(labels(("task", "dbid_2")), ["⏸ Pause", "Open the folder", "—", remove])
        row = self.row(page, ("task", "dbid_2"))
        items = page.menu_items(row, row.entry, nowhere)
        self.assertTrue(items[-1].danger)
        items[0].action()  # ⏸ Pause
        await self.settle()
        self.assertEqual(self.status.calls, [("pause", SEVERANCE_TASK)])

    async def test_remove_asks_then_the_row_goes_at_once_and_comes_back_on_a_failure(self) -> None:
        page = self.view()
        self.status.publish(overview(grab(1, task=remote("dbid_1", "x")), ISO_TASK,
                                     removes_partial=True))
        await self.settle()
        row = self.row(page, ("task", "dbid_3"))
        items = page.menu_items(row, row.entry, Located(None, "not_found"))
        items[-1].action()  # 🗑 Remove from Download Station…
        dialog = page.dialog
        assert isinstance(dialog, ConfirmDialog)
        self.assertEqual(dialog.title(), "Remove from Download Station?")
        self.assertIn("Its unfinished download is removed too", dialog.text.cget("text"))
        self.assertEqual(dialog.confirm_button.cget("text"), "Remove")
        self.status.gate.clear()
        self.status.failure = "Can't reach the NAS at 192.168.1.20:5000."
        dialog.confirm_button.invoke()
        await self.settle()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual([row_key(r.entry) for r in page._rows], [("grab", 1)])  # at once
        self.status.gate.set()
        await self.settle()
        self.assertEqual([row_key(r.entry) for r in page._rows], [("grab", 1), ("task", "dbid_3")])
        self.assertEqual(page._note.cget("text"), "⚠ Can't reach the NAS at 192.168.1.20:5000.")
        # Cancel does nothing.
        items[-1].action()
        assert page.dialog is not None
        page.dialog.cancel_button.invoke()
        await self.settle()
        self.assertEqual(len(self.status.calls), 1)

    async def test_clear_finished_asks_with_the_count(self) -> None:
        page = self.view()
        self.status.publish(overview(
            grab(1, task=remote("dbid_1", "x")),
            grab(6, DownloadState.READY, task=remote("dbid_6", "z", DownloadState.READY, 1.0)),
            added(remote("dbid_9", "Seeding.Thing", DownloadState.READY, 1.0, hours=5)),
            ISO_TASK,
        ))
        await self.settle()
        page._clear_finished()
        dialog = page.dialog
        assert isinstance(dialog, ConfirmDialog)
        self.assertEqual(dialog.title(), "Clear finished downloads?")
        self.assertEqual(
            dialog.text.cget("text"),
            "2 finished downloads are removed from Download Station and from this list. Their "
            "files stay where they are.",
        )
        self.assertEqual(dialog.confirm_button.cget("text"), "Clear 2")
        dialog.confirm_button.invoke()
        await self.settle()
        self.assertEqual(self.status.calls, [("clear_finished", None)])
        self.assertEqual(
            [row_key(r.entry) for r in page._rows], [("grab", 1), ("task", "dbid_3")]
        )
        self.assertEqual(str(page.clear_button.cget("state")), "disabled")

    async def test_300_tasks_are_updated_in_place(self) -> None:
        page = self.view()
        tasks = [
            added(remote(f"dbid_{n}", f"Some.Thing.{n:03d}.1080p", DownloadState.READY, 1.0,
                         hours=n))
            for n in range(1, 301)
        ]
        steps: list[float] = []
        build_batch = page._build_batch

        def timed_batch() -> None:
            step = time.perf_counter()
            build_batch()
            self.root.update_idletasks()  # their layout and drawing count too
            steps.append(time.perf_counter() - step)

        page._build_batch = timed_batch
        started = time.perf_counter()
        self.status.publish(overview(*tasks))
        self.root.update_idletasks()
        first = time.perf_counter() - started  # the first screenful, at once
        self.assertEqual(len(page._rows), FIRST_ROWS)
        self.assertTrue(page.building)
        while page.building:  # the rest a few at a time, the loop turning in between
            self.root.update()
            await asyncio.sleep(0.005)
        built = time.perf_counter() - started
        longest = max(steps)
        await self.settle()
        rows = list(page._rows)
        self.assertEqual(len(rows), 300)
        self.assertEqual([row.grid_row for row in rows], list(range(1, 301)))
        # A check later: a few downloading, one more on top. Every row stays; one is new.
        changed = [
            added(remote(f"dbid_{n}", f"Some.Thing.{n:03d}.1080p", progress=0.5, hours=n))
            if n % 50 == 0 else task
            for n, task in enumerate(tasks, start=1)
        ]
        newest = added(remote("dbid_0", "Brand.New.Thing", hours=0))
        started = time.perf_counter()
        self.status.publish(overview(newest, *changed))
        updated = time.perf_counter() - started
        await self.settle()
        self.assertEqual(page._rows[1:], rows)
        self.assertEqual(page._rows[50]._state.cget("text"), "Downloading 50 %")
        started = time.perf_counter()
        self.status.publish(overview(newest, *changed))  # nothing changed
        same = time.perf_counter() - started
        # The filter hides rows and keeps them: back to All, nothing is made again.
        page.set_filter(EntryFilter.DOWNLOADING)
        await self.settle()
        self.assertEqual(len(page._rows), 7)  # the six now downloading and the new one
        started = time.perf_counter()
        page.set_filter(EntryFilter.ALL)
        self.root.update_idletasks()
        back = time.perf_counter() - started
        self.assertEqual(page._rows[1:], rows)
        self.assertFalse(page.building)
        # In place: a check (changed or not) and the filter switch are far cheaper than building.
        self.assertLess(max(updated, same, back), built / 5)


if __name__ == "__main__":
    unittest.main()
