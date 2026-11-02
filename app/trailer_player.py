"""The trailer player: a YouTube trailer in a borderless window of its own, in its own process.

Tk can't play video, so the info window's "▶ Watch trailer" (app/ui/trailer.py) starts this module
as `python -m app.trailer_player [--background #161616 …] -- <YouTube video id>`: a pywebview
window (Edge WebView2) without a title bar, AirGrabber's pop-up look done in the page itself —
the dark background as an even frame MARGIN wide around the 16:9 video (the frame drags the
window: CSS `app-region: drag`, WebView2's non-client regions, so Windows' own move loop;
pywebview's drag region as the fallback), and over the video's top-right corner the round ✕
(borderless.py's CloseButton: the disc, red with a white ✕ under the pointer, "Close (Esc)" as
its tip) with the Fill toggle beside it. Both show fully while the pointer moves over the
window and fade to IDLE_OPACITY after IDLE_S without movement (`_PointerWatch`: the pointer
over YouTube's cross-origin player never reaches the page); meanwhile, and while the video
isn't playing, on the embedded player they slide down ACTIVE_TOP, below YouTube's top bar (its
volume, CC and settings show then), and back SLIDE_BACK_DELAY_S after it went idle; a pointer
resting on them keeps them where they are (`controls_zone`). Fill zooms the picture by
FILL_SCALE so a cinemascope trailer's black bars are cropped (a 16:9 one loses its edges, hence
a toggle): on the embedded player only while idle, so YouTube's controls show unzoomed when the
pointer moves; remembered in the data folder (`PlayerSettings`, SETTINGS_FILE). The window is
about WIDTH_SHARE of the work area's width of the monitor under the pointer (where "▶ Watch
trailer" was just clicked), centred, at most MAX_HEIGHT_SHARE of its height
(`player_geometry`), with Windows 11's rounded corners. Escape closes it (in the player's own
full screen: leaves that first); YouTube's full-screen button fills the monitor.

The embedded player needs an embedding page with an identity: YouTube refuses to play
("Error 153") for a page that sends no Referer (about:blank, file://, data:), and some videos
(error 150) when the Referer is an IP address such as 127.0.0.1, so the page comes from a tiny
http server in this process (a free port, the page only, nothing logged) and is opened as
http://localhost:<port>/, with `strict-origin-when-cross-origin` and the player's `origin` set
to it. When the embedded player still reports an error (`TrailerFlow`): its number goes to the
app's log (an EVENT_EMBED_ERROR line), and the same window opens YouTube's own watch page,
which no embedding rule applies to, made to look like the player (`watch_script`: only the
video shows, in the same frame, with the ✕ and Fill; YouTube's dialogs stay usable — its
"Before you continue to YouTube" cookie question is the user's to answer and is never clicked
here; the profile keeps the answer). Only when the watch page can't play the video either
(YouTube's playability status isn't OK: region block, gone, sign-in needed) or doesn't load
(WATCH_TIMEOUT_S, a failed navigation) does the window close and the video open in the
browser (EXIT_IN_BROWSER, an EVENT_WATCH_ERROR line first). WebView2's profile is kept in the
data folder (`profile_folder`).

With the app: one line "ready" on stdout once the page has loaded, then only the event lines
above (`event_line`: a name and a code, never a link or the video id); when stdin reaches its
end — the app closing this trailer, or the app ending, which closes its end of the pipe — the
window closes. A page that hasn't loaded after LOAD_TIMEOUT_S closes it too. The exit code
says how it went (EXIT_CODES).

This process may use threads (the http server, the stdin and pointer watches): pywebview runs
the window's own WinForms loop. Stdlib only at import time (the tests and the app import this
module): pywebview and pythonnet load in `main`, in this process only. Neither the video id
nor a link is ever printed or logged.
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
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Protocol
from urllib.parse import urlsplit

READY_LINE = b"ready"
LOAD_TIMEOUT_S = 30.0  # the page loaded by then, or the window closes (EXIT_SLOW)
WATCH_TIMEOUT_S = 25.0  # YouTube's watch page loaded by then, or the browser instead

EXIT_CLOSED = 0  # shown, then closed
EXIT_USAGE = 2  # bad arguments
EXIT_NO_WEBVIEW = 3  # pywebview / pythonnet can't be loaded
EXIT_NO_WEBVIEW2 = 4  # no Edge WebView2 runtime, or it failed to start
EXIT_SLOW = 5  # the page didn't load within LOAD_TIMEOUT_S
EXIT_FAILED = 6  # the window failed otherwise
EXIT_IN_BROWSER = 7  # neither YouTube player would play it here: opened in the browser instead
EXIT_CODES = {
    EXIT_CLOSED: "closed",
    EXIT_USAGE: "bad arguments",
    EXIT_NO_WEBVIEW: "pywebview can't be loaded",
    EXIT_NO_WEBVIEW2: "no WebView2",
    EXIT_SLOW: "the page didn't load",
    EXIT_FAILED: "failed",
    EXIT_IN_BROWSER: "opened in the browser",
}

# The lines after "ready" on stdout (app/ui/trailer.py logs them): "<event> <code>".
EVENT_EMBED_ERROR = "embed-error"  # the embedded player's error number (150, 153, …)
EVENT_WATCH_ERROR = "watch-error"  # why the watch page can't play it (UNPLAYABLE, navigation, …)
_CODE = re.compile(r"[A-Za-z0-9_-]{1,32}")

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")  # YouTube's video ids
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_LANGUAGE = re.compile(r"[a-z]{2}(?:-[A-Za-z]{2})?")  # "en", "nl", "pt-BR"

# The window's size: a share of the work area's width, the video 16:9 inside an even frame.
WIDTH_SHARE = 0.7
MAX_HEIGHT_SHARE = 0.9
MARGIN = 18  # the frame around the video, every side (CSS pixels = Tk's units)
VIDEO_RADIUS = 8
# Copies of app/ui/borderless.py's close button (that module needs customtkinter; the tests
# keep the two in step), in CSS pixels = Tk's units: WebView2 scales the page like CTk does.
CLOSE_SIZE = 34
CLOSE_FONT_SIZE = 15
CLOSE_GLYPH = "✕"
BUTTON_INSET = 10  # the buttons from the video's top-right corner (idle)
# On the embedded player, while the pointer moves (YouTube's own controls showing), the buttons
# slide down this far from the video's top: below YouTube's top bar (volume, CC, settings).
ACTIVE_TOP = 68
# They stay there a little longer than the pointer moves (YouTube's bar lingers), and for as
# long as the video isn't playing (YouTube shows its bar then).
SLIDE_BACK_DELAY_S = 1.2
BUTTON_GAP = 8  # between the Fill toggle and the ✕
IDLE_S = 2.0  # without pointer movement this long, the buttons fade (and Fill zooms)
IDLE_OPACITY = 0.5
FILL_SCALE = 4 / 3  # 2.39:1 bars cropped from a 16:9 picture (16/9 × 4/3 ≈ 2.37)

# WebView2's own switches: play with sound without a click first, a cache of at most 50 MB
# (pywebview's own switch kept).
BROWSER_ARGUMENTS = (
    "--autoplay-policy=no-user-gesture-required --disk-cache-size=52428800"
    " --disable-features=ElasticOverscroll"
)
PROFILE_FOLDER = "trailer-player"  # in the data folder (`profile_folder`)
SETTINGS_FILE = "trailer-player.json"  # beside it: {"fill": true}
EMBED_HOST = "https://www.youtube-nocookie.com"
WATCH_URL = "https://www.youtube.com/watch?v={id}"
PAGE_HOST = "localhost"  # not 127.0.0.1: some videos refuse an IP address as the embedder

Rect = tuple[int, int, int, int]  # left, top, right, bottom (physical pixels)

# The Fill toggle's icons (SVG paths in a 24-unit box): corners pointing out (fill the window),
# and in (show the whole picture) while Fill is on.
FILL_ICON_PATH = "M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"
FIT_ICON_PATH = "M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5"


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
    fill_tip: str = "Fill the window"
    fit_tip: str = "Show the whole picture"
    title: str = "AirGrabber"
    language: str = "en"


_TEXT_OPTIONS = ("tip", "fill_tip", "fit_tip", "title")
_COLOUR_OPTIONS = ("background", "button", "button_hover", "glyph", "glyph_hover")


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
    its width, a 16:9 video inside an even MARGIN, never taller than MAX_HEIGHT_SHARE of its
    height (then narrower, the video still 16:9)."""
    left, top, right, bottom = work
    work_width, work_height = max(1, right - left), max(1, bottom - top)
    margin = round(MARGIN * scale)
    width = max(2 * margin + 16, round(work_width * WIDTH_SHARE))
    video_height = round((width - 2 * margin) * 9 / 16)
    height = video_height + 2 * margin
    limit = int(work_height * MAX_HEIGHT_SHARE)
    if height > limit:
        video_height = max(9, limit - 2 * margin)
        width = round(video_height * 16 / 9) + 2 * margin
        height = video_height + 2 * margin
    return Geometry(
        x=left + (work_width - width) // 2,
        y=top + (work_height - height) // 2,
        width=width,
        height=height,
    )


