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
- The Microsoft Store's Python (`app.config.store_python`) writes AppData into a private copy other programs
  can't see: a data-folder file handed to another program goes as its `os.path.realpath`. install.ps1 skips that
  Python and brings its library over once (`Copy-StoreData`).
- One asyncio loop drives Tk (async-tkinter-loop). No threads, executors or UI queues for I/O:
  await httpx/aiosqlite directly, then update widgets. CPU-heavy Pillow work goes through `asyncio.to_thread`.
- The one exception: the tray icon (`app/ui/tray.py`, pystray) runs its own Win32 message loop on a daemon
  thread. Its callbacks may only hand work to the loop with `loop.call_soon_threadsafe` and never touch Tk.
  And the quit watchdog (`app/ui/quit_watchdog.py`, 2026-11-09): armed once quitting starts, it logs every
  thread's stack and ends the process if closing takes over a minute; it touches neither Tk nor the loop.
- The player (`app/media_player.py`, `app/media_player_window.py`) is a separate process, so it may use threads and
  libVLC while the app's side (`app/ui/media_player.py`) keeps the one loop. The Plex token never reaches the player
  (command line, stdin or log); it gets a file path (often a share), never a URL, and makes no requests of its own; it
  logs no paths, file names or titles. python-vlc is optional (below `# optional`); without it ▶ Play falls back to
  VLC / Windows' player.
- A plain second start waits for the running copy's answer (`single_instance.show_running`); a copy that
  is quitting gives none, and the new start waits for it to close, then runs (before, nothing opened).
- GC guard (D6, adopted 2026-11-08 after F-28's soaks had said no): `app/ui/gc_guard.py`, started in `main._run`: automatic GC off,
  the Tk thread collects on a timer. A collection on aiosqlite's thread had freed a Tk font there and hung that thread
  for good in `Font.__del__` (every search and grab froze). Never turn automatic GC back on; keep Tk images made and dropped on the loop.
- Settings (`app/ui/settings_dialog.py`, pages in `app/ui/settings_pages_registry.py`, opens on Overview's cards,
  `app/ui/settings_overview.py`, which never start Forge): every change stores itself (`app/ui/settings_commit.py`).
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
  the main window at its start) are ctypes on the Tk thread behind a seam (tests: fakes). Its Series / Movies
  folders are marked in Settings → Folders' lists (carried once, `folders_carry.py`).
- The bridges between AirGrabber and Forge (phase 2): no SQL across `airgrabber.db` and `forge.db`.
  Forge's move records (`ForgeMoveService`, `app/forge/core/moves.py`) reach AirGrabber only through
  `app/services/download_status.py` (where a grab was moved, matched by a film's TMDB id or the
  release name; a moved grab shows as finished whatever its torrent program says). There is no
  index of Forge's library and no "have it" from it (removed by the owner's decision, 2026-11-02); the user's own
  folders count only through the folder library (below), which reads them itself. AirGrabber's start
  imports Forge's types only under `TYPE_CHECKING`; these log ids and counts, never a file's path.
  The other bridge: `app/services/plex_scans.py` hears the same move records and has Plex scan
  the folders they went to (`PlexService.scan_folders`).
