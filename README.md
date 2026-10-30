# AirGrabber

Track your shows. Grab your movies.

A Windows desktop app: a calendar of the episodes airing for the shows you follow (TVMaze), TV and
movie discovery (TMDB), and a torrent search across several indexers that saves `.torrent` files to
your watch folder or sends magnets to Synology Download Station.

## Install

On Windows 10 or 11:

1. Click **Start**, type **powershell** and open **Windows PowerShell**.
2. Copy this line, paste it into the blue window (Ctrl+V or right-click) and press **Enter**:

   ```
   irm is.gd/airgrabber | iex
   ```

3. Follow the questions.

AirGrabber then appears in the Start menu and on your desktop, and opens by itself. If you don't have
Python yet (the program AirGrabber runs on), the installer adds it for you. If you have no torrent
program yet, it also installs qBittorrent (the free program AirGrabber downloads with) and sets it up
so downloads start with one click, without a pop-up for each one; a torrent program you already have
is left alone. It also asks whether to add a free helper program (Prowlarr) that searches more
websites; you can say no.

To install without qBittorrent, use this line instead:

```
& ([scriptblock]::Create((irm is.gd/airgrabber))) -NoQBittorrent
```

Only paste commands like this from sources you trust. This one downloads AirGrabber's installer,
[`install.ps1`](install.ps1), from this page; you can read it first. `is.gd/airgrabber` is a short
link to it; the same command with the full address is:

```
irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex
```

## Update

AirGrabber updates itself: new versions download in the background and install when you close it.
You can also check under ⚙ Settings → **Check for updates**. Your library and settings are kept.

## Uninstall

Open Windows **Settings → Apps → Installed apps**, find **AirGrabber** and choose **Uninstall**.

Windows then asks whether to **also delete your library and settings** (your followed shows,
download history and settings):

- Answer **No** to keep them. If you install AirGrabber again later, everything is still there.
- Answer **Yes** only if you want them gone for good.

Python and helper programs (like qBittorrent and Prowlarr) stay installed; remove them the same
way if you no longer need them.

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
