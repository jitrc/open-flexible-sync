"""Bounded scans: each set is complete for its root names, so it may be applied."""

from __future__ import annotations

import os
from pathlib import Path

import ofs.cache as cache_module
from ofs.compare import Action
from ofs.model import Profile, SyncMode
from ofs.scan import root_names, scan_profile


def _write(path: Path, text: str, mtime: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def _pair(tmp_path: Path, **kwargs) -> Profile:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir(exist_ok=True)
    right.mkdir(exist_ok=True)
    return Profile(name="set scan", left=str(left), right=str(right), **kwargs)


def test_root_names_covers_both_sides_and_sorts_them(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "beta" / "b.txt", "b")
    _write(left / "alpha.txt", "a")
    _write(right / "gamma" / "c.txt", "c")

    assert root_names(left, right) == ["alpha.txt", "beta", "gamma"]


def test_sets_cover_every_root_name_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    for name in ("one", "two", "three"):
        _write(tmp_path / "left" / name / "file.txt", name)
    _write(tmp_path / "left" / "root.txt", "root")

    first = scan_profile(profile, set_limit=2)
    assert (first.set_first, first.set_last, first.set_next, first.set_total) == ("one", "root.txt", "root.txt", 4)
    assert [change.rel for change in first.changes] == ["one/file.txt", "root.txt"]

    second = scan_profile(profile, set_limit=2, after_name=first.set_next)
    assert (second.set_first, second.set_last, second.set_next) == ("three", "two", "")
    assert sorted(change.rel for change in second.changes) == ["three/file.txt", "two/file.txt"]

    covered = [change.rel for change in first.changes] + [change.rel for change in second.changes]
    assert sorted(covered) == ["one/file.txt", "root.txt", "three/file.txt", "two/file.txt"]
    assert len(covered) == len(set(covered))


def test_each_set_is_a_complete_plan_for_its_names(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path, mode=SyncMode.EXACT_MIRROR)
    _write(tmp_path / "left" / "keep" / "same.txt", "same")
    _write(tmp_path / "right" / "keep" / "same.txt", "same")
    _write(tmp_path / "right" / "keep" / "extra.txt", "extra")
    _write(tmp_path / "right" / "other" / "extra.txt", "extra")

    first = scan_profile(profile, set_limit=1)
    assert first.set_first == "keep"
    assert [(change.rel, change.action) for change in first.changes] == [("keep/extra.txt", Action.DELETE_RIGHT)]

    second = scan_profile(profile, set_limit=1, after_name=first.set_next)
    assert second.set_first == "other"
    assert [(change.rel, change.action) for change in second.changes] == [("other/extra.txt", Action.DELETE_RIGHT)]
    assert second.set_next == ""


def test_a_set_never_uses_or_saves_the_destination_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path, cache_destination=True)
    _write(tmp_path / "left" / "one" / "a.txt", "a")
    _write(tmp_path / "right" / "one" / "a.txt", "a")

    result = scan_profile(profile, set_limit=1)

    assert result.cached is False
    assert "Set scan" in result.note
    assert not cache_module.cache_path(profile.name).exists()


def test_a_set_never_replaces_the_file_history_baseline(tmp_path, monkeypatch):
    from ofs.history import load_history

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path, track=True)
    _write(tmp_path / "left" / "one" / "a.txt", "a")

    result = scan_profile(profile, set_limit=1)

    assert load_history(profile) == (None, "")
    assert "Set scan" in result.note


def test_excluded_names_do_not_produce_empty_sets(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path, exclusions=["hidden"])
    _write(tmp_path / "left" / "aaa" / "a.txt", "a")
    _write(tmp_path / "left" / "hidden" / "secret.txt", "s")
    _write(tmp_path / "left" / "zzz" / "z.txt", "z")

    first = scan_profile(profile, set_limit=1)
    assert [change.rel for change in first.changes] == ["aaa/a.txt"]
    second = scan_profile(profile, set_limit=1, after_name=first.set_next)
    assert [change.rel for change in second.changes] == ["zzz/z.txt"]
    assert second.set_next == ""


def test_a_set_limit_of_zero_keeps_one_complete_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "one" / "a.txt", "a")
    _write(tmp_path / "left" / "two" / "b.txt", "b")

    result = scan_profile(profile)

    assert result.set_last == "" and result.set_next == ""
    assert sorted(change.rel for change in result.changes) == ["one/a.txt", "two/b.txt"]


def test_a_root_level_socket_never_fails_a_set_scan_and_is_ignored(tmp_path, monkeypatch):
    """A unix socket at the root must not abort the scan, and a set must ignore it like `walk` does."""

    import socket

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "keep" / "a.txt", "a")
    _write(tmp_path / "left" / "other" / "b.txt", "b")
    sock_path = tmp_path / "left" / "ipc.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))

    try:
        full = scan_profile(profile)
        first = scan_profile(profile, set_limit=1)
        second = scan_profile(profile, set_limit=1, after_name=first.set_next)
    finally:
        server.close()

    assert full.errors == [] and first.errors == [] and second.errors == []
    assert "ipc.sock" not in {change.rel for change in full.changes}
    seen = {change.rel for change in first.changes} | {change.rel for change in second.changes}
    assert "ipc.sock" not in seen
    assert seen == {"keep/a.txt", "other/b.txt"}


def test_a_pipe_inside_a_folder_is_ignored_by_sets_and_full_scans(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _pair(tmp_path)
    _write(tmp_path / "left" / "area" / "a.txt", "a")
    os.mkfifo(tmp_path / "left" / "area" / "pipe")

    full = scan_profile(profile)
    one = scan_profile(profile, set_limit=1)

    assert [change.rel for change in full.changes] == ["area/a.txt"]
    assert [change.rel for change in one.changes] == ["area/a.txt"]
