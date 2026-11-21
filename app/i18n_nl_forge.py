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
    "Done today": "Vandaag klaar",
    # A queue band's status, question and answers (the owner's choice "B", 2026-11-17)
    "Already in your library": "Al in je bibliotheek",
    "Not sure which film": "Niet zeker welke film",
    "Not sure which show": "Niet zeker welke serie",
    "Couldn't forge": "Forgen lukte niet",
    "Ready to forge": "Klaar om te forgen",
    "Forged": "Geforged",
    "Forged {time}": "Geforged {time}",
    "Name style": "Naamstijl",
    "You already have this film.": "Je hebt deze film al.",
    "You already have this episode.": "Je hebt deze aflevering al.",
    "You already have {n} of these episodes.": "Je hebt {n} van deze afleveringen al.",
    "Is this {title}?": "Is dit {title}?",
    "The file name and the film database don't fully agree.":
        "De bestandsnaam en de filmdatabase zijn het niet helemaal eens.",
    "The file name and the show database don't fully agree.":
        "De bestandsnaam en de seriedatabase zijn het niet helemaal eens.",
    "Which film is this?": "Welke film is dit?",
    "📂 Show file": "📂 Bestand tonen",
    "Forge this": "Dit forgen",
    "Wrong film?": "Verkeerde film?",
    "Wrong show?": "Verkeerde serie?",
    "No, pick the right film…": "Nee, kies de juiste film…",
    "No, pick the right show…": "Nee, kies de juiste serie…",
    "Pick the film…": "Kies de film…",
    "Pick the show…": "Kies de serie…",
    "Yes, forge it": "Ja, forgen",
    "Leave it alone ▾": "Met rust laten ▾",
    "Keep the one I have ▾": "Houd die ik heb ▾",
    "Takes it off the list. Sorting won't touch it again.":
        "Haalt het van de lijst. Sorteren komt er niet meer aan.",
    "Use this one instead": "Gebruik deze in de plaats",
    "MediaForge's guess changed meanwhile: have another look.":
        "Wat MediaForge dacht, is intussen veranderd: kijk nog eens.",
    "The one in your library moves to the Duplicates folder. Nothing is deleted; Undo puts "
    "both back.":
        "Die in je bibliotheek gaat naar de map Duplicates. Er wordt niets verwijderd; Ongedaan "
        "maken zet ze allebei terug.",
    # app/forge/core/replace_facts.py
    "In your library: {library} — this download: {download}":
        "In je bibliotheek: {library} — deze download: {download}",
    "In your library: {library}": "In je bibliotheek: {library}",
    "This download: {download}": "Deze download: {download}",
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
    "📁 Sorted in {folder}": "📁 Wordt gesorteerd in {folder}",
    "📁 Was sorted in {folder}": "📁 Gesorteerd in {folder}",
    "Rule: {name}": "Regel: {name}",
    "Waits for you: rule “{name}” is Manual": "Wacht op jou: regel “{name}” is Handmatig",
    "Its rule was changed: check it": "Zijn regel is gewijzigd: kijk het na",
    "⚠ The preset of rule “{name}” is gone: choose another":
        "⚠ De voorinstelling van regel “{name}” is weg: kies een andere",
    "Move to library folder": "Naar bibliotheekmap verplaatsen",
    "Move": "Verplaatsen",
    "To library folder": "Naar bibliotheekmap",
    "After forging, the files go to your Series or Movies folder. Off: they are only renamed, "
    "where they are.":
        "Na het forgen gaan de bestanden naar je map Series of Films. Uit: ze krijgen alleen een "
        "nieuwe naam, op de plek waar ze staan.",
    "Downloads MediaForge finds always move to your library folder.":
        "Downloads die MediaForge vindt, gaan altijd naar je bibliotheekmap.",
    "These files move to your library folder. Nothing changes until you click Rename.":
        "Deze bestanden gaan naar je bibliotheekmap. Er verandert niets tot je op Hernoemen klikt.",
    "{n} left alone: choose a library folder first": "{n} met rust gelaten: kies eerst een bibliotheekmap",
    "several folders": "meerdere mappen",
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
    # app/forge/core/rules.py (a queued file's way)
    "Choose the Series library folder.": "Kies de bibliotheekmap voor series.",
    "Choose the Movies library folder.": "Kies de bibliotheekmap voor films.",
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
    "Copying {done} of {total} GB": "{done} van {total} GB gekopieerd",
    "Copying {done} of {total} MB": "{done} van {total} MB gekopieerd",
    "The copy was stopped": "Het kopiëren is gestopt",
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
    # "Use this one instead" (auto.py, executor.py, queue_rows.py, queue_view.py)
    "Replace {name} in the library? The one there now moves to {folder}.":
        "{name} in de bibliotheek vervangen? Die er nu staat, gaat naar {folder}.",
    "Replace these {n} files in the library? The ones there now move to {folder}.":
        "Deze {n} bestanden in de bibliotheek vervangen? Die er nu staan, gaan naar {folder}.",
    "Where should the copies you replace go?": "Waar moeten de vervangen exemplaren heen?",
    "Moved the copy in the library aside: {name}": "Exemplaar uit de bibliotheek opzijgezet: {name}",
    "{n} old copy moved aside": "{n} oud exemplaar opzijgezet",
    "{n} old copies moved aside": "{n} oude exemplaren opzijgezet",
    "Couldn't move the copy in the library aside: {name} ({reason})":
        "Kon het exemplaar in de bibliotheek niet opzijzetten: {name} ({reason})",
    "Put the copy back in the library: {name}": "Exemplaar teruggezet in de bibliotheek: {name}",
    "Couldn't put the copy back in the library: {name}. History can undo it.":
        "Kon het exemplaar niet terugzetten in de bibliotheek: {name}. "
        "Geschiedenis kan het ongedaan maken.",
    "Nothing in the library to replace for {n} file: renamed as usual.":
        "Niets in de bibliotheek te vervangen voor {n} bestand: gewoon hernoemd.",
    "Nothing in the library to replace for {n} files: renamed as usual.":
        "Niets in de bibliotheek te vervangen voor {n} bestanden: gewoon hernoemd.",
    "Two downloads would get the same name.": "Twee downloads zouden dezelfde naam krijgen.",
    "It isn't in a watched folder any more.": "Het staat niet meer in een bewaakte map.",
    "No rule takes this file.": "Geen regel neemt dit bestand.",
    "TMDB isn't sure which one it is.": "TMDB weet niet zeker welke het is.",
    "Not found at TMDB.": "Niet gevonden bij TMDB.",
    "No TMDB key, or TMDB can't be reached.": "Geen TMDB-sleutel, of TMDB is niet bereikbaar.",
    "Skipped before.": "Eerder overgeslagen.",
    "The name and TMDB don't agree.": "De naam en TMDB komen niet overeen.",
    "No season or episode number in the name.": "Geen seizoen- of afleveringsnummer in de naam.",
    "TMDB doesn't know this episode.": "TMDB kent deze aflevering niet.",
    "No year in the name: is it a movie?": "Geen jaartal in de naam: is het een film?",
    "You undid this once; click Forge this to move it again.":
        "Je hebt dit eerder ongedaan gemaakt; klik op Dit forgen om het opnieuw te verplaatsen.",
    "Look for new downloads now": "Nu naar nieuwe downloads zoeken",
    "Found {n} new file": "{n} nieuw bestand gevonden",
    "Found {n} new files": "{n} nieuwe bestanden gevonden",
    "No new files found": "Geen nieuwe bestanden gevonden",
    "{n} download finishing, ready in about {m} min":
        "{n} download bijna binnen, klaar over ongeveer {m} min",
    "{n} downloads finishing, ready in about {m} min":
        "{n} downloads bijna binnen, klaar over ongeveer {m} min",
    "{n} download finishing, ready in under a minute":
        "{n} download bijna binnen, klaar binnen een minuut",
    "{n} downloads finishing, ready in under a minute":
        "{n} downloads bijna binnen, klaar binnen een minuut",
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
    "Waiting for Sorting to finish…": "Wachten tot Sorteren klaar is…",
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
    # Off (the player's subtitles) and Manual (the rules)
    "Off": "Uit",
    "Manual": "Handmatig",
    # app/forge/ui/settings_pages.py: Sorting's first box and its rules
    "Rules": "Regels",
    "Checked from the top down. The first rule that fits a file takes it.":
        "Van boven naar beneden nagelopen. De eerste regel waar een bestand onder valt, neemt het.",
    "＋ Add a rule": "＋ Regel toevoegen",
    "Follows AirGrabber's settings. You can turn it off but not delete it.":
        "Volgt de instellingen van AirGrabber. Je kunt hem uitzetten maar niet verwijderen.",
    "Edit": "Bewerken",
    "Couldn't read your presets. Try again in a moment.":
        "Je voorinstellingen konden niet worden gelezen. Probeer het zo nog eens.",
    "Show them": "Laat ze zien",
    "Files no rule fits": "Bestanden waar geen regel bij past",
    "Add a rule for this folder": "Regel voor deze map toevoegen",
    "None yet": "Nog geen",
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
    "Getting MediaForge ready…": "MediaForge klaarmaken…",
    "MediaForge isn't available.": "MediaForge is niet beschikbaar.",
    "MediaForge's settings couldn't be saved. The log has the details.":
        "De instellingen van MediaForge konden niet worden opgeslagen. Het logboek heeft de details.",
    "Remove": "Weghalen",
    "Clean up downloads after moving (samples, .nfo, .txt, .url, empty folders)":
        "Downloads opruimen na het verplaatsen (samples, .nfo, .txt, .url, lege mappen)",
    "Tell me when files need me": "Laat het me weten als bestanden mij nodig hebben",
    "A summary in the evening": "Een overzicht 's avonds",
    "AirGrabber's downloads": "Downloads van AirGrabber",
    "The folders where AirGrabber's downloads end up, as AirGrabber's settings say. AirGrabber's "
    "rule watches them; a suggested folder is only watched once you add a rule for it.":
        "De mappen waar de downloads van AirGrabber terechtkomen, zoals de instellingen van "
        "AirGrabber zeggen. De regel van AirGrabber bewaakt ze; een voorgestelde map wordt pas "
        "bewaakt als je er een regel voor toevoegt.",
    "Add": "Toevoegen",
    # app/forge/ui/page.py, app/forge/ui/host.py
    "Sorting off": "Sorteren uit",
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
    "Clear": "Leegmaken",
    "Clear what's done": "Weghalen wat klaar is",
    "Clear everything": "Alles weghalen",
    "Remove files from the queue after they're forged": "Na het forgen uit de wachtrij halen",
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
    "The menu points to an old location of Python. Untick the box and tick it again to put "
    "it right.":
        "Het menu wijst naar een oude plek van Python. Vink het vakje uit en weer aan om het "
        "goed te zetten.",
    "The menu starts another copy of AirGrabber. Tick the box to point it at this one.":
        "Het menu start een andere kopie van AirGrabber. Vink het vakje aan om het naar deze "
        "te laten wijzen.",
    "The menu starts the old MediaForge. Tick the box to point it at AirGrabber.":
        "Het menu start de oude MediaForge. Vink het vakje aan om het naar AirGrabber te "
        "laten wijzen.",
    # app/forge/ui/settings_naming.py
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
    "“Finished downloads” in Settings › Folders":
        "“Voltooide downloads” in Instellingen › Mappen",
    "qBittorrent on this PC saves AirGrabber's downloads here":
        "qBittorrent op deze pc bewaart de downloads van AirGrabber hier",
    "{program} saves AirGrabber's downloads here": "{program} bewaart de downloads van AirGrabber hier",
    "{program} saves AirGrabber's films here": "{program} bewaart de films van AirGrabber hier",
    "Suggested: {reason}": "Voorstel: {reason}",
    "This PC can reach it.": "Deze pc kan erbij.",
    "This PC can't reach it right now.": "Deze pc kan er nu niet bij.",
    "Not checked yet.": "Nog niet nagekeken.",
    "Not watched: it's inside a library folder or a folder a rule moves files to.":
        "Niet bewaakt: hij ligt in een bibliotheekmap of in een map waar een regel bestanden "
        "naartoe verplaatst.",
    "AirGrabber's downloads now also end up in {folder}. What's there now waits in the queue "
    "once; after that, AirGrabber's rule works as you set it.":
        "De downloads van AirGrabber komen nu ook terecht in {folder}. Wat daar nu staat, wacht "
        "één keer in de wachtrij; daarna werkt de regel van AirGrabber zoals je hem hebt ingesteld.",
    # Ignoring files and folders (app/forge/core/ignored.py, queue_rows.py, queue_view.py,
    # settings_pages.py)
    "Leave this file alone": "Dit bestand met rust laten",
    "Leave these {n} files alone": "Deze {n} bestanden met rust laten",
    "Leave this whole folder alone": "Deze hele map met rust laten",
    "Left alone: {name}. MediaForge won't pick it up again.":
        "Met rust gelaten: {name}. MediaForge pakt het niet meer op.",
    "Ignored": "Genegeerd",
    "Nothing ignored.": "Niets genegeerd.",
    "Stop ignoring": "Niet meer negeren",
    # A preset per band (app/forge/ui/queue_view.py)
    "Only {name}. Other cards keep their own preset.": "Alleen {name}. Andere kaarten houden hun eigen voorinstelling.",
    "For all {n} shows. A card's chip changes just that one.": "Voor alle {n} series. De chip van een kaart verandert alleen die ene.",
    "For all {n} films. A card's chip changes just that one.": "Voor alle {n} films. De chip van een kaart verandert alleen die ene.",
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
    "The same episode or film more than once. The biggest copy stays; the others move to a "
    "folder you choose. Nothing is deleted, and History can undo it.":
        "Dezelfde aflevering of film meer dan eens. Het grootste exemplaar blijft; de andere gaan naar "
        "een map die je zelf kiest. Er wordt niets verwijderd, en Geschiedenis kan het terugdraaien.",
    "Where should the extra copies go?": "Waar moeten de extra exemplaren heen?",
    "Choose a folder outside your Series and Movies folders.":
        "Kies een map buiten je mappen Series en Films.",
    "No duplicates: every episode and film is in your library once.":
        "Geen dubbele bestanden: elke aflevering en film staat er één keer in.",
    "Keeps {name} ({size})": "Houdt {name} ({size})",
    "Moves {name} ({size})": "Verplaatst {name} ({size})",
    "Move {n} extra copy aside": "{n} extra exemplaar wegzetten",
    "Move {n} extra copies aside": "{n} extra exemplaren wegzetten",
    "✓ Moved {n} copy to Duplicates": "✓ {n} exemplaar naar Duplicates verplaatst",
    "✓ Moved {n} copies to Duplicates": "✓ {n} exemplaren naar Duplicates verplaatst",
    "✓ Moved {n} copy to the folder you chose": "✓ {n} exemplaar naar de gekozen map verplaatst",
    "✓ Moved {n} copies to the folder you chose":
        "✓ {n} exemplaren naar de gekozen map verplaatst",
    "Moving…": "Verplaatsen…",
    "{name}: plays {language} first; {wanted} is there too":
        "{name}: speelt eerst {language} af; {wanted} zit er ook in",
    "{name}: says {promised}p, but the picture is {height}p":
        "{name}: zegt {promised}p, maar het beeld is {height}p",
    # app/forge/core/rules.py
    "✕ Can't save: it moves files to the folder it watches, so they would be found again and "
    "again.":
        "✕ Kan niet opslaan: hij verplaatst bestanden naar de map die hij bewaakt, dus ze worden "
        "steeds opnieuw gevonden.",
    "✕ Can't save: the folder it moves to is inside the folder it watches, so files would be "
    "found again and again.":
        "✕ Kan niet opslaan: de map waar hij naartoe verplaatst ligt in de map die hij bewaakt, "
        "dus bestanden worden steeds opnieuw gevonden.",
    "✕ Can't save: the folder it watches is a library folder, so files would be found again "
    "and again.":
        "✕ Kan niet opslaan: de map die hij bewaakt is een bibliotheekmap, dus bestanden worden "
        "steeds opnieuw gevonden.",
    "✕ Can't save: choose the folder this rule watches.":
        "✕ Kan niet opslaan: kies de map die deze regel bewaakt.",
    "✕ Can't save: choose the folder to move them to.":
        "✕ Kan niet opslaan: kies de map waar ze heen gaan.",
    "⚠ Can't reach {folder} right now.": "⚠ Kan {folder} nu niet bereiken.",
    "⚠ Can't reach {folder} right now. Films wait in the queue until it's back.":
        "⚠ Kan {folder} nu niet bereiken. Films wachten in de wachtrij tot hij er weer is.",
    "⚠ Can't reach {folder} right now. Shows wait in the queue until it's back.":
        "⚠ Kan {folder} nu niet bereiken. Series wachten in de wachtrij tot hij er weer is.",
    "⚠ Can't reach {folder} right now. Files wait in the queue until it's back.":
        "⚠ Kan {folder} nu niet bereiken. Bestanden wachten in de wachtrij tot hij er weer is.",
    "⚠ This rule never gets a file: rule {n} above takes the same files first. Drag it higher, "
    "or change one of them.":
        "⚠ Deze regel krijgt nooit een bestand: regel {n} hierboven neemt dezelfde bestanden "
        "eerst. Sleep hem hoger, of verander een van beide.",
    "⚠ Its preset is gone: choose another.": "⚠ De voorinstelling is weg: kies een andere.",
    "⚠ Choose your Series folder under Folders.":
        "⚠ Kies je map Series onder Mappen.",
    "⚠ Choose your Movies folder under Folders.":
        "⚠ Kies je map Films onder Mappen.",
    "Rule {n} moves files into this folder. MediaForge won't pick them up again here.":
        "Regel {n} verplaatst bestanden naar deze map. MediaForge pakt ze hier niet opnieuw op.",
    "Rule {n} can't be saved yet. Its row says why.":
        "Regel {n} kan nog niet worden opgeslagen. Zijn rij zegt waarom.",
    "Rule {n}: {why}": "Regel {n}: {why}",
    # app/forge/ui/rule_rows.py
    "Everything from {folder}": "Alles uit {folder}",
    "Shows from {folder}": "Series uit {folder}",
    "Films from {folder}": "Films uit {folder}",
    "Everything in 4K from {folder}": "Alles in 4K uit {folder}",
    "4K shows from {folder}": "4K-series uit {folder}",
    "4K films from {folder}": "4K-films uit {folder}",
    "Everything not in 4K from {folder}": "Alles behalve 4K uit {folder}",
    "Shows not in 4K from {folder}": "Series niet in 4K uit {folder}",
    "Films not in 4K from {folder}": "Films niet in 4K uit {folder}",
    "Everything": "Alles",
    "Everything in 4K": "Alles in 4K",
    "4K shows": "4K-series",
    "4K films": "4K-films",
    "Everything not in 4K": "Alles behalve 4K",
    "Shows not in 4K": "Series niet in 4K",
    "Films not in 4K": "Films niet in 4K",
    "{n} film fits this rule.": "{n} film valt onder deze regel.",
    "{n} films fit this rule.": "{n} films vallen onder deze regel.",
    "{n} episode fits this rule.": "{n} aflevering valt onder deze regel.",
    "{n} episodes fit this rule.": "{n} afleveringen vallen onder deze regel.",
    "{n} file doesn't: rule {rule} above takes it.":
        "{n} bestand valt er niet onder: regel {rule} hierboven neemt het.",
    "{n} files don't: rule {rule} above takes them.":
        "{n} bestanden vallen er niet onder: regel {rule} hierboven neemt ze.",
    "{n} file doesn't: it's in a folder inside this one.":
        "{n} bestand valt er niet onder: het staat in een map in deze map.",
    "{n} files don't: they're in folders inside this one.":
        "{n} bestanden vallen er niet onder: ze staan in mappen in deze map.",
    "{n} episode doesn't: it's a show.": "{n} aflevering valt er niet onder: het is een serie.",
    "{n} episodes don't: they're shows.":
        "{n} afleveringen vallen er niet onder: het zijn series.",
    "{n} film doesn't: it's a film.": "{n} film valt er niet onder: het is een film.",
    "{n} films don't: they're films.": "{n} films vallen er niet onder: het zijn films.",
    "{n} film doesn't: it's not 4K.": "{n} film valt er niet onder: die is niet in 4K.",
    "{n} films don't: they're not 4K.": "{n} films vallen er niet onder: die zijn niet in 4K.",
    "{n} episode doesn't: it's not 4K.":
        "{n} aflevering valt er niet onder: die is niet in 4K.",
    "{n} episodes don't: they're not 4K.":
        "{n} afleveringen vallen er niet onder: die zijn niet in 4K.",
    "{n} film doesn't: it's 4K.": "{n} film valt er niet onder: die is in 4K.",
    "{n} films don't: they're 4K.": "{n} films vallen er niet onder: die zijn in 4K.",
    "{n} episode doesn't: it's 4K.": "{n} aflevering valt er niet onder: die is in 4K.",
    "{n} episodes don't: they're 4K.": "{n} afleveringen vallen er niet onder: die zijn in 4K.",
    "Rule {rule} takes it.": "Regel {rule} neemt het.",
    "Rule {rule} takes them.": "Regel {rule} neemt ze.",
    "No rule takes it.": "Geen regel neemt het.",
    "No rule takes them.": "Geen regel neemt ze.",
    "Top folder only": "Alleen de bovenste map",
    "sorted in this folder": "gesorteerd in deze map",
    "your Series folder": "je map Series",
    "your Movies folder": "je map Films",
    "{n} file in {folder} fits no rule and is left alone.":
        "{n} bestand in {folder} past bij geen regel en blijft met rust.",
    "{n} files in {folder} fit no rule and are left alone.":
        "{n} bestanden in {folder} passen bij geen regel en blijven met rust.",
    # app/forge/ui/rule_editor.py
    "Edit rule": "Regel bewerken",
    "New rule": "Nieuwe regel",
    "Shown in the queue as “Rule: {name}”. Leave it empty to have one made from your choices.":
        "In de wachtrij te zien als “Regel: {name}”. Laat het leeg om er een te laten maken "
        "uit je keuzes.",
    "Watch this folder": "Deze map bewaken",
    "Browse…": "Bladeren…",
    "Choose the folder this rule watches": "Kies de map die deze regel bewaakt",
    "Also look in the folders inside it (a download that comes in its own folder, a season pack)":
        "Ook in de mappen erin kijken (een download in een eigen map, een seizoenspakket)",
    "Only the files directly in this folder": "Alleen de bestanden direct in deze map",
    "The folders AirGrabber downloads to": "De mappen waar AirGrabber naartoe downloadt",
    "Take": "Neem",
    "Anything": "Alles",
    "4K only": "Alleen 4K",
    "Not 4K": "Geen 4K",
    "Forge them with": "Forge ze met",
    "This preset names them like: {example}": "Deze voorinstelling noemt ze zo: {example}",
    "Ticked as in the preset. A change here is for this rule only.":
        "Aangevinkt zoals in de voorinstelling. Een wijziging hier geldt alleen voor deze regel.",
    "Then": "Daarna",
    "Move them to": "Verplaats ze naar",
    "Your Series folder": "Je map Series",
    "Your Movies folder": "Je map Films",
    "Choose the folder to move them to": "Kies de map waar ze heen gaan",
    "Sort them inside this folder": "Sorteer ze in deze map",
    "(the preset's folders are made here, e.g. {example})":
        "(de mappen van de voorinstelling komen hier, bijv. {example})",
    "How": "Hoe",
    "Done by itself when MediaForge is sure. If it isn't, the file waits for you.":
        "Gaat vanzelf als MediaForge het zeker weet. Zo niet, dan wacht het bestand op jou.",
    "Put in the queue with this rule filled in. Nothing happens until you press Forge.":
        "Komt in de wachtrij met deze regel ingevuld. Er gebeurt niets tot je op Forge drukt.",
    "Right now in {folder}": "Nu in {folder}",
    "Looking…": "Kijken…",
    "Can't reach this folder right now": "Kan deze map nu niet bereiken",
    "No downloads in this folder right now.": "Nu geen downloads in deze map.",
    "Delete rule": "Regel verwijderen",
    # Settings, reorganized (2026-11-19)
    "AirGrabber doesn't know where its finished downloads end up on this PC. Fill in “Finished downloads” in Settings › Folders, or add a rule for the folder.":
        "AirGrabber weet niet waar zijn voltooide downloads op deze pc terechtkomen. Vul “Voltooide downloads” in bij Instellingen › Mappen, of voeg een regel voor de map toe.",
    "AirGrabber sends downloads to {program} on another computer. Add a rule for the folder where they show up on this PC (a shared network folder), or fill in “Finished downloads” in Settings › Folders.":
        "AirGrabber stuurt downloads naar {program} op een andere computer. Voeg een regel toe voor de map waar ze op deze pc verschijnen (een gedeelde netwerkmap), of vul “Voltooide downloads” in bij Instellingen › Mappen.",
    "Download Station keeps AirGrabber's downloads in its own download folder. Add a rule for the shared folder where they show up on this PC, or fill in “Finished downloads” in Settings › Folders.":
        "Download Station bewaart de downloads van AirGrabber in zijn eigen downloadmap. Voeg een regel toe voor de gedeelde map waar ze op deze pc verschijnen, of vul “Voltooide downloads” in bij Instellingen › Mappen.",
    "Its rule's preset is gone: choose another in Settings › Sorting.":
        "De voorinstelling van de regel is weg: kies een andere in Instellingen › Sorteren.",
    "Choose a library folder in Settings › Folders first.":
        "Kies eerst een bibliotheekmap in Instellingen › Mappen.",
    "Choose your Series and Movies folders in Settings › Folders first.":
        "Kies eerst je map voor series en films in Instellingen › Mappen.",
    "{n} file wasn't forged: choose its library folder in Settings › Folders.":
        "{n} bestand is niet geforged: kies de bibliotheekmap in Instellingen › Mappen.",
    "{n} files weren't forged: choose their library folder in Settings › Folders.":
        "{n} bestanden zijn niet geforged: kies de bibliotheekmap in Instellingen › Mappen.",
    "⚠ Choose your Series folder in Settings › Folders":
        "⚠ Kies je map Series in Instellingen › Mappen",
    "⚠ Choose your Movies folder in Settings › Folders":
        "⚠ Kies je map Films in Instellingen › Mappen",
    "Explorer's right-click menu": "Rechtsklikmenu van Verkenner",
    "? Codes": "? Codes",
    "Watch folders": "Mappen bewaken",
    "When sorting": "Bij het sorteren",
    "Take sorted files off the list (Undo stays in History)":
        "Gesorteerde bestanden van de lijst halen (Ongedaan maken staat in Geschiedenis)",
    "Files and folders Sorting leaves alone.": "Bestanden en mappen die Sorteren met rust laat.",
    "OpenSubtitles": "OpenSubtitles",
}
