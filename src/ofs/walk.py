"""Metadata walk. File contents are not read here."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ofs.compare import FileStat


class MissingVolume(Exception):
    """A configured path cannot be used: the volume is absent or the folder is gone."""

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


class MissingFolder(MissingVolume):
    """The folder itself does not exist, although its volume may still be mounted."""

    def __init__(self, path: str):
        super().__init__(path)


class DestinationMissing(MissingFolder):
    """A one-way profile's destination root is absent while its source exists.

    `creatable` says whether the parent folder is still there, so creating the
    root is a single mkdir instead of building a path on the wrong filesystem.
    """

    def __init__(self, path: str, side: str, creatable: bool):
        super().__init__(path)
        self.side = side
        self.creatable = creatable


class WalkStopped(Exception):
    """Pause/cancel control asked the walk to stop."""


class WalkStalled(Exception):
    """No progress for a long time: the path may be unavailable, not merely slow."""

    def __init__(self, rel: str, seconds: float):
        super().__init__(rel)
        self.rel = rel
        self.seconds = seconds


class WalkSkipped(Exception):
    """Skip Rest preserves discovered metadata for a review-only listing."""

    def __init__(self, files: dict[str, FileStat] | None = None, discovered: int = 0, filtered: int = 0):
        super().__init__("scan skipped")
        self.files = dict(files or {})
        self.discovered = discovered
        self.filtered = filtered


@dataclass(frozen=True)
class WalkStats:
    discovered: int
    filtered: int
    added: int


def ensure_dir(path: str) -> Path:
    folder = Path(path)
    if not folder.is_dir():
        raise MissingFolder(path)
    return folder


def _file_stat(info, *, kind: str = "file", target: str = "") -> FileStat:
    """Metadata for one path, shared by the walk and by scoped single entries."""

    return FileStat(info.st_size, info.st_mtime, kind, target, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


def entry_stat(path: Path, *, symlinks: str = "follow") -> FileStat | None:
    """Metadata for one file or link, following the same rules as `walk`."""

    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        if symlinks == "skip":
            return None
        try:
            target = os.readlink(path)
        except OSError:
            return None
        if symlinks == "preserve":
            return _file_stat(info, kind="symlink", target=target)
        try:
            followed = path.stat()
        except OSError:
            return _file_stat(info, kind="broken", target=target)
        if stat.S_ISREG(followed.st_mode):
            return _file_stat(followed)
        return _file_stat(info, kind="unsupported", target=target)
    if stat.S_ISREG(info.st_mode):
        return _file_stat(info)
    return _file_stat(info, kind="unsupported")


def walk(
    root: Path,
    skip_dir,
    *,
    skip_file=None,
    control=None,
    on_progress=None,
    on_stats=None,
    on_entry=None,
    symlinks: str = "follow",
    on_unreadable=None,
    stall_seconds: float = 0,
) -> dict[str, FileStat]:
    """Return file/link metadata, with optional following and cycle protection."""


    found: dict[str, FileStat] = {}
    discovered = 0
    filtered = 0
    last_report = 0.0
    progress_at = time.monotonic()
    current = ""

    def report(rel: str, force: bool = False) -> None:
        nonlocal last_report, progress_at, current
        now = time.monotonic()
        if rel:
            current = rel
        progress_at = now
        if on_progress is None and on_stats is None:
            return
        if not force and now - last_report < 0.1:
            return
        last_report = now
        if on_progress is not None:
            on_progress(rel or ".", len(found))
        if on_stats is not None:
            on_stats(rel or ".", WalkStats(discovered, filtered, len(found)))

    def emit(rel: str) -> None:
        # Metadata still comes from the real walk; listeners never affect the plan.
        if on_entry is not None and rel in found:
            on_entry(rel, found[rel])

    def check() -> None:
        nonlocal progress_at
        if stall_seconds and time.monotonic() - progress_at > stall_seconds:
            raise WalkStalled(current or ".", stall_seconds)
        if control is not None:
            if not control.checkpoint():
                raise WalkStopped
            if control.skipping:
                raise WalkSkipped(found, discovered, filtered)

    def file_stat(info, path: Path, *, kind: str = "file", target: str = "") -> FileStat:
        return _file_stat(info, kind=kind, target=target)

    def filtered_out(child: str, *, directory: bool) -> bool:
        nonlocal filtered
        skip = skip_dir(child) if directory else skip_file is not None and skip_file(child)
        if not skip:
            return False
        filtered += 1
        report(child)
        return True

    def unreadable(rel: str, exc: OSError) -> None:
        # Never treat a folder that cannot be read as an empty folder: its contents are unknown.
        if on_unreadable is not None:
            on_unreadable(rel or ".", str(exc))

    def visit(directory: Path, rel: str, ancestors: set[tuple[int, int]]) -> bool:
        nonlocal discovered, filtered
        check()
        try:
            info = directory.stat()
            identity = (info.st_dev, info.st_ino)
        except OSError as exc:
            unreadable(rel, exc)
            return False
        if identity in ancestors:
            filtered += 1
            report(rel or ".")
            return False
        ancestors = ancestors | {identity}
        report(rel)
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            unreadable(rel, exc)
            return False
        # Listing a huge folder can take a while; it is not a stall.
        progress_at = time.monotonic()
        for entry in entries:
            check()
            child = f"{rel}/{entry.name}" if rel else entry.name
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                # An entry that cannot even be examined is unknown, not absent. Treating it as
                # absent is what turns a filesystem error into a silent mirror deletion.
                if not (skip_dir(child) or (skip_file is not None and skip_file(child))):
                    unreadable(child, exc)
                continue
            discovered += 1
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(path)
                except OSError:
                    continue
                if filtered_out(child, directory=False):
                    continue
                if symlinks == "skip":
                    filtered += 1
                    report(child)
                    continue
                if symlinks == "preserve":
                    found[child] = file_stat(info, path, kind="symlink", target=target)
                    emit(child)
                    report(child)
                    continue
                try:
                    followed = entry.stat(follow_symlinks=True)
                except OSError:
                    found[child] = file_stat(info, path, kind="broken", target=target)
                    emit(child)
                    report(child)
                    continue
                if stat.S_ISDIR(followed.st_mode):
                    if not filtered_out(child, directory=True):
                        if not visit(path, child, ancestors):
                            found[child] = file_stat(info, path, kind="unsupported", target=target)
                            emit(child)
                    continue
                if stat.S_ISREG(followed.st_mode):
                    found[child] = file_stat(followed, path)
                    emit(child)
                else:
                    # Never disappear a present source and mistake the other side for an extra.
                    found[child] = file_stat(info, path, kind="unsupported", target=target)
                    emit(child)
                report(child)
                continue
            if stat.S_ISDIR(info.st_mode):
                if not filtered_out(child, directory=True):
                    visit(path, child, ancestors)
                continue
            if stat.S_ISREG(info.st_mode) and not filtered_out(child, directory=False):
                found[child] = file_stat(info, path)
                emit(child)
                report(child)
        return True

    visit(root, "", set())
    report("", force=True)
    return found
