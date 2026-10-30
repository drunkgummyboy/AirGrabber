"""qBittorrent set up for AirGrabber: no "Add new torrent" pop-up for each download.

By default qBittorrent opens its "Add new torrent" dialog for every magnet link or .torrent file
it is handed, so AirGrabber's one-click and automatic downloads (DownloadMode.APP: the file or
link opened with Windows' handler) would each wait for a click. That dialog is one setting in
qBittorrent's settings file, `%APPDATA%\\qBittorrent\\qBittorrent.ini` (QSettings, INI format):

- `[AddNewTorrentDialog] Enabled=false`: qBittorrent 4.4 and later, 5.x included, read the key
  "AddNewTorrentDialog/Enabled" (src/base/preferences.cpp `Preferences::isAddNewTorrentDialogEnabled`
  on 5.x / master, `AddNewTorrentDialog::isEnabled` in src/gui/addnewtorrentdialog.cpp on 4.4–4.6);
- `[Preferences] Downloads\\NewAdditionDialog=false`: the same setting as qBittorrent 4.3 and older
  keep it on disk (src/base/settingsstorage.cpp maps "AddNewTorrentDialog/Enabled" to
  "Preferences/Downloads/NewAdditionDialog"); later versions copy it into the new key when they
  migrate older settings (src/app/upgrade.cpp `handleSettingKeys`).

Both are written: each version reads its own and ignores the other, and qBittorrent's migration
copies the old key's value into the new one, which then agree. Nothing else changes: the other
lines stay byte for byte, the original file is backed up once (`qBittorrent.ini.airgrabber-backup`),
and qBittorrent's first-run legal notice (`[LegalNotice] Accepted`) is never touched — the user
sees and accepts that in qBittorrent itself.

qBittorrent writes its whole settings file when it closes, so the file is only changed while
qBittorrent isn't running (`set_up` answers RUNNING instead). When qBittorrent never ran, the file
is made with just these keys; qBittorrent then starts as after an update (src/app/application.cpp:
a non-empty file runs `upgrade()` and its older defaults, e.g. torrent queueing on), not as new.

The save path for "Where finished downloads end up" is read from `[BitTorrent]
Session\\DefaultSavePath` (4.4+), else `[Preferences] Downloads\\SavePath` (4.3 and older), else the
user's Downloads folder (qBittorrent's own default).

AirGrabber's look (app/services/qbittorrent_theme.py, when a ThemeFolder is given): the setup also
copies the theme into AirGrabber's data folder and sets `[Preferences] General\\UseCustomUITheme=true`
and `General\\CustomUIThemePath=<that folder>\\config.json` (src/base/preferences.cpp, 4.4 – 5.3),
but only when qBittorrent has no theme path yet (`look_state` NONE): a theme of the user's own, or
AirGrabber's look turned off (by Settings' switch or in qBittorrent's options), stays as it is.
`look` / `set_look` read and change just those two keys (turning it off only sets
UseCustomUITheme=false, so the path tells a later setup that it was turned off), under the same
rules: never while qBittorrent runs, the same one-time backup.

qBittorrent's Web UI for AirGrabber (app/services/qbittorrent_webui.py has the keys and the
password's hash): `set_up_web_ui` turns it on, bound to 127.0.0.1 with a free port and a new
password, so AirGrabber can send downloads to it and show their progress; only these keys, under
the same rules (never while qBittorrent runs, the same one-time backup), and never over a Web UI
of the user's own. app/services/qbittorrent_local.py decides when (and points AirGrabber at it).

Also run by install.ps1 right after it installed qBittorrent (main.py `--setup-qbittorrent`,
after the package check: `run_setup`; on a new AirGrabber install that also sets the Web UI up). Blocking: the app calls it through `asyncio.to_thread`
(TorrentAppService). The file and the process check are injectable, so tests never touch the
real ones.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

from app.models import QBittorrentSetupResult as SetupResult
from app.models import QBittorrentState
from app.services import qbittorrent_webui as webui
from app.services.qbittorrent_theme import ThemeFolder
from app.services.qbittorrent_webui import PortCheck, WebUiLogin, WebUiResult

log = logging.getLogger(__name__)

SETTINGS_DIR = "qBittorrent"  # in %APPDATA%
SETTINGS_NAME = "qBittorrent.ini"
BACKUP_SUFFIX = ".airgrabber-backup"
PROCESS_NAME = "qbittorrent.exe"
_TEMP_SUFFIX = ".airgrabber-new"
_DEFAULT_NEWLINE = "\r\n"  # what QSettings writes on Windows


@dataclass(frozen=True, slots=True)
class IniKey:
    """A QSettings key as the INI file has it: its section and its key inside that section
    (sub-keys joined with a backslash, as QSettings writes them)."""

    section: str
    key: str


# The "Add new torrent" dialog's switch: qBittorrent 4.4+ / 5.x, then 4.3 and older.
DIALOG_KEYS: tuple[IniKey, ...] = (
    IniKey("AddNewTorrentDialog", "Enabled"),
    IniKey("Preferences", "Downloads\\NewAdditionDialog"),
)
# The default save folder, in the same order.
SAVE_PATH_KEYS: tuple[IniKey, ...] = (
    IniKey("BitTorrent", "Session\\DefaultSavePath"),
    IniKey("Preferences", "Downloads\\SavePath"),
)
QUIET_VALUES: Mapping[IniKey, str] = {key: "false" for key in DIALOG_KEYS}
# A custom UI theme: on / off, and the theme's config.json (qBittorrent 4.4 – 5.x).
THEME_ON_KEY = IniKey("Preferences", "General\\UseCustomUITheme")
THEME_PATH_KEY = IniKey("Preferences", "General\\CustomUIThemePath")


class Look(Enum):
    """qBittorrent's theme settings as AirGrabber sees them."""

    NONE = "none"  # no theme path: qBittorrent's own look
    ON = "on"  # AirGrabber's look
    OFF = "off"  # AirGrabber's look, turned off: left off
    OTHER_ON = "other_on"  # a theme of the user's own, in use: left alone
    OTHER_OFF = "other_off"  # a theme of the user's own, turned off: left alone by the setup


