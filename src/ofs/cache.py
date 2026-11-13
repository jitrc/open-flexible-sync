"""Opt-in persistent destination metadata cache. Never copy file contents from it."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from ofs.compare import FileStat
from ofs.model import Direction, Profile
from ofs.store import config_dir

FORMAT = "ofs-destination-cache"
VERSION = 1


def destination_sides(profile: Profile) -> tuple[str, ...]:
    if profile.direction == Direction.RIGHT_TO_LEFT:
        return ("Left",)
    if profile.direction == Direction.BOTH:
        return ("Left", "Right")
    return ("Right",)


def cache_path(name: str) -> Path:
    """Where one profile's destination snapshot lives. Never creates the folder."""

    return config_dir() / "cache" / f"{quote(name, safe='')}.json"


def _path(profile: Profile) -> Path:
    """Snapshot path for writing: the cache folder is created on demand."""

    folder = config_dir() / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return cache_path(profile.name)


def forget(name: str) -> None:
    """Remove a profile's snapshot without creating the cache folder."""

    cache_path(name).unlink(missing_ok=True)


def _settings_signature(profile: Profile, global_masks: list[str], global_mode) -> list:
    return [
        profile.include,
        profile.exclusions,
        profile.exclusion_mode.value,
        profile.symlinks.value,
        global_masks,
        global_mode.value,
    ]


def load_destination(profile: Profile, side: str, global_masks: list[str], global_mode) -> tuple[dict[str, FileStat], float] | None:
    if not profile.cache_destination:
        return None
    path = cache_path(profile.name)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format") != FORMAT or document.get("version") != VERSION:
            return None
        root = profile.right if side == "Right" else profile.left
        if document.get("root") != root or document.get("side") != side:
            return None
        if document.get("settings") != _settings_signature(profile, global_masks, global_mode):
            return None
        # Age is reported, not expired: periodic backup jobs may reuse this
        # destination walk for months. OFS writes and setting changes invalidate it.
        saved = float(document["saved"])
        if not math.isfinite(saved):
            return None
        files = {}
        for item in document["entries"]:
            rel = item["rel"]
            if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts:
                raise ValueError("invalid cached relative path")
            files[rel] = FileStat(
                int(item["size"]),
                float(item["mtime"]),
                item.get("kind", "file"),
                item.get("target", ""),
                int(item.get("mode", 0)),
                int(item.get("uid", 0)),
                int(item.get("gid", 0)),
            )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return files, saved


def save_destination(profile: Profile, side: str, global_masks: list[str], global_mode, files: dict[str, FileStat]) -> None:
    """Atomically save a completed walk; interrupted scans never produce a manifest."""

    document = {
        "format": FORMAT,
        "version": VERSION,
        "side": side,
        "root": profile.right if side == "Right" else profile.left,
        "settings": _settings_signature(profile, global_masks, global_mode),
        "saved": time.time(),
        "entries": [{"rel": rel, **asdict(info)} for rel, info in sorted(files.items())],
    }
    path = _path(profile)
    descriptor, name = tempfile.mkstemp(prefix=".ofs-cache-", suffix=".part", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def invalidate_destination(profile: Profile) -> None:
    """Remove this profile's snapshot after OFS writes, even for manual preview overrides.

    Reading the snapshot never creates the cache folder, so invalidation is a
    no-op for a profile that never saved one and cannot fail on a missing or
    read-only cache directory that holds nothing.
    """

    forget(profile.name)