def event_line(event: str, code: object) -> str:
    """A line for the app: the event and a short code (anything else becomes "unknown")."""
    text = str(code).strip()
    return f"{event} {text if _CODE.fullmatch(text) else 'unknown'}"


def watch_url(video_id: str, language: str) -> str:
    return WATCH_URL.format(id=video_id) + f"&hl={language}"


def is_watch_page(url: str) -> bool:
    """Whether `url` is YouTube's watch page (not its cookie question, sign-in or anything else)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return host in ("www.youtube.com", "youtube.com", "m.youtube.com") and parts.path == "/watch"


# -- the page's own parts: the frame, the ✕ and Fill ------------------------------------------


def _controls_css(look: Look) -> str:
    """The frame (four drag strips) and the buttons, shared by the player's page and the watch
    page."""
    m, size, inset = MARGIN, CLOSE_SIZE, MARGIN + BUTTON_INSET
    return f"""
.ag-strip {{ position: fixed; z-index: 2147483000; app-region: drag; -webkit-app-region: drag; }}
#ag-top {{ left: 0; top: 0; right: 0; height: {m}px; }}
#ag-bottom {{ left: 0; bottom: 0; right: 0; height: {m}px; }}
#ag-left {{ left: 0; top: 0; bottom: 0; width: {m}px; }}
#ag-right {{ right: 0; top: 0; bottom: 0; width: {m}px; }}
#ag-controls {{ position: fixed; top: {inset}px; right: {inset}px; z-index: 2147483647;
  display: flex; gap: {BUTTON_GAP}px; opacity: {IDLE_OPACITY};
  transition: opacity .4s, top .25s ease {SLIDE_BACK_DELAY_S}s;
  app-region: no-drag; -webkit-app-region: no-drag; visibility: visible !important; }}
