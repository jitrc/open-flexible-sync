import json

import pytest

from ofs.model import Direction, Profile, SyncMode
from ofs.store import load_profiles, save_profile
from ofs.transfer import export_profiles, install_profile_bundle, read_profile_bundle


def _profile(name: str, root: str) -> Profile:
    return Profile(
        name=name,
        left=f"{root}/left",
        right=f"{root}/right",
        include=["*.jpg"],
        exclusions=["cache"],
        direction=Direction.BOTH,
        mode=SyncMode.EXACT_MIRROR,
        keep_older=True,
        keep_deleted=True,
        older_keep=3,
        imported_settings=True,
        last_result="old history",
    )


def test_profile_bundle_round_trip_and_renaming_duplicates(tmp_path, monkeypatch):
    home = tmp_path / "config"
    destination = tmp_path / "target-config"
    monkeypatch.setenv("OFS_HOME", str(home))
    save_profile(_profile("alpha one", "/original"))
    save_profile(_profile("alpha one (imported)", "/other"))
    bundle = tmp_path / "profiles.json"
    original = load_profiles()
    export_profiles(bundle, [original[0]])
    assert json.loads(bundle.read_text()) == {"format": "ofs-profiles", "version": 1, "profiles": [original[0].to_json()]}
    imported = read_profile_bundle(bundle)
    assert imported[0].to_json() == original[0].to_json()
    monkeypatch.setenv("OFS_HOME", str(destination))
    assert install_profile_bundle(imported, duplicates="skip").saved == 1
    assert load_profiles()[0].imported_settings is True
    save_profile(_profile("alpha one (imported)", "/other"))
    result = install_profile_bundle(imported, duplicates="rename")
    assert result.saved == 1 and result.renamed == 1 and result.skipped == 0
    assert {item.name for item in load_profiles()} == {"alpha one", "alpha one (imported)", "alpha one (imported) 2"}


def test_import_duplicate_skip_preserves_existing_and_replace_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "config"))
    existing = _profile("alpha one", "/edited")
    existing.note = "not silently replaced"
    save_profile(existing)
    incoming = [_profile("alpha one", "/imported"), _profile("alpha two", "/new")]
    result = install_profile_bundle(incoming, duplicates="skip")
    assert result.saved == 1 and result.skipped == 1
    current = {item.name: item for item in load_profiles()}
    assert current["alpha one"].to_json() == existing.to_json()
    assert "alpha two" in current
    replaced = install_profile_bundle(incoming[:1], duplicates="replace")
    assert replaced.saved == 1 and not replaced.skipped
    stored = next(item for item in load_profiles() if item.name == "alpha one")
    assert stored.to_json() == incoming[0].to_json()
    assert stored.left == "/imported/left"


def test_invalid_profile_bundle_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "config"))
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": "ofs-profiles", "version": 1, "profiles": [{"name": "valid", "left": "l", "right": "r"}, {"name": "bad=name", "left": "l", "right": "r"}]}))
    with pytest.raises(ValueError):
        read_profile_bundle(path)
    assert load_profiles() == []
    path.write_text(json.dumps({"format": "wrong", "version": 1, "profiles": [{"name": "valid"}]}))
    with pytest.raises(ValueError, match="not.*supported"):
        read_profile_bundle(path)
    path.write_text('{"format":"ofs-profiles","version":1,"profiles":[{"name":"x","left":"l","right":"r","disabled":"false"}]}')
    with pytest.raises(ValueError, match="disabled"):
        read_profile_bundle(path)


def test_export_is_atomic_and_requires_a_selection(tmp_path):
    path = tmp_path / "profiles.json"
    with pytest.raises(ValueError):
        export_profiles(path, [])
    assert not path.exists()
    assert list(tmp_path.glob(".ofs-export-*.part")) == []
