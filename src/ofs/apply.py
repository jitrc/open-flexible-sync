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

from ofs.cache import invalidate_destination
from ofs.compare import Action, Change
from ofs.model import Profile, SymlinkMode
from ofs.review import deletion_violations


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
    deletions_archived: int = 0
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
        self.rules_lock = threading.RLock()

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


def _inside(root: Path, rel: str, *, link_ok: bool = False) -> Path:
    path = root / rel
    root_resolved = root.resolve()
    # A preserved link is an object in the tree, not its target's path.
    target = path.parent.resolve() / path.name if link_ok else path.resolve()
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
    listing: list | None = None,
) -> ApplyReport:
    """Apply a serial plan with legacy operation events and byte-level copy events."""

    left_root = Path(profile.left)
    right_root = Path(profile.right)
    report = ApplyReport()
    ordered = sorted(changes, key=_apply_order)
    progress = _CopyProgressReporter(ordered, report, on_progress, on_copy_progress)
    limiter = _RateLimiter(bytes_per_second)
    cache_invalidated = False

    def invalidate_before_write() -> bool:
        nonlocal cache_invalidated
        if cache_invalidated:
            return True
        try:
            invalidate_destination(profile)
        except OSError as exc:
            report.errors.append(f"Destination cache invalidation stopped the operation: {exc}")
            progress.finish()
            return False
        cache_invalidated = True
        return True

    deleting = any(change.action in (Action.DELETE_LEFT, Action.DELETE_RIGHT) for change in ordered)
    if deleting and listing is None and (profile.max_delete_percent or profile.block_delete_all):
        report.errors.append("Deletion protection requires the complete scan listing; no operations were applied.")
        progress.finish()
        return report
    if deleting:
        from ofs.review import Listed

        policy_rows = listing if listing is not None else [Listed(change, False, False) for change in ordered]
        violations = deletion_violations(profile, policy_rows)
        if violations:
            report.errors.extend(f"Deletion protection: {message}" for message in violations)
            progress.finish()
            return report
    layout_errors = _link_layout_problems(profile, ordered)
    if layout_errors:
        report.errors.extend(layout_errors)
        progress.finish()
        return report
    for index, change in enumerate(ordered):
        progress.begin(index, change)
        if control is not None and not control.checkpoint():
            report.cancelled = True
            break
        if change.action == Action.ASK:
            report.skipped += 1
            progress.complete(index, change)
            continue
        if change.action in {Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT, Action.MOVE_ON_RIGHT, Action.MOVE_ON_LEFT, Action.DELETE_RIGHT, Action.DELETE_LEFT}:
            if not invalidate_before_write():
                return report
        try:
            if change.action == Action.COPY_LEFT_TO_RIGHT:
                _copy(profile, left_root, right_root, change.rel, report, "right", control, progress, limiter)
            elif change.action == Action.COPY_RIGHT_TO_LEFT:
                _copy(profile, right_root, left_root, change.rel, report, "left", control, progress, limiter)
            elif change.action == Action.DELETE_RIGHT:
                _delete(profile, right_root, change.rel, report, "right")
            elif change.action == Action.DELETE_LEFT:
                _delete(profile, left_root, change.rel, report, "left")
            elif change.action == Action.MOVE_ON_RIGHT:
                _move(profile, right_root, change.rel, change.dest_rel, report, "right")
            elif change.action == Action.MOVE_ON_LEFT:
                _move(profile, left_root, change.rel, change.dest_rel, report, "left")
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


def _link_layout_problems(profile: Profile, changes: list[Change]) -> list[str]:
    """Do not partially execute a link/directory transition in Preserve mode."""

    if profile.symlinks != SymlinkMode.PRESERVE:
        return []
    roots = (Path(profile.left), Path(profile.right))
    errors = []

    def link_parents(root: Path, rel: str) -> list[str]:
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            return [f"{rel}: invalid relative path"]
        current = root
        links = []
        for part in path.parts[:-1]:
            if part == ".":
                continue
            current /= part
            if current.is_symlink():
                links.append(current.relative_to(root).as_posix())
        return links

    for change in changes:
        action = change.action
        operations: list[tuple[Path, str, Path, str]] = []
        if action == Action.COPY_LEFT_TO_RIGHT:
            operations = [(roots[0], change.rel, roots[1], change.rel)]
        elif action == Action.COPY_RIGHT_TO_LEFT:
            operations = [(roots[1], change.rel, roots[0], change.rel)]
        elif action == Action.MOVE_ON_RIGHT:
            operations = [(roots[1], change.rel, roots[1], change.dest_rel)]
        elif action == Action.MOVE_ON_LEFT:
            operations = [(roots[0], change.rel, roots[0], change.dest_rel)]
        elif action in (Action.DELETE_RIGHT, Action.DELETE_LEFT):
            root = roots[1] if action == Action.DELETE_RIGHT else roots[0]
            operations = [(root, change.rel, root, change.rel)]
        for source_root, rel, dest_root, dest_rel in operations:
            for root, one_rel in ((source_root, rel), (dest_root, dest_rel)):
                for parent in link_parents(root, one_rel):
                    message = f"{one_rel}: preserved link {parent!r} is a parent of a planned entry; resolve the link/directory conflict first."
                    if message not in errors:
                        errors.append(message)
            if action in (Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT):
                source = source_root / rel
                destination = dest_root / dest_rel
                if source.is_symlink() and destination.is_dir() and not destination.is_symlink():
                    errors.append(f"{rel}: source link conflicts with a real destination directory; resolve it before copying.")
    return errors


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


