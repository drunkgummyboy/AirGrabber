"""The Dutch interface texts of Forge (app/forge): the English text → its Dutch one, like
app/i18n_nl.py and merged with it by app/i18n.py. Grouped by the file that shows them, each
group under a `# app/forge/<file>` comment. The same style as app/i18n_nl.py: friendly,
informal Dutch ("je"), short, every {placeholder} of the English text kept. A text lives in one
of the two catalogs, never both; tests/test_i18n.py checks them as one. Stdlib only."""

from __future__ import annotations

NL: dict[str, str] = {
    # Each Forge file's texts under its own "# app/forge/<file>" comment, added below this line.
    # app/forge/ui/queue_rows.py
    "Same folder": "Zelfde map",
    "Needs you": "Wacht op jou",
    "Renamed": "Hernoemd",
    "Done today": "Vandaag klaar",
    "Current name:": "Huidige naam:",
    "Drop video files or folders here": "Sleep videobestanden of mappen hierheen",
    "or add them with the buttons above. Nothing is renamed until you click Forge.":
        "of voeg ze toe met de knoppen hierboven. Er wordt niets hernoemd tot je op Forge klikt.",
    "Nothing needs you": "Niets wacht op jou",
    "Everything in the queue can be renamed.": "Alles in de wachtrij kan worden hernoemd.",
    "Nothing is ready yet": "Nog niets is klaar",
    "The files in the queue need you first.": "De bestanden in de wachtrij wachten eerst op jou.",
    "Film": "Film",
    "Film · {year}": "Film · {year}",
    "Seasons {first}–{last}": "Seizoenen {first}–{last}",
    "Seasons {numbers}": "Seizoenen {numbers}",
    "📦 Moves to {folder}": "📦 Gaat naar {folder}",
    "📦 Moved to {folder}": "📦 Verplaatst naar {folder}",
    "📁 Stays in its folder": "📁 Blijft in zijn map",
    "📁 Stays here, in {folder}": "📁 Blijft hier, in {folder}",
    "📁 Stayed in its folder": "📁 Bleef in zijn map",
    "📁 Stayed here, in {folder}": "📁 Bleef hier, in {folder}",
    "⚠ Choose your Series folder in Settings › Auto-rename":
        "⚠ Kies je map Series in Instellingen › Automatisch hernoemen",
    "⚠ Choose your Movies folder in Settings › Auto-rename":
        "⚠ Kies je map Films in Instellingen › Automatisch hernoemen",
    "Move to library folder": "Naar bibliotheekmap verplaatsen",
    "Move": "Verplaatsen",
    "To library folder": "Naar bibliotheekmap",
    "After forging, the files go to your Series or Movies folder. Off: they are only renamed, "
    "where they are.":
        "Na het forgen gaan de bestanden naar je map Series of Films. Uit: ze krijgen alleen een "
        "nieuwe naam, op de plek waar ze staan.",
    "Downloads MediaForge finds always move to your library folder.":
        "Downloads die MediaForge vindt, gaan altijd naar je bibliotheekmap.",
    "{n} file wasn't forged: choose its library folder in Settings › Auto-rename.":
        "{n} bestand is niet geforged: kies de bibliotheekmap in Instellingen › Automatisch "
        "hernoemen.",
    "{n} files weren't forged: choose their library folder in Settings › Auto-rename.":
        "{n} bestanden zijn niet geforged: kies de bibliotheekmap in Instellingen › Automatisch "
        "hernoemen.",
    "These files move to your library folder. Nothing changes until you click Rename.":
        "Deze bestanden gaan naar je bibliotheekmap. Er verandert niets tot je op Hernoemen klikt.",
    "Choose a library folder in Settings › Auto-rename first.":
        "Kies eerst een bibliotheekmap in Instellingen › Automatisch hernoemen.",
    "{n} left alone: choose a library folder first": "{n} met rust gelaten: kies eerst een bibliotheekmap",
    "several folders": "meerdere mappen",
    "TMDB isn't sure · {n} file": "TMDB weet het niet zeker · {n} bestand",
    "TMDB isn't sure · {n} files": "TMDB weet het niet zeker · {n} bestanden",
    "Waiting for you": "Wacht op jou",
    "Done": "Klaar",
    "Rename": "Hernoemen",
    # app/forge/ui/match_picker.py
    "Choose the right match": "Kies de juiste titel",
    "Which show is this?": "Welke serie is dit?",
    "Which movie is this?": "Welke film is dit?",
    "Your choice is remembered for this title.": "Je keuze wordt onthouden voor deze titel.",
    "Use top result": "Bovenste nemen",
    "Skip TMDB for this file": "TMDB overslaan voor dit bestand",
    "TMDB found nothing for this file.": "TMDB vond niets voor dit bestand.",
    "No description.": "Geen beschrijving.",
    "Use this": "Deze nemen",
    "Search TMDB by name…": "Zoek op TMDB op naam…",
    "🔍 Search": "🔍 Zoeken",
    "Searching…": "Zoeken…",
    "Nothing found for \"{query}\".": "Niets gevonden voor \"{query}\".",
    "TMDB couldn't be reached. Try again.": "TMDB is niet bereikbaar. Probeer het opnieuw.",
    # app/forge/core/library.py
    "Choose a download folder to watch.": "Kies een downloadmap om te bewaken.",
    "Choose the Series library folder.": "Kies de bibliotheekmap voor series.",
    "Choose the Movies library folder.": "Kies de bibliotheekmap voor films.",
    "A watched folder can't be inside a library folder, or the other way round.":
        "Een bewaakte map kan niet in een bibliotheekmap liggen, of andersom.",
    "A watched folder can't be inside another watched folder.":
        "Een bewaakte map kan niet in een andere bewaakte map liggen.",
    # app/forge/core/planner.py
    "already correctly named": "heeft al de juiste naam",
    "Clean junk in: {folder}": "Rommel opruimen in: {folder}",
    "Skip: {name} ({reason})": "Overslaan: {name} ({reason})",
    "Rename: {name} → {target}": "Hernoemen: {name} → {target}",
    "Poster: {target}": "Poster: {target}",
    "Trailer: {target}": "Trailer: {target}",
    "Remove empty folders: checks {n} folder": "Lege mappen weghalen: controleert {n} map",
    "Remove empty folders: checks {n} folders": "Lege mappen weghalen: controleert {n} mappen",
    # app/forge/clients/trailers.py
    "{step}: download failed (HTTP {status})": "{step}: downloaden mislukt (HTTP {status})",
    "{step}: checksum list unavailable (HTTP {status})":
        "{step}: lijst met controlesommen niet beschikbaar (HTTP {status})",
    "{step}: no published checksum for {name}": "{step}: geen gepubliceerde controlesom voor {name}",
    "{step}: the download doesn't match its checksum":
        "{step}: de download klopt niet met de controlesom",
    "Trailer tools installed": "Trailerhulpmiddelen geïnstalleerd",
    "Trailer for {title} couldn't be downloaded; saved a search link":
        "Trailer voor {title} kon niet worden gedownload; zoeklink opgeslagen",
    # app/forge/core/executor.py
    "Renaming {name}": "{name} hernoemen",
    "nothing downloaded": "niets gedownload",
    "Couldn't remove {name}": "Kon {name} niet verwijderen",
    "Removed junk: {name}": "Rommel verwijderd: {name}",
    "Removed the download's folder: {name}": "Map van de download verwijderd: {name}",
    "Stopped; {n} file not renamed": "Gestopt; {n} bestand niet hernoemd",
    "Stopped; {n} files not renamed": "Gestopt; {n} bestanden niet hernoemd",
    "Skipped {name} ({reason})": "{name} overgeslagen ({reason})",
    "Couldn't rename {name}: {reason}": "Kon {name} niet hernoemen: {reason}",
    "Renamed: {name} → {target}": "Hernoemd: {name} → {target}",
    "Couldn't get poster {name}": "Kon poster {name} niet ophalen",
    "Couldn't save poster {name}": "Kon poster {name} niet opslaan",
    "Saved poster: {name}": "Poster opgeslagen: {name}",
    "Trailer for {title} failed": "Trailer voor {title} mislukt",
    "Downloading trailer {n} of {total} · {title}": "Trailer {n} van {total} downloaden · {title}",
    "Stopped; {n} trailer not downloaded": "Gestopt; {n} trailer niet gedownload",
    "Stopped; {n} trailers not downloaded": "Gestopt; {n} trailers niet gedownload",
    "Saved trailer: {name}": "Trailer opgeslagen: {name}",
    "Removed empty folder: {name}": "Lege map verwijderd: {name}",
    "Couldn't move subtitle {name}": "Kon ondertitel {name} niet verplaatsen",
    "Moved subtitle: {name} → {target}": "Ondertitel verplaatst: {name} → {target}",
    # app/forge/core/fsops.py
    "Something already has this name": "Er heeft al iets deze naam",
    # app/forge/core/undo.py
    "file was moved or renamed again": "bestand is opnieuw verplaatst of hernoemd",
    "something else now has the old name": "iets anders heeft nu de oude naam",
    "file has changed since": "bestand is sindsdien veranderd",
    "already gone": "was al weg",
    "folder isn't empty": "map is niet leeg",
    "deleted junk can't be restored": "verwijderde rommel kan niet terug",
    "Put back: {name}": "Teruggezet: {name}",
    "Not undone: {name} ({reason})": "Niet ongedaan gemaakt: {name} ({reason})",
    # app/forge/core/recovery.py
    "interrupted": "onderbroken",
    # app/forge/core/matcher.py
    "Don't use TMDB": "TMDB niet gebruiken",
    "TMDB lookup for {name} failed": "Opzoeken bij TMDB mislukt voor {name}",
    "Season lookup for {title} failed": "Seizoen opzoeken mislukt voor {title}",
    # app/forge/core/auto.py
    "Already in the library.": "Staat al in de bibliotheek.",
    "Two downloads would get the same name.": "Twee downloads zouden dezelfde naam krijgen.",
    "It isn't in a watched folder any more.": "Het staat niet meer in een bewaakte map.",
    "TMDB isn't sure which one it is.": "TMDB weet niet zeker welke het is.",
    "Not found at TMDB.": "Niet gevonden bij TMDB.",
    "No TMDB key, or TMDB can't be reached.": "Geen TMDB-sleutel, of TMDB is niet bereikbaar.",
    "Skipped before.": "Eerder overgeslagen.",
    "The name and TMDB don't agree.": "De naam en TMDB komen niet overeen.",
    "No season or episode number in the name.": "Geen seizoen- of afleveringsnummer in de naam.",
    "TMDB doesn't know this episode.": "TMDB kent deze aflevering niet.",
    "No year in the name: is it a movie?": "Geen jaartal in de naam: is het een film?",
    "You undid this once; click Rename to move it again.":
        "Je hebt dit eerder ongedaan gemaakt; klik op Hernoemen om het opnieuw te verplaatsen.",
    "Look for new downloads now": "Nu naar nieuwe downloads zoeken",
    "Found {n} new file": "{n} nieuw bestand gevonden",
    "Found {n} new files": "{n} nieuwe bestanden gevonden",
    "No new files found": "Geen nieuwe bestanden gevonden",
    "{n} file needs you": "{n} bestand heeft je nodig",
    "{n} files need you": "{n} bestanden hebben je nodig",
    "Open MediaForge to pick or confirm them.": "Open MediaForge om ze te kiezen of te bevestigen.",
    "{n} movie": "{n} film",
    "{n} movies": "{n} films",
    "Today: {episodes} and {movies} renamed": "Vandaag: {episodes} en {movies} hernoemd",
    "Today: {what} renamed": "Vandaag: {what} hernoemd",
    "Not watching": "Bewaken staat uit",
    "Set up watching in Settings": "Stel bewaken in bij Instellingen",
    "Can't reach {folder}": "Kan {folder} niet bereiken",
    "Can't reach {folder}.": "Kan {folder} niet bereiken.",
    "Manual: watching {n} folder": "Handmatig: bewaakt {n} map",
    "Manual: watching {n} folders": "Handmatig: bewaakt {n} mappen",
    "Watching {n} folder": "Bewaakt {n} map",
    "Watching {n} folders": "Bewaakt {n} mappen",
    "Couldn't move it: {reason}": "Kon het niet verplaatsen: {reason}",
    "Looking at the download folders failed": "Kijken in de downloadmappen is mislukt",
    "Can reach {folder} again": "Kan {folder} weer bereiken",
    # app/forge/services.py
    "Checked {n} change left unfinished last time":
        "{n} wijziging gecontroleerd die de vorige keer onafgemaakt bleef",
    "Checked {n} changes left unfinished last time":
        "{n} wijzigingen gecontroleerd die de vorige keer onafgemaakt bleven",
    # app/forge/runtime.py
    "MediaForge isn't ready yet: a part it needs to read file names (guessit) isn't installed.":
        "MediaForge is nog niet klaar: een onderdeel dat het nodig heeft om bestandsnamen te "
        "lezen (guessit) is niet geïnstalleerd.",
    "MediaForge couldn't start. The log has the details.":
        "MediaForge kon niet starten. Het logboek heeft de details.",
    # app/forge/ui/history_view.py
    "Nothing here yet. Runs show up after you Forge some files.":
        "Nog niets te zien. Rondes verschijnen hier zodra je bestanden hebt hernoemd.",
    "right-click": "rechtsklik",
    "imported": "geïmporteerd",
    "automatic": "automatisch",
    "Yesterday": "Gisteren",
    "was {name}": "was {name}",
    "downloaded": "gedownload",
    "junk file, deleted": "rommelbestand, verwijderd",
    "new folder": "nieuwe map",
    "empty folder, removed": "lege map, verwijderd",
    "can't be restored": "kan niet terug",
    "didn't happen": "is niet gebeurd",
    "already undone": "al ongedaan gemaakt",
    "Show files ▾": "Bestanden tonen ▾",
    "Hide files ▴": "Bestanden verbergen ▴",
    "Undo run": "Ronde ongedaan maken",
    "{n} file": "{n} bestand",
    "{n} files": "{n} bestanden",
    "no preset": "geen voorinstelling",
    "{n} extra": "{n} extra",
    "{n} extras": "{n} extra's",
    "Undone {date}": "Ongedaan gemaakt {date}",
    "Undone": "Ongedaan gemaakt",
    "Partly undone": "Deels ongedaan gemaakt",
    "Undo {n} selected": "{n} geselecteerde ongedaan maken",
    "Undo": "Ongedaan maken",
    "your last {n} runs · newest first": "je laatste {n} rondes · nieuwste eerst",
    "No renamed shows yet": "Nog geen hernoemde series",
    "No renamed movies yet": "Nog geen hernoemde films",
    "Some items weren't undone": "Sommige onderdelen zijn niet ongedaan gemaakt",
    "{n} change reversed. These were left as they are:":
        "{n} wijziging teruggedraaid. Deze zijn ongemoeid gelaten:",
    "{n} changes reversed. These were left as they are:":
        "{n} wijzigingen teruggedraaid. Deze zijn ongemoeid gelaten:",
    "Undo this run?": "Deze ronde ongedaan maken?",
    "This puts back {files} and removes {created} from this run.":
        "Dit zet {files} terug en haalt {created} van deze ronde weg.",
    "{n} renamed file": "{n} hernoemd bestand",
    "{n} renamed files": "{n} hernoemde bestanden",
    "{n} downloaded poster, trailer or subtitle": "{n} gedownloade poster, trailer of ondertitel",
    "{n} downloaded posters, trailers or subtitles": "{n} gedownloade posters, trailers of ondertitels",
    "Undo the selected items?": "De geselecteerde onderdelen ongedaan maken?",
    "This puts back {n} item.": "Dit zet {n} onderdeel terug.",
    "This puts back {n} items.": "Dit zet {n} onderdelen terug.",
    # app/forge/ui/activity_panel.py
    "Activity": "Activiteit",
    # app/forge/ui/settings_pages.py: Auto-rename's first box
    "Off": "Uit",
    "Manual": "Handmatig",
    "On": "Aan",
    "Manual first: downloads wait in the queue until you forge them; switch to On when you're happy.":
        "Eerst handmatig: downloads wachten in de wachtrij tot je op Forge klikt; zet het op Aan als je tevreden bent.",
    "MediaForge can rename finished downloads by itself and move them into your Series and "
    "Movies folders.":
        "MediaForge kan klaar gedownloade bestanden zelf hernoemen en naar je mappen voor series "
        "en films verplaatsen.",
    "Off: nothing happens by itself; you rename files in the queue.":
        "Uit: er gebeurt niets vanzelf; je hernoemt bestanden in de wachtrij.",
    "Manual: downloads it finds wait in the queue; nothing moves until you forge them.":
        "Handmatig: gevonden downloads wachten in de wachtrij; er wordt niets verplaatst tot je op Forge klikt.",
    "On: sure matches are renamed and moved; the rest waits in the queue.":
        "Aan: wat TMDB zeker herkent, wordt hernoemd en verplaatst; de rest wacht in de wachtrij.",
    "Manual and On need these first:": "Voor Handmatig en Aan is eerst dit nodig:",
    "Download folders to watch": "Downloadmappen om te bewaken",
    "MediaForge looks in these folders and every folder inside them for finished downloads, when AirGrabber opens and every few minutes.":
        "MediaForge kijkt in deze mappen en elke map daarin naar voltooide downloads, als AirGrabber opent en om de paar minuten.",
    "Library folders": "Bibliotheekmappen",
    "Downloads MediaForge finds always move here. Files you add yourself only when their preset "
    "has Move to library folder on.":
        "Downloads die MediaForge vindt, gaan altijd hierheen. Bestanden die je zelf toevoegt "
        "alleen als hun voorinstelling Naar bibliotheekmap verplaatsen aan heeft.",
    "None yet": "Nog geen",
    "Not chosen yet": "Nog niet gekozen",
    # app/forge/ui/confirm_window.py
    "Change match…": "Andere kiezen…",
    "MediaForge · Confirm rename": "MediaForge · Hernoemen bevestigen",
    "Reading file names…": "Bestandsnamen lezen…",
    "Nothing changes until you click Rename.": "Er verandert niets tot je op Hernoemen klikt.",
    "Preset": "Voorinstelling",
    "Open in MediaForge": "Openen in MediaForge",
    "TMDB isn't sure, using {title}  ·  {old}": "TMDB weet het niet zeker, voorlopig {title}  ·  {old}",
    "Rename {n} file?": "{n} bestand hernoemen?",
    "Rename {n} files?": "{n} bestanden hernoemen?",
    "{chosen} of {total} selected": "{chosen} van {total} geselecteerd",
    "Rename {n} file": "{n} bestand hernoemen",
    "Rename {n} files": "{n} bestanden hernoemen",
    "{n} renamed": "{n} hernoemd",
    "{n} already right": "{n} al goed",
    "{n} failed": "{n} mislukt",
    "Done. You can undo this here or later in History.":
        "Klaar. Je kunt dit hier of later bij Geschiedenis ongedaan maken.",
    "Put back {n} change": "{n} wijziging teruggezet",
    "Put back {n} changes": "{n} wijzigingen teruggezet",
    "{n} left as it is: {reason}": "{n} ongemoeid gelaten: {reason}",
    "{n} left as they are, such as: {reason}": "{n} ongemoeid gelaten, bijvoorbeeld: {reason}",
    # app/forge/ui/settings_pages.py
    "MediaForge": "MediaForge",
    "Choose the preset for each folder under Naming › Library presets.":
        "Met welke voorinstelling elke soort een naam krijgt: zie Naamgeving.",
    "Getting MediaForge ready…": "MediaForge klaarmaken…",
    "MediaForge's settings couldn't be saved. The log has the details.":
        "De instellingen van MediaForge konden niet worden opgeslagen. Het logboek heeft de details.",
    "Add folder…": "Map toevoegen…",
    "Remove": "Weghalen",
    "Choose a download folder to watch": "Kies een downloadmap om te bewaken",
    "Choose the Series library folder": "Kies de bibliotheekmap voor series",
    "Choose the Movies library folder": "Kies de bibliotheekmap voor films",
    "Clean up downloads after moving (samples, .nfo, .txt, .url, empty folders)":
        "Downloads opruimen na het verplaatsen (samples, .nfo, .txt, .url, lege mappen)",
    "Tell me when files need me": "Laat het me weten als bestanden mij nodig hebben",
    "Remove files from the queue after they're forged (Undo stays in History)": (
        "Na het forgen uit de wachtrij halen (Ongedaan maken staat in Geschiedenis)"
    ),
    "A summary in the evening": "Een overzicht 's avonds",
    "AirGrabber's downloads": "Downloads van AirGrabber",
    "Watch the folders where AirGrabber's downloads end up. They follow AirGrabber's settings; a "
    "suggested folder is only watched once you add it.":
        "Bewaak de mappen waar de downloads van AirGrabber terechtkomen. Ze volgen de "
        "instellingen van AirGrabber; een voorgestelde map wordt pas bewaakt als je hem toevoegt.",
    "Use AirGrabber's downloads": "Downloads van AirGrabber gebruiken",
    "Add": "Toevoegen",
    # app/forge/ui/page.py, app/forge/ui/host.py
    "Auto-rename": "Automatisch hernoemen",  # Downloading's "Automatic" is "Automatisch"
    "Auto-rename off": "Automatisch uit",
    "MediaForge isn't ready yet: one of its parts couldn't be installed.":
        "MediaForge is nog niet klaar: een van de onderdelen kon niet worden geïnstalleerd.",
    "Getting ready…": "Klaarmaken…",
    # app/forge/ui/queue_view.py
    "Movie poster": "Filmposter",
    "Series poster": "Serieposter",
    "Season posters": "Seizoensposters",
    "Trailer": "Trailer",
    "Clean junk": "Rommel opruimen",
    "Remove empty folders": "Lege mappen weghalen",
    "Add video files or folders": "Voeg videobestanden of mappen toe",
    "with the buttons above. Nothing is renamed until you click Forge.":
        "met de knoppen hierboven. Er wordt niets hernoemd tot je op Forge klikt.",
    "Tidy up": "Opruimen",
    "Poster": "Poster",
    "Saves the film or show poster next to the files":
        "Bewaart de poster van de film of serie bij de bestanden",
    "One poster per season folder": "Eén poster per seizoensmap",
    "Downloads the trailer next to the files": "Downloadt de trailer bij de bestanden",
    "Removes sample files, .nfo and .txt left by the release":
        "Haalt samplebestanden, .nfo en .txt van de release weg",
    "Empty folders": "Lege mappen",
    "After moving, deletes folders that are left empty":
        "Haalt na het verplaatsen de mappen weg die leeg zijn gebleven",
    "Clear queue": "Wachtrij leegmaken",
    "Custom": "Aangepast",
    "Save as preset…": "Opslaan als voorinstelling…",
    "Type a naming rule for this item only; it isn't saved.":
        "Typ een naamregel voor alleen dit item; die wordt niet bewaard.",
    "Changed for this item": "Aangepast voor dit item",
    "Save to preset": "Opslaan in voorinstelling",
    "Saved to the preset {name}": "Opgeslagen in de voorinstelling {name}",
    "Save as preset": "Opslaan als voorinstelling",
    "A name for the new preset:": "Een naam voor de nieuwe voorinstelling:",
    "There is a preset called {name} already. Replace it?":
        "Er is al een voorinstelling {name}. Wil je die vervangen?",
    "Saved the preset {name}": "Voorinstelling {name} opgeslagen",
    "Choose a folder…": "Kies een map…",
    "Downloading the trailer tools…": "Trailerhulpmiddelen downloaden…",
    "Renaming {n} of {total} · {name}": "Hernoemen {n} van {total} · {name}",
    "Renaming {n} of {total}": "Hernoemen {n} van {total}",
    "Search history…": "Zoek in geschiedenis…",
    "Show {n} more": "Toon nog {n}",
    "Nothing in your history matches “{query}”.": "Niets in je geschiedenis past bij “{query}”.",
    "Forged: {moved} moved, {renamed} renamed, {skipped} already right, {failed} failed":
        "Geforged: {moved} verplaatst, {renamed} hernoemd, {skipped} al goed, {failed} mislukt",
    "{n} moved": "{n} verplaatst",
    "Forged: {renamed} renamed, {skipped} already right, {failed} failed":
        "Klaar: {renamed} hernoemd, {skipped} al goed, {failed} mislukt",
    "Quality in the file name": "Kwaliteit in de bestandsnaam",
    "NFO files": "NFO-bestanden",
    "Background picture (fanart)": "Achtergrondafbeelding (fanart)",
    "Adds the quality the current file name mentions, like 1080p H.265, to the new name":
        "Zet de kwaliteit uit de huidige bestandsnaam, zoals 1080p H.265, in de nieuwe naam",
    "Background": "Achtergrond",
    "Saves the film's or show's background picture (fanart) with the files":
        "Bewaart de achtergrondafbeelding (fanart) van de film of serie bij de bestanden",
    "NFO": "NFO",
    "Saves an NFO file with the title, year and summary, for Kodi and Jellyfin":
        "Bewaart een NFO-bestand met titel, jaar en samenvatting, voor Kodi en Jellyfin",
    "Background: {target}": "Achtergrond: {target}",
    "NFO: {target}": "NFO: {target}",
    "Couldn't get background {name}": "Achtergrond {name} kon niet worden opgehaald",
    "Couldn't save background {name}": "Achtergrond {name} kon niet worden opgeslagen",
    "Saved background: {name}": "Achtergrond opgeslagen: {name}",
    "Couldn't save NFO {name}": "NFO {name} kon niet worden opgeslagen",
    "Saved NFO: {name}": "NFO opgeslagen: {name}",
    "{n} background": "{n} achtergrond",
    "{n} backgrounds": "{n} achtergronden",
    "{n} NFO file": "{n} NFO-bestand",
    "{n} NFO files": "{n} NFO-bestanden",
    "{n} poster": "{n} poster",
    "{n} posters": "{n} posters",
    "{n} trailer": "{n} trailer",
    "{n} trailers": "{n} trailers",
    "cancelled": "geannuleerd",
    "+ Files": "+ Bestanden",
    "+ Folder": "+ Map",
    "Scan subfolders": "Submappen doorzoeken",
    "Forge": "Forge",
    "Forge {n} file": "Forge {n} bestand",
    "Forge {n} files": "Forge {n} bestanden",
    "Preview": "Voorbeeld",
    "Add files to see how they'll be named.": "Voeg bestanden toe om te zien hoe ze gaan heten.",
    "not a known code: {codes}": "geen bekende code: {codes}",
    "Choose a folder with videos": "Kies een map met video's",
    "Choose video files": "Kies videobestanden",
    "Video files": "Videobestanden",
    "Added {n} file to the queue": "{n} bestand aan de wachtrij toegevoegd",
    "Added {n} files to the queue": "{n} bestanden aan de wachtrij toegevoegd",
    "Some matches are uncertain": "TMDB twijfelt over sommige bestanden",
    "TMDB wasn't sure about {n} file. You can check it now or use TMDB's top result.":
        "TMDB wist {n} bestand niet zeker. Je kunt het nu nakijken of het bovenste resultaat "
        "van TMDB nemen.",
    "TMDB wasn't sure about {n} files. You can check them now or use TMDB's top result for "
    "each.":
        "TMDB wist {n} bestanden niet zeker. Je kunt ze nu nakijken of voor elk het bovenste "
        "resultaat van TMDB nemen.",
    "Review them": "Nakijken",
    "Use top results": "Bovenste resultaten nemen",
    "Download trailer tools?": "Trailerhulpmiddelen downloaden?",
    "Trailers need two free tools, yt-dlp and ffmpeg (about 110 MB, downloaded once from their "
    "official GitHub pages and checked before use).":
        "Voor trailers zijn twee gratis hulpmiddelen nodig, yt-dlp en ffmpeg (ongeveer 110 MB, "
        "één keer gedownload van hun officiële GitHub-pagina's en gecontroleerd voor gebruik).",
    "Skip trailers": "Trailers overslaan",
    "Couldn't install the trailer tools: {reason}":
        "Kon de trailerhulpmiddelen niet installeren: {reason}",
    "Stopping after the current file, then closing…":
        "Stoppen na het huidige bestand, daarna sluiten…",
    "Preview (nothing has been changed):": "Voorbeeld (er is niets veranderd):",
    "Preview is in History › Activity. Nothing has been changed.":
        "Het voorbeeld staat bij Geschiedenis › Activiteit. Er is niets veranderd.",
    "Nothing to undo for these files.": "Niets ongedaan te maken voor deze bestanden.",
    "Put back {n} file": "{n} bestand teruggezet",
    "Put back {n} files": "{n} bestanden teruggezet",
    "Nothing to undo.": "Niets ongedaan te maken.",
    "Undo the last run?": "De laatste ronde ongedaan maken?",
    "This puts back {files} and removes {created} from that run.":
        "Dit zet {files} terug en haalt {created} van die ronde weg.",
    "Undo finished: {n} change reversed": "Ongedaan maken klaar: {n} wijziging teruggedraaid",
    "Undo finished: {n} changes reversed": "Ongedaan maken klaar: {n} wijzigingen teruggedraaid",
    # app/forge/context_menu.py
    "Forge: {preset}": "Forge: {preset}",
    # app/forge/ui/settings_menu.py
    "Right-click menu": "Rechtsklikmenu",
    "Start MediaForge from Explorer: right-click files or folders.":
        "Start MediaForge vanuit Verkenner: klik met rechts op bestanden of mappen.",
    "In the right-click menu": "In het rechtsklikmenu",
    "Adds a MediaForge submenu to Explorer's right-click menu for files and folders (on "
    "Windows 11 it's under \"Show more options\"). No administrator rights needed.":
        "Voegt een MediaForge-submenu toe aan het rechtsklikmenu van Verkenner, voor bestanden "
        "en mappen (in Windows 11 onder \"Meer opties weergeven\"). Je hebt geen "
        "beheerdersrechten nodig.",
    "Show MediaForge in the right-click menu": "MediaForge in het rechtsklikmenu tonen",
    "Presets in the menu": "Voorinstellingen in het menu",
    "One \"Forge: …\" entry for each ticked preset. With every preset ticked, presets you "
    "add later appear too.":
        "Eén \"Forge: …\"-item voor elke aangevinkte voorinstelling. Staan ze allemaal "
        "aangevinkt, dan komen voorinstellingen die je later toevoegt er ook bij.",
    "Registry files": "Registerbestanden",
    "The menu as ticked above, as a .reg file to add by hand.":
        "Het menu zoals hierboven aangevinkt, als .reg-bestand om zelf toe te voegen.",
    "Save as .reg file…": "Opslaan als .reg-bestand…",
    "Save .reg file": ".reg-bestand opslaan",
    "Registry file": "Registerbestand",
    "An old MediaForge menu was found. Save the removal file and double-click it (Windows asks "
    "for administrator approval) to remove the old entries.":
        "Er is een oud MediaForge-menu gevonden. Sla het verwijderbestand op en dubbelklik erop "
        "(Windows vraagt om toestemming als beheerder) om de oude items weg te halen.",
    "Save removal file for the old menu…": "Verwijderbestand voor het oude menu opslaan…",
    "Save removal file": "Verwijderbestand opslaan",
    "The file couldn't be saved.": "Het bestand kon niet worden opgeslagen.",
    "Tick at least one preset for the right-click menu.":
        "Vink minstens één voorinstelling aan voor het rechtsklikmenu.",
    "The right-click menu couldn't be changed. The log has the details.":
        "Het rechtsklikmenu kon niet worden aangepast. Het logboek heeft de details.",
    "MediaForge's presets couldn't be read. The log has the details.":
        "De voorinstellingen van MediaForge konden niet worden gelezen. Het logboek heeft de "
        "details.",
    "✓ MediaForge is in the right-click menu.": "✓ MediaForge staat in het rechtsklikmenu.",
    "MediaForge isn't in the right-click menu.": "MediaForge staat niet in het rechtsklikmenu.",
    "The menu points to an old location of Python. Save puts it right.":
        "Het menu wijst naar een oude plek van Python. Opslaan zet het goed.",
    "The menu starts another copy of AirGrabber. Tick the box and Save to point it at "
    "this one.":
        "Het menu start een andere kopie van AirGrabber. Vink het vakje aan en sla op om het "
        "naar deze te laten wijzen.",
    "The menu starts the old MediaForge. Tick the box and Save to point it at AirGrabber.":
        "Het menu start de oude MediaForge. Vink het vakje aan en sla op om het naar "
        "AirGrabber te laten wijzen.",
    # app/forge/ui/settings_naming.py
    "Naming": "Naamgeving",
    "How MediaForge names shows and movies, and the matches it remembers.":
        "Hoe MediaForge series en films noemt, en de keuzes die het onthoudt.",
    "Presets": "Voorinstellingen",
    "A preset is a media type, a naming format and extras.":
        "Een voorinstelling is een soort media, een naamopmaak en extra's.",
    "New preset": "Nieuwe voorinstelling",
    "Delete preset": "Voorinstelling verwijderen",
    "Type": "Soort",
    "Format": "Opmaak",
    "Extras": "Extra's",
    "Movie": "Film",
    "TV": "Tv",
    "⚠ Not a known code: {codes}": "⚠ Geen bekende code: {codes}",
    "Codes: click one to add it": "Codes: klik erop om toe te voegen",
    "Examples: click one to use it": "Voorbeelden: klik erop om te gebruiken",
    "Code": "Code",
    "Gives": "Geeft",
    "Result": "Resultaat",
    "starts a new folder": "begint een nieuwe map",
    "a dash": "een streepje",
    "Give every preset a name.": "Geef elke voorinstelling een naam.",
    "Give this preset a name.": "Geef deze voorinstelling een naam.",
    "Two presets are called “{name}”. Give each its own name.":
        "Twee voorinstellingen heten “{name}”. Geef ze elk een eigen naam.",
    "Another preset is called that already.": "Een andere voorinstelling heet al zo.",
    "Library presets": "Voorinstellingen voor de bibliotheek",
    "The preset that names what is moved into your Series and Movies folders.":
        "De voorinstelling die de naam geeft aan wat naar je map Series en Films gaat.",
    "Names language": "Taal van de namen",
    "The language of titles and episode names from TMDB. AirGrabber's own language doesn't "
    "change it.":
        "De taal van titels en afleveringsnamen van TMDB. De taal van AirGrabber zelf verandert "
        "die niet.",
    "English (US)": "Engels (VS)",
    "English (UK)": "Engels (VK)",
    "Remembered matches": "Onthouden keuzes",
    "Your picks for uncertain TMDB matches. Forget one to be asked again.":
        "Je keuzes bij twijfelachtige TMDB-resultaten. Vergeet er een, dan wordt het je "
        "opnieuw gevraagd.",
    "Forget": "Vergeten",
    "Clear all": "Alles wissen",
    "No trailer: the trailer tools aren't downloaded yet.":
        "Geen trailer: de trailerhulpmiddelen zijn nog niet gedownload.",
    "Manage…": "Beheren…",
    "{n} match remembered": "{n} keuze onthouden",
    "{n} matches remembered": "{n} keuzes onthouden",
    "{n} to forget when you save": "{n} te vergeten bij Opslaan",
    "Nothing is forgotten until you click Save in Settings.":
        "Er wordt niets vergeten tot je in Instellingen op Opslaan klikt.",
    "{title} → {label} · show": "{title} → {label} · serie",
    "{title} → {label} · movie": "{title} → {label} · film",
    # app/forge/core/auto.py (F-26: the old MediaForge still installed)
    "Not watching until the old MediaForge is removed":
        "Bewaakt niets zolang de oude MediaForge nog geïnstalleerd is",
    # app/forge/migration.py
    "Your old MediaForge is still open. Close it (also from the tray), then try again.":
        "Je oude MediaForge is nog open. Sluit het (ook vanuit het systeemvak) en probeer het "
        "dan opnieuw.",
    "Your old MediaForge's settings and history weren't found.":
        "De instellingen en geschiedenis van je oude MediaForge zijn niet gevonden.",
    "Your old MediaForge's data comes from a newer version than this AirGrabber can read. "
    "Update AirGrabber, then try again.":
        "De gegevens van je oude MediaForge komen van een nieuwere versie dan deze AirGrabber "
        "kan lezen. Werk AirGrabber bij en probeer het dan opnieuw.",
    "Your old MediaForge's settings and history couldn't be read. The log has the details.":
        "De instellingen en geschiedenis van je oude MediaForge konden niet worden gelezen. "
        "Het logboek heeft de details.",
    "MediaForge's settings couldn't be written here. The log has the details.":
        "De instellingen van MediaForge konden hier niet worden opgeslagen. Het logboek heeft "
        "de details.",
    # app/forge/ui/import_step.py
    "Bring over your MediaForge settings, presets and history?":
        "Je MediaForge-instellingen, voorinstellingen en geschiedenis overzetten?",
    "MediaForge is part of AirGrabber now. Your presets, folders, past renames and remembered "
    "matches can come along. The old MediaForge itself isn't changed.":
        "MediaForge hoort nu bij AirGrabber. Je voorinstellingen, mappen, eerdere hernoemingen en "
        "onthouden keuzes kunnen mee. Aan de oude MediaForge zelf verandert niets.",
    "Bring over": "Overzetten",
    "Not now": "Nu niet",
    "Replace what's here?": "Vervangen wat hier staat?",
    "MediaForge in AirGrabber already has presets, folders or history of its own. Bringing "
    "your old MediaForge's over replaces them.":
        "MediaForge in AirGrabber heeft al eigen voorinstellingen, mappen of geschiedenis. Als je die "
        "van je oude MediaForge overzet, worden ze vervangen.",
    "Bringing your MediaForge settings over…": "Je MediaForge-instellingen overzetten…",
    "Nothing was brought over": "Er is niets overgezet",
    "Your MediaForge settings are here": "Je MediaForge-instellingen staan hier",
    "Brought over from your old MediaForge:": "Overgezet van je oude MediaForge:",
    "{n} preset": "{n} voorinstelling",
    "{n} presets": "{n} voorinstellingen",
    "{n} folder": "{n} map",
    "{n} folders": "{n} mappen",
    "{n} past rename": "{n} eerdere hernoeming",
    "{n} past renames": "{n} eerdere hernoemingen",
    "{n} remembered match": "{n} onthouden keuze",
    "{n} remembered matches": "{n} onthouden keuzes",
    "Your TMDB key": "Je TMDB-sleutel",
    "The right-click menu now opens MediaForge in AirGrabber":
        "Het rechtsklikmenu opent nu MediaForge in AirGrabber",
    "Automatic renaming starts once the old MediaForge is removed.":
        "Automatisch hernoemen begint zodra de oude MediaForge is verwijderd.",
    "The old MediaForge isn't needed any more. Its own uninstaller asks before it deletes "
    "anything.":
        "De oude MediaForge is niet meer nodig. Het eigen verwijderprogramma vraagt het "
        "eerst voordat het iets wist.",
    "Remove the old MediaForge": "Oude MediaForge verwijderen",
    "Your old MediaForge's settings, presets and history can still be brought over.":
        "De instellingen, voorinstellingen en geschiedenis van je oude MediaForge kunnen nog worden "
        "overgezet.",
    "Bring over…": "Overzetten…",
    # app/forge/old_install.py
    "Your MediaForge is now part of AirGrabber: open it to bring your settings over.":
        "Je MediaForge zit nu in AirGrabber: open het om je instellingen over te zetten.",
    "Renaming on its own waits while the old MediaForge is installed: both would move the same "
    "downloads.":
        "Vanzelf hernoemen wacht zolang de oude MediaForge is geïnstalleerd: ze zouden allebei "
        "dezelfde downloads verplaatsen.",
    "Bring your settings over first; then you can remove the old MediaForge.":
        "Zet eerst je instellingen over; daarna kun je de oude MediaForge verwijderen.",
    "Waiting for the old MediaForge's uninstaller…":
        "Wachten op het verwijderprogramma van de oude MediaForge…",
    "The old MediaForge was removed.": "De oude MediaForge is verwijderd.",
    "The old MediaForge is still installed.": "De oude MediaForge is nog geïnstalleerd.",
    "The old MediaForge's uninstaller wasn't recognised, so it wasn't started. You can "
    "remove MediaForge under Installed apps in Windows' Settings.":
        "Het verwijderprogramma van de oude MediaForge werd niet herkend en is dus niet "
        "gestart. Je kunt MediaForge verwijderen onder Geïnstalleerde apps in de "
        "Instellingen van Windows.",
    "The old MediaForge's uninstaller couldn't be started. You can remove MediaForge under "
    "Installed apps in Windows' Settings.":
        "Het verwijderprogramma van de oude MediaForge kon niet starten. Je kunt MediaForge "
        "verwijderen onder Geïnstalleerde apps in de Instellingen van Windows.",
    # app/forge/core/airgrabber_folders.py
    "“Where finished downloads end up” in Settings":
        "“Waar voltooide downloads terechtkomen” in Instellingen",
    "qBittorrent on this PC saves AirGrabber's downloads here":
        "qBittorrent op deze pc bewaart de downloads van AirGrabber hier",
    "{program} saves AirGrabber's downloads here": "{program} bewaart de downloads van AirGrabber hier",
    "{program} saves AirGrabber's films here": "{program} bewaart de films van AirGrabber hier",
    "AirGrabber doesn't know where its finished downloads end up on this PC. Fill in “Where "
    "finished downloads end up” in Settings › Where downloads go, or add the folder to the download "
    "folders to watch.":
        "AirGrabber weet niet waar zijn voltooide downloads op deze pc terechtkomen. Vul “Waar "
        "voltooide downloads terechtkomen” in bij Instellingen › Waar downloads naartoe gaan, of voeg de map toe "
        "aan de downloadmappen om te bewaken.",
    "AirGrabber sends downloads to {program} on another computer. Add the folder where they "
    "show up on this PC (a shared network folder) to the download folders to watch, or fill in "
    "“Where finished downloads end up” in Settings › Where downloads go.":
        "AirGrabber stuurt downloads naar {program} op een andere computer. Voeg de map waar ze "
        "op deze pc verschijnen (een gedeelde netwerkmap) toe aan de downloadmappen om te "
        "bewaken, of vul “Waar voltooide downloads terechtkomen” in bij Instellingen › "
        "Waar downloads naartoe gaan.",
    "Download Station keeps AirGrabber's downloads in its own download folder. Add the shared "
    "folder where they show up on this PC to the download folders to watch, or fill in “Where "
    "finished downloads end up” in Settings › Where downloads go.":
        "Download Station bewaart de downloads van AirGrabber in zijn eigen downloadmap. Voeg de "
        "gedeelde map waar ze op deze pc verschijnen toe aan de downloadmappen om te bewaken, of "
        "vul “Waar voltooide downloads terechtkomen” in bij Instellingen › Waar downloads naartoe gaan.",
    "Suggested: {reason}": "Voorstel: {reason}",
    "This PC can reach it.": "Deze pc kan erbij.",
    "This PC can't reach it right now.": "Deze pc kan er nu niet bij.",
    "Not checked yet.": "Nog niet nagekeken.",
    "Watched already: it is in a folder that is watched.":
        "Al bewaakt: hij staat in een map die al bewaakt wordt.",
    "Not watched: a folder you added is inside it.":
        "Niet bewaakt: er staat een map in die je zelf hebt toegevoegd.",
    "Not watched: it overlaps a library folder.":
        "Niet bewaakt: hij overlapt met een bibliotheekmap.",
    "AirGrabber's downloads now also end up in {folder}. Renaming on its own is on Manual again: "
    "switch it to On when you're happy.":
        "De downloads van AirGrabber komen nu ook terecht in {folder}. Vanzelf hernoemen staat "
        "weer op Handmatig: zet het op Aan als je tevreden bent.",
    # Ignoring files and folders (app/forge/core/ignored.py, queue_rows.py, queue_view.py,
    # settings_pages.py)
    "Ignore": "Negeren",
    "Ignore this file": "Dit bestand negeren",
    "Ignore these {n} files": "Deze {n} bestanden negeren",
    "Ignore this whole folder": "Deze hele map negeren",
    "Ignored: {name}. MediaForge won't pick it up again.":
        "Genegeerd: {name}. MediaForge pakt het niet meer op.",
    "Ignored": "Genegeerd",
    "Files and folders Auto-rename leaves alone.":
        "Bestanden en mappen die automatisch hernoemen met rust laat.",
    "Nothing ignored.": "Niets genegeerd.",
    "Stop ignoring": "Niet meer negeren",
    # A preset per band (app/forge/ui/queue_view.py)
    "Only {name}. Other cards keep their own preset.": "Alleen {name}. Andere kaarten houden hun eigen voorinstelling.",
    "Auto: every show and film gets a preset by itself, and you can change it on its card. Manual: shows the preset bar, with the naming code and the extras.":
        "Auto: elke serie en film krijgt vanzelf een voorinstelling, en die kun je op de kaart wijzigen. Handmatig: toont de voorinstellingenbalk, met de naamcode en de extra's.",
    "For: new files": "Voor: nieuwe bestanden",
    "Use for all ▾": "Gebruiken voor alles ▾",
    "All films ({n})": "Alle films ({n})",
    "All shows ({n})": "Alle series ({n})",
    "Everything in the queue ({n})": "Alles in de wachtrij ({n})",
    " · doesn't fit": " · past niet",
    "Used {preset} for {n} film": "Gebruikt {preset} voor {n} film",
    "Used {preset} for {n} films": "Gebruikt {preset} voor {n} films",
    "Used {preset} for {n} show": "Gebruikt {preset} voor {n} serie",
    "Used {preset} for {n} shows": "Gebruikt {preset} voor {n} series",
    "Used {preset} for {n} item": "Gebruikt {preset} voor {n} item",
    "Used {preset} for {n} items": "Gebruikt {preset} voor {n} items",
    "no subtitle found": "geen ondertitel gevonden",
    "OpenSubtitles took too long": "OpenSubtitles deed er te lang over",
    "Subtitles for {name}: {languages}": "Ondertitels voor {name}: {languages}",
    "No subtitles found for {name}: {languages}": "Geen ondertitels gevonden voor {name}: {languages}",
    "Subtitles for: {name}": "Ondertitels voor: {name}",
    "Couldn't save subtitles next to the video. Check that the folder is writable":
        "Kon de ondertitels niet naast de video opslaan. Controleer of je in de map mag schrijven",
    "subtitle": "ondertitel",
    "{n} subtitle": "{n} ondertitel",
    "{n} subtitles": "{n} ondertitels",
    "Gets a subtitle per chosen language right after the rename, named to match":
        "Haalt na het hernoemen per gekozen taal een ondertitel op, met de bijpassende naam",
    "Found on OpenSubtitles.com and saved next to your videos.":
        "Gevonden op OpenSubtitles.com en opgeslagen naast je video's.",
    "The subtitle settings couldn't be saved. The log has the details.":
        "De ondertitelinstellingen konden niet worden opgeslagen. Het logboek heeft de details.",
    "Looks for subtitles in each of these.": "Zoekt ondertitels in elk van deze talen.",
    "Subtitles that also describe sounds.": "Ondertitels die ook geluiden beschrijven.",
    "Optional: 20 a day instead of 5.": "Optioneel: 20 per dag in plaats van 5.",
    "Own API key": "Eigen API-sleutel",
    "Optional. Only needed if you have your own API key from opensubtitles.com":
        "Optioneel. Alleen nodig als je een eigen API-sleutel van opensubtitles.com hebt",
    "Found: {languages}": "Gevonden: {languages}",
    "The video is no longer there": "De video staat er niet meer",
    # Subtitles (app/forge/core/subtitles.py, app/forge/clients/opensubtitles.py,
    # app/forge/ui/subtitle_control.py, app/forge/ui/settings_subtitles.py)
    "Subtitles": "Ondertitels",
    "Languages": "Talen",
    "Subtitles aren't available in this version yet":
        "Ondertitels zijn nog niet beschikbaar in deze versie",
    "Subtitles provided by OpenSubtitles.com": "Ondertitels aangeleverd door OpenSubtitles.com",
    "OpenSubtitles account": "OpenSubtitles-account",
    "Create a free account": "Maak een gratis account aan",
    "+ Add": "+ Toevoegen",
    "Sign in": "Aanmelden",
    "Leave empty to use AirGrabber's key": "Laat leeg om de sleutel van AirGrabber te gebruiken",
    "Checking the key…": "Sleutel controleren…",
    "✓ OpenSubtitles accepted the key": "✓ OpenSubtitles heeft de sleutel geaccepteerd",
    "Empty: AirGrabber's own key is used": "Leeg: de eigen sleutel van AirGrabber wordt gebruikt",
    "Couldn't check the key right now; it is saved anyway":
        "Kon de sleutel nu niet controleren; hij wordt toch opgeslagen",
    "No subtitle downloads left today; more after {time}":
        "Vandaag geen ondertiteldownloads meer; na {time} weer",
    "No subtitle downloads left today": "Vandaag geen ondertiteldownloads meer",
    "{n} subtitle download left today": "Vandaag nog {n} ondertiteldownload",
    "{n} subtitle downloads left today": "Vandaag nog {n} ondertiteldownloads",
    "{n} downloads a day": "{n} downloads per dag",
    "{n} left today": "nog {n} vandaag",
    "✓ Signed in": "✓ Aangemeld",
    "Fill in both the OpenSubtitles user name and password, or neither":
        "Vul zowel de OpenSubtitles-gebruikersnaam als het wachtwoord in, of geen van beide",
    "Choose at least one subtitle language": "Kies minstens één ondertiteltaal",
    "Fill in your OpenSubtitles user name and password":
        "Vul je OpenSubtitles-gebruikersnaam en wachtwoord in",
    "Signing in…": "Aanmelden…",
    "Options": "Opties",
    "Get {language} subtitles": "Ondertitels ophalen: {language}",
    "Get all my languages": "Al mijn talen ophalen",
    "Looking for subtitles…": "Ondertitels zoeken…",
    "Already there": "Al aanwezig",
    "Not found yet: {languages}": "Nog niet gevonden: {languages}",
    "Today's limit reached": "Limiet van vandaag bereikt",
    "Couldn't get subtitles": "Kon geen ondertitels ophalen",
    "Hearing impaired": "Slechthorenden",
    "Avoid": "Vermijden",
    "Prefer": "Liefst wel",
    "Either": "Maakt niet uit",
    "Ready": "Klaar",
    "OpenSubtitles refused AirGrabber's key. An update of AirGrabber may fix this":
        "OpenSubtitles weigert de sleutel van AirGrabber. Een update van AirGrabber lost dat misschien op",
    "Wrong OpenSubtitles user name or password":
        "Verkeerde OpenSubtitles-gebruikersnaam of -wachtwoord",
    "Couldn't reach OpenSubtitles. Check your internet connection":
        "OpenSubtitles is niet bereikbaar. Controleer je internetverbinding",
    "Today's subtitle downloads are used up": "De ondertiteldownloads van vandaag zijn op",
    "OpenSubtitles refused your own API key. Check it, or leave it empty to use AirGrabber's key":
        "OpenSubtitles weigert je eigen API-sleutel. Kijk hem na, of laat het veld leeg om de sleutel van AirGrabber te gebruiken",
    "OpenSubtitles sent an unexpected download link":
        "OpenSubtitles stuurde een onverwachte downloadlink",
    "OpenSubtitles didn't hand over the subtitle file (HTTP {status})":
        "OpenSubtitles gaf het ondertitelbestand niet (HTTP {status})",
    "OpenSubtitles sent an empty or oversized subtitle file":
        "OpenSubtitles stuurde een leeg of te groot ondertitelbestand",
    "OpenSubtitles {path}: HTTP {status}": "OpenSubtitles {path}: HTTP {status}",
    "OpenSubtitles {path}: the answer wasn't readable":
        "OpenSubtitles {path}: het antwoord was niet leesbaar",
    "OpenSubtitles {path}: unexpected answer ({n} invalid field(s))":
        "OpenSubtitles {path}: onverwacht antwoord ({n} ongeldige veld(en))",
    "Today's subtitle downloads are used up; more tomorrow":
        "De ondertiteldownloads van vandaag zijn op; morgen weer",
    "Today's subtitle downloads are used up; more after {time}":
        "De ondertiteldownloads van vandaag zijn op; na {time} weer",
    "Open in Explorer": "Openen in Verkenner",
    "That file isn't there any more.": "Dat bestand is er niet meer.",
    "{name}: the audio is {language}": "{name}: het geluid is {language}",
    "🧰 Fix my library": "🧰 Mijn bibliotheek opknappen",
    "🔁 Find duplicates…": "🔁 Dubbele bestanden zoeken…",
    "Looking for duplicates in your library…": "Dubbele bestanden zoeken in je bibliotheek…",
    "Duplicates in your library": "Dubbele bestanden in je bibliotheek",
    "The same episode or film more than once. The biggest copy stays; the others move to a "
    "\"Duplicates\" folder beside the library folder. Nothing is deleted, and History can undo it.":
        "Dezelfde aflevering of film meer dan eens. Het grootste exemplaar blijft; de andere gaan naar "
        "een map \"Duplicates\" naast de bibliotheekmap. Er wordt niets verwijderd, en Geschiedenis "
        "kan het terugdraaien.",
    "No duplicates: every episode and film is in your library once.":
        "Geen dubbele bestanden: elke aflevering en film staat er één keer in.",
    "Choose your Series and Movies folders in Settings › Auto-rename first.":
        "Kies eerst je map voor series en films in Instellingen › Automatisch hernoemen.",
    "Keeps {name} ({size})": "Houdt {name} ({size})",
    "Moves {name} ({size})": "Verplaatst {name} ({size})",
    "Move {n} extra copy aside": "{n} extra exemplaar wegzetten",
    "Move {n} extra copies aside": "{n} extra exemplaren wegzetten",
    "✓ Moved {n} copy to Duplicates": "✓ {n} exemplaar naar Duplicates verplaatst",
    "✓ Moved {n} copies to Duplicates": "✓ {n} exemplaren naar Duplicates verplaatst",
    "Moving…": "Verplaatsen…",
    "{name}: plays {language} first; {wanted} is there too":
        "{name}: speelt eerst {language} af; {wanted} zit er ook in",
    "{name}: says {promised}p, but the picture is {height}p":
        "{name}: zegt {promised}p, maar het beeld is {height}p",
}
