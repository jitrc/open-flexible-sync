from ofs.apply import apply_plan
from ofs.cli import main
from ofs.compare import Action, Change, FileStat
from ofs.model import Profile, SyncMode
from ofs.review import Listed, build_listing, deletion_violations, safety_text
from ofs.settings import config_dir
from ofs.store import load_profiles, save_profile


def _listing(delete_count: int, unchanged: int) -> list[Listed]:
    rows = [Listed(Change(f"drop/{index}", Action.DELETE_RIGHT, "extra", 1), False, True) for index in range(delete_count)]
    rows.extend(Listed(Change(f"keep/{index}", Action.UNCHANGED, "same", 1), True, True) for index in range(unchanged))
    return rows


def test_configurable_deletion_limits_and_folder_protection():
    profile = Profile(name="job", left="l", right="r", max_delete_percent=25)
    assert deletion_violations(profile, _listing(2, 6)) == []
    violations = deletion_violations(profile, _listing(3, 6))
    assert "33.33%" in violations[0] and "25%" in violations[0]
    default = Profile(name="job", left="l", right="r")
    assert deletion_violations(default, _listing(2, 0)) == []
    profile.block_delete_all = True
    violations = deletion_violations(profile, _listing(2, 6))
    assert any("every scanned file in drop/" in item for item in violations)
    assert not any("keep/" in item for item in violations)
    assert "DELETION POLICY BLOCKS CONFIRM DELETIONS:" in safety_text(profile, _listing(2, 6))


def test_never_delete_files_blocks_apply_before_any_other_action(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "copy.txt").write_text("new")
    (right / "delete.txt").write_text("old")
    profile = Profile(name="job", left=str(left), right=str(right), never_delete_files=True)
    changes = [
        Change("copy.txt", Action.COPY_LEFT_TO_RIGHT, "new", 3),
        Change("delete.txt", Action.DELETE_RIGHT, "extra", 3),
    ]
    report = apply_plan(profile, changes, listing=build_listing({"copy.txt": FileStat(3, 1)}, {"delete.txt": FileStat(3, 1)}, changes))
    assert report.copied == report.deleted == report.moved == 0
    assert report.errors and "File deletion is disabled" in report.errors[0]
    assert (right / "delete.txt").read_text() == "old"
    assert not (right / "copy.txt").exists()
    profile.never_delete_files = False
    report = apply_plan(profile, changes, listing=build_listing({"copy.txt": FileStat(3, 1)}, {"delete.txt": FileStat(3, 1)}, changes))
    assert report.copied == report.deleted == 1


def test_percent_and_delete_all_protection_require_the_full_listing(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (right / "extra").write_text("old")
    profile = Profile(name="job", left=str(left), right=str(right), max_delete_percent=50)
    changes = [Change("extra", Action.DELETE_RIGHT, "extra", 3)]
    report = apply_plan(profile, changes)
    assert report.deleted == 0 and "complete scan listing" in report.errors[0]
    assert (right / "extra").exists()
    listing = [Listed(changes[0], False, True), Listed(Change("keep", Action.UNCHANGED, "same"), True, True)]
    report = apply_plan(profile, changes, listing=listing)
    assert report.deleted == 1 and not report.errors


def test_cli_refuses_protected_deletions_before_copy_or_logging(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "same.txt").write_text("same")
    (right / "same.txt").write_text("same")
    (left / "new.txt").write_text("new")
    (right / "extra.txt").write_text("old")
    save_profile(Profile(name="guarded", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR, max_delete_percent=10))
    assert main(["run", "guarded", "--apply"]) == 1
    assert "deletion protection" in capsys.readouterr().err
    assert (right / "extra.txt").read_text() == "old"
    assert not (right / "new.txt").exists()
    assert not (config_dir() / "logs").exists()
    assert load_profiles()[0].last_result == ""
