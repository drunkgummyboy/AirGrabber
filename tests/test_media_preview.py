"""The seek bar's preview pictures (app/media_preview.py): places, the key colour, the worker."""

from __future__ import annotations

import queue
import threading
import unittest

from PIL import Image

from app.media_preview import (
    LEAST_STEP_MS,
    PreviewFrame,
    PreviewWorker,
    place_of,
    without_key,
)


class PlaceTests(unittest.TestCase):
    def test_a_long_film_is_cut_in_steps(self) -> None:
        two_hours = 7_200_000  # a step of 24 s
        self.assertEqual(place_of(0, two_hours), 12_000)
        self.assertEqual(place_of(23_999, two_hours), 12_000)
        self.assertEqual(place_of(24_000, two_hours), 36_000)
        self.assertEqual(place_of(two_hours, two_hours), two_hours - 12_000)

    def test_a_short_video_has_the_least_step(self) -> None:
        minute = 60_000
        self.assertEqual(place_of(0, minute), LEAST_STEP_MS // 2)
        self.assertEqual(place_of(5_000, minute), 5_000)

    def test_always_within_the_video(self) -> None:
        self.assertEqual(place_of(-5, 60_000), 1_000)
        self.assertEqual(place_of(100, 1_500), 1_000)
        self.assertEqual(place_of(500, 0), 0)


class KeyTests(unittest.TestCase):
    def test_no_pixel_of_the_key_colour_is_left(self) -> None:
        image = Image.new("RGB", (4, 1), (1, 2, 3))
        image.putpixel((1, 0), (1, 9, 9))
        image.putpixel((2, 0), (200, 2, 3))
        result = without_key(image, (1, 2, 3))
        pixels = [result.getpixel((x, 0)) for x in range(4)]
        self.assertEqual(pixels, [(2, 2, 3), (2, 9, 9), (200, 2, 3), (2, 2, 3)])


class FakePreview:
    def __init__(self) -> None:
        self.asked: list[tuple[str, int, int]] = []
        self.hold = threading.Event()
        self.hold.set()
        self.started = threading.Event()
        self.released = threading.Event()
        self.fail_at: int | None = None

    def picture(self, path: str, ms: int, width: int) -> Image.Image | None:
        self.started.set()
        self.asked.append((path, ms, width))
        self.hold.wait(5)
        if ms == self.fail_at:
            raise OSError("broken")
        return Image.new("RGB", (width, width * 9 // 16))

    def release(self) -> None:
        self.released.set()


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preview = FakePreview()
        self.frames: queue.Queue[PreviewFrame] = queue.Queue()
        self.worker = PreviewWorker(self.preview, self.frames.put)
        self.worker.start()
        self.addCleanup(self._end)

    def _end(self) -> None:
        self.preview.hold.set()
        self.worker.stop()
        self.worker.join(5)

    def test_a_wish_comes_back_as_a_frame(self) -> None:
        self.worker.want(3, "C:\\video.mkv", 12_000, 320)
        frame = self.frames.get(timeout=5)
        self.assertEqual((frame.generation, frame.place_ms), (3, 12_000))
        self.assertEqual(frame.image.size, (320, 180))
        self.assertEqual(self.preview.asked, [("C:\\video.mkv", 12_000, 320)])

    def test_only_the_newest_wish_waits(self) -> None:
        self.preview.hold.clear()
        self.worker.want(1, "v", 1_000, 100)
        self.assertTrue(self.preview.started.wait(5))  # busy with the first
        for ms in (2_000, 3_000, 4_000):
            self.worker.want(1, "v", ms, 100)
        self.preview.hold.set()
        places = [self.frames.get(timeout=5).place_ms for _ in range(2)]
        self.assertEqual(places, [1_000, 4_000])
        self.assertTrue(self.frames.empty())

    def test_a_failure_is_a_frame_without_a_picture(self) -> None:
        self.preview.fail_at = 5_000
        with self.assertLogs("app.media_preview", "WARNING") as logs:
            self.worker.want(1, "C:\\secret\\Title.mkv", 5_000, 100)
            frame = self.frames.get(timeout=5)
        self.assertIsNone(frame.image)
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertNotIn("Title", "\n".join(logs.output))

    def test_stopping_lets_go_of_the_preview(self) -> None:
        self.worker.stop()
        self.assertTrue(self.worker.join(5))
        self.assertTrue(self.preview.released.is_set())


if __name__ == "__main__":
    unittest.main()