# Returns True while qBittorrent runs.
RunningCheck = Callable[[], bool]


def settings_file(appdata: str | None = None) -> Path:
    """%APPDATA%\\qBittorrent\\qBittorrent.ini (qBittorrent's non-portable settings)."""
    root = appdata if appdata is not None else os.environ.get("APPDATA", "")
    base = Path(root) if root else Path.home() / "AppData" / "Roaming"
    return base / SETTINGS_DIR / SETTINGS_NAME


def default_save_path(home: Path | None = None) -> Path:
    """qBittorrent's own default save folder: the user's Downloads folder."""
    return (home if home is not None else Path.home()) / "Downloads"


# -- reading and editing the INI text (pure) ------------------------------------------------------


def _key_name(text: str) -> str:
    """A key for comparing: QSettings reads "\\" and "/" alike; Windows INI keys ignore case."""
    return text.strip().replace("\\", "/").casefold()


def _section_name(line: str) -> str | None:
    stripped = line.strip().lstrip("\ufeff")
    if len(stripped) >= 2 and stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip().casefold()
    return None


@dataclass(frozen=True, slots=True)
class _Entry:
    index: int  # the line's index
    section: str  # casefolded
    key: str  # _key_name
    value: str  # the raw text after "=" (line ending removed)


def _lines(text: str) -> list[str]:
    """The lines with their "\\r" kept (split on "\\n" only, so joining gives the text back)."""
    return text.split("\n")


def _entries(lines: list[str]) -> Iterator[_Entry]:
    section = ""
    for index, line in enumerate(lines):
        body = line[:-1] if line.endswith("\r") else line
        name = _section_name(body)
        if name is not None:
            section = name
            continue
        stripped = body.lstrip()
        if not stripped or stripped.startswith(";") or "=" not in body:
            continue
        key, value = body.split("=", 1)
        yield _Entry(index, section, _key_name(key), value)


