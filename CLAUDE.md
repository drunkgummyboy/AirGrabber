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
  of github.com/drunkgummyboy/AirGrabber (kept outside Google Drive), review `git status`, commit, push to
  the `candidate` branch first. Installed copies only read `main`: push `main` only once `.github/workflows/tests.yml`
  is green on `candidate`. Users install with `install.ps1` (one-liner in README.md) and update from Settings.
  Its notes (`app/whats_new.py`): a big feature = a `Feature` + a picture drawn in `tools/whats_new_art.py`, no
  words; smaller = a line. Only the newest release's pictures stay in `app/assets/whats_new`, older Features go with them.

## Rules
- `main.py`'s top-level imports, `app/__init__.py`, `app/config.py`, `app/bootstrap.py`, `app/update_guard.py`,
  `app/single_instance.py`, `app/identity.py`, `app/i18n.py`, `app/i18n_nl.py` and `app/i18n_nl_forge.py`
  run before the package check, so they must stay stdlib-only. Import third-party packages from `main._run` onwards.
- All user-visible text goes through `t()` (`app/i18n.py`; `tn()` for one / more, `N_()` for a text kept in
  a constant, translated where it is shown; placeholders as `t("… {name} …", name=…)`, never an f-string
  inside `t()`): add the Dutch entry to `app/i18n_nl.py`, for `app/forge` to `app/i18n_nl_forge.py` (merged
  into one lookup; tests/test_i18n.py checks both ways, a text in one catalog only). Log
  messages, `install.ps1` and `app/bootstrap.py` stay English. The language is set once at startup.
- An installed copy is only `main.py`, `requirements.txt` and `app/` (`APP_ENTRIES` in `app/services/updates.py`
  and `install.ps1`): runtime files belong under `app/` (images in `app/assets`). Installed copies only see a
  release whose `__version__` is higher. A folder with `tests/` (this one) never updates itself.
- An update keeps what it replaced in `<install>/.previous` with a copy of the databases (`app/update_guard.py`):
  two starts that never got going, or Settings' "Go back to version …", put it back; that version is then
  skipped. So a migration must leave nothing an older version's start can't survive, beyond `user_version`
  (a newer `user_version` is what makes the database go back too, the newer file kept beside it).
- `install.ps1` must run in Windows PowerShell 5.1 under `irm | iex`: no `&&`/ternaries/`??`, and no `exit`.
- One asyncio loop drives Tk (async-tkinter-loop). No threads, executors or UI queues for I/O:
  await httpx/aiosqlite directly, then update widgets. CPU-heavy Pillow work goes through `asyncio.to_thread`.
- The one exception: the tray icon (`app/ui/tray.py`, pystray) runs its own Win32 message loop on a daemon
  thread. Its callbacks may only hand work to the loop with `loop.call_soon_threadsafe` and never touch Tk.
  And the quit watchdog (`app/ui/quit_watchdog.py`, 2026-11-09): armed once quitting starts, it logs every
  thread's stack and ends the process if closing takes over a minute; it touches neither Tk nor the loop.
- A plain second start waits for the running copy's answer (`single_instance.show_running`); a copy that
  is quitting gives none, and the new start waits for it to close, then runs (before, nothing opened).
