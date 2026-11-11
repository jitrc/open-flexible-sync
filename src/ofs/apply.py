"""Copy and delete the files a plan names. Paths must stay inside their side."""

from __future__ import annotations

import filecmp
import shutil
import threading
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
) -> ApplyReport:
    """Apply changes. `on_progress(index, total, rel, copied, deleted, moved)` may run on this thread."""

    left_root = Path(profile.left)
    right_root = Path(profile.right)
    report = ApplyReport()
    ordered = sorted(changes, key=_apply_order)
    total = len(ordered)
    completed = 0
    for index, change in enumerate(ordered):
        _emit(on_progress, index, total, change.rel, report)
        if control is not None and not control.checkpoint():
            report.cancelled = True
            break
        if change.action == Action.ASK:
            report.skipped += 1
            completed = index + 1
            continue
        try:
            if change.action == Action.COPY_LEFT_TO_RIGHT:
                _copy(profile, left_root, right_root, change.rel, report, "right")
            elif change.action == Action.COPY_RIGHT_TO_LEFT:
                _copy(profile, right_root, left_root, change.rel, report, "left")
            elif change.action == Action.DELETE_RIGHT:
                _delete(right_root, change.rel, report, "right")
            elif change.action == Action.DELETE_LEFT:
                _delete(left_root, change.rel, report, "left")
            elif change.action == Action.MOVE_ON_RIGHT:
                _move(right_root, change.rel, change.dest_rel, report, "right")
            elif change.action == Action.MOVE_ON_LEFT:
                _move(left_root, change.rel, change.dest_rel, report, "left")
            completed = index + 1
        except (OSError, ApplyError) as exc:
            report.errors.append(f"{change.rel}: {exc}")
            completed = index + 1
    _emit(on_progress, completed, total, None, report)
    return report


def _emit(on_progress, index: int, total: int, rel: str | None, report: ApplyReport) -> None:
    if on_progress is None:
        return
    on_progress(index, total, rel, report.copied, report.deleted, report.moved)


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


def _copy(profile: Profile, source_root: Path, dest_root: Path, rel: str, report: ApplyReport, side: str) -> None:
    source = _inside(source_root, rel)
    dest = _inside(dest_root, rel)
    if not source.is_file():
        raise ApplyError("source is missing")
    size = source.stat().st_size
    _mkdirs(dest_root, dest, report)
    if dest.exists() and profile.keep_older:
        _keep_older(profile, dest_root, dest, rel, report)
    shutil.copy2(source, dest)
    _count(report, side, "copy", size)


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
