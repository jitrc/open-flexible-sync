"""Copy and delete the files a plan names. Paths must stay inside their side."""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ofs.compare import Action, Change
from ofs.model import Profile


class ApplyError(Exception):
    pass


@dataclass
class SideStats:
    copied: int = 0
    copied_bytes: int = 0
    moved: int = 0
    moved_bytes: int = 0
    deleted: int = 0
    deleted_bytes: int = 0


@dataclass
class ApplyReport:
    copied: int = 0
    deleted: int = 0
    moved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    left: SideStats = field(default_factory=SideStats)
    right: SideStats = field(default_factory=SideStats)
    folders_created: int = 0
    folders_removed: int = 0
    versions_removed: int = 0
    cancelled: bool = False

    def summary(self) -> str:
        text = f"{self.copied} copied, {self.deleted} deleted, {self.moved} moved"
        if self.cancelled:
            text = "stopped, " + text
        if self.errors:
            text += f", {len(self.errors)} errors"
        return text


@dataclass(frozen=True)
class CopyProgress:
    completed: int
    total: int
    rel: str
    current_bytes: int
    current_total: int
    bytes_done: int
    bytes_total: int
    copied: int
    deleted: int
    moved: int
    errors: int

    @property
    def overall_fraction(self) -> float:
        if self.bytes_total:
            return min(1.0, self.bytes_done / self.bytes_total)
        return min(1.0, self.completed / self.total) if self.total else 0.0

    @property
    def current_fraction(self) -> float:
        if not self.current_total:
            return 1.0 if self.rel else 0.0
        return min(1.0, self.current_bytes / self.current_total)


class RunControl:
    """Pause, skip-current-scan, and cancel checked between files. UI-thread safe."""

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._stop = threading.Event()
        self._skip = threading.Event()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        self._stop.set()
        self._running.set()

    @property
    def skipping(self) -> bool:
        return self._skip.is_set()

    def skip_current(self) -> None:
        """Skip the current scan only, without cancelling later profiles."""
        self._skip.set()
        self._running.set()

    def clear_skip(self) -> None:
        self._skip.clear()

    def checkpoint(self) -> bool:
        """Wait while paused. Return False when the run should stop."""

        self._running.wait()
        return not self._stop.is_set()


class _CopyStopped(Exception):
    pass


def _copy_checkpoint(control: RunControl | None) -> None:
    if control is not None and not control.checkpoint():
        raise _CopyStopped


class _RateLimiter:
    """One byte budget for the run, with pause/cancel-responsive sleeps."""

    def __init__(self, bytes_per_second: int):
        self.limit = max(0, int(bytes_per_second))
        self.bytes = 0
        self.started: float | None = None

    def spend(self, count: int, control: RunControl | None) -> None:
        if not self.limit:
            return
        if self.started is None:
            self.started = time.monotonic()
        self.bytes += count
        while True:
            if control is not None and control.paused:
                paused_at = time.monotonic()
                _copy_checkpoint(control)
                self.started += time.monotonic() - paused_at
            else:
                _copy_checkpoint(control)
            target = self.started + self.bytes / self.limit
            delay = target - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(0.05, delay))


class _CopyProgressReporter:
    """Legacy operation callbacks plus independent byte/chunk callbacks."""

    def __init__(self, changes: list[Change], report: ApplyReport, on_progress, on_copy_progress):
        self.report = report
        self.on_progress = on_progress
        self.on_copy_progress = on_copy_progress
        self.total = len(changes)
        self.total_bytes = sum(change.size for change in changes if _is_copy(change.action))
        self.completed = 0
        self.bytes_done = 0
        self.rel = ""
        self.current_size = 0
        self.current_plan = 0
        self.current_bytes = 0
        self.last_emit = 0.0

    def begin(self, index: int, change: Change) -> None:
        self.completed = index
        self.rel = change.rel
        self.current_size = change.size if _is_copy(change.action) else 0
        self.current_plan = change.size if _is_copy(change.action) else 0
        self.current_bytes = 0
        self._legacy(index, change.rel)
        self.emit(force=True)

    def chunk(self, done: int, size: int) -> None:
        first = self.current_bytes == 0
        self.current_size = size
        self.current_bytes = min(done, size) if size else 0
        self.emit(force=first or done == size)

    def complete(self, index: int, change: Change) -> None:
        self.completed = index + 1
        if _is_copy(change.action):
            actual = self.current_size
            self.total_bytes += actual - change.size
            self.current_plan = actual
            self.current_bytes = actual
            self.emit(force=True)
            self.bytes_done += actual
        else:
            self.emit(force=True)

    def finish(self) -> None:
        self.rel = ""
        self.current_bytes = 0
        self.current_size = 0
        self.current_plan = 0
        self._legacy(self.completed, None)
        self.emit(force=True)

    def emit(self, force: bool = False) -> None:
        if self.on_copy_progress is None:
            return
        now = time.monotonic()
        if not force and now - self.last_emit < 0.05:
            return
        self.last_emit = now
        current = self.current_bytes if self.rel else 0
        self.on_copy_progress(CopyProgress(
            self.completed,
            self.total,
            self.rel,
            current,
            self.current_size,
            self.bytes_done + current,
            max(0, self.total_bytes + self.current_size - self.current_plan),
            self.report.copied,
            self.report.deleted,
            self.report.moved,
            len(self.report.errors),
        ))

    def _legacy(self, index: int, rel: str | None) -> None:
        if self.on_progress is not None:
            self.on_progress(index, self.total, rel, self.report.copied, self.report.deleted, self.report.moved)


