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
    "Would rename": "Zou hernoemen",
    "Renamed": "Hernoemd",
    "Done today": "Vandaag klaar",
    "Now:": "Nu:",
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
    "the same folder": "dezelfde map",
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
    "Trial: watching {n} folder": "Proef: bewaakt {n} map",
    "Trial: watching {n} folders": "Proef: bewaakt {n} mappen",
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
    "{n} downloaded": "{n} gedownload",
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
    "{n} downloaded poster or trailer": "{n} gedownloade poster of trailer",
    "{n} downloaded posters or trailers": "{n} gedownloade posters of trailers",
    "Undo the selected items?": "De geselecteerde onderdelen ongedaan maken?",
    "This puts back {n} item.": "Dit zet {n} onderdeel terug.",
    "This puts back {n} items.": "Dit zet {n} onderdelen terug.",
    # app/forge/ui/activity_panel.py
    "Activity": "Activiteit",
    # app/forge/ui/library_page.py
    "Off": "Uit",
    "Trial": "Proef",
    "On": "Aan",
    "Trial first: MediaForge shows what it would do; switch to On when you're happy.":
        "Eerst een proef: MediaForge laat zien wat het zou doen; zet het op Aan als je tevreden "
        "bent.",
    "MediaForge can rename finished downloads by itself and move them into your Series and "
    "Movies folders.":
        "MediaForge kan klaar gedownloade bestanden zelf hernoemen en naar je mappen voor series "
        "en films verplaatsen.",
    "Off: nothing happens by itself; you rename files in the queue.":
        "Uit: er gebeurt niets vanzelf; je hernoemt bestanden in de wachtrij.",
    "Trial: the queue shows what it would do; nothing is moved.":
        "Proef: de wachtrij laat zien wat er zou gebeuren; er wordt niets verplaatst.",
    "On: sure matches are renamed and moved; the rest waits in the queue.":
        "Aan: wat TMDB zeker herkent, wordt hernoemd en verplaatst; de rest wacht in de wachtrij.",
    "Trial and On need these first:": "Voor Proef en Aan is eerst dit nodig:",
    "Watch these folders": "Deze mappen bewaken",
    "Put renamed files here": "Hernoemde bestanden hierheen",
    "How files are named": "Zo krijgen bestanden hun naam",
    "The preset each kind is named with when it moves.":
        "De voorinstelling waarmee elke soort een naam krijgt als hij verplaatst wordt.",
    "Your folders": "Je eigen mappen",
    "That couldn't be saved. The log has the details.":
        "Dat kon niet worden opgeslagen. Het logboek heeft de details.",
    "Download folders to watch": "Downloadmappen om te bewaken",
    "MediaForge looks here every few minutes for downloads that have finished.":
        "MediaForge kijkt hier om de paar minuten naar voltooide downloads.",
    "Library folders": "Bibliotheekmappen",
    "Shows and movies are moved here, each named with its preset.":
        "Series en films worden hierheen verplaatst, elk met de naam uit zijn voorinstelling.",
    "Rename on its own": "Vanzelf hernoemen",
    "Sure matches are renamed and moved by themselves; anything else waits in the queue. Trial "
    "shows what would happen without moving anything.":
        "Wat TMDB zeker herkent, wordt vanzelf hernoemd en verplaatst; de rest wacht in de "
        "wachtrij. Proef laat zien wat er zou gebeuren zonder iets te verplaatsen.",
    "More in Settings…": "Meer bij Instellingen…",
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
    "Library & watching": "Bibliotheek & bewaken",
    "Where MediaForge finds finished downloads, and where it moves them.":
        "Waar MediaForge voltooide downloads vindt, en waar het ze naartoe verplaatst.",
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
    "A summary in the evening": "Een overzicht 's avonds",
    "AirGrabber's downloads": "Downloads van AirGrabber",
    "Watch the folders where AirGrabber's downloads end up. They follow AirGrabber's settings; a "
    "suggested folder is only watched once you add it.":
        "Bewaak de mappen waar de downloads van AirGrabber terechtkomen. Ze volgen de "
        "instellingen van AirGrabber; een voorgestelde map wordt pas bewaakt als je hem toevoegt.",
    "Use AirGrabber's downloads": "Downloads van AirGrabber gebruiken",
    "Add": "Toevoegen",
    # app/forge/ui/window.py, app/forge/ui/host.py
    "Queue": "Wachtrij",
    "Auto-rename": "Automatisch",
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
    "Forged: {renamed} renamed, {skipped} already right, {failed} failed":
        "Klaar: {renamed} hernoemd, {skipped} al goed, {failed} mislukt",
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
    "Click to add to the format:": "Klik om toe te voegen aan de opmaak:",
    "Name (year)": "Naam (jaar)",
    "Title": "Titel",
    "Episode name": "Afleveringsnaam",
    "Season + episode": "Seizoen + afl.",
    "Season": "Seizoen",
    "Episode": "Aflevering",
    "New folder": "Nieuwe map",
    "Dash": "Streepje",
    "Adds {code} to the format": "Voegt {code} toe aan de opmaak",
    "Whatever comes before it becomes a folder": "Wat ervoor staat, wordt een map",
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
    "“Where finished downloads end up” in AirGrabber's Settings":
        "“Waar voltooide downloads terechtkomen” in de instellingen van AirGrabber",
    "qBittorrent on this PC saves AirGrabber's downloads here":
        "qBittorrent op deze pc bewaart de downloads van AirGrabber hier",
    "{program} saves AirGrabber's downloads here": "{program} bewaart de downloads van AirGrabber hier",
    "{program} saves AirGrabber's films here": "{program} bewaart de films van AirGrabber hier",
    "AirGrabber doesn't know where its finished downloads end up on this PC. Fill in “Where "
    "finished downloads end up” in AirGrabber's Settings › Subtitles, or add the folder to the "
    "download folders to watch.":
        "AirGrabber weet niet waar zijn voltooide downloads op deze pc terechtkomen. Vul “Waar "
        "voltooide downloads terechtkomen” in bij de instellingen van AirGrabber › Ondertitels, "
        "of voeg de map toe aan de downloadmappen om te bewaken.",
    "AirGrabber sends downloads to {program} on another computer. Add the folder where they "
    "show up on this PC (a shared network folder) to the download folders to watch, or fill in "
    "“Where finished downloads end up” in AirGrabber's Settings › Subtitles.":
        "AirGrabber stuurt downloads naar {program} op een andere computer. Voeg de map waar ze "
        "op deze pc verschijnen (een gedeelde netwerkmap) toe aan de downloadmappen om te "
        "bewaken, of vul “Waar voltooide downloads terechtkomen” in bij de instellingen van "
        "AirGrabber › Ondertitels.",
    "Download Station keeps AirGrabber's downloads in its own download folder. Add the shared "
    "folder where they show up on this PC to the download folders to watch, or fill in “Where "
    "finished downloads end up” in AirGrabber's Settings › Subtitles.":
        "Download Station bewaart de downloads van AirGrabber in zijn eigen downloadmap. Voeg de "
        "gedeelde map waar ze op deze pc verschijnen toe aan de downloadmappen om te bewaken, of "
        "vul “Waar voltooide downloads terechtkomen” in bij de instellingen van AirGrabber › "
        "Ondertitels.",
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
    "AirGrabber's downloads now also end up in {folder}. Renaming on its own is on Trial again: "
    "switch it to On when you're happy.":
        "De downloads van AirGrabber komen nu ook terecht in {folder}. Vanzelf hernoemen staat "
        "weer op Proef: zet het op Aan als je tevreden bent.",
    # app/forge/ui/settings_window.py (MediaForge's own Settings window)
    "MediaForge Settings": "MediaForge-instellingen",
    "Language, theme, TMDB key and updates are shared with AirGrabber.":
        "Taal, thema, TMDB-sleutel en updates deel je met AirGrabber.",
    "Open AirGrabber's Settings": "AirGrabber-instellingen openen",
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
    "For: {name}": "Voor: {name}",
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
}
