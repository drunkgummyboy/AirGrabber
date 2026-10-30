# AirGrabber look for qBittorrent

A qBittorrent UI theme made for AirGrabber (https://github.com/drunkgummyboy/AirGrabber): its
slate-blue accent and edges, rounded buttons and tabs, and its green / amber / red for the
torrents' states. AirGrabber copies this folder to `%LOCALAPPDATA%\AirGrabber\qbittorrent-theme`
and points qBittorrent at its `config.json` while setting qBittorrent up (only while qBittorrent is
closed, and never over a theme of your own). Settings → Where downloads go → "AirGrabber look in
qBittorrent" turns it off again; so does qBittorrent's Options → Behavior → "Use custom UI theme".

- `config.json`: qBittorrent's colours. `colors` is the light set, `colors.dark` the dark one; the
  palette overrides (`Palette.*`, only the selection accent here) come from `colors` alone, in
  both modes.
- `stylesheet.qss`: the shapes and accents. Backgrounds and text come from the palette, so the same
  file suits light and dark.

Light or dark: qBittorrent 5 follows Windows' light or dark mode (or its own Options → Behavior →
Color scheme, where offered). qBittorrent 4.4 – 4.6 always show their light look with these
accents. Folder themes need qBittorrent 4.4 or later.

Torrent states: downloading green, complete (seeding or stopped) slate blue, paused before it's
complete amber, queued yellow, stalled / checking / moving muted, errors and missing files red.
