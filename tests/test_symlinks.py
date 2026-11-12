import os
import sqlite3
import stat

import ofs.apply as apply_module
import ofs.archive as archive_module
from ofs.apply import apply_plan
from ofs.archive import older_files, restore_version
from ofs.compare import Action, Change, FileStat, build_plan
from ofs.review import Listed
from ofs.history import database_path, load_history, save_history
from ofs.model import Direction, Profile, SymlinkMode, SyncMode
from ofs.walk import walk


def _profile(tmp_path, **kwargs):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    return Profile(name="links", left=str(left), right=str(right), **kwargs)


def test_walk_preserves_file_and_directory_links_without_following(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.PRESERVE)
    root = tmp_path / "left"
    (root / "payload").write_text("content")
    (root / "linked file").symlink_to("payload")
    (root / "linked directory").symlink_to(".", target_is_directory=True)
    found = walk(root, lambda _rel: False, symlinks=profile.symlinks.value)
    assert found["linked file"].kind == "symlink"
    assert found["linked file"].target == "payload"
    assert found["linked file"].size == len("payload")
    assert found["linked directory"].kind == "symlink"
    assert "linked directory/payload" not in found
    assert stat.S_IMODE(os.lstat(root / "payload").st_mode) == found["payload"].mode


def test_follow_has_cycle_protection_and_dangling_sources_are_undecided(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.FOLLOW, mode=SyncMode.EXACT_MIRROR)
    root = tmp_path / "left"
    (root / "payload").write_text("content")
    (root / "loop").symlink_to(".", target_is_directory=True)
    (root / "missing").symlink_to("does-not-exist")
    (tmp_path / "right" / "missing").write_text("existing destination")
    found = walk(root, lambda _rel: False, symlinks="follow")
    assert "payload" in found and "loop/payload" not in found
    assert found["loop"].kind == "unsupported"
    cycle = build_plan(profile, {"loop": found["loop"]}, {"loop": FileStat(21, 2)})
    assert [item.action for item in cycle] == [Action.ASK]
    assert found["missing"].kind == "broken"
    plan = build_plan(profile, {"missing": found["missing"]}, {"missing": FileStat(21, 2)})
    assert [(item.action, item.reason) for item in plan] == [(Action.ASK, "source link is dangling or has an unsupported target")]
    skipped = walk(root, lambda _rel: False, symlinks="skip")
    assert set(skipped) == {"payload"}


def test_follow_copies_contents_of_a_link_inside_the_profile(tmp_path):
    profile = _profile(tmp_path)
    source = tmp_path / "left" / "payload"
    source.write_text("linked data")
    (tmp_path / "left" / "alias").symlink_to("payload")
    found = walk(tmp_path / "left", lambda _rel: False, symlinks="follow")
    assert found["alias"].kind == "file"
    report = apply_plan(profile, [Change("alias", Action.COPY_LEFT_TO_RIGHT, "new", 11)])
    assert report.copied == 1 and not report.errors
    assert not (tmp_path / "right" / "alias").is_symlink()
    assert (tmp_path / "right" / "alias").read_text() == "linked data"


def test_preserve_copies_file_and_directory_link_objects_and_archives_destination(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.PRESERVE, keep_older=True)
    left = tmp_path / "left"
    right = tmp_path / "right"
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be modified")
    (left / "actual").mkdir()
    (left / "actual" / "inside").write_text("payload")
    (left / "external link").symlink_to(outside)
    (left / "directory link").symlink_to("actual", target_is_directory=True)
    (right / "external link").write_text("old destination")
    report = apply_plan(profile, [
        Change("external link", Action.COPY_LEFT_TO_RIGHT, "new", len(str(outside))),
        Change("directory link", Action.COPY_LEFT_TO_RIGHT, "new", len("actual")),
    ])
    assert report.copied == 2 and not report.errors
    assert os.readlink(right / "external link") == str(outside)
    assert os.readlink(right / "directory link") == "actual"
    assert (right / "external link").read_text() == outside.read_text()
    assert outside.read_text() == "must not be modified"
    assert (right / "Older" / "external link").read_text() == "old destination"


def test_preserved_link_delete_and_restore_never_touch_the_target(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.PRESERVE, keep_deleted=True, mode=SyncMode.EXACT_MIRROR)
    right = tmp_path / "right"
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    (right / "delete link").symlink_to(outside)
    plan = build_plan(profile, {}, {"delete link": FileStat(len(str(outside)), 1, kind="symlink", target=str(outside))})
    assert plan[0].action == Action.DELETE_RIGHT
    report = apply_plan(profile, plan, listing=[type("Row", (), {"change": plan[0], "on_left": False, "on_right": True})()])
    assert report.deleted == 1 and report.deletions_archived == 1
    assert outside.read_text() == "untouched"
    archived = older_files(profile)
    assert len(archived) == 1 and archived[0].path.is_symlink()
    restored = restore_version(archived[0], tmp_path / "recovery")
    assert os.readlink(restored) == str(outside)
    assert outside.read_text() == "untouched"


def test_preserve_owner_failure_keeps_old_destination_and_cleans_temp(tmp_path, monkeypatch):
    profile = _profile(tmp_path, preserve_ownership=True)
    (tmp_path / "left" / "a.txt").write_text("new")
    (tmp_path / "right" / "a.txt").write_text("old")
    called = []

    def deny(path, uid, gid, follow_symlinks):
        called.append((path, uid, gid, follow_symlinks))
        raise PermissionError("ownership not permitted")

    monkeypatch.setattr(apply_module.os, "chown", deny)
    report = apply_plan(profile, [Change("a.txt", Action.COPY_LEFT_TO_RIGHT, "new", 3)])
    assert called and report.copied == 0
    assert len(report.errors) == 1 and "not permitted" in report.errors[0]
    assert (tmp_path / "right" / "a.txt").read_text() == "old"
    assert list(tmp_path.rglob(".ofs-*.part")) == []


