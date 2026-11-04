"""The Dutch interface texts (app/i18n.py): the English text → its Dutch one, grouped by the
file that shows them. Friendly, informal Dutch ("je") that reads well in Flanders and the
Netherlands, and short: buttons and chips have little room. Keep every {placeholder} of the
English text. tests/test_i18n.py checks that every translated text has its entry here and that
none is left over. Stdlib only."""

from __future__ import annotations

NL: dict[str, str] = {
    # main.py
    "AirGrabber hit an unrecoverable error.\n\nDetails are in:\n{path}":
        "AirGrabber liep vast op een fout die niet te herstellen is.\n\nDe details staan in:\n{path}",
    # app/models/settings.py
    "Use a number, like 2.5": "Gebruik een getal, zoals 2,5",
    "Use a size above 0": "Gebruik een grootte boven 0",
    "Use a full address like https://nas.local:5001":
        "Gebruik een volledig adres zoals https://nas.local:5001",
    "The first number can't be bigger than the second":
        "Het eerste getal mag niet groter zijn dan het tweede",
    "Choose a folder": "Kies een map",
    "English": "Engels",
    "Dutch": "Nederlands",
    "German": "Duits",
    "French": "Frans",
    "Spanish": "Spaans",
    "Italian": "Italiaans",
    "Portuguese": "Portugees",
    "Portuguese (Brazil)": "Portugees (Brazilië)",
    "Swedish": "Zweeds",
    "Danish": "Deens",
    "Norwegian": "Noors",
    "Finnish": "Fins",
    "Polish": "Pools",
    "Czech": "Tsjechisch",
    "Hungarian": "Hongaars",
    "Romanian": "Roemeens",
    "Greek": "Grieks",
    "Turkish": "Turks",
    "Russian": "Russisch",
    "Ukrainian": "Oekraïens",
    "Arabic": "Arabisch",
    "Hebrew": "Hebreeuws",
    "Hindi": "Hindi",
    "Indonesian": "Indonesisch",
    "Japanese": "Japans",
    "Korean": "Koreaans",
    "Chinese (simplified)": "Chinees (vereenvoudigd)",
    "Chinese (traditional)": "Chinees (traditioneel)",
    # app/ui/main_window.py
    "Open Settings to update": "Open Instellingen om bij te werken",
    "Installs the downloaded update and restarts AirGrabber":
        "Installeert de gedownloade update en start AirGrabber opnieuw",
    "Connected to Prowlarr on this PC · added {sites} to your sites":
        "Verbonden met Prowlarr op deze pc · {sites} toegevoegd aan je sites",
    "Connected to Prowlarr on this PC": "Verbonden met Prowlarr op deze pc",
    "Prowlarr: added {sites} to your sites": "Prowlarr: {sites} toegevoegd aan je sites",
    "Updated to v{version}": "Bijgewerkt naar v{version}",
    "{quality} · picked automatically": "{quality} · automatisch gekozen",
    "Picked automatically": "Automatisch gekozen",
    "{label} is downloading": "{label} wordt gedownload",
    "{label} · downloading (picked automatically)":
        "{label} · wordt gedownload (automatisch gekozen)",
    "Couldn't find {label}": "{label} niet gevonden",
    "No good download turned up in {days} days. Choose one yourself from the calendar.":
        "In {days} dagen dook er geen goede download op. Kies er zelf een in de kalender.",
    "picked automatically from your watchlist": "automatisch gekozen uit je kijklijst",
    "Downloaded {title}": "{title} gedownload",
    "Downloaded {title} ({how})": "{title} gedownload ({how})",
    "No good download turned up in {days} days. Choose one yourself from your watchlist on the Movies page.":
        "In {days} dagen dook er geen goede download op. Kies er zelf een in je kijklijst op de pagina Films.",
    "Automatic download didn't work": "Automatisch downloaden is mislukt",
    "Series": "Series",
    "Movies": "Films",
    "Calendar": "Kalender",
    "Discover": "Ontdekken",
    "Tracked": "Mijn series",
    "{app} — Track your shows. Grab your movies.": "{app} — Volg je series. Haal je films binnen.",
    "Search shows & movies…": "Zoek series en films…",
    "Search": "Zoeken",
    "Settings": "Instellingen",
    "v{current} · v{latest} on GitHub": "v{current} · v{latest} op GitHub",
    "⬆ Update to v{version}": "⬆ Bijwerken naar v{version}",
    "⬆ Restart to update": "⬆ Herstart om bij te werken",
    "AirGrabber {version} is ready. It installs when you close AirGrabber.":
        "AirGrabber {version} staat klaar en wordt geïnstalleerd als je AirGrabber sluit.",
    "⚠ Update failed: {error}": "⚠ Bijwerken mislukt: {error}",
    "Updated to v{version}. Restarting…": "Bijgewerkt naar v{version}. AirGrabber start opnieuw…",
    "last synced {date}": "laatst bijgewerkt {date}",
    "not synced yet": "nog niet bijgewerkt",
    "{shows} tracked shows · {episodes} episodes cached · {synced}":
        "{shows} gevolgde series · {episodes} afleveringen bewaard · {synced}",
    "Syncing library… {done}/{total}": "Bibliotheek bijwerken… {done}/{total}",
    # app/ui/settings_dialog.py
    "Where downloads go": "Waar downloads naartoe gaan",
    "Quality": "Kwaliteit",
    "Automatic": "Automatisch",
    "Movie & show info": "Film- en serie-info",
    "Looks": "Uiterlijk",
    "Updates": "Updates",
    "Advanced": "Geavanceerd",
    "Downloading": "Downloaden",
    "App": "App",
    "What happens when you click Download.": "Wat er gebeurt als je op Downloaden klikt.",
    "Which release AirGrabber picks when you click Download, and for automatic downloads.":
        "Welke release AirGrabber kiest als je op Downloaden klikt, en bij automatische downloads.",
    "What AirGrabber does on its own, without you clicking anything.":
        "Wat AirGrabber zelf doet, zonder dat je ergens op klikt.",
    "The Movies and Discover pages get their lists, posters and release dates from TMDB, a free film database.":
        "De pagina's Films en Ontdekken halen hun lijsten, posters en releasedata bij TMDB, een gratis filmdatabank.",
    "How AirGrabber's pages look.": "Hoe de pagina's van AirGrabber eruitzien.",
    "Getting new versions of AirGrabber.": "Nieuwe versies van AirGrabber krijgen.",
    "Where AirGrabber searches, and which releases it skips. Most people never need to change these.":
        "Waar AirGrabber zoekt en welke releases het overslaat. De meeste mensen hoeven hier nooit iets aan te veranderen.",
    "Open in my torrent program on this PC": "Openen in mijn torrentprogramma op deze pc",
    "Each download opens in your torrent program by itself. Best for most people.":
        "Elke download opent vanzelf in je torrentprogramma. Het beste voor de meeste mensen.",
    "Send to a torrent program on my NAS or another computer":
        "Naar een torrentprogramma op mijn NAS of andere pc",
    "For a NAS or home server (Synology, qBittorrent, Transmission).":
        "Voor een NAS of thuisserver (Synology, qBittorrent, Transmission).",
    "qBittorrent on this PC (set up by AirGrabber)": "qBittorrent op deze pc (ingesteld door AirGrabber)",
    "Save .torrent files to a folder": ".torrent-bestanden in een map opslaan",
    "For a watch folder, for example on a NAS.": "Voor een bewaakte map, bijvoorbeeld op een NAS.",
    "For one episode. Season packs ignore the size.":
        "Voor één aflevering. Bij een volledig seizoen telt de grootte niet.",
    "For one film.": "Voor één film.",
    "If nothing matches, AirGrabber takes the closest match: the right quality first, then the size nearest your range. Leave a size empty for no limit.":
        "Past er niets, dan neemt AirGrabber wat er het dichtst bij komt: eerst de juiste kwaliteit, dan de grootte die het dichtst bij je bereik ligt. Laat een grootte leeg voor geen limiet.",
    "In the background": "Op de achtergrond",
    "Needed for new episodes to download while the window is closed.":
        "Nodig om nieuwe afleveringen te downloaden terwijl het venster dicht is.",
    "Keep running in the tray when I close the window":
        "Blijf actief in het systeemvak als ik het venster sluit",
    "Start AirGrabber when Windows starts": "AirGrabber starten als Windows opstart",
    "New shows": "Nieuwe series",
    "You can still change this for each show.": "Je kunt dit nog per serie aanpassen.",
    "Download new episodes for shows I start following":
        "Nieuwe afleveringen downloaden van series die ik ga volgen",
    "TMDB key": "TMDB-sleutel",
    "A free code from themoviedb.org that lets AirGrabber ask TMDB for this information. Without it, the Movies and Discover pages stay empty. Your shows and the Calendar work without it.":
        "Een gratis code van themoviedb.org waarmee AirGrabber deze informatie aan TMDB kan vragen. Zonder sleutel blijven de pagina's Films en Ontdekken leeg. Je series en de Kalender werken ook zonder.",
    "How do I get a free key?": "Hoe krijg ik een gratis sleutel?",
    "Week starts on": "Week begint op",
    "The first day of each week on the Calendar.": "De eerste dag van elke week in de Kalender.",
    "Theme": "Thema",
    "Light or dark. Follow Windows uses the same look as your other Windows apps.":
        "Licht of donker. Zoals Windows gebruikt hetzelfde uiterlijk als je andere Windows-apps.",
    "Language": "Taal",
    "The language of AirGrabber's pages, buttons and messages.":
        "De taal van de pagina's, knoppen en meldingen van AirGrabber.",
    "Restart AirGrabber to switch the language": "Herstart AirGrabber om van taal te wisselen",
    "Restart now": "Nu herstarten",
    "Accent colour": "Accentkleur",
    "The colour of buttons, chosen items and links.":
        "De kleur van knoppen, gekozen onderdelen en links.",
    "Applies after a restart": "Geldt na een herstart",
    "Info window layout": "Indeling infovenster",
    "Where a film's or series' info window shows its releases.":
        "Waar het infovenster van een film of serie de releases toont.",
    "Side by side": "Naast elkaar",
    "Tabs": "Tabbladen",
    "Spotlight": "Spotlight",
    "Move the spotlight to the next title by itself":
        "Spotlight wisselt vanzelf naar de volgende titel",
    "Look, download quality, card size and language, each with one click.":
        "Uiterlijk, downloadkwaliteit, kaartgrootte en taal, elk met één klik.",
    "Automatic updates": "Automatische updates",
    "New versions download in the background and are installed when AirGrabber closes.":
        "Nieuwe versies worden op de achtergrond gedownload en geïnstalleerd als AirGrabber sluit.",
    "Install updates automatically": "Updates automatisch installeren",
    "Check whether a newer version is out.": "Kijk of er een nieuwere versie is.",
    "Built-in sites": "Ingebouwde sites",
    "Websites AirGrabber searches by itself.": "Websites waarop AirGrabber zelf zoekt.",
    "Skip releases with these words": "Releases met deze woorden overslaan",
    "Separate with commas, for example: cam, telesync":
        "Scheid met komma's, bijvoorbeeld: cam, telesync",
    "Prefer releases from": "Liefst releases van",
    "Release group names, separated with commas":
        "Namen van releasegroepen, gescheiden door komma's",
    "Full HD (1080p)": "Full HD (1080p)",
    "HD (720p)": "HD (720p)",
    "4K (2160p)": "4K (2160p)",
    "Smaller files (1080p x265)": "Kleinere bestanden (1080p x265)",
    "Any quality": "Elke kwaliteit",
    "Follow Windows": "Zoals Windows",
    "Dark": "Donker",
    "Light": "Licht",
    "Series quality": "Kwaliteit series",
    "Movies quality": "Kwaliteit films",
    "Series size": "Grootte series",
    "Movies size": "Grootte films",
    "Prowlarr URL": "Prowlarr-URL",
    "Prowlarr API key": "Prowlarr-API-sleutel",
    "Cancel": "Annuleren",
    "Save": "Opslaan",
    "Browse": "Bladeren",
    "between": "tussen",
    "and": "en",
    "Choose…": "Kiezen…",
    "Check": "Controleren",
    "Version {version}": "Versie {version}",
    "Check for updates": "Updates zoeken",
    "API key": "API-sleutel",
    "Test connection": "Verbinding testen",
    "Saving and testing…": "Opslaan en testen…",
    "Checking GitHub…": "GitHub nakijken…",
    "✓ You have the latest version.": "✓ Je hebt de nieuwste versie.",
    "{version} is on GitHub. This development copy is not updated in place.":
        "{version} staat op GitHub. Deze ontwikkelkopie wordt niet ter plaatse bijgewerkt.",
    "Version {version} is available. AirGrabber restarts to finish.":
        "Versie {version} is beschikbaar. AirGrabber herstart om af te ronden.",
    "Update to {version}": "Bijwerken naar {version}",
    "Where finished downloads end up": "Waar voltooide downloads terechtkomen",
    "Your torrent program's download folder. MediaForge watches it, and the Downloads page looks there for free space and your files.":
        "De downloadmap van je torrentprogramma. MediaForge houdt hem in de gaten, en de pagina Downloads kijkt er voor vrije ruimte en je bestanden.",
    "Choose where finished downloads end up first (Settings → Where downloads go)":
        "Kies eerst waar voltooide downloads terechtkomen (Instellingen → Waar downloads naartoe gaan)",
    "User name": "Gebruikersnaam",
    "Password": "Wachtwoord",
    # app/ui/remote_program_form.py
    "video/tv — empty: Download Station's default":
        "video/tv — leeg: standaardmap van Download Station",
    "/downloads/tv — empty: qBittorrent's default":
        "/downloads/tv — leeg: standaardmap van qBittorrent",
    "/downloads/tv — empty: Transmission's default":
        "/downloads/tv — leeg: standaardmap van Transmission",
    "Program": "Programma",
    "Address": "Adres",
    "Destination folder": "Doelmap",
    "Verify the certificate (https)": "Het certificaat controleren (https)",
    "Some releases have no .torrent file, only a magnet link.":
        "Sommige releases hebben geen .torrent-bestand, alleen een magnetlink.",
    "Those can't be saved to a folder.": "Die kunnen niet in een map worden opgeslagen.",
    "Change": "Wijzigen",
    "Set up a torrent program to receive them": "Stel een torrentprogramma in om ze te ontvangen",
    "Destination folder (optional)": "Doelmap (optioneel)",
    "Films folder": "Map voor films",
    "Leave empty to use the same folder": "Laat leeg om dezelfde map te gebruiken",
    "{program} at {host}": "{program} op {host}",
    "Those are sent to {where}.": "Die gaan naar {where}.",
    # app/ui/prowlarr_panel.py
    " · a free helper program that searches many more sites":
        " · een gratis hulpprogramma dat op veel meer sites zoekt",
    "Windows will ask for permission once; AirGrabber then sets it up":
        "Windows vraagt één keer toestemming; daarna stelt AirGrabber het in",
    "Use a Prowlarr on another computer…": "Een Prowlarr op een andere computer gebruiken…",
    "Checking…": "Controleren…",
    "Couldn't install it automatically.": "Automatisch installeren is niet gelukt.",
    "✓ Connected to Prowlarr on this PC": "✓ Verbonden met Prowlarr op deze pc",
    "✓ Connected to Prowlarr at {address}": "✓ Verbonden met Prowlarr op {address}",
    "Prowlarr is installed but isn't answering. Restart your PC, or open it from the Start menu.":
        "Prowlarr is geïnstalleerd maar antwoordt niet. Herstart je pc, of open het via het Startmenu.",
    "Prowlarr isn't installed": "Prowlarr is niet geïnstalleerd",
    "Not connected": "Niet verbonden",
    "{n} site": "{n} site",
    "{n} sites": "{n} sites",
    "Get Prowlarr": "Prowlarr downloaden",
    "Install Prowlarr": "Prowlarr installeren",
    "Use Prowlarr": "Prowlarr gebruiken",
    "Open Prowlarr": "Prowlarr openen",
    # app/ui/torrent_app_status.py
    "✓ {name} found": "✓ {name} gevonden",
    "No torrent program found": "Geen torrentprogramma gevonden",
    "Installing… Windows may ask for permission": "Installeren… Windows kan om toestemming vragen",
    "✓ qBittorrent installed": "✓ qBittorrent geïnstalleerd",
    "Install qBittorrent": "qBittorrent installeren",
    "Get qBittorrent": "qBittorrent downloaden",
    "Set up qBittorrent for AirGrabber (no pop-up for each download)":
        "qBittorrent instellen voor AirGrabber (geen pop-up per download)",
    "Setting up qBittorrent…": "qBittorrent instellen…",
    "Close qBittorrent to finish: AirGrabber sets it up as soon as it's closed.":
        "Sluit qBittorrent om af te ronden: AirGrabber stelt het in zodra het dicht is.",
    "Couldn't change qBittorrent's settings.": "De instellingen van qBittorrent konden niet worden aangepast.",
    "✓ qBittorrent is installed and ready": "✓ qBittorrent is geïnstalleerd en klaar",
    "Try again": "Opnieuw proberen",
    "Windows' app installer (winget) isn't on this PC. Get qBittorrent from its website.":
        "Het installatieprogramma van Windows (winget) staat niet op deze pc. Download qBittorrent van de website.",
    "Windows couldn't download it. Check your internet connection, then try again.":
        "Windows kon het niet downloaden. Controleer je internetverbinding en probeer het opnieuw.",
    "Windows asked for permission and didn't get it. Try again and choose Yes.":
        "Windows vroeg om toestemming en kreeg die niet. Probeer opnieuw en kies Ja.",
    "Another installation is still running. Wait until it's done, then try again.":
        "Er loopt nog een andere installatie. Wacht tot die klaar is en probeer het dan opnieuw.",
    "The disk is full. Make some room, then try again.":
        "De schijf is vol. Maak wat ruimte vrij en probeer het opnieuw.",
    "This PC's settings don't allow installing programs. Ask whoever manages it.":
        "De instellingen van deze pc staan installeren niet toe. Vraag het aan wie de pc beheert.",
    "Windows needs a restart first. Restart the PC, then try again.":
        "Windows moet eerst opnieuw opstarten. Start de pc opnieuw op en probeer het dan nog eens.",
    "Windows doesn't open torrents with qBittorrent yet.": "Windows opent torrents nog niet met qBittorrent.",
    "Open Default apps": "Standaard-apps openen",
    "In Default apps, choose qBittorrent for .torrent and MAGNET, then click Try again.":
        "Kies in Standaard-apps qBittorrent voor .torrent en MAGNET en klik dan op Opnieuw proberen.",
    "Try again, or get qBittorrent from its website and install it yourself.":
        "Probeer opnieuw, of download qBittorrent van de website en installeer het zelf.",
    # app/ui/torrent_app_status.py: AirGrabber's look in qBittorrent
    "AirGrabber look in qBittorrent": "AirGrabber-uiterlijk in qBittorrent",
    "qBittorrent in AirGrabber's colours, light or dark like Windows (qBittorrent 5). AirGrabber changes it while qBittorrent is closed.":
        "qBittorrent in de kleuren van AirGrabber, licht of donker zoals Windows (qBittorrent 5). AirGrabber past het aan terwijl qBittorrent dicht is.",
    "Close qBittorrent to finish: AirGrabber changes its look as soon as it's closed.":
        "Sluit qBittorrent om af te ronden: AirGrabber past het uiterlijk aan zodra het dicht is.",
    # app/ui/tmdb_key_guide.py
    "Movies and Discover": "Films en Ontdekken",
    "These pages need a free key from The Movie Database. It takes about two minutes.":
        "Deze pagina's hebben een gratis sleutel van The Movie Database nodig. Dat duurt ongeveer twee minuten.",
    "✓ The key works": "✓ De sleutel werkt",
    "That key didn't work. Check that you copied the \"API Key\" (not the long token).":
        "Die sleutel werkt niet. Kijk na of je de \"API Key\" hebt gekopieerd (niet het lange token).",
    "Couldn't reach The Movie Database. Check your internet connection and try again.":
        "The Movie Database is niet bereikbaar. Controleer je internetverbinding en probeer opnieuw.",
    "Create a free account, or sign in if you already have one":
        "Maak een gratis account aan, of log in als je er al een hebt",
    "Sign up or sign in": "Registreren of inloggen",
    "Ask for an API key": "Vraag een API-sleutel aan",
    "Choose \"Developer\"; the app details can be anything":
        "Kies \"Developer\"; de app-gegevens mogen van alles zijn",
    "Open API page": "API-pagina openen",
    "Copy the \"API Key\" and paste it below": "Kopieer de \"API Key\" en plak hem hieronder",
    "TMDB API key": "TMDB-API-sleutel",
    # app/ui/card_scale.py
    "Card size": "Kaartgrootte",
    "How big the cards are on the calendar, Movies, Discover, My shows and in search results. Smaller cards fit more on the screen.":
        "Hoe groot de kaarten zijn in de kalender, Films, Ontdekken, Mijn series en de zoekresultaten. Met kleinere kaarten past er meer op het scherm.",
    # app/ui/spotlight_band.py
    "Downloads": "Downloads",
    # app/ui/spotlight.py
    "Trending": "Populair",
    "New to stream": "Net digitaal uit",
    "Tonight": "Vanavond",
    "Tomorrow": "Morgen",
    "{show} — {code} “{name}”": "{show} — {code} ‘{name}’",
    "{n} episode today": "{n} aflevering vandaag",
    "{n} episodes today": "{n} afleveringen vandaag",
    "{n} episode in the next 7 days": "{n} aflevering in de komende 7 dagen",
    "{n} episodes in the next 7 days": "{n} afleveringen in de komende 7 dagen",
    # app/ui/import_dialog.py
    "Show names": "Namen van series",
    "From airdates.tv": "Van airdates.tv",
    "Import Shows": "Series importeren",
    "Paste show names or TVMaze IDs, one per line:":
        "Plak namen van series of TVMaze-ID's, één per regel:",
    "Import": "Importeren",
    "Enter at least one show.": "Vul minstens één serie in.",
    # app/ui/whats_new_dialog.py
    "OK": "OK",
    "Also new": "Ook nieuw",
    # app/ui/borderless.py: the pop-ups' round ✕
    "Close (Esc)": "Sluiten (Esc)",
    "Close": "Sluiten",
    # app/ui/watchlist_button.py
    "+ Watchlist": "+ Kijklijst",
    "✓ On watchlist": "✓ Op kijklijst",
    "Add to your watchlist": "Op je kijklijst zetten",
    "On your watchlist. Click to take it off.": "Staat op je kijklijst. Klik om het eraf te halen.",
    # app/ui/media_cards.py
    "{minutes}m": "{minutes} min",
    "{hours}h {minutes}m": "{hours} u {minutes} min",
    "{hours}h": "{hours} u",
    "Search / Grab": "Zoeken / Downloaden",
    "Digital {date}": "Digitaal {date}",
    "Not Released": "Nog niet uit",
    "Finding…": "Zoeken…",
    # app/ui/views/movie_view.py
    "Hides the films you have in Plex (your watchlist always stays)":
        "Verbergt de films die je in Plex hebt (je kijklijst blijft altijd staan)",
    "You already have every film here.": "Je hebt alle films hier al.",
    "Movie Discovery": "Films ontdekken",
    "↻ Refresh": "↻ Vernieuwen",
    "Just Dropped (Digital)": "Net uit (digitaal)",
    "Trending Movies": "Populaire films",
    "Upcoming Digital": "Binnenkort digitaal",
    # app/ui/views/shelves.py
    "TMDB API key needed": "TMDB-API-sleutel nodig",
    "Add your TMDB API key in ⚙ Settings to see trending, airing and upcoming titles.":
        "Vul je TMDB-API-sleutel in bij ⚙ Instellingen om populaire, lopende en komende titels te zien.",
    "Open Settings": "Instellingen openen",
    "TMDB rejected the API key": "TMDB weigert de API-sleutel",
    "Check the key in ⚙ Settings.": "Kijk de sleutel na bij ⚙ Instellingen.",
    "Couldn't reach TMDB": "TMDB is niet bereikbaar",
    "Retry": "Opnieuw proberen",
    "Couldn't load {title} right now.": "{title} kon nu niet geladen worden.",
    # app/ui/views/discover_view.py
    "Hides the shows you already follow": "Verbergt de series die je al volgt",
    "You already follow every show here.": "Je volgt alle series hier al.",
    "Trending This Week": "Populair deze week",
    "Currently Airing": "Nu op tv",
    "Upcoming Premieres": "Binnenkort nieuw",
    "{name} isn't on TVMaze yet": "{name} staat nog niet op TVMaze",
    "Couldn't track {name}: {error}": "Kon {name} niet volgen: {error}",
    "Now tracking {name}": "Je volgt nu {name}",
    # app/ui/shelf_filter.py
    "Action": "Actie",
    "Comedy": "Komedie",
    "Drama": "Drama",
    "Animation": "Animatie",
    "Documentary": "Documentaire",
    "Kids & Family": "Kids & familie",
    "Crime": "Misdaad",
    "Sci-Fi & Fantasy": "Sci-fi & fantasy",
    "Horror": "Horror",
    "All": "Alles",
    "Hide what I already have": "Verberg wat ik al heb",
    "Hide shows I follow": "Verberg series die ik volg",
    "Nothing new in {genre} here right now. Pick another genre or {all}.":
        "Hier staat nu niets nieuws in {genre}. Kies een ander genre of {all}.",
    "Nothing in {genre} here right now. Pick another genre or {all}.":
        "Hier staat nu niets in {genre}. Kies een ander genre of {all}.",
    # app/ui/download_control.py
    "⬇ Download": "⬇ Download",
    "Choose a download yourself": "Kies zelf een download",
    "Download": "Downloaden",
    "Finding the best one…": "De beste zoeken…",
    "Queued": "In de wachtrij",
    "No season pack found; the episodes are queued.":
        "Geen seizoenspakket gevonden; de afleveringen staan in de wachtrij.",
    "Something went wrong with {label} ({error})": "Er ging iets mis met {label} ({error})",
    "⬇ Whole season": "⬇ Hele seizoen",
    "Download the whole season in one go (a season pack). Without one, the episodes you still need are queued one by one.":
        "Download het hele seizoen in één keer (een seizoenspakket). Is er geen, dan komen de afleveringen die je nog nodig hebt een voor een in de wachtrij.",
    "⬇ All missing ({n})": "⬇ Alles wat ontbreekt ({n})",
    "Download this season's missing episodes":
        "Download de ontbrekende afleveringen van dit seizoen",
    "Waiting ({n})": "Wachtend ({n})",
    "Remove from the queue": "Uit de wachtrij halen",
    "✓ Opened": "✓ Geopend",
    "✓ Saved": "✓ Bewaard",
    "✓ Sent": "✓ Verstuurd",
    "smaller files (1080p x265)": "kleinere bestanden (1080p x265)",
    "any quality": "elke kwaliteit",
    "at least {size} GB": "minstens {size} GB",
    "up to {size} GB": "tot {size} GB",
    "Looks for {quality}": "Zoekt {quality}",
    "Picked {what}": "Gekozen: {what}",
    "No good download found for {label}. Choose one yourself.":
        "Geen goede download gevonden voor {label}. Kies er zelf een.",
    "{label} was already downloaded": "{label} was al gedownload",
    "{n} seeder": "{n} seeder",
    "{n} seeders": "{n} seeders",
    # app/ui/missing_control.py
    "⬇ Download missing": "⬇ Ontbrekende downloaden",
    "Finding missing episodes…": "Ontbrekende afleveringen zoeken…",
    "Starting…": "Starten…",
    "Couldn't download": "Downloaden mislukt",
    "Nothing missing": "Niets ontbreekt",
    "Missing": "Ontbreekt",
    "In Plex": "In Plex",
    "More options": "Meer opties",
    "{n} missing": "{n} ontbrekend",
    "Downloading {index} of {total}…": "{index} van {total} downloaden…",
    "Getting {index} of {total}…": "{index} van {total} halen…",
    "{index} of {total}…": "{index} van {total}…",
    "✓ {got} downloaded · stopped": "✓ {got} gedownload · gestopt",
    "✓ {got} · stopped": "✓ {got} · gestopt",
    "Stopped": "Gestopt",
    "Failed": "Mislukt",
    "✓ {got} downloaded · {missed} not found yet":
        "✓ {got} gedownload · {missed} nog niet gevonden",
    "✓ {got} downloaded · {missed} not found": "✓ {got} gedownload · {missed} niet gevonden",
    "✓ {got} · {missed} not found": "✓ {got} · {missed} niet gevonden",
    "✓ {got} downloaded": "✓ {got} gedownload",
    "{missed} not found yet": "{missed} nog niet gevonden",
    "Not found yet": "Nog niet gevonden",
    "{got} downloaded": "{got} gedownload",
    "{n} more missing: download missing again for those":
        "nog {n} ontbrekend: kies nog eens Ontbrekende downloaden voor die",
    "stopped: {error}": "gestopt: {error}",
    "nothing missing": "niets ontbreekt",
    "{n} episode": "{n} aflevering",
    "{n} episodes": "{n} afleveringen",
    # app/ui/poster_card.py
    "✓ Following": "✓ Volgend",
    "+ Track": "+ Volgen",
    "Not on TVMaze": "Niet op TVMaze",
    # app/ui/cards.py
    "Auto": "Auto",
    "Auto-download: new episodes of this show download by themselves, about 2 hours after they air.":
        "Automatisch downloaden: nieuwe afleveringen van deze serie downloaden vanzelf, ongeveer 2 uur na de uitzending.",
    "Open on IMDb": "Openen op IMDb",
    "Remove show…": "Serie verwijderen…",
    "Next: {code} · {when}": "Volgende: {code} · {when}",
    "Adding…": "Toevoegen…",
    # app/ui/calendar_cards.py
    "Not aired": "Niet uitgezonden",
    "More about this show": "Meer over deze serie",
    "Not downloaded yet": "Nog niet gedownload",
    "Not downloaded": "Niet gedownload",
    "Aired, but not in Plex and not downloaded yet.":
        "Uitgezonden, maar nog niet in Plex en nog niet gedownload.",
    "Nothing good enough was found. Choose a download yourself.":
        "Er is niets goed genoeg gevonden. Kies zelf een download.",
    "Not Aired": "Niet uitgezonden",
    "Suggested": "Suggestie",
    # app/ui/calendar_stack.py
    "⬇ Download all ({n})": "⬇ Alle downloaden ({n})",
    "⬇ Download ({n})": "⬇ Download ({n})",
    "⬇ All ({n})": "⬇ Alle ({n})",
    "not aired": "niet uitgezonden",
    "✓ in Plex": "✓ in Plex",
    "still needed": "nog nodig",
    # app/ui/watchlist_cards.py
    "📌 My watchlist": "📌 Mijn kijklijst",
    "Download when it's out": "Downloaden zodra uit",
    "Out now": "Nu uit",
    "Waiting for a good release": "Wacht op een goede release",
    "Release date unknown": "Releasedatum onbekend",
    "{n} film": "{n} film",
    "{n} films": "{n} films",
    "just now": "net",
    "{n} min ago": "{n} min geleden",
    "{n} h ago": "{n} u geleden",
    "{n} day ago": "{n} dag geleden",
    "{n} days ago": "{n} dagen geleden",
    "Out digitally {date}": "Digitaal op {date}",
    "In cinemas {date}": "In de bioscoop {date}",
    "Expected {date}": "Verwacht {date}",
    "No good release found in {days} days": "Geen goede release gevonden in {days} dagen",
    "{status} (searched {ago})": "{status} ({ago} gezocht)",
    # app/ui/search.py
    "Search Results: {query}": "Zoekresultaten: {query}",
    "📺 TV Shows": "📺 Series",
    "🎬 Movies": "🎬 Films",
    "Couldn't reach TVMaze: {error}": "TVMaze is niet bereikbaar: {error}",
    "No TV shows found.": "Geen series gevonden.",
    "No movies found.": "Geen films gevonden.",
    "Add a TMDB API key in ⚙ Settings to search movies.":
        "Vul een TMDB-API-sleutel in bij ⚙ Instellingen om films te zoeken.",
    "TMDB rejected the API key — check it in ⚙ Settings.":
        "TMDB weigert de API-sleutel — kijk hem na bij ⚙ Instellingen.",
    "Couldn't reach TMDB: {error}": "TMDB is niet bereikbaar: {error}",
    # app/ui/show_details.py
    "More": "Meer",
    "Couldn't find this show on TVMaze": "Deze serie staat niet op TVMaze",
    "No episodes listed yet.": "Nog geen afleveringen bekend.",
    "TBA": "Nog onbekend",
    "Running": "Loopt",
    "Ended": "Afgelopen",
    "To Be Determined": "Nog onzeker",
    "In Development": "In ontwikkeling",
    "Next: {code}": "Volgende: {code}",
    "Special": "Special",
    "Couldn't load the episodes: {error}": "Kon de afleveringen niet laden: {error}",
    "Season {n}": "Seizoen {n}",
    "Could not switch automatic downloads on for {name}":
        "Kon automatisch downloaden niet aanzetten voor {name}",
    "Could not switch automatic downloads off for {name}":
        "Kon automatisch downloaden niet uitzetten voor {name}",
    "Opening {name}…": "{name} openen…",
    "Couldn't open {name}: {error}": "Kon {name} niet openen: {error}",
    "{n} season": "{n} seizoen",
    "{n} seasons": "{n} seizoenen",
    "Adult": "Volwassenen",
    "Adventure": "Avontuur",
    "Anime": "Anime",
    "Children": "Kinderen",
    "DIY": "Doe-het-zelf",
    "Espionage": "Spionage",
    "Family": "Familie",
    "Fantasy": "Fantasy",
    "Food": "Eten",
    "History": "Geschiedenis",
    "Legal": "Juridisch",
    "Medical": "Medisch",
    "Music": "Muziek",
    "Mystery": "Mysterie",
    "Nature": "Natuur",
    "Romance": "Romantiek",
    "Science fiction": "Sciencefiction",
    "Sports": "Sport",
    "Supernatural": "Bovennatuurlijk",
    "Thriller": "Thriller",
    "Travel": "Reizen",
    "War": "Oorlog",
    "Western": "Western",
    # 2.0.7: app/ui/show_details.py (new episodes, when one airs)
    "New · aired today": "Nieuw · vandaag",
    "New · aired yesterday": "Nieuw · gisteren",
    "New": "Nieuw",
    "Aired less than a day ago: a release usually turns up within hours. It counts as missing after a day.":
        "Minder dan een dag geleden uitgezonden: meestal is er binnen enkele uren een release. Na een dag telt hij als ontbrekend.",
    "airs today": "vandaag op tv",
    "airs tomorrow": "morgen op tv",
    "airs in {n} day": "over {n} dag op tv",
    "airs in {n} days": "over {n} dagen op tv",
    # 2.0.7: app/ui/calendar_stack.py
    "new": "nieuw",
    # 2.0.7: app/ui/row_actions.py
    "⬇ Auto": "⬇ Auto",
    "Download automatically: AirGrabber picks the best release":
        "Automatisch downloaden: AirGrabber kiest de beste release",
    "☰ Choose…": "☰ Kiezen…",
    "Choose a release yourself from the search results": "Kies zelf een release uit de zoekresultaten",
    "⬇ Download again automatically": "⬇ Automatisch opnieuw downloaden",
    "☰ Choose a release…": "☰ Zelf een release kiezen…",
    "No good release found": "Geen goede release gevonden",
    "None found": "Niets gevonden",
    # 2.0.7: app/ui/views/downloads_view.py (filter, Clear finished, the ⋯ menu)
    "✓ Clear finished": "✓ Voltooide wissen",
    "Takes finished downloads, and ones your torrent program no longer has, off this list. Your files and AirGrabber's history stay.":
        "Haalt voltooide downloads, en downloads die je torrentprogramma niet meer heeft, van deze lijst. Je bestanden en de geschiedenis van AirGrabber blijven.",
    "Nothing here right now.": "Hier staat nu niets.",
    "Open the folder": "Map openen",
    "Download again…": "Opnieuw downloaden…",
    "Remove from list": "Van de lijst halen",
    "Only takes it off this list. Your files stay where they are.":
        "Haalt hem alleen van deze lijst. Je bestanden blijven waar ze zijn.",
    "Not found in the folder where finished downloads end up":
        "Niet gevonden in de map waar voltooide downloads terechtkomen",
    "The folder where finished downloads end up can't be reached":
        "De map waar voltooide downloads terechtkomen is niet bereikbaar",
    "AirGrabber doesn't know this show any more": "AirGrabber kent deze serie niet meer",
    "Finished": "Voltooid",
    "Problems": "Problemen",
    # 3.0.2: app/ui/views/downloads_view.py (in sync with the torrent program)
    "✓ Clear finished…": "✓ Voltooide wissen…",
    "Removes finished downloads from {program} and from this list. Their files stay where they are.":
        "Haalt voltooide downloads weg uit {program} en van deze lijst. Hun bestanden blijven waar ze zijn.",
    "Live from {program} · {n} download": "Live uit {program} · {n} download",
    "Live from {program} · {n} downloads": "Live uit {program} · {n} downloads",
    "sent by AirGrabber": "verstuurd door AirGrabber",
    "Added in {program}": "Toegevoegd in {program}",
    "Recognised as {name}": "Herkend als {name}",
    "⏸ Pause": "⏸ Pauzeren",
    "▶ Resume": "▶ Hervatten",
    "🗑 Remove from {program}…": "🗑 Weghalen uit {program}…",
    "Remove from {program}?": "Weghalen uit {program}?",
    "{name} is removed from {program} and from this list. What it downloaded so far stays on the NAS.":
        "{name} wordt weggehaald uit {program} en van deze lijst. Wat al gedownload is, blijft op de NAS.",
    "{name} is removed from {program} and from this list. What it downloaded so far stays on this PC.":
        "{name} wordt weggehaald uit {program} en van deze lijst. Wat al gedownload is, blijft op deze pc.",
    "{name} is removed from {program} and from this list. Its unfinished download is removed too; finished files stay.":
        "{name} wordt weggehaald uit {program} en van deze lijst. De onvoltooide download gaat ook weg; voltooide bestanden blijven.",
    "Clear finished downloads?": "Voltooide downloads wissen?",
    "{n} finished download is removed from {program} and from this list. Its files stay where they are.":
        "{n} voltooide download wordt weggehaald uit {program} en van deze lijst. De bestanden blijven waar ze zijn.",
    "{n} finished downloads are removed from {program} and from this list. Their files stay where they are.":
        "{n} voltooide downloads worden weggehaald uit {program} en van deze lijst. De bestanden blijven waar ze zijn.",
    "Clear {n}": "{n} wissen",
    "Paused at {percent} %": "Gepauzeerd op {percent} %",
    "✓ Finished": "✓ Voltooid",
    "Waiting for {program} to pick it up…": "Wacht tot {program} hem oppakt…",
    "{program} never picked this up": "{program} heeft deze nooit opgepakt",
    # 2.0.7: app/ui/main_window.py
    "Couldn't look up {title} on TMDB": "Kon {title} niet opzoeken bij TMDB",
    # app/ui/trailer.py (the trailer player's Fill toggle)
    "Fill the window": "Venster vullen",
    "Show the whole picture": "Hele beeld tonen",
    # app/ui/title_info.py
    "Watch trailer": "Trailer bekijken",
    "Opening trailer…": "Trailer openen…",
    "Add a TMDB key in Settings for posters, trailers and cast":
        "Vul een TMDB-sleutel in bij Instellingen voor posters, trailers en de cast",
    "Rumoured": "Gerucht",
    "Planned": "Gepland",
    "In production": "In productie",
    "Post-production": "In postproductie",
    "Cancelled": "Geannuleerd",
    "Out digitally since {date}": "Digitaal uit sinds {date}",
    "Out digitally on {date}": "Digitaal uit op {date}",
    "Episodes": "Afleveringen",
    "Cast": "Cast",
    "← Episodes": "← Afleveringen",
    "Click ☰ Choose… on an episode to see its releases here.":
        "Klik op ☰ Kiezen… bij een aflevering om de releases hier te zien.",
    "Releases · {code}": "Releases · {code}",
    "+{n} more": "+{n} meer",
    # app/ui/tray.py
    "Open AirGrabber": "AirGrabber openen",
    "Open MediaForge": "MediaForge openen",
    "Check for new episodes now": "Nu naar nieuwe afleveringen kijken",
    "Quit": "Afsluiten",
    "AirGrabber keeps running": "AirGrabber blijft actief",
    "It's in the tray next to the clock (click ^ if you don't see it), so new episodes still download. Right-click its icon and choose Quit to close it completely.":
        "Het staat in het systeemvak naast de klok (klik op ^ als je het niet ziet), zodat nieuwe afleveringen nog steeds downloaden. Klik met de rechtermuisknop op het icoon en kies Afsluiten om het helemaal te sluiten.",
    "Got it": "Begrepen",
    # app/ui/welcome_dialog.py
    "Already follow shows on airdates.tv?": "Volg je al series op airdates.tv?",
    "Bring your list over, so your calendar is ready right away.":
        "Neem je lijst mee, dan staat je kalender meteen klaar.",
    "Import from airdates.tv": "Importeren van airdates.tv",
    "Follow your favourite shows, see what's on each day, and download episodes and movies with one click.":
        "Volg je favoriete series, zie wat er elke dag op is, en download afleveringen en films met één klik.",
    "For a NAS or home server (Synology, qBittorrent, Transmission)":
        "Voor een NAS of thuisserver (Synology, qBittorrent, Transmission)",
    "Step {step} of {count}": "Stap {step} van {count}",
    "Back": "Terug",
    "Next": "Volgende",
    "Skip": "Overslaan",
    "Skip for now": "Nu overslaan",
    "Finish": "Klaar",
    "Welcome to {app}": "Welkom bij {app}",
    "Get started": "Aan de slag",
    "Yes, import my shows": "Ja, importeer mijn series",
    "No, I'll add shows myself": "Nee, ik voeg zelf series toe",
    "How do you want to download?": "Hoe wil je downloaden?",
    "You can change this later in Settings.": "Je kunt dit later nog wijzigen in Instellingen.",
    "Open in my torrent program": "Openen in mijn torrentprogramma",
    "Save to a folder": "In een map opslaan",
    "Choose folder…": "Map kiezen…",
    "You can skip this and do it later.": "Je kunt dit overslaan en later doen.",
    # app/ui/quick_setup.py
    "Make it yours": "Maak het van jou",
    "You can change all of this later in Settings.":
        "Je kunt dit allemaal later nog wijzigen in Instellingen.",
    "Look": "Uiterlijk",
    "Download quality": "Downloadkwaliteit",
    "Quick setup": "Snel instellen",
    "Like Windows": "Zoals Windows",
    "Restart AirGrabber to switch the accent colour":
        "Herstart AirGrabber om van accentkleur te wisselen",
    "Restart AirGrabber to switch the language and the accent colour":
        "Herstart AirGrabber om van taal en accentkleur te wisselen",
    # app/ui/palettes.py (the accent colours)
    "Slate": "Leisteen",
    "Ocean": "Oceaan",
    "Petrol": "Petrol",
    "Teal": "Groenblauw",
    "Sage": "Salie",
    "Indigo": "Indigo",
    "Plum": "Pruim",
    "Rose": "Roze",
    "Mocha": "Mokka",
    "Graphite": "Grafiet",
    "Smaller files": "Kleinere bestanden",
    "720p · saves space": "720p · spaart ruimte",
    "Recommended": "Aanbevolen",
    "1080p · Full HD": "1080p · Full HD",
    "Best picture": "Beste beeld",
    "2160p (4K) · big files": "2160p (4K) · grote bestanden",
    "Small": "Klein",
    "more on screen": "meer op het scherm",
    "Medium": "Middel",
    "Large": "Groot",
    "bigger posters": "grotere posters",
    # app/ui/card_preview.py
    "Your show": "Jouw serie",
    "{size} (now)": "{size} (nu)",
    # app/ui/airdates_import.py
    "Couldn't reach airdates.tv. Check your internet connection and try again.":
        "airdates.tv is niet bereikbaar. Controleer je internetverbinding en probeer opnieuw.",
    "Untick any that don't look right.": "Vink uit wat niet klopt.",
    "from \"{name}\"": "als \"{name}\"",
    "Found {found} of {n} show": "{found} van {n} serie gevonden",
    "Found {found} of {n} shows": "{found} van {n} series gevonden",
    "Couldn't find: {names}": "Niet gevonden: {names}",
    "airdates.tv didn't recognise {n} show": "airdates.tv herkende {n} serie niet",
    "airdates.tv didn't recognise {n} shows": "airdates.tv herkende {n} series niet",
    "{n} couldn't be looked up; try again later":
        "Opzoeken mislukt voor {n}; probeer later opnieuw",
    "Looking up {done} of {total}…": "{done} van {total} opzoeken…",
    "Importing {done} of {total}…": "{done} van {total} importeren…",
    "Import {n} show": "Importeer {n} serie",
    "Import {n} shows": "Importeer {n} series",
    "Imported {n} show": "{n} serie geïmporteerd",
    "Imported {n} shows": "{n} series geïmporteerd",
    "Couldn't import: {names} — try again later":
        "Niet geïmporteerd: {names} — probeer het later opnieuw",
    "Nothing to paste yet: copy the text on airdates.tv first.":
        "Nog niets om te plakken: kopieer eerst de tekst op airdates.tv.",
    "Paste": "Plakken",
    "Open airdates.tv in the browser you use for it":
        "Open airdates.tv in de browser die je ervoor gebruikt",
    "Open airdates.tv": "airdates.tv openen",
    "At the very bottom, click ": "Klik helemaal onderaan op ",
    "and copy the text": "en kopieer de tekst",
    "Paste it here": "Plak hem hier",
    "Find my shows": "Zoek mijn series",
    "{name} · already following": "{name} · volg je al",
    # app/ui/widgets.py: the right-click menu of text fields ("Paste" above)
    "Cut": "Knippen",
    "Copy": "Kopiëren",
    "Select all": "Alles selecteren",
    # app/ui/views/calendar_view.py
    "How many weeks to show": "Hoeveel weken je wilt zien",
    "💡 Suggestions": "💡 Suggesties",
    "Also show one popular episode a day from a show you don't follow yet. Click + Track on it to start following that show.":
        "Toon ook elke dag een populaire aflevering van een serie die je nog niet volgt. Klik erop op + Volgen om die serie te volgen.",
    "Today": "Vandaag",
    "Previous week": "Vorige week",
    "Next week": "Volgende week",
    "Finding suggestions…": "Suggesties zoeken…",
    "Couldn't load suggestions": "Kon de suggesties niet laden",
    # app/ui/views/tracked_view.py
    "Tracked TV Shows": "Gevolgde series",
    "A–Z": "A–Z",
    "Next episode": "Volgende aflevering",
    "Recently downloaded": "Laatst gedownload",
    "Has missing": "Met ontbrekende",
    "Auto on": "Auto aan",
    "No shows.": "Geen series.",
    "No shows with missing episodes.": "Geen series met ontbrekende afleveringen.",
    "No shows with automatic downloads on.":
        "Geen series waarvoor automatisch downloaden aanstaat.",
    "No ended shows.": "Geen afgelopen series.",
    "Sort:": "Sorteer:",
    "Show:": "Toon:",
    "Remove ended shows…": "Afgelopen series verwijderen…",
    "{names} … and {n} more": "{names} … en nog {n}",
    "Added {n} show.": "{n} serie toegevoegd.",
    "Added {n} shows.": "{n} series toegevoegd.",
    "Already tracked: {n} ({names})": "Al gevolgd: {n} ({names})",
    "Not found: {names}": "Niet gevonden: {names}",
    "Failed (network): {names}": "Mislukt (netwerk): {names}",
    "Nothing to import.": "Niets te importeren.",
    "{title} ({shown} of {total})": "{title} ({shown} van {total})",
    "No tracked shows yet": "Je volgt nog geen series",
    "Search for a show at the top right, or use Import Shows to paste a list.":
        "Zoek rechtsboven een serie, of gebruik Series importeren om een lijst te plakken.",
    "Removed {name}": "{name} verwijderd",
    "Removed {n} ended show": "{n} afgelopen serie verwijderd",
    "Removed {n} ended shows": "{n} afgelopen series verwijderd",
    "No ended shows to remove": "Geen afgelopen series om te verwijderen",
    "Importing shows… {done}/{total}": "Series importeren… {done}/{total}",
    "Import failed: {error}": "Importeren mislukt: {error}",
    "Import finished": "Importeren klaar",
    "Import complete": "Importeren voltooid",
    # app/ui/views/downloads_view.py
    "No downloads yet": "Nog geen downloads",
    "Episodes and films you download show up here for 30 days.":
        "Afleveringen en films die je downloadt, staan hier 30 dagen.",
    "Progress shows when AirGrabber can reach your torrent program — set it up in Settings → Where downloads go":
        "De voortgang verschijnt als AirGrabber je torrentprogramma kan bereiken — stel het in bij Instellingen → Waar downloads naartoe gaan",
    "Downloading {percent} %": "Downloaden {percent} %",
    "Waiting": "Wacht",
    "Problem in {program}": "Probleem in {program}",
    "Problem": "Probleem",
    "Not found": "Niet gevonden",
    "Status unknown": "Status onbekend",
    "Sent to your torrent program": "Naar je torrentprogramma gestuurd",
    "Saved to folder": "In map opgeslagen",
    "Opened in {program}": "Geopend in {program}",
    "Today {time}": "Vandaag {time}",
    "Yesterday {time}": "Gisteren {time}",
    "Your NAS is almost full: {size} free": "Je NAS is bijna vol: nog {size} vrij",
    "Your download drive is almost full: {size} free":
        "Je downloadschijf is bijna vol: nog {size} vrij",
    # app/ui/release_panel.py
    "ANY": "ALLES",
    "Downloading…": "Downloaden…",
    "Click a release above to select it": "Klik hierboven op een release om hem te kiezen",
    "Name": "Naam",
    "Size": "Grootte",
    "Seeds": "Seeds",
    "Leech": "Leech",
    "Source": "Bron",
    "Age": "Ouderdom",
    "Open this site's search in your browser": "De zoekpagina van deze site openen in je browser",
    "{n}m": "{n}m",
    "{n}h": "{n}u",
    "{n}d": "{n}d",
    "{n}y": "{n}j",
    "failed": "mislukt",
    "Searching indexers…": "Bronnen doorzoeken…",
    "Searching indexers for Season {season} pack…":
        "Bronnen doorzoeken naar volledig seizoen {season}…",
    "Searching indexers for {code}…": "Bronnen doorzoeken naar {code}…",
    "{n} hidden by name/episode matching or your filters — Hide":
        "{n} verborgen door de naam-/afleveringscontrole of je filters — Verbergen",
    "{n} hidden by name/episode matching or your filters — Show":
        "{n} verborgen door de naam-/afleveringscontrole of je filters — Tonen",
    "✎ Fix Match": "✎ Koppeling herstellen",
    "SEASON": "SEIZOEN",
    "EPISODE": "AFLEVERING",
    "VERSION": "VERSIE",
    "Releases": "Releases",
    "Selected": "Gekozen",
    "Now tracking {name} (TVMaze {id})": "Je volgt nu {name} (TVMaze {id})",
    "“{answer}” is not a TVMaze ID (a number).": "“{answer}” is geen TVMaze-ID (een getal).",
    "Fetching TVMaze show {id}…": "TVMaze-serie {id} ophalen…",
    "TVMaze has no show with ID {id}.": "TVMaze heeft geen serie met ID {id}.",
    "Couldn't fetch TVMaze show {id}: {error}": "Kon TVMaze-serie {id} niet ophalen: {error}",
    "Enter the correct TVMaze ID for this show:": "Vul de juiste TVMaze-ID voor deze serie in:",
    "Fix Match": "Koppeling herstellen",
    "Pack": "Alles",
    "Searching… {n} found": "Zoeken… {n} gevonden",
    "Releases ({n})": "Releases ({n})",
    "No matching releases found.": "Geen passende releases gevonden.",
    "★ Recommended: {name}": "★ Aanbevolen: {name}",
    "fast": "snel",
    "slower": "trager",
    "may stall": "kan blijven hangen",
    "seeders unknown": "seeders onbekend",
    "most seeders": "meeste seeders",
    "lots of seeders": "veel seeders",
    "few seeders": "weinig seeders",
    "right size": "juiste grootte",
    "smaller than your size range": "kleiner dan je gekozen grootte",
    "bigger than your size range": "groter dan je gekozen grootte",
    "small size": "klein bestand",
    "normal size": "normale grootte",
    "large size": "groot bestand",
    "size unknown": "grootte onbekend",
    "your preferred group": "je favoriete releasegroep",
    "a pack with this episode": "een bundel met deze aflevering",
    # app/services/downloads.py
    "No torrent program is set up on this PC. Install one, such as qBittorrent, or choose another option under \"Where downloads go\" in Settings.":
        "Er is geen torrentprogramma ingesteld op deze pc. Installeer er een, zoals qBittorrent, of kies een andere optie onder \"Waar downloads naartoe gaan\" in Instellingen.",
    "No .torrent file is available for this release, and no torrent program on a NAS or another computer is set up in Settings to send its magnet link to.":
        "Er is geen .torrent-bestand voor deze release, en er is in Instellingen geen torrentprogramma op een NAS of andere computer ingesteld om de magnetlink naartoe te sturen.",
    "No .torrent file or magnet link is available for this release.":
        "Er is geen .torrent-bestand of magnetlink voor deze release.",
    "Saved {name}": "{name} opgeslagen",
    "Sent to {program}": "Naar {program} gestuurd",
    "Renamed → {folder}": "Hernoemd → {folder}",
    "Opened in your torrent program":"Geopend in je torrentprogramma",
    "No .torrent file is available for this release, and it has no magnet link to send to {program}.":
        "Er is geen .torrent-bestand voor deze release, en ook geen magnetlink om naar {program} te sturen.",
    "Couldn't open this release in {program}: {reason}":
        "Kon deze release niet openen in {program}: {reason}",
    "Can't write the .torrent file to {folder}: {reason}":
        "Kan het .torrent-bestand niet opslaan in {folder}: {reason}",
    "Can't write to the download folder {folder}: {reason}":
        "Kan niet schrijven in de downloadmap {folder}: {reason}",
    "Opening torrents is only supported on Windows": "Torrents openen kan alleen op Windows",
    "{program} isn't set up yet: enter its address, user name and password in Settings.":
        "{program} is nog niet ingesteld: vul het adres, de gebruikersnaam en het wachtwoord in bij Instellingen.",
    "{program} isn't set up yet: enter its address in Settings.":
        "{program} is nog niet ingesteld: vul het adres in bij Instellingen.",
    # app/services/remote.py
    "the configured address": "het ingestelde adres",
    "{program} at {host} didn't answer in time.": "{program} op {host} antwoordde niet op tijd.",
    "Certificate not trusted: {host} has a certificate this PC doesn't trust. Untick \"{setting}\" in Settings for a self-signed certificate.":
        "Certificaat niet vertrouwd: {host} heeft een certificaat dat deze pc niet vertrouwt. Vink \"{setting}\" uit in Instellingen voor een zelfondertekend certificaat.",
    "Couldn't make a secure connection to {host}. Use an http:// address unless {program} has HTTPS turned on.":
        "Kon geen beveiligde verbinding maken met {host}. Gebruik een http://-adres, tenzij HTTPS aanstaat in {program}.",
    "Couldn't reach {host}. Check the address in Settings and that {program} is on.":
        "{host} is niet bereikbaar. Controleer het adres in Instellingen en of {program} aanstaat.",
    "The connection to {program} at {host} failed ({error}).":
        "De verbinding met {program} op {host} is mislukt ({error}).",
    "{host} redirects to {target}: use that address in Settings.":
        "{host} verwijst door naar {target}: gebruik dat adres in Instellingen.",
    "{host} answered with a redirect: check the address in Settings.":
        "{host} antwoordde met een doorverwijzing: controleer het adres in Instellingen.",
    # app/services/synology.py
    "This DSM account uses 2-step verification, which AirGrabber can't sign in with. Use a DSM account without 2-step verification.":
        "Dit DSM-account gebruikt verificatie in 2 stappen, waarmee AirGrabber niet kan aanmelden. Gebruik een DSM-account zonder verificatie in 2 stappen.",
    "The DSM account's password has expired. Sign in to DSM to change it.":
        "Het wachtwoord van het DSM-account is verlopen. Meld je aan bij DSM om het te wijzigen.",
    "The NAS doesn't offer the Download Station API AirGrabber needs.":
        "De NAS biedt de Download Station-API niet aan die AirGrabber nodig heeft.",
    "The DSM session ended unexpectedly. Try again.":
        "De DSM-sessie is onverwacht beëindigd. Probeer opnieuw.",
    "DSM rejected the user name or password.": "DSM weigert de gebruikersnaam of het wachtwoord.",
    "This DSM account is disabled.": "Dit DSM-account is uitgeschakeld.",
    "This DSM account isn't allowed to sign in (permission denied).":
        "Dit DSM-account mag niet aanmelden (geen toestemming).",
    "The NAS has blocked this computer's IP address (DSM auto block).":
        "De NAS heeft het IP-adres van deze computer geblokkeerd (DSM automatisch blokkeren).",
    "The DSM account's password must be changed. Sign in to DSM to change it.":
        "Het wachtwoord van het DSM-account moet gewijzigd worden. Meld je aan bij DSM om het te wijzigen.",
    "This DSM account isn't allowed to use Download Station.":
        "Dit DSM-account mag Download Station niet gebruiken.",
    "its default folder": "de standaardmap",
    "Download Station has reached its maximum number of tasks.":
        "Download Station heeft het maximale aantal taken bereikt.",
    "Download Station isn't allowed to save to {where}.":
        "Download Station mag niet opslaan in {where}.",
    "The Download Station destination {where} doesn't exist.":
        "De Download Station-doelmap {where} bestaat niet.",
    "Download Station has no default destination folder. Set one in Download Station or in AirGrabber's settings.":
        "Download Station heeft geen standaard doelmap. Stel er een in bij Download Station of in de instellingen van AirGrabber.",
    "Download Station couldn't save to {where}.": "Download Station kon niet opslaan in {where}.",
    "{program} on DSM {version}": "{program} op DSM {version}",
    "No Synology DSM answered at {host}. Check the NAS address in Settings.":
        "Geen Synology DSM gevonden op {host}. Controleer het NAS-adres in Instellingen.",
    "Download Station isn't installed or isn't running on the NAS at {host}.":
        "Download Station is niet geïnstalleerd of draait niet op de NAS op {host}.",
    "DSM accepted the sign-in but returned no session.":
        "DSM aanvaardde de aanmelding maar gaf geen sessie terug.",
    "Download Station reported error {code}.": "Download Station meldde fout {code}.",
    "Download Station couldn't do that with this download.":
        "Download Station kon dat niet doen met deze download.",
    "Download Station no longer has this download.": "Download Station heeft deze download niet meer.",
    "Download Station can't do that with this download right now.":
        "Download Station kan dat nu niet doen met deze download.",
    "Download Station couldn't remove this download right now.":
        "Download Station kon deze download nu niet weghalen.",
    "The NAS at {host} didn't answer in time.": "De NAS op {host} antwoordde niet op tijd.",
    "The connection to the NAS at {host} failed ({error}).":
        "De verbinding met de NAS op {host} is mislukt ({error}).",
    "The NAS at {host} answered HTTP {status}.": "De NAS op {host} antwoordde met HTTP {status}.",
    "The NAS at {host} sent a reply AirGrabber doesn't understand.":
        "De NAS op {host} stuurde een antwoord dat AirGrabber niet begrijpt.",
    "Download Station isn't set up: enter the NAS address, user name and password in Settings.":
        "Download Station is niet ingesteld: vul het NAS-adres, de gebruikersnaam en het wachtwoord in bij Instellingen.",
    "Certificate not trusted: the NAS at {host} has a certificate this PC doesn't trust. Untick \"{setting}\" in Settings for a self-signed NAS certificate.":
        "Certificaat niet vertrouwd: de NAS op {host} heeft een certificaat dat deze pc niet vertrouwt. Vink \"{setting}\" uit in Instellingen voor een zelfondertekend NAS-certificaat.",
    "Couldn't make a secure connection to the NAS at {host}. Check that the address uses https with the NAS's HTTPS port (usually 5001), or http with its HTTP port.":
        "Kon geen beveiligde verbinding maken met de NAS op {host}. Controleer of het adres https gebruikt met de HTTPS-poort van de NAS (meestal 5001), of http met de HTTP-poort.",
    "Can't reach the NAS at {host}. Check the address in Settings and that the NAS is on.":
        "De NAS op {host} is niet bereikbaar. Controleer het adres in Instellingen en of de NAS aanstaat.",
    "The NAS at {host} redirects to {target}: use that address in Settings.":
        "De NAS op {host} verwijst door naar {target}: gebruik dat adres in Instellingen.",
    "The NAS at {host} answered with a redirect: check the address in Settings.":
        "De NAS op {host} antwoordde met een doorverwijzing: controleer het adres in Instellingen.",
    # app/services/qbittorrent.py
    "qBittorrent isn't set up: enter its address in Settings.":
        "qBittorrent is niet ingesteld: vul het adres in bij Instellingen.",
    "qBittorrent: wrong user name or password.":
        "qBittorrent: verkeerde gebruikersnaam of wachtwoord.",
    "qBittorrent didn't add the torrent. It may have it already, or the save folder may not be usable.":
        "qBittorrent heeft de torrent niet toegevoegd. Misschien heeft het hem al, of is de opslagmap niet bruikbaar.",
    "qBittorrent has blocked this computer after too many failed sign-ins. Try again later, or restart qBittorrent.":
        "qBittorrent heeft deze computer geblokkeerd na te veel mislukte aanmeldingen. Probeer later opnieuw, of herstart qBittorrent.",
    "qBittorrent wants a user name and password: enter those of its Web UI in Settings.":
        "qBittorrent vraagt een gebruikersnaam en wachtwoord: vul die van de Web-UI in bij Instellingen.",
    "qBittorrent says this .torrent file isn't valid.":
        "Volgens qBittorrent is dit .torrent-bestand ongeldig.",
    "qBittorrent didn't add the torrent. It may have it already.":
        "qBittorrent heeft de torrent niet toegevoegd. Misschien heeft het hem al.",
    "No qBittorrent Web UI answered at {host}. Check the address and that the Web UI is on (Tools → Options → Web UI).":
        "Geen qBittorrent-Web-UI gevonden op {host}. Controleer het adres en of de Web-UI aanstaat (Extra → Opties → Web-UI).",
    "qBittorrent at {host} answered HTTP {status}.":
        "qBittorrent op {host} antwoordde met HTTP {status}.",
    "qBittorrent at {host} sent a reply AirGrabber doesn't understand.":
        "qBittorrent op {host} stuurde een antwoord dat AirGrabber niet begrijpt.",
    # app/services/transmission.py
    "Transmission isn't set up: enter its address in Settings.":
        "Transmission is niet ingesteld: vul het adres in bij Instellingen.",
    "Transmission refused it: {reason}.": "Transmission weigerde het: {reason}.",
    "Transmission: wrong user name or password.":
        "Transmission: verkeerde gebruikersnaam of wachtwoord.",
    "Transmission wants a user name and password: enter them in Settings.":
        "Transmission vraagt een gebruikersnaam en wachtwoord: vul ze in bij Instellingen.",
    "Transmission refused this computer: add its address to Transmission's RPC whitelist, or turn the whitelist off.":
        "Transmission weigert deze computer: zet het adres ervan op de RPC-whitelist van Transmission, of zet de whitelist uit.",
    "No Transmission answered at {host}. Check the address and that remote access (RPC) is on.":
        "Geen Transmission gevonden op {host}. Controleer het adres en of toegang op afstand (RPC) aanstaat.",
    "Transmission at {host} answered HTTP {status}.":
        "Transmission op {host} antwoordde met HTTP {status}.",
    "Transmission at {host} sent a reply AirGrabber doesn't understand.":
        "Transmission op {host} stuurde een antwoord dat AirGrabber niet begrijpt.",
    # app/services/remote_downloads.py
    "AirGrabber can't read the downloads of {program}.":
        "AirGrabber kan de downloads van {program} niet lezen.",
    "AirGrabber can't change the downloads of {program}.":
        "AirGrabber kan de downloads van {program} niet aanpassen.",
    # app/services/tmdb.py
    "TMDB {path}: request failed ({error})": "TMDB {path}: verzoek mislukt ({error})",
    "TMDB API key is not set": "Er is geen TMDB-API-sleutel ingesteld",
    "TMDB API key contains invalid characters": "De TMDB-API-sleutel bevat ongeldige tekens",
    "TMDB rejected the API key (HTTP 401 on {path})":
        "TMDB weigert de API-sleutel (HTTP 401 op {path})",
    "TMDB {path}: not found (HTTP 404)": "TMDB {path}: niet gevonden (HTTP 404)",
    "TMDB {path}: {reason} (gave up after {n} attempts)":
        "TMDB {path}: {reason} (opgegeven na {n} pogingen)",
    "TMDB {path}: response is not valid JSON": "TMDB {path}: het antwoord is geen geldige JSON",
    "TMDB {path}: unexpected payload ({problem})": "TMDB {path}: onverwacht antwoord ({problem})",
    "TMDB {path}: expected an object with a 'results' list":
        "TMDB {path}: verwachtte een object met een lijst 'results'",
    # app/services/tvmaze.py
    "TVMaze {path}: request failed ({error})": "TVMaze {path}: verzoek mislukt ({error})",
    "TVMaze {path}: not found (HTTP 404)": "TVMaze {path}: niet gevonden (HTTP 404)",
    "TVMaze {path}: {reason} (gave up after {n} attempts)":
        "TVMaze {path}: {reason} (opgegeven na {n} pogingen)",
    "TVMaze {path}: response is not valid JSON": "TVMaze {path}: het antwoord is geen geldige JSON",
    "TVMaze {path}: unexpected payload ({problem})":
        "TVMaze {path}: onverwacht antwoord ({problem})",
    "TVMaze {path}: expected a JSON list, got {kind}":
        "TVMaze {path}: verwachtte een JSON-lijst, kreeg {kind}",
    # app/services/updates.py
    "Could not reach GitHub.": "GitHub is niet bereikbaar.",
    "GitHub doesn't have an AirGrabber 2 version yet.":
        "GitHub heeft nog geen versie 2 van AirGrabber.",
    "GitHub answered HTTP {status}.": "GitHub antwoordde met HTTP {status}.",
    "This is a development copy; it is never overwritten by an update.":
        "Dit is een ontwikkelkopie; die wordt nooit overschreven door een update.",
    "Downloading update…": "Update downloaden…",
    "Installing update…": "Update installeren…",
    "Could not unpack the update.": "Kon de update niet uitpakken.",
    "Download failed: GitHub answered HTTP {status}.":
        "Downloaden mislukt: GitHub antwoordde met HTTP {status}.",
    "Download failed: the archive is unexpectedly large.":
        "Downloaden mislukt: het archief is onverwacht groot.",
    "Download failed: could not reach GitHub.": "Downloaden mislukt: GitHub is niet bereikbaar.",
    "The download is version {version}, which is not newer than {current}.":
        "De download is versie {version}, die niet nieuwer is dan {current}.",
    "Could not clear the folder {folder}; delete it and try again.":
        "Kon de map {folder} niet leegmaken; verwijder hem en probeer opnieuw.",
    "The download is not a valid archive.": "De download is geen geldig archief.",
    "The download has no {entry}; nothing was changed.":
        "De download bevat geen {entry}; er is niets veranderd.",
    "The download has no version number; nothing was changed.":
        "De download heeft geen versienummer; er is niets veranderd.",
    "The download has an unexpected layout; nothing was changed.":
        "De download is onverwacht opgebouwd; er is niets veranderd.",
    "The download contains unsafe paths; nothing was changed.":
        "De download bevat onveilige paden; er is niets veranderd.",
    "The update failed half-way and could not be undone. Reinstall AirGrabber with the install command.":
        "De update liep halverwege mis en kon niet ongedaan gemaakt worden. Installeer AirGrabber opnieuw met het installatiecommando.",
    "Could not replace the app files (is another program using them?). Nothing was changed.":
        "Kon de app-bestanden niet vervangen (gebruikt een ander programma ze?). Er is niets veranderd.",
    # app/services/startup.py
    "AirGrabber could not be removed from Windows startup.":
        "AirGrabber kon niet uit het opstarten van Windows gehaald worden.",
    "AirGrabber could not be added to Windows startup.":
        "AirGrabber kon niet toegevoegd worden aan het opstarten van Windows.",
    # app/services/download_status.py
    "{show} Season {season}": "{show} seizoen {season}",
    # app/services/prowlarr_setup.py
    "Setting up Prowlarr…": "Prowlarr instellen…",
    # app/services/torrent_app.py
    "Installing qBittorrent…": "qBittorrent installeren…",
    "Checking the installation…": "De installatie controleren…",
    # app/services/qbittorrent_local.py
    "qBittorrent isn't running, and AirGrabber couldn't start it. Start qBittorrent, then try again.":
        "qBittorrent draait niet en AirGrabber kon het niet starten. Start qBittorrent en probeer het dan opnieuw.",
    "qBittorrent didn't start in time. Try again in a moment.":
        "qBittorrent startte niet op tijd. Probeer het zo meteen opnieuw.",
    "qBittorrent is waiting for you: accept its notice in its window, then try again.":
        "qBittorrent wacht op je: accepteer de melding in het venster van qBittorrent en probeer het dan opnieuw.",
    # app/services/quick_download.py
    "Grabbing the best one…": "De beste downloaden…",
    ", {n} source still searching": ", {n} bron zoekt nog",
    ", {n} sources still searching": ", {n} bronnen zoeken nog",
    # app/services/airdates.py
    "That doesn't look like the text from airdates.tv's \"Export your color selection\".":
        "Dat lijkt niet op de tekst van \"Export your color selection\" op airdates.tv.",
    "airdates.tv lookup failed ({error})": "Opzoeken op airdates.tv mislukt ({error})",
    "airdates.tv answered {reason}": "airdates.tv antwoordde met {reason}",
    "airdates.tv could not be reached ({reason})": "airdates.tv is niet bereikbaar ({reason})",
    # app/services/search.py
    "not available for this search": "niet beschikbaar voor deze zoekopdracht",
    "disabled in Settings": "uitgezet in Instellingen",
    "movies only": "alleen films",
    "TV only": "alleen series",
    "not configured": "niet ingesteld",
    "needs an IMDb id": "heeft een IMDb-ID nodig",
    "timed out": "duurde te lang",
    "unexpected error": "onverwachte fout",
    "Prowlarr is not available": "Prowlarr is niet beschikbaar",
    # app/indexers/base.py
    "{label}: request to {path} failed ({error})": "{label}: verzoek naar {path} mislukt ({error})",
    "no response from {path} ({error})": "geen antwoord van {path} ({error})",
    "{path} answered HTTP {status}": "{path} antwoordde met HTTP {status}",
    "{n} attempt": "{n} poging",
    "{n} attempts": "{n} pogingen",
    "{label}: {reason}; gave up after {attempts}": "{label}: {reason}; opgegeven na {attempts}",
    "{label}: {path} answered HTTP {status}": "{label}: {path} antwoordde met HTTP {status}",
    "{label}: {path} did not answer with valid JSON": "{label}: {path} gaf geen geldige JSON terug",
    "{label}: unexpected answer from {path} ({detail})":
        "{label}: onverwacht antwoord van {path} ({detail})",
    # app/indexers/prowlarr.py
    "Prowlarr: the URL answered, but not like Prowlarr does; check the URL":
        "Prowlarr: de URL antwoordt, maar niet zoals Prowlarr; controleer de URL",
    "Prowlarr is not set up: enter its URL and API key in Settings":
        "Prowlarr is niet ingesteld: vul de URL en API-sleutel in bij Instellingen",
    "Prowlarr API key contains invalid characters":
        "De Prowlarr-API-sleutel bevat ongeldige tekens",
    "Prowlarr rejected the API key": "Prowlarr weigert de API-sleutel",
    "Prowlarr: {path} not found (HTTP 404); check the URL":
        "Prowlarr: {path} niet gevonden (HTTP 404); controleer de URL",
    # app/services/backup.py (Backup & move)
    "AirGrabber backup {date}": "AirGrabber-back-up {date}",
    "That file isn't an AirGrabber backup.": "Dat bestand is geen back-up van AirGrabber.",
    "This backup is damaged and can't be loaded.":
        "Deze back-up is beschadigd en kan niet geladen worden.",
    "This backup was made by a newer AirGrabber ({version}). Update AirGrabber, then load it again.":
        "Deze back-up is gemaakt met een nieuwere AirGrabber ({version}). Werk AirGrabber bij en laad hem dan opnieuw.",
    "This backup was made by a newer AirGrabber. Update AirGrabber, then load it again.":
        "Deze back-up is gemaakt met een nieuwere AirGrabber. Werk AirGrabber bij en laad hem dan opnieuw.",
    "Couldn't read that file.": "Kon dat bestand niet lezen.",
    "Couldn't save the backup there. Choose another folder.":
        "Kon de back-up daar niet opslaan. Kies een andere map.",
    # app/ui/plex_settings.py, app/ui/plex_connect.py and Settings' Plex page
    "Plex": "Plex",
    "What's in your Plex libraries counts as something you already have: it isn't missing and isn't downloaded again.":
        "Wat in je Plex-bibliotheken staat, telt als iets wat je al hebt: het ontbreekt niet en wordt niet opnieuw gedownload.",
    "Let AirGrabber see what's in your Plex libraries, so it never downloads something you already have. It only reads; your Plex password isn't needed.":
        "Laat AirGrabber zien wat er in je Plex-bibliotheken staat, zodat het nooit iets downloadt wat je al hebt. Het leest alleen; je Plex-wachtwoord is niet nodig.",
    "Connect to Plex…": "Verbinden met Plex…",
    "↻ Check now": "↻ Nu controleren",
    "Disconnect": "Verbinding verbreken",
    "Libraries:": "Bibliotheken:",
    "Server:": "Server:",
    "Not checked yet": "Nog niet gecontroleerd",
    "No show or film libraries found on this server":
        "Geen serie- of filmbibliotheken gevonden op deze server",
    "your Plex server": "je Plex-server",
    "Connected to {server}": "Verbonden met {server}",
    "Plex didn't answer": "Plex reageerde niet",
    "Plex asks to connect again": "Plex vraagt om opnieuw te verbinden",
    "Your Plex account has no server AirGrabber can read":
        "Je Plex-account heeft geen server die AirGrabber kan lezen",
    "Choose which Plex server AirGrabber reads": "Kies welke Plex-server AirGrabber leest",
    "Plex's answer didn't read": "Het antwoord van Plex was onleesbaar",
    "{reason}: the last list from {when} is used": "{reason}: de laatste lijst van {when} wordt gebruikt",
    "{episodes} of {series} · {films} · checked {when}":
        "{episodes} van {series} · {films} · laatst gekeken: {when}",
    "{n} of your series": "{n} van je series",
    "Disconnect from Plex?": "Verbinding met Plex verbreken?",
    "AirGrabber forgets its Plex sign-in and what it read from Plex. Nothing changes in Plex itself.":
        "AirGrabber vergeet de aanmelding bij Plex en wat het uit Plex las. In Plex zelf verandert niets.",
    "Connect to Plex": "Verbinden met Plex",
    "Go to plex.tv/link, sign in if asked, and enter this code:":
        "Ga naar plex.tv/link, meld je aan als daarom gevraagd wordt en vul deze code in:",
    "Open plex.tv/link": "plex.tv/link openen",
    "Getting a code from Plex…": "Code ophalen bij Plex…",
    "⟳ Waiting for you to enter the code…": "⟳ Wachten tot je de code invult…",
    "This code has expired.": "Deze code is verlopen.",
    "Get a new code": "Nieuwe code ophalen",
    "Plex didn't answer. Check the internet connection and try again.":
        "Plex reageerde niet. Controleer de internetverbinding en probeer het opnieuw.",
    # app/ui/backup_dialog.py
    "Save a backup": "Back-up opslaan",
    "Your followed shows, film watchlist, download history and settings go into one file. Load it on another PC in Settings → Backup.":
        "Je gevolgde series, je kijklijst, je downloadgeschiedenis en je instellingen komen in één bestand. Laad het op een andere pc via Instellingen → Back-up.",
    "Include keys and passwords": "Sleutels en wachtwoorden meenemen",
    "Anyone with the file can then use them. Keep it somewhere safe.":
        "Iedereen met het bestand kan ze dan gebruiken. Bewaar het op een veilige plek.",
    "Save…": "Opslaan…",
    "Saving…": "Bezig met opslaan…",
    "Backup saved": "Back-up opgeslagen",
    "The keys and passwords are in it: keep it somewhere safe.":
        "De sleutels en wachtwoorden zitten erin: bewaar het op een veilige plek.",
    "Load a backup": "Back-up laden",
    "Reading the backup…": "Back-up lezen…",
    "Can't load this file": "Kan dit bestand niet laden",
    "Nothing was changed.": "Er is niets veranderd.",
    "Loading the backup didn't work.": "Het laden van de back-up is niet gelukt.",
    "Load this backup?": "Deze back-up laden?",
    "Replace": "Vervangen",
    "Your shows, watchlist and settings become the backup's. Keys and passwords it doesn't have stay as they are.":
        "Je series, kijklijst en instellingen worden die van de back-up. Sleutels en wachtwoorden die er niet in zitten, blijven zoals ze zijn.",
    "Add to what I have": "Toevoegen aan wat ik heb",
    "The backup's shows and films join yours; where you have one already, yours stays. Your settings don't change.":
        "De series en films uit de back-up komen bij de jouwe; wat je al hebt, blijft zoals het is. Je instellingen veranderen niet.",
    "Loading the backup…": "Bezig met laden…",
    "Backup loaded": "Back-up geladen",
    "Your settings are now the backup's.": "Je instellingen zijn nu die van de back-up.",
    "Your downloads keep using qBittorrent on this PC.":
        "Je downloads blijven naar qBittorrent op deze pc gaan.",
    "Where downloads go stays as it was on this PC: the backup's qBittorrent was on the other PC.":
        "Waar downloads naartoe gaan, blijft zoals het op deze pc was: de qBittorrent uit de back-up stond op de andere pc.",
    "Check your folders in Settings: they may be different on this PC.":
        "Kijk je mappen na in Instellingen: die kunnen op deze pc anders zijn.",
    "Keys and passwords weren't in the backup. Fill in any you need in Settings.":
        "Er zaten geen sleutels en wachtwoorden in de back-up. Vul in Instellingen in wat je nodig hebt.",
    "AirGrabber backup": "AirGrabber-back-up",
    "All files": "Alle bestanden",
    "{n} show": "{n} serie",
    "{n} shows": "{n} series",
    "{n} film on the watchlist": "{n} film op de kijklijst",
    "{n} films on the watchlist": "{n} films op de kijklijst",
    "settings": "instellingen",
    "Saved {date} by AirGrabber {version}": "Opgeslagen op {date} met AirGrabber {version}",
    "Saved {date}": "Opgeslagen op {date}",
    "{n} download in the history": "{n} download in de geschiedenis",
    "{n} downloads in the history": "{n} downloads in de geschiedenis",
    "with keys and passwords": "met sleutels en wachtwoorden",
    "without keys and passwords": "zonder sleutels en wachtwoorden",
    "Added: {shows}, {films}.": "Toegevoegd: {shows}, {films}.",
    "Removed, as they aren't in the backup: {shows}, {films}.":
        "Verwijderd, want niet in de back-up: {shows}, {films}.",
    "{n} download added to the history.": "{n} download toegevoegd aan de geschiedenis.",
    "{n} downloads added to the history.": "{n} downloads toegevoegd aan de geschiedenis.",
    # app/ui/settings_dialog.py (Backup)
    "Backup": "Back-up",
    "Keep a copy of your shows, watchlist and settings, or move them to another PC.":
        "Bewaar een kopie van je series, kijklijst en instellingen, of zet ze over naar een andere pc.",
    "One file with your followed shows, film watchlist, download history and settings. Load it on this PC or another one: you see what's in it before anything changes.":
        "Eén bestand met je gevolgde series, je kijklijst, je downloadgeschiedenis en je instellingen. Laad het op deze pc of een andere: je ziet eerst wat erin zit, voor er iets verandert.",
    "Save a backup…": "Back-up opslaan…",
    "Load a backup…": "Back-up laden…",
    # app/ui/welcome_dialog.py (Backup)
    "Moving from another PC? Load a backup": "Kom je van een andere pc? Laad een back-up",
    # app/services/profiles.py, app/ui/people.py, app/ui/people_dialogs.py (people)
    "Type a name": "Typ een naam",
    "Use at most {count} characters": "Gebruik hoogstens {count} tekens",
    "Someone is called {name} already": "Er heet al iemand {name}",
    "That person isn't there any more": "Die persoon is er niet meer",
    "The last person can't be removed": "De laatste persoon kan niet weg",
    "Me": "Ik",
    "＋ Add a person…": "＋ Persoon toevoegen…",
    "✎ Rename or change colour…": "✎ Naam of kleur wijzigen…",
    "🗑 Remove a person…": "🗑 Persoon verwijderen…",
    "Now showing {name}'s list": "Je ziet nu de lijst van {name}",
    "Add a person": "Persoon toevoegen",
    "They start with an empty list. Downloads and settings are shared.":
        "Deze persoon begint met een lege lijst. Downloads en instellingen worden gedeeld.",
    "Rename or change colour": "Naam of kleur wijzigen",
    "Remove a person": "Persoon verwijderen",
    "Who do you want to remove?": "Wie wil je verwijderen?",
    "Remove {name}? Their followed series and watchlist are removed. Downloads stay.":
        "{name} verwijderen? De gevolgde series en de kijklijst gaan weg. Downloads blijven.",
    "Colour": "Kleur",
    # ("Add" and "Remove": Forge's catalog has them, app/i18n_nl_forge.py.)
    # app/ui/welcome_dialog.py (people)
    "Who uses AirGrabber?": "Wie gebruikt AirGrabber?",
    "Each person gets their own followed series, watchlist and calendar. Downloads and settings are shared.":
        "Iedereen krijgt eigen gevolgde series, een eigen kijklijst en kalender. Downloads en instellingen worden gedeeld.",
    "Your name": "Je naam",
    "＋ Add another person": "＋ Nog iemand toevoegen",
    # app/whats_new.py
    "MediaForge is now a page in AirGrabber": "MediaForge is nu een pagina in AirGrabber",
    "No second window any more: Series, Movies, MediaForge. The number on it shows how many files need you.":
        "Geen tweede venster meer: Series, Films, MediaForge. Het getal erop laat zien hoeveel bestanden op je wachten.",
    "Every show or film is one card": "Elke serie of film is één kaart",
    "Its picture runs behind all its files, and 📂 opens them in Explorer.":
        "De afbeelding loopt door achter alle bestanden, en 📂 opent ze in Verkenner.",
    "History is behind the 🕘 next to Settings.": "Geschiedenis zit achter de 🕘 naast Instellingen.",
    "MediaForge now has AirGrabber's colours.": "MediaForge heeft nu de kleuren van AirGrabber.",
    "Auto-rename moved to Settings, and there is now one Settings window for everything.":
        "Automatisch hernoemen staat nu in Instellingen, en er is één venster met alle instellingen.",
    "Subtitles are part of MediaForge now, and prefer the release of your file's original name.":
        "Ondertitels horen nu bij MediaForge en passen beter: bij de release van de oorspronkelijke bestandsnaam.",
    "New setting: remove files from the queue after they're forged.":
        "Nieuwe instelling: bestanden na het forgen uit de wachtrij halen.",
    "Ignored files and folders that are gone drop off the Ignored list by themselves.":
        "Genegeerde bestanden en mappen die weg zijn, verdwijnen vanzelf uit de lijst Genegeerd.",
    "The show window always has a season's download buttons, also when it's already in Plex.":
        "Het serievenster heeft altijd de downloadknoppen van een seizoen, ook als het al in Plex staat.",
    "MediaForge's format codes are now buttons: click one to add it to the format.":
        "De codes voor de opmaak in MediaForge zijn nu knoppen: klik erop om ze aan de opmaak toe te voegen.",
    "MediaForge's Ignored list shows just the names; hover over one for its whole path.":
        "De lijst Genegeerd in MediaForge toont alleen de namen; wijs er een aan voor het hele pad.",
    "New here? \"Let's get started\" lets you pick a few current series to follow in one go.":
        "Nieuw hier? Met \"Aan de slag\" kies je in één keer een paar actuele series om te volgen.",
    "It opens from the empty calendar or from My shows (+ Pick shows…).":
        "Je opent het vanaf de lege kalender of vanuit Mijn series (+ Series kiezen…).",
    "MediaForge's \"Which show is this?\" has a search box for when none of its suggestions is right.":
        "MediaForge's \"Welke serie is dit?\" heeft een zoekvak voor als geen van de voorstellen klopt.",
    "Connect Plex in Settings → Plex: what's in your Plex libraries counts as something you already have.":
        "Koppel Plex via Instellingen → Plex: wat in je Plex-bibliotheken staat, telt als iets wat je al hebt.",
    "Episodes in Plex are no longer missing, and automatic downloads skip episodes and films you have in Plex.":
        "Afleveringen in Plex ontbreken niet meer, en automatische downloads slaan afleveringen en films in Plex over.",
    "A quiet ✓ \"In Plex\" shows on those episodes and films.":
        "Een rustig ✓ \"In Plex\" staat bij die afleveringen en films.",
    "Without Plex, AirGrabber no longer marks what you have or what you're missing.":
        "Zonder Plex markeert AirGrabber niet meer wat je hebt of wat je mist.",
    "The Downloads page now shows everything in your torrent program, also what you added there yourself.":
        "De Downloads-pagina toont nu alles uit je torrentprogramma, ook wat je daar zelf hebt toegevoegd.",
    "Pause, resume or remove a download from its ⋯ menu; AirGrabber never deletes your files.":
        "Pauzeer, hervat of verwijder een download via het ⋯-menu; AirGrabber verwijdert nooit je bestanden.",
    "A damaged or wrong .torrent file is no longer sent to your torrent program.":
        "Een beschadigd of verkeerd .torrent-bestand gaat niet meer naar je torrentprogramma.",
    "Search again right in the search results window, and click the logo to go back to the calendar.":
        "Zoek opnieuw in het venster met zoekresultaten, en klik op het logo om terug te gaan naar de kalender.",
    "A calmer calendar: no suggestions in past weeks, and \"+ Track\" no longer reloads the page.":
        "Een rustigere kalender: geen suggesties in voorbije weken, en \"+ Volgen\" laadt de pagina niet meer opnieuw.",
    "MediaForge's Ignore menu stays open when you click it.":
        "Het Negeren-menu van MediaForge blijft nu open als je erop klikt.",
    "Everyone at home can have their own series, watchlist and calendar: click your name at the top right.":
        "Iedereen thuis kan eigen series, een eigen kijklijst en kalender hebben: klik rechtsboven op je naam.",
    "Downloads now wait in a queue: click a whole season and close the window; the Downloads page shows what's waiting.":
        "Downloads wachten nu in een wachtrij: klik een heel seizoen aan en sluit het venster, de Downloads-pagina toont wat er wacht.",
    "Each season has a \"Whole season\" button that looks for a season pack first.":
        "Elk seizoen heeft een knop \"Hele seizoen\" die eerst een seizoenspakket zoekt.",
    "MediaForge has its own Settings (⚙), and every item in its queue can have its own preset.":
        "MediaForge heeft eigen instellingen (⚙), en elk item in de wachtrij kan een eigen voorinstelling krijgen.",
    "Ignore keeps a file or folder out of MediaForge's Auto-rename, which now only renames videos whose name matches.":
        "Met Negeren blijft een bestand of map buiten automatisch hernoemen in MediaForge, dat nu alleen video's met een kloppende naam hernoemt.",
    "AirGrabber opens on the calendar, and a series opened from search comes to the front.":
        "AirGrabber opent op de kalender, en een serie die je uit de zoekresultaten opent komt naar voren.",
    "MediaForge is now part of AirGrabber: open it with the 🔨 button, from the tray or from the Start menu.":
        "MediaForge zit nu in AirGrabber: open het met de 🔨-knop, vanuit het systeemvak of vanuit het Startmenu.",
    "Your old MediaForge's presets, folders and history come along the first time you open it.":
        "De voorinstellingen, mappen en geschiedenis van je oude MediaForge komen mee als je het de eerste keer opent.",
    "Right-click files in Explorer to rename them, and let MediaForge rename AirGrabber's downloads by itself.":
        "Klik met rechts op bestanden in Verkenner om ze te hernoemen, en laat MediaForge AirGrabbers downloads zelf hernoemen.",
    "The Downloads page shows where a download ended up, and its subtitles move along with it.":
        "De pagina Downloads laat zien waar een download is beland, en de ondertitels verhuizen mee.",
    "Trailers now play in their own window for more videos, with a Fill button against black bars.":
        "Trailers spelen nu vaker in hun eigen venster, met een knop Vullen tegen zwarte balken.",
    "The Series / Movies switch is bigger and moved down, next to Calendar, Discover and Tracked.":
        "De schakelaar Series / Films is groter en staat nu lager, naast Kalender, Ontdekken en Mijn series.",
    "The top bar keeps just the logo and the search, so it looks calmer.":
        "De bovenbalk houdt alleen het logo en het zoekveld over, zodat hij rustiger oogt.",
    "The switch stays in the same place on every page, so nothing jumps when you switch.":
        "De schakelaar staat op elke pagina op dezelfde plek, zodat er niets verspringt als je wisselt.",
    "Save a backup of your shows, watchlist and settings, and load it on a new PC (Settings → Backup).":
        "Bewaar een back-up van je series, kijklijst en instellingen en laad die op een nieuwe pc (Instellingen → Back-up).",
    "Films can go to their own folder: fill in Films folder in Settings → Where downloads go.":
        "Films kunnen naar een eigen map: vul Map voor films in bij Instellingen → Waar downloads naartoe gaan.",
    "The releases list marks the one ⬇ Auto would pick with ★ and shows how fast each will download.":
        "De lijst met releases markeert met ★ welke ⬇ Auto zou kiezen en toont hoe snel elke release downloadt.",
    "Trailers now play in their own AirGrabber window instead of the browser.":
        "Trailers spelen nu af in een eigen AirGrabber-venster in plaats van in de browser.",
    "A clearer top bar and info screen: bigger poster, calmer episode marks, softer fades.":
        "Een duidelijkere bovenbalk en infoscherm: grotere poster, rustigere afleveringslabels, zachtere overgangen.",
    "Saturdays in the calendar get suggestions too.":
        "Ook de zaterdagen in de kalender krijgen nu suggesties.",
    "A new top bar with a spotlight on tonight's episodes or new films; click it to open the title.":
        "Een nieuwe bovenbalk met een spotlight op de afleveringen van vanavond of nieuwe films; klik erop om de titel te openen.",
    "The info screen and the releases are now one window: pick a release right beside the info.":
        "Het infoscherm en de releases zijn nu één venster: kies een release direct naast de info.",
    "Choose how the info screen looks in Settings → Looks: side by side or with tabs.":
        "Kies in Instellingen → Uiterlijk hoe het infoscherm eruitziet: naast elkaar of met tabbladen.",
    "Pick one of ten accent colours in Settings → Looks or during setup.":
        "Kies een van tien accentkleuren in Instellingen → Uiterlijk of tijdens het instellen.",
    "Pop-up windows lost their title bar: drag them anywhere and close them with the round ✕.":
        "Pop-upvensters hebben geen titelbalk meer: sleep ze waarheen je wilt en sluit ze met het ronde ✕.",
    "The calendar fills each week with suggestions up to the height of its busiest day.":
        "De kalender vult elke week met suggesties tot de hoogte van de drukste dag.",
    "After installing qBittorrent, AirGrabber now sets Windows to open downloads with it, so you don't have to.":
        "Na het installeren van qBittorrent laat AirGrabber Windows downloads er nu zelf mee openen.",
    "AirGrabber never does this when you already use another torrent program.":
        "AirGrabber doet dit nooit als je al een ander torrentprogramma gebruikt.",
    "\"Clear finished\" also clears downloads your torrent program no longer has.":
        "\"Voltooide wissen\" wist ook downloads die je torrentprogramma niet meer heeft.",
    "An episode now counts as missing one day after it airs; until then it's marked New.":
        "Een aflevering telt nu pas een dag na uitzending als ontbrekend; tot dan staat er Nieuw.",
    "Quieter download buttons: ⬇ Auto picks the best release, ☰ Choose… lets you pick, ↻ Again downloads it again.":
        "Rustigere downloadknoppen: ⬇ Auto kiest de beste release, ☰ Kiezen… laat je zelf kiezen, ↻ Opnieuw downloadt nog eens.",
    "Get subtitles yourself with CC ▾, and choose when and which kind in Settings → Subtitles.":
        "Haal zelf ondertitels op met CC ▾, en kies wanneer en welke soort in Instellingen → Ondertitels.",
    "The Downloads page can filter, clear finished downloads and open a download's folder.":
        "De pagina Downloads kan filteren, voltooide downloads wissen en de map van een download openen.",
    "Installing qBittorrent and Prowlarr works when the Microsoft Store can't be reached, or says why it didn't.":
        "qBittorrent en Prowlarr installeren lukt ook als de Microsoft Store onbereikbaar is, of AirGrabber zegt waarom niet.",
    "The airdates.tv import finds more shows and names any it couldn't add; right-click a text box to paste.":
        "De import van airdates.tv vindt meer series en noemt de series die niet lukten; klik met rechts in een tekstvak om te plakken.",
    "New installs come with qBittorrent: downloads start without a pop-up, and the Downloads page shows their progress.":
        "Nieuwe installaties krijgen qBittorrent erbij: downloads starten zonder pop-up en de pagina Downloads toont hoe ver ze zijn.",
    "Already use qBittorrent? Set it up for AirGrabber in Settings → Where downloads go.":
        "Gebruik je qBittorrent al? Stel het in voor AirGrabber via Instellingen → Waar downloads naartoe gaan.",
    "qBittorrent gets AirGrabber's look (light or dark like Windows) when set up; switch it in Settings → Where downloads go.":
        "qBittorrent krijgt het uiterlijk van AirGrabber (licht of donker zoals Windows) als AirGrabber het instelt; aan of uit via Instellingen → Waar downloads naartoe gaan.",
    "Click a series or film for its new info screen: a big picture, the trailer, the cast and the full story.":
        "Klik op een serie of film voor het nieuwe infoscherm: een grote foto, de trailer, de cast en het hele verhaal.",
    "Settings → Looks shows a real card at the size you pick.":
        "Instellingen → Uiterlijk toont een echte kaart op de grootte die je kiest.",
    "Quick setup chooses the look, download quality, card size and language in one go (Settings → Looks).":
        "Snel instellen kiest het uiterlijk, de downloadkwaliteit, de kaartgrootte en de taal in één keer (Instellingen → Uiterlijk).",
    "My shows can be sorted (A–Z, next episode, latest download) and filtered (missing episodes, Auto on, ended).":
        "Mijn series kun je sorteren (A–Z, volgende aflevering, laatste download) en filteren (ontbrekende afleveringen, Auto aan, afgelopen).",
    "A new Downloads page shows how far along your downloads are on your NAS.":
        "Een nieuwe pagina Downloads toont hoe ver je downloads op je NAS zijn.",
    "The calendar marks the episodes you still need, and shows several episodes of one show on the same day as one card.":
        "De kalender markeert de afleveringen die je nog nodig hebt, en toont meerdere afleveringen van een serie op dezelfde dag als één kaart.",
    "Filter Movies and Discover by genre.": "Filter Films en Ontdekken op genre.",
    "AirGrabber now speaks Dutch too, and there's a light theme.":
        "AirGrabber spreekt nu ook Nederlands, en er is een licht thema.",
    "Shortcuts: Ctrl+F to search, F5 to refresh the page, Esc to close a window.":
        "Sneltoetsen: Ctrl+F om te zoeken, F5 om de pagina te vernieuwen, Esc om een venster te sluiten.",
    "What's new in {app} {version}": "Nieuw in {app} {version}",
    # app/ui/get_started.py, the empty calendar's box, My shows' "+ Pick shows…"
    "Let's get started": "Aan de slag",
    "Click the series you watch. Their new episodes show up on your calendar.":
        "Klik de series aan die je kijkt. Hun nieuwe afleveringen verschijnen in je kalender.",
    "Looking for another show? Type its name…": "Zoek je een andere serie? Typ de naam…",
    "Loading…": "Laden…",
    "Couldn't load this list.": "Kon deze lijst niet laden.",
    "No shows in this list.": "Geen series in deze lijst.",
    "Nothing chosen yet": "Nog niets gekozen",
    "Following…": "Bezig met volgen…",
    "Popular now": "Nu populair",
    "On the air": "Nu op tv",
    "Coming soon": "Binnenkort",
    "{n} show chosen": "{n} serie gekozen",
    "{n} shows chosen": "{n} series gekozen",
    "Follow {n} show": "{n} serie volgen",
    "Follow {n} shows": "{n} series volgen",
    "Couldn't follow: {names}": "Kon niet volgen: {names}",
    'Results for "{query}"': 'Resultaten voor "{query}"',
    "You don't follow any shows yet": "Je volgt nog geen series",
    "Pick a few series and their new episodes appear here.":
        "Kies een paar series en hun nieuwe afleveringen verschijnen hier.",
    "Pick shows to follow…": "Kies series om te volgen…",
    "+ Pick shows…": "+ Series kiezen…",
}
