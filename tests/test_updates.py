"""Self-update: version check, archive validation, swapping the app files in (and rolling back)."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx

from app.services import updates
from app.services.updates import (
    ARCHIVE_URL,
    VERSION_URL,
    UpdateError,
    UpdateService,
    install_archive,
    is_development_copy,
    is_newer,
    parse_version,
)

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
        info = await self.service().check()
        self.assertTrue(info.available)
        self.assertFalse(info.installable)
        with self.assertRaisesRegex(UpdateError, "development copy"):
            await self.service().install()
        self.assertNotIn(ARCHIVE_URL, self.requests)
        self.assert_untouched()


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
