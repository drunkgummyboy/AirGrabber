"""Single instance: the named mutex and the "show the window" event, under unique test names so
a running AirGrabber is never involved."""

from __future__ import annotations

import sys
import unittest
import uuid
from unittest import mock

from app.single_instance import InstanceLock


def _unique_names() -> tuple[str, str]:
    tag = uuid.uuid4().hex
    return f"Local\\AirGrabberTest-{tag}", f"Local\\AirGrabberTest-{tag}-show"


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

    def test_released_lock_can_be_taken_again(self) -> None:
        self.assertTrue(self.first.acquire())
        self.first.release()
        self.assertTrue(self.second.acquire())
        self.first.release()  # twice is fine
        self.assertFalse(self.first.show_requested())  # released: no handles left

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


class NotWindowsTests(unittest.TestCase):
    def test_everything_is_a_no_op(self) -> None:
        lock = InstanceLock(*_unique_names())
        with mock.patch.object(sys, "platform", "linux"):
            self.assertTrue(lock.acquire())
            self.assertTrue(InstanceLock(*_unique_names()).acquire())
            self.assertFalse(lock.signal_show())
            self.assertFalse(lock.show_requested())
            lock.release()


if __name__ == "__main__":
    unittest.main()
