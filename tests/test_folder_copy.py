import os
from pathlib import Path

import pytest

from ofs.apply import RunControl
from ofs.folder_copy import FolderCopyError, copy_new_folder
from ofs.model import Direction, Profile, SymlinkMode


def _profile(tmp_path: Path, **kwargs) -> Profile:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    return Profile(name="new folder", left=str(left), right=str(right), **kwargs)


def test_new_folder_copy_preserves_empty_folders_metadata_and_masks(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = _profile(tmp_path, exclusions=["cache"])
    source = Path(profile.left) / "inbox"
    (source / "empty").mkdir(parents=True)
    (source / "cache").mkdir()
    (source / "photo.bin").write_bytes(b"complete bytes")
    (source / "session.tmp").write_bytes(b"not this")
    os.utime(source / "photo.bin", (100, 100))
    events = []
    outcome = copy_new_folder(profile, "inbox", on_progress=events.append, session_masks=["session-only"])
    assert outcome.report.copied == 1 and not outcome.report.errors
    destination = Path(profile.right) / "inbox"
    assert (destination / "photo.bin").read_bytes() == b"complete bytes"
    assert (destination / "photo.bin").stat().st_mtime == 100
    assert (destination / "empty").is_dir()
    assert not (destination / "cache").exists()
    assert not (destination / "session.tmp").exists()
    assert outcome.report.folders_created == 2
    assert [item.rel for item in outcome.changes] == ["inbox/photo.bin"]
    assert all(event.bytes_total == 0 for event in events)
    assert list(Path(profile.right).glob(".inbox.ofs-*")) == []


def test_include_masks_recurse_into_folders_but_only_copy_matching_files(tmp_path):
    profile = _profile(tmp_path, include=["*.jpg"])
    source = Path(profile.left) / "inbox"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "photo.jpg").write_text("matching")
    (source / "nested" / "notes.txt").write_text("not included")
    outcome = copy_new_folder(profile, "inbox")
    assert not outcome.report.errors
    assert (Path(profile.right) / "inbox" / "nested" / "photo.jpg").read_text() == "matching"
    assert not (Path(profile.right) / "inbox" / "nested" / "notes.txt").exists()
    assert outcome.report.copied == 1


def test_existing_destination_is_not_scanned_or_modified(tmp_path):
    profile = _profile(tmp_path)
    destination = Path(profile.right) / "inbox"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    (Path(profile.left) / "inbox").mkdir()
    with pytest.raises(FolderCopyError, match="already exists"):
        copy_new_folder(profile, "inbox")
    assert (destination / "keep.txt").read_text() == "keep"


def test_race_created_destination_survives_no_replace_install(tmp_path, monkeypatch):
    import ofs.folder_copy as module

    profile = _profile(tmp_path)
    (Path(profile.left) / "inbox").mkdir()
    (Path(profile.left) / "inbox" / "new.txt").write_text("incoming")
    real_rename = module._rename_noreplace

    def create_conflict(source, destination):
        destination.mkdir()
        (destination / "belongs-to-other-program.txt").write_text("keep")
        real_rename(source, destination)

    monkeypatch.setattr(module, "_rename_noreplace", create_conflict)
    outcome = copy_new_folder(profile, "inbox")
    assert outcome.report.errors and not outcome.report.cancelled
    assert (Path(profile.right) / "inbox" / "belongs-to-other-program.txt").read_text() == "keep"
    assert not (Path(profile.right) / "inbox" / "new.txt").exists()
    assert list(Path(profile.right).glob(".inbox.ofs-*")) == []


def test_cancel_between_chunks_removes_all_incomplete_output(tmp_path):
    profile = _profile(tmp_path)
    source = Path(profile.left) / "inbox"
    source.mkdir()
    (source / "large.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    control = RunControl()

    def stop_after_chunk(event):
        if event.current_bytes:
            assert event.bytes_total == 0
            control.stop()

    outcome = copy_new_folder(profile, "inbox", control=control, on_progress=stop_after_chunk, bytes_per_second=0)
    assert outcome.report.cancelled is True
    assert not outcome.report.errors
    assert not (Path(profile.right) / "inbox").exists()
    assert list(Path(profile.right).glob(".inbox.ofs-*")) == []


@pytest.mark.parametrize("mode", [SymlinkMode.FOLLOW, SymlinkMode.PRESERVE, SymlinkMode.SKIP])
def test_new_folder_copy_respects_symlink_policy(tmp_path, mode):
    profile = _profile(tmp_path, symlinks=mode)
    source = Path(profile.left) / "inbox"
    source.mkdir()
    (source / "real.txt").write_text("valid")
    os.symlink("real.txt", source / "shortcut")
    os.symlink("missing-target", source / "dangling")
    outcome = copy_new_folder(profile, "inbox")
    destination = Path(profile.right) / "inbox"
    if mode == SymlinkMode.FOLLOW:
        assert outcome.report.errors
        assert not destination.exists()
        return
    assert not outcome.report.errors
    if mode == SymlinkMode.PRESERVE:
        assert os.readlink(destination / "shortcut") == "real.txt"
        assert os.readlink(destination / "dangling") == "missing-target"
    if mode == SymlinkMode.SKIP:
        assert (destination / "real.txt").read_text() == "valid"
        assert not (destination / "shortcut").exists()
        assert not (destination / "dangling").exists()


def test_masks_use_profile_relative_paths_for_rooted_rules(tmp_path):
    profile = _profile(tmp_path, include=["inbox/*.jpg"], exclusions=["inbox/cache"])
    source = Path(profile.left) / "inbox"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "secret.jpg").write_text("excluded")
    (source / "photo.jpg").write_text("allowed")
    outcome = copy_new_folder(profile, "inbox")
    assert not outcome.report.errors
    assert outcome.report.copied == 1
    assert (Path(profile.right) / "inbox" / "photo.jpg").read_text() == "allowed"
    assert not (Path(profile.right) / "inbox" / "cache").exists()


