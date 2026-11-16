import os

from ofs.compare import Action, FileStat
from ofs.model import Profile, SyncMode
from ofs.scan import scan_profile


def _profile(left, right, verify):
    return Profile(
        name="verify",
        left=str(left),
        right=str(right),
        mode=SyncMode.EXACT_MIRROR,
        verify_moves=verify,
    )


def _put(root, rel, data, mtime=1_700_000_000):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def _plan(profile, tmp_path, monkeypatch, left_data, right_data):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    _put(tmp_path / "left", "new/shared/song.mp3", left_data)
    _put(tmp_path / "right", "old/shared/song.mp3", right_data)
    result = scan_profile(_profile(tmp_path / "left", tmp_path / "right", profile))
    return result


def test_verified_move_is_allowed_and_an_unverified_pair_stays_copy_and_delete(tmp_path, monkeypatch):
    # Same bytes: the rename is proven and the move survives verification.
    result = _plan(True, tmp_path, monkeypatch, b"same bytes", b"same bytes")
    assert [change.action for change in result.changes] == [Action.MOVE_ON_RIGHT]
    move = result.changes[0]
    assert move.rel == "old/shared/song.mp3"
    assert move.dest_rel == "new/shared/song.mp3"

    # Same size but different bytes: no rename, the real copy is still planned.
    result = _plan(True, tmp_path, monkeypatch, b"same bytes", b"other data")
    actions = {change.rel: change.action for change in result.changes}
    assert actions == {
        "new/shared/song.mp3": Action.COPY_LEFT_TO_RIGHT,
        "old/shared/song.mp3": Action.DELETE_RIGHT,
    }
    assert "Verify moves by content" in result.note


def test_without_the_option_a_same_size_pair_is_still_a_move(tmp_path, monkeypatch):
    result = _plan(False, tmp_path, monkeypatch, b"same bytes", b"other data")
    assert [change.action for change in result.changes] == [Action.MOVE_ON_RIGHT]


def test_an_unreadable_move_pair_is_downgraded_not_renamed(tmp_path, monkeypatch):
    from pathlib import Path

    from ofs.compare import Change
    from ofs.scan import _verify_moves

    changes = [Change("old/shared/song.mp3", Action.MOVE_ON_RIGHT, "moved to new/shared/song.mp3", 10, "new/shared/song.mp3")]
    plan, read, downgraded = _verify_moves(changes, tmp_path / "left", tmp_path / "right")
    assert downgraded == 1
    assert read == 20
    assert {(item.rel, item.action) for item in plan} == {
        ("new/shared/song.mp3", Action.COPY_LEFT_TO_RIGHT),
        ("old/shared/song.mp3", Action.DELETE_RIGHT),
    }
    # A left-direction move is demoted with the matching actions too.
    changes = [Change("old/shared/song.mp3", Action.MOVE_ON_LEFT, "moved", 10, "new/shared/song.mp3")]
    plan, _read, downgraded = _verify_moves(changes, Path(tmp_path / "left"), Path(tmp_path / "right"))
    assert downgraded == 1
    assert {item.action for item in plan} == {Action.COPY_RIGHT_TO_LEFT, Action.DELETE_LEFT}


def test_verify_moves_is_persisted_and_defaults_off():
    profile = Profile(name="p", left="l", right="r", verify_moves=True)
    assert Profile.from_json(profile.to_json()).verify_moves is True
    assert Profile.from_json(profile.to_json() | {"verify_moves": None}).verify_moves is False
    assert Profile.from_json({"name": "p"}).verify_moves is False