def files_identical(left: Path, right: Path) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    if left.stat().st_size != right.stat().st_size:
        return False
    return filecmp.cmp(left, right, shallow=False)


def _inside(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ApplyError(f"path escapes the profile folder: {rel}")
    return target


def apply_plan(
    profile: Profile,
    changes: list[Change],
    *,
    control: RunControl | None = None,
    on_progress=None,
    on_copy_progress=None,
    bytes_per_second: int = 0,
) -> ApplyReport:
    """Apply a serial plan with legacy operation events and byte-level copy events."""

    left_root = Path(profile.left)
    right_root = Path(profile.right)
    report = ApplyReport()
    ordered = sorted(changes, key=_apply_order)
    progress = _CopyProgressReporter(ordered, report, on_progress, on_copy_progress)
    limiter = _RateLimiter(bytes_per_second)
    for index, change in enumerate(ordered):
        progress.begin(index, change)
        if control is not None and not control.checkpoint():
            report.cancelled = True
            break
        if change.action == Action.ASK:
            report.skipped += 1
            progress.complete(index, change)
            continue
        try:
            if change.action == Action.COPY_LEFT_TO_RIGHT:
                _copy(profile, left_root, right_root, change.rel, report, "right", control, progress, limiter)
            elif change.action == Action.COPY_RIGHT_TO_LEFT:
                _copy(profile, right_root, left_root, change.rel, report, "left", control, progress, limiter)
            elif change.action == Action.DELETE_RIGHT:
                _delete(right_root, change.rel, report, "right")
            elif change.action == Action.DELETE_LEFT:
                _delete(left_root, change.rel, report, "left")
            elif change.action == Action.MOVE_ON_RIGHT:
                _move(right_root, change.rel, change.dest_rel, report, "right")
            elif change.action == Action.MOVE_ON_LEFT:
                _move(left_root, change.rel, change.dest_rel, report, "left")
        except _CopyStopped:
            report.cancelled = True
            break
        except (OSError, ApplyError) as exc:
            report.errors.append(f"{change.rel}: {exc}")
        progress.complete(index, change)
    progress.finish()
    return report


def _is_copy(action: Action) -> bool:
    return action in (Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT)


def _apply_order(change: Change) -> int:
    if change.action in (Action.MOVE_ON_LEFT, Action.MOVE_ON_RIGHT):
        return 0
    if change.action in (Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT):
        return 1
    return 2


def _count(report: ApplyReport, side: str, kind: str, size: int) -> None:
    stats = report.left if side == "left" else report.right
    if kind == "copy":
        stats.copied += 1
        stats.copied_bytes += size
        report.copied += 1
    elif kind == "move":
        stats.moved += 1
        stats.moved_bytes += size
        report.moved += 1
    else:
        stats.deleted += 1
        stats.deleted_bytes += size
        report.deleted += 1


def _mkdirs(root: Path, file_path: Path, report: ApplyReport) -> None:
    root_resolved = root.resolve()
    missing: list[Path] = []
    current = file_path.parent
    while not current.exists():
        if current == root or current == root_resolved or current.parent == current:
            break
        missing.append(current)
        current = current.parent
    if missing:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        report.folders_created += len(missing)


def _prune_empty(root: Path, directory: Path, report: ApplyReport) -> None:
    root_resolved = root.resolve()
    current = directory
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == root_resolved or root_resolved not in resolved.parents:
            return
        try:
            current.rmdir()
        except OSError:
            return
        report.folders_removed += 1
        current = current.parent


def _move(root: Path, old_rel: str, new_rel: str, report: ApplyReport, side: str) -> None:
    source = _inside(root, old_rel)
    dest = _inside(root, new_rel)
    if not source.is_file():
        raise ApplyError("source is missing")
    if dest.exists():
        raise ApplyError("destination already exists")
    size = source.stat().st_size
    parent = source.parent
    _mkdirs(root, dest, report)
    source.rename(dest)
    _count(report, side, "move", size)
    _prune_empty(root, parent, report)


def _copy(
    profile: Profile,
    source_root: Path,
    dest_root: Path,
    rel: str,
    report: ApplyReport,
    side: str,
    control: RunControl | None = None,
    progress: _CopyProgressReporter | None = None,
    limiter: _RateLimiter | None = None,
) -> None:
    source = _inside(source_root, rel)
    dest = _inside(dest_root, rel)
    if not source.is_file():
        raise ApplyError("source is missing")
    size = source.stat().st_size
    if progress is not None:
        progress.chunk(0, size)
    _mkdirs(dest_root, dest, report)
    temporary: Path | None = None
    try:
        with source.open("rb") as reading:
            descriptor, name = tempfile.mkstemp(prefix=".ofs-", suffix=".part", dir=dest.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as writing:
                copied = 0
                while True:
                    _copy_checkpoint(control)
                    block = reading.read(1024 * 1024)
                    if not block:
                        break
                    writing.write(block)
                    copied += len(block)
                    if progress is not None:
                        progress.chunk(copied, size)
                    if limiter is not None:
                        limiter.spend(len(block), control)
            shutil.copystat(source, temporary)
        # Once archiving starts, do not pause between it and the atomic replace.
        _copy_checkpoint(control)
        archived = bool(dest.exists() and profile.keep_older)
        if archived:
            _keep_older(profile, dest_root, dest, rel, report)
        try:
            os.replace(temporary, dest)
        except OSError as exc:
            if archived:
                try:
                    _restore_older(profile, dest_root, dest, rel)
                except OSError as restore_error:
                    raise ApplyError(f"{exc}; also could not restore the destination: {restore_error}") from exc
            raise
        temporary = None
        _count(report, side, "copy", size)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Never hide the copy/cancel error or fail a committed replace.
                pass


def _keep_older(profile: Profile, dest_root: Path, dest: Path, rel: str, report: ApplyReport) -> None:
    folder = profile.older_folder.strip("/") or "Older"
    archive = _inside(dest_root, f"{folder}/{rel}")
    keep = profile.older_keep if profile.older_keep > 1 else 1
    _mkdirs(dest_root, archive, report)
    if keep == 1:
        if archive.exists():
            archive.unlink()
        shutil.move(str(dest), str(archive))
        return
    slots = [archive]
    for index in range(1, keep):
        slots.append(archive.with_name(f"{archive.name}.~{index}~"))
    oldest = slots[-1]
    if oldest.exists() and oldest.is_file():
        oldest.unlink()
        report.versions_removed += 1
    for index in range(len(slots) - 2, -1, -1):
        source = slots[index]
        target = slots[index + 1]
        if source.exists():
            if target.exists():
                target.unlink()
            source.rename(target)
    shutil.move(str(dest), str(archive))


def _restore_older(profile: Profile, dest_root: Path, dest: Path, rel: str) -> None:
    """Best-effort rollback after replace fails, preserving every surviving version."""

    folder = profile.older_folder.strip("/") or "Older"
    archive = _inside(dest_root, f"{folder}/{rel}")
    if not archive.exists():
        raise ApplyError("versioned destination backup is missing")
    keep = profile.older_keep if profile.older_keep > 1 else 1
    slots = [archive]
    for index in range(1, keep):
        slots.append(archive.with_name(f"{archive.name}.~{index}~"))
    archive.replace(dest)
    for index in range(len(slots) - 1):
        source = slots[index + 1]
        if source.exists():
            source.replace(slots[index])


def _delete(root: Path, rel: str, report: ApplyReport, side: str) -> None:
    target = _inside(root, rel)
    if not target.exists():
        return
    if target.is_dir():
        raise ApplyError("refusing to delete a directory entry from a file plan")
    size = target.stat().st_size
    parent = target.parent
    target.unlink()
    _count(report, side, "delete", size)
    _prune_empty(root, parent, report)
