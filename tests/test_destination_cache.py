import json
import os
from pathlib import Path

import ofs.cache as cache_module
import ofs.scan as scan_module
from ofs.apply import apply_plan
from ofs.cache import destination_sides, invalidate_destination, load_destination, save_destination
from ofs.compare import Action, Change, FileStat
from ofs.model import Direction, ExclusionMode, Profile
from ofs.scan import invalidate_cached_destinations, scan_profile
from ofs.store import config_dir, save_profile


def _profile(tmp_path: Path, **kwargs):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    return Profile(name="cache test", left=str(left), right=str(right), **kwargs)


def test_destination_cache_is_opt_in_and_preserves_full_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path)
    assert profile.cache_destination is False
    assert load_destination(profile, "Right", [], ExclusionMode.IGNORE_TOTALLY) is None
    profile.cache_destination = True
    info = FileStat(42, 1234.5, kind="symlink", target="relative-target", mode=0o640, uid=12, gid=34)
    save_destination(profile, "Right", [], profile.exclusion_mode, {"folder/a": info})
    loaded = load_destination(profile, "Right", [], profile.exclusion_mode)
    assert loaded is not None and loaded[0] == {"folder/a": info}
    document = json.loads(cache_module._path(profile).read_text())
    assert document["root"] == profile.right
    assert document["version"] == 1
    assert document["entries"][0]["target"] == "relative-target"
    assert Profile.from_json(profile.to_json()).cache_destination is True


