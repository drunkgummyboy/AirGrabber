"""Copy the publishable files from this folder into a git clone of the GitHub repo.

    python -m tools.sync_repo PATH_TO_CLONE

The development folder also holds 1.x data (settings.json has API keys) and scratch files, so only
PUBLISHED is copied; directories are mirrored (files deleted here are deleted there). Everything else
in the clone, such as the 1.x files that old copies still download, is left alone. Review with
`git status` / `git diff` before committing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
PUBLISHED_FILES = (
    ".gitattributes",
    ".gitignore",
    "CLAUDE.md",
    "README.md",
    "filemap.md",
    "install.ps1",
    "main.py",
    "mypy.ini",
    "requirements.txt",
)
PUBLISHED_DIRS = ("app", "tests", "tools")
_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".mypy_cache")


def sync(clone: Path, source: Path = SOURCE) -> None:
    if not (clone / ".git").exists():
        raise SystemExit(f"{clone} is not a git clone")
    for name in PUBLISHED_FILES:
        shutil.copy2(source / name, clone / name)
    for name in PUBLISHED_DIRS:
        target = clone / name
        if target.exists():
            shutil.rmtree(target, onexc=_clear_read_only)
        # copyfile, not copy2: Google Drive marks folders read-only and copystat would carry that over.
        shutil.copytree(source / name, target, ignore=_IGNORED, copy_function=shutil.copyfile)
        for folder, _dirs, _files in os.walk(target):
            os.chmod(folder, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


def _clear_read_only(function: Callable[[str], object], path: str, _exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("clone", type=Path, help="git clone of github.com/drunkgummyboy/AirGrabber")
    args = parser.parse_args(argv)
    sync(args.clone)
    print(f"Copied the publishable files into {args.clone}; review with git status before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
