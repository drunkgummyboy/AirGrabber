"""Pictures for the seek bar's preview, in the player's process (app/media_player.py).

Hovering the seek bar shows the picture at that place above the time (owner, 2026-11-20). The
pictures come from a second, hidden and silent libVLC player on the same video (`VlcPreview`:
libVLC's memory output, no window), opened at the place (`:start-time`) for one picture and then
stopped, so nothing is read in the background and nothing else is installed. The bar is cut into
places (`place_of`: STEPS of the length, at least LEAST_STEP_MS apart); each place's picture is
asked once per video and kept by the window. `PreviewWorker` is the pictures' own thread: only
the newest wish waits (a pointer swept along the bar asks for many), so a share that answers
slowly never piles them up, and a libVLC call that hangs holds up only the previews.

Stdlib only at import time (Pillow inside the functions): app/media_player.py imports
`PreviewFrame`. Nothing here logs a path, a file name or a title.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger(__name__)

STEPS = 300  # the seek bar's places: a 2-hour film gets a picture every 24 s
LEAST_STEP_MS = 2_000
FRAME_TIMEOUT_S = 8.0  # one picture by then (a share asleep), else none for that place
_ALIGN = 32  # libVLC wants its picture buffers on 32-byte boundaries


def place_of(ms: int, duration_ms: int) -> int:
    """The place of `ms` on the bar: the middle of its step, within the video."""
    if duration_ms <= 0:
        return 0
    step = max(LEAST_STEP_MS, duration_ms // STEPS)
    ms = max(0, min(ms, duration_ms - 1))
    return min((ms // step) * step + step // 2, max(0, duration_ms - 1))


def without_key(image: Image.Image, key: tuple[int, int, int]) -> Image.Image:
    """`image` with no pixel of the colour `key` (the overlay's see-through colour: such a pixel
    would be a hole): a red of exactly key's red is made one higher."""
    from PIL import Image as PilImage

    red, green, blue = image.convert("RGB").split()
    red = red.point(lambda value: key[0] + 1 if value == key[0] else value)
    return PilImage.merge("RGB", (red, green, blue))


@dataclass(frozen=True, slots=True)
class PreviewFrame:
    """The picture at `place_ms` of video `generation` (None: there is none to be had)."""

    generation: int
    place_ms: int
    image: Any  # a Pillow RGB image, or None


class PreviewSeam(Protocol):
    """Where the pictures come from (`VlcPreview`; tests: a fake). Called only on the previews'
    own thread (`PreviewWorker`)."""

    def picture(self, path: str, ms: int, width: int) -> Image.Image | None:
        """The picture of `path` at `ms`, `width` pixels wide (None: none to be had)."""
        ...

    def release(self) -> None: ...


