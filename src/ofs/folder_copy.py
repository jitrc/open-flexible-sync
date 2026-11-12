"""Explicit whole-folder copy into an absent destination, without comparison."""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ofs.apply import ApplyError, ApplyReport, CopyProgress, RunControl, _CopyStopped, _RateLimiter, _copy, _copy_checkpoint, _inside
from ofs.cache import invalidate_destination
from ofs.compare import Action, Change, FileStat, outside_filter
from ofs.masks import included, path_matches
from ofs.model import Direction, Profile, SymlinkMode
from ofs.settings import copy_speed_limit, global_exclusions
from ofs.walk import ensure_dir
from ofs.volume import ensure_volume

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class FolderCopyError(Exception):
    pass


@dataclass
class FolderCopyOutcome:
    report: ApplyReport
    changes: list[Change] = field(default_factory=list)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically install only an absent name; failure leaves the incumbent alone."""

    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(destination), _RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _clean_relative(folder: str) -> str:
    relative = PurePosixPath(folder.replace("\\", "/").strip())
    if not folder.strip() or str(relative) in {"", "."} or relative.is_absolute() or ".." in relative.parts:
        raise FolderCopyError("Choose a folder inside the profile, not an absolute path or profile root.")
    return relative.as_posix()


def _create_destination_parents(root: Path, destination: Path) -> list[Path]:
    root = root.resolve()
    current = destination.parent
    missing = []
    while current != root:
        if root not in current.parents:
            raise FolderCopyError("Destination parent is outside the profile folder.")
        if not current.exists():
            missing.append(current)
        current = current.parent
    if not root.is_dir():
        raise FolderCopyError("Destination profile folder must be available.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return list(reversed(missing))


def _remove_temporary_tree(path: Path) -> None:
    """Recursively remove only our own directory tree, without following links."""

    root = path.absolute()
    user_directory = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR

    def remove_file(file: Path) -> None:
        if file != root and root not in file.parents:
            raise OSError(f"Refusing to remove a path outside the temporary tree: {file}")
        try:
            file.unlink()
        except PermissionError:
            os.chmod(file.parent, user_directory)
            file.unlink()

    def remove_directory(directory: Path) -> None:
        if directory != root and root not in directory.parents:
            raise OSError(f"Refusing to remove a directory outside the temporary tree: {directory}")
        # chmod needs no directory-open callback and works on Python 3.11+.
        os.chmod(directory, user_directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                child = directory / entry.name
                if entry.is_dir(follow_symlinks=False):
                    remove_directory(child)
                else:
                    # Preserved links are unlinked, never followed to their targets.
                    remove_file(child)
        try:
            directory.rmdir()
        except PermissionError:
            os.chmod(directory.parent, user_directory)
            directory.rmdir()

    remove_directory(root)


def copy_new_folder(
    profile: Profile,
    folder: str,
    *,
    control: RunControl | None = None,
    on_progress=None,
    session_masks: list[str] | None = None,
    bytes_per_second: int | None = None,
) -> FolderCopyOutcome:
    """Stream a new tree to a temporary sibling, never overwriting a destination."""

    if profile.disabled:
        raise FolderCopyError("This profile is disabled. Enable it before copying a new folder.")
    if profile.direction == Direction.BOTH:
        raise FolderCopyError("Choose a one-way profile so the source and destination are explicit.")
    relative = _clean_relative(folder)
    ensure_volume(profile.left, profile.left_volume)
    ensure_volume(profile.right, profile.right_volume)
    source_root = ensure_dir(profile.left if profile.direction == Direction.LEFT_TO_RIGHT else profile.right)
    destination_root = ensure_dir(profile.right if profile.direction == Direction.LEFT_TO_RIGHT else profile.left)
    selected_source = source_root / relative
    if selected_source.is_symlink():
        if profile.symlinks == SymlinkMode.SKIP:
            raise FolderCopyError("Skip links is selected: a top-level symbolic link cannot be copied as a folder.")
        if profile.symlinks == SymlinkMode.PRESERVE:
            raise FolderCopyError("Use a normal scan for a top-level symbolic link; this copies directory trees.")
    source = _inside(source_root, relative)
    destination = _inside(destination_root, relative, link_ok=True)
    if source == destination or source in destination.parents:
        raise FolderCopyError("The destination folder is inside the selected source folder. Use separate folders or a normal scan.")
    if not source.is_dir():
        raise FolderCopyError("The selected source must be an accessible directory, not a file.")
    if os.path.lexists(destination):
        raise FolderCopyError("The matching destination already exists use a complete comparison scan instead.")

    globals_ = global_exclusions()
    sessions = list(session_masks or [])

    def excluded(profile_path: str) -> bool:
        return any(path_matches(profile_path, mask) for mask in [*globals_, *sessions, *profile.exclusions] if mask)

    if excluded(relative):
        raise FolderCopyError("The selected folder is excluded. Adjust the profile exclusions before using this action.")

    invalidate_destination(profile)
    extra_parents = _create_destination_parents(destination_root, destination)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.ofs-", suffix=".new", dir=destination.parent))
    report = ApplyReport()
    report.folders_created = 1 + len(extra_parents)
    outcome = FolderCopyOutcome(report)
    action = Action.COPY_LEFT_TO_RIGHT if profile.direction == Direction.LEFT_TO_RIGHT else Action.COPY_RIGHT_TO_LEFT
    side = "right" if profile.direction == Direction.LEFT_TO_RIGHT else "left"
    limit = copy_speed_limit() if bytes_per_second is None else max(0, int(bytes_per_second))
    limiter = _RateLimiter(limit)
    current = {"rel": relative, "bytes": 0, "total": 0, "active": False}
    committed = False

    def emit() -> None:
        if on_progress is None:
            return
        copied_bytes = getattr(report, side).copied_bytes
        if current["active"]:
            copied_bytes += current["bytes"]
        on_progress(CopyProgress(
            report.copied, 0, current["rel"], current["bytes"], current["total"],
            copied_bytes, 0, report.copied, report.deleted, report.moved, len(report.errors),
        ))

    def progress_for(path: str):
        class Reporter:
            def chunk(self, copied: int, size: int) -> None:
                current.update(rel=path, bytes=copied, total=size, active=True)
                emit()
        return Reporter()

    def copy_file(path: Path, inner: str, info: FileStat) -> None:
        current.update(rel=f"{relative}/{inner}", bytes=0, total=info.size, active=True)
        _copy(profile, source, temporary, inner, report, side, control, progress_for(current["rel"]), limiter)
        current["active"] = False
        outcome.changes.append(Change(current["rel"], action, "new folder; no destination comparison", info.size))
        emit()

    def copy_directory(path: Path, inner: str, ancestors: set[tuple[int, int]]) -> None:
        _copy_checkpoint(control)
        path = _inside(source, inner)
        info = path.stat()
        identity = info.st_dev, info.st_ino
        if identity in ancestors:
            raise FolderCopyError(f"Symbolic-link cycle detected at {relative}/{inner}")
        if not stat.S_ISDIR(info.st_mode):
            raise FolderCopyError(f"Expected a directory at {relative}/{inner}")
        directory_info = info
        target = temporary / inner if inner else temporary
        if not target.exists():
            target.mkdir()
            report.folders_created += 1
        with os.scandir(path) as entries:
            for entry in entries:
                _copy_checkpoint(control)
                child = f"{inner}/{entry.name}" if inner else entry.name
                profile_path = f"{relative}/{child}"
                if excluded(profile_path):
                    continue
                child_path = source / child
                try:
                    link_info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise FolderCopyError(f"Could not read {relative}/{child}: {exc}") from exc
                preserved_link = stat.S_ISLNK(link_info.st_mode) and profile.symlinks == SymlinkMode.PRESERVE
                if stat.S_ISLNK(link_info.st_mode) and profile.symlinks == SymlinkMode.SKIP:
                    continue
                if preserved_link:
                    file_info = link_info
                else:
                    file_info = _inside(source, child).stat()
                    if stat.S_ISDIR(file_info.st_mode):
                        copy_directory(child_path, child, ancestors | {identity})
                        continue
                    if not stat.S_ISREG(file_info.st_mode):
                        raise FolderCopyError(f"Unsupported non-file entry at {relative}/{child}")
                filter_info = FileStat(file_info.st_size, file_info.st_mtime)
                if not included(profile_path, profile.include) or outside_filter(filter_info, profile, time.time()):
                    continue
                copy_file(child_path, child, filter_info)
        shutil.copystat(path, target)
        if profile.preserve_ownership:
            os.chown(target, directory_info.st_uid, directory_info.st_gid)

    try:
        copy_directory(source, "", set())
        _copy_checkpoint(control)
        _rename_noreplace(temporary, destination)
        committed = True
    except _CopyStopped:
        report.cancelled = True
    except (OSError, ApplyError, FolderCopyError) as exc:
        report.errors.append(f"{relative}: {exc}")
    finally:
        if not committed:
            try:
                _remove_temporary_tree(temporary)
            except FileNotFoundError:
                pass
            except Exception as exc:
                # Cleanup failures must become a visible report, not strand the worker.
                report.errors.append(f"{relative}: could not remove incomplete temporary folder: {exc}")
            for parent in reversed(extra_parents):
                try:
                    parent.rmdir()
                except OSError:
                    # A concurrent non-empty folder or unrelated parent must survive.
                    pass
            # Temporary work is not a completed synchronization operation.
            outcome.changes.clear()
            report.copied = 0
            report.folders_created = 0
            report.left.copied = report.left.copied_bytes = 0
            report.right.copied = report.right.copied_bytes = 0
    return outcome