html.ag-active #ag-controls, #ag-controls:hover {{ opacity: 1; transition: opacity .15s, top .25s ease; }}
.ag-button {{ width: {size}px; height: {size}px; border-radius: 50%; background: {look.button};
  color: {look.glyph}; cursor: default; user-select: none; display: flex; align-items: center;
  justify-content: center; font: {CLOSE_FONT_SIZE}px "Segoe UI Symbol", "Segoe UI", sans-serif;
  visibility: visible !important; }}
.ag-button svg {{ display: block; }}
.ag-button:hover {{ color: {look.glyph_hover}; background: {look.button};
  filter: brightness(1.35); }}
#ag-close:hover {{ background: {look.button_hover}; filter: none; }}
"""


_CONTROLS_JS = """
function agApp(name, value) {
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
function agControls(o) {
  if (document.getElementById("ag-controls")) { return; }
  var style = document.createElement("style");
  style.id = "ag-style";
  style.textContent = o.css;
  (document.head || document.documentElement).appendChild(style);
  if (o.drag) {
    ["ag-top", "ag-bottom", "ag-left", "ag-right"].forEach(function (id) {
      var strip = document.createElement("div");
      strip.id = id;
      strip.className = "ag-strip pywebview-drag-region";
      document.body.appendChild(strip);
    });
  }
  var controls = document.createElement("div");
  controls.id = "ag-controls";
  function button(id, title) {
    var b = document.createElement("div");
    b.id = id;
    b.className = "ag-button";
    b.title = title;
    controls.appendChild(b);
    return b;
  }
  // Built from elements, never from markup strings: YouTube's pages refuse those (Trusted Types).
  var ns = "http://www.w3.org/2000/svg";
  var icon = document.createElementNS(ns, "svg");
  var path = document.createElementNS(ns, "path");
  [["viewBox", "0 0 24 24"], ["width", "16"], ["height", "16"], ["fill", "none"],
   ["stroke", "currentColor"], ["stroke-width", "2"], ["stroke-linecap", "round"],
   ["stroke-linejoin", "round"]].forEach(function (a) { icon.setAttribute(a[0], a[1]); });
  icon.appendChild(path);
  var fill = button("ag-fill", "");
  fill.appendChild(icon);
  var close = button("ag-close", o.tip);
  close.textContent = o.glyph;
  var root = document.documentElement;
  window.agSetFill = function (on) {
    root.classList.toggle("ag-fill", !!on);
    path.setAttribute("d", on ? o.fitPath : o.fillPath);
    fill.title = on ? o.fitTip : o.fillTip;
  };
  window.agActive = function (on) { root.classList.toggle("ag-active", !!on); };
  fill.addEventListener("click", function () {
    var on = !root.classList.contains("ag-fill");
    window.agSetFill(on);
    agApp("fill", on);
  });
  close.addEventListener("click", function () { agApp("close"); });
  document.body.appendChild(controls);
  window.agSetFill(o.fill);
  window.agActive(true);
}
"""


def _controls_options(look: Look, *, fill: bool, drag: bool, css: str) -> str:
    return json.dumps(
        {
            "css": css,
            "drag": drag,
            "fill": fill,
            "tip": look.tip,
            "fillTip": look.fill_tip,
            "fitTip": look.fit_tip,
            "glyph": CLOSE_GLYPH,
            "fillPath": FILL_ICON_PATH,
            "fitPath": FIT_ICON_PATH,
        }
    ).replace("</", "<\\/")


def page_html(video_id: str, origin: str, look: Look, *, fill: bool = False) -> str:
    """The player's page: the frame, the ✕ and Fill, and YouTube's embedded player (its IFrame
    API, so an error reaches `pywebview.api.failed`)."""
    css = _controls_css(look) + (
        "html.ag-fill:not(.ag-active):not(.ag-paused) #video iframe"
        f" {{ transform: scale({FILL_SCALE:.4f}); }}\n"
        # YouTube's top bar shows with its controls (pointer moving, video not playing): out of
        # its way meanwhile (the watch page's player has no icons up there: corner there)
        "html.ag-active #ag-controls, html.ag-paused #ag-controls"
        f" {{ top: {MARGIN + ACTIVE_TOP}px; }}\n"
    )
    return _PAGE.substitute(
        background=look.background,
        title=html.escape(look.title),
        margin=MARGIN,
        radius=VIDEO_RADIUS,
        controls_js=_CONTROLS_JS,
        controls=_controls_options(look, fill=fill, drag=True, css=css),
        video=json.dumps(video_id),
        origin=json.dumps(origin),
        host=json.dumps(EMBED_HOST),
        language=json.dumps(look.language),
    )


_PAGE = Template(
    """<!doctype html>
<html class="ag-paused"><head><meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<title>$title</title>
<style>
html, body { margin: 0; height: 100%; overflow: hidden; background: $background; }
#video { position: fixed; left: ${margin}px; right: ${margin}px; top: ${margin}px; bottom: ${margin}px;
  border-radius: ${radius}px; overflow: hidden; background: #000;
  app-region: no-drag; -webkit-app-region: no-drag; }