class PreviewWorker:
    """The previews' own thread: the newest wish only, each answered as a `PreviewFrame` handed
    to `deliver` (the window's events queue; no Tk here)."""

    def __init__(self, preview: PreviewSeam, deliver: Callable[[PreviewFrame], None]) -> None:
        self._preview = preview
        self._deliver = deliver
        self._wish: tuple[int, str, int, int] | None = None
        self._stopped = False
        self._changed = threading.Condition()
        self._thread = threading.Thread(target=self._run, name="media-preview", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def want(self, generation: int, path: str, ms: int, width: int) -> None:
        """Ask for the picture of video `generation` at `ms`; it replaces a wish still waiting."""
        with self._changed:
            self._wish = (generation, path, ms, width)
            self._changed.notify()

    def stop(self) -> None:
        """End the thread (after the picture it is making) and let go of the preview."""
        with self._changed:
            self._stopped = True
            self._changed.notify()

    def join(self, timeout: float) -> bool:
        if not self._thread.is_alive():
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            while True:
                with self._changed:
                    while self._wish is None and not self._stopped:
                        self._changed.wait()
                    if self._stopped:
                        return
                    wish, self._wish = self._wish, None
                if wish is None:
                    continue
                generation, path, ms, width = wish
                try:
                    image = self._preview.picture(path, ms, width)
                except Exception as exc:  # one picture missing, never the player
                    log.warning("A preview picture failed (%s)", type(exc).__name__)
                    image = None
                self._deliver(PreviewFrame(generation, ms, image))
        finally:
            try:
                self._preview.release()
            except Exception as exc:
                log.warning("Letting go of the preview failed (%s)", type(exc).__name__)


# -- libVLC ---------------------------------------------------------------------------------------

_UINT_P = ctypes.POINTER(ctypes.c_uint)
# libVLC's format callback with the chroma as a writable pointer (python-vlc's says c_char_p,
# which arrives as a copy); cast to python-vlc's own type when handed over.
_FormatCb = ctypes.CFUNCTYPE(
    ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, _UINT_P, _UINT_P, _UINT_P,
    _UINT_P,
)


class VlcPreview:
    """PreviewSeam on a second python-vlc player of the engine's instance, drawing into memory
    (`video_set_callbacks`, RV32) instead of a window; no sound, no subtitles."""

    def __init__(self, vlc: Any, instance: Any) -> None:
        self._vlc = vlc
        self._instance = instance
        self._player = instance.media_player_new()
        self._media: Any = None
        self._wanted_width = 0
        self._size = (0, 0)
        self._source = (0, 0)  # the video's size as libVLC first gives it (later ones may be padded)
        self._buffer: Any = None  # the picture buffer (kept alive while libVLC draws into it)
        self._address = 0
        self._want = False
        self._got: bytes | None = None
        self._ready = threading.Event()
        callbacks = vlc.CallbackDecorators
        # Kept on self: libVLC calls them for as long as the player lives.
        self._format_own = _FormatCb(self._format)
        self._format_cb = ctypes.cast(self._format_own, callbacks.VideoFormatCb)
        self._cleanup_cb = callbacks.VideoCleanupCb(self._cleanup)
        self._lock_cb = callbacks.VideoLockCb(self._lock)
        self._unlock_cb = callbacks.VideoUnlockCb(self._unlock)
        self._display_cb = callbacks.VideoDisplayCb(self._display)
        self._player.video_set_format_callbacks(self._format_cb, self._cleanup_cb)
        self._player.video_set_callbacks(self._lock_cb, self._unlock_cb, self._display_cb, None)
        self._player.video_set_mouse_input(False)
        self._player.video_set_key_input(False)

    def picture(self, path: str, ms: int, width: int) -> Image.Image | None:
        from PIL import Image as PilImage

        self._player.stop()
        self._wanted_width = max(16, width)
        media = self._instance.media_new_path(path)
        if media is None:  # libVLC couldn't make a media of it
            return None
        for option in (":no-audio", ":no-spu", ":no-sub-autodetect-file"):
            media.add_option(option)
        media.add_option(f":start-time={max(0, ms) / 1000:.3f}")
        self._player.set_media(media)
        if self._media is not None:
            self._media.release()
        self._media = media
        self._source = (0, 0)
        self._got = None
        self._ready.clear()
        self._want = True
        self._player.play()
        got = self._ready.wait(FRAME_TIMEOUT_S)
        self._want = False
        self._player.stop()
        data, (pic_width, pic_height) = self._got, self._size
        if not got or data is None or not pic_width or not pic_height:
            return None
        image = PilImage.frombuffer(
            "RGB", (pic_width, pic_height), data, "raw", "BGRX", pic_width * 4, 1
        )
        source_width, source_height = self._source
        if source_width > 0 and source_height > 0:  # libVLC's later size may be padded: its shape
            true_height = max(2, round(source_height * pic_width / source_width))
            if abs(true_height - pic_height) > 1:
                return image.resize((pic_width, true_height))
        return image.copy()

    def release(self) -> None:
        self._player.stop()
        self._player.release()
        if self._media is not None:
            self._media.release()
            self._media = None

    # libVLC's own threads call these: no Tk, nothing but the buffer and the flags.

    def _format(
        self,
        _opaque: Any,
        chroma: int | None,
        width: Any,
        height: Any,
        pitches: Any,
        lines: Any,
    ) -> int:
        try:
            source_width, source_height = int(width[0]), int(height[0])
            if not chroma or source_width <= 0 or source_height <= 0:
                return 0
            if self._source == (0, 0):
                self._source = (source_width, source_height)
            out_width = self._wanted_width & ~1
            out_height = max(2, round(source_height * out_width / source_width)) & ~1
            ctypes.memmove(chroma, b"RV32", 4)
            width[0], height[0] = out_width, out_height
            pitches[0], lines[0] = out_width * 4, out_height
            size = out_width * 4 * out_height
            self._buffer = ctypes.create_string_buffer(size + _ALIGN)
            start = ctypes.addressof(self._buffer)
            self._address = (start + _ALIGN - 1) & ~(_ALIGN - 1)
            self._size = (out_width, out_height)
            return 1
        except Exception:
            return 0

    def _cleanup(self, _opaque: Any) -> None:
        pass  # the buffer stays until the next format replaces it

    def _lock(self, _opaque: Any, planes: Any) -> None:
        planes[0] = self._address
        return None

    def _unlock(self, _opaque: Any, _picture: Any, _planes: Any) -> None:
        pass

    def _display(self, _opaque: Any, _picture: Any) -> None:
        if self._want and self._address:
            width, height = self._size
            self._got = ctypes.string_at(self._address, width * 4 * height)
            self._want = False
            self._ready.set()
