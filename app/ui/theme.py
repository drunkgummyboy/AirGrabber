"""Colour palette and fonts (the dark theme carried over from AirGrabber 1.x)."""

from __future__ import annotations

import customtkinter as ctk

BG = "#161616"  # window background
SURFACE = "#242424"  # cards and inputs
TAB_BG = "#1C1C1C"  # tab view body
EDGE = "#4E6A85"  # slate-blue borders
ACCENT = "#4E6A85"  # primary buttons
ACCENT_HOVER = "#427839"  # button hover
TEXT = "#FFFFFF"
TEXT_MUTED = "#A4B2C6"
TEXT_DIM = "gray60"
DANGER = "#E74C3C"
SUCCESS = "#2FA572"  # positive results (connection test passed)
LINK = "#5D8AA8"
DANGER_BUTTON = "#C0392B"  # destructive buttons (remove)
DANGER_BUTTON_HOVER = "#922B21"
IMDB = "#F5C518"  # IMDb brand yellow
IMDB_HOVER = "#D4A710"
PLACEHOLDER = "gray20"  # poster placeholder

# Calendar grid
CELL_BG = "#121620"  # day cell
CELL_BORDER = "#1C222E"
TODAY_BG = "#182133"  # today's cell (its border is ACCENT)
SUGGESTION_BG = "#14121A"  # untracked-show suggestion card
SUGGESTION_BORDER = "#2A2438"
GRABBED = "#2E7D4F"  # episode button once downloaded
NOT_AIRED = "gray25"  # disabled episode button before broadcast

# Discover / Movies shelf titles
SECTION_HOT = "#F39C12"  # trending
SECTION_NEW = "#2ECC71"  # premieres, fresh digital releases
WARNING = SECTION_HOT  # amber: something is missing rather than broken (no torrent program)
PANEL_EDGE = PLACEHOLDER  # a quiet 1 px border around a group of settings (Prowlarr)

# Indexer search dialog
CHIP = "gray15"  # unselected season / episode / version button
CHIP_HOVER = "gray25"
PENDING = "#F1C40F"  # indexer still searching
ROW_SELECTED = "#2A3A4D"  # selected results-table row
TEXT_FAINT = "gray40"  # skipped indexers, results hidden by the filters


def font(size: int = 13, *, bold: bool = False, underline: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight="bold" if bold else "normal", underline=underline)


def apply_appearance() -> None:
    """Global look; call before the root window is created."""
    ctk.set_appearance_mode("dark")