#video iframe, #player { display: block; width: 100%; height: 100%; border: 0; }
#video iframe { transition: transform .35s ease; transform-origin: center; }
</style></head>
<body>
<div id="video"><div id="player"></div></div>
<script>
$controls_js
agControls($controls);
function onYouTubeIframeAPIReady() {
  new YT.Player("player", {
    host: $host,
    videoId: $video,
    playerVars: { autoplay: 1, rel: 0, playsinline: 1, cc_load_policy: 0, hl: $language, origin: $origin },
    events: {
      onError: function (event) { agApp("failed", String(event.data)); },
      // not playing (nor loading): YouTube shows its top bar, so the buttons keep clear of it
      onStateChange: function (event) {
        document.documentElement.classList.toggle("ag-paused", event.data !== 1 && event.data !== 3);
      }
    }
  });
}
</script>
<script src="https://www.youtube.com/iframe_api" onerror="agApp('failed', 'script')"></script>
</body></html>
"""
)


def watch_script(look: Look, *, fill: bool) -> str:
    """JavaScript for a page of YouTube's after the embedded player failed. On the watch page:
    only the video shows, in the player's frame (the page around it hidden, never removed:
    YouTube's dialogs — its cookie question included — stay visible and clickable; nothing is
    clicked here), with the ✕ and Fill; elsewhere (a sign-in or cookie page of its own) only
    the ✕ and Fill. Returns YouTube's playability status ("OK", "UNPLAYABLE", …; null when
    the page doesn't say)."""
    m = MARGIN
    watch_css = f"""
html, body {{ background: {look.background} !important; overflow: hidden !important; }}
body {{ visibility: hidden !important; }}
#movie_player, #movie_player *, ytd-consent-bump-v2-lightbox, ytd-consent-bump-v2-lightbox *,
tp-yt-iron-overlay-backdrop, tp-yt-paper-dialog, tp-yt-paper-dialog *, ytd-popup-container,
ytd-popup-container * {{ visibility: visible !important; }}
#movie_player {{ position: fixed !important; left: {m}px !important; top: {m}px !important;
  width: calc(100vw - {2 * m}px) !important; height: calc(100vh - {2 * m}px) !important;
  z-index: 2100 !important; border-radius: {VIDEO_RADIUS}px; overflow: hidden !important;
  background: #000; app-region: no-drag; -webkit-app-region: no-drag; }}
#movie_player video {{ transition: transform .35s ease; transform-origin: center; }}
html.ag-fill #movie_player video {{ transform: scale({FILL_SCALE:.4f}); }}
"""
    options = _controls_options(look, fill=fill, drag=True, css=_controls_css(look) + watch_css)
    plain = _controls_options(look, fill=fill, drag=False, css=_controls_css(look))
    return f"""(function () {{
{_CONTROLS_JS}
var onWatch = location.pathname === "/watch" && !!document.getElementById("movie_player");
agControls(onWatch ? {options} : {plain});
if (!onWatch) {{ return null; }}
window.dispatchEvent(new Event("resize"));
var status = null;
try {{
  var player = document.getElementById("movie_player");
  var response = (player && player.getPlayerResponse && player.getPlayerResponse())
    || window.ytInitialPlayerResponse;
  status = response && response.playabilityStatus ? String(response.playabilityStatus.status) : null;
}} catch (e) {{ status = null; }}
return status;
}})()"""


