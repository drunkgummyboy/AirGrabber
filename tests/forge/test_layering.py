"""Forge's layers (ported from MediaForge's tests/test_layering.py, plus AirGrabber's rules).

- Non-UI Forge code never imports Tk (it must stay importable and testable without a display).
- `app/forge/ui` never imports `app.forge.db`, `app.database` or httpx (it gets services).
- Nothing imports tkinterdnd2 or aiohttp; guessit only inside `app/forge/core/parser.py`'s
  functions.
- AirGrabber's start (`import app.services`) loads neither Forge's window, its services nor
  guessit.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

from tests.forge.base import ForgeTestCase

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
FORGE = APP / "forge"
UI = FORGE / "ui"
TK = ("tkinter", "customtkinter", "async_tkinter_loop", "tkinterdnd2", "app.forge.ui", "app.ui")
UI_FORBIDDEN = ("app.forge.db", "app.database", "httpx")
NOWHERE = ("tkinterdnd2", "aiohttp")
PARSER = FORGE / "core" / "parser.py"


def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, anywhere in it (also inside functions)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def module_level_imports(path: Path) -> set[str]:
    """The modules a file imports when it is imported (not inside a function)."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        else:
            pending.extend(ast.iter_child_nodes(node))
    return names


def _hits(name: str, forbidden: tuple[str, ...]) -> bool:
    return any(name == f or name.startswith(f + ".") for f in forbidden)


def _offenders(files: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{file.relative_to(ROOT).as_posix()} imports {name}"
        for file in files
        for name in sorted(imported_modules(file))
        if _hits(name, forbidden)
    ]


def _python_files(folder: Path) -> list[Path]:
    return sorted(folder.rglob("*.py"))


class LayeringTest(ForgeTestCase):
    def test_non_ui_forge_code_never_imports_tk(self) -> None:
        files = [f for f in _python_files(FORGE) if UI not in f.parents]
        self.assertTrue(files)
        self.assertEqual(_offenders(files, TK), [])

    def test_the_forge_window_never_reaches_the_database_or_httpx(self) -> None:
        self.assertEqual(_offenders(_python_files(UI), UI_FORBIDDEN), [])

    def test_nothing_imports_tkinterdnd2_or_aiohttp(self) -> None:
        files = [*_python_files(APP), ROOT / "main.py"]
        self.assertEqual(_offenders(files, NOWHERE), [])

    def test_guessit_only_inside_the_parsers_functions(self) -> None:
        files = [*_python_files(APP), ROOT / "main.py"]
        parser = PARSER.relative_to(ROOT).as_posix()
        elsewhere = [f for f in _offenders(files, ("guessit",)) if not f.startswith(parser)]
        self.assertEqual(elsewhere, [])
        self.assertNotIn("guessit", module_level_imports(PARSER))

    def test_the_guard_actually_detects_imports(self) -> None:
        sample = self.tmp / "x.py"
        sample.write_text(
            "import customtkinter\n"
            "from app.forge.ui import thing\n"
            "def later():\n"
            "    import guessit\n",
            encoding="utf-8",
        )
        found = imported_modules(sample)
        self.assertLessEqual({"customtkinter", "app.forge.ui", "guessit"}, found)
        self.assertNotIn("guessit", module_level_imports(sample))
        self.assertIn("customtkinter", module_level_imports(sample))


class StartupImportsTest(unittest.TestCase):
    def test_airgrabber_services_load_no_forge_window_services_or_guessit(self) -> None:
        probe = (
            "import sys\n"
            "import app.services\n"
            "loaded = [m for m in sys.modules if m == 'guessit' or m.startswith('guessit.')\n"
            "          or m.startswith('app.forge.ui') or m == 'app.forge.services']\n"
            "print(','.join(sorted(loaded)))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip(), "")