- Where a watched download goes: the first on rule from the top that holds and fits it (`app/forge/core/rules.py`,
  forge.db's `rules`, v5; `RuleService`), by its preset + changed extras to its destination (a library folder, another
  folder, or sorted inside its watched folder); a file no rule fits is left alone. A rule's destination is never
  watched; a Stays rule's past outputs (move records) never come back. Nothing moves unseen: what a new Automatic rule
  or folder, or an un-pause, finds already there waits as Manual once (`manual_once`, kept across restarts). Logs never
  name a folder or a rule. `ForgeSettings`' `watch_folders` / `series_preset` / `movies_preset` are read only by the
  one-time carry-over; `automatic` "off" is the pause switch. Files added by hand: a preset's `move_to_library` extra
  (`app/forge/core/library.py`'s `route_inputs`; one-time `carry_move_option` ticks Series (Folder) and Movie (Folder)):
  on, to the Series / Movies folder, whose absence leaves the file alone and says so; off, renamed where it is. The
  queue's band says which ("📦 Moves to" / "📁 Sorted in", "Rule: …").
- Subtitles are MediaForge's (`app/forge/core/subtitles.py`, `services.subtitles`): a preset's Subtitles
  extra (right after a rename, on every route, through the executor; a failure never touches the
  rename) and CC ▾ on the queue's done bands and History's rows, drawn on the list's canvas. One try,
  no background checks; the disk is the only memory (no table in `forge.db`); a fetched subtitle is
  journaled in its video's run, so Undo takes it along. The settings stay in AirGrabber's `Settings`
  (same fields, through `airgrabber_settings`); `subtitles_enabled` / `_mode` / `_for` are read only by
  the one-time `carry_subtitles`. `subtitles_folder` is the Folders page's "Finished downloads"
  (Forge's watching, free space, "Open the folder"). `subtitle_files` / `subtitle_grabs` stay in
  `airgrabber.db`, unread and unwritten.
- The old MediaForge's project folder (`…\scripts\MediaForge`) is read-only reference, like `airgrabber.py`.
  Its install and data (`%APPDATA%\MediaForge`) are only read by `app/forge/migration.py` (a snapshot)
  and its uninstaller only started by `app/forge/old_install.py` on the user's click; tests hide both.
- Explorer's "Open with" AirGrabber player (`main.py --play FILE`, handed over like `--forge`; a first start
  stays hidden and leaves when the player closes, D5's rule) is written only by `app/services/open_with.py`, under
  HKCU\Software\Classes only (ProgID `AirGrabber.Video` + each video extension's OpenWithProgids), never a default,
  UserChoice or another copy's; tests/__init__.py refuses the real writers. Such a file never reaches Plex; its
  place is kept by a hash of its path only (`app/services/local_files.py`).
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
  (`app/services/torrent_files.py`, `ProgramInTorrent`): malware dressed up as a video. Magnets can't be read first,
  so a release caught once is remembered by its info hash (`Settings.refused_hashes`) and never sent again.
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
  their URLs are never logged, nor `str(httpx_exc)`. Logs give ids and counts only. What counts as
  had is what is in Plex (`plex_episodes` / `plex_movies`, replaced per Plex check) or in the user's
  own folders (`folder_episodes` / `folder_movies`, replaced per folder check), or what left the
  folders less than `GONE_GRACE_DAYS` (3) ago (`folder_gone`: moved by hand, Plex hasn't scanned it
  yet); `_IN_LIBRARY` in `app/database.py`. Neither source's check, Plex's Disconnect nor "Count what's
  in them" off touches the other's rows. Without a library (`has_library`, `app/services/library_source.py`:
  no token and the folders off) the UI shows no have / missing cues at all, not even for AirGrabber's
  own grabs (owner's decision, 3.1.0): no ✓, "missing", counts or manual marks (`have_episodes` is
  kept, never read). The words say where (`app/ui/library_words.py`): what Plex has "✓ In Plex";
  beside Plex, what only the folders have "✓ Downloaded"; with the folders only, "In your library".
  The download history keeps things from being fetched twice; with a library the calendar also
  marks a grab of the last 14 days that isn't had "✓ Downloaded" (on its way; older means it was
  deleted there). Connecting or
  disconnecting reloads the pages (`on_change`). Tests use `tests/fixtures/plex/` and MockTransport
  (`tests/plex_link.py` for settings with a token); never plex.tv or a real server.
- The user's own folders (`app/services/folder_library/`, docs/folder-library/): off until "Count what's
  in them" (`folder_library_on`; `folders_on` = on with a folder set), then counted beside Plex or
  without it; their lists are Settings → Folders' one list per kind (`app/ui/folders_page.py`).
  Read-only (never a file moved, renamed or deleted); each folder read in a thread under its time
  limit (`scan.py`), so a NAS asleep never holds the window and keeps its last result; logs give
  counts and kinds only, never a path, file name, title or TMDB id. Its rows: `library_files` and the
  `folder_*` tables (`db.folders`, FolderRepository, v13), never `plex_*`. The Library page's
  two shelves, Plex and Downloaded (`Shelf`), are never merged; a show's or film's window has both
  together, each video played from where it is (`is_folder_video`): the folder copy where both have
  one, Plex's copy when the folder's isn't there. Tests: temporary folders and fakes, never a real
  NAS, Plex or TMDB.
- The phone connection (`app/services/phone/`, page `app/ui/phone_settings.py`, for the Companion app; design in
  `docs/companion/`): open to everyone since 3.23.0 (`OPEN_TO_EVERYONE` in `app/services/phone/service.py`, owner's
  decision 2026-11-20): every copy has its Settings page, but nothing listens until `phone_connection_on` (off at first).
  Before, it was only on a PC whose data folder held `phone-connection.enabled` (`ENABLED_MARKER`); setting the constant
  back to False brings that back, with the hidden switch (seven quick clicks on Settings → Updates & backup's
  "Version …" write or delete that file, `switch_here`; while open, the clicks aren't bound: `can_switch`). The Companion
  app's download code (`app/ui/phone_app.py`, `PHONE_APP_URL`: the APK as the `phone-app` GitHub release's file,
  replaced per Companion version, uploaded only with the owner's OK). Home addresses and paired phones only; only a token's hash is
  kept; the Plex token only in `/v1/pair`'s and `/v1/hello`'s answers; release links never leave the PC; the log gets no
  token, code, address or exception text. `cryptography` and `segno` are optional (below `# optional`), imported only
  inside `certificate.py`'s and `code_picture`'s functions; without them the page says so.
  Roles: each phone's own Library manager tick in Settings decides its role (the first phone pairs as one; `manager` in
  `/v1/pair` and `/v1/hello`; missing means manager, so a phone from before keeps its tabs). Settings also renames a phone
  and sets its person (`change_phone`). Send to phone (`sends.py`, `PUT /v1/on-phone`, design `docs/companion/2026-11-08-send-to-phone-design.md`):
  orders and each phone's last report in `Settings.phone_orders` / `phone_contents`, this PC's own, titles never logged; the Forge routes refuse others with 403 `not_manager`. `GET /v1/suggestions` is
  what the phone may fetch, without what is had (Plex's or the folders'); its genres are always in English with compound names split
  (`_TWO_GENRES`), since the phone joins them to Plex's by name. `/v1/shortlist` is tonight's shared list, this PC's own;
  a tap fetches by the row's id only, never by its text. `/v1/find` (`find.py`) searches TMDB by name; only what it found
  this run (TMDB ids, a film with `found: true`, a single episode as `found_episode`) can be fetched or followed, and its log never gets the text, a
  title or a TMDB id; `/v1/person` (`person.py`) lists an actor's credits as find's results (found too, logged as counts only),
  and `downloading` items' `progress` reads only `DownloadStatusService.latest` (`progress.py`). No library, no phone app (owner, 2026-11-08; Plex or the
  user's own folders, `library_source`): without one the page is one line and no code is made, and every route but
  `/v1/pair` / `/v1/hello` answers 409 (`_needs_library`). A Companion that knows the folder library says so on every
  request (`X-Companion-Can: folder-library`, `knows_folders`); one that doesn't sees `no_plex` unless Plex is connected
  (old Companions see today's PC), one that does `no_library` without either. `hello` gives `library` (the main source:
  "plex" with a token, else "folders", else null), `folders` (`folders_on`, beside Plex too: the phone's Downloaded
  section), `plex` stays null without Plex, `api_version` 1. The folder library's routes (`library.py`, `/v1/library…`,
  contract `docs/folder-library/2026-11-16-companion-contract.md`) answer whenever the folders are on, with Plex or
  without (`_while_folders_on`; 409 `plex_library` only while they are off): lists
  from the database, every share read in a thread under `READ_TIMEOUT_S` (a share asleep is 404 `no_longer_there`,
  never a window that waits), places and watched marks the phone's own person's (`PairedPhone.profile_id`), never the
  PC's. Play links (`play_links.py`, for the phone's player and TVs): port 58762, plain http, home addresses only, a
  `secrets` token good for 6 h, in memory only; the link, its token, the address and the path are never logged.
  Phone-sized copies (`copies.py`, contract in the Companion's docs/2026-11-09-phone-sized-copies.md): a 4K film fetched
  from Plex into `<data>\phone-copies` (a folder library film's made straight from the PC's own file, `local_source`)
  and made 1080p HEVC on the card (MediaForge's ffmpeg, `hevc_nvenc`), one at a time; Send to phone takes
  a folder video's key (`movie:<tmdb>`, `episode:<tmdb>:<s>:<e>`; `_PHONE_KEY`), the copies a Plex ratingKey or `movie:<tmdb>` only (`_film_key`), logged only as its kind (`_logged`); `hello`'s `phone_copies` says whether the card works. Send to phone's Phone-sized tick (4K films) starts one from
  the PC at once (`phone_sized` in the order; cancelling drops it; `resume_copies` after a restart). The file goes out as a `FileReply` (server.py, Range).
  ffmpeg runs as a subprocess of the loop (no threads); its output holds paths: never logged. Tests fake Plex and ffmpeg.
  Cast sound streams (`cast_stream.py`, owner's OK 2026-11-21; docs/companion/2026-11-21-cast-sound-stream.md): for a Cast TV
  that can't play Dolby / DTS / TrueHD, `POST /v1/cast-stream` finds the file on this PC (never through Plex's stream) and
  makes a `/c/<token>/stream.mp4` link on the play link server (same port, home addresses only, PLAY_LINK_HOURS, in memory
  only); each GET runs MediaForge's ffmpeg (picture copied, sound to AAC, fragmented MP4 from `?t=`), at most MAX_STREAMS
  at once, killed and reaped when the TV goes away, a newer request comes or the server stops. `hello`'s `cast_sound` says
  whether this PC can. The link, token, address, path, title and ffmpeg's own words are never logged; a Plex ratingKey may be.

See `filemap.md` for where things live.