def parse_options(argv: Sequence[str]) -> Options | None:
    """The command line (app/ui/trailer.py's `player_command`); None when it isn't usable."""
    parser = argparse.ArgumentParser(prog="trailer_player", add_help=False, exit_on_error=False)
    parser.add_argument("video_id")
    defaults = Look()
    for name in (*_COLOUR_OPTIONS, *_TEXT_OPTIONS, "language"):
        parser.add_argument("--" + name.replace("_", "-"), dest=name, default=getattr(defaults, name))
    try:
        namespace, _unknown = parser.parse_known_args(list(argv))
    except (argparse.ArgumentError, SystemExit):
        return None
    if not VIDEO_ID.fullmatch(namespace.video_id):
        return None
    values: dict[str, str] = {}
    for name in _COLOUR_OPTIONS:
        value = str(getattr(namespace, name))
        values[name] = value if _HEX.fullmatch(value) else getattr(defaults, name)
    for name in _TEXT_OPTIONS:
        values[name] = str(getattr(namespace, name))
    language = str(namespace.language)
    values["language"] = language if _LANGUAGE.fullmatch(language) else defaults.language
    return Options(video_id=namespace.video_id, look=Look(**values))


# -- the fallbacks -------------------------------------------------------------------------------


class Window(Protocol):
    """What TrailerFlow uses of pywebview's window (the tests pass a fake)."""

    def load_url(self, url: str) -> None: ...

    def evaluate_js(self, script: str) -> Any: ...

    def destroy(self) -> None: ...


