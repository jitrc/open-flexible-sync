"""A scan that stops making progress must say so instead of looking hung."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ofs.model import Profile
from ofs.scan import scan_profile
from ofs.settings import scan_stall_minutes, set_scan_stall_minutes
from ofs.walk import WalkStalled, walk


def _write(path: Path, text: str, mtime: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def _pair(tmp_path: Path) -> Profile:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir(exist_ok=True)
    right.mkdir(exist_ok=True)
    return Profile(name="stall", left=str(left), right=str(right))


def test_the_stall_limit_defaults_to_five_minutes_and_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))

    assert scan_stall_minutes() == 5

    set_scan_stall_minutes(0)
    assert scan_stall_minutes() == 0

    set_scan_stall_minutes(12)
    assert scan_stall_minutes() == 12


def test_a_walk_without_progress_raises_walk_stalled(tmp_path, monkeypatch):
    left = tmp_path / "left"
    _write(left / "area" / "a.txt", "a")

    # The clock only moves when the walk reads a new folder, so every entry looks
    # like it happened long ago and the guard fires on the first check.
    ticks = iter(float(index * 100) for index in range(1000))
    monkeypatch.setattr("ofs.walk.time.monotonic", lambda: next(ticks))

    with pytest.raises(WalkStalled) as caught:
        walk(left, lambda rel: False, stall_seconds=60)

    assert caught.value.rel
    assert caught.value.seconds == 60


def test_a_stalled_side_becomes_a_review_only_scan_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")

    real_walk = __import__("ofs.walk", fromlist=["walk"]).walk
    calls = {"n": 0}

    def stalling_walk(root, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WalkStalled("area/deeper", 300)
        return real_walk(root, *args, **kwargs)

    monkeypatch.setattr("ofs.scan.walk", stalling_walk)

    result = scan_profile(profile)

    assert result.partial_sides == ("Left",)
    assert "no progress for 5 minute(s) at area/deeper" in result.note
    assert "review only" in result.note
    assert result.changes == []


def test_a_stall_on_the_destination_side_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    calls = {"n": 0}
    real_walk = __import__("ofs.walk", fromlist=["walk"]).walk

    def walk_then_stall(root, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_walk(root, *args, **kwargs)
        raise WalkStalled("shared/mount", 420)

    monkeypatch.setattr("ofs.scan.walk", walk_then_stall)

    result = scan_profile(profile)

    assert result.partial_sides == ("Right",)
    assert "the right side made no progress for 7 minute(s) at shared/mount" in result.note
