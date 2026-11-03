# Plex fixtures

**Built by hand** from Plex's published API shapes (plex.tv's `/api/v2/pins` and
`/api/v2/resources`, a Plex Media Server's `/identity`, `/library/sections` and
`/library/sections/{key}/all`, all with `Accept: application/json`), not recorded: AirGrabber's
tests never call plex.tv or a real server. Every name, address, id and token in them is made up.

- `pin_new.json`: `POST /api/v2/pins` (a 4-character code, no token yet)
- `pin_linked.json`: `GET /api/v2/pins/{id}` once the code was entered (`authToken`)
- `resources.json`: `GET /api/v2/resources?includeHttps=1&includeRelay=0`: the owned server
  "Alex-NAS" (a remote https, a local http and a local https plex.direct connection, in that
  order), a player (not a server) and a friend's shared server (not owned)
- `resources_two.json`: two owned servers
- `identity.json`: `GET /identity` of Alex-NAS
- `sections.json`: `GET /library/sections`: TV Shows (show, key 1), Movies (movie, key 2), Home
  videos (movie, key 3), Music (artist, key 4: never read)
- `shows.json`: `GET /library/sections/1/all?type=2&includeGuids=1`: shows from Plex's new agent
  (a `Guid` list), the old TheTVDB agent, the old IMDb agent (a TV show), Hama, and one
  AirGrabber doesn't know
- `episodes.json`: `GET /library/sections/1/all?type=4`: episodes of those shows (one without a
  number)
- `movies.json`: `GET /library/sections/2/all?type=1&includeGuids=1`: films from the new agent,
  the old TheMovieDB agent, the old IMDb agent and a local one without any outside id
