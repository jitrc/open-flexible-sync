import os
from pathlib import Path

import pytest

from ofs.apply import apply_plan
from ofs.archive import older_files, restore_version
from ofs.compare import Action, Change
from ofs.model import Profile, SyncMode


def test_delete_can_be_archived_without_enabling_replacement_versions(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    profile = Profile(name="recovery", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR, keep_deleted=True, older_keep=2)
    extra = right / "folder" / "extra.txt"
    extra.parent.mkdir()
    extra.write_text("first recovery")
    report = apply_plan(profile, [Change("folder/extra.txt", Action.DELETE_RIGHT, "extra", len("first recovery"))])
    archive = right / "Older" / "folder" / "extra.txt"
    assert report.deleted == 1 and report.deletions_archived == 1
    assert not extra.exists()
    assert archive.read_text() == "first recovery"
    extra.parent.mkdir()
    extra.write_text("second recovery")
    apply_plan(profile, [Change("folder/extra.txt", Action.DELETE_RIGHT, "extra", len("second recovery"))])
    assert archive.read_text() == "second recovery"
    assert archive.with_name("extra.txt.~1~").read_text() == "first recovery"


def test_permanent_deletion_remains_the_existing_profile_default(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    extra = right / "extra.txt"
    extra.write_text("extra")
    profile = Profile(name="legacy behavior", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR)
    report = apply_plan(profile, [Change("extra.txt", Action.DELETE_RIGHT, "extra", 5)])
    assert report.deleted == 1 and report.deletions_archived == 0
    assert not (right / "Older").exists()
    assert Profile.from_json(profile.to_json()).keep_deleted is False


def test_list_versions_and_restore_atomically_without_overwriting(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    archive = right / "Older" / "folder" / "a.txt"
    archive.parent.mkdir(parents=True)
    archive.write_text("newer")
    archive.with_name("a.txt.~1~").write_text("older")
    os.utime(archive, (1234, 1234))
    profile = Profile(name="recovery", left=str(left), right=str(right), keep_deleted=True)
    entries = older_files(profile)
    assert [(item.relative, item.version, item.side) for item in entries] == [
        ("folder/a.txt", 0, "right"),
        ("folder/a.txt", 1, "right"),
    ]
    destination = tmp_path / "restore"
    target = restore_version(entries[1], destination)
    assert target.read_bytes() == b"older"
    assert archive.read_text() == "newer"
    assert not (right / "a.txt").exists()
    assert target.stat().st_mtime == archive.with_name("a.txt.~1~").stat().st_mtime
    with pytest.raises(FileExistsError):
        restore_version(entries[1], destination)
    assert target.read_text() == "older"
    assert list(destination.rglob(".ofs-restore-*.part")) == []
    with pytest.raises(ValueError, match="outside"):
        restore_version(entries[0], right)


def test_restore_rejects_symlink_escape(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    archive = right / "Older" / "a.txt"
    archive.parent.mkdir()
    archive.write_text("saved")
    profile = Profile(name="recovery", left=str(left), right=str(right))
    entry = older_files(profile)[0]
    destination = tmp_path / "restore"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    (destination / "nested").symlink_to(outside, target_is_directory=True)
    nested_entry = type(entry)(profile, "right", "nested/a.txt", 0, archive, 5, 0)
    with pytest.raises(ValueError, match="escapes"):
        restore_version(nested_entry, destination)
    assert not (outside / "a.txt").exists()
