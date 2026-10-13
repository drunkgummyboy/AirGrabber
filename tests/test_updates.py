"""Self-update: version check, archive validation, swapping the app files in (and rolling back)."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
import zipfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import httpx

import main
from app.services import updates
from app.services.install_info import UNINSTALL_KEY
from app.services.updates import (
    ARCHIVE_URL,
    READY_FILE,
    STAGING_DIR,
    VERSION_URL,
    UpdateError,
    UpdateService,
    apply_staged,
    install_archive,
    is_development_copy,
    is_newer,
    parse_version,
    stage_archive,
)
from tests.test_install_info import FakeRegistry

TOP = "AirGrabber-main/"


def archive(files: dict[str, str], top: str = TOP) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(top, "")  # GitHub archives list their top folder first
        for name, text in files.items():
            zf.writestr(top + name, text)
    return buffer.getvalue()


def release(version: str = "2.1.0", **extra: str) -> dict[str, str]:
    return {
        "main.py": "print('new main')\n",
        "requirements.txt": "httpx>=0.28\n",
        "app/__init__.py": f'APP_NAME = "AirGrabber"\n__version__ = "{version}"\n',
        "app/ui/new_module.py": "NEW = True\n",
        **extra,
    }


class VersionTests(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(parse_version('"""Doc."""\n\nAPP_NAME = "x"\n__version__ = "2.0.1"\n'), "2.0.1")
        self.assertEqual(parse_version("__version__ = '2.1'"), "2.1")
        self.assertIsNone(parse_version("CURRENT_VERSION = '1.1.13'"))

    def test_is_newer(self) -> None:
        self.assertTrue(is_newer("2.0.1", "2.0.0"))
        self.assertTrue(is_newer("2.10.0", "2.9.3"))
        self.assertFalse(is_newer("2.0.0", "2.0.0"))
        self.assertFalse(is_newer("2.0", "2.0.0"))
        self.assertFalse(is_newer("1.9.9", "2.0.0"))
        self.assertFalse(is_newer("garbage", "2.0.0"))


class InstallDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / "AirGrabber"
        (self.dir / "app" / "ui").mkdir(parents=True)
        (self.dir / "main.py").write_text("print('old main')\n", encoding="utf-8")
        (self.dir / "requirements.txt").write_text("httpx>=0.27\n", encoding="utf-8")
        (self.dir / "app" / "__init__.py").write_text('__version__ = "2.0.0"\n', encoding="utf-8")
        (self.dir / "app" / "ui" / "stale.py").write_text("OLD = True\n", encoding="utf-8")
        (self.dir / "notes.txt").write_text("mine\n", encoding="utf-8")

    def read(self, relative: str) -> str:
        return (self.dir / relative).read_text(encoding="utf-8")

    def assert_untouched(self) -> None:
        self.assertEqual(self.read("main.py"), "print('old main')\n")
        self.assertEqual(self.read("requirements.txt"), "httpx>=0.27\n")
        self.assertTrue((self.dir / "app" / "ui" / "stale.py").exists())
        self.assertFalse((self.dir / "app" / "ui" / "new_module.py").exists())
        self.assertEqual(self.read("notes.txt"), "mine\n")


class InstallArchiveTests(InstallDirTestCase):
    def test_replaces_exactly_the_app_files(self) -> None:
        data = archive(release(**{"tests/test_x.py": "", "airgrabber.exe": "MZ", "app/__pycache__/x.pyc": ""}))
        self.assertEqual(install_archive(data, self.dir), "2.1.0")
        self.assertEqual(self.read("main.py"), "print('new main')\n")
        self.assertEqual(self.read("requirements.txt"), "httpx>=0.28\n")
        self.assertTrue((self.dir / "app" / "ui" / "new_module.py").exists())
        self.assertFalse((self.dir / "app" / "ui" / "stale.py").exists())  # app/ is replaced whole
        self.assertFalse((self.dir / "app" / "__pycache__").exists())
        self.assertEqual(self.read("notes.txt"), "mine\n")
        self.assertFalse((self.dir / "tests").exists())
        self.assertFalse((self.dir / "airgrabber.exe").exists())
        self.assertFalse((self.dir / updates.STAGING_DIR).exists())

    def test_a_leftover_read_only_staging_folder_is_cleared(self) -> None:
        leftover = self.dir / updates.STAGING_DIR / "old" / "app"
        leftover.mkdir(parents=True)
        (leftover / "x.py").write_text("", encoding="utf-8")
        os.chmod(leftover / "x.py", stat.S_IREAD)
        os.chmod(leftover, stat.S_IREAD | stat.S_IEXEC)  # read-only folder, as Google Drive makes them
        self.assertEqual(install_archive(archive(release()), self.dir), "2.1.0")
        self.assertFalse((self.dir / updates.STAGING_DIR).exists())

    def test_incomplete_archive_changes_nothing(self) -> None:
        files = release()
        del files["requirements.txt"]
        with self.assertRaisesRegex(UpdateError, "requirements.txt"):
            install_archive(archive(files), self.dir)
        self.assert_untouched()

    def test_archive_without_version_changes_nothing(self) -> None:
        with self.assertRaisesRegex(UpdateError, "version"):
            install_archive(archive(release() | {"app/__init__.py": "APP_NAME = 'x'\n"}), self.dir)
        self.assert_untouched()

    def test_unsafe_paths_are_refused(self) -> None:
        with self.assertRaisesRegex(UpdateError, "unsafe"):
            install_archive(archive(release(**{"app/../../evil.py": "x"})), self.dir)
        self.assert_untouched()
        self.assertFalse((self.dir.parent / "evil.py").exists())

    def test_not_a_zip(self) -> None:
        with self.assertRaisesRegex(UpdateError, "not a valid archive"):
            install_archive(b"<html>rate limited</html>", self.dir)
        self.assert_untouched()

    def test_more_than_one_top_folder_is_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, text in release().items():
                zf.writestr(TOP + name, text)
            zf.writestr("Other/main.py", "")
        with self.assertRaisesRegex(UpdateError, "layout"):
            install_archive(buffer.getvalue(), self.dir)
        self.assert_untouched()

    def test_a_failed_swap_is_rolled_back(self) -> None:
        real_rename = updates._rename

        def flaky(source: Path, destination: Path) -> None:
            if source.name == "app" and source.parent.name == "new":  # the last entry to go in
                raise PermissionError("locked")
            real_rename(source, destination)

        with mock.patch.object(updates, "_rename", side_effect=flaky), self.assertLogs(updates.log, "WARNING"):
            with self.assertRaisesRegex(UpdateError, "Nothing was changed"):
                install_archive(archive(release()), self.dir)
        self.assert_untouched()
        self.assertEqual(self.read("app/__init__.py"), '__version__ = "2.0.0"\n')

    def test_rename_retries_briefly_locked_files(self) -> None:
        calls: list[Path] = []
        real = os.rename

        def locked_once(source: Path, destination: Path) -> None:
            calls.append(source)
            if len(calls) == 1:
                raise PermissionError("scanning")
            real(source, destination)

        with mock.patch("os.rename", side_effect=locked_once), mock.patch("time.sleep"):
            self.assertEqual(install_archive(archive(release()), self.dir), "2.1.0")
        self.assertEqual(self.read("main.py"), "print('new main')\n")


class StageAndApplyTests(InstallDirTestCase):
    """Automatic updates: unpack in the background, swap in once the app has closed."""

    @property
    def staging(self) -> Path:
        return self.dir / STAGING_DIR

    def test_stage_then_apply_swaps_the_files(self) -> None:
        self.assertEqual(stage_archive(archive(release()), self.dir, newer_than="2.0.0"), "2.1.0")
        self.assert_untouched()  # staging alone changes nothing
        self.assertTrue((self.staging / "new" / "app" / "ui" / "new_module.py").exists())
        self.assertEqual((self.staging / READY_FILE).read_text(encoding="utf-8"), "2.1.0")

        with self.assertLogs(updates.log, "INFO"):
            self.assertEqual(apply_staged(self.dir), "2.1.0")
        self.assertEqual(self.read("main.py"), "print('new main')\n")
        self.assertEqual(self.read("requirements.txt"), "httpx>=0.28\n")
        self.assertTrue((self.dir / "app" / "ui" / "new_module.py").exists())
        self.assertFalse((self.dir / "app" / "ui" / "stale.py").exists())
        self.assertEqual(self.read("notes.txt"), "mine\n")
        self.assertFalse(self.staging.exists())
        self.assertIsNone(apply_staged(self.dir))  # nothing left to apply

    def test_nothing_staged(self) -> None:
        self.assertIsNone(apply_staged(self.dir))
        self.assert_untouched()

    def test_a_stale_staged_update_is_deleted_not_applied(self) -> None:
        for staged in ("2.0.0", "1.9.9"):  # equal to / older than the version on disk
            with self.subTest(staged=staged):
                stage_archive(archive(release(staged)), self.dir)
                with self.assertLogs(updates.log, "INFO"):
                    self.assertIsNone(apply_staged(self.dir))
                self.assert_untouched()
                self.assertFalse(self.staging.exists())

    def test_staged_update_older_than_a_reinstall_is_deleted(self) -> None:
        stage_archive(archive(release("2.1.0")), self.dir, newer_than="2.0.0")
        # The user reran the installer meanwhile, which brought 2.2.0.
        (self.dir / "app" / "__init__.py").write_text('__version__ = "2.2.0"\n', encoding="utf-8")
        with self.assertLogs(updates.log, "INFO"):
            self.assertIsNone(apply_staged(self.dir))
        self.assertEqual(self.read("app/__init__.py"), '__version__ = "2.2.0"\n')
        self.assertEqual(self.read("main.py"), "print('old main')\n")
        self.assertFalse(self.staging.exists())

    def test_an_incomplete_staged_update_is_deleted(self) -> None:
        cases: dict[str, Callable[[], object]] = {
            "no ready marker": lambda: (self.staging / READY_FILE).unlink(),
            "marker for another version": lambda: (self.staging / READY_FILE).write_text(
                "2.2.0", encoding="utf-8"
            ),
            "an entry missing": lambda: (self.staging / "new" / "main.py").unlink(),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                stage_archive(archive(release()), self.dir)
                damage()
                with self.assertLogs(updates.log, "INFO"):
                    self.assertIsNone(apply_staged(self.dir))
                self.assert_untouched()
                self.assertFalse(self.staging.exists())

    def test_a_failed_apply_rolls_back_without_raising(self) -> None:
        stage_archive(archive(release()), self.dir)
        real_rename = updates._rename

        def flaky(source: Path, destination: Path) -> None:
            if source.name == "app" and source.parent.name == "new":  # the last entry to go in
                raise PermissionError("locked")
            real_rename(source, destination)

        with mock.patch.object(updates, "_rename", side_effect=flaky), mock.patch("time.sleep"):
            with self.assertLogs(updates.log, "WARNING"):
                self.assertIsNone(apply_staged(self.dir))
        self.assert_untouched()
        self.assertEqual(self.read("app/__init__.py"), '__version__ = "2.0.0"\n')
        self.assertFalse(self.staging.exists())  # rolled back: never tried again

    def test_unexpected_errors_never_escape(self) -> None:
        stage_archive(archive(release()), self.dir)
        with mock.patch.object(updates, "_swap", side_effect=RuntimeError("bug")):
            with self.assertLogs(updates.log, "ERROR"):
                self.assertIsNone(apply_staged(self.dir))
        self.assert_untouched()

    def test_a_development_copy_never_applies(self) -> None:
        stage_archive(archive(release()), self.dir)
        (self.dir / "tests").mkdir()
        self.assertIsNone(apply_staged(self.dir))
        self.assert_untouched()
        self.assertTrue((self.staging / READY_FILE).exists())  # not even cleaned up

    def test_stage_refuses_a_version_that_is_not_newer(self) -> None:
        with self.assertRaisesRegex(UpdateError, "not newer"):
            stage_archive(archive(release("2.0.0")), self.dir, newer_than="2.0.0")
        self.assertFalse(self.staging.exists())
        self.assert_untouched()

    def test_a_failed_stage_leaves_nothing_behind(self) -> None:
        with self.assertRaisesRegex(UpdateError, "unsafe"):
            stage_archive(archive(release(**{"app/../../evil.py": "x"})), self.dir)
        self.assertFalse(self.staging.exists())
        with self.assertRaisesRegex(UpdateError, "not a valid archive"):
            stage_archive(b"<html>rate limited</html>", self.dir)
        self.assertFalse(self.staging.exists())
        self.assert_untouched()

    def test_install_replaces_a_staged_update(self) -> None:
        stage_archive(archive(release("2.1.0")), self.dir)
        self.assertEqual(install_archive(archive(release("2.2.0")), self.dir), "2.2.0")
        self.assertFalse(self.staging.exists())
        self.assertIsNone(apply_staged(self.dir))
        self.assertIn('__version__ = "2.2.0"', self.read("app/__init__.py"))


class ExitTests(InstallDirTestCase):
    """main.py applies a staged update after shutdown and never fails the exit over it."""

    def test_applies_and_refreshes_the_installed_apps_entry(self) -> None:
        with (
            mock.patch.object(updates, "apply_staged", return_value="2.1.0") as apply,
            mock.patch("app.services.install_info.ensure_uninstall_entry") as ensure,
        ):
            main._apply_staged_update()
        install_dir = Path(main.__file__).resolve().parent
        apply.assert_called_once_with(install_dir)
        ensure.assert_called_once_with(install_dir, "2.1.0")

    def test_nothing_applied_leaves_the_entry_alone(self) -> None:
        with (
            mock.patch.object(updates, "apply_staged", return_value=None),
            mock.patch("app.services.install_info.ensure_uninstall_entry") as ensure,
        ):
            main._apply_staged_update()
        ensure.assert_not_called()

    def test_failures_never_escape(self) -> None:
        with (
            mock.patch.object(updates, "apply_staged", return_value="2.1.0"),
            mock.patch(
                "app.services.install_info.ensure_uninstall_entry", side_effect=RuntimeError("x")
            ),
            self.assertLogs(main.log, "ERROR"),
        ):
            main._apply_staged_update()

    def test_this_development_copy_is_never_updated_on_exit(self) -> None:
        with mock.patch.object(updates, "_swap") as swap:
            main._apply_staged_update()
        swap.assert_not_called()


class UpdateServiceTests(InstallDirTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.routes: dict[str, httpx.Response] = {}
        self.requests: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            return self.routes.get(str(request.url), httpx.Response(404))

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(self.http.aclose)

    def service(self, current: str = "2.0.0") -> UpdateService:
        return UpdateService(self.http, self.dir, current=current)

    async def test_check_finds_a_newer_version(self) -> None:
        self.routes[VERSION_URL] = httpx.Response(200, text='__version__ = "2.1.0"\n')
        info = await self.service().check()
        self.assertEqual((info.current, info.latest, info.available, info.installable), ("2.0.0", "2.1.0", True, True))

    async def test_check_when_up_to_date(self) -> None:
        self.routes[VERSION_URL] = httpx.Response(200, text='__version__ = "2.0.0"\n')
        self.assertFalse((await self.service().check()).available)

    async def test_check_before_2_0_is_published(self) -> None:
        with self.assertRaisesRegex(UpdateError, "AirGrabber 2"):
            await self.service().check()  # 404: app/__init__.py doesn't exist on GitHub yet

    async def test_check_offline(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
            with self.assertRaisesRegex(UpdateError, "reach GitHub"):
                await UpdateService(http, self.dir).check()

    async def test_install_downloads_and_swaps(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(200, content=archive(release()))
        steps: list[str] = []
        self.assertEqual(await self.service().install(progress=steps.append), "2.1.0")
        self.assertEqual(steps, ["Downloading update…", "Installing update…"])
        self.assertEqual(self.read("main.py"), "print('new main')\n")

    async def test_install_reports_http_errors(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(503)
        with self.assertRaisesRegex(UpdateError, "503"):
            await self.service().install()
        self.assert_untouched()

    async def test_install_refuses_oversized_downloads(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(200, content=archive(release()))
        with mock.patch.object(updates, "MAX_ARCHIVE_BYTES", 100):
            with self.assertRaisesRegex(UpdateError, "large"):
                await self.service().install()
        self.assert_untouched()

    async def test_development_copy_is_never_overwritten(self) -> None:
        (self.dir / "tests").mkdir()
        self.routes[VERSION_URL] = httpx.Response(200, text='__version__ = "2.1.0"\n')
        self.routes[ARCHIVE_URL] = httpx.Response(200, content=archive(release()))
        info = await self.service().check()
        self.assertTrue(info.available)
        self.assertFalse(info.installable)
        with self.assertRaisesRegex(UpdateError, "development copy"):
            await self.service().install()
        with self.assertRaisesRegex(UpdateError, "development copy"):
            await self.service().stage()
        self.assertNotIn(ARCHIVE_URL, self.requests)
        self.assertFalse((self.dir / STAGING_DIR).exists())
        self.assertIsNone(apply_staged(self.dir))
        self.assert_untouched()

    async def test_stage_downloads_and_unpacks_without_swapping(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(200, content=archive(release()))
        with self.assertLogs(updates.log, "INFO"):
            self.assertEqual(await self.service().stage(), "2.1.0")
        self.assert_untouched()
        self.assertTrue((self.dir / STAGING_DIR / READY_FILE).exists())
        self.assertEqual(apply_staged(self.dir), "2.1.0")
        self.assertEqual(self.read("main.py"), "print('new main')\n")

    async def test_stage_refuses_a_download_that_is_not_newer(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(200, content=archive(release("2.0.0")))
        with self.assertRaisesRegex(UpdateError, "not newer"):
            await self.service().stage()
        self.assertFalse((self.dir / STAGING_DIR).exists())

    async def test_stage_reports_http_errors(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(503)
        with self.assertRaisesRegex(UpdateError, "503"):
            await self.service().stage()
        self.assertFalse((self.dir / STAGING_DIR).exists())

    async def test_stage_reports_file_errors_as_update_errors(self) -> None:
        self.routes[ARCHIVE_URL] = httpx.Response(200, content=archive(release()))
        with (
            mock.patch.object(updates, "_extract", side_effect=PermissionError("denied")),
            self.assertLogs(updates.log, "WARNING"),
            self.assertRaisesRegex(UpdateError, "unpack"),
        ):
            await self.service().stage()
        self.assertFalse((self.dir / STAGING_DIR).exists())

    async def test_register_install_adds_the_installed_apps_entry(self) -> None:
        registry = FakeRegistry()
        service = UpdateService(self.http, self.dir, current="2.0.0", registry=registry)
        with self.assertLogs("app.services.install_info", "INFO"):
            await service.register_install()
        entry = registry.keys[UNINSTALL_KEY]
        self.assertEqual(entry["DisplayVersion"], "2.0.0")
        self.assertEqual(entry["InstallLocation"], str(self.dir))

    async def test_register_install_skips_a_development_copy(self) -> None:
        (self.dir / "tests").mkdir()
        registry = FakeRegistry()
        await UpdateService(self.http, self.dir, registry=registry).register_install()
        self.assertEqual(registry.keys, {})
        self.assertEqual(registry.writes, [])


class DevelopmentCopyTests(unittest.TestCase):
    def test_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertFalse(is_development_copy(root))
            (root / ".git").mkdir()
            self.assertTrue(is_development_copy(root))
        self.assertTrue(is_development_copy(updates.INSTALL_DIR))  # this checkout has tests/


if __name__ == "__main__":
    unittest.main()