class PlayerSettings:
    """What the player remembers (SETTINGS_FILE beside its profile): Fill on or off. Never raises."""

    def __init__(self, path: Path, *, fill: bool = False) -> None:
        self.path = path
        self.fill = fill

    @classmethod
    def load(cls, path: Path) -> PlayerSettings:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path)
        return cls(path, fill=isinstance(data, dict) and data.get("fill") is True)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            spare = self.path.with_name(self.path.name + ".tmp")
            spare.write_text(json.dumps({"fill": self.fill}), encoding="utf-8")
            os.replace(spare, self.path)
        except OSError:
            pass


class _Outcome:
    """What the exit code says, whether the browser opens once the window is gone, and the
    lines to the app: "ready" once, then events (from pywebview's threads)."""

    def __init__(self, ready_fd: int | None) -> None:
        self.code = EXIT_CLOSED
        self.browser = False
        self.loaded = threading.Event()
        self._fd = ready_fd
        self._lock = threading.Lock()

    def ready(self) -> None:
        """The page has loaded (or the player gave up on it itself): "ready" to the app."""
        with self._lock:
            if self.loaded.is_set():
                return  # a later navigation
            self.loaded.set()
            self._write(READY_LINE)

    def say(self, line: str) -> None:
        """An event line for the app (after "ready")."""
        self.ready()
        with self._lock:
            self._write(line.encode("ascii", "replace"))

    def _write(self, data: bytes) -> None:
        if self._fd is not None:
            try:
                os.write(self._fd, data + b"\n")
            except OSError:
                self._fd = None


class TrailerFlow:
    """The trailer's way through the players: YouTube's embedded player, then (on its error)
    YouTube's watch page in the same window, then (when that can't play it) the browser."""

    EMBED, WATCH, DONE = "embed", "watch", "done"

    def __init__(
        self,
        video_id: str,
        look: Look,
        outcome: _Outcome,
        settings: PlayerSettings,
        *,
        timer: Callable[[float, Callable[[], None]], object] | None = None,
    ) -> None:
        self.video_id = video_id
        self.look = look
        self.outcome = outcome
        self.settings = settings
        self.stage = self.EMBED
        self.window: Window | None = None
        self._watch_shown = False
        self._timer = timer or _start_timer
        self._lock = threading.Lock()

    def embed_failed(self, code: object) -> None:
        """YouTube's embedded player reported an error: its number to the app, then the watch page."""
        with self._lock:
            if self.stage != self.EMBED:
                return
            self.stage = self.WATCH
        self.outcome.say(event_line(EVENT_EMBED_ERROR, code))
        self._timer(WATCH_TIMEOUT_S, self._watch_timeout)
        if self.window is not None:
            self.window.load_url(watch_url(self.video_id, self.look.language))

    def page_loaded(self, url: str) -> None:
        """A page finished loading: on the watch stage, dress it and ask whether it plays."""
        if self.stage != self.WATCH or self.window is None or not url.startswith("https://"):
            return
        try:
            status = self.window.evaluate_js(watch_script(self.look, fill=self.settings.fill))
        except Exception:
            status = None  # YouTube's page changed: the video at least shows, undressed
        if not is_watch_page(url):
            return  # YouTube's own cookie or sign-in page: the user's, with the ✕ to leave
        if isinstance(status, str) and status != "OK":
            self.watch_failed(status)
            return
        self._watch_shown = True

    def navigation_failed(self, reason: str) -> None:
        if self.stage == self.WATCH:
            self.watch_failed(reason)

    def watch_failed(self, reason: object) -> None:
        """The watch page can't play it either: the window closes, the browser opens."""
        with self._lock:
            if self.stage != self.WATCH:
                return
            self.stage = self.DONE
        self.outcome.say(event_line(EVENT_WATCH_ERROR, reason))
        self.outcome.code = EXIT_IN_BROWSER
        self.outcome.browser = True
        if self.window is not None:
            self.window.destroy()

    def set_fill(self, on: object) -> None:
        self.settings.fill = on is True
        self.settings.save()

    def _watch_timeout(self) -> None:
        if not self._watch_shown:
            self.watch_failed("timeout")


