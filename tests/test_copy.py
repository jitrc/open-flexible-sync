import os
import stat
import threading
import time
from pathlib import Path

import ofs.apply as apply_module
from ofs.apply import CopyProgress, RunControl, apply_plan
from ofs.compare import Action, Change
from ofs.model import Profile


def _profile(left: Path, right: Path, **kwargs) -> Profile:
    left.mkdir(parents=True, exist_ok=True)
    right.mkdir(parents=True, exist_ok=True)
    return Profile(name="copy", left=str(left), right=str(right), **kwargs)


def _change(rel: str, size: int, action=Action.COPY_LEFT_TO_RIGHT) -> Change:
    return Change(rel, action, "test", size)


def _temps(root: Path) -> list[Path]:
    return list(root.rglob(".ofs-*.part"))


def test_atomic_copy_preserves_metadata_and_reports_bytes(tmp_path, monkeypatch):
    left = tmp_path / "left"
    right = tmp_path / "right"
    profile = _profile(left, right)
    _write(left / "a.bin", b"new bytes", 0o640, 123456789)
    _write(right / "a.bin", b"old", 0o600, 100)
    source = left / "a.bin"
    profile_size = source.stat().st_size
    seen = {}
    real_replace = os.replace

    def checked_replace(temp, target):
        target = Path(target)
        temp = Path(temp)
        seen["old_at_replace"] = target.read_bytes()
        seen["complete_temp"] = temp.read_bytes()
        seen["sibling"] = temp.parent == target.parent
        return real_replace(temp, target)

    monkeypatch.setattr(apply_module.os, "replace", checked_replace)
    events: list[CopyProgress] = []
    report = apply_plan(profile, [_change("a.bin", profile_size)], on_copy_progress=events.append)
    assert report.copied == 1
    assert report.right.copied_bytes == profile_size
    assert (right / "a.bin").read_bytes() == b"new bytes"
    assert stat.S_IMODE((right / "a.bin").stat().st_mode) == 0o640
    assert (right / "a.bin").stat().st_mtime_ns == source.stat().st_mtime_ns
    assert seen["old_at_replace"] == b"old"
    assert seen["complete_temp"] == b"new bytes"
    assert seen["sibling"]
    assert _temps(tmp_path) == []
    assert events[-1].bytes_done == profile_size
    assert events[-1].bytes_total == profile_size
    assert events[-1].overall_fraction == 1.0


def test_progress_reconciles_stale_sizes_without_changing_legacy_events(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    profile = _profile(left, right)
    _write(left / "six", b"123456")
    _write(left / "three", b"123")
    events: list[CopyProgress] = []
    legacy = []
    report = apply_plan(
        profile,
        [_change("six", 100), _change("three", 100)],
        on_progress=lambda index, total, rel, *_counts: legacy.append((index, total, rel)),
        on_copy_progress=events.append,
    )
    assert report.copied == 2
    assert report.right.copied_bytes == 9
    assert events[-1].bytes_done == 9
    assert events[-1].bytes_total == 9
    assert events[-1].current_total == 0
    assert all(event.bytes_done <= event.bytes_total for event in events)
    assert legacy == [(0, 2, "six"), (1, 2, "three"), (2, 2, None)]


def test_mid_copy_cancel_preserves_destination_and_older(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    profile = _profile(left, right, keep_older=True)
    _write(left / "big.bin", os.urandom(2 * 1024 * 1024 + 37))
    _write(right / "big.bin", b"old destination")
    _write(right / "Older" / "big.bin", b"old version")
    control = RunControl()

    def stop_after_first_chunk(event: CopyProgress):
        if event.rel == "big.bin" and event.current_bytes:
            control.stop()

    report = apply_plan(
        profile,
        [_change("big.bin", 2 * 1024 * 1024 + 37), _change("later.txt", 1)],
        control=control,
        on_copy_progress=stop_after_first_chunk,
    )
    assert report.cancelled is True
    assert report.copied == 0
    assert report.right.copied_bytes == 0
    assert report.errors == []
    assert (right / "big.bin").read_bytes() == b"old destination"
    assert (right / "Older" / "big.bin").read_bytes() == b"old version"
    assert not (right / "later.txt").exists()
    assert _temps(tmp_path) == []


def test_mid_copy_pause_holds_final_replacement_until_resume(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    profile = _profile(left, right)
    payload = os.urandom(2 * 1024 * 1024 + 19)
    _write(left / "a.bin", payload)
    _write(right / "a.bin", b"old destination")
    control = RunControl()
    started = threading.Event()
    finished = threading.Event()
    holder = {}

    def pause_first_chunk(event: CopyProgress):
        if event.current_bytes and not started.is_set():
            control.pause()
            started.set()

    def work():
        holder["report"] = apply_plan(
            profile,
            [_change("a.bin", len(payload))],
            control=control,
            on_copy_progress=pause_first_chunk,
        )
        finished.set()

    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(2)
    time.sleep(0.05)
    assert not finished.is_set()
    assert (right / "a.bin").read_bytes() == b"old destination"
    control.resume()
    thread.join(2)
    assert finished.is_set()
    assert holder["report"].cancelled is False
    assert (right / "a.bin").read_bytes() == payload
    assert _temps(tmp_path) == []


def test_failed_replacement_restores_active_file_and_surviving_versions(tmp_path, monkeypatch):
    left = tmp_path / "left"
    right = tmp_path / "right"
    profile = _profile(left, right, keep_older=True, older_keep=3)
    _write(left / "a.txt", b"new")
    _write(right / "a.txt", b"active old")
    archive = right / "Older" / "a.txt"
    _write(archive, b"version zero")
    _write(archive.with_name("a.txt.~1~"), b"version one")
    real_replace = os.replace

    def fail_new_file(temp, target):
        if Path(temp).name.endswith(".part"):
            raise OSError("replacement blocked")
        return real_replace(temp, target)

    monkeypatch.setattr(apply_module.os, "replace", fail_new_file)
    report = apply_plan(profile, [_change("a.txt", 3)])
    assert report.copied == 0
    assert len(report.errors) == 1
    assert (right / "a.txt").read_bytes() == b"active old"
    assert archive.read_bytes() == b"version zero"
    assert archive.with_name("a.txt.~1~").read_bytes() == b"version one"
    assert _temps(tmp_path) == []


def test_rate_limiter_shares_one_budget_and_zero_is_unlimited(tmp_path, monkeypatch):
    from ofs.apply import _RateLimiter

    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(apply_module.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(apply_module.time, "sleep", sleep)
    limiter = _RateLimiter(10)
    limiter.spend(5, RunControl())
    assert abs(clock[0] - 0.5) < 0.0001
    limiter.spend(5, RunControl())
    assert abs(clock[0] - 1.0) < 0.0001
    assert sleeps
    unlimited = _RateLimiter(0)
    unlimited.spend(1000, RunControl())
    assert clock[0] == 1.0


def _write(path: Path, data: bytes, mode: int | None = None, mtime: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
