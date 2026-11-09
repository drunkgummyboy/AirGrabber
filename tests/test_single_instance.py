"""Single instance: the named mutex, the "show the window" event and the Forge hand-over mailbox,
under unique test names and temporary folders so a running AirGrabber is never involved."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, cast
from unittest import mock

from app.single_instance import (
    HANDOVER_FOLDER,
    REQUEST_MAX_AGE_S,
    ForgeRequest,
    InstanceLock,
    hand_over,
    handover_folder,
    send_request,
    show_running,
    take_requests,
)


def _unique_names() -> tuple[str, str, str]:
    tag = uuid.uuid4().hex
    base = f"Local\\AirGrabberTest-{tag}"
    return base, f"{base}-show", f"{base}-forge"


REQUEST = ForgeRequest(paths=("C:\\Media\\Alex.S01E01.mkv",), preset="Plex", confirm=True)


class _TempDataDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)

    def files(self) -> list[str]:
        folder = handover_folder(self.data_dir)
        return sorted(os.listdir(folder)) if folder.exists() else []


@unittest.skipUnless(sys.platform == "win32", "named kernel objects are Windows-only")
class InstanceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.names = _unique_names()
        self.first = InstanceLock(*self.names)
        self.second = InstanceLock(*self.names)
        self.addCleanup(self.second.release)
        self.addCleanup(self.first.release)

    def test_the_second_acquire_fails_while_the_first_holds_it(self) -> None:
        self.assertTrue(self.first.acquire())
        self.assertFalse(self.second.acquire())

    def test_a_second_start_asks_the_first_to_show_its_window(self) -> None:
        self.assertTrue(self.first.acquire())
        self.assertFalse(self.first.show_requested())  # nothing asked yet
        self.assertFalse(self.second.acquire())
        self.assertTrue(self.second.signal_show())
        self.assertTrue(self.first.show_requested())
        self.assertFalse(self.first.show_requested())  # auto-reset: one request, one show
        self.assertFalse(self.first.forge_requested())  # showing is no Forge request

    def test_released_lock_can_be_taken_again(self) -> None:
        self.assertTrue(self.first.acquire())
        self.first.release()
        self.assertTrue(self.second.acquire())
        self.first.release()  # twice is fine
        self.assertFalse(self.first.show_requested())  # released: no handles left
        self.assertFalse(self.first.forge_requested())

    def test_a_failed_acquire_does_not_keep_the_lock_alive(self) -> None:
        self.assertTrue(self.first.acquire())
        self.assertFalse(self.second.acquire())
        self.first.release()  # the second start holds no mutex handle of its own
        third = InstanceLock(*self.names)
        self.addCleanup(third.release)
        self.assertTrue(third.acquire())

    def test_other_names_do_not_collide(self) -> None:
        other = InstanceLock(*_unique_names())
        self.addCleanup(other.release)
        self.assertTrue(self.first.acquire())
        self.assertTrue(other.acquire())

    def test_the_default_names_stay(self) -> None:
        lock = InstanceLock()
        self.assertEqual(lock.forge_event_name, "Local\\AirGrabber-forge")

    def test_the_running_copy_answers_a_show_request(self) -> None:
        self.assertTrue(self.first.acquire())
        self.assertFalse(self.second.acquire())
        answered: list[bool] = []

        def running_copy() -> None:  # the running copy's poll, on its own thread here
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.first.show_requested():
                    self.first.answer_show()
                    answered.append(True)
                    return
                time.sleep(0.01)

        worker = threading.Thread(target=running_copy)
        worker.start()
        self.assertTrue(self.second.ask_to_show(answer_s=5))
        worker.join()
        self.assertEqual(answered, [True])

    def test_no_answer_from_a_copy_that_doesnt_poll(self) -> None:
        self.assertTrue(self.first.acquire())
        self.assertFalse(self.second.acquire())
        self.assertFalse(self.second.ask_to_show(answer_s=0.05))
        self.assertTrue(self.first.show_requested())  # the request still waits for it

    def test_an_old_answer_doesnt_count(self) -> None:
        self.assertTrue(self.first.acquire())
        self.first.answer_show()  # nobody waited for this one
        self.assertFalse(self.second.acquire())
        self.assertFalse(self.second.ask_to_show(answer_s=0.05))


class _FakeLock:
    def __init__(self, *, acquired: bool, answers: bool = False) -> None:
        self.acquired = acquired
        self.answers = answers
        self.asked = False
        self.drained = False
        self.released = False

    def acquire(self) -> bool:
        return self.acquired

    def ask_to_show(self, answer_s: float = 5.0) -> bool:
        self.asked = True
        return self.answers

    def show_requested(self) -> bool:
        self.drained = True
        return True

    def release(self) -> None:
        self.released = True


class ShowRunningTests(unittest.TestCase):
    """A plain second start: shown by the running copy, or that copy is waited out."""

    def setUp(self) -> None:
        self.clock = 0.0
        self.made: list[_FakeLock] = []
        self.free_after = 3  # polls until the running copy has let go

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def new_lock(self) -> Any:
        lock = _FakeLock(acquired=len(self.made) + 1 >= self.free_after)
        self.made.append(lock)
        return lock

    def show(self, first: _FakeLock, wait_s: float = 10.0) -> Any:
        return show_running(
            cast(Any, first),
            self.new_lock,
            wait_s=wait_s,
            poll_s=1.0,
            sleep=self.sleep,
            clock=lambda: self.clock,
        )

    def test_an_answer_ends_this_start(self) -> None:
        first = _FakeLock(acquired=False, answers=True)
        self.assertIsNone(self.show(first))
        self.assertTrue(first.asked)
        self.assertTrue(first.released)
        self.assertEqual(self.made, [])

    def test_a_closing_copy_is_waited_out(self) -> None:
        first = _FakeLock(acquired=False)
        lock = self.show(first)
        self.assertIs(lock, self.made[-1])
        self.assertTrue(first.released)
        self.assertTrue(self.made[-1].drained)  # its own unanswered request: nothing to show
        self.assertFalse(self.made[-1].released)  # held: this start is AirGrabber now
        self.assertTrue(all(lock.released for lock in self.made[:-1]))

    def test_a_copy_that_never_lets_go_is_given_up_on(self) -> None:
        self.free_after = 1000
        self.assertIsNone(self.show(_FakeLock(acquired=False), wait_s=5.0))
        self.assertEqual(len(self.made), 5)
        self.assertTrue(all(lock.released for lock in self.made))


class MailboxTests(_TempDataDir):
    def test_a_request_goes_in_and_comes_out_once(self) -> None:
        file = send_request(self.data_dir, REQUEST, event_name=None)
        self.assertEqual(file.parent, self.data_dir / HANDOVER_FOLDER)
        self.assertTrue(file.name.endswith(".json"))
        self.assertEqual(take_requests(self.data_dir), [REQUEST])
        self.assertFalse(file.exists())
        self.assertEqual(take_requests(self.data_dir), [])  # taken: gone

    def test_the_file_holds_the_request(self) -> None:
        file = send_request(self.data_dir, REQUEST, event_name=None, now=1_000.0)
        data = json.loads(file.read_text(encoding="utf-8"))
        self.assertEqual(data["paths"], ["C:\\Media\\Alex.S01E01.mkv"])
        self.assertEqual((data["preset"], data["confirm"], data["forge"]), ("Plex", True, True))
        self.assertEqual(data["created"], 1_000.0)
        self.assertEqual(self.files(), [file.name])  # no .part left behind

    def test_oldest_first(self) -> None:
        now = time.time()
        requests = [ForgeRequest(paths=(f"C:\\Media\\{n}.mkv",)) for n in range(5)]
        # Written out of order: the time in the name decides.
        for offset in (3, 0, 4, 1, 2):
            send_request(self.data_dir, requests[offset], event_name=None, now=now - 10 + offset)
        self.assertEqual(take_requests(self.data_dir, now=now), requests)

    def test_stale_requests_are_dropped_and_deleted(self) -> None:
        now = time.time()
        send_request(self.data_dir, REQUEST, event_name=None, now=now - REQUEST_MAX_AGE_S - 1)
        fresh = ForgeRequest(paths=("C:\\Media\\fresh.mkv",))
        send_request(self.data_dir, fresh, event_name=None, now=now - 5)
        future = ForgeRequest(paths=("C:\\Media\\future.mkv",))  # a clock that went back
        send_request(self.data_dir, future, event_name=None, now=now + REQUEST_MAX_AGE_S + 60)
        self.assertEqual(take_requests(self.data_dir, now=now), [fresh])
        self.assertEqual(self.files(), [])

    def test_unreadable_files_are_dropped(self) -> None:
        folder = handover_folder(self.data_dir)
        folder.mkdir(parents=True)
        (folder / "00000000000000000001-1-a.json").write_text("{not json", encoding="utf-8")
        (folder / "00000000000000000002-1-b.json").write_text(
            json.dumps({"created": time.time(), "paths": "C:\\not-a-list"}), encoding="utf-8"
        )
        (folder / "00000000000000000003-1-c.json").write_text("[1, 2]", encoding="utf-8")
        (folder / "notes.txt").write_text("not ours", encoding="utf-8")
        send_request(self.data_dir, REQUEST, event_name=None)
        with self.assertLogs("app.single_instance", "WARNING") as logs:
            self.assertEqual(take_requests(self.data_dir), [REQUEST])
        self.assertEqual(len(logs.records), 3)
        self.assertEqual(self.files(), ["notes.txt"])  # other files are left alone

    def test_half_written_files_wait_and_old_ones_go(self) -> None:
        folder = handover_folder(self.data_dir)
        folder.mkdir(parents=True)
        writing = folder / "00000000000000000001-1-a.json.part"
        writing.write_text("{", encoding="utf-8")
        crashed = folder / "00000000000000000002-1-b.json.part"
        crashed.write_text("{", encoding="utf-8")
        old = time.time() - REQUEST_MAX_AGE_S - 10
        os.utime(crashed, (old, old))
        self.assertEqual(take_requests(self.data_dir), [])
        self.assertEqual(self.files(), [writing.name])

    def test_no_folder_no_requests(self) -> None:
        self.assertEqual(take_requests(self.data_dir / "missing"), [])

    def test_the_lock_reads_its_data_folders_mailbox(self) -> None:
        send_request(self.data_dir, REQUEST, event_name=None)
        self.assertEqual(InstanceLock(*_unique_names()).take_requests(), [])  # no data folder
        lock = InstanceLock(*_unique_names(), data_dir=self.data_dir)
        self.assertEqual(lock.take_requests(), [REQUEST])

    def test_a_file_that_cannot_be_deleted_yet_waits(self) -> None:
        send_request(self.data_dir, REQUEST, event_name=None)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError):
            self.assertEqual(take_requests(self.data_dir), [])  # never taken twice
        self.assertEqual(take_requests(self.data_dir), [REQUEST])


@unittest.skipUnless(sys.platform == "win32", "named kernel objects are Windows-only")
class ForgeEventTests(_TempDataDir):
    def setUp(self) -> None:
        super().setUp()
        self.names = _unique_names()
        self.running = InstanceLock(*self.names, data_dir=self.data_dir)
        self.addCleanup(self.running.release)
        self.assertTrue(self.running.acquire())

    def test_a_request_wakes_the_running_copy(self) -> None:
        self.assertFalse(self.running.forge_requested())
        send_request(self.data_dir, REQUEST, event_name=self.names[2])
        self.assertTrue(self.running.forge_requested())
        self.assertFalse(self.running.forge_requested())  # auto-reset
        self.assertFalse(self.running.show_requested())  # a Forge request doesn't show the window
        self.assertEqual(self.running.take_requests(), [REQUEST])

    def test_many_requests_one_wake_all_taken(self) -> None:
        for n in range(10):
            send_request(self.data_dir, ForgeRequest(paths=(f"C:\\Media\\{n}.mkv",)), event_name=self.names[2])
        self.assertTrue(self.running.forge_requested())
        self.assertEqual(len(self.running.take_requests()), 10)

    def test_a_file_arriving_without_the_event_is_still_found(self) -> None:
        # An elevated sender can't open the event: only its file arrives.
        send_request(self.data_dir, REQUEST, event_name=f"{self.names[2]}-nobody")
        self.assertFalse(self.running.forge_requested())
        self.assertEqual(self.running.take_requests(), [REQUEST])


@unittest.skipUnless(sys.platform == "win32", "named kernel objects are Windows-only")
class HandOverTests(_TempDataDir):
    def setUp(self) -> None:
        super().setUp()
        self.names = _unique_names()
        self.clock = 0.0
        self.locks: list[InstanceLock] = []

    def new_lock(self) -> InstanceLock:
        lock = InstanceLock(*self.names, data_dir=self.data_dir)
        self.locks.append(lock)
        self.addCleanup(lock.release)
        return lock

    def run_hand_over(self, on_poll: object = None) -> InstanceLock | None:
        def sleep(seconds: float) -> None:
            self.clock += seconds
            if callable(on_poll):
                on_poll()

        return hand_over(
            self.data_dir,
            REQUEST,
            lock_factory=self.new_lock,
            sleep=sleep,
            clock=lambda: self.clock,
        )

    def test_with_nobody_running_this_start_becomes_airgrabber(self) -> None:
        lock = self.run_hand_over()
        self.assertIsNotNone(lock)
        self.assertEqual(self.files(), [])  # the request stays this start's own
        self.assertFalse(self.new_lock().acquire())  # it holds the lock

    def test_the_running_copy_takes_it(self) -> None:
        running = InstanceLock(*self.names, data_dir=self.data_dir)
        self.addCleanup(running.release)
        self.assertTrue(running.acquire())
        taken: list[ForgeRequest] = []

        def poll() -> None:
            if running.forge_requested():
                taken.extend(running.take_requests())

        self.assertIsNone(self.run_hand_over(poll))
        self.assertEqual(taken, [REQUEST])
        self.assertLess(self.clock, 0.5)  # no waiting once it was taken

    def test_a_copy_that_never_takes_it_is_given_up_on_after_the_wait(self) -> None:
        running = InstanceLock(*self.names, data_dir=self.data_dir)
        self.addCleanup(running.release)
        self.assertTrue(running.acquire())
        self.assertIsNone(self.run_hand_over())
        self.assertGreaterEqual(self.clock, 3.0)
        self.assertLess(self.clock, 3.5)
        self.assertEqual(running.take_requests(), [REQUEST])  # it still waits in the folder

    def test_a_copy_that_shuts_down_without_taking_it(self) -> None:
        running = InstanceLock(*self.names, data_dir=self.data_dir)
        self.addCleanup(running.release)
        self.assertTrue(running.acquire())
        polls = 0

        def poll() -> None:
            nonlocal polls
            polls += 1
            if polls == 5:
                running.release()  # it exits

        lock = self.run_hand_over(poll)
        self.assertIsNotNone(lock)
        self.assertEqual(self.files(), [])  # taken back: this start carries it itself
        self.assertFalse(InstanceLock(*self.names).acquire())  # and holds the lock now

    def test_taken_just_before_the_copy_let_go(self) -> None:
        running = InstanceLock(*self.names, data_dir=self.data_dir)
        self.addCleanup(running.release)
        self.assertTrue(running.acquire())
        taken: list[ForgeRequest] = []

        def poll() -> None:
            if not taken:
                taken.extend(running.take_requests())
                running.release()

        # The sender looked just before the running copy took the file and let go.
        with mock.patch.object(Path, "exists", return_value=True):
            self.assertIsNone(self.run_hand_over(poll))
        self.assertEqual(taken, [REQUEST])
        after = InstanceLock(*self.names)
        self.addCleanup(after.release)
        self.assertTrue(after.acquire())  # the sender didn't keep the lock: nobody starts twice

    def test_a_request_that_cannot_be_written_raises(self) -> None:
        running = InstanceLock(*self.names, data_dir=self.data_dir)
        self.addCleanup(running.release)
        self.assertTrue(running.acquire())
        (self.data_dir / HANDOVER_FOLDER).write_text("a file, not a folder", encoding="utf-8")
        with self.assertRaises(OSError):
            self.run_hand_over()
        self.assertTrue(all(lock._event is None for lock in self.locks))  # handles closed


class NotWindowsTests(_TempDataDir):
    def test_everything_but_the_mailbox_is_a_no_op(self) -> None:
        lock = InstanceLock(*_unique_names(), data_dir=self.data_dir)
        with mock.patch.object(sys, "platform", "linux"):
            self.assertTrue(lock.acquire())
            self.assertTrue(InstanceLock(*_unique_names()).acquire())
            self.assertFalse(lock.signal_show())
            self.assertFalse(lock.ask_to_show(answer_s=0))
            self.assertFalse(lock.show_requested())
            self.assertFalse(lock.forge_requested())
            send_request(self.data_dir, REQUEST)
            self.assertEqual(lock.take_requests(), [REQUEST])
            self.assertIs(hand_over(self.data_dir, REQUEST, lock_factory=lambda: lock), lock)
            lock.release()


if __name__ == "__main__":
    unittest.main()