def _move(profile: Profile, root: Path, old_rel: str, new_rel: str, report: ApplyReport, side: str) -> None:
    preserving = profile.symlinks == SymlinkMode.PRESERVE
    source = _inside(root, old_rel, link_ok=preserving)
    dest = _inside(root, new_rel)
    if not (source.is_file() or (preserving and source.is_symlink())):
        raise ApplyError("source is missing")
    if os.path.lexists(dest):
        raise ApplyError("destination already exists")
    size = source.lstat().st_size if preserving else source.stat().st_size
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
    preserving = profile.symlinks == SymlinkMode.PRESERVE
    source = _inside(source_root, rel, link_ok=preserving)
    dest = _inside(dest_root, rel, link_ok=preserving)
    if preserving and source.is_symlink():
        info = source.lstat()
    elif source.is_file():
        info = source.stat()
    else:
        raise ApplyError("source is missing, dangling, or inconsistent with the symlink policy")
    if os.path.lexists(dest) and dest.is_dir() and not dest.is_symlink():
        raise ApplyError("refusing to replace a real destination directory with a file or link")
    size = info.st_size
    if progress is not None:
        progress.chunk(0, size)
    _mkdirs(dest_root, dest, report)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".ofs-", suffix=".part", dir=dest.parent)
        temporary = Path(name)
        if preserving and source.is_symlink():
            os.close(descriptor)
            temporary.unlink()
            os.symlink(os.readlink(source), temporary)
            try:
                shutil.copystat(source, temporary, follow_symlinks=False)
            except NotImplementedError:
                # Some platforms cannot update link timestamps; the link target is still exact.
                pass
            if progress is not None:
                progress.chunk(size, size)
        else:
            with source.open("rb") as reading, os.fdopen(descriptor, "wb") as writing:
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
        if profile.preserve_ownership:
            os.chown(temporary, info.st_uid, info.st_gid, follow_symlinks=False)
        # Once archiving starts, do not pause between it and the atomic replace.
        _copy_checkpoint(control)
        archived = bool(os.path.lexists(dest) and profile.keep_older)
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
    archive = _inside(dest_root, f"{folder}/{rel}", link_ok=True)
    keep = profile.older_keep if profile.older_keep > 1 else 1
    _mkdirs(dest_root, archive, report)
    if keep == 1:
        if os.path.lexists(archive):
            archive.unlink()
        shutil.move(str(dest), str(archive))
        return
    slots = [archive]
    for index in range(1, keep):
        slots.append(archive.with_name(f"{archive.name}.~{index}~"))
    oldest = slots[-1]
    if os.path.lexists(oldest):
        oldest.unlink()
        report.versions_removed += 1
    for index in range(len(slots) - 2, -1, -1):
        source = slots[index]
        target = slots[index + 1]
        if os.path.lexists(source):
            if os.path.lexists(target):
                target.unlink()
            source.rename(target)
    shutil.move(str(dest), str(archive))


def _restore_older(profile: Profile, dest_root: Path, dest: Path, rel: str) -> None:
    """Best-effort rollback after replace fails, preserving every surviving version."""

    folder = profile.older_folder.strip("/") or "Older"
    archive = _inside(dest_root, f"{folder}/{rel}", link_ok=True)
    if not os.path.lexists(archive):
        raise ApplyError("versioned destination backup is missing")
    keep = profile.older_keep if profile.older_keep > 1 else 1
    slots = [archive]
    for index in range(1, keep):
        slots.append(archive.with_name(f"{archive.name}.~{index}~"))
    archive.replace(dest)
    for index in range(len(slots) - 1):
        source = slots[index + 1]
        if os.path.lexists(source):
            source.replace(slots[index])


def _delete(profile: Profile, root: Path, rel: str, report: ApplyReport, side: str) -> None:
    target = _inside(root, rel, link_ok=True)
    if not os.path.lexists(target):
        return
    if target.is_dir() and not target.is_symlink():
        raise ApplyError("refusing to delete a directory entry from a file plan")
    size = target.lstat().st_size
    parent = target.parent
    if profile.keep_deleted:
        _keep_older(profile, root, target, rel, report)
        report.deletions_archived += 1
    else:
        target.unlink()
    _count(report, side, "delete", size)
    _prune_empty(root, parent, report)
