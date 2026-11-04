# AirGrabber

Track your shows. Grab your movies.

A Windows desktop app: a calendar of the episodes airing for the shows you follow (TVMaze), TV and
movie discovery (TMDB), and a torrent search across several indexers that saves `.torrent` files to
your watch folder or sends magnets to Synology Download Station.

It also holds **MediaForge**: a page that gives downloaded episodes and films tidy names
and moves them into your Series and Movies folders (see [MediaForge](#mediaforge) below).

## Install

On Windows 10 or 11:

1. Click **Start**, type **powershell** and open **Windows PowerShell**.
2. Copy this line, paste it into the blue window (Ctrl+V or right-click) and press **Enter**:

   ```
   irm is.gd/airgrabber | iex
   ```

3. Follow the questions.

AirGrabber then appears in the Start menu and on your desktop, and opens by itself. MediaForge gets
its own Start-menu entry and an entry in the right-click menu of files and folders. If you don't have
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

MediaForge's Start-menu entry and right-click menu go with it. Python and helper programs (like
qBittorrent and Prowlarr) stay installed; remove them the same way if you no longer need them.

## MediaForge

MediaForge renames downloaded episodes and films with their official names from TMDB (for example
`Gold Rush (2010) - S17E05 - The Big Pour.mkv`) and moves them into your Series and Movies folders, with
posters and, if you want, trailers. Every rename can be undone.

**Opening it**: **MediaForge** in the Series · Movies · MediaForge switch of AirGrabber's window
(its amber number says how many files need you), **Open MediaForge** in the tray icon's menu, or
**MediaForge** in the Start menu. Drag files or folders from Explorer anywhere onto AirGrabber's
window to add them. The 🕘 at the top shows the History of what was renamed, where every run can be
undone.

**The right-click menu**: select files or folders in Explorer, right-click and choose
**MediaForge → Forge: <preset>** (on Windows 11 first **Show more options**). A small window shows
the new names; nothing is renamed until you click **Rename**. It works while AirGrabber is closed
too. Turn it on or off under ⚙ Settings → **MediaForge → Right-click menu**.

**Renaming on its own**: under ⚙ Settings → **MediaForge → Auto-rename**, choose your Series
and Movies folders and the folders to watch (AirGrabber's own download folder can be watched for
you). **Trial** shows what would happen without touching anything; **On** renames finished
downloads by itself. Subtitles next to a download move along with it, and the
Downloads page shows the download as finished with where it went ("Renamed → Series › …").

### Coming from the old MediaForge

1. Install AirGrabber as above and close the old MediaForge.
2. Open MediaForge from AirGrabber. It asks **Bring over your MediaForge settings, presets and
   history?**: choose **Bring over**. (Not now? It asks again next time, and ⚙ Settings →
   MediaForge → Auto-rename has a **Bring over…** button.) Your TMDB key comes along when
   AirGrabber doesn't have one yet.
3. Choose **Remove the old MediaForge** (offered after the import, and in ⚙ Settings → MediaForge
   while the old one is installed). Until it is removed, AirGrabber's MediaForge doesn't rename
   anything on its own, so the two never move the same files. The old MediaForge's data is never
   deleted.

**Licences**: MediaForge reads file names with [guessit](https://github.com/guessit-io/guessit) and
rebulk (LGPL), installed by Python's package installer, not copied into AirGrabber. For trailers it
downloads [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) and
[ffmpeg](https://github.com/yt-dlp/FFmpeg-Builds) (GPL) from their GitHub releases the first time
you ask for one, checks them against their published checksums, and keeps them in AirGrabber's data
folder.

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
