"""A folder that cannot be read has unknown contents, never empty contents."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import ofs.cache as cache_module
from ofs.apply import apply_plan
from ofs.compare import Action, Change
from ofs.model import Profile, SyncMode
from ofs.scan import scan_profile


def _write(path: Path, text: str, mtime: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder, so permissions cannot block it")


def _closed(path: Path) -> None:
    """Make a folder unreadable for the duration of one test."""

    path.chmod(0)


def _open(path: Path) -> None:
    path.chmod(0o755)


def _pair(tmp_path: Path, **kwargs) -> Profile:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir(exist_ok=True)
    right.mkdir(exist_ok=True)
    return Profile(name="unreadable", left=str(left), right=str(right), **kwargs)


def test_an_unreadable_destination_folder_blocks_copies_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    (tmp_path / "right" / "area").mkdir()
    _closed(tmp_path / "right" / "area")

    try:
        result = scan_profile(profile)
    finally:
        _open(tmp_path / "right" / "area")

    assert [(side, rel) for side, rel, _message in result.unreadable] == [("Right", "area")]
    assert [(change.rel, change.action) for change in result.changes] == [("area/a.txt", Action.ASK)]
    assert "contents unknown" in result.changes[0].reason
    assert "could not be read, so the contents are unknown" in result.note


def test_an_unreadable_source_folder_never_plans_destination_deletions(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path, mode=SyncMode.EXACT_MIRROR)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    _write(tmp_path / "right" / "area" / "a.txt", "a")
    _closed(tmp_path / "left" / "area")

    try:
        result = scan_profile(profile)
    finally:
        _open(tmp_path / "left" / "area")

    assert [(side, rel) for side, rel, _message in result.unreadable] == [("Left", "area")]
    assert [(change.rel, change.action) for change in result.changes] == [("area/a.txt", Action.ASK)]


def test_a_readable_neighbour_keeps_its_own_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    _write(tmp_path / "left" / "fine" / "b.txt", "b")
    (tmp_path / "right" / "area").mkdir()
    _closed(tmp_path / "right" / "area")

    try:
        result = scan_profile(profile)
    finally:
        _open(tmp_path / "right" / "area")

    actions = {change.rel: change.action for change in result.changes}
    assert actions == {"area/a.txt": Action.ASK, "fine/b.txt": Action.COPY_LEFT_TO_RIGHT}


def test_an_unreadable_folder_never_saves_a_destination_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path, cache_destination=True)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    _write(tmp_path / "right" / "fine" / "b.txt", "b")
    (tmp_path / "right" / "area").mkdir()
    _closed(tmp_path / "right" / "area")

    try:
        result = scan_profile(profile)
    finally:
        _open(tmp_path / "right" / "area")

    assert not cache_module.cache_path(profile.name).exists()
    assert "was not saved because a folder could not be read" in result.note


def test_a_set_scan_reports_an_unreadable_root_name(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    _write(tmp_path / "right" / "fine" / "b.txt", "b")
    (tmp_path / "right" / "area").mkdir()
    _closed(tmp_path / "right" / "area")

    try:
        result = scan_profile(profile, set_limit=1)
    finally:
        _open(tmp_path / "right" / "area")

    assert [(side, rel) for side, rel, _message in result.unreadable] == [("Right", "area")]
    assert [(change.rel, change.action) for change in result.changes] == [("area/a.txt", Action.ASK)]


def test_copy_reports_one_error_for_a_folder_it_cannot_open(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    _write(tmp_path / "left" / "area" / "b.txt", "b")
    # A real file where the destination folder belongs makes every copy under it fail.
    _write(tmp_path / "right" / "area", "not a folder")
    changes = [
        Change("area/a.txt", Action.COPY_LEFT_TO_RIGHT, "only on source", 1),
        Change("area/b.txt", Action.COPY_LEFT_TO_RIGHT, "only on source", 1),
    ]

    report = apply_plan(profile, changes)

    assert len(report.errors) == 1, report.errors
    assert report.errors[0].startswith("area/: ")
    assert "1 more entry inside this folder was skipped. (right)" in report.errors[0]
    assert report.skipped == 1
