"""Refuse a scan when a profile's folder is not on the named volume."""

from __future__ import annotations

import os
import re
from pathlib import Path

from sfs.walk import MissingVolume

_ESCAPE = re.compile(r"\\x([0-9A-Fa-f]{2})")


def decode_udev_name(name: str) -> str:
    """Turn a `/dev/disk/by-label` file name into the label. Spaces are `\\x20`."""

    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), name)


def label_devices() -> dict[str, int]:
    """Map a volume label to the device id of that disk. Missing folder means no labels."""

    folder = Path("/dev/disk/by-label")
    found: dict[str, int] = {}
    if not folder.is_dir():
        return found
    for entry in folder.iterdir():
        try:
            found[decode_udev_name(entry.name)] = entry.stat().st_rdev
        except OSError:
            continue
    return found


def path_device(path: str) -> int | None:
    """Device id of the filesystem that holds `path`, walking up to a path that exists."""

    current = Path(path)
    while not current.exists() and current != current.parent:
        current = current.parent
    try:
        return os.stat(current).st_dev
    except OSError:
        return None


def ensure_volume(
    path: str,
    label: str,
    *,
    devices: dict[str, int] | None = None,
    device_of=path_device,
) -> None:
    """Raise MissingVolume when `label` is set and `path` is not on that volume.

    An empty label does not check. A label that is not mounted, or a path on a
    different disk, both fail before any scan.
    """

    label = label.strip()
    if not label:
        return
    known = label_devices() if devices is None else devices
    if known.get(label) is None or device_of(path) != known.get(label):
        raise MissingVolume(f"{label} ({path})")
