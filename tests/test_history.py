import os

from sfs.apply import apply_plan
from sfs.compare import Action
from sfs.model import Direction, Profile, SyncMode
from sfs.scan import remember, scan_profile


def _write(path, text, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def _profile(tmp_path, **kwargs):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    return Profile(name="tracked", left=str(left), right=str(right), track=True, **kwargs), left, right


def test_first_run_records_and_does_not_invent_a_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path / "home"))
    profile, left, right = _profile(tmp_path)
    _write(left / "a.txt", "a", 10)
    _write(right / "a.txt", "a", 10)
    _write(right / "extra.txt", "e", 10)
    first = scan_profile(profile)
    assert first.changes == []
    assert "Recorded" in first.note
    second = scan_profile(profile)
    assert second.changes == []
    assert second.note == ""


def test_rename_becomes_a_move(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path / "home"))
    profile, left, right = _profile(tmp_path)
    _write(left / "a.txt", "same", 20)
    _write(right / "a.txt", "same", 20)
    scan_profile(profile)
    (left / "a.txt").rename(left / "b.txt")
    os.utime(left / "b.txt", (20, 20))
    result = scan_profile(profile)
    moves = [change for change in result.changes if change.action == Action.MOVE_ON_RIGHT]
    assert [(change.rel, change.dest_rel) for change in moves] == [("a.txt", "b.txt")]
    assert not any(change.action == Action.COPY_LEFT_TO_RIGHT for change in result.changes)
    report = apply_plan(profile, result.changes)
    remember(profile)
    assert report.moved == 1
    assert (right / "b.txt").read_text() == "same"
    assert not (right / "a.txt").exists()
    assert scan_profile(profile).changes == []


def test_exact_mirror_database_does_not_list_the_same_move_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path / "home"))
    profile, left, right = _profile(tmp_path, mode=SyncMode.EXACT_MIRROR)
    _write(left / "old" / "song.mp3", "same", 20)
    _write(right / "old" / "song.mp3", "same", 20)
    scan_profile(profile)
    (left / "90s").mkdir()
    (left / "old" / "song.mp3").rename(left / "90s" / "song.mp3")
    os.utime(left / "90s" / "song.mp3", (20, 20))
    result = scan_profile(profile)
    moves = [change for change in result.changes if change.action == Action.MOVE_ON_RIGHT]
    assert [(change.rel, change.dest_rel) for change in moves] == [("old/song.mp3", "90s/song.mp3")]


def test_deletion_propagates_only_when_the_other_side_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path / "home"))
    profile, left, right = _profile(tmp_path)
    _write(left / "a.txt", "a", 30)
    _write(right / "a.txt", "a", 30)
    _write(left / "b.txt", "b", 30)
    _write(right / "b.txt", "b", 30)
    scan_profile(profile)
    (left / "a.txt").unlink()
    _write(right / "b.txt", "changed", 90)
    (left / "b.txt").unlink()
    result = scan_profile(profile)
    actions = {change.rel: change.action for change in result.changes}
    assert actions["a.txt"] == Action.DELETE_RIGHT
    assert actions["b.txt"] == Action.ASK


def test_both_sides_changed_is_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path / "home"))
    profile, left, right = _profile(tmp_path, direction=Direction.BOTH)
    _write(left / "a.txt", "a", 40)
    _write(right / "a.txt", "a", 40)
    scan_profile(profile)
    _write(left / "a.txt", "left", 80)
    _write(right / "a.txt", "right", 90)
    result = scan_profile(profile)
    assert [change.action for change in result.changes] == [Action.ASK]


def test_database_for_other_folders_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path / "home"))
    profile, left, right = _profile(tmp_path)
    _write(left / "a.txt", "a", 10)
    _write(right / "a.txt", "a", 10)
    scan_profile(profile)
    profile.left = str(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()
    _write(tmp_path / "elsewhere" / "a.txt", "a", 10)
    (left / "a.txt").rename(left / "b.txt")
    result = scan_profile(profile)
    assert "ignored" in result.note
    assert not any(change.action == Action.MOVE_ON_RIGHT for change in result.changes)
