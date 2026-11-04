"""Forge's own settings, stored as one JSON document in forge.db.

MediaForge's `AppSettings` without what AirGrabber now owns (the TMDB key, theme, accent,
updates, the tray notice, the quiet restart and the first start). Unknown keys are ignored, so a
document stored by MediaForge (or an older Forge) still loads.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Why a folder comes from AirGrabber's settings (app/forge/core/airgrabber_folders.py says it in
# words): "finished" = "Where finished downloads end up"; "qbittorrent" = the save folder of the
# qBittorrent on this PC that AirGrabber set up; "shows" / "films" = the download folder AirGrabber
# gives its torrent program (`remote_destination` / `movie_remote_destination`).
AirGrabberWhy = Literal["finished", "qbittorrent", "shows", "films"]
# What AirGrabber's settings leave to the owner when they name no folder on this PC: "" nothing;
# "finished" = no "Where finished downloads end up"; "other_computer" = qBittorrent / Transmission
# on another computer; "nas_folder" = Download Station without a download folder of AirGrabber's.
AirGrabberAsk = Literal["", "finished", "other_computer", "nas_folder"]


class AirGrabberFolder(BaseModel):
    """A folder AirGrabber's settings point at. A `suggested` one (a NAS path guessed from
    Download Station's address) is never watched until the owner adds it; `reachable` is what
    the last check found (None: not checked)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    path: str
    why: AirGrabberWhy
    program: str = ""  # the torrent program's name ("Download Station"), for the reason's words
    suggested: bool = False
    reachable: bool | None = None


class AirGrabberDownloads(BaseModel):
    """What AirGrabber's settings say about where its downloads end up, as last derived (B-01).
    Empty until the first look."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    folders: list[AirGrabberFolder] = []
    ask: AirGrabberAsk = ""
    program: str = ""  # for "other_computer": the program on the other computer


class ForgeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    tmdb_language: str = "en-US"
    scan_subfolders: bool = True
    window_geometry: str | None = None  # unread: the old Forge window's; MediaForge is a page now
    last_preset: str | None = None
    # The queue's preset card: the preset last chosen for shows and for films (None: the
    # library's preset for that kind, `series_preset` / `movies_preset`).
    last_show_preset: str | None = None
    last_film_preset: str | None = None
    legacy_imported: bool = False
    context_menu_presets: list[str] = []
    trailer_tools_ok: bool = False
    ytdlp_checked_at: float = 0.0
    # Renaming on its own: every folder is chosen in Settings, nothing assumed.
    watch_folders: list[str] = []
    series_folder: str = ""
    movies_folder: str = ""
    series_preset: str = "Series (Folder)"
    movies_preset: str = "Movie (Folder)"
    automatic: Literal["off", "trial", "on"] = "off"
    clean_downloads: bool = True
    notify_needs: bool = True
    daily_summary: bool = True
    remove_after_forge: bool = False  # a run's finished files leave the queue (History keeps them)
    summary_sent_on: str = ""  # ISO date of the last evening summary
    kept_back: list[str] = []  # downloads put back by an undo: never moved on their own again
    imported_from: str = ""  # set once the old MediaForge's data was imported (F-26)
    # "Use AirGrabber's downloads" (B-01): None = not chosen yet (see `airgrabber_on`).
    use_airgrabber_downloads: bool | None = None
    # Where AirGrabber's downloads end up, kept up to date from AirGrabber's settings
    # (app/forge/core/airgrabber_folders.py); watched next to `watch_folders` while `airgrabber_on`.
    airgrabber: AirGrabberDownloads = AirGrabberDownloads()
    # AirGrabber's automatic subtitles became presets' Subtitles extra, once
    # (app/forge/core/subtitles.py's `carry_subtitles`): never again after that.
    subtitles_carried: bool = False
    # Series (Folder) and Movie (Folder) got the Move to library folder extra once
    # (app/forge/core/library.py's `carry_move_option`): never again after that.
    move_carried: bool = False

    @property
    def airgrabber_on(self) -> bool:
        """Whether AirGrabber's downloads are watched: as chosen; until then on for a new
        setup and off for one brought over from the old MediaForge."""
        if self.use_airgrabber_downloads is None:
            return not self.imported_from
        return self.use_airgrabber_downloads
