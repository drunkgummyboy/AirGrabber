"""AirGrabber's qBittorrent theme (app/assets/qbittorrent-theme, app/services/qbittorrent_theme.py):
config.json holds only colour ids the targeted qBittorrent versions read, with valid colours from
AirGrabber's own tokens (light in `colors`, dark in `colors.dark`) and no palette override that
would stop qBittorrent 5 following Windows' light / dark; stylesheet.qss parses as plain Qt style
sheet text, uses known properties, AirGrabber's colours and palette roles only, and references no
file that isn't shipped; and the copy into AirGrabber's data folder (temporary folders here)."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from app.services import qbittorrent_theme as qt
from app.services.qbittorrent_theme import (
    CONFIG_NAME,
    SOURCE_DIR,
    STYLESHEET_NAME,
    ThemeFolder,
    shipped_files,
)
from app.ui import theme

# The colour ids each qBittorrent version reads (its source: src/gui/uithemecommon.h
# `defaultUIThemeColors` / `defaultPaletteColors` from 4.6; transferlistmodel.cpp, log/logmodel.cpp,
# rss/articlelistwidget.cpp and uithememanager.cpp `applyPalette` in 4.4.5 / 4.5.5).
_PALETTE = {
    f"Palette.{role}"
    for role in (
        "Window WindowText Base AlternateBase Text ToolTipBase ToolTipText BrightText Highlight "
        "HighlightedText Button ButtonText Link LinkVisited Light Midlight Mid Dark Shadow "
        "WindowTextDisabled TextDisabled ToolTipTextDisabled BrightTextDisabled "
        "HighlightedTextDisabled ButtonTextDisabled"
    ).split()
}
_LOG_RSS = {
    "Log.TimeStamp", "Log.Normal", "Log.Info", "Log.Warning", "Log.Critical", "Log.BannedPeer",
    "RSS.ReadArticle", "RSS.UnreadArticle",
}  # fmt: skip
_STATES = {
    f"TransferList.{state}"
    for state in (
        "Downloading StalledDownloading DownloadingMetadata ForcedDownloadingMetadata "
        "ForcedDownloading Uploading StalledUploading ForcedUploading QueuedDownloading "
        "QueuedUploading CheckingDownloading CheckingUploading CheckingResumeData Moving "
        "MissingFiles Error"
    ).split()
}
_PAUSED = {"TransferList.PausedDownloading", "TransferList.PausedUploading"}  # 4.x
_STOPPED = {"TransferList.StoppedDownloading", "TransferList.StoppedUploading"}  # 5.x
_BARS = {
    "PiecesBar.Border", "PiecesBar.Piece", "PiecesBar.PartialPiece", "PiecesBar.MissingPiece",
    "ProgressBar",
}  # fmt: skip
KNOWN_IDS: dict[str, set[str]] = {
    "4.4 – 4.6": _PALETTE | _LOG_RSS | _STATES | _PAUSED,
    "5.0 – 5.1": _PALETTE | _LOG_RSS | _STATES | _STOPPED,
    "5.2 – 5.3": _PALETTE | _LOG_RSS | _STATES | _STOPPED | _BARS,
}
# Palette roles that suit a light and a dark palette alike: anything else (Window, Base, Text…)
# is applied in both modes and would freeze qBittorrent 5 in one of them.
MODE_FREE_PALETTE = {"Palette.Highlight", "Palette.HighlightedText"}

_HEX = re.compile(r"^#[0-9A-F]{6}$")


def _token_colors() -> tuple[set[str], set[str]]:
    """The hex colours of AirGrabber's tokens: (light halves and single colours, dark halves
    and single colours)."""
    light: set[str] = set()
    dark: set[str] = set()
    for value in vars(theme).values():
        if isinstance(value, str) and value.startswith("#"):
            light.add(value.upper())
            dark.add(value.upper())
        elif (
            isinstance(value, tuple)
            and len(value) == 2
            and all(isinstance(part, str) for part in value)
        ):
            first, second = value
            if first.startswith("#"):
                light.add(first.upper())
            if second.startswith("#"):
                dark.add(second.upper())
    return light, dark


LIGHT_TOKENS, DARK_TOKENS = _token_colors()


def _config() -> dict[str, dict[str, str]]:
    data = json.loads((SOURCE_DIR / CONFIG_NAME).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class ConfigTests(unittest.TestCase):
    def test_only_the_sections_qbittorrent_reads(self) -> None:
        self.assertEqual(set(_config()), {"colors", "colors.dark"})

    def test_only_known_ids_with_valid_colours(self) -> None:
        known = set().union(*KNOWN_IDS.values())
        for section, colors in _config().items():
            for color_id, value in colors.items():
                with self.subTest(section=section, id=color_id):
                    self.assertIn(color_id, known)
                    self.assertRegex(value, _HEX)  # QColor reads "#RRGGBB"

    def test_every_state_has_a_light_and_a_dark_colour_in_every_version(self) -> None:
        config = _config()
        for version, ids in KNOWN_IDS.items():
            wanted = {color_id for color_id in ids if color_id.startswith("TransferList.")}
            for section in ("colors", "colors.dark"):
                with self.subTest(version=version, section=section):
                    self.assertLessEqual(wanted, set(config[section]))
        # The same ids in both sections, the palette aside (read from `colors` only).
        self.assertEqual(set(config["colors"]) - MODE_FREE_PALETTE, set(config["colors.dark"]))

    def test_the_palette_keeps_following_windows(self) -> None:
        config = _config()
        palette = {color_id for color_id in config["colors"] if color_id.startswith("Palette.")}
        self.assertLessEqual(palette, MODE_FREE_PALETTE)
        self.assertFalse(any(color_id.startswith("Palette.") for color_id in config["colors.dark"]))

    def test_the_colours_are_airgrabbers_tokens(self) -> None:
        config = _config()
        for section, tokens in (("colors", LIGHT_TOKENS), ("colors.dark", DARK_TOKENS)):
            for color_id, value in config[section].items():
                with self.subTest(section=section, id=color_id):
                    self.assertIn(value.upper(), tokens)
        # A few by name: green downloading, amber paused, red errors, slate-blue accent.
        light, dark = config["colors"], config["colors.dark"]
        self.assertEqual((light["TransferList.Downloading"], dark["TransferList.Downloading"]), theme.SUCCESS)
        self.assertEqual((light["TransferList.StoppedDownloading"], dark["TransferList.StoppedDownloading"]), theme.WARNING)
        self.assertEqual((light["TransferList.Error"], dark["TransferList.Error"]), theme.DANGER)
        self.assertEqual(light["Palette.Highlight"], theme.ACCENT[1])
        self.assertEqual(light["ProgressBar"], theme.ACCENT[1])


# -- the style sheet ------------------------------------------------------------------------------

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_WIDGETS = {
    "QAbstractItemView", "QAbstractSpinBox", "QComboBox", "QHeaderView", "QLineEdit",
    "QPushButton", "QStatusBar", "QTabBar", "QTabWidget", "QToolBar", "QToolButton",
    "TransferListWidget",  # qBittorrent's torrent list (a QTreeView with Q_OBJECT)
}  # fmt: skip
_SUBCONTROLS = {"section", "tab", "pane", "separator"}
_STATES_QSS = {"hover", "pressed", "checked", "default", "disabled", "flat", "focus", "selected"}
_PROPERTIES = {
    "alternate-background-color", "background-color", "border", "border-bottom", "border-color",
    "border-radius", "border-right", "border-top", "color", "margin", "padding",
    "selection-background-color", "selection-color", "spacing", "width",
}  # fmt: skip
_PALETTE_ROLES = {
    "window", "window-text", "base", "alternate-base", "text", "button", "button-text",
    "highlight", "highlighted-text", "light", "midlight", "mid", "dark", "shadow", "link",
    "link-visited", "bright-text",
}  # fmt: skip
_SELECTOR_PART = re.compile(r"^([A-Za-z]\w*)(?:::([a-z-]+))?((?::!?[a-z-]+)*)$")


def _rules() -> list[tuple[list[str], list[tuple[str, str]]]]:
    """The style sheet as (selectors, declarations) per rule; fails on anything that isn't."""
    text = _COMMENT.sub("", (SOURCE_DIR / STYLESHEET_NAME).read_text(encoding="utf-8"))
    rules: list[tuple[list[str], list[tuple[str, str]]]] = []
    for selectors, body in _RULE.findall(text):
        declarations: list[tuple[str, str]] = []
        for item in body.split(";"):
            if not item.strip():
                continue
            name, sep, value = item.partition(":")
            assert sep, f"not a declaration: {item.strip()!r}"
            declarations.append((name.strip(), value.strip()))
        rules.append(([part.strip() for part in selectors.split(",")], declarations))
    leftover = _RULE.sub("", text)
    assert not leftover.strip(), f"text outside the rules: {leftover.strip()[:80]!r}"
    return rules


