"""The folder library (docs/folder-library/): without Plex, the user's own film and series
folders are the library. `scan` reads and recognises a folder's videos, `match` turns them into
the titles, episodes and films Plex's check stores (`PlexRepository.replace`); the files found
are kept per file in `library_files` (`LibraryFileRepository`). Read-only: never a file moved,
renamed or deleted."""
