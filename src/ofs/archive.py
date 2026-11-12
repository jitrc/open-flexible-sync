"""List and safely restore files preserved under a profile's Older tree."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ofs.model import Profile

_VERSION = re.compile(r"^(?P<name>.+)\.~(?P<number>[1-9][0-9]*)~$")


@dataclass(frozen=True)
class ArchivedFile:
    profile: Profile
    side: str
    relative: str
    version: int
    path: Path
    size: int
    modified: float

    @property
    def label(self) -> str:
        version = "newest" if self.version == 0 else f"version -{self.version}"
        return f"{self.side}: {self.relative} ({version})"


def _base(profile: Profile, side: str) -> Path:
    root = Path(profile.left if side == "left" else profile.right)
    folder = profile.older_folder.strip("/") or "Older"
    return root.joinpath(*folder.split("/"))


def older_files(profile: Profile) -> list[ArchivedFile]:
    """Present versions, including archived deletions, newest first per path."""

    found: list[ArchivedFile] = []
    for side in ("left", "right"):
        base = _base(profile, side)
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            relative = path.relative_to(base).as_posix()
            match = _VERSION.fullmatch(path.name)
            if match:
                version = int(match.group("number"))
                relative = str(Path(relative).with_name(match.group("name"))).replace(os.sep, "/")
            else:
                version = 0
            info = path.lstat()
            found.append(ArchivedFile(profile, side, relative, version, path, info.st_size, info.st_mtime))
    found.sort(key=lambda item: (item.side, item.relative.lower(), item.version))
    return found


def restore_version(item: ArchivedFile, destination_root: Path, *, overwrite: bool = False) -> Path:
    """Copy a version to a separate tree, atomically, without touching live roots."""

    destination_root = Path(destination_root)
    live_roots = [Path(item.profile.left).resolve(), Path(item.profile.right).resolve()]
    root = destination_root.resolve()
    if any(root == live or live in root.parents for live in live_roots):
        raise ValueError("choose a restore folder outside both synchronized folders")
    relative = Path(*item.relative.split("/"))
    if item.relative.startswith("/") or ".." in relative.parts:
        raise ValueError("invalid archived relative path")
    target = root / relative
    if root not in target.resolve().parents:
        raise ValueError("restore path escapes the selected folder")
    if not item.path.is_file() and not item.path.is_symlink():
        raise OSError("archived entry is missing or is not a file/symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ofs-restore-", suffix=".part", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if item.path.is_symlink():
            temporary.unlink()
            os.symlink(os.readlink(item.path), temporary)
            try:
                shutil.copystat(item.path, temporary, follow_symlinks=False)
            except NotImplementedError:
                pass
        else:
            shutil.copy2(item.path, temporary)
        if item.profile.preserve_ownership:
            info = item.path.lstat()
            os.chown(temporary, info.st_uid, info.st_gid, follow_symlinks=False)
        if overwrite:
            os.replace(temporary, target)
        else:
            # temp and target share one filesystem: link is an atomic no-clobber commit.
            os.link(temporary, target, follow_symlinks=False)
            temporary.unlink()
        return target
    finally:
        temporary.unlink(missing_ok=True)