def _find(lines: list[str], wanted: IniKey) -> _Entry | None:
    section, key = wanted.section.casefold(), _key_name(wanted.key)
    found = None
    for entry in _entries(lines):
        if entry.section == section and entry.key == key:
            found = entry  # the last one wins, as in QSettings
    return found


def raw_value(text: str, wanted: IniKey) -> str | None:
    """The value text of `wanted` (unescaped), or None when the file doesn't have it."""
    entry = _find(_lines(text), wanted)
    return None if entry is None else unescape(entry.value)


def unescape(value: str) -> str:
    """A QSettings INI value as a string: surrounding spaces dropped, "..." quoting, backslash
    escapes (\\\\ \\" \\n \\t …) and \\xHHHH escapes (Qt 5 writes non-ASCII characters that way;
    Qt 6 writes UTF-8). A leading "@@" stands for "@"."""
    result: list[str] = []
    text = value.strip()
    quoted = False
    i = 0
    simple = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    while i < len(text):
        char = text[i]
        if char == '"':
            quoted = not quoted
            i += 1
            continue
        if char == "\\" and i + 1 < len(text):
            following = text[i + 1]
            if following in "xX":
                j = i + 2
                while j < len(text) and text[j] in "0123456789abcdefABCDEF":
                    j += 1
                if j > i + 2:
                    result.append(chr(int(text[i + 2 : j], 16)))
                    i = j
                    continue
            result.append(simple.get(following, following))
            i += 2
            continue
        result.append(char)
        i += 1
    out = "".join(result)
    return out[1:] if out.startswith("@@") else out


def set_values(text: str, values: Mapping[IniKey, str]) -> str:
    """`text` with each key of `values` set to its (plain, unescaped-safe) value: an existing line
    keeps its key text and only gets the new value; a missing key goes at the end of its section;
    a missing section goes at the end of the file. Every other line stays as it was."""
    lines = _lines(text)
    newline = "\r\n" if "\r\n" in text else ("\n" if "\n" in text else _DEFAULT_NEWLINE)
    eol = "\r" if newline == "\r\n" else ""
    for wanted, value in values.items():
        entry = _find(lines, wanted)
        if entry is not None:
            line = lines[entry.index]
            ending = "\r" if line.endswith("\r") else ""
            key_text = line.split("=", 1)[0]
            lines[entry.index] = f"{key_text}={value}{ending}"
            continue
        last = _section_end(lines, wanted.section.casefold())
        if last is not None:
            if last == len(lines) - 1:  # the file's last line, without a line ending
                lines[last] += eol
                lines.append(f"{wanted.key}={value}")
            else:
                lines.insert(last + 1, f"{wanted.key}={value}{eol}")
            continue
        # A new section at the end, one blank line below the rest (QSettings' layout).
        current = "\n".join(lines)
        start = current.rstrip("\r\n") + newline * 2 if current.strip() else ""
        lines = _lines(f"{start}[{wanted.section}]{newline}{wanted.key}={value}{newline}")
    return "\n".join(lines)


def _section_end(lines: list[str], section: str) -> int | None:
    """The index of the last non-blank line of `section` (its header when it has no keys), or
    None when the file has no such section."""
    current = ""
    last: int | None = None
    for index, line in enumerate(lines):
        body = line[:-1] if line.endswith("\r") else line
        name = _section_name(body)
        if name is not None:
            current = name
            if name == section:
                last = index
            continue
        if current == section and body.strip():
            last = index
    return last


def dialog_off(text: str) -> bool:
    """No "Add new torrent" pop-up: the dialog keys the file has all say false (and it has at
    least one). Without either, qBittorrent shows the dialog (its default)."""
    found = [raw_value(text, key) for key in DIALOG_KEYS]
    present = [value for value in found if value is not None]
    return bool(present) and all(value.strip().casefold() == "false" for value in present)


def save_path(text: str | None, home: Path | None = None) -> str:
    """qBittorrent's default save folder as a Windows path ("C:\\Users\\Alex\\Downloads"):
    from the settings file, else qBittorrent's default (the Downloads folder)."""
    for key in SAVE_PATH_KEYS:
        value = raw_value(text, key) if text is not None else None
        if value and value.strip():
            return str(PureWindowsPath(value.strip()))
    return str(default_save_path(home))