def test_cache_persists_for_months_and_rejects_invalid_manifests(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    save_destination(profile, "Right", [], profile.exclusion_mode, {"a": FileStat(1, 1)})
    path = cache_module._path(profile)
    document = json.loads(path.read_text())
    months_later = document["saved"] + 183 * 86400
    monkeypatch.setattr(cache_module.time, "time", lambda: months_later)
    cached = load_destination(profile, "Right", [], profile.exclusion_mode)
    assert cached is not None and cached == ({"a": FileStat(1, 1)}, document["saved"])
    document["saved"] = float("nan")
    path.write_text(json.dumps(document))
    assert load_destination(profile, "Right", [], profile.exclusion_mode) is None
    path.write_text("{not json")
    assert load_destination(profile, "Right", [], profile.exclusion_mode) is None
    save_destination(profile, "Right", [], profile.exclusion_mode, {"a": FileStat(1, 1)})
    assert load_destination(profile, "Right", [], profile.exclusion_mode) is not None
    profile.exclusions.append("changed-exclusion")
    assert load_destination(profile, "Right", [], profile.exclusion_mode) is None
    invalidate_destination(profile)
    assert list((config_dir() / "cache").glob(".ofs-cache-*.part")) == []


def test_repeated_scan_uses_only_cached_destination_and_always_reads_source(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    (tmp_path / "left" / "a").write_text("new")
    (tmp_path / "right" / "a").write_text("old")
    os.utime(tmp_path / "left" / "a", (200, 200))
    os.utime(tmp_path / "right" / "a", (100, 100))
    calls = []
    actual_walk = scan_module.walk

    def counted_walk(root, *args, **kwargs):
        calls.append(root)
        return actual_walk(root, *args, **kwargs)

    monkeypatch.setattr(scan_module, "walk", counted_walk)
    first = scan_profile(profile)
    assert first.cached is False
    assert [item.rel for item in first.changes] == ["a"]
    assert calls == [tmp_path / "left", tmp_path / "right"]
    calls.clear()
    second = scan_profile(profile)
    assert calls == [tmp_path / "left"]
    assert second.cached is True
    assert "cached right destination" in second.note and "Run uncached" in second.note
    scan_profile(profile, force_uncached=True)
    assert calls == [tmp_path / "left", tmp_path / "left", tmp_path / "right"]


def test_live_entry_events_include_cached_metadata_and_skip_dynamic_session_masks(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    (tmp_path / "left" / "folder").mkdir()
    (tmp_path / "right" / "folder").mkdir()
    (tmp_path / "left" / "folder" / "same.txt").write_text("same")
    (tmp_path / "right" / "folder" / "same.txt").write_text("same")
    seen = []
    first = scan_profile(profile, on_entry=lambda side, rel, stat: seen.append((side, rel, stat.size)))
    assert ("Left", "folder/same.txt", 4) in seen
    assert ("Right", "folder/same.txt", 4) in seen
    seen.clear()
    second = scan_profile(profile, on_entry=lambda side, rel, stat: seen.append((side, rel, stat.size)))
    assert second.cached is True
    assert seen == [("Left", "folder/same.txt", 4), ("Right", "folder/same.txt", 4)]

    (tmp_path / "left" / "folder" / "ignore.txt").write_text("skip")
    seen.clear()
    third = scan_profile(profile, session_masks=["folder"], on_entry=lambda side, rel, stat: seen.append((side, rel, stat.size)))
    assert seen == []
    assert third.listing == [] and third.changes == []


def test_uncached_cli_scan_refreshes_manifest(tmp_path, monkeypatch, capsys):
    from ofs.cli import main

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    (tmp_path / "right" / "a").write_text("old")
    save_profile(profile)
    scan_profile(profile)
    manifest = cache_module._path(profile)
    fresh_time = cache_module.time.time() + 1000
    monkeypatch.setattr(cache_module.time, "time", lambda: fresh_time)
    assert main(["scan", "cache test", "--uncached"]) == 0
    assert "Using cached" not in capsys.readouterr().out
    assert json.loads(manifest.read_text())["saved"] == fresh_time


def test_apply_invalidates_before_write_but_not_for_ask_or_prework_cancel(tmp_path, monkeypatch):
    import ofs.apply as apply_module_aliased
    from ofs.apply import RunControl

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    source = tmp_path / "left" / "a"
    target = tmp_path / "right" / "a"
    source.write_text("new")
    target.write_text("old")
    save_destination(profile, "Right", [], profile.exclusion_mode, {})
    manifest = cache_module._path(profile)
    events = []
    invalidate = apply_module_aliased.invalidate_destination

    def before_write(current_profile):
        assert target.read_text() == "old"
        assert manifest.exists()
        events.append(current_profile.name)
        invalidate(current_profile)

    monkeypatch.setattr(apply_module_aliased, "invalidate_destination", before_write)
    report = apply_plan(profile, [Change("a", Action.COPY_LEFT_TO_RIGHT, "target changed", 3)])
    assert report.copied == 1 and not report.errors
    assert events == [profile.name]
    assert target.read_text() == "new"
    assert not manifest.exists()

    save_destination(profile, "Right", [], profile.exclusion_mode, {})
    ignored = apply_plan(profile, [Change("a", Action.ASK, "undecided", 3)])
    assert ignored.skipped == 1 and manifest.exists()
    control = RunControl()
    control.stop()
    canceled = apply_plan(profile, [Change("a", Action.COPY_LEFT_TO_RIGHT, "canceled before copy", 3)], control=control)
    assert canceled.cancelled and manifest.exists()


def test_invalidation_after_copy_removes_completed_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    (tmp_path / "left" / "a").write_text("new")
    (tmp_path / "right" / "a").write_text("old")
    save_destination(profile, "Right", [], profile.exclusion_mode, {})
    report = apply_plan(profile, [Change("a", Action.COPY_LEFT_TO_RIGHT, "new", 3)])
    assert report.copied == 1 and not report.errors
    invalidate_cached_destinations(profile)
    assert not cache_module._path(profile).exists()


def test_cached_destination_stats_show_what_is_being_used_not_a_dot(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, cache_destination=True)
    seen = []
    scan_profile(profile)  # Build the fresh manifest; the next scan reuses it.
    scan_profile(profile, on_stats=lambda side, rel, values: seen.append((side, rel)))
    assert seen[-1][0] == "Right"
    assert seen[-1][1].startswith("Using cached destination metadata (")
    assert seen[-1][1].endswith("s old)")


def test_destination_selection_supports_each_direction():
    left = Profile(name="left to right", left="l", right="r")
    right = Profile(name="right to left", left="l", right="r", direction=Direction.RIGHT_TO_LEFT)
    both = Profile(name="both", left="l", right="r", direction=Direction.BOTH)
    assert destination_sides(left) == ("Right",)
    assert destination_sides(right) == ("Left",)
    assert destination_sides(both) == ("Left", "Right")
