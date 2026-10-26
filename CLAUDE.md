# AirGrabber 2.0

Python 3.13 desktop app: customtkinter 6 + async-tkinter-loop, aiosqlite, httpx, Pydantic v2, Pillow.
`airgrabber.py` is the legacy 1.x monolith — reference only, never edit or import it.

## Commands
- Run: `python main.py` or `pythonw main.py` (data in `%LOCALAPPDATA%\AirGrabber`; override with env
  `AIRGRABBER_DATA_DIR`). Missing/outdated `requirements.txt` packages are pip-installed at launch.
- Tests: `python -m unittest discover -s tests -t .` (stdlib unittest; no pytest)
- Types: `python -m mypy` (config in `mypy.ini`, strict) — must report no issues
- Import 1.x JSON data: `python -m tools.import_legacy [LEGACY_DIR] [--db PATH]`
- Release: bump `__version__` in `app/__init__.py`, run `python -m tools.sync_repo CLONE` into the git clone
  of github.com/drunkgummyboy/AirGrabber (kept outside Google Drive), review `git status`, commit, push.
  Users install with `install.ps1` (one-liner in README.md) and update from Settings.

## Rules
- `main.py`'s top-level imports, `app/__init__.py`, `app/config.py`, `app/bootstrap.py`,
  `app/single_instance.py`, `app/identity.py`, `app/i18n.py` and `app/i18n_nl.py` run before the package
  check, so they must stay stdlib-only. Import third-party packages from `main._run` onwards.
- All user-visible text goes through `t()` (`app/i18n.py`; `tn()` for one / more, `N_()` for a text kept in
  a constant, translated where it is shown; placeholders as `t("… {name} …", name=…)`, never an f-string
  inside `t()`): add the Dutch entry to `app/i18n_nl.py` (tests/test_i18n.py checks both ways). Log
  messages, `install.ps1` and `app/bootstrap.py` stay English. The language is set once at startup.
- An installed copy is only `main.py`, `requirements.txt` and `app/` (`APP_ENTRIES` in `app/services/updates.py`
  and `install.ps1`): runtime files belong under `app/` (images in `app/assets`). Installed copies only see a
  release whose `__version__` is higher. A folder with `tests/` (this one) never updates itself.
- `install.ps1` must run in Windows PowerShell 5.1 under `irm | iex`: no `&&`/ternaries/`??`, and no `exit`.
- One asyncio loop drives Tk (async-tkinter-loop). No threads, executors or UI queues for I/O:
  await httpx/aiosqlite directly, then update widgets. CPU-heavy Pillow work goes through `asyncio.to_thread`.
- The one exception: the tray icon (`app/ui/tray.py`, pystray) runs its own Win32 message loop on a daemon
  thread. Its callbacks may only hand work to the loop with `loop.call_soon_threadsafe` and never touch Tk.
- UI code starts coroutines only via `app.ui.tasks.spawn` / `async_command` (they log + report failures).
  Nothing may spawn before the loop runs (see `MainWindow.async_mainloop`).
- Layers: `app/ui` → `app/services` → repositories (`app/database.py`) and API clients.
  UI never imports `app.database` or httpx. All SQL lives in `app/database.py`.
- The UI consumes only frozen Pydantic models from `app.models`; API JSON is validated into models at the boundary.
- Widgets touched after an `await` may be gone: views cancel their tasks in `destroy()`; long-lived async
  widget code checks `winfo_exists()` first.
- CTk scales widget sizes by the DPI factor (150% on the dev machine); images handed to `CTkImage` should
  be rendered at physical size (`ctk.ScalingTracker.get_widget_scaling(widget)`) to stay sharp.
- Colours are (light, dark) token pairs in `app/ui/theme.py` (Settings → Looks → Theme: Follow
  Windows / Dark / Light, switched live); reuse them instead of new hex values. Buttons without a
  colour fill need `text_color=theme.BUTTON_TEXT`. What CTk doesn't repaint (plain Tk, canvas
  items, ttk, Pillow-baked images) takes `theme.pick(token)` and repaints on a switch
  (`theme.on_mode_change`, or the CTk widget's own `_set_appearance_mode`). Tabs: `widgets.Tabview`.
- Never call live APIs from tests: use `httpx.MockTransport` and `tests/fixtures/`.
- The TMDB v3 key travels as a URL parameter and Prowlarr's `TorrentRelease.download_url` embeds its
  API key: never log request URLs or release objects, never put `str(httpx_exc)` in messages, and keep
  the `httpx`/`httpcore` loggers at WARNING (`app/config.py`). DSM passwords go in POST bodies only.
  The remote programs' status calls (Downloads page) keep their sign-in (DSM sid, qBittorrent
  SID cookie, Transmission session id) in memory only, never logged or shown; a task's link
  (magnet / `uri`, may hold a tracker passkey) is read for its hash or file name and dropped.
  OpenSubtitles: AirGrabber's one API key (`APP_API_KEY` in `app/services/opensubtitles.py`) ships
  with the app and is public, but is still never logged; it travels in the `Api-Key` header. A user's
  own key (`opensubtitles_own_key`, replaces the shipped one when filled in) is a secret: header only,
  never in a URL, message or log. Signing in with an account is optional: the password only in the
  /login POST body, the session token in the `Authorization` header and in memory only; /download's
  temporary links are never logged.

See `filemap.md` for where things live.
