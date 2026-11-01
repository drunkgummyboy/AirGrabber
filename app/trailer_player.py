"""The trailer player: a YouTube trailer in a borderless window of its own, in its own process.

Tk can't play video, so the info window's "▶ Watch trailer" (app/ui/trailer.py) starts this module
as `python -m app.trailer_player [--background #161616 …] -- <YouTube video id>`: a pywebview
window (Edge WebView2) without a title bar, AirGrabber's pop-up look done in the page itself —
the dark background, a band across the top that drags the window (CSS `app-region: drag`,
WebView2's non-client regions, so Windows' own move loop; pywebview's drag region as the
fallback), the round ✕ at its right (borderless.py's CloseButton: the disc, red with a white ✕
under the pointer, "Close (Esc)" as its tip) — and the video 16:9 under it, playing at once.
The window is about WIDTH_SHARE of the work area's width of the monitor under the pointer
(where "▶ Watch trailer" was just clicked), centred, at most MAX_HEIGHT_SHARE of its height
(`player_geometry`), with Windows 11's rounded corners. Escape closes it (in the player's own
full screen: leaves that first); YouTube's full-screen button fills the monitor.

Why a page from a server: YouTube's embedded player refuses to play ("Error 153") for a page
that sends no Referer — about:blank, file:// and data: pages — so the page comes from a tiny
http server on 127.0.0.1 in this process (a free port, the page only, nothing logged), with
`strict-origin-when-cross-origin` and the player's `origin` set to it. When YouTube's player
reports an error (embedding not allowed, video gone, its script unreachable) the video opens
on YouTube in the browser instead and the window closes. WebView2's profile is kept in the
data folder (`profile_folder`).

With the app: one line "ready" on stdout once the page has loaded (nothing else is written
there); when stdin reaches its end — the app closing this trailer, or the app ending, which
closes its end of the pipe — the window closes. A page that hasn't loaded after
LOAD_TIMEOUT_S closes it too. The exit code says how it went (EXIT_CODES).

This process may use threads (the http server, the stdin watch): pywebview runs the window's
own WinForms loop. Stdlib only at import time (the tests and the app import this module):
pywebview and pythonnet load in `main`, in this process only. Neither the video id nor a link
is ever printed or logged.
"""

from __future__ import annotations

import argparse
import ctypes
import html
import http.server
import json
import os
import re
import sys
import threading
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

READY_LINE = b"ready"
LOAD_TIMEOUT_S = 30.0  # the page loaded by then, or the window closes (EXIT_SLOW)

EXIT_CLOSED = 0  # shown, then closed
EXIT_USAGE = 2  # bad arguments
EXIT_NO_WEBVIEW = 3  # pywebview / pythonnet can't be loaded
EXIT_NO_WEBVIEW2 = 4  # no Edge WebView2 runtime, or it failed to start
EXIT_SLOW = 5  # the page didn't load within LOAD_TIMEOUT_S
EXIT_FAILED = 6  # the window failed otherwise
EXIT_IN_BROWSER = 7  # YouTube's player refused the video: opened in the browser instead
EXIT_CODES = {
    EXIT_CLOSED: "closed",
    EXIT_USAGE: "bad arguments",
    EXIT_NO_WEBVIEW: "pywebview can't be loaded",
    EXIT_NO_WEBVIEW2: "no WebView2",
    EXIT_SLOW: "the page didn't load",
    EXIT_FAILED: "failed",
    EXIT_IN_BROWSER: "opened in the browser",
}

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")  # YouTube's video ids
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_LANGUAGE = re.compile(r"[a-z]{2}(?:-[A-Za-z]{2})?")  # "en", "nl", "pt-BR"

# The window's size: a share of the work area's width, the video 16:9 under the band.
WIDTH_SHARE = 0.7
MAX_HEIGHT_SHARE = 0.9
# Copies of app/ui/borderless.py's close button (that module needs customtkinter; the tests
# keep the two in step), in CSS pixels = Tk's units: WebView2 scales the page like CTk does.
CLOSE_SIZE = 34
CLOSE_INSET = 12
CLOSE_FONT_SIZE = 15
CLOSE_GLYPH = "✕"
BAND = CLOSE_INSET + CLOSE_SIZE  # borderless.CLOSE_ROOM: the band the ✕ sits in
PAD = CLOSE_INSET  # around the video, left, right and below
VIDEO_RADIUS = 8

