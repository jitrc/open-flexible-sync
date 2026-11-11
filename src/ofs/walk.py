"""Metadata walk. File contents are not read here."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ofs.compare import FileStat


class MissingVolume(Exception):
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


class WalkStopped(Exception):
    """Pause/cancel control asked the walk to stop."""


class WalkSkipped(Exception):
    """Skip Rest asked to abandon this profile's incomplete scan."""


@dataclass(frozen=True)
class WalkStats:
    discovered: int
    filtered: int
    added: int


def ensure_dir(path: str) -> Path:
    folder = Path(path)
    if not folder.is_dir():
        raise MissingVolume(path)
    return folder


def walk(root: Path, skip_dir, *, skip_file=None, control=None, on_progress=None, on_stats=None) -> dict[str, FileStat]:
    """Return relative file paths, leaving ignored folders and files out.

    Symlinks are recorded and not followed. Control checks run between entries.
    on_progress(rel, count) retains the original progress callback; on_stats
    additionally receives discovered/filtered/added walk statistics.
    """

    found: dict[str, FileStat] = {}
    discovered = 0
    filtered = 0
    last_report = 0.0

    def report(rel: str, force: bool = False) -> None:
        nonlocal last_report
        if on_progress is None and on_stats is None:
            return
        now = time.monotonic()
        if not force and now - last_report < 0.1:
            return
        last_report = now
        if on_progress is not None:
            on_progress(rel or ".", len(found))
        if on_stats is not None:
            on_stats(rel or ".", WalkStats(discovered, filtered, len(found)))

    def check() -> None:
        if control is not None:
            if not control.checkpoint():
                raise WalkStopped
            if control.skipping:
                raise WalkSkipped

    def visit(directory: Path, rel: str) -> None:
        nonlocal discovered, filtered
        check()
        report(rel)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            check()
            child = f"{rel}/{entry.name}" if rel else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            discovered += 1
            if stat.S_ISLNK(info.st_mode):
                if skip_file is not None and skip_file(child):
                    filtered += 1
                    report(child)
                    continue
                found[child] = FileStat(info.st_size, info.st_mtime)
                report(child)
                continue
            if stat.S_ISDIR(info.st_mode):
                if skip_dir(child):
                    filtered += 1
                    report(child)
                    continue
                visit(directory / entry.name, child)
                continue
            if stat.S_ISREG(info.st_mode):
                if skip_file is not None and skip_file(child):
                    filtered += 1
                    report(child)
                    continue
                found[child] = FileStat(info.st_size, info.st_mtime)
                report(child)

    visit(root, "")
    report("", force=True)
    return found
