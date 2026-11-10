from sfs.model import Profile, parse_size
from sfs.scan import scan_profile
from sfs.settings import compare_command, global_exclusions, set_compare_command, set_global_exclusions
from sfs.store import duplicate_profile, load_profiles, save_profile


def test_duplicate_keeps_the_job_and_clears_the_last_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path))
    save_profile(Profile(name="alpha notes", left="/l", right="/r", exclusions=["*.tmp"], last_result="3 copied"))
    first = duplicate_profile(next(item for item in load_profiles() if item.name == "alpha notes"))
    second = duplicate_profile(next(item for item in load_profiles() if item.name == "alpha notes"))
    assert first.name == "alpha notes copy"
    assert second.name == "alpha notes copy 2"
    assert first.exclusions == ["*.tmp"]
    assert first.left == "/l"
    assert first.last_result == ""
    assert {item.name for item in load_profiles()} == {"alpha notes", "alpha notes copy", "alpha notes copy 2"}


def test_parse_size_accepts_units():
    assert parse_size("") == 0
    assert parse_size("512") == 512
    assert parse_size("1.5 KB") == 1536
    assert parse_size("2M") == 2 * 1024 * 1024


def test_default_global_masks_hide_temp_files_until_settings_replace_them(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path))
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "notes.txt").write_text("keep")
    (left / "notes.tmp").write_text("drop")
    assert "*.tmp" in global_exclusions()
    profile = Profile(name="p", left=str(left), right=str(right))
    result = scan_profile(profile)
    assert [change.rel for change in result.changes] == ["notes.txt"]
    set_global_exclusions([])
    assert global_exclusions() == []
    again = scan_profile(profile)
    assert {change.rel for change in again.changes} == {"notes.txt", "notes.tmp"}


def test_compare_command_uses_the_setting_unless_the_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SFS_HOME", str(tmp_path))
    monkeypatch.delenv("SFS_COMPARE", raising=False)
    assert compare_command() == "meld"
    set_compare_command("kdiff3")
    assert compare_command() == "kdiff3"
    monkeypatch.setenv("SFS_COMPARE", "nvim -d")
    assert compare_command() == "nvim -d"
