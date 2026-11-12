import pytest

from ofs.compare import FileStat
from ofs.history import database_path, save_history
from ofs.model import Profile
from ofs.store import load_profiles, name_replacements, path_replacements, rename_profiles, replace_in_paths, save_profile, validate_name_replacements


def test_path_replacement_supports_mount_prefix_and_side_scope():
    profile = Profile(name="job", left="/mnt/old/photos", right="/mnt/old-archive/photos", imported_settings=True)
    before = profile.to_json()
    preview = path_replacements(profile, "/mnt/old", "/mnt/new", sides="left", prefix_only=True)
    assert preview == {"left": "/mnt/new/photos"}
    assert profile.to_json() == before
    assert replace_in_paths(profile, "/mnt/old", "/mnt/new", sides="left", prefix_only=True)
    assert profile.left == "/mnt/new/photos"
    assert profile.right == "/mnt/old-archive/photos"
    assert profile.imported_settings is True
    profile.left = "/mnt/old"
    assert path_replacements(profile, "/mnt/old", "/mnt/new", sides="both", prefix_only=True) == {"left": "/mnt/new"}
    assert path_replacements(profile, "/mnt/old", "/mnt/new", sides="right", prefix_only=True) == {}
    profile.left = "/mnt/old/"
    assert path_replacements(profile, "/mnt/old", "/mnt/new/", sides="left", prefix_only=True) == {"left": "/mnt/new/"}
    profile.left = "/mnt/old/photos"
    assert path_replacements(profile, "/mnt/old/", "/mnt/new/", sides="left", prefix_only=True) == {"left": "/mnt/new/photos"}
    assert path_replacements(profile, "/mnt/old", "/mnt/new/", sides="left", prefix_only=True) == {"left": "/mnt/new/photos"}
    with pytest.raises(ValueError):
        path_replacements(profile, "/mnt/old", "/mnt/new", sides="source")


def test_batch_rename_previews_changes_and_preserves_every_other_field(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "config"))
    first = Profile(name="alpha one", left="/new/left", right="/new/right", imported_settings=True, keep_deleted=True, max_delete_percent=35)
    second = Profile(name="alpha two", left="/other", right="/backup")
    untouched = Profile(name="beta existing", left="l", right="r")
    for profile in (first, second, untouched):
        save_profile(profile)
    save_history(first, {"a": FileStat(1, 1)}, {})
    old_database = database_path(first)
    original = {profile.name: profile.to_json() for profile in load_profiles()}
    changes = name_replacements([first, second], "alpha", "photos", prefix_only=True)
    assert changes == [("alpha one", "photos one"), ("alpha two", "photos two")]
    assert [profile.name for profile in load_profiles()] == ["alpha one", "alpha two", "beta existing"]
    rename_profiles(changes)
    after = {profile.name: profile for profile in load_profiles()}
    assert set(after) == {"photos one", "photos two", "beta existing"}
    assert after["photos one"].left == "/new/left" and after["photos one"].keep_deleted is True
    assert after["photos one"].imported_settings is True
    assert after["photos one"].max_delete_percent == 35
    assert after["photos two"].to_json()["left"] == original["alpha two"]["left"]
    assert after["beta existing"].to_json() == original["beta existing"]
    assert not old_database.exists()
    assert not database_path(after["photos one"]).exists()


def test_batch_rename_rejects_collisions_without_writing_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "config"))
    save_profile(Profile(name="alpha one", left="l", right="r"))
    save_profile(Profile(name="alpha two", left="l", right="r"))
    save_profile(Profile(name="beta existing", left="l", right="r"))
    before = {profile.name: profile.to_json() for profile in load_profiles()}
    pending = name_replacements(load_profiles()[:1], "one", "two")
    assert pending == [("alpha one", "alpha two")]
    with pytest.raises(ValueError, match="already exists"):
        validate_name_replacements(pending, load_profiles())
    with pytest.raises(ValueError, match="already exists"):
        rename_profiles([("alpha one", "beta existing")])
    assert {profile.name: profile.to_json() for profile in load_profiles()} == before
    path = tmp_path / "config" / "profiles" / "alpha%20one.json"
    assert path.exists()


def test_batch_rename_rejects_empty_padding_and_legacy_forbidden_character():
    profile = Profile(name="alpha one", left="l", right="r")
    for invalid in ("", " padded", "old=name"):
        with pytest.raises(ValueError):
            name_replacements([profile], "alpha", invalid)
