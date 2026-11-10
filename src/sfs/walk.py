"""Metadata walk. File contents are not read here."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from sfs.compare import FileStat


class MissingVolume(Exception):
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


class WalkStopped(Exception):
    """Pause/cancel control asked the walk to stop."""


def ensure_dir(path: str) -> Path:
    folder = Path(path)
    if not folder.is_dir():
        raise MissingVolume(path)
    return folder


def walk(root: Path, skip_dir, *, control=None, on_progress=None) -> dict[str, FileStat]:
    """Return relative file paths. skip_dir(rel) skips that folder and its children.

    Symlinks are recorded and not followed. Directory entries themselves are not
    returned; only files are. control.checkpoint() runs on each entry so pause
    and cancel take effect between files. on_progress(rel, count) is told about
    the current folder at most ten times a second.
    """

    found: dict[str, FileStat] = {}
    last_report = 0.0

    def report(rel: str, force: bool = False) -> None:
        nonlocal last_report
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and now - last_report < 0.1:
            return
        last_report = now
        on_progress(rel or ".", len(found))

    def check() -> None:
        if control is not None and not control.checkpoint():
            raise WalkStopped

    def visit(directory: Path, rel: str) -> None:
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
            if stat.S_ISLNK(info.st_mode):
                found[child] = FileStat(info.st_size, info.st_mtime)
                report(child)
                continue
            if stat.S_ISDIR(info.st_mode):
                if skip_dir(child):
                    continue
                visit(directory / entry.name, child)
                continue
            if stat.S_ISREG(info.st_mode):
                found[child] = FileStat(info.st_size, info.st_mtime)
                report(child)

    visit(root, "")
    return found
