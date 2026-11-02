"""Forge: MediaForge inside AirGrabber (renames and sorts films and episodes into a library).

Ported from the old stand-alone MediaForge. What lives here:

- `models/`: Forge's frozen Pydantic models.
- `core/`: the pure core (parsing file names, naming rules, matching); no I/O, no Tk.
- `db/`: Forge's own database (`forge.db` in `AppPaths.forge`) and its repositories: the only
  place besides `app/database.py` that holds SQL.
- `clients/`: TMDB, images and trailers on AirGrabber's httpx clients.
- `ui/`: the Forge window, its views and `ui/widgets/`; never imports `app.forge.db`,
  `app.database` or httpx.

Non-UI Forge code never imports tkinter / customtkinter. Dutch texts: `app/i18n_nl_forge.py`.
The findings, decisions and task plan: `docs/forge/INTEGRATION.md` and `docs/forge/PLAN.md`.
"""
