from sfs.model import Profile
from sfs.scan import scan_profile
from sfs.store import replace_in_paths, save_profile
from sfs.volume import decode_udev_name, ensure_volume
from sfs.walk import MissingVolume


def test_udev_label_decodes_spaces():
    assert decode_udev_name(r"Data\x20Win") == "Data Win"
    assert decode_udev_name("plain") == "plain"


def test_volume_must_be_the_disk_that_holds_the_path():
    ensure_volume("/media/data/photos", "DATA", devices={"DATA": 7}, device_of=lambda _path: 7)
    ensure_volume("/anywhere", "", devices={}, device_of=lambda _path: 1)
    for devices, device in (({"DATA": 7}, 9), ({}, 7)):
        try:
            ensure_volume("/media/data/photos", "DATA", devices=devices, device_of=lambda _path: device)
        except MissingVolume as exc:
            assert exc.path == "DATA (/media/data/photos)"
        else:
            raise AssertionError("expected MissingVolume")


def test_scan_stops_before_a_folder_on_the_wrong_volume(tmp_path, monkeypatch):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    def refuse(path, label, **_kwargs):
        if label:
            raise MissingVolume(f"{label} ({path})")

    monkeypatch.setattr("sfs.scan.ensure_volume", refuse)
    profile = Profile(name="p", left=str(left), right=str(right), left_volume="DATA")
    try:
        scan_profile(profile)
    except MissingVolume as exc:
        assert "DATA" in exc.path
    else:
        raise AssertionError("expected MissingVolume")


def test_replace_in_paths_changes_only_matching_sides(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path))
    profile = Profile(name="p", left="/media/old/photos", right="/backup/old/photos")
    assert replace_in_paths(profile, "/media/old", "/media/new")
    assert profile.left == "/media/new/photos"
    assert profile.right == "/backup/old/photos"
    assert not replace_in_paths(profile, "missing", "x")
    save_profile(profile)
