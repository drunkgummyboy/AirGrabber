"""Move to library folder, a preset's extra: a file added by hand goes to its Series / Movies folder
with the extra on, stays where it is with it off, and the queue says which before anything happens.
Downloads MediaForge finds always move."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from app.forge.core.library import carry_move_option, library_folder, route_inputs
from app.forge.core.queue import ItemStatus
from app.forge.models.media import MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.plan import PlanInput
from app.forge.models.preset import DEFAULT_PRESETS, PresetOptions
from app.forge.models.settings import ForgeSettings
from app.forge.ui.queue_rows import _destination
from tests.forge.base import ForgeTestCase
from tests.forge.tk import pump
from tests.forge.ui.test_queue_view import QueueCase, make_files


def _input(kind: MediaKind, root: Path) -> PlanInput:
    parsed = ParsedMedia(source_name="x.mkv", kind=kind, title="Heat")
    return PlanInput(
        item_id=1,
        source=root / "x.mkv",
        root=root,
        dropped_folder=root,
        media=ResolvedMedia.offline(parsed),
    )


class RouteTest(ForgeTestCase):
    def test_the_defaults(self) -> None:
        ticked = {p.name for p in DEFAULT_PRESETS if p.options.move_to_library}
        self.assertEqual(ticked, {"Series (Folder)", "Movie (Folder)"})

    def test_the_folder_per_kind(self) -> None:
        settings = ForgeSettings(series_folder="S:/Series", movies_folder="")
        self.assertEqual(library_folder(MediaKind.EPISODE, settings), Path("S:/Series"))
        self.assertIsNone(library_folder(MediaKind.MOVIE, settings))

    def test_off_stays_on_starts_in_the_library_keeping_what_was_dropped(self) -> None:
        here = Path("D:/in")
        settings = ForgeSettings(series_folder="S:/Series", movies_folder="M:/Movies")
        item = _input(MediaKind.MOVIE, here)
        self.assertEqual(route_inputs([item], PresetOptions(), settings), ([item], []))
        routed, left = route_inputs([item], PresetOptions(move_to_library=True), settings)
        self.assertEqual(left, [])
        self.assertEqual(routed[0].root, Path("M:/Movies"))
        self.assertEqual(routed[0].dropped_folder, here)  # Clean junk looks there

    def test_without_its_folder_a_file_is_left_alone(self) -> None:
        item = _input(MediaKind.EPISODE, Path("D:/in"))
        settings = ForgeSettings(series_folder="", movies_folder="M:/Movies")
        options = PresetOptions(move_to_library=True)
        self.assertEqual(route_inputs([item], options, settings), ([], [item]))

    def test_the_words(self) -> None:
        for state, moves, known, folder, expected, look in (
            ("ready", True, True, "Series › Heat", "📦 Moves to Series › Heat", "moves"),
            ("done", True, True, "Series", "📦 Moved to Series", "moves"),
            ("ready", False, False, "", "📁 Stays in its folder", "stays"),
            ("ready", False, False, "Heat", "📁 Stays here, in Heat", "stays"),
            ("done", False, False, "", "📁 Stayed in its folder", "stays"),
            ("done", False, False, "Heat", "📁 Stayed here, in Heat", "stays"),
            ("needs", True, True, "Series", "", "moves"),
        ):
            self.assertEqual(
                _destination(state, MediaKind.MOVIE, moves, known, folder),  # type: ignore[arg-type]
                (expected, look),
            )
        self.assertEqual(
            _destination("ready", MediaKind.EPISODE, True, False, ""),
            ("⚠ Choose your Series folder in Settings › Auto-rename", "warn"),
        )
        self.assertEqual(
            _destination("ready", MediaKind.MOVIE, True, False, ""),
            ("⚠ Choose your Movies folder in Settings › Auto-rename", "warn"),
        )


class MoveInTheQueueTest(QueueCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        async def skip_trailers(_title: str, _message: str, _choices: Sequence[str]) -> str | None:
            return "Skip trailers"

        self.view.ask = skip_trailers

    async def folders(self) -> tuple[Path, Path, Path]:
        here = self.tmp / "in"
        series, movies = self.tmp / "lib" / "Series", self.tmp / "lib" / "Movies"
        for folder in (here, series, movies):
            folder.mkdir(parents=True, exist_ok=True)
        await self.services.settings.update(series_folder=str(series), movies_folder=str(movies))
        return here, series, movies

    async def test_ticked_goes_to_the_library_and_undo_brings_it_back(self) -> None:
        here, _series, movies = await self.folders()
        make_files(here, ["Heat.1995.mkv"])
        await self.choose("Movie (Folder)")
        await self.add_and_settle(here)
        band = self.band("Heat")
        self.assertEqual((band.dest, band.dest_look), ("📦 Moves to Movies › Heat (1995)", "moves"))
        await self.view.forge()
        moved = movies / "Heat (1995)" / "Heat (1995).mkv"
        self.assertTrue(moved.is_file())
        self.assertIn("Forged: 1 moved, 0 renamed", self.services.activity.lines()[-1].text)
        self.assertEqual(self.names(here), [])
        self.view.refresh()
        await pump(self.root, 0.1)
        self.assertEqual(self.band("Heat").dest, "📦 Moved to Movies › Heat (1995)")
        await self.view.undo_band([self.services.queue.items[0].id])
        self.assertEqual(self.names(here), ["Heat.1995.mkv"])
        self.assertFalse(moved.exists())

    async def test_not_ticked_stays_where_it_is(self) -> None:
        here, _series, movies = await self.folders()
        make_files(here, ["Heat.1995.mkv"])
        await self.choose("Movie (Flat)")
        await self.add_and_settle(here)
        band = self.band("Heat")
        self.assertEqual((band.dest, band.dest_look), ("📁 Stays in its folder", "stays"))
        await self.view.forge()
        self.assertEqual(self.names(here), ["Heat (1995).mkv"])
        self.assertEqual(self.names(movies), [])
        self.assertIn("Forged: 1 renamed", self.services.activity.lines()[-1].text)

    async def test_the_button_on_a_band_decides_for_that_band(self) -> None:
        here, _series, movies = await self.folders()
        make_files(here, ["Heat.1995.mkv"])
        await self.choose("Movie (Flat)")
        await self.add_and_settle(here)
        self.select_band("Heat")
        await pump(self.root, 0.1)
        self.view.option_buttons["To library folder"].invoke()
        self.view.refresh()
        await pump(self.root, 0.1)
        self.assertEqual(self.band("Heat").dest, "📦 Moves to Movies")
        await self.view.forge()
        self.assertEqual(self.names(movies), ["Heat (1995).mkv"])

    async def test_without_a_folder_the_file_is_left_alone_and_says_why(self) -> None:
        here = self.tmp / "in"
        make_files(here, ["Heat.1995.mkv", "Show.Name.S01E02.mkv"])
        await self.choose("Movie (Folder)", "Series (Flat)")
        await self.add_and_settle(here)
        band = self.band("Heat")
        self.assertEqual(
            (band.dest, band.dest_look),
            ("⚠ Choose your Movies folder in Settings › Auto-rename", "warn"),
        )
        opened: list[str] = []
        self.view.on_choose_folders = lambda: opened.append("folders")
        self.view.list.on_action(self.band("Heat").key, "folders")
        self.assertEqual(opened, ["folders"])
        await self.view.forge()
        self.assertIn("Heat.1995.mkv", self.names(here))  # untouched
        self.assertEqual(self.item("Heat").status, ItemStatus.READY)
        self.assertFalse((here / "Show.Name.S01E02.mkv").exists())  # the other band still ran

    async def test_clean_junk_stays_out_of_the_library(self) -> None:
        here, _series, movies = await self.folders()
        make_files(here / "Heat.1995", ["Heat.1995.mkv"])
        (here / "Heat.1995" / "Thumbs.db").write_text("x")
        (movies / "Thumbs.db").write_text("mine")
        await self.choose("Movie (Folder)")
        await self.add_and_settle(here)
        self.select_band("Heat")
        await pump(self.root, 0.1)
        self.view.option_buttons["Clean junk"].invoke()
        await self.view.forge()
        self.assertTrue((movies / "Thumbs.db").is_file())
        self.assertFalse((here / "Heat.1995" / "Thumbs.db").exists())

    async def test_a_found_download_always_moves_and_its_button_says_so(self) -> None:
        downloads, _series, _movies = await self.library()
        await self.choose("Movie (Flat)")
        await self.watched(downloads, "Heat.1995.mkv")
        self.select_band("Heat")
        await pump(self.root, 0.1)
        button = self.view.option_buttons["To library folder"]
        self.assertTrue(button.value)
        self.assertTrue(button.locked)
        button.invoke()
        self.assertTrue(button.value)
        self.assertIn("always move", self.view._option_tips["To library folder"].text)
        self.assertTrue(self.band("Heat").dest.startswith("📦"))

    async def test_a_hand_added_band_next_to_it_can_be_clicked_again(self) -> None:
        downloads, _series, _movies = await self.library()
        here = self.tmp / "in"
        make_files(here, ["Dune.2021.mkv"])
        await self.watched(downloads, "Heat.1995.mkv")
        await self.add_and_settle(here)
        self.select_band("Heat")
        await pump(self.root, 0.1)
        self.assertTrue(self.view.option_buttons["To library folder"].locked)
        self.select_band("Dune")
        await pump(self.root, 0.1)
        self.assertFalse(self.view.option_buttons["To library folder"].locked)

    async def test_the_presets_get_the_option_once(self) -> None:
        presets, settings = self.services.presets, self.services.settings
        self.assertTrue(settings.current.move_carried)  # at the start
        for preset in await presets.list():  # as a copy from before the option
            off = preset.options.model_copy(update={"move_to_library": False})
            await presets.save(preset.model_copy(update={"options": off}))
        await settings.update(move_carried=False)
        await carry_move_option(settings, presets)
        ticked = {p.name for p in await presets.list() if p.options.move_to_library}
        self.assertEqual(ticked, {"Series (Folder)", "Movie (Folder)"})
        self.assertTrue(settings.current.move_carried)
        for preset in await presets.list():  # turned off stays off
            off = preset.options.model_copy(update={"move_to_library": False})
            await presets.save(preset.model_copy(update={"options": off}))
        await carry_move_option(settings, presets)
        self.assertFalse(any(p.options.move_to_library for p in await presets.list()))
