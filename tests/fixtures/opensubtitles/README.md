OpenSubtitles.com REST API answers, constructed from its documentation (no live server was
called). Sources, read on 2026-10-19:

- the OpenAPI file https://stoplight.io/api/v1/projects/opensubtitles/opensubtitles-api/nodes/open_api.json
  (rendered at https://opensubtitles.stoplight.io/docs/opensubtitles-api)
- "Getting started" (headers, User-Agent, 5 downloads per IP without login, 20 for a signed-up
  user, counters reset at midnight UTC, JWT valid 24 hours)
- "Best practices" https://stoplight.io/api/v1/projects/opensubtitles/opensubtitles-api/nodes/docs/2-Best-Practices.md
  (sorted lowercase GET parameters, 5 requests a second per IP, 429 "API rate limit exceeded",
  /login 1 request a second, stop after a refused login)
- "Moviehash source codes" https://stoplight.io/api/v1/projects/opensubtitles/opensubtitles-api/nodes/docs/7-Moviehash.md
  (size + 64-bit little-endian sum of the first and last 64 KiB, 9000000000 > size > 131072;
  test vector breakdance.avi, 12,909,756 bytes = 8e245d9679d31e12 — that file is not bundled:
  tests/test_opensubtitles.py checks the documented Python implementation against ours on
  generated files instead)

Files:

- `login.json`: `POST /login` 200 (the OpenAPI example; the token replaced by a fake one)
- `login_vip.json`: the same with `base_url` = vip-api.opensubtitles.com (the schema's enum)
- `infos_user.json`: `GET /infos/user` 200 (the OpenAPI example)
- `infos_formats.json`: `GET /infos/formats` 200 (shape of the documented answer, trimmed)
- `search_episode.json`: `GET /subtitles` 200, attribute names and shape from the OpenAPI
  example ("Desperate Housewives S01E05"); entries arranged to cover the choices: a moviehash
  match, hearing-impaired, machine- and AI-translated, a two-CD subtitle, two languages
- `search_empty.json`: `GET /subtitles` without results
- `download.json`: `POST /download` 200 (the OpenAPI example; the link shortened)
- `download_last.json`: the same with `remaining` 0 (the last download of the day)
- `download_quota.json`: the answer to `POST /download` when the day's downloads are used up.
  The OpenAPI file documents only the 200 answer; this body (HTTP 406, the 200 answer's
  `requests` / `remaining` / `message` / `reset_time` / `reset_time_utc`) follows what API
  clients such as subliminal's opensubtitlescom provider handle (406 = download limit reached)
