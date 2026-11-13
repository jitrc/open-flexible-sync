import os
import threading
import time
from pathlib import Path

from ofs.apply import RunControl, apply_plan
from ofs.compare import Action, Change
from ofs.jobs import run_checked
from ofs.model import ExclusionMode, Profile, SyncMode
from ofs.scan import scan_profile
from ofs.settings import set_global_exclusion_mode, set_global_exclusions
from ofs.walk import MissingVolume, WalkStopped, walk


def _write(path: Path, text: str, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_scan_does_not_read_bytes_unless_asked(tmp_path, monkeypatch):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "same", 200)
    _write(right / "a.txt", "same", 10)
    opened: list[str] = []
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        opened.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    profile = Profile(name="p", left=str(left), right=str(right))
    result = scan_profile(profile)
    assert result.bytes_read == 0
    assert any(item.action == Action.COPY_LEFT_TO_RIGHT for item in result.changes)
    assert opened == []

    profile.binary_compare = True
    result = scan_profile(profile)
    assert result.bytes_read > 0
    assert result.changes == []


def test_walk_reports_discovered_filtered_and_added_counts(tmp_path):
    from ofs.walk import WalkStats

    root = tmp_path / "tree"
    root.mkdir()
    _write(root / "keep.txt", "keep")
    _write(root / "skip.tmp", "skip")
    _write(root / "ignored" / "nested" / "file.txt", "ignored")
    _write(root / "visible" / "nested" / "file.txt", "visible")
    reports: list[WalkStats] = []
    found = walk(
        root,
        lambda rel: rel == "ignored",
        skip_file=lambda rel: rel.endswith(".tmp"),
        on_stats=lambda _rel, stats: reports.append(stats),
    )
    assert set(found) == {"keep.txt", "visible/nested/file.txt"}
    assert reports[-1] == WalkStats(discovered=6, filtered=2, added=2)