def test_mirror_move_never_pairs_different_link_targets_or_file_types():
    profile = Profile(name="move", left="l", right="r", symlinks=SymlinkMode.PRESERVE, mode=SyncMode.EXACT_MIRROR)
    plan = build_plan(
        profile,
        {"new": FileStat(5, 4, kind="symlink", target="first")},
        {"old": FileStat(5, 4, kind="symlink", target="other")},
    )
    assert [(change.rel, change.action) for change in plan] == [
        ("new", Action.COPY_LEFT_TO_RIGHT),
        ("old", Action.DELETE_RIGHT),
    ]
    plan = build_plan(profile, {"new": FileStat(5, 4)}, {"old": FileStat(5, 4, kind="symlink", target="items")})
    assert all(change.action != Action.MOVE_ON_RIGHT for change in plan)


def test_file_directory_link_conflicts_are_refused_before_any_change(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.PRESERVE, mode=SyncMode.EXACT_MIRROR)
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "d").mkdir()
    (left / "d" / "file.txt").write_text("wanted content")
    (right / "actual").mkdir()
    (right / "actual" / "file.txt").write_text("old target content")
    (right / "d").symlink_to("actual", target_is_directory=True)
    plan = build_plan(profile, {"d/file.txt": FileStat(14, 5)}, {"d": FileStat(6, 1, kind="symlink", target="actual")})
    report = apply_plan(profile, plan)
    assert report.copied == report.deleted == 0 and report.errors
    assert (right / "d").is_symlink()
    assert (right / "actual" / "file.txt").read_text() == "old target content"


def test_source_link_and_real_destination_directory_are_refused_before_cleanup(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.PRESERVE, mode=SyncMode.EXACT_MIRROR)
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "actual").mkdir()
    (left / "d").symlink_to("actual", target_is_directory=True)
    (right / "d").mkdir()
    (right / "d" / "old.txt").write_text("keep")
    plan = build_plan(profile, {"d": FileStat(6, 4, kind="symlink", target="actual")}, {"d/old.txt": FileStat(4, 5)})
    report = apply_plan(profile, plan)
    assert report.copied == report.deleted == 0 and report.errors
    assert (right / "d" / "old.txt").read_text() == "keep"
    assert (left / "d").is_symlink()
    assert not (right / "Older").exists()


def test_regular_archive_restore_honors_requested_owner_and_group(tmp_path, monkeypatch):
    profile = _profile(tmp_path, preserve_ownership=True)
    archive = tmp_path / "right" / "Older" / "a.txt"
    archive.parent.mkdir()
    archive.write_text("saved")
    item = older_files(profile)[0]
    seen = []
    monkeypatch.setattr(archive_module.os, "chown", lambda *args, **kwargs: seen.append((args, kwargs)))
    target = restore_version(item, tmp_path / "restore")
    assert target.read_text() == "saved"
    assert seen[0][1]["follow_symlinks"] is False
    assert seen[0][0][1:] == (archive.stat().st_uid, archive.stat().st_gid)


def test_binary_compare_retains_matching_permissions_and_owner_in_both_way_scan(tmp_path, monkeypatch):
    from ofs.scan import scan_profile

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "config"))
    profile = _profile(tmp_path, direction=Direction.BOTH, binary_compare=True, compare_permissions=True, compare_ownership=True)
    left = tmp_path / "left" / "same.txt"
    right = tmp_path / "right" / "same.txt"
    left.write_bytes(b"identical")
    right.write_bytes(b"identical")
    left.chmod(0o640)
    right.chmod(0o640)
    os.utime(left, (100, 100))
    os.utime(right, (200, 200))
    result = scan_profile(profile)
    assert result.changes == []
    assert result.bytes_read == 18


def test_permission_and_owner_comparison_are_opt_in_for_files():
    old = FileStat(10, 5, mode=0o644, uid=1, gid=2)
    newer = FileStat(10, 5, mode=0o600, uid=1, gid=2)
    profile = Profile(name="attrs", left="l", right="r")
    assert build_plan(profile, {"a": old}, {"a": newer}) == []
    profile.compare_permissions = True
    plan = build_plan(profile, {"a": old}, {"a": newer})
    assert plan[0].action == Action.COPY_LEFT_TO_RIGHT and plan[0].reason == "permissions differ"
    older_uid = FileStat(10, 5, mode=0o600, uid=3, gid=4)
    assert build_plan(profile, {"a": older_uid}, {"a": newer}) == []
    profile.compare_ownership = True
    assert build_plan(profile, {"a": older_uid}, {"a": newer})[0].reason == "owner/group differ"
    profile.direction = Direction.BOTH
    assert build_plan(profile, {"a": older_uid}, {"a": newer})[0].action == Action.ASK


def test_file_database_upgrades_and_retains_link_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "config"))
    profile = Profile(name="links", left="left", right="right")
    path = database_path(profile)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE files (side TEXT, rel TEXT, size INTEGER, mtime REAL, PRIMARY KEY (side, rel))")
        conn.execute("INSERT INTO meta VALUES ('left', 'left')")
        conn.execute("INSERT INTO meta VALUES ('right', 'right')")
        conn.execute("INSERT INTO files VALUES ('left', 'old.txt', 4, 10.0)")
    history, note = load_history(profile)
    assert note == "" and history is not None
    assert history.left["old.txt"].kind == "file"
    link = FileStat(7, 10, kind="symlink", target="payload", mode=0o777, uid=12, gid=34)
    save_history(profile, {"alias": link}, {})
    history, note = load_history(profile)
    assert note == "" and history is not None
    assert history.left["alias"] == link
