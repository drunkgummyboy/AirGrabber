"""What's new in each release: the short notes shown once after an update (MainWindow, with
app/ui/whats_new_dialog.py). How a release adds its notes, each text in N_() with its Dutch in
app/i18n_nl.py (the dialog shows them through t()):

- a big feature: a Feature in FEATURES (at most 3): a title, one sentence and a drawn picture
  without words, a 1464 x 488 PNG from tools/whats_new_art.py in app/assets/whats_new. Only the
  newest release's pictures stay there: the older Features go with them.
- something smaller: a short line in plain words in NOTES (3–6 of them where there is no Feature).

Stdlib only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from app import APP_NAME
from app.bootstrap import version_tuple
from app.i18n import N_, t

# Version -> its notes, one line each.
NOTES: dict[str, tuple[str, ...]] = {
    "3.9.0": (
        N_("The rolling week shows what's coming up: under the days, the next episode of each of your shows."),
        N_("The number on MediaForge now counts every file waiting in the queue, not only those that need you."),
        N_("Settings, Naming: remembered matches moved to a small window of their own."),
        N_("LostFilm's Russian releases are always skipped."),
    ),
    "3.7.0": (
        N_("A torrent is read before it downloads: one holding a program, like a fake episode.exe, is refused."),
        N_("Calendar cards show ✓ In Plex and ✓ Downloaded, and keep them after a restart or an update."),
        N_("A small loader in the footer shows while AirGrabber reads your Plex."),
        N_("MediaForge has a ↻ to look for new downloads now, and looks by itself when you open it."),
        N_("A season pack no longer counts for episodes that came out after you downloaded it."),
    ),
    "3.6.0": (
        N_("MediaForge has an Auto and Manual switch: in Auto the preset bar stays out of the way."),
        N_("A rolling week for the calendar, with today in the middle: Settings, Looks, Calendar view."),
        N_("Season packs are judged by your episode size times the season's episodes."),
        N_("The movie watchlist is sorted by release date."),
        N_("A click on the main window closes info panels and search results."),
        N_("The pages take a tint of your accent colour, and the top fades out more softly."),
    ),
    "3.5.0": (
        N_("The main menu moved to the top, with each page's own buttons on one row under it."),
        N_("The calendar's date sits between two new arrows; click the date to go back to today."),
        N_("A series' trailer button follows the season you view; Discover shows and filters by streaming service."),
        N_("MediaForge's History keeps 500 runs and has a search; a warning about a folder now opens the setting."),
        N_("MediaForge: Choose… on every show and film; new options for quality in the name, NFO files and backgrounds."),
        N_("From now on, if an update won't start AirGrabber goes back by itself; Settings offers the way back too."),
    ),
    "3.4.0": (
        N_("The calendar now also suggests shows from streaming services like Netflix and Disney+."),
        N_("Calendar cards say where a show airs, like \"S03E05 · Netflix\"."),
        N_("Streaming suggestions come from worldwide and US services, not YouTube or Twitch."),
    ),
    "3.3.1": (
        N_("Auto-rename's Trial is now called Manual."),
        N_("On Manual, downloads MediaForge finds wait in the queue until you forge them."),
        N_("Downloads that finished while AirGrabber was closed now show up about a minute after opening."),
    ),
    "3.3.0": (
        N_("New preset extra: Move to library folder sends forged files to your Series or Movies folder."),
        N_("Series (Folder) and Movie (Folder) have it on; turn it off to rename files where they are."),
        N_("The queue now says for each show or film whether its files will move or stay."),
        N_("The format box shows every code with what it gives, and examples you can click to use."),
        N_("New format codes: y for the year, and sxe for the 1x02 style."),
    ),
    "3.2.0": (
        N_("History is behind the 🕘 next to Settings."),
        N_("MediaForge now has AirGrabber's colours."),
        N_("Auto-rename moved to Settings, and there is now one Settings window for everything."),
        N_("Subtitles are part of MediaForge now, and prefer the release of your file's original name."),
        N_("New setting: remove files from the queue after they're forged."),
        N_("Ignored files and folders that are gone drop off the Ignored list by themselves."),
    ),
    "3.1.2": (
        N_("The show window always has a season's download buttons, also when it's already in Plex."),
        N_("MediaForge's format codes are now buttons: click one to add it to the format."),
        N_("MediaForge's Ignored list shows just the names; hover over one for its whole path."),
    ),
    "3.1.1": (
        N_("New here? \"Let's get started\" lets you pick a few current series to follow in one go."),
        N_("It opens from the empty calendar or from My shows (+ Pick shows…)."),
        N_("MediaForge's \"Which show is this?\" has a search box for when none of its suggestions is right."),
    ),
    "3.1.0": (
        N_("Connect Plex in Settings → Plex: what's in your Plex libraries counts as something you already have."),
        N_("Episodes in Plex are no longer missing, and automatic downloads skip episodes and films you have in Plex."),
        N_("A quiet ✓ \"In Plex\" shows on those episodes and films."),
        N_("Without Plex, AirGrabber no longer marks what you have or what you're missing."),
    ),
    "3.0.2": (
        N_("The Downloads page now shows everything in your torrent program, also what you added there yourself."),
        N_("Pause, resume or remove a download from its ⋯ menu; AirGrabber never deletes your files."),
        N_("A damaged or wrong .torrent file is no longer sent to your torrent program."),
        N_("Search again right in the search results window, and click the logo to go back to the calendar."),
        N_("A calmer calendar: no suggestions in past weeks, and \"+ Track\" no longer reloads the page."),
        N_("MediaForge's Ignore menu stays open when you click it."),
    ),
    "3.0.1": (
        N_("Everyone at home can have their own series, watchlist and calendar: click your name at the top right."),
        N_("Downloads now wait in a queue: click a whole season and close the window; the Downloads page shows what's waiting."),
        N_("Each season has a \"Whole season\" button that looks for a season pack first."),
        N_("MediaForge has its own Settings (⚙), and every item in its queue can have its own preset."),
        N_("Ignore keeps a file or folder out of MediaForge's Auto-rename, which now only renames videos whose name matches."),
        N_("AirGrabber opens on the calendar, and a series opened from search comes to the front."),
    ),
    "3.0.0": (
        N_("MediaForge is now part of AirGrabber: open it with the 🔨 button, from the tray or from the Start menu."),
        N_("Your old MediaForge's presets, folders and history come along the first time you open it."),
        N_("Right-click files in Explorer to rename them, and let MediaForge rename AirGrabber's downloads by itself."),
        N_("The Downloads page shows where a download ended up, and its subtitles move along with it."),
        N_("Trailers now play in their own window for more videos, with a Fill button against black bars."),
    ),
    "2.0.11": (
        N_("The Series / Movies switch is bigger and moved down, next to Calendar, Discover and Tracked."),
        N_("The top bar keeps just the logo and the search, so it looks calmer."),
        N_("The switch stays in the same place on every page, so nothing jumps when you switch."),
    ),
    "2.0.10": (
        N_("Save a backup of your shows, watchlist and settings, and load it on a new PC (Settings → Backup)."),
        N_("Films can go to their own folder: fill in Films folder in Settings → Where downloads go."),
        N_("The releases list marks the one ⬇ Auto would pick with ★ and shows how fast each will download."),
        N_("Trailers now play in their own AirGrabber window instead of the browser."),
        N_("A clearer top bar and info screen: bigger poster, calmer episode marks, softer fades."),
        N_("Saturdays in the calendar get suggestions too."),
    ),
    "2.0.9": (
        N_(
            "A new top bar with a spotlight on tonight's episodes or new films; click it to open "
            "the title."
        ),
        N_("The info screen and the releases are now one window: pick a release right beside the info."),
        N_("Choose how the info screen looks in Settings → Looks: side by side or with tabs."),
        N_("Pick one of ten accent colours in Settings → Looks or during setup."),
        N_("Pop-up windows lost their title bar: drag them anywhere and close them with the round ✕."),
        N_("The calendar fills each week with suggestions up to the height of its busiest day."),
    ),
    "2.0.8": (
        N_(
            "After installing qBittorrent, AirGrabber now sets Windows to open downloads with it, "
            "so you don't have to."
        ),
        N_("AirGrabber never does this when you already use another torrent program."),
        N_("\"Clear finished\" also clears downloads your torrent program no longer has."),
    ),
    "2.0.7": (
        N_("An episode now counts as missing one day after it airs; until then it's marked New."),
        N_(
            "Quieter download buttons: ⬇ Auto picks the best release, ☰ Choose… lets you pick, "
            "↻ Again downloads it again."
        ),
        N_("Get subtitles yourself with CC ▾, and choose when and which kind in Settings → Subtitles."),
        N_("The Downloads page can filter, clear finished downloads and open a download's folder."),
        N_(
            "Installing qBittorrent and Prowlarr works when the Microsoft Store can't be reached, "
            "or says why it didn't."
        ),
        N_(
            "The airdates.tv import finds more shows and names any it couldn't add; right-click "
            "a text box to paste."
        ),
    ),
    "2.0.6": (
        N_(
            "New installs come with qBittorrent: downloads start without a pop-up, and the "
            "Downloads page shows their progress."
        ),
        N_(
            "Already use qBittorrent? Set it up for AirGrabber in Settings → Where downloads go."
        ),
        N_(
            "qBittorrent gets AirGrabber's look (light or dark like Windows) when set up; switch "
            "it in Settings → Where downloads go."
        ),
        N_(
            "Click a series or film for its new info screen: a big picture, the trailer, the "
            "cast and the full story."
        ),
        N_("Settings → Looks shows a real card at the size you pick."),
        N_(
            "Quick setup chooses the look, download quality, card size and language in one go "
            "(Settings → Looks)."
        ),
    ),
    "2.0.5": (
        N_(
            "My shows can be sorted (A–Z, next episode, latest download) and filtered (missing "
            "episodes, Auto on, ended)."
        ),
        N_("A new Downloads page shows how far along your downloads are on your NAS."),
        N_(
            "The calendar marks the episodes you still need, and shows several episodes of one "
            "show on the same day as one card."
        ),
        N_("Filter Movies and Discover by genre."),
        N_("AirGrabber now speaks Dutch too, and there's a light theme."),
        N_("Shortcuts: Ctrl+F to search, F5 to refresh the page, Esc to close a window."),
    ),
}


class Feature(NamedTuple):
    """A big feature of a release: its card in the dialog."""

    title: str
    text: str
    picture: str  # a file name in app/assets/whats_new


# Version -> its big features, at most 3.
FEATURES: dict[str, tuple[Feature, ...]] = {
    "3.2.0": (
        Feature(
            N_("MediaForge is now a page in AirGrabber"),
            N_(
                "No second window any more: Series, Movies, MediaForge. The number on it shows "
                "how many files need you."
            ),
            "mediaforge_page.png",
        ),
        Feature(
            N_("Every show or film is one card"),
            N_("Its picture runs behind all its files, and 📂 opens them in Explorer."),
            "one_card.png",
        ),
    ),
}


class WhatsNew(NamedTuple):
    version: str
    lines: tuple[str, ...]
    features: tuple[Feature, ...] = ()


def whats_new(
    last_run: str,
    running: str,
    notes: Mapping[str, tuple[str, ...]] = NOTES,
    features: Mapping[str, tuple[Feature, ...]] = FEATURES,
) -> WhatsNew | None:
    """The notes to show when AirGrabber starts on `running` after `last_run` ran: the newest
    entry (with lines or features) above `last_run` and at most `running`. None on a first start
    (`last_run` empty: the welcome shows instead), when the version didn't go up, or without
    such an entry."""
    old, new = version_tuple(last_run), version_tuple(running)
    if not last_run.strip() or old is None or new is None or new <= old:
        return None
    best: tuple[tuple[int, ...], str] | None = None
    for version in (*notes, *features):
        key = version_tuple(version)
        if key is None or not (notes.get(version) or features.get(version)):
            continue
        if not old < key <= new:
            continue
        if best is None or key > best[0]:
            best = key, version
    if best is None:
        return None
    version = best[1]
    return WhatsNew(version, tuple(notes.get(version, ())), tuple(features.get(version, ())))


def dialog_title(version: str) -> str:
    """ "What's new in AirGrabber 2.0.5"."""
    return t("What's new in {app} {version}", app=APP_NAME, version=version)
