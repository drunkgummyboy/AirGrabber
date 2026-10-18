"""AirGrabber's own identity in Windows: its AppUserModelID.

Without one, Windows files AirGrabber under python(w).exe: notifications are titled "Python" and
its taskbar button groups with any other Python program. main.py gives the process the ID before
any window or the tray icon exists (`set_process_app_id`); every shortcut AirGrabber makes carries
the same ID (install.ps1, app/services/startup.py, app/services/app_identity.py), because Windows
takes a notification's name and icon from the Start-menu shortcut that has it.

Stdlib only: main.py uses this before the package check. Elsewhere than Windows it is a no-op.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

# One ID for every copy (installed and development copies share one data folder). Keep in step
# with install.ps1's $AppId.
APP_USER_MODEL_ID = "drunkgummyboy.AirGrabber"


def set_process_app_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Give this process `app_id`. Call it before the first window or tray icon exists.

    False (logged) when Windows refuses; always False outside Windows.
    """
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32")
        setter = shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = (wintypes.LPCWSTR,)
        setter.restype = ctypes.c_long  # HRESULT
        result = int(setter(app_id))
    except (OSError, AttributeError) as exc:
        log.warning("Could not set the AppUserModelID: %r", exc)
        return False
    if result < 0:
        log.warning("Could not set the AppUserModelID (HRESULT 0x%08X)", result & 0xFFFFFFFF)
        return False
    return True
