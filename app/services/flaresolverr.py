"""FlareSolverr: whether the free helper that gets Prowlarr past sites' robot checks is running
on this PC. AirGrabber never downloads, starts or configures it; the "Add FlareSolverr" guide
(app/ui/flaresolverr_guide.py) only explains the steps and uses this check.

A running FlareSolverr answers GET / on port 8191 with JSON like
{"msg": "FlareSolverr is ready!", "version": "3.3.21", "userAgent": "..."}. Logs hold only the
outcome, never the answer.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

FLARESOLVERR_URL = "http://localhost:8191/"
_TIMEOUT = httpx.Timeout(3.0, connect=2.0)


async def flaresolverr_running(
    http: httpx.AsyncClient, url: str = FLARESOLVERR_URL
) -> bool:
    """True when `url` answers 200 with JSON whose "msg" mentions FlareSolverr."""
    try:
        response = await http.get(url, timeout=_TIMEOUT, follow_redirects=False)
    except (httpx.HTTPError, httpx.InvalidURL):
        log.info("FlareSolverr check: no answer")
        return False
    if response.status_code != 200:
        log.info("FlareSolverr check: HTTP %d", response.status_code)
        return False
    try:
        payload = response.json()
    except ValueError:
        payload = None
    message = payload.get("msg") if isinstance(payload, dict) else None
    running = isinstance(message, str) and "flaresolverr" in message.casefold()
    if not running:
        log.info("FlareSolverr check: another program answered")
    return running


class FlareSolverrService:
    """The "Check" in the Add FlareSolverr guide."""

    def __init__(self, http: httpx.AsyncClient, *, url: str = FLARESOLVERR_URL) -> None:
        self._http = http
        self._url = url

    async def is_running(self) -> bool:
        return await flaresolverr_running(self._http, self._url)
