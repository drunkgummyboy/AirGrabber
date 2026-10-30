Answers of qBittorrent's Web API v2, constructed from its documentation and source (no live
server was called):

- `app_version.txt`: `GET /api/v2/app/version` ("The response is a string with the application
  version, e.g. v4.1.3"), https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
- login "Ok." / "Fails." and 403 for a banned IP: `AuthController::loginAction`,
  src/webui/api/authcontroller.cpp (v5_1_x)
- torrents/add "Ok." / "Fails." and 415 (BadData) for an invalid file: `TorrentsController::addAction`,
  src/webui/api/torrentscontroller.cpp (v5_1_x)
- `torrents_add_json.json`: the JSON counts `addAction` sets on the master branch (success_count,
  failure_count, pending_count, added_torrent_ids; nothing added = HTTP 409 Conflict)
- `torrents_info.json`: `GET /api/v2/torrents/info` (wiki "Get torrent list": a JSON array with
  `hash`, `infohash_v1` (v4.4+), `name`, `progress` 0…1 and `state`, e.g. downloading, queuedDL,
  pausedDL, stoppedUP (v5 renamed paused* to stopped*), missingFiles), trimmed to a few keys
- `sync_maindata.json`: `GET /api/v2/sync/maindata` (wiki "Get main data": `server_state` with
  `free_space_on_disk` in bytes), trimmed

qBittorrent's own settings file (`%APPDATA%\qBittorrent\qBittorrent.ini`, QSettings INI with
Windows line endings), built from its source (src/base/preferences.cpp, settingsstorage.cpp,
src/app/upgrade.cpp, bittorrent/sessionimpl.cpp; no real file was read), for
tests/test_qbittorrent_setup.py:

- `settings_5.ini`: a 5.x file (Meta MigrationVersion 8): the "Add new torrent" dialog's switch
  would be `[AddNewTorrentDialog] Enabled` (absent = on), the save folder is `[BitTorrent]
  Session\DefaultSavePath`, the legal notice accepted
- `settings_4.ini`: a 4.3 file: the switch is `[Preferences] Downloads\NewAdditionDialog=true`,
  the save folder `Downloads\SavePath` (with a trailing slash, as 4.3 kept it)
- `settings_5_own_theme.ini`: `settings_5.ini` with a theme of the user's own in use: `[Preferences]
  General\UseCustomUITheme=true` and `General\CustomUIThemePath` naming a .qbtheme file
  (src/base/preferences.cpp `useCustomUITheme` / `customUIThemePath`; the path as QSettings writes
  qBittorrent's `Path::toString()`, backslashes doubled), for the AirGrabber-look tests
- `settings_5_3_webui.ini`: a 5.3 file (Meta MigrationVersion 12, after 5.3's src/app/upgrade.cpp
  `migrateWebUISettings` moved the Web UI keys from `[Preferences] WebUI\…` to `[WebUI]`) with a
  Web UI of the user's own turned on: `[WebUI] Enabled=true`, `Port=8080`, `Username=admin` and
  `Password_PBKDF2` = the built-in hash of "adminadmin" (the default in 4.2.0's and
  4.3.9's src/base/preferences.cpp `getWebUIPassword`), written as QSettings writes a QByteArray
  (`"@ByteArray(…)"`, quoted for its "="), for tests/test_qbittorrent_webui.py
