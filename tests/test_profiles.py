from ofs.model import Profile, parse_size
from ofs.scan import scan_profile
from ofs.settings import compare_command, global_exclusions, set_compare_command, set_global_exclusions
from ofs.store import duplicate_profile, load_profiles, save_profile


def test_duplicate_keeps_the_job_and_clears_the_last_result(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path))
    save_profile(Profile(name="alpha notes", left="/l", right="/r", exclusions=["*.tmp"], last_result="3 copied"))
    first = duplicate_profile(next(item for item in load_profiles() if item.name == "alpha notes"))
    second = duplicate_profile(next(item for item in load_profiles() if item.name == "alpha notes"))
    assert first.name == "alpha notes copy"
    assert second.name == "alpha notes copy 2"
    assert first.exclusions == ["*.tmp"]
    assert first.left == "/l"
    assert first.last_result == ""
    assert {item.name for item in load_profiles()} == {"alpha notes", "alpha notes copy", "alpha notes copy 2"}


def test_disabled_profile_persists_and_cannot_run(tmp_path, monkeypatch, capsys):
    from ofs.cli import main
    from ofs.jobs import run_checked

    monkeypatch.setenv("OFS_HOME", str(tmp_path))
    save_profile(Profile(name="disabled job", left="/left", right="/right", disabled=True))
    saved = load_profiles()[0]
    assert saved.disabled is True
    assert saved.left == "/left" and saved.right == "/right"
    assert run_checked([saved], checked={saved.name}) == []
    assert main(["run", "disabled job", "--apply"]) == 1
    assert "profile is disabled" in capsys.readouterr().err


def test_parse_size_accepts_units():
    assert parse_size("") == 0
    assert parse_size("512") == 512
    assert parse_size("1.5 KB") == 1536
    assert parse_size("2M") == 2 * 1024 * 1024


def test_default_global_masks_hide_temp_files_until_settings_replace_them(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path))
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
    monkeypatch.setenv("OFS_HOME", str(tmp_path))
    monkeypatch.delenv("OFS_COMPARE", raising=False)
    assert compare_command() == "meld"
    set_compare_command("kdiff3")
    assert compare_command() == "kdiff3"
    monkeypatch.setenv("OFS_COMPARE", "nvim -d")
    assert compare_command() == "nvim -d"