def _start_timer(seconds: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


class _Api:
    """What the pages call (`pywebview.api.close()` / `.failed(code)` / `.fill(on)`): every page
    the window shows can, YouTube's included, so nothing here does more than that."""

    def __init__(self, flow: TrailerFlow) -> None:
        self._flow = flow

    def close(self, *_ignored: object) -> None:
        if self._flow.window is not None:
            self._flow.window.destroy()

    def failed(self, code: object = "") -> None:
        self._flow.embed_failed(code)

    def fill(self, on: object = False) -> None:
        self._flow.set_fill(on)


# -- the process --------------------------------------------------------------------------------


class _PageServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, page: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _PageHandler)
        self.page = page

    @property
    def origin(self) -> str:
        return f"http://{PAGE_HOST}:{self.server_address[1]}"


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
    profile = profile_folder()
    settings = PlayerSettings.load(profile.parent / SETTINGS_FILE)

    server = _PageServer(b"")
    page = page_html(options.video_id, server.origin, options.look, fill=settings.fill)
    server.page = page.encode("utf-8")
    threading.Thread(target=server.serve_forever, name="trailer-page", daemon=True).start()

    outcome = _Outcome(ready_fd)
    flow = TrailerFlow(options.video_id, options.look, outcome, settings)
    window: Any = webview.create_window(
        options.look.title,
        server.origin + "/",
        js_api=_Api(flow),
        width=round(geometry.width / scale),  # pywebview's units; `_prepare` sets the pixels
        height=round(geometry.height / scale),
        frameless=True,
        easy_drag=False,
        resizable=False,  # also no maximising by a double click on the frame
        background_color=options.look.background,
    )
    flow.window = window
    window.events.before_show += lambda: _prepare(window, geometry, outcome, flow)
    window.events.loaded += outcome.ready
    window.events.loaded += lambda: flow.page_loaded(str(window.get_current_url() or ""))
    icon = Path(__file__).resolve().parent / "assets" / "airgrabber.ico"
    try:
        webview.start(
            _watch,
            (window, outcome),
            gui="edgechromium",
            private_mode=False,
            storage_path=str(profile),
            icon=str(icon) if icon.is_file() else None,
        )
    except Exception:
        return EXIT_FAILED
    if outcome.browser:
        webbrowser.open(WATCH_URL.format(id=options.video_id))
    return outcome.code


def profile_folder() -> Path:
    """WebView2's profile, kept between trailers (in the data folder: a trailer starts about
    two seconds sooner than with a new one each time); its cache capped by BROWSER_ARGUMENTS."""
    from app.config import AppPaths

    return AppPaths.default().root / PROFILE_FOLDER


