# AirGrabber

Track your shows. Grab your movies.

A Windows desktop app: a calendar of the episodes airing for the shows you follow (TVMaze), TV and
movie discovery (TMDB), and a torrent search across several indexers that saves `.torrent` files to
your watch folder or sends magnets to Synology Download Station.

## Install

On Windows 10 or 11:

1. Press the **Windows key** and **R** together. A small "Run" window opens.
2. Paste this line into it:

   ```
   powershell -c "irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex"
   ```

3. Press **Enter** and follow the questions.

AirGrabber then appears in the Start menu and on your desktop, and opens by itself. If you don't have
Python yet (the program AirGrabber runs on), the installer adds it for you. It also asks whether to
add a free helper program (Prowlarr) that searches more websites; you can say no.

Already in PowerShell? Run `irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex`.

## Update

AirGrabber updates itself: new versions download in the background and install when you close it.
You can also check under ⚙ Settings → **Check for updates**. Your library and settings are kept.

## Uninstall

Open Windows **Settings → Apps → Installed apps**, find **AirGrabber** and choose **Uninstall**.
It asks whether to also delete your library and settings. Python and helper programs (like Prowlarr)
stay installed; remove them the same way if you no longer need them.

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