def test_walk_stop_and_pause(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(4):
        (root / f"f{index}.txt").write_text("x")
    control = RunControl()

    def stop_now(_rel, _count) -> None:
        control.stop()

    try:
        walk(root, lambda _rel: False, control=control, on_progress=stop_now)
    except WalkStopped:
        pass
    else:
        raise AssertionError("expected WalkStopped")

    paused = RunControl()
    released = threading.Event()

    def hold(_rel, _count) -> None:
        if released.is_set():
            return
        released.set()
        paused.pause()
        threading.Thread(target=lambda: (time.sleep(0.05), paused.resume()), daemon=True).start()

    found = walk(root, lambda _rel: False, control=paused, on_progress=hold)
    assert set(found) == {f"f{index}.txt" for index in range(4)}


def test_stopped_scan_does_not_start_the_next_profile(tmp_path):
    first_left = tmp_path / "a" / "left"
    second_left = tmp_path / "b" / "left"
    first_left.mkdir(parents=True)
    second_left.mkdir(parents=True)
    (first_left / "song.txt").write_text("a")
    (second_left / "secret.txt").write_text("b")
    right = tmp_path / "right"
    right.mkdir()
    first = Profile(name="first", left=str(first_left), right=str(right))
    second = Profile(name="second", left=str(second_left), right=str(right))
    control = RunControl()

    def stop_first(name: str, _side: str, _rel: str, _count: int) -> None:
        if name == "first":
            control.stop()

    results = run_checked([first, second], checked={"first", "second"}, control=control, on_progress=stop_first)
    assert [job.profile.name for job in results] == ["first"]
    assert results[0].error == "scan stopped"
    assert results[0].result is None


def test_skip_rest_returns_review_only_results_and_continues(tmp_path, monkeypatch):
    from ofs.settings import scan_estimate
    from ofs.cache import _path
    from ofs.history import database_path

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    first_left = tmp_path / "a" / "left"
    second_left = tmp_path / "b" / "left"
    second_left.mkdir(parents=True)
    _write(first_left / "partial.txt", "not a usable plan")
    _write(first_left / "not-yet-scanned.txt", "unknown")
    _write(second_left / "complete.txt", "scanned fully")
    right = tmp_path / "right"
    right.mkdir()
    first = Profile(name="first", left=str(first_left), right=str(right), track=True, cache_destination=True)
    second = Profile(name="second", left=str(second_left), right=str(right))
    control = RunControl()
    skipped: list[str] = []

    def skip_first(name: str, side: str, rel: str, count: int) -> None:
        if name == "first" and side == "Left" and count == 0:
            time.sleep(0.11)
        if name == "first" and side == "Left" and count == 1 and name not in skipped:
            skipped.append(name)
            control.skip_current()

    results = run_checked([first, second], checked={"first", "second"}, control=control, on_progress=skip_first)
    assert [job.profile.name for job in results] == ["first", "second"]
    assert results[0].error is None
    assert results[0].result is not None
    assert results[0].result.partial_sides == ("Left",)
    assert len(results[0].result.listing) == 1
    assert "review only" in results[0].result.note.lower()
    assert results[1].error is None
    assert results[1].result is not None
    assert results[1].result.partial_sides == ()
    assert [change.rel for change in results[1].result.changes] == ["complete.txt"]
    assert control.skipping is False
    assert scan_estimate("first") == (0, 0.0)
    assert not _path(first).exists()
    assert not database_path(first).exists()
    next_entries, next_seconds = scan_estimate("second")
    assert next_entries > 0 and next_seconds > 0


def test_cli_never_applies_a_partial_scan(tmp_path, monkeypatch, capsys):
    import ofs.cli as cli
    from ofs.jobs import JobResult
    from ofs.scan import ScanResult
    from ofs.store import save_profile

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _write(left / "known.txt", "known")
    profile = Profile(name="partial", left=str(left), right=str(right))
    save_profile(profile)
    result = ScanResult([Change("known.txt", Action.COPY_LEFT_TO_RIGHT, "only on source", 5)], partial_sides=("Left",))
    monkeypatch.setattr(cli, "run_checked", lambda *_args, **_kwargs: [JobResult(profile, result, None)])
    assert cli.main(["run", "partial", "--apply"]) == 1
    assert "review-only" in capsys.readouterr().err
    assert not (right / "known.txt").exists()


def test_missing_folder_raises_before_scanning(tmp_path):
    from ofs.walk import MissingFolder

    profile = Profile(name="p", left=str(tmp_path / "missing"), right=str(tmp_path))
    try:
        scan_profile(profile)
    except MissingFolder as exc:
        assert exc.path == profile.left
        assert isinstance(exc, MissingVolume)
    else:
        raise AssertionError("expected MissingFolder")


def test_older_keep_rotates_and_drops_the_oldest(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "third", 30)
    _write(right / "a.txt", "second", 20)
    _write(right / "Older" / "a.txt", "first", 10)
    _write(right / "Older" / "a.txt.~1~", "zero", 5)
    profile = Profile(name="p", left=str(left), right=str(right), keep_older=True, older_keep=2)
    report = apply_plan(profile, [Change("a.txt", Action.COPY_LEFT_TO_RIGHT, "source is newer", 5)])
    assert report.copied == 1
    assert report.versions_removed == 1
    assert (right / "a.txt").read_text() == "third"
    assert (right / "Older" / "a.txt").read_text() == "second"
    assert (right / "Older" / "a.txt.~1~").read_text() == "first"
    assert not (right / "Older" / "a.txt.~2~").exists()


def test_apply_keeps_the_previous_file_under_older(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "docs" / "a.txt", "new", 50)
    _write(right / "docs" / "a.txt", "old", 10)
    profile = Profile(name="p", left=str(left), right=str(right), keep_older=True)
    report = apply_plan(profile, [Change("docs/a.txt", Action.COPY_LEFT_TO_RIGHT, "source is newer")])
    assert report.copied == 1
    assert (right / "docs" / "a.txt").read_text() == "new"
    assert (right / "Older" / "docs" / "a.txt").read_text() == "old"


def test_apply_deletes_only_the_planned_file(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    _write(right / "gone.txt", "x")
    _write(right / "keep.txt", "y")
    profile = Profile(name="p", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR)
    report = apply_plan(profile, [Change("gone.txt", Action.DELETE_RIGHT, "only on destination")])
    assert report.deleted == 1
    assert not (right / "gone.txt").exists()
    assert (right / "keep.txt").read_text() == "y"


def test_apply_counts_sides_and_folders(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "new" / "a.txt", "hello", 20)
    _write(left / "new" / "b.txt", "world", 20)
    _write(right / "gone" / "old.txt", "z", 10)
    _write(right / "gone" / "other.txt", "yy", 10)
    profile = Profile(name="p", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR)
    report = apply_plan(
        profile,
        [
            Change("new/a.txt", Action.COPY_LEFT_TO_RIGHT, "new", 5),
            Change("new/b.txt", Action.COPY_LEFT_TO_RIGHT, "new", 5),
            Change("gone/old.txt", Action.DELETE_RIGHT, "extra", 1),
            Change("gone/other.txt", Action.DELETE_RIGHT, "extra", 2),
        ],
    )
    assert report.copied == 2
    assert report.deleted == 2
    assert report.right.copied == 2
    assert report.right.copied_bytes == 10
    assert report.right.deleted_bytes == 3
    assert report.left.copied == 0
    assert report.folders_created == 1
    assert report.folders_removed == 1
    assert (right / "new" / "a.txt").read_text() == "hello"
    assert not (right / "gone").exists()


def test_cancel_stops_before_the_announced_file(tmp_path):
    from ofs.apply import RunControl

    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "a", 1)
    _write(left / "b.txt", "b", 1)
    _write(left / "c.txt", "c", 1)
    right.mkdir()
    profile = Profile(name="p", left=str(left), right=str(right))
    control = RunControl()

    def on_progress(_index, _total, rel, *_rest):
        if rel == "b.txt":
            control.stop()

    report = apply_plan(
        profile,
        [
            Change("a.txt", Action.COPY_LEFT_TO_RIGHT, "new", 1),
            Change("b.txt", Action.COPY_LEFT_TO_RIGHT, "new", 1),
            Change("c.txt", Action.COPY_LEFT_TO_RIGHT, "new", 1),
        ],
        control=control,
        on_progress=on_progress,
    )
    assert report.cancelled
    assert report.copied == 1
    assert (right / "a.txt").read_text() == "a"
    assert not (right / "b.txt").exists()
    assert not (right / "c.txt").exists()


def test_pause_holds_the_next_file_until_resume(tmp_path):
    import threading
    import time

    from ofs.apply import RunControl

    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "a", 1)
    _write(left / "b.txt", "b", 1)
    right.mkdir()
    profile = Profile(name="p", left=str(left), right=str(right))
    control = RunControl()
    paused = threading.Event()

    def on_progress(_index, _total, rel, *_rest):
        if rel == "b.txt":
            control.pause()
            paused.set()

    def resume() -> None:
        assert paused.wait(2)
        time.sleep(0.05)
        control.resume()

    threading.Thread(target=resume, daemon=True).start()
    report = apply_plan(
        profile,
        [
            Change("a.txt", Action.COPY_LEFT_TO_RIGHT, "new", 1),
            Change("b.txt", Action.COPY_LEFT_TO_RIGHT, "new", 1),
        ],
        control=control,
        on_progress=on_progress,
    )
    assert not report.cancelled
    assert report.copied == 2
    assert (right / "b.txt").read_text() == "b"


