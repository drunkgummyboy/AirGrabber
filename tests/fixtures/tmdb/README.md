# TMDB fixtures

The lists and details without a prefix (`movie_*.json`, `tv_*.json`, `error_401.json`) are
answers recorded from TMDB's API.

The `extras_*.json` files (the info window: a film's and a show's details in Dutch, their
videos, their cast, and `/find` by IMDb id) were **built by hand from TMDB's API reference**
(https://developer.themoviedb.org/reference: "Movie Details", "Movie Videos", "Movie Credits",
"TV Series Details", "TV Series Aggregate Credits", "Find By ID"), not recorded. Every film,
show, person and video key in them is made up; the image paths are only there to have a path.

- `extras_movie_info_nl.json`: `/movie/4242?language=nl-NL` (Dutch overview and genre names)
- `extras_movie_videos.json`: `/movie/4242/videos`: a teaser, an unofficial Dutch trailer, an
  official English and an official Dutch trailer, a Vimeo trailer, a featurette and an entry
  without a key
- `extras_movie_credits.json`: `/movie/4242/credits`: eleven cast entries in billing order (one
  without a name, several without a photo or a character) and one crew entry
- `extras_tv_info_nl.json`: `/tv/7171?language=nl-NL` with an empty overview (no Dutch text)
- `extras_tv_aggregate_credits.json`: `/tv/7171/aggregate_credits`: an actor with two parts
  (the one with the most episodes counts) and one without any
- `extras_find_tv.json`: `/find/tt9900071?external_source=imdb_id`