def escape(value: str) -> str:
    """`value` as QSettings writes a string into an INI file (the inverse of `unescape`):
    backslashes and double quotes escaped, control and non-ASCII characters as \\xHHHH (which
    Qt 5 and Qt 6 both read; a hex digit right after one is escaped too, so it isn't read as part
    of it), in quotes when it has a ";", "," or "=" or starts or ends with a space; a leading "@"
    doubled."""
    text = "@" + value if value.startswith("@") else value
    out: list[str] = []
    after_hex = False
    for unit in _utf16_units(text):
        char = chr(unit)
        if after_hex and char in "0123456789abcdefABCDEF":
            out.append(f"\\x{unit:x}")
            continue
        after_hex = False
        if char in '\\"':
            out.append("\\" + char)
        elif unit < 0x20 or unit >= 0x7F:
            out.append(f"\\x{unit:x}")
            after_hex = True
        else:
            out.append(char)
    escaped = "".join(out)
    if any(char in value for char in ";,=") or escaped.startswith(" ") or escaped.endswith(" "):
        return f'"{escaped}"'
    return escaped


def _utf16_units(text: str) -> list[int]:
    """The UTF-16 code units of `text` (QString's characters; a character beyond U+FFFF is two)."""
    data = text.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def qt_bool(value: str | None) -> bool:
    """A setting's text as Qt reads it as a bool: true unless missing, empty, "0" or "false"."""
    return value is not None and value.strip().casefold() not in ("", "0", "false")


def _same_path(first: str, second: Path) -> bool:
    """Windows paths compared as Windows does: "/" and "\\" alike, case ignored."""
    return str(PureWindowsPath(first.strip())).casefold() == str(PureWindowsPath(second)).casefold()


def look_state(text: str | None, config: Path) -> Look:
    """qBittorrent's theme settings in `text`, `config` being AirGrabber's theme's config.json.
    qBittorrent uses a theme only when it has a path (src/base/preferences.cpp)."""
    path = raw_value(text, THEME_PATH_KEY) if text is not None else None
    if path is None or not path.strip():
        return Look.NONE
    on = qt_bool(raw_value(text, THEME_ON_KEY) if text is not None else None)
    if _same_path(path, config):
        return Look.ON if on else Look.OFF
    return Look.OTHER_ON if on else Look.OTHER_OFF


def look_values(config: Path, on: bool) -> dict[IniKey, str]:
    """The settings for AirGrabber's look on (its path and the switch) or off (only the switch:
    the path stays, so a later setup sees it was turned off)."""
    if not on:
        return {THEME_ON_KEY: "false"}
    return {THEME_ON_KEY: "true", THEME_PATH_KEY: escape(str(PureWindowsPath(config)))}


def _reader(text: str | None) -> webui.Reader:
    """`text`'s values by (section, key), unescaped (none when there is no file)."""

    def read(section: str, key: str) -> str | None:
        return raw_value(text, IniKey(section, key)) if text is not None else None

    return read


