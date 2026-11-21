"""Where the Companion app (the phone app) can be downloaded: the `phone-app` release on
AirGrabber's GitHub page, whose files each new Companion version replaces (uploaded only with the
owner's OK), so these links stay the same.

The one place the PC knows them: Settings' download code (app/ui/phone_app.py) shows PHONE_APP_URL,
and `/v1/hello`'s `app_update` hands both to the phone, which checks VERSION_URL for a newer
Companion (docs/companion/2026-11-21-portability-contract.md).
"""

from __future__ import annotations

RELEASE_URL = "https://github.com/drunkgummyboy/AirGrabber/releases/download/phone-app/"
PHONE_APP_URL = RELEASE_URL + "AirGrabber-Companion.apk"
VERSION_URL = RELEASE_URL + "version.json"


def app_update() -> dict[str, str]:
    """`/v1/hello`'s `app_update`: where the phone checks for, and fetches, a newer Companion."""
    return {"version_url": VERSION_URL, "apk_url": PHONE_APP_URL}
