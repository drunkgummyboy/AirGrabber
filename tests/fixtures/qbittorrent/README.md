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
