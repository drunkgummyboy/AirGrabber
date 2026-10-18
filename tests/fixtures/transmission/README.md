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
