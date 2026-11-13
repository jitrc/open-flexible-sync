#!/usr/bin/env python3
"""Set the release version in every file that hardcodes it.

Usage: python3 scripts/bump_version.py 0.4.8

Updates pyproject.toml, src/ofs/__init__.py, and the download names in
README.md. The CHANGELOG heading stays manual because release notes are
written by hand. `tests/test_version.py` fails if any of these drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _replace(path: Path, pattern: str, replacement: str, *, count: int = 0) -> int:
    text = path.read_text(encoding="utf-8")
    updated, changes = re.subn(pattern, replacement, text, count=count, flags=re.M)
    if changes:
        path.write_text(updated, encoding="utf-8")
    return changes


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not VERSION.match(argv[1]):
        raise SystemExit("usage: bump_version.py X.Y.Z")
    new = argv[1]

    pyproject = ROOT / "pyproject.toml"
    if not _replace(pyproject, r'^version = "[^"]+"', f'version = "{new}"', count=1):
        raise SystemExit("pyproject.toml has no version line")

    init = ROOT / "src" / "ofs" / "__init__.py"
    if not _replace(init, r'^__version__ = "[^"]+"', f'__version__ = "{new}"', count=1):
        raise SystemExit("src/ofs/__init__.py has no __version__ line")

    readme = ROOT / "README.md"
    _replace(readme, r"OFS-\d+\.\d+\.\d+", f"OFS-{new}")
    _replace(readme, r"releases/download/v\d+\.\d+\.\d+", f"releases/download/v{new}")

    print(f"version set to {new} in pyproject.toml, src/ofs/__init__.py, and README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