# WebView2's own switches: play with sound without a click first, a cache of at most 50 MB
# (pywebview's own switch kept).
BROWSER_ARGUMENTS = (
    "--autoplay-policy=no-user-gesture-required --disk-cache-size=52428800"
    " --disable-features=ElasticOverscroll"
)
PROFILE_FOLDER = "trailer-player"  # in the data folder (`profile_folder`)
EMBED_HOST = "https://www.youtube-nocookie.com"
WATCH_URL = "https://www.youtube.com/watch?v={id}"

Rect = tuple[int, int, int, int]  # left, top, right, bottom (physical pixels)


@dataclass(frozen=True, slots=True)
class Look:
    """The window's colours (#rrggbb: theme.py's dark halves, from the app), texts (already in
    the app's language) and that language's code (YouTube's player speaks it: `hl`)."""

    background: str = "#161616"
    button: str = "#262626"
    button_hover: str = "#C0392B"
    glyph: str = "#A4B2C6"
    glyph_hover: str = "#FFFFFF"
    tip: str = "Close (Esc)"
    title: str = "AirGrabber"
    language: str = "en"


@dataclass(frozen=True, slots=True)
class Options:
    video_id: str
    look: Look


@dataclass(frozen=True, slots=True)
class Geometry:
    """Where the window goes, physical pixels."""

    x: int
    y: int
    width: int
    height: int


def player_geometry(work: Rect, scale: float) -> Geometry:
    """The window centred in `work` (a monitor's work area) at Windows' `scale`: WIDTH_SHARE of
    its width, the band and the padding around a 16:9 video, never taller than
    MAX_HEIGHT_SHARE of its height (then narrower, the video still 16:9)."""
    left, top, right, bottom = work
    work_width, work_height = max(1, right - left), max(1, bottom - top)
    band, pad = round(BAND * scale), round(PAD * scale)
    width = max(2 * pad + 16, round(work_width * WIDTH_SHARE))
    video_height = round((width - 2 * pad) * 9 / 16)
    height = band + video_height + pad
    limit = int(work_height * MAX_HEIGHT_SHARE)
    if height > limit:
        video_height = max(9, limit - band - pad)
        width = round(video_height * 16 / 9) + 2 * pad
        height = band + video_height + pad
    return Geometry(
        x=left + (work_width - width) // 2,
        y=top + (work_height - height) // 2,
        width=width,
        height=height,
    )


def page_html(video_id: str, origin: str, look: Look) -> str:
    """The player's page: the band, the ✕ and YouTube's player (its IFrame API, so an error
    reaches `pywebview.api.failed`)."""
    return _PAGE.substitute(
        background=look.background,
        button=look.button,
        button_hover=look.button_hover,
        glyph=look.glyph,
        glyph_hover=look.glyph_hover,
        tip=html.escape(look.tip),
        title=html.escape(look.title),
        band=BAND,
        pad=PAD,
        size=CLOSE_SIZE,
        inset=CLOSE_INSET,
        font=CLOSE_FONT_SIZE,
        glyph_text=CLOSE_GLYPH,
        radius=VIDEO_RADIUS,
        video=json.dumps(video_id),
        origin=json.dumps(origin),
        host=json.dumps(EMBED_HOST),
        language=json.dumps(look.language),
    )