def _decode(data: bytes) -> str:
    # surrogateescape: bytes that aren't UTF-8 come back unchanged when written again.
    return data.decode("utf-8", errors="surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


# -- is qBittorrent running? ----------------------------------------------------------------------


def qbittorrent_running() -> bool:
    """True while a qbittorrent.exe runs (this user's or another's). When Windows can't say,
    True as well: better to ask than to have qBittorrent overwrite the change when it closes."""
    if sys.platform != "win32":
        return False
    try:
        return PROCESS_NAME in {name.casefold() for name in _process_names()}
    except OSError:
        log.warning("Couldn't list the running programs; assuming qBittorrent runs")
        return True


def _process_names() -> list[str]:
    """The executable names of the running processes (Toolhelp32 snapshot). Windows only."""
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot_processes = 0x00000002
    invalid = wintypes.HANDLE(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(snapshot_processes, 0)
    if snapshot is None or snapshot == invalid:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    names: list[str] = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.append(entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


# -- the setup ------------------------------------------------------------------------------------


class QBittorrentSetup:
    """qBittorrent's settings file: read its state, turn the pop-up off, and with a `theme`
    (AirGrabber's copy of its qBittorrent theme) give qBittorrent AirGrabber's look. Blocking."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        running: RunningCheck = qbittorrent_running,
        home: Path | None = None,
        theme: ThemeFolder | None = None,
    ) -> None:
        self._path = path
        self._running = running
        self._home = home
        self._theme = theme

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else settings_file()

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + BACKUP_SUFFIX)

    def _read(self) -> str | None:
        try:
            return _decode(self.path.read_bytes())
        except FileNotFoundError:
            return None

    def state(self) -> QBittorrentState:
        """Raises OSError when the file exists but can't be read."""
        text = self._read()
        return QBittorrentState(
            exists=text is not None,
            quiet=text is not None and dialog_off(text),
            save_path=save_path(text, self._home),
        )

    def set_up(self) -> SetupResult:
        """Turn the "Add new torrent" pop-up off and, with a theme, give qBittorrent AirGrabber's
        look when it has no theme yet; unless qBittorrent runs (RUNNING: nothing is changed).
        Creates the folder and file when qBittorrent never ran. Never raises."""
        try:
            if self._running():
                log.info("qBittorrent runs: its settings were left alone")
                return SetupResult.RUNNING
            original = self._read()
            values: dict[IniKey, str] = {}
            if original is None or not dialog_off(original):
                values.update(QUIET_VALUES)
            values.update(self._setup_look(original))
            if not values:
                return SetupResult.ALREADY
            self._write(original, values)
        except OSError as exc:
            log.warning("Couldn't change qBittorrent's settings (%s)", type(exc).__name__)
            return SetupResult.FAILED
        log.info("qBittorrent set up for AirGrabber")
        return SetupResult.DONE

    def _setup_look(self, original: str | None) -> dict[IniKey, str]:
        """What the setup changes for the look: AirGrabber's theme when qBittorrent has none
        (its files copied first; when that fails, the setup goes on without it); its files again
        when it is on (they may have gone); else nothing."""
        if self._theme is None:
            return {}
        state = look_state(original, self._theme.config)
        if state not in (Look.NONE, Look.ON):
            return {}
        try:
            self._theme.install()
        except OSError as exc:
            log.warning("Couldn't copy the qBittorrent theme (%s)", type(exc).__name__)
            return {}
        return look_values(self._theme.config, True) if state is Look.NONE else {}

    def look(self) -> bool | None:
        """Whether qBittorrent has AirGrabber's look on (Settings' switch); None without a theme
        or while a theme of the user's own is in use (AirGrabber leaves that alone). Raises
        OSError when the file exists but can't be read."""
        if self._theme is None:
            return None
        state = look_state(self._read(), self._theme.config)
        if state is Look.OTHER_ON:
            return None
        return state is Look.ON

    def set_look(self, on: bool) -> SetupResult:
        """Turn AirGrabber's look on (its files copied, its path and the switch set; also over a
        theme of the user's own that is turned off) or off (only the switch, and only when it is
        AirGrabber's look), unless qBittorrent runs (RUNNING). Never raises."""
        if self._theme is None:
            return SetupResult.FAILED
        config = self._theme.config
        try:
            if self._running():
                log.info("qBittorrent runs: its look was left alone")
                return SetupResult.RUNNING
            original = self._read()
            state = look_state(original, config)
            if on:
                self._theme.install()  # also when already on: the files may have gone
                if state is Look.ON:
                    return SetupResult.ALREADY
            elif state is not Look.ON:
                return SetupResult.ALREADY
            self._write(original, look_values(config, on))
        except OSError as exc:
            log.warning("Couldn't change qBittorrent's look (%s)", type(exc).__name__)
            return SetupResult.FAILED
        log.info("qBittorrent's AirGrabber look turned %s", "on" if on else "off")
        return SetupResult.DONE

    def refresh_theme(self) -> None:
        """Bring AirGrabber's copy of the theme up to date with the shipped one (when there is
        a copy). Never raises; qBittorrent's settings file isn't touched."""
        if self._theme is not None:
            self._theme.refresh()

    def set_up_web_ui(
        self,
        known: WebUiLogin | None = None,
        *,
        port_free: PortCheck = webui.port_is_free,
        spare_port: Callable[[], int] = webui.spare_port,
        new_password: Callable[[], str] = webui.new_password,
        new_salt: Callable[[], bytes] = webui.new_salt,
    ) -> tuple[WebUiResult, WebUiLogin | None]:
        """Turn qBittorrent's Web UI on for AirGrabber: 127.0.0.1, a free port, the user name
        "airgrabber", a new password (its PBKDF2 hash written), a password asked of this PC
        too. KEPT (no change) when it is AirGrabber's already and takes `known` (the login
        AirGrabber's settings hold); USER when it is the user's own (left alone); RUNNING while
        qBittorrent runs. Never raises; nothing but the Web UI's keys changes."""
        try:
            if self._running():
                log.info("qBittorrent runs: its Web UI was left alone")
                return WebUiResult.RUNNING, None
            original = self._read()
            read = _reader(original)
            found = webui.owner(read)
            if found is webui.WebUi.USER:
                log.info("qBittorrent's Web UI is the user's own: left alone")
                return WebUiResult.USER, None
            if found is webui.WebUi.AIRGRABBER and known is not None and webui.matches(read, known):
                return WebUiResult.KEPT, known
            earlier = webui.port_of(read) if found is webui.WebUi.AIRGRABBER else None
            port = webui.choose_port(read, port_free, spare_port, preferred=earlier)
            login = WebUiLogin(port, webui.USERNAME, new_password())
            values = {IniKey(*key): text for key, text in webui.entries(login, new_salt()).items()}
            self._write(original, values)
        except OSError as exc:
            log.warning("Couldn't turn qBittorrent's Web UI on (%s)", type(exc).__name__)
            return WebUiResult.FAILED, None
        log.info("qBittorrent's Web UI turned on for AirGrabber (port %d, this PC only)", port)
        return WebUiResult.CREATED, login

    def legal_notice_accepted(self) -> bool:
        """qBittorrent's first-run notice was accepted (read only; False when unreadable)."""
        try:
            return webui.legal_notice_accepted(_reader(self._read()))
        except OSError:
            return False

    def _write(self, original: str | None, values: Mapping[IniKey, str]) -> None:
        """`values` set in the file (made when missing), the original backed up once; the new
        text written whole, then swapped in. Raises OSError."""
        path = self.path
        changed = set_values(original if original is not None else "", values)
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = self.backup_path
        if original is not None and not backup.exists():
            backup.write_bytes(_encode(original))
        temp = path.with_name(path.name + _TEMP_SUFFIX)
        temp.write_bytes(_encode(changed))
        os.replace(temp, path)


def run_setup(setup: QBittorrentSetup | None = None, *, data_dir: Path | None = None) -> int:
    """main.py's `--setup-qbittorrent` (install.ps1, after it installed qBittorrent): the exit
    code says how it went (EXIT_CODES). `data_dir`: AirGrabber's data folder, for its copy of
    the qBittorrent theme (without it, no theme) and, on a new AirGrabber install (no database
    there yet), for pointing AirGrabber at qBittorrent's Web UI (qbittorrent_local)."""
    if setup is None:
        theme = ThemeFolder.in_data_dir(data_dir) if data_dir is not None else None
        setup = QBittorrentSetup(theme=theme)
    result = setup.set_up()
    if data_dir is not None and result in (SetupResult.DONE, SetupResult.ALREADY):
        # Here, not at the top: it needs the settings and the database (main.py's way in only).
        from app.services.qbittorrent_local import set_up_new_install

        set_up_new_install(setup, data_dir)  # never raises
    return EXIT_CODES[result]


# install.ps1 reads these (Install-QBittorrentIfNeeded): keep them in step.
EXIT_CODES: Mapping[SetupResult, int] = {
    SetupResult.DONE: 0,
    SetupResult.ALREADY: 0,
    SetupResult.FAILED: 1,
    SetupResult.RUNNING: 3,
}
