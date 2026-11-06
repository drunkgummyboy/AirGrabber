"""The trailer player: YouTube links → video ids, the player's command line and options, its
window's size and page, and TrailerPlayer's decisions (the player or the browser, one trailer
at a time, what `on_state` hears) with fake processes — no window, no pywebview, no network."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from app import trailer_player
from app.i18n import current_language
from app.trailer_player import (
    EXIT_CLOSED,
    EXIT_IN_BROWSER,
    EXIT_NO_WEBVIEW,
    EXIT_NO_WEBVIEW2,
    MARGIN,
    Geometry,
    Look,
    page_html,
    parse_options,
    player_geometry,
)
from app.ui import borderless, theme
from app.ui.trailer import (
    APP_ROOT,
    PLAYER_MODULE,
    TrailerPlayer,
    TrailerState,
    close_trailer,
    exit_text,
    player_available,
    player_command,
    player_look,
    youtube_id,
)

VIDEO = "Way9Dexny3w"
URL = f"https://www.youtube.com/watch?v={VIDEO}"


class YoutubeIdTests(unittest.TestCase):
    def test_youtube_links(self) -> None:
        for url in (
            URL,
            f"http://youtube.com/watch?v={VIDEO}&t=42s",
            f"https://m.youtube.com/watch?feature=share&v={VIDEO}",
            f"https://youtu.be/{VIDEO}",
            f"https://youtu.be/{VIDEO}?si=abc",
            f"https://www.youtube.com/embed/{VIDEO}?autoplay=1",
            f"https://www.youtube-nocookie.com/embed/{VIDEO}",
            f"https://www.youtube.com/shorts/{VIDEO}",
            f"  {URL}  ",
        ):
            with self.subTest(url=url):
                self.assertEqual(youtube_id(url), VIDEO)

    def test_ids_with_dash_and_underscore(self) -> None:
        self.assertEqual(youtube_id("https://www.youtube.com/watch?v=-a_b-C_d-E_"), "-a_b-C_d-E_")

    def test_anything_else(self) -> None:
        for url in (
            "",
            "not a link",
            "https://vimeo.com/123456789",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/watch?v=Way9Dexny3w0",  # 12 characters
            "https://www.youtube.com/watch?v=Way9Dexny3%",
            "https://www.youtube.com/channel/UCabcdefghij",
            "https://youtube.com.evil.example/watch?v=Way9Dexny3w",
            "ftp://www.youtube.com/watch?v=Way9Dexny3w",
            "javascript:alert(1)",
            "https://[::1/watch?v=Way9Dexny3w",  # an unreadable host
        ):
            with self.subTest(url=url):
                self.assertIsNone(youtube_id(url))


class CommandLineTests(unittest.TestCase):
    def test_command(self) -> None:
        command = player_command(VIDEO, "pythonw.exe", ["--tip", "Close (Esc)"])
        self.assertEqual(
            command, ["pythonw.exe", "-m", PLAYER_MODULE, "--tip", "Close (Esc)", "--", VIDEO]
        )
        self.assertEqual(PLAYER_MODULE, trailer_player.__name__)

    def test_the_player_reads_it_back(self) -> None:
        look = ["--background", "#101010", "--button", "#202020", "--tip", "Sluiten (Esc)"]
        for video in (VIDEO, "-a_b-C_d-E_"):  # an id may start with "-"
            with self.subTest(video=video):
                options = parse_options(player_command(video, "python", look)[3:])
                assert options is not None
                self.assertEqual(options.video_id, video)
                self.assertEqual(options.look.background, "#101010")
                self.assertEqual(options.look.button, "#202020")
                self.assertEqual(options.look.tip, "Sluiten (Esc)")
                self.assertEqual(options.look.glyph, Look().glyph)  # not given: the default

    def test_bad_options(self) -> None:
        self.assertIsNone(parse_options([]))
        self.assertIsNone(parse_options(["--", "not-an-id"]))
        self.assertIsNone(parse_options(["--", "<script>abc"]))
        options = parse_options(
            ["--background", "red; x", "--glyph-hover", "#12345", "--language", '"x', "--", VIDEO]
        )
        assert options is not None
        self.assertEqual(options.look.background, Look().background)  # refused: the default
        self.assertEqual(options.look.glyph_hover, Look().glyph_hover)
        self.assertEqual(options.look.language, "en")

    def test_look_is_the_dark_theme(self) -> None:
        options = player_look()
        values = dict(zip(options[::2], options[1::2], strict=True))
        self.assertEqual(values["--background"], theme.pick(theme.BG, dark=True).upper())
        self.assertEqual(values["--button"], "#1E262E")  # CHIP's dark
        self.assertEqual(values["--button-hover"], theme.pick(theme.CLOSE_BUTTON_HOVER, dark=True))
        self.assertEqual(values["--glyph-hover"], "#FFFFFF")
        self.assertEqual(values["--tip"], "Close (Esc)")
        self.assertEqual(values["--fill-tip"], "Fill the window")
        self.assertEqual(values["--fit-tip"], "Show the whole picture")
        self.assertEqual(values["--language"], current_language().value)  # YouTube's player's `hl`
        parsed = parse_options([*options, "--", VIDEO])
        assert parsed is not None
        self.assertEqual(parsed.look.button, "#1E262E")
        self.assertEqual(parsed.look.language, current_language().value)
        self.assertEqual((parsed.look.fill_tip, parsed.look.fit_tip), ("Fill the window", "Show the whole picture"))

    def test_fill_texts_in_dutch(self) -> None:
        from app.i18n import Language, set_language

        try:
            set_language(Language.NL)
            values = dict(zip(player_look()[::2], player_look()[1::2], strict=True))
        finally:
            set_language(Language.EN)
        self.assertEqual((values["--fill-tip"], values["--fit-tip"]), ("Venster vullen", "Hele beeld tonen"))
        self.assertEqual(values["--tip"], "Sluiten (Esc)")

    def test_the_player_without_pywebview(self) -> None:
        # In the player's process: pywebview can't be imported -> its exit code, and the app
        # opens the browser (TrailerPlayerTests.test_a_player_that_ends_before_its_page_loads).
        with (
            mock.patch.dict(sys.modules, {"webview": None}),  # `import webview` raises ImportError
            mock.patch.dict(os.environ),
            mock.patch.object(trailer_player, "_quiet_output", return_value=None),
            mock.patch.object(trailer_player, "_per_monitor_dpi"),
        ):
            self.assertEqual(trailer_player.main(["--", VIDEO]), EXIT_NO_WEBVIEW)
        self.assertEqual(trailer_player.main(["--", "bad"]), trailer_player.EXIT_USAGE)

    def test_close_button_matches_the_pop_ups(self) -> None:
        self.assertEqual(trailer_player.CLOSE_SIZE, borderless.CLOSE_SIZE)
        self.assertEqual(trailer_player.CLOSE_FONT_SIZE, borderless.CLOSE_FONT_SIZE)
        self.assertEqual(trailer_player.CLOSE_GLYPH, borderless.CLOSE_GLYPH)

    def test_exit_text(self) -> None:
        self.assertEqual(exit_text(EXIT_NO_WEBVIEW2), "exit code 4: no WebView2")
        self.assertEqual(exit_text(77), "exit code 77")
        self.assertEqual(exit_text(None), "no answer")

    def test_available_only_on_windows(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(player_available())
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch("importlib.util.find_spec", return_value=None),
        ):
            self.assertFalse(player_available())


class GeometryTests(unittest.TestCase):
    def test_seventy_percent_wide_and_centred_with_an_even_frame(self) -> None:
        geometry = player_geometry((0, 0, 2560, 1552), 1.5)
        self.assertEqual(geometry.width, 1792)
        margin = round(MARGIN * 1.5)
        video_width, video_height = geometry.width - 2 * margin, geometry.height - 2 * margin
        self.assertAlmostEqual(video_width / video_height, 16 / 9, places=2)  # the same frame all round
        self.assertEqual(geometry.x, (2560 - geometry.width) // 2)
        self.assertEqual(geometry.y, (1552 - geometry.height) // 2)
        self.assertTrue(18 <= MARGIN <= 20)

    def test_a_short_screen_limits_the_height(self) -> None:
        geometry = player_geometry((0, 0, 3440, 1400), 1.0)
        self.assertLessEqual(geometry.height, int(1400 * trailer_player.MAX_HEIGHT_SHARE))
        self.assertLess(geometry.width, round(3440 * trailer_player.WIDTH_SHARE))
        video_width, video_height = geometry.width - 2 * MARGIN, geometry.height - 2 * MARGIN
        self.assertAlmostEqual(video_width / video_height, 16 / 9, places=2)

    def test_on_a_second_monitor(self) -> None:
        geometry = player_geometry((-1920, 0, 0, 1040), 1.0)
        self.assertEqual(geometry.width, 1344)
        self.assertEqual(geometry.x, -1920 + (1920 - 1344) // 2)
        self.assertIsInstance(geometry, Geometry)


class PageTests(unittest.TestCase):
    def test_page(self) -> None:
        look = Look(tip='Close "now" </script><b>', title="AirGrabber & co")
        page = page_html(VIDEO, "http://localhost:50123", look)
        self.assertIn('content="strict-origin-when-cross-origin"', page)  # YouTube's Error 153
        self.assertIn('origin: "http://localhost:50123"', page)
        self.assertIn(f'videoId: "{VIDEO}"', page)
        self.assertIn('host: "https://www.youtube-nocookie.com"', page)
        self.assertIn("autoplay: 1", page)
        self.assertIn('"tip": "Close \\"now\\" <\\/script><b>"', page)  # in JSON, can't end the script
        self.assertEqual(page.count("</script>"), 2)  # ours only
        self.assertIn("<title>AirGrabber &amp; co</title>", page)
        self.assertIn("app-region: drag", page)  # the frame's four strips
        self.assertIn('agApp("failed"', page)
        self.assertIn(look.button_hover, page)
        self.assertIn('cc_load_policy: 0, hl: "en"', page)  # captions not forced on
        self.assertNotIn("$", page)  # every placeholder filled
        self.assertNotIn("innerHTML", page)  # YouTube's pages refuse it (Trusted Types)
        self.assertIn('hl: "nl"', page_html(VIDEO, "http://localhost:1", Look(language="nl")))
        self.assertIn('"fill": true', page_html(VIDEO, "http://localhost:1", look, fill=True))

    def test_the_buttons_keep_clear_of_youtubes_top_bar(self) -> None:
        page = page_html(VIDEO, "http://localhost:1", Look())
        top = MARGIN + trailer_player.ACTIVE_TOP
        self.assertTrue(64 <= trailer_player.ACTIVE_TOP <= 72)
        # down below YouTube's volume / CC / settings while they show: the pointer moving, or
        # the video not playing (which it isn't yet when the page opens)
        self.assertIn(f"html.ag-active #ag-controls, html.ag-paused #ag-controls {{ top: {top}px; }}", page)
        self.assertIn('<html class="ag-paused">', page)
        self.assertIn('toggle("ag-paused", event.data !== 1 && event.data !== 3)', page)
        self.assertIn(f"top: {MARGIN + trailer_player.BUTTON_INSET}px", page)  # idle: the corner
        self.assertIn(f"top .25s ease {trailer_player.SLIDE_BACK_DELAY_S}s", page)  # back a bit later
        self.assertIn("html.ag-fill:not(.ag-active):not(.ag-paused) #video iframe", page)
        # the watch page's player has nothing up there: its buttons stay in the corner
        self.assertNotIn("html.ag-active #ag-controls, html.ag-paused", trailer_player.watch_script(Look(), fill=False))

    def test_a_pointer_resting_on_the_buttons_keeps_them(self) -> None:
        left, top, right, bottom = trailer_player.controls_zone(1792, 1.5)
        self.assertEqual((top, right), (0, 1792))
        self.assertEqual(left, 1792 - round(MARGIN * 1.5) - round((10 + 2 * 34 + 8 + 8) * 1.5))
        self.assertEqual(bottom, round((MARGIN + trailer_player.ACTIVE_TOP + 34 + 8) * 1.5))
        # YouTube's settings gear (99 px from the right at 150 %) is in it, its CC (170) isn't
        self.assertTrue(left <= 1792 - 99 < right)
        self.assertFalse(left <= 1792 - 170 < right)

    def test_the_page_comes_from_localhost(self) -> None:
        # Some videos refuse an IP address as the embedding page (error 150): never 127.0.0.1.
        server = trailer_player._PageServer(b"page")
        try:
            self.assertEqual(server.origin, f"http://localhost:{server.server_address[1]}")
        finally:
            server.server_close()
        self.assertEqual(trailer_player.PAGE_HOST, "localhost")

    def test_the_watch_page_script(self) -> None:
        script = trailer_player.watch_script(Look(tip="Sluiten (Esc)"), fill=True)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn(".click(", script)  # YouTube's cookie question is the user's to answer
        self.assertNotIn("dispatchEvent(new MouseEvent", script)
        self.assertIn("ytd-consent-bump-v2-lightbox *", script)  # kept visible, never hidden
        self.assertIn("playabilityStatus", script)
        self.assertIn('"fill": true', script)
        self.assertIn("Sluiten (Esc)", script)

    def test_event_lines_never_carry_more_than_a_code(self) -> None:
        line = trailer_player.event_line
        self.assertEqual(line("embed-error", 150), "embed-error 150")
        self.assertEqual(line("watch-error", "UNPLAYABLE"), "watch-error UNPLAYABLE")
        self.assertEqual(line("watch-error", f"https://www.youtube.com/watch?v={VIDEO}"), "watch-error unknown")
        self.assertEqual(line("embed-error", "a b"), "embed-error unknown")

    def test_watch_page_urls(self) -> None:
        self.assertEqual(trailer_player.watch_url(VIDEO, "nl"), f"https://www.youtube.com/watch?v={VIDEO}&hl=nl")
        self.assertTrue(trailer_player.is_watch_page(f"https://www.youtube.com/watch?v={VIDEO}&themeRefresh=1"))
        for url in ("https://consent.youtube.com/m?continue=x", "https://accounts.google.com/", "http://localhost:1/"):
            self.assertFalse(trailer_player.is_watch_page(url), url)


class FakeWindow:
    """pywebview's window for TrailerFlow: notes what it is asked; `evaluate_js` answers with
    `status` (or raises it when it is an exception)."""

    def __init__(self, status: object = "OK") -> None:
        self.status = status
        self.loaded: list[str] = []
        self.scripts: list[str] = []
        self.destroyed = False

    def load_url(self, url: str) -> None:
        self.loaded.append(url)

    def evaluate_js(self, script: str) -> object:
        self.scripts.append(script)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def destroy(self) -> None:
        self.destroyed = True


class FlowHarness:
    def __init__(self, tmp: Path, status: object = "OK") -> None:
        self.read_fd, write_fd = os.pipe()
        self.outcome = trailer_player._Outcome(write_fd)
        self.settings = trailer_player.PlayerSettings(tmp / "trailer-player.json")
        self.timers: list[tuple[float, object]] = []
        self.flow = trailer_player.TrailerFlow(
            VIDEO, Look(language="nl"), self.outcome, self.settings, timer=self.timer
        )
        self.window = FakeWindow(status)
        self.flow.window = self.window
        self._write_fd = write_fd

    def timer(self, seconds: float, action: object) -> None:
        self.timers.append((seconds, action))

    def lines(self) -> list[str]:
        os.close(self._write_fd)
        with os.fdopen(self.read_fd, "rb") as pipe:
            return pipe.read().decode().splitlines()


class TrailerFlowTests(unittest.TestCase):
    """The player's fallbacks: the embedded player, YouTube's watch page in the same window, the
    browser last; YouTube's error numbers to the app; YouTube's cookie question never answered."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

    def watch(self, harness: FlowHarness) -> str:
        return f"https://www.youtube.com/watch?v={VIDEO}&hl=nl"

    def test_an_embed_error_opens_the_watch_page_in_the_window(self) -> None:
        harness = FlowHarness(self.tmp)
        harness.flow.embed_failed("150")
        harness.flow.embed_failed("150")  # once
        self.assertEqual(harness.window.loaded, [self.watch(harness)])
        harness.flow.page_loaded(self.watch(harness))
        self.assertEqual(len(harness.window.scripts), 1)
        self.assertFalse(harness.window.destroyed)  # it plays there
        self.assertEqual(harness.outcome.code, EXIT_CLOSED)
        self.assertFalse(harness.outcome.browser)
        self.assertEqual(harness.lines(), ["ready", "embed-error 150"])

    def test_the_browser_only_when_the_watch_page_cannot_play_it(self) -> None:
        harness = FlowHarness(self.tmp, status="UNPLAYABLE")
        harness.flow.embed_failed(101)
        harness.flow.page_loaded(self.watch(harness))
        self.assertTrue(harness.window.destroyed)
        self.assertEqual(harness.outcome.code, EXIT_IN_BROWSER)
        self.assertTrue(harness.outcome.browser)  # opened by main once the window is gone
        self.assertEqual(harness.lines(), ["ready", "embed-error 101", "watch-error UNPLAYABLE"])

    def test_a_failed_navigation_or_a_slow_page(self) -> None:
        harness = FlowHarness(self.tmp)
        harness.flow.navigation_failed("navigation")  # the player's own page: not this flow's
        self.assertFalse(harness.window.destroyed)
        harness.flow.embed_failed("5")
        harness.flow.navigation_failed("navigation")
        self.assertTrue(harness.window.destroyed)
        self.assertEqual(harness.lines(), ["ready", "embed-error 5", "watch-error navigation"])

        slow = FlowHarness(self.tmp)
        slow.flow.embed_failed("150")
        seconds, action = slow.timers[0]
        self.assertEqual(seconds, trailer_player.WATCH_TIMEOUT_S)
        assert callable(action)
        action()
        self.assertTrue(slow.outcome.browser)
        self.assertEqual(slow.lines()[-1], "watch-error timeout")

        shown = FlowHarness(self.tmp)
        shown.flow.embed_failed("150")
        shown.flow.page_loaded(self.watch(shown))
        action = shown.timers[0][1]
        assert callable(action)
        action()  # it showed in time: the timer does nothing
        self.assertFalse(shown.window.destroyed)

    def test_youtube_cookie_question_is_left_to_the_user(self) -> None:
        harness = FlowHarness(self.tmp, status=None)
        harness.flow.embed_failed("150")
        harness.flow.page_loaded("https://consent.youtube.com/m?continue=watch")
        self.assertFalse(harness.window.destroyed)  # the user's page, with the ✕ to leave
        harness.flow.page_loaded(self.watch(harness))  # the question as a dialog on the page
        self.assertFalse(harness.window.destroyed)
        for script in harness.window.scripts:
            self.assertNotIn(".click(", script)
            self.assertNotIn("Akkoord", script)
            self.assertNotIn("Accept", script)

    def test_a_changed_youtube_page_still_shows_the_video(self) -> None:
        harness = FlowHarness(self.tmp, status=RuntimeError("TypeError in YouTube's page"))
        harness.flow.embed_failed("150")
        harness.flow.page_loaded(self.watch(harness))
        self.assertFalse(harness.window.destroyed)

    def test_nothing_happens_for_the_players_own_page(self) -> None:
        harness = FlowHarness(self.tmp)
        harness.flow.page_loaded("http://localhost:50123/")
        harness.flow.page_loaded(self.watch(harness))  # still the embedded stage
        self.assertEqual(harness.window.scripts, [])
        self.assertEqual(harness.window.loaded, [])

    def test_fill_is_remembered(self) -> None:
        harness = FlowHarness(self.tmp)
        api = trailer_player._Api(harness.flow)
        api.fill(True)
        self.assertTrue(trailer_player.PlayerSettings.load(self.tmp / "trailer-player.json").fill)
        api.fill(False)
        self.assertFalse(trailer_player.PlayerSettings.load(self.tmp / "trailer-player.json").fill)
        api.fill("yes")  # only a real true turns it on
        self.assertFalse(trailer_player.PlayerSettings.load(self.tmp / "trailer-player.json").fill)
        (self.tmp / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(trailer_player.PlayerSettings.load(self.tmp / "broken.json").fill)
        api.close()
        self.assertTrue(harness.window.destroyed)


class EventLogTests(unittest.TestCase):
    def test_the_app_logs_the_players_codes_only(self) -> None:
        from app.ui.trailer import log_event

        with self.assertLogs("app.ui.trailer", "INFO") as logs:
            log_event(b"embed-error 150\n")
            log_event(b"watch-error UNPLAYABLE\n")
            log_event(f"embed-error {VIDEO} https://www.youtube.com/watch?v={VIDEO}".encode())
            log_event(f"embed-error {VIDEO}".encode())  # never an id, even alone
            log_event(f"watch-error {VIDEO}".encode())
            log_event(b"something else\n")
        self.assertEqual(len(logs.records), 2)
        self.assertIn("(error 150)", logs.output[0])
        self.assertIn("(UNPLAYABLE)", logs.output[1])
        self.assertNotIn("youtube.com/watch", "\n".join(logs.output))


class FakeInput:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeOutput:
    def __init__(self) -> None:
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._eof = False

    async def readline(self) -> bytes:
        if self._eof:
            return b""  # like asyncio's StreamReader after the end
        chunk = await self.chunks.get()
        self._eof = chunk == b""
        return chunk

    async def read(self, n: int = -1) -> bytes:
        return await self.readline()


class FakeProcess:
    """A player process: says "ready" (or not), ends when told, or when its stdin closes
    (`ends_on_close`)."""

    def __init__(self, *, ends_on_close: bool = True) -> None:
        self.stdin = FakeInput()
        self.stdout = FakeOutput()
        self.returncode: int | None = None
        self.killed = False
        self._ends_on_close = ends_on_close
        self._ended = asyncio.Event()

    def say_ready(self) -> None:
        self.stdout.chunks.put_nowait(b"ready\n")

    def end(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self.stdout.chunks.put_nowait(b"")
            self._ended.set()

    async def wait(self) -> int:
        await self._ended.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.end(1)

    def check_closed(self) -> None:
        if self.stdin.closed and self._ends_on_close:
            self.end(0)


class Harness:
    def __init__(self, *, available: bool | None = True, fail: Exception | None = None) -> None:
        self.processes: list[FakeProcess] = []
        self.commands: list[tuple[list[str], Path]] = []
        self.opened: list[str] = []
        self.states: list[str] = []
        self.fail = fail
        self.player = TrailerPlayer(
            starter=self.start,
            opener=self.opened.append,
            available=player_available if available is None else (lambda: available),
            look=lambda: ["--tip", "Close (Esc)"],
            python="pythonw.exe",
            ready_timeout=0.2,
        )

    def open(self, url: str, label: str = "") -> None:
        """Open with a listener that notes "<label><state>"."""

        def listen(state: TrailerState) -> None:
            self.states.append(label + state)

        self.player.open(url, listen)

    async def start(self, command: Sequence[str], cwd: Path) -> FakeProcess:
        self.commands.append((list(command), cwd))
        if self.fail is not None:
            raise self.fail
        process = FakeProcess()
        self.processes.append(process)
        return process


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class TrailerPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plays_in_its_own_window(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "INFO") as logs:
            harness.player.open(URL)
            await settle()
            process = harness.processes[0]
            process.say_ready()
            await settle()
            self.assertIs(harness.player.process, process)
            self.assertEqual(harness.opened, [])
            process.end(0)  # the user closed it
            await settle()
        command, cwd = harness.commands[0]
        self.assertEqual(command, ["pythonw.exe", "-m", PLAYER_MODULE, "--tip", "Close (Esc)", "--", VIDEO])
        self.assertEqual(cwd, APP_ROOT)
        self.assertTrue((APP_ROOT / "app" / "trailer_player.py").is_file())
        self.assertIsNone(harness.player.process)
        task = harness.player.task
        assert task is not None
        self.assertTrue(task.done())
        self.assertEqual(harness.opened, [])
        for record in logs.records:
            self.assertNotIn(VIDEO, record.getMessage())

    async def test_not_youtube_opens_the_browser(self) -> None:
        harness = Harness()
        harness.player.open("https://vimeo.com/123456789")
        await settle()
        self.assertEqual(harness.opened, ["https://vimeo.com/123456789"])
        self.assertEqual(harness.commands, [])

    async def test_without_pywebview_the_browser(self) -> None:
        harness = Harness(available=False)
        with self.assertLogs("app.ui.trailer", "INFO") as logs:
            harness.player.open(URL)
            await settle()
        self.assertEqual(harness.opened, [URL])
        self.assertEqual(harness.commands, [])
        self.assertNotIn(VIDEO, "\n".join(logs.output))

    async def test_a_player_that_cannot_start(self) -> None:
        harness = Harness(fail=FileNotFoundError("pythonw.exe"))
        with self.assertLogs("app.ui.trailer", "WARNING") as logs:
            harness.player.open(URL)
            await settle()
        self.assertEqual(harness.opened, [URL])
        self.assertIn("FileNotFoundError", logs.output[0])
        self.assertIsNone(harness.player.process)

    async def test_a_player_that_ends_before_its_page_loads(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "WARNING") as logs:
            harness.player.open(URL)
            await settle()
            harness.processes[0].end(EXIT_NO_WEBVIEW2)
            await settle()
        self.assertEqual(harness.opened, [URL])  # once
        self.assertIn("exit code 4: no WebView2", logs.output[0])
        self.assertNotIn(VIDEO, "\n".join(logs.output))
        self.assertNotIn("youtube", "\n".join(logs.output).lower())
        self.assertIsNone(harness.player.process)

    async def test_a_silent_player_is_ended(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "WARNING"):
            harness.player.open(URL)
            await asyncio.sleep(0.4)  # past the 0.2 s ready timeout
            await settle()
        self.assertTrue(harness.processes[0].killed)
        self.assertEqual(harness.opened, [URL])

    async def test_one_trailer_at_a_time(self) -> None:
        harness = Harness()
        harness.player.open(URL)
        await settle()
        first = harness.processes[0]
        first.say_ready()
        await settle()
        first_task = harness.player.task
        harness.player.open("https://youtu.be/-a_b-C_d-E_")
        await settle()
        self.assertTrue(first.stdin.closed)  # it closes its window and ends
        self.assertFalse(first.killed)
        assert first_task is not None
        self.assertTrue(first_task.cancelled())
        second = harness.processes[1]
        second.say_ready()
        await settle()
        self.assertIs(harness.player.process, second)
        self.assertFalse(second.stdin.closed)
        self.assertEqual(harness.commands[1][0][-1], "-a_b-C_d-E_")
        self.assertEqual(harness.opened, [])
        first.check_closed()
        await settle()
        self.assertIs(harness.player.process, second)  # the old one ending changes nothing

    async def test_replaced_while_starting(self) -> None:
        harness = Harness()
        harness.player.open(URL)
        await settle()
        harness.player.open(URL)
        await settle()
        self.assertTrue(harness.processes[0].stdin.closed)
        self.assertEqual(len(harness.processes), 2)
        self.assertIs(harness.player.process, harness.processes[1])
        self.assertEqual(harness.opened, [])

    async def test_a_link_elsewhere_closes_the_trailer_too(self) -> None:
        harness = Harness()
        harness.player.open(URL)
        await settle()
        harness.processes[0].say_ready()
        await settle()
        harness.player.open("https://vimeo.com/123456789")
        await settle()
        self.assertTrue(harness.processes[0].stdin.closed)
        self.assertEqual(harness.opened, ["https://vimeo.com/123456789"])

    async def test_close_and_shutting_down(self) -> None:
        harness = Harness()
        harness.player.open(URL)
        await settle()
        harness.processes[0].say_ready()
        await settle()
        harness.player.close()
        await settle()
        self.assertTrue(harness.processes[0].stdin.closed)
        self.assertIsNone(harness.player.process)
        harness.player.close()  # nothing showing: nothing happens

        harness.player.open(URL)
        await settle()
        harness.processes[1].say_ready()
        await settle()
        task = harness.player.task
        assert task is not None
        task.cancel()  # what tasks.cancel_all does when AirGrabber shuts down
        await settle()
        self.assertTrue(harness.processes[1].stdin.closed)
        self.assertIsNone(harness.player.process)

    async def test_module_close_without_a_player(self) -> None:
        close_trailer()  # never opened: nothing to do

    async def test_pywebview_not_installed_opens_the_browser(self) -> None:
        # The real availability check: pywebview not found (it could not be installed).
        harness = Harness(available=None)
        with (
            mock.patch("importlib.util.find_spec", return_value=None),
            self.assertLogs("app.ui.trailer", "INFO"),
        ):
            harness.open(URL)
            await settle()
        self.assertEqual(harness.opened, [URL])
        self.assertEqual(harness.commands, [])
        self.assertEqual(harness.states, ["failed"])

    async def test_a_player_without_pywebview_ends_at_once(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "WARNING") as logs:
            harness.open(URL)
            await settle()
            harness.processes[0].end(EXIT_NO_WEBVIEW)  # pywebview / pythonnet can't be loaded
            await settle()
        self.assertEqual(harness.opened, [URL])
        self.assertIn("exit code 3: pywebview can't be loaded", logs.output[0])
        self.assertEqual(harness.states, ["opening", "failed"])


class TrailerStateTests(unittest.IsolatedAsyncioTestCase):
    """What `on_state` hears: opening -> playing -> closed, or failed when the trailer went to
    the browser instead."""

    async def test_played_then_closed(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "INFO"):
            harness.open(URL)
            self.assertEqual(harness.states, ["opening"])  # at once, on the loop
            await settle()
            harness.processes[0].say_ready()
            await settle()
            self.assertEqual(harness.states, ["opening", "playing"])
            harness.processes[0].end(0)
            await settle()
        self.assertEqual(harness.states, ["opening", "playing", "closed"])

    async def test_the_browser_at_once(self) -> None:
        for available, url in ((True, "https://vimeo.com/123456789"), (False, URL)):
            with self.subTest(available=available):
                harness = Harness(available=available)
                with self.assertLogs("app.ui.trailer", "INFO"):
                    harness.open(url)
                await settle()
                self.assertEqual(harness.states, ["failed"])

    async def test_the_player_did_not_come_up(self) -> None:
        cases: list[tuple[str, Exception | None]] = [("cannot start", OSError("no")), ("ended", None)]
        for name, fail in cases:
            with self.subTest(name=name):
                harness = Harness(fail=fail)
                with self.assertLogs("app.ui.trailer", "WARNING"):
                    harness.open(URL)
                    await settle()
                    if fail is None:
                        harness.processes[0].end(EXIT_NO_WEBVIEW2)
                        await settle()
                self.assertEqual(harness.states, ["opening", "failed"])
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "WARNING"):
            harness.open(URL)
            await asyncio.sleep(0.4)  # silent past the ready timeout
            await settle()
        self.assertEqual(harness.states, ["opening", "failed"])

    async def test_the_players_events_reach_the_log(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "INFO") as logs:
            harness.open(URL)
            await settle()
            process = harness.processes[0]
            process.say_ready()
            process.stdout.chunks.put_nowait(b"embed-error 150\n")  # then the watch page plays
            await settle()
            self.assertEqual(harness.states, ["opening", "playing"])
            process.stdout.chunks.put_nowait(b"watch-error UNPLAYABLE\n")
            process.end(EXIT_IN_BROWSER)
            await settle()
        text = "\n".join(logs.output)
        self.assertIn("refused the trailer (error 150)", text)
        self.assertIn("can't play the trailer either (UNPLAYABLE)", text)
        self.assertNotIn(VIDEO, text)
        self.assertEqual(harness.states, ["opening", "playing", "failed"])
        self.assertEqual(harness.opened, [])  # the player opened the browser, not the app

    async def test_youtube_refused_it_in_the_window(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "INFO"):
            harness.open(URL)
            await settle()
            harness.processes[0].say_ready()
            await settle()
            harness.processes[0].end(EXIT_IN_BROWSER)  # the player opened it in the browser
            await settle()
        self.assertEqual(harness.states, ["opening", "playing", "failed"])
        self.assertEqual(harness.opened, [])  # the player did that, not the app as well

    async def test_replaced_the_old_one_hears_closed_first(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "INFO"):
            harness.open(URL, "first ")
            await settle()
            harness.processes[0].say_ready()
            await settle()
            harness.open(URL, "second ")
            await settle()
            harness.processes[0].check_closed()  # it ends after its stdin closed
            await settle()
        self.assertEqual(harness.states, ["first opening", "first playing", "first closed", "second opening"])

    async def test_shutting_down_and_close(self) -> None:
        harness = Harness()
        with self.assertLogs("app.ui.trailer", "INFO"):
            harness.open(URL)
            await settle()
            harness.processes[0].say_ready()
            await settle()
        task = harness.player.task
        assert task is not None
        task.cancel()  # tasks.cancel_all
        await settle()
        self.assertEqual(harness.states, ["opening", "playing", "closed"])
        harness.states.clear()
        harness.open(URL)
        await settle()
        harness.player.close()  # while it is still starting
        await settle()
        self.assertEqual(harness.states, ["opening", "closed"])

    async def test_a_failing_listener_is_logged(self) -> None:
        harness = Harness()

        def broken(_state: TrailerState) -> None:
            raise RuntimeError("widget gone")

        with self.assertLogs("app.ui.trailer", "ERROR") as logs:
            harness.player.open(URL, broken)
            await settle()
            harness.processes[0].say_ready()
            await settle()
            harness.processes[0].end(0)
            await settle()
        self.assertIn("A trailer state listener failed", logs.output[0])
        self.assertIsNone(harness.player.process)  # it carried on regardless


if __name__ == "__main__":
    unittest.main()
