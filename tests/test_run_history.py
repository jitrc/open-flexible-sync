import json
import time

from ofs.run_history import list_history, record
from ofs.store import config_dir


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("OFS_HOME", str(home))
    return home


def _write_legacy(folder, profile, stamp, body="body", tail=""):
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch not in '/\\:' else "_" for ch in profile)
    path = folder / f"{safe} - {stamp}"
    path.write_text(f"{profile}\n{body}\n{tail}", encoding="utf-8")
    return path


def test_empty_history_does_not_create_a_file(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    assert list_history() == []
    assert not (home / "run-history.jsonl").exists()


def test_scans_and_copies_are_shared_latest_first(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    record("first", "scan", "12 files compared")
    time.sleep(0.01)
    record("second", "copy", "2 files copied")
    time.sleep(0.01)
    record("first", "folder-copy", "Folder copied")

    rows = list_history()
    assert [(row.profile, row.kind) for row in rows] == [
        ("first", "folder-copy"),
        ("second", "copy"),
        ("first", "scan"),
    ]
    assert [row.summary for row in rows] == ["Folder copied", "2 files copied", "12 files compared"]
    assert not (home / "logs").exists()
    assert (home / "run-history.jsonl").is_file()


def test_log_reference_is_basename_and_resolved_under_config(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    supplied = tmp_path / "outside" / "copy.log"
    supplied.parent.mkdir()
    supplied.write_text("unopened supplied content", encoding="utf-8")

    entry = record("media", "copy", "One file copied", log_path=supplied)
    expected = home / "logs" / "copy.log"
    assert entry.log_path == expected
    assert list_history()[0].log_path == expected

    document = json.loads((home / "run-history.jsonl").read_text(encoding="utf-8"))
    assert document["log"] == "copy.log"
    assert str(supplied) not in (home / "run-history.jsonl").read_text(encoding="utf-8")


def test_malformed_and_untrusted_rows_are_safe(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    home.mkdir()
    good = {
        "epoch": 10,
        "when": "2024-01-01 00:00:10",
        "profile": "<plain profile>",
        "kind": "scan",
        "summary": "<b>plain text, not markup</b>",
        "log": None,
    }
    bad_path = dict(good, profile="escape", log="../../outside.log")
    lines = [
        "not json",
        "[]",
        json.dumps({"profile": "missing fields"}),
        json.dumps(dict(good, epoch=True)),
        json.dumps(dict(good, epoch=float("nan"))),
        json.dumps(dict(good, epoch=10**400)),
        json.dumps(dict(good, kind="unknown")),
        json.dumps(dict(good, kind=[])),
        json.dumps(bad_path),
        json.dumps(good),
    ]
    (home / "run-history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = list_history()
    assert len(rows) == 1
    assert rows[0].profile == "<plain profile>"
    assert rows[0].summary == "<b>plain text, not markup</b>"


def test_invalid_jsonl_does_not_hide_one_good_row(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    original = record("kept", "scan", "Completed")
    path = home / "run-history.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{bad json\n")
        stream.write('{"epoch": 20, "when": "later", "profile": "also kept", ')
        stream.write('"kind": "copy", "summary": "Done", "log": null}\n')

    assert {(row.profile, row.epoch) for row in list_history()} == {("also kept", 20), ("kept", original.epoch)}


def test_legacy_backfill_uses_filename_first_line_and_bounded_tail(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    logs = home / "logs"
    path = _write_legacy(
        logs,
        "Real <Profile>",
        "2024-01-02 03.04.05.log",
        body="Left: /private/source\n" + "large body\n" * 20000,
        tail="Synchronization finished with errors.\nErrors omitted from structured history",
    )
    _write_legacy(logs, "Unfinished", "2024-01-03 03.04.05.log", body="temporary")
    unfinished = logs / "Unfinished - 2024-01-03 03.04.05.log"
    unfinished.rename(logs / "Unfinished - 2024-01-03 03.04.05.log.tmp")
    invalid = _write_legacy(logs, "Invalid", "2024-01-04 03.04.05.log")
    invalid.write_bytes(b"\xff\xfeinvalid\nbody")
    target = _write_legacy(logs, "Link target", "2024-01-05 03.04.05.log")
    (logs / "Linked - 2024-01-06 03.04.05.log").symlink_to(target)

    rows = list_history()
    assert len(rows) == 2
    legacy = next(row for row in rows if row.log_path == path)
    assert legacy.profile == "Real <Profile>"
    assert legacy.kind == "copy"
    assert legacy.when == "2024-01-02 03:04:05"
    assert legacy.summary == "Synchronization finished with errors."
    assert "/private/source" not in legacy.summary
    other = next(row for row in rows if row.profile == "Link target")
    assert other.summary == "See log"
    assert all(row.log_path.name != "Linked - 2024-01-06 03.04.05.log" for row in rows)

    record("Real <Profile>", "copy", "One file copied", log_path=path)
    deduped = list_history()
    assert sum(row.log_path == path for row in deduped) == 1
    assert next(row for row in deduped if row.log_path == path).summary == "One file copied"


def test_same_epoch_order_is_deterministic_and_latest_append_first(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    home.mkdir()
    first = {"epoch": 100, "when": "same", "profile": "first", "kind": "scan", "summary": "one", "log": None}
    second = {"epoch": 100, "when": "same", "profile": "second", "kind": "copy", "summary": "two", "log": None}
    (home / "run-history.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
    )
    assert [row.profile for row in list_history()] == ["second", "first"]
    assert [row.profile for row in list_history()] == ["second", "first"]
    assert config_dir() == home


def test_checked_scan_records_summary_without_copy_log_or_last_copy_edit(tmp_path, monkeypatch):
    from ofs.jobs import run_checked
    from ofs.model import Profile

    home = _home(tmp_path, monkeypatch)
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    source.mkdir()
    destination.mkdir()
    (source / "note.txt").write_text("same")
    (destination / "note.txt").write_text("same")
    profile = Profile(name="scan history", left=str(source), right=str(destination))

    results = run_checked([profile], checked={profile.name})

    assert len(results) == 1 and results[0].error is None
    entries = list_history()
    assert [(entry.profile, entry.kind) for entry in entries] == [("scan history", "scan")]
    assert entries[0].summary.startswith("Complete scan:")
    assert entries[0].log_path is None
    assert not (home / "logs").exists()
    assert profile.last_result == ""


def test_a_missing_folder_is_a_folder_error_that_keeps_the_path(tmp_path, monkeypatch):
    from ofs.jobs import run_checked
    from ofs.model import Profile

    _home(tmp_path, monkeypatch)
    destination = tmp_path / "backup"
    destination.mkdir()
    missing = tmp_path / "gone"
    profile = Profile(name="broken scan", left=str(missing), right=str(destination))

    results = run_checked([profile], checked={profile.name})

    assert results[0].result is None
    assert results[0].error == f"folder not found: {missing}"
    assert list_history()[0].summary == f"Scan error: folder not found: {missing}"


def test_a_missing_volume_keeps_its_own_message(tmp_path, monkeypatch):
    from ofs.jobs import run_checked
    from ofs.model import Profile

    _home(tmp_path, monkeypatch)
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    source.mkdir()
    destination.mkdir()
    profile = Profile(name="offline disk", left=str(source), right=str(destination), left_volume="OFS-TEST-NOT-A-LABEL")

    results = run_checked([profile], checked={profile.name})

    assert results[0].error == f"volume is not mounted: OFS-TEST-NOT-A-LABEL ({source})"
    assert list_history()[0].summary == f"Scan error: volume is not mounted: OFS-TEST-NOT-A-LABEL ({source})"


def test_a_missing_destination_names_the_side_and_whether_it_can_be_created(tmp_path, monkeypatch):
    from ofs.jobs import run_checked
    from ofs.model import Profile

    _home(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.txt").write_text("data")
    destination = tmp_path / "backup"
    profile = Profile(name="fresh backup", left=str(source), right=str(destination))

    results = run_checked([profile], checked={profile.name})

    assert results[0].result is None
    assert results[0].error == f"destination folder is missing: {destination} (it can be created; its parent exists)"
    assert list_history()[0].summary == f"Scan error: {results[0].error}"
    assert not destination.exists()