_PAGE = Template(
    """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<title>$title</title>
<style>
html, body { margin: 0; height: 100%; overflow: hidden; background: $background; }
#band { position: fixed; left: 0; top: 0; right: 0; height: ${band}px;
  app-region: drag; -webkit-app-region: drag; }
#close { position: fixed; top: ${inset}px; right: ${inset}px; width: ${size}px; height: ${size}px;
  border-radius: 50%; background: $button; color: $glyph; cursor: default; user-select: none;
  display: flex; align-items: center; justify-content: center;
  font: ${font}px "Segoe UI Symbol", "Segoe UI", sans-serif;
  app-region: no-drag; -webkit-app-region: no-drag; }
#close:hover { background: $button_hover; color: $glyph_hover; }
#video { position: fixed; left: ${pad}px; right: ${pad}px; top: ${band}px; bottom: ${pad}px;
  border-radius: ${radius}px; overflow: hidden; background: #000; }
#video iframe, #player { display: block; width: 100%; height: 100%; border: 0; }
</style></head>
<body>
<div id="band" class="pywebview-drag-region"></div>
<div id="close" title="$tip">$glyph_text</div>
<div id="video"><div id="player"></div></div>
<script>
function app(name, value) {
  function call() {
    var api = window.pywebview.api;
    if (value === undefined) { api[name](); } else { api[name](value); }
  }
  if (window.pywebview && window.pywebview.api && window.pywebview.api[name]) {
    call();
  } else {
    window.addEventListener("pywebviewready", call);
  }
}
document.getElementById("close").addEventListener("click", function () { app("close"); });
function onYouTubeIframeAPIReady() {
  new YT.Player("player", {
    host: $host,
    videoId: $video,
    playerVars: { autoplay: 1, rel: 0, playsinline: 1, cc_load_policy: 0, hl: $language, origin: $origin },
    events: { onError: function (event) { app("failed", String(event.data)); } }
  });
}
</script>
<script src="https://www.youtube.com/iframe_api" onerror="app('failed', 'script')"></script>
</body></html>
"""
)


def parse_options(argv: Sequence[str]) -> Options | None:
    """The command line (app/ui/trailer.py's `player_command`); None when it isn't usable."""
    parser = argparse.ArgumentParser(prog="trailer_player", add_help=False, exit_on_error=False)
    parser.add_argument("video_id")
    defaults = Look()
    for name in ("background", "button", "button_hover", "glyph", "glyph_hover", "tip", "title", "language"):
        parser.add_argument("--" + name.replace("_", "-"), dest=name, default=getattr(defaults, name))
    try:
        namespace, _unknown = parser.parse_known_args(list(argv))
    except (argparse.ArgumentError, SystemExit):
        return None
    if not VIDEO_ID.fullmatch(namespace.video_id):
        return None
    colours = {}
    for name in ("background", "button", "button_hover", "glyph", "glyph_hover"):
        value = str(getattr(namespace, name))
        colours[name] = value if _HEX.fullmatch(value) else getattr(defaults, name)
    language = str(namespace.language)
    look = Look(
        **colours,
        tip=str(namespace.tip),
        title=str(namespace.title),
        language=language if _LANGUAGE.fullmatch(language) else defaults.language,
    )
    return Options(video_id=namespace.video_id, look=look)


# -- the process --------------------------------------------------------------------------------


class _PageServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, page: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _PageHandler)
        self.page = page

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _PageHandler(http.server.BaseHTTPRequestHandler):
    server: _PageServer

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        if self.path.split("?", 1)[0] != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.server.page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(self.server.page)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # nothing logged (the page holds the video id)


class _Outcome:
    """What the exit code says, and the ready line sent once (from pywebview's threads)."""

    def __init__(self, ready_fd: int | None) -> None:
        self.code = EXIT_CLOSED
        self.loaded = threading.Event()
        self._ready_fd = ready_fd
        self._lock = threading.Lock()

    def ready(self) -> None:
        """The page has loaded (or the player gave up on it itself): "ready" to the app."""
        with self._lock:
            if self.loaded.is_set():
                return  # a later navigation
            self.loaded.set()
            if self._ready_fd is not None:
                try:
                    os.write(self._ready_fd, READY_LINE + b"\n")
                    os.close(self._ready_fd)
                except OSError:
                    pass
                self._ready_fd = None