- GC guard (D6, adopted 2026-11-08 after F-28's soaks had said no): `app/ui/gc_guard.py`, started in `main._run`: automatic GC off,
  the Tk thread collects on a timer. A collection on aiosqlite's thread had freed a Tk font there and hung that thread
  for good in `Font.__del__` (every search and grab froze). Never turn automatic GC back on; keep Tk images made and dropped on the loop.
- UI code starts coroutines only via `app.ui.tasks.spawn` / `async_command` (they log + report failures).
  Nothing may spawn before the loop runs (see `MainWindow.async_mainloop`).
- Layers: `app/ui` → `app/services` → repositories (`app/database.py`) and API clients.
  UI never imports `app.database` or httpx. All SQL lives in `app/database.py` and `app/forge/db/`
  (MediaForge's own `forge.db`).
- Forge (`app/forge`, MediaForge inside AirGrabber; plan and decisions in `docs/forge/`) is a page of the main
  window (`Mode.FORGE`, `app/forge/ui/page.py`, `docs/forge/PAGE.md`), built at its first visit by `ForgeHost`;
  there is no Forge window. It isn't a BaseView (`_visible_view` is None on it). What it binds on the main window
  (the queue's keys, Escape) acts only while it is on screen and never from a text field. `app/forge/ui` →
  `app/forge/services.py` → `app/forge/db` and `app/forge/clients`; `app/forge/ui` never imports
  `app.forge.db`, `app.database` or httpx; non-UI Forge code never imports tkinter / customtkinter.
  guessit is the one optional package Forge needs (below `# optional` in requirements.txt), imported only
  inside `app/forge/core/parser.py`'s functions; no tkinterdnd2, no aiohttp. Tests in `tests/forge/`
  (`tests/forge/base.py`). Its Dutch is the second catalog, `app/i18n_nl_forge.py`. yt-dlp / ffmpeg go
  to `AppPaths.forge / "tools"`, never under `app/`. Explorer drops (`app/forge/ui/file_drops.py`, attached to
  the main window at its start) are ctypes on the Tk thread behind a seam (tests: fakes).
- The bridges between AirGrabber and Forge (phase 2): no SQL across `airgrabber.db` and `forge.db`.
  Forge's move records (`ForgeMoveService`, `app/forge/core/moves.py`) reach AirGrabber only through
  `app/services/download_status.py` (where a grab was moved, matched by a film's TMDB id or the
  release name; a moved grab shows as finished whatever its torrent program says). There is no
  library index and no "have it" from the library (removed by the owner's decision, 2026-11-02). AirGrabber's start
  imports Forge's types only under `TYPE_CHECKING`; these log ids and counts, never a file's path.
  The other bridge: `app/services/plex_scans.py` hears the same move records and has Plex scan
  the folders they went to (`PlexService.scan_folders`).
- Where a forged file goes: a preset's `move_to_library` extra (`app/forge/core/library.py`'s `route_inputs`;
  one-time `carry_move_option` ticks Series (Folder) and Movie (Folder)). On: to the Series / Movies folder, whose
  absence leaves the file alone and says so. Off: renamed where it is. Downloads Auto-rename finds always move
  (left in a watched folder they would be found again). The queue's band says which ("📦 Moves to" / "📁 Stays").
- Subtitles are MediaForge's (`app/forge/core/subtitles.py`, `services.subtitles`): a preset's Subtitles
  extra (right after a rename, on every route, through the executor; a failure never touches the
  rename) and CC ▾ on the queue's done bands and History's rows, drawn on the list's canvas. One try,
  no background checks; the disk is the only memory (no table in `forge.db`); a fetched subtitle is
  journaled in its video's run, so Undo takes it along. The settings stay in AirGrabber's `Settings`
  (same fields, through `airgrabber_settings`); `subtitles_enabled` / `_mode` / `_for` are read only by
  the one-time `carry_subtitles`. `subtitles_folder` is Where downloads go's "Where finished downloads
  end up" (Forge's watching, free space, "Open the folder"). `subtitle_files` / `subtitle_grabs` stay in
  `airgrabber.db`, unread and unwritten.
- The old MediaForge's project folder (`…\scripts\MediaForge`) is read-only reference, like `airgrabber.py`.
  Its install and data (`%APPDATA%\MediaForge`) are only read by `app/forge/migration.py` (a snapshot)
  and its uninstaller only started by `app/forge/old_install.py` on the user's click; tests hide both.
- Right-click hand-over: `main.py --forge [--confirm] …` hands its request to a running AirGrabber
  (`app/single_instance.py`) before logging and the package check, so that path stays stdlib-only. A plain
  request shows the main window on MediaForge's page; a confirm request only its confirm window, and a start for
  it leaves when the last one closes unless it stays in the tray (D5, `quits_after_forge`).
- MediaForge's Explorer right-click menu is written only by `app/forge/context_menu.py`, under
  HKCU\Software\Classes (`*\shell\MediaForge`, `Directory\shell\MediaForge`) only, never over a menu
  that starts another copy or program (the old MediaForge's only by the import); tests use a fake
  registry (`tests/forge/base.py` patches the real writers). On for a new install (`--forge-menu on`).
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
- A .torrent holding a program (`.exe` and the like) never leaves `DownloadService._obtain`
  (`app/services/torrent_files.py`, `ProgramInTorrent`): malware dressed up as a video. Magnets can't be read first.
- qBittorrent's settings file (`%APPDATA%\qBittorrent\qBittorrent.ini`) is changed only by
  `app/services/qbittorrent_setup.py`: just the "Add new torrent" dialog keys (and its look's and
  Web UI's keys, below), only while qBittorrent isn't running, the original backed up once; never
  accept its legal notice for the user. Settings' "Reinstall qBittorrent…" (winget `--force`) only
  while it isn't running; its Clean start renames that folder and `%LOCALAPPDATA%\qBittorrent`
  aside (`set_aside`), never deletes them. install.ps1 installs qBittorrent only when Windows has no
  torrent program (`-NoQBittorrent` skips it) and sets it up through `main.py --setup-qbittorrent`.
  AirGrabber may register qBittorrent for magnet links / .torrent files (`QBittorrentLinker`,
  app/services/torrent_app.py) under HKCU\Software\Classes only (never UserChoice or HKLM), and
  only when no other torrent program is set; tests use a fake registry.
  The Web UI for AirGrabber (`app/services/qbittorrent_webui.py`, both key sets: `[Preferences]
  WebUI\…` for 4.2–5.2, `[WebUI]` for 5.3+): on, 127.0.0.1 only, a free port, user `airgrabber`, a
  PBKDF2 password, LocalHostAuth on; never over a Web UI of the user's own. AirGrabber is pointed at
  it (REMOTE, `app/services/qbittorrent_local.py`) only from APP mode, its own, or a new install —
  never over a NAS / folder the user chose — and starts qBittorrent when that one isn't running.
  The generated password is `remote_password`: never logged. Tests use temporary files, a fake
  process check, fake starters and ports; never start qBittorrent.
- The TMDB v3 key travels as a URL parameter and Prowlarr's `TorrentRelease.download_url` embeds its
  API key: never log request URLs or release objects, never put `str(httpx_exc)` in messages, and keep
  the `httpx`/`httpcore` loggers at WARNING (`app/config.py`). DSM passwords go in POST bodies only.
  The remote programs' status calls (Downloads page) keep their sign-in (DSM sid, qBittorrent
  SID cookie, Transmission session id) in memory only, never logged or shown; a task's link
  (magnet / `uri`, may hold a tracker passkey) is read for its hash or file name and dropped.
  OpenSubtitles: AirGrabber's one API key (`APP_API_KEY` in `app/forge/clients/opensubtitles.py`) ships
  with the app and is public, but is still never logged; it travels in the `Api-Key` header. A user's
  own key (`opensubtitles_own_key`, replaces the shipped one when filled in) is a secret: header only,
  never in a URL, message or log. Signing in with an account is optional: the password only in the
  /login POST body, the session token in the `Authorization` header and in memory only; /download's
  temporary links are never logged.
- Plex (`app/services/plex.py`): sign-in only through Plex's PIN flow (never a password). The token
  (`plex_token`) is a secret: the `X-Plex-Token` header only, never in a URL, log line, message or
  exception text; never in a backup, not even with keys and passwords (`MACHINE_SECRETS` in
  `app/services/backup.py`; all `plex_*` settings are this PC's). Plex requests never follow redirects,
  their URLs are never logged, nor `str(httpx_exc)`. Logs give ids and counts only. What is in Plex
  is the only thing that counts as had (`plex_episodes` / `plex_movies`, replaced per check); without
  Plex connected (no token) the UI shows no have / missing cues at all, not even for AirGrabber's own
  grabs (owner's decision, 3.1.0): no ✓, "missing", counts or manual marks (`have_episodes` is kept,
  never read). The download history keeps things from being fetched twice; with Plex connected the
  calendar also marks a grab of the last 14 days that Plex doesn't have "✓ Downloaded" (on its way;
  older means it was deleted there). Connecting or
  disconnecting reloads the pages (`on_change`). Tests use `tests/fixtures/plex/` and MockTransport
  (`tests/plex_link.py` for settings with a token); never plex.tv or a real server.
- The phone connection (`app/services/phone/`, page `app/ui/phone_settings.py`, for the Companion app; design in
  `docs/companion/`): for now only on a PC whose data folder holds the file `phone-connection.enabled` (`ENABLED_MARKER`,
  owner's decision): without it no Settings page, `PhoneService.run` ends at once and nothing listens, even with
  `phone_connection_on` true, and no text tells others about it (no What's new line either). Opening it up is one line:
  `OPEN_TO_EVERYONE` in `app/services/phone/service.py`. Home addresses and paired phones only; only a token's hash is
  kept; the Plex token only in `/v1/pair`'s and `/v1/hello`'s answers; release links never leave the PC; the log gets no
  token, code, address or exception text. `cryptography` and `segno` are optional (below `# optional`), imported only
  inside `certificate.py`'s and `code_picture`'s functions; without them the page says so.
  Roles: each phone's own Library manager tick in Settings decides its role (the first phone pairs as one; `manager` in
  `/v1/pair` and `/v1/hello`; missing means manager, so a phone from before keeps its tabs). Settings also renames a phone
  and sets its person (`change_phone`). Send to phone (`sends.py`, `PUT /v1/on-phone`, design `docs/companion/2026-11-08-send-to-phone-design.md`):
  orders and each phone's last report in `Settings.phone_orders` / `phone_contents`, this PC's own, titles never logged; the Forge routes refuse others with 403 `not_manager`. `GET /v1/suggestions` is
  what the phone may fetch, without what Plex has; its genres are always in English with compound names split
  (`_TWO_GENRES`), since the phone joins them to Plex's by name. `/v1/shortlist` is tonight's shared list, this PC's own;
  a tap fetches by the row's id only, never by its text. No Plex, no phone app (owner, 2026-11-08): without a Plex
  token the page is one line and no code is made, and every route but `/v1/pair` / `/v1/hello` answers 409 `no_plex`.

See `filemap.md` for where things live.
