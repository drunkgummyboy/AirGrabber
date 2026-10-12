# AirGrabber

Track your shows. Grab your movies.

A Windows desktop app: a calendar of the episodes airing for the shows you follow (TVMaze), TV and
movie discovery (TMDB), and a torrent search across several indexers that saves `.torrent` files to
your watch folder or sends magnets to Synology Download Station.

## Install

On Windows 10 or 11, open **PowerShell** and run:

```powershell
irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex
```

This installs Python 3.13 if you don't have it, puts AirGrabber in
`%LOCALAPPDATA%\Programs\AirGrabber`, adds Start menu and desktop shortcuts, and starts it.

## Update

Open ⚙ Settings and click **Check for updates**; the status bar also tells you when a new version is
out. Running the install command again works too. Your library and settings live in
`%LOCALAPPDATA%\AirGrabber` and are kept.

## Uninstall

Delete `%LOCALAPPDATA%\Programs\AirGrabber` and the two shortcuts. Also delete
`%LOCALAPPDATA%\AirGrabber` to remove your library and settings.

## AirGrabber 1.x

`airgrabber.py` is the old single-file version. It stays here so 1.x copies can still update
themselves.

## Development

```powershell
python main.py                                  # run from a checkout
python -m unittest discover -s tests -t .       # tests
python -m mypy                                  # strict type check
```

A checkout (anything with a `tests` folder) never updates itself; see `CLAUDE.md` for the project rules.
