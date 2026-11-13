"""Guards against release-version drift between the code, packaging, and README."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from ofs import __version__

ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_package_version_matches_pyproject():
    assert __version__ == _project_version()


def test_readme_downloads_name_the_current_version():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    version = _project_version()
    assert f"OFS-{version}-x86_64.AppImage" in text
    assert f"OFS-{version}-linux-x86_64.tar.gz" in text
    assert f"releases/download/v{version}/" in text


def test_changelog_has_a_section_for_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = _project_version()
    start = re.search(rf"^## \[{re.escape(version)}\][^\n]*$", text, re.M)
    assert start is not None, f"CHANGELOG.md has no '## [{version}]' section"
    rest = text[start.end():]
    following = re.search(r"^## \[", rest, re.M)
    body = rest[: following.start()] if following else rest
    assert body.strip(), f"CHANGELOG.md section {version} is empty"