class _Api:
    """What the page calls (`pywebview.api.close()` / `.failed(code)`)."""

    def __init__(self, video_id: str, outcome: _Outcome) -> None:
        self._video_id = video_id
        self._outcome = outcome
        self._window: Any = None  # set once the window exists

    def close(self, *_ignored: object) -> None:
        if self._window is not None:
            self._window.destroy()

    def failed(self, _code: object = "") -> None:
        """YouTube's player can't play it here: on YouTube in the browser instead (the app
        told "ready" first, so it doesn't open it too; the exit code says so)."""
        self._outcome.ready()
        self._outcome.code = EXIT_IN_BROWSER
        webbrowser.open(WATCH_URL.format(id=self._video_id))
        self.close()


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_options(sys.argv[1:] if argv is None else argv)
    if options is None:
        return EXIT_USAGE
    ready_fd = _quiet_output()
    _per_monitor_dpi()
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = BROWSER_ARGUMENTS
    try:
        import webview
        from webview.platforms import winforms
    except Exception:
        return EXIT_NO_WEBVIEW
    if not winforms.is_chromium:  # pywebview would fall back to Internet Explorer
        return EXIT_NO_WEBVIEW2
    from app.identity import set_process_app_id

    set_process_app_id()  # AirGrabber's taskbar group, not Python's
    work, scale = _work_area_under_pointer()
    geometry = player_geometry(work, scale)

    server = _PageServer(b"")
    server.page = page_html(options.video_id, server.origin, options.look).encode("utf-8")
    threading.Thread(target=server.serve_forever, name="trailer-page", daemon=True).start()

    outcome = _Outcome(ready_fd)
    api = _Api(options.video_id, outcome)
    window: Any = webview.create_window(
        options.look.title,
        server.origin + "/",
        js_api=api,
        width=round(geometry.width / scale),  # pywebview's units; `_prepare` sets the pixels
        height=round(geometry.height / scale),
        frameless=True,
        easy_drag=False,
        resizable=False,  # also no maximising by a double click on the band
        background_color=options.look.background,
    )
    api._window = window
    window.events.before_show += lambda: _prepare(window, geometry, outcome)
    window.events.loaded += outcome.ready
    icon = Path(__file__).resolve().parent / "assets" / "airgrabber.ico"
    try:
        webview.start(
            _watch,
            (window, outcome),
            gui="edgechromium",
            private_mode=False,
            storage_path=str(profile_folder()),
            icon=str(icon) if icon.is_file() else None,
        )
    except Exception:
        return EXIT_FAILED
    return outcome.code


def profile_folder() -> Path:
    """WebView2's profile, kept between trailers (in the data folder: a trailer starts about
    two seconds sooner than with a new one each time); its cache capped by BROWSER_ARGUMENTS."""
    from app.config import AppPaths

    return AppPaths.default().root / PROFILE_FOLDER


def _quiet_output() -> int | None:
    """Keep stdout's pipe for the ready line only; everything else (pywebview's prints and
    logging) goes nowhere. The pipe's own descriptor, or None without one."""
    try:
        ready_fd: int | None = os.dup(1)
    except OSError:
        ready_fd = None
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    except OSError:
        pass
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    return ready_fd


def _watch(window: Any, outcome: _Outcome) -> None:
    """pywebview's start function (its own thread): close when the page doesn't load in time,
    and when stdin ends (the app closed this trailer, or ended)."""
    threading.Thread(target=_watch_load, args=(window, outcome), daemon=True).start()
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        return  # no stdin to watch: the window closes only by itself
    window.destroy()


def _watch_load(window: Any, outcome: _Outcome) -> None:
    if not outcome.loaded.wait(LOAD_TIMEOUT_S):
        if outcome.code == EXIT_CLOSED:
            outcome.code = EXIT_SLOW
        window.destroy()


def _prepare(window: Any, geometry: Geometry, outcome: _Outcome) -> None:
    """pywebview's `before_show` (the window's own thread, before it shows): its place and
    size, Windows 11's rounded corners and dark border, then WebView2's settings once it runs."""
    from System.Windows.Forms import FormStartPosition

    form = window.native
    form.StartPosition = FormStartPosition.Manual
    hwnd = int(form.Handle.ToInt64())
    _set_bounds(hwnd, geometry)
    _dwm_attribute(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)
    _dwm_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    browser = form.webview
    full_screen = _FullScreen(hwnd)

    def initialized(sender: Any, args: Any) -> None:
        if not args.IsSuccess:
            outcome.code = EXIT_NO_WEBVIEW2
            form.Close()
            return
        core = sender.CoreWebView2
        try:
            core.Settings.IsNonClientRegionSupportEnabled = True  # the band drags natively
        except Exception:
            pass  # an older WebView2: pywebview's own drag region does it
        core.ContainsFullScreenElementChanged += lambda _sender, _args: full_screen.follow(
            bool(core.ContainsFullScreenElement)
        )

    def key_down(_sender: Any, args: Any) -> None:
        # WebView2 hands Escape to the form even while YouTube's player has the focus.
        if str(args.KeyCode) != "Escape":
            return
        core = browser.CoreWebView2
        if core is not None and bool(core.ContainsFullScreenElement):
            core.ExecuteScriptAsync("document.exitFullscreen && document.exitFullscreen()")
        else:
            form.Close()

    browser.CoreWebView2InitializationCompleted += initialized
    browser.KeyDown += key_down


