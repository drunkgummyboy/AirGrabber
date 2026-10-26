Transmission RPC answers, constructed from its RPC spec (no live server was called):
https://github.com/transmission/transmission/blob/4.0.x/docs/rpc-spec.md

- `session_get.json`: `session-get` (section 4.1: `version` = "$version ($revision)",
  `rpc-version`, `rpc-version-semver`), trimmed to a few keys
- `torrent_add_added.json` / `torrent_add_duplicate.json`: `torrent-add` (section 3.4: a
  `torrent-added` or `torrent-duplicate` object with id, name, hashString; both "success")
- `torrent_add_failure.json`: a non-success `result` (an error string), as libtransmission
  reports a bad .torrent
- 409 + X-Transmission-Session-Id (section 2.3.1) and Basic auth (2.3.3) are answered by the fake
  server in tests/test_transmission.py
- `torrent_get.json`: `torrent-get` (section 3.3: `torrents` with the asked `fields`; `status`
  0 stopped … 4 downloading … 6 seeding (3.3 table), `error` 0 ok / 1 tracker warning / 2 tracker
  error / 3 local error, `percentDone` 0…1, `isFinished`)
- `free_space.json`: `free-space` (section 4.4: `path`, `size-bytes`, and `total_size` from
  Transmission 4.0)