def _quiet_output() -> int | None:
    """Keep stdout's pipe for the lines to the app only; everything else (pywebview's prints
    and logging) goes nowhere. The pipe's own descriptor, or None without one."""
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


def _prepare(window: Any, geometry: Geometry, outcome: _Outcome, flow: TrailerFlow) -> None:
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
            core.Settings.IsNonClientRegionSupportEnabled = True  # the frame drags natively
        except Exception:
            pass  # an older WebView2: pywebview's own drag region does it
        core.ContainsFullScreenElementChanged += lambda _sender, _args: full_screen.follow(
            bool(core.ContainsFullScreenElement)
        )
        core.NavigationCompleted += navigated
        _PointerWatch(hwnd, window).start()

    def navigated(_sender: Any, args: Any) -> None:
        status = str(args.WebErrorStatus)
        if not args.IsSuccess and status not in ("OperationCanceled", "ConnectionAborted"):
            flow.navigation_failed("navigation")

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


def controls_zone(width: int, scale: float) -> Rect:
    """Where the ✕ and Fill can be, in a window `width` physical pixels wide (left, top, right,
    bottom from its top-left corner): the top-right corner down to their active place. A pointer
    resting there keeps them active, so they never slide away from under it."""
    right = width - round(MARGIN * scale)
    left = right - round((BUTTON_INSET + 2 * CLOSE_SIZE + BUTTON_GAP + 8) * scale)
    bottom = round((MARGIN + ACTIVE_TOP + CLOSE_SIZE + 8) * scale)
    return left, 0, width, bottom


class _PointerWatch:
    """The pointer over the window (YouTube's cross-origin player never tells the page): the
    page hears `agActive(true)` when it moves there and `agActive(false)` after IDLE_S without
    movement (unless it rests on the buttons' corner: `controls_zone`), or when it leaves; again
    every REPEAT_S, as a new page (the watch page after the player's) starts out active."""

    POLL_S = 0.15
    REPEAT_S = 1.0

    def __init__(self, hwnd: int, window: Any) -> None:
        self._hwnd = hwnd
        self._window = window

    def start(self) -> None:
        threading.Thread(target=self._run, name="trailer-pointer", daemon=True).start()

    def _run(self) -> None:
        if sys.platform != "win32":
            return
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        point, last = wintypes.POINT(), (-1, -1)
        active, moved = True, time.monotonic()
        told = moved
        while user32.IsWindow(ctypes.c_void_p(self._hwnd)):
            time.sleep(self.POLL_S)
            user32.GetCursorPos(ctypes.byref(point))
            here = (point.x, point.y)
            left, top, width, height = _astuple(_window_geometry(self._hwnd))
            inside = left <= here[0] < left + width and top <= here[1] < top + height
            now = time.monotonic()
            if inside and here != last:
                moved = now
            last = here
            zone_left, zone_top, zone_right, zone_bottom = controls_zone(width, _scale_of(self._hwnd))
            x, y = here[0] - left, here[1] - top
            on_buttons = zone_left <= x < zone_right and zone_top <= y < zone_bottom
            wanted = inside and (on_buttons or now - moved < IDLE_S)
            if wanted != active or now - told >= self.REPEAT_S:
                active, told = wanted, now
                self._tell(active)

    def _tell(self, active: bool) -> None:
        script = f"window.agActive && window.agActive({'true' if active else 'false'})"
        try:
            run = getattr(self._window, "run_js", None)
            if run is not None:
                run(script)
            else:
                self._window.evaluate_js(script)
        except Exception:
            pass  # the window is closing


def _astuple(geometry: Geometry) -> tuple[int, int, int, int]:
    return geometry.x, geometry.y, geometry.width, geometry.height


def _scale_of(hwnd: int) -> float:
    """Windows' scaling for the window's monitor (1.5 at 150 %)."""
    try:
        dpi = int(ctypes.WinDLL("user32").GetDpiForWindow(ctypes.c_void_p(hwnd)))
    except (OSError, AttributeError):
        return 1.0
    return dpi / 96 if dpi > 0 else 1.0


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
