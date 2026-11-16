# Folder library fixtures

Tiny `.nfo` files as MediaForge (app/forge/core/nfo.py) and Kodi-style tools write them, built by
hand for tests/test_folder_scan.py:

- `movie.nfo`: a film's, beside its video (`<uniqueid type="tmdb">` 329865).
- `tvshow.nfo`: a show's, in its show folder (TMDB 95396, and an IMDb id that is not read).
- `episode.nfo`: an episode's; its TMDB id is the episode's own, never read as the show's.
