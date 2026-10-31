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
    "⚠ Subtitles: {message}": "⚠ Ondertitels: {message}",
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
    "Subtitles": "Ondertitels",
    "Movie & show info": "Film- en serie-info",
    "Looks": "Uiterlijk",
    "Updates": "Updates",
    "Advanced": "Geavanceerd",
    "Downloading": "Downloaden",
    "App": "App",
    "What happens when you click Download.": "Wat er gebeurt als je op Downloaden klikt.",
    "Which copy AirGrabber picks when you click Download, and for automatic downloads.":
        "Welke versie AirGrabber kiest als je op Downloaden klikt, en bij automatische downloads.",
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
    "AirGrabber finds subtitles for what you download on OpenSubtitles.com.":
        "AirGrabber zoekt ondertitels voor wat je downloadt op OpenSubtitles.com.",
    "Languages": "Talen",
    "AirGrabber looks for subtitles in each of these languages.":
        "AirGrabber zoekt ondertitels in elk van deze talen.",
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
    "Subtitle languages": "Ondertiteltalen",
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
    # app/ui/subtitle_settings.py
    "Saved next to your videos, so your player shows them.":
        "Opgeslagen naast je video's, zodat je speler ze toont.",
    "Subtitles aren't available in this version yet":
        "Ondertitels zijn nog niet beschikbaar in deze versie",
    "Subtitles provided by OpenSubtitles.com": "Ondertitels aangeleverd door OpenSubtitles.com",
    "Get subtitles for my downloads": "Ondertitels zoeken voor mijn downloads",
    "Where finished downloads end up": "Waar voltooide downloads terechtkomen",
    "Your torrent program's download folder": "De downloadmap van je torrentprogramma",
    "OpenSubtitles account": "OpenSubtitles-account",
    "Optional: 20 subtitles a day instead of 5.":
        "Optioneel: 20 ondertitels per dag in plaats van 5.",
    "Create a free account": "Maak een gratis account aan",
    "+ Add": "+ Toevoegen",
    "Sign in": "Aanmelden",
    "Own OpenSubtitles key (optional)": "Eigen OpenSubtitles-sleutel (optioneel)",
    "Leave empty to use AirGrabber's key": "Laat leeg om de sleutel van AirGrabber te gebruiken",
    "Only needed if you have your own API key from opensubtitles.com":
        "Alleen nodig als je een eigen API-sleutel van opensubtitles.com hebt",
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
    "Choose where finished downloads end up, for subtitles":
        "Kies waar voltooide downloads terechtkomen, voor de ondertitels",
    "User name": "Gebruikersnaam",
    "Password": "Wachtwoord",
    "Fill in your OpenSubtitles user name and password":
        "Vul je OpenSubtitles-gebruikersnaam en wachtwoord in",
    "Signing in…": "Aanmelden…",
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
    # app/ui/downloads_button.py
    "Downloads": "Downloads",
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
    "Hides the films you've already downloaded (your watchlist always stays)":
        "Verbergt de films die je al gedownload hebt (je kijklijst blijft altijd staan)",
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
    "Nothing new in {genre} here right now. Pick another genre or {all}.":
        "Hier staat nu niets nieuws in {genre}. Kies een ander genre of {all}.",
    "Nothing in {genre} here right now. Pick another genre or {all}.":
        "Hier staat nu niets in {genre}. Kies een ander genre of {all}.",
    # app/ui/download_control.py
    "⬇ Download": "⬇ Download",
    "Choose a download yourself": "Kies zelf een download",
    "Download": "Downloaden",
    "Finding the best one…": "De beste zoeken…",
    "✓ Grabbed": "✓ Gehaald",
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
    "✓ Have it": "✓ Heb ik",
    "Options": "Opties",
    "I already have this": "Deze heb ik al",
    "I already have this season": "Dit seizoen heb ik al",
    "Mark as missing again": "Weer als ontbrekend markeren",
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
    "{show}: {episodes} marked as already had": "{show}: {episodes} gemarkeerd als al in bezit",
    "{show}: {episodes} marked as missing again":
        "{show}: {episodes} weer als ontbrekend gemarkeerd",
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
    "today": "vandaag",
    "yesterday": "gisteren",
    "Next: {code} · {when}": "Volgende: {code} · {when}",
    "Adding…": "Toevoegen…",
    # app/ui/calendar_cards.py
    "Not aired": "Niet uitgezonden",
    "More about this show": "Meer over deze serie",
    "Not downloaded yet": "Nog niet gedownload",
    "Not downloaded": "Niet gedownload",
    "Aired, but not downloaded yet. Already have it? Mark it in the show's window.":
        "Uitgezonden, maar nog niet gedownload. Heb je hem al? Markeer hem in het venster van de serie.",
    "Nothing good enough was found. Choose a download yourself.":
        "Er is niets goed genoeg gevonden. Kies zelf een download.",
    "Not Aired": "Niet uitgezonden",
    "Suggested": "Suggestie",
    # app/ui/calendar_stack.py
    "⬇ Download all ({n})": "⬇ Alle downloaden ({n})",
    "⬇ Download ({n})": "⬇ Download ({n})",
    "⬇ All ({n})": "⬇ Alle ({n})",
    "not aired": "niet uitgezonden",
    "✓ downloaded": "✓ gedownload",
    "✓ have it": "✓ heb ik",
    "still needed": "nog nodig",
    # app/ui/watchlist_cards.py
    "📌 My watchlist": "📌 Mijn kijklijst",
    "Download when it's out": "Downloaden zodra uit",
    "Out now": "Nu uit",
    "Waiting for a good copy": "Wacht op een goede versie",
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
    "No good copy found in {days} days": "Geen goede versie gevonden in {days} dagen",
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
    "✓ Downloaded": "✓ Gedownload",
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
    "Aired less than a day ago: a copy usually turns up within hours. It counts as missing after a day.":
        "Minder dan een dag geleden uitgezonden: meestal is er binnen enkele uren een versie. Na een dag telt hij als ontbrekend.",
    "airs today": "vandaag op tv",
    "airs tomorrow": "morgen op tv",
    "airs in {n} day": "over {n} dag op tv",
    "airs in {n} days": "over {n} dagen op tv",
    # 2.0.7: app/ui/calendar_stack.py
    "new": "nieuw",
    # 2.0.7: app/ui/row_actions.py
    "⬇ Auto": "⬇ Auto",
    "Download automatically: AirGrabber picks the best copy":
        "Automatisch downloaden: AirGrabber kiest de beste versie",
    "☰ Choose…": "☰ Kiezen…",
    "Choose a copy yourself from the search results": "Kies zelf een versie uit de zoekresultaten",
    "↻ Again": "↻ Opnieuw",
    "Download it again": "Opnieuw downloaden",
    "⬇ Download again automatically": "⬇ Automatisch opnieuw downloaden",
    "☰ Choose a copy…": "☰ Zelf een versie kiezen…",
    "No good copy found": "Geen goede versie gevonden",
    "None found": "Niets gevonden",
    # 2.0.7: app/ui/subtitle_control.py
    "Get subtitles": "Ondertitels ophalen",
    "Turn on subtitles in Settings → Subtitles": "Zet ondertitels aan in Instellingen → Ondertitels",
    "Get {language} subtitles": "Ondertitels ophalen: {language}",
    "Get all my languages": "Al mijn talen ophalen",
    "Looking for subtitles…": "Ondertitels zoeken…",
    "Already there": "Al aanwezig",
    "OpenSubtitles has none for this yet. Try again later.":
        "OpenSubtitles heeft er nog geen. Probeer het later opnieuw.",
    "Not found yet: {languages}": "Nog niet gevonden: {languages}",
    "Video not in the folder": "Video niet in de map",
    "AirGrabber couldn't find the video in the folder where finished downloads end up (Settings → Subtitles).":
        "AirGrabber vond de video niet in de map waar voltooide downloads terechtkomen (Instellingen → Ondertitels).",
    "Still downloading": "Nog bezig met downloaden",
    "No download found": "Geen download gevonden",
    "Today's limit reached": "Limiet van vandaag bereikt",
    "Tried recently": "Net nog geprobeerd",
    "Couldn't get subtitles": "Kon geen ondertitels ophalen",
    "Choose the downloads folder first": "Kies eerst de downloadmap",
    # 2.0.7: app/ui/settings_dialog.py (Subtitles: When, Hearing impaired, For)
    "When": "Wanneer",
    "Automatically searches every half hour after a download. Only when I ask: use CC ▾ in a show's or film's window, or on the Downloads page.":
        "Automatisch zoekt elk halfuur na een download. Alleen als ik het vraag: gebruik CC ▾ in het venster van een serie of film, of op de pagina Downloads.",
    "Hearing impaired": "Slechthorenden",
    'Subtitles that also describe sounds ("[door slams]").':
        'Ondertitels die ook geluiden beschrijven ("[deur slaat dicht]").',
    "For": "Voor",
    "Which downloads get subtitles automatically.": "Welke downloads automatisch ondertitels krijgen.",
    "Automatically": "Automatisch",
    "Only when I ask": "Alleen als ik het vraag",
    "Avoid": "Vermijden",
    "Prefer": "Liefst wel",
    "Either": "Maakt niet uit",
    "Series and films": "Series en films",
    "Only series": "Alleen series",
    "Only films": "Alleen films",
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
    "Choose where finished downloads end up first (Settings → Subtitles)":
        "Kies eerst waar voltooide downloads terechtkomen (Instellingen → Ondertitels)",
    "Not found in the folder where finished downloads end up":
        "Niet gevonden in de map waar voltooide downloads terechtkomen",
    "The folder where finished downloads end up can't be reached":
        "De map waar voltooide downloads terechtkomen is niet bereikbaar",
    "AirGrabber doesn't know this show any more": "AirGrabber kent deze serie niet meer",
    "Subtitles need the folder where finished downloads end up (Settings → Subtitles)":
        "Ondertitels hebben de map nodig waar voltooide downloads terechtkomen (Instellingen → Ondertitels)",
    "Finished": "Voltooid",
    "Problems": "Problemen",
    # 2.0.7: app/ui/main_window.py
    "Couldn't look up {title} on TMDB": "Kon {title} niet opzoeken bij TMDB",
    # app/ui/title_info.py
    "Watch trailer": "Trailer bekijken",
    "Add a TMDB key in Settings for posters, trailers and cast":
        "Vul een TMDB-sleutel in bij Instellingen voor posters, trailers en de cast",
    "Rumoured": "Gerucht",
    "Planned": "Gepland",
    "In production": "In productie",
    "Post-production": "In postproductie",
    "Cancelled": "Geannuleerd",
    "Out digitally since {date}": "Digitaal uit sinds {date}",
    "Out digitally on {date}": "Digitaal uit op {date}",
    # app/ui/tray.py
    "Open AirGrabber": "AirGrabber openen",
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
    "Ready": "Klaar",
    "Problem in {program}": "Probleem in {program}",
    "Problem": "Probleem",
    "Not found in {program}": "Niet gevonden in {program}",
    "Not found": "Niet gevonden",
    "Status unknown": "Status onbekend",
    "Sent to your torrent program": "Naar je torrentprogramma gestuurd",
    "Saved to folder": "In map opgeslagen",
    "Opened in {program}": "Geopend in {program}",
    "Today {time}": "Vandaag {time}",
    "Yesterday {time}": "Gisteren {time}",
    "Subtitles {languages}": "Ondertitels {languages}",
    "Your NAS is almost full: {size} free": "Je NAS is bijna vol: nog {size} vrij",
    "Your download drive is almost full: {size} free":
        "Je downloadschijf is bijna vol: nog {size} vrij",
    # app/ui/indexer_dialog.py
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
    "{show}: Season {season} (Pack)": "{show}: seizoen {season} (volledig)",
    "Rated {rating}": "Classificatie {rating}",
    "Searching indexers…": "Bronnen doorzoeken…",
    "Searching indexers for Season {season} pack…":
        "Bronnen doorzoeken naar volledig seizoen {season}…",
    "Searching indexers for {code}…": "Bronnen doorzoeken naar {code}…",
    "{n} hidden by name/episode matching or your filters — Hide":
        "{n} verborgen door de naam-/afleveringscontrole of je filters — Verbergen",
    "{n} hidden by name/episode matching or your filters — Show":
        "{n} verborgen door de naam-/afleveringscontrole of je filters — Tonen",
    "Advanced Indexer Interrogation": "Uitgebreid zoeken in bronnen",
    "✎ Fix Match": "✎ Koppeling herstellen",
    "SEASON": "SEIZOEN",
    "EPISODE": "AFLEVERING",
    "VERSION": "VERSIE",
    "Torrents": "Torrents",
    "Selected": "Gekozen",
    "Tracking": "Volgend",
    "Stopped tracking {name}": "Je volgt {name} niet meer",
    "Now tracking {name} (TVMaze {id})": "Je volgt nu {name} (TVMaze {id})",
    "“{answer}” is not a TVMaze ID (a number).": "“{answer}” is geen TVMaze-ID (een getal).",
    "Fetching TVMaze show {id}…": "TVMaze-serie {id} ophalen…",
    "TVMaze has no show with ID {id}.": "TVMaze heeft geen serie met ID {id}.",
    "Couldn't fetch TVMaze show {id}: {error}": "Kon TVMaze-serie {id} niet ophalen: {error}",
    "Enter the correct TVMaze ID for this show:": "Vul de juiste TVMaze-ID voor deze serie in:",
    "Fix Match": "Koppeling herstellen",
    "Pack": "Alles",
    "Searching… {n} found": "Zoeken… {n} gevonden",
    "Torrents ({n})": "Torrents ({n})",
    "No matching torrents found.": "Geen passende torrents gevonden.",
    # app/services/downloads.py
    "No torrent program is set up on this PC. Install one, such as qBittorrent, or choose another option under \"Where downloads go\" in Settings.":
        "Er is geen torrentprogramma ingesteld op deze pc. Installeer er een, zoals qBittorrent, of kies een andere optie onder \"Waar downloads naartoe gaan\" in Instellingen.",
    "No .torrent file is available for this release, and no torrent program on a NAS or another computer is set up in Settings to send its magnet link to.":
        "Er is geen .torrent-bestand voor deze release, en er is in Instellingen geen torrentprogramma op een NAS of andere computer ingesteld om de magnetlink naartoe te sturen.",
    "No .torrent file or magnet link is available for this release.":
        "Er is geen .torrent-bestand of magnetlink voor deze release.",
    "Saved {name}": "{name} opgeslagen",
    "Sent to {program}": "Naar {program} gestuurd",
    "Opened in your torrent program": "Geopend in je torrentprogramma",
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
    # app/services/opensubtitles.py
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
    # app/services/subtitles.py
    "Choose where finished downloads end up (Settings, Subtitles)":
        "Kies waar voltooide downloads terechtkomen (Instellingen, Ondertitels)",
    "Couldn't save subtitles next to the videos. Check that the folder is writable":
        "Kon de ondertitels niet naast de video's opslaan. Controleer of je in de map mag schrijven",
    "Couldn't open {folder}. Check that the folder (or the NAS) is reachable":
        "Kon {folder} niet openen. Controleer of de map (of de NAS) bereikbaar is",
    "Today's subtitle downloads are used up; more tomorrow":
        "De ondertiteldownloads van vandaag zijn op; morgen weer",
    "Today's subtitle downloads are used up; more after {time}":
        "De ondertiteldownloads van vandaag zijn op; na {time} weer",
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
    # app/whats_new.py
    "After installing qBittorrent, AirGrabber now sets Windows to open downloads with it, so you don't have to.":
        "Na het installeren van qBittorrent laat AirGrabber Windows downloads er nu zelf mee openen.",
    "AirGrabber never does this when you already use another torrent program.":
        "AirGrabber doet dit nooit als je al een ander torrentprogramma gebruikt.",
    "\"Clear finished\" also clears downloads your torrent program no longer has.":
        "\"Voltooide wissen\" wist ook downloads die je torrentprogramma niet meer heeft.",
    "An episode now counts as missing one day after it airs; until then it's marked New.":
        "Een aflevering telt nu pas een dag na uitzending als ontbrekend; tot dan staat er Nieuw.",
    "Quieter download buttons: ⬇ Auto picks the best copy, ☰ Choose… lets you pick, ↻ Again downloads it again.":
        "Rustigere downloadknoppen: ⬇ Auto kiest de beste versie, ☰ Kiezen… laat je zelf kiezen, ↻ Opnieuw downloadt nog eens.",
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
}