class StyleSheetTests(unittest.TestCase):
    def test_it_parses_with_known_selectors_and_properties(self) -> None:
        rules = _rules()
        self.assertGreater(len(rules), 10)
        for selectors, declarations in rules:
            self.assertTrue(declarations, selectors)
            for selector in selectors:
                for part in selector.split():
                    with self.subTest(selector=selector, part=part):
                        match = _SELECTOR_PART.match(part)
                        assert match is not None
                        widget, subcontrol, states = match.groups()
                        self.assertIn(widget, _WIDGETS)
                        if subcontrol:
                            self.assertIn(subcontrol, _SUBCONTROLS)
                        for state in filter(None, states.split(":")):
                            self.assertIn(state.lstrip("!"), _STATES_QSS)
            for name, value in declarations:
                with self.subTest(selectors=selectors, name=name):
                    self.assertIn(name, _PROPERTIES)
                    self.assertTrue(value)

    def test_only_airgrabbers_colours_and_palette_roles(self) -> None:
        tokens = LIGHT_TOKENS | DARK_TOKENS
        token_rgb = {tuple(int(value[i : i + 2], 16) for i in (1, 3, 5)) for value in tokens}
        for _selectors, declarations in _rules():
            for name, value in declarations:
                for hex_color in re.findall(r"#[0-9A-Fa-f]+", value):
                    with self.subTest(name=name, value=value):
                        self.assertRegex(hex_color.upper(), _HEX)
                        self.assertIn(hex_color.upper(), tokens)
                for red, green, blue, alpha in re.findall(
                    r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", value
                ):
                    with self.subTest(name=name, value=value):
                        self.assertIn((int(red), int(green), int(blue)), token_rgb)
                        self.assertTrue(0 < float(alpha) <= 1)
                for role in re.findall(r"palette\(([^)]*)\)", value):
                    with self.subTest(name=name, value=value):
                        self.assertIn(role.strip(), _PALETTE_ROLES)
                self.assertNotIn("rgb(", value.replace("rgba(", ""))  # one colour syntax

    def test_it_references_only_shipped_files(self) -> None:
        text = (SOURCE_DIR / STYLESHEET_NAME).read_text(encoding="utf-8")
        shipped = {path.as_posix() for path in shipped_files()}
        for url in re.findall(r"url\(([^)]*)\)", _COMMENT.sub("", text)):
            # A folder theme reads ":/uitheme/<file>" as a file next to config.json.
            name = url.strip("'\" ").removeprefix(":/uitheme/")
            self.assertIn(name, shipped)

    def test_the_torrent_states_keep_their_colours(self) -> None:
        # A text colour on list items would override the per-state colours qBittorrent gives them.
        for selectors, declarations in _rules():
            if any("::item" in selector for selector in selectors):
                self.assertNotIn("color", [name for name, _value in declarations])

    def test_the_readme_says_what_it_is(self) -> None:
        readme = " ".join((SOURCE_DIR / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("made for AirGrabber", readme)
        self.assertIn("AirGrabber look in qBittorrent", readme)  # the switch's name in Settings


# -- the copy in AirGrabber's data folder ---------------------------------------------------------


class CopyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / CONFIG_NAME).write_text('{"colors": {}}', encoding="utf-8")
        (self.source / STYLESHEET_NAME).write_text("QPushButton { border-radius: 6px; }", encoding="utf-8")
        self.folder = ThemeFolder(self.root / "data" / "qbittorrent-theme", self.source)

    def test_the_shipped_theme_is_complete(self) -> None:
        names = {path.as_posix() for path in shipped_files()}
        self.assertLessEqual({CONFIG_NAME, STYLESHEET_NAME, "README.md"}, names)

    def test_install_copies_what_changed(self) -> None:
        self.assertTrue(self.folder.install())
        self.assertEqual((self.folder.target / CONFIG_NAME).read_text(encoding="utf-8"), '{"colors": {}}')
        self.assertFalse(self.folder.install())  # nothing changed
        (self.source / STYLESHEET_NAME).write_text("/* new */", encoding="utf-8")
        (self.folder.target / "notes.txt").write_text("mine", encoding="utf-8")
        self.assertTrue(self.folder.install())
        self.assertEqual((self.folder.target / STYLESHEET_NAME).read_text(encoding="utf-8"), "/* new */")
        self.assertTrue((self.folder.target / "notes.txt").exists())  # left alone
        self.assertEqual(sorted(p.name for p in self.folder.target.iterdir()),
                         [CONFIG_NAME, "notes.txt", STYLESHEET_NAME])  # fmt: skip  (no temp files)

    def test_refresh_only_updates_an_existing_copy(self) -> None:
        self.assertFalse(self.folder.refresh())
        self.assertFalse(self.folder.target.exists())
        self.folder.install()
        (self.source / CONFIG_NAME).write_text('{"colors.dark": {}}', encoding="utf-8")
        with self.assertLogs(qt.log, "INFO"):
            self.assertTrue(self.folder.refresh())
        self.assertEqual((self.folder.target / CONFIG_NAME).read_text(encoding="utf-8"), '{"colors.dark": {}}')
        self.assertFalse(self.folder.refresh())

    def test_refresh_never_raises(self) -> None:
        self.folder.install()
        broken = ThemeFolder(self.folder.target, self.root / "missing")
        with self.assertLogs(qt.log, "WARNING"):
            self.assertFalse(broken.refresh())
        with self.assertRaises(OSError):
            broken.install()

    def test_the_copy_lives_in_the_data_folder_as_an_absolute_path(self) -> None:
        folder = ThemeFolder.in_data_dir(Path("relative-data"))
        self.assertTrue(folder.target.is_absolute())
        self.assertEqual(folder.target.name, "qbittorrent-theme")
        self.assertEqual(folder.config.name, CONFIG_NAME)
        self.assertEqual(folder.source, SOURCE_DIR)


if __name__ == "__main__":
    unittest.main()