def test_followed_links_use_target_size_and_age_for_filters(tmp_path):
    profile = _profile(tmp_path, max_size=1024)
    source = Path(profile.left) / "inbox"
    source.mkdir()
    (source / "large.bin").write_bytes(b"x" * 4096)
    (source / "alias").symlink_to("large.bin")
    outcome = copy_new_folder(profile, "inbox")
    assert not outcome.report.errors
    assert outcome.report.copied == 0
    assert not (Path(profile.right) / "inbox" / "alias").exists()


def test_top_level_skip_link_is_rejected(tmp_path):
    profile = _profile(tmp_path, symlinks=SymlinkMode.SKIP)
    (Path(profile.left) / "real").mkdir()
    (Path(profile.left) / "real" / "file.txt").write_text("not copied")
    (Path(profile.left) / "inbox").symlink_to("real", target_is_directory=True)
    with pytest.raises(FolderCopyError, match="Skip links"):
        copy_new_folder(profile, "inbox")
    assert not (Path(profile.right) / "inbox").exists()


def test_destination_inside_source_is_rejected_without_recursive_output(tmp_path):
    source_root = tmp_path / "left"
    source = source_root / "inbox"
    destination_root = source / "out"
    destination_root.mkdir(parents=True)
    (source / "file.txt").write_text("one")
    profile = Profile(name="overlap", left=str(source_root), right=str(destination_root))
    with pytest.raises(FolderCopyError, match="inside"):
        copy_new_folder(profile, "inbox")
    assert list(destination_root.iterdir()) == []


def test_readonly_directories_cleanup_and_failed_commit_report_zero(tmp_path):
    profile = _profile(tmp_path)
    source = Path(profile.left) / "inbox"
    (source / "readonly" / "empty").mkdir(parents=True)
    (source / "payload.txt").write_text("copied but not committed")
    (source / "readonly").chmod(0o555)
    destination = Path(profile.right) / "inbox"

    def create_conflict(event):
        if event.copied == 1:
            destination.mkdir()
            (destination / "keep.txt").write_text("incumbent")

    outcome = copy_new_folder(profile, "inbox", on_progress=create_conflict)
    (source / "readonly").chmod(0o700)
    assert outcome.report.errors
    assert outcome.report.copied == 0
    assert outcome.report.folders_created == 0
    assert outcome.changes == []
    assert (destination / "keep.txt").read_text() == "incumbent"
    assert list(Path(profile.right).glob(".inbox.ofs-*")) == []


def test_cleanup_never_follows_a_temporary_symlink(tmp_path):
    from ofs.folder_copy import _remove_temporary_tree

    outside = tmp_path / "outside.txt"
    outside.write_text("outside stays")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    (temporary / "link").symlink_to(outside)
    _remove_temporary_tree(temporary)
    assert outside.read_text() == "outside stays"
    assert not temporary.exists()


def test_failed_nested_copy_rolls_back_all_empty_destination_parents(tmp_path):
    profile = _profile(tmp_path)
    source = Path(profile.left) / "a" / "b" / "new"
    source.mkdir(parents=True)
    (source / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    control = RunControl()

    def cancel(event):
        if event.current_bytes:
            control.stop()

    outcome = copy_new_folder(profile, "a/b/new", control=control, on_progress=cancel)
    assert outcome.report.cancelled
    assert outcome.report.copied == 0 and outcome.report.folders_created == 0
    assert outcome.changes == []
    assert not (Path(profile.right) / "a").exists()


def test_cleanup_retries_directory_open_with_required_flags(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("Root can read directories without ordinary permission checks")
    from ofs.folder_copy import _remove_temporary_tree

    temporary = tmp_path / "temporary"
    locked = temporary / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    _remove_temporary_tree(temporary)
    assert not temporary.exists()


def test_left_and_right_need_unambiguous_validation(tmp_path):
    profile = _profile(tmp_path, direction=Direction.BOTH)
    (Path(profile.left) / "inbox").mkdir()
    with pytest.raises(FolderCopyError, match="one-way"):
        copy_new_folder(profile, "inbox")
    for index, folder in enumerate(("", "/outside", "../escape")):
        with pytest.raises(FolderCopyError):
            copy_new_folder(_profile(tmp_path / "bad" / str(index)), folder)


def test_right_to_left_copy_and_symlink_cycles(tmp_path):
    profile = _profile(tmp_path, direction=Direction.RIGHT_TO_LEFT)
    source = Path(profile.right) / "inbox"
    source.mkdir()
    (source / "new.txt").write_text("right source")
    outcome = copy_new_folder(profile, "inbox")
    assert not outcome.report.errors
    assert (Path(profile.left) / "inbox" / "new.txt").read_text() == "right source"
    (source / "loop").symlink_to(".", target_is_directory=True)
    # The existing destination prevents a second copy; cycles are checked in the isolated case below.
    cyclic_profile = _profile(tmp_path / "cyclic")
    cyclic = Path(cyclic_profile.left) / "root"
    cyclic.mkdir()
    (cyclic / "file.txt").write_text("one")
    (cyclic / "loop").symlink_to(".", target_is_directory=True)
    cyclic_outcome = copy_new_folder(cyclic_profile, "root")
    assert cyclic_outcome.report.errors
    assert not (Path(cyclic_profile.right) / "root").exists()