class _FullScreen:
    """YouTube's full-screen button: the window covers its monitor, then goes back."""

    def __init__(self, hwnd: int) -> None:
        self._hwnd = hwnd
        self._saved: Geometry | None = None

    def follow(self, full: bool) -> None:
        if full and self._saved is None:
            self._saved = _window_geometry(self._hwnd)
            left, top, right, bottom = _monitor_of_window(self._hwnd)
            _set_bounds(self._hwnd, Geometry(left, top, right - left, bottom - top))
        elif not full and self._saved is not None:
            _set_bounds(self._hwnd, self._saved)
            self._saved = None


# -- Win32 ----------------------------------------------------------------------------------------

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33  # Windows 11; refused on Windows 10
_DWMWCP_ROUND = 2
_MONITOR_DEFAULTTONEAREST = 2
_MDT_EFFECTIVE_DPI = 0
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", ctypes.c_long * 4),
        ("rcWork", ctypes.c_long * 4),
        ("dwFlags", ctypes.c_ulong),
    ]


def _per_monitor_dpi() -> None:
    """Sharp on every monitor (before pywebview asks for less)."""
    if sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32")
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except (OSError, AttributeError):
            pass


def _work_area_under_pointer() -> tuple[Rect, float]:
    """The work area of the monitor under the pointer and that monitor's scaling."""
    fallback: Rect = (0, 0, 1280, 720)
    if sys.platform != "win32":
        return fallback, 1.0
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromPoint(point, _MONITOR_DEFAULTTONEAREST)
        info = _monitor_info(monitor)
        if info is None:
            return fallback, 1.0
        dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
        try:
            shcore = ctypes.WinDLL("shcore")
            shcore.GetDpiForMonitor(
                monitor, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
            )
        except (OSError, AttributeError):
            pass
        work = info.rcWork
        return (work[0], work[1], work[2], work[3]), max(1.0, dpi_x.value / 96)
    except (OSError, AttributeError):
        return fallback, 1.0


def _monitor_info(monitor: Any) -> _MonitorInfo | None:
    user32 = ctypes.WinDLL("user32")
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return info


def _monitor_of_window(hwnd: int) -> Rect:
    user32 = ctypes.WinDLL("user32")
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), _MONITOR_DEFAULTTONEAREST)
    info = _monitor_info(ctypes.c_void_p(monitor))
    if info is None:
        geometry = _window_geometry(hwnd)
        return (geometry.x, geometry.y, geometry.x + geometry.width, geometry.y + geometry.height)
    rect = info.rcMonitor
    return (rect[0], rect[1], rect[2], rect[3])


def _window_geometry(hwnd: int) -> Geometry:
    rect = (ctypes.c_long * 4)()
    ctypes.WinDLL("user32").GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return Geometry(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])


def _set_bounds(hwnd: int, geometry: Geometry) -> None:
    ctypes.WinDLL("user32").SetWindowPos(
        ctypes.c_void_p(hwnd),
        None,
        geometry.x,
        geometry.y,
        geometry.width,
        geometry.height,
        _SWP_NOZORDER | _SWP_NOACTIVATE,
    )


def _dwm_attribute(hwnd: int, attribute: int, value: int) -> None:
    try:
        data = ctypes.c_int(value)
        ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), attribute, ctypes.byref(data), ctypes.sizeof(data)
        )
    except (OSError, AttributeError):
        pass


if __name__ == "__main__":
    # Straight out once the window is gone: a normal exit waits seconds for .NET to shut
    # down (and for the page server's thread), with nothing left to save.
    os._exit(main())