def test_checked_subset_does_not_scan_the_other_job(tmp_path, monkeypatch):
    from ofs import jobs

    scanned: list[str] = []
    real = jobs.scan_profile

    def spy(profile, **kwargs):
        scanned.append(profile.name)
        return real(profile, **kwargs)

    monkeypatch.setattr(jobs, "scan_profile", spy)
    left = tmp_path / "left"
    right = tmp_path / "right"
    other = tmp_path / "other"
    _write(left / "a.txt", "a", 2)
    right.mkdir()
    other.mkdir()
    first = Profile(name="one", left=str(left), right=str(right))
    second = Profile(name="two", left=str(other), right=str(other))
    results = jobs.run_checked([first, second], checked={"one"})
    assert scanned == ["one"]
    assert results[0].error is None
    assert results[0].result is not None
    assert len(results[0].result.changes) == 1


def _global_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OFS_HOME", str(home))
    set_global_exclusions(["Thumbs.db"])


def test_global_exclusion_ignore_totally_hides_from_both_sides(tmp_path, monkeypatch):
    _global_home(tmp_path, monkeypatch)
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "same", 100)
    _write(right / "a.txt", "same", 100)
    _write(left / "Thumbs.db", "junk", 200)
    _write(right / "Thumbs.db", "other", 5)
    profile = Profile(name="p", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR)
    result = scan_profile(profile)
    assert result.changes == []
    assert result.left_files == 1
    assert result.right_files == 1


def test_global_exclusion_dont_copy_never_transfers(tmp_path, monkeypatch):
    _global_home(tmp_path, monkeypatch)
    set_global_exclusion_mode(ExclusionMode.DONT_COPY)
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "same", 100)
    _write(right / "a.txt", "same", 100)
    _write(left / "Thumbs.db", "junk", 200)
    profile = Profile(name="p", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR)
    assert scan_profile(profile).changes == []


def test_global_exclusion_dont_copy_deletes_an_extra_on_the_destination(tmp_path, monkeypatch):
    _global_home(tmp_path, monkeypatch)
    set_global_exclusion_mode(ExclusionMode.DONT_COPY)
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write(left / "a.txt", "same", 100)
    _write(right / "a.txt", "same", 100)
    _write(right / "Thumbs.db", "extra", 5)
    profile = Profile(name="p", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR)
    changes = scan_profile(profile).changes
    assert [(item.rel, item.action) for item in changes] == [("Thumbs.db", Action.DELETE_RIGHT)]
