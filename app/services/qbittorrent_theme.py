"""AirGrabber's look for qBittorrent: the folder theme shipped in app/assets/qbittorrent-theme.

How qBittorrent uses a folder theme (its source, release-4.4.5 to release-5.3.0):

- `[Preferences] General\\UseCustomUITheme=true` with `General\\CustomUIThemePath` naming the theme's
  `config.json` (src/base/preferences.cpp `useCustomUITheme`: only on with a path). A path whose file
  name is config.json is a folder theme (src/gui/uithememanager.cpp → FolderThemeSource; 4.3 and
  older only take .qbtheme files), read once when qBittorrent starts.
- config.json: `colors` (light) and, from 4.6, `colors.dark` (src/gui/uithemesource.cpp
  `CustomThemeSource::loadColors` / `getColor`: the dark set first in dark mode, else `colors`).
  Dark mode is the palette's: "dark" when its Base colour's lightness is below 127
  (uithememanager.cpp `isDarkTheme`).
- The palette overrides (`Palette.Window`, `Palette.Base`, …) are applied once at startup and only
  from `colors` ("For backward compatibility, the palette color overrides are read from the section
  of the 'light mode' colors", `UIThemeManager::applyPalette`). A theme that sets Window / Base /
  Text is therefore always light or always dark. AirGrabber's theme sets only the selection accent,
  so in qBittorrent 5 (Fusion style, Qt 6.7+) the rest of the palette is still Qt's light or dark one
  for Windows' mode (or qBittorrent's own Options → Behavior → Color scheme, `[Appearance]
  ColorScheme`, in builds with Qt 6.8+), switched live (`QStyleHints::colorSchemeChanged`).
  qBittorrent 4.x on Windows (Qt 6.4.3, Windows Vista style) has no dark palette: it shows the
  light look with these accents.
- stylesheet.qss: applied once, the same in both modes; it takes its backgrounds from the palette.

AirGrabber keeps its copy in its data folder (`ThemeFolder.in_data_dir`: survives updates of the
app folder), refreshed when the shipped files change (`refresh`, at every start). The two settings
keys are written by app/services/qbittorrent_setup.py, the only code that changes qBittorrent's
settings file. Blocking file I/O: call through `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "qbittorrent-theme"
FOLDER_NAME = "qbittorrent-theme"  # in AirGrabber's data folder
CONFIG_NAME = "config.json"  # what qBittorrent's CustomUIThemePath names
STYLESHEET_NAME = "stylesheet.qss"
_TEMP_SUFFIX = ".airgrabber-new"


def shipped_files(source: Path = SOURCE_DIR) -> list[Path]:
    """The theme's files, relative to its folder (config.json, stylesheet.qss, README.md)."""
    return sorted(
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() and not path.name.endswith(_TEMP_SUFFIX)
    )


@dataclass(frozen=True, slots=True)
class ThemeFolder:
    """AirGrabber's copy of the theme (`target`) and the shipped files it copies (`source`)."""

    target: Path
    source: Path = SOURCE_DIR

    @classmethod
    def in_data_dir(cls, data_dir: Path) -> ThemeFolder:
        """`<AirGrabber's data folder>\\qbittorrent-theme`, as an absolute path (qBittorrent 5 reads
        a relative theme path against its own settings folder)."""
        return cls(Path(os.path.abspath(data_dir / FOLDER_NAME)))

    @property
    def config(self) -> Path:
        """The file qBittorrent's `CustomUIThemePath` names."""
        return self.target / CONFIG_NAME

    def install(self) -> bool:
        """Copy the shipped files that are missing or differ (each written whole, then swapped
        in); True when something was written. Files only AirGrabber's copy has stay. Raises
        OSError (the shipped files missing, the folder not writable)."""
        files = shipped_files(self.source)
        if not files:
            raise FileNotFoundError(f"No theme files in {self.source.name}")
        written = False
        for relative in files:
            data = (self.source / relative).read_bytes()
            target = self.target / relative
            try:
                if target.read_bytes() == data:
                    continue
            except FileNotFoundError:
                pass
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + _TEMP_SUFFIX)
            temp.write_bytes(data)
            os.replace(temp, target)
            written = True
        return written

    def refresh(self) -> bool:
        """`install`, but only when the copy exists (qBittorrent was given AirGrabber's look
        before): an update's changed theme then shows at qBittorrent's next start. True when
        something was written. Never raises."""
        if not self.target.is_dir():
            return False
        try:
            written = self.install()
        except OSError as exc:
            log.warning("Couldn't refresh the qBittorrent theme (%s)", type(exc).__name__)
            return False
        if written:
            log.info("qBittorrent theme refreshed")
        return written
