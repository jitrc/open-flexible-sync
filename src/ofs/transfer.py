"""Versioned OFS profile bundles, with explicit duplicate handling."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ofs.model import Profile
from ofs.store import load_profiles, save_profile

FORMAT = "ofs-profiles"
VERSION = 1


def export_profiles(path: Path, profiles: list[Profile]) -> Path:
    """Atomically write one portable bundle; profiles keep all OFS settings."""

    if not profiles:
        raise ValueError("check at least one profile to export")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"format": FORMAT, "version": VERSION, "profiles": [profile.to_json() for profile in profiles]}
    descriptor, name = tempfile.mkstemp(prefix=".ofs-export-", suffix=".part", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_profile_bundle(path: Path) -> list[Profile]:
    """Validate the complete bundle before anything is saved."""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read the OFS profile bundle: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != FORMAT or document.get("version") != VERSION:
        raise ValueError("this is not a supported OFS profile bundle")
    entries = document.get("profiles")
    if not isinstance(entries, list) or not entries:
        raise ValueError("the bundle contains no profiles")
    profiles = []
    names = set()
    for data in entries:
        profile = _validate(data)
        if profile.name in names:
            raise ValueError(f"duplicate profile name in the bundle: {profile.name}")
        names.add(profile.name)
        profiles.append(profile)
    return profiles


def _validate(data) -> Profile:
    if not isinstance(data, dict):
        raise ValueError("each exported profile must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or "=" in name:
        raise ValueError("profile names must be nonempty and cannot contain '='")
    for field in ("left", "right", "left_volume", "right_volume", "older_folder", "group_override", "last_result", "last_scan_result", "note"):
        if field in data and not isinstance(data[field], str):
            raise ValueError(f"invalid {field} in profile {name}")
    for field in ("keep_older", "keep_deleted", "binary_compare", "track", "always_copy", "disabled", "block_delete_all", "never_delete_files", "compare_permissions", "compare_ownership", "preserve_ownership", "cache_destination", "imported_settings"):
        if field in data and not isinstance(data[field], bool):
            raise ValueError(f"invalid {field} in profile {name}")
    for field in ("include", "exclusions"):
        if field in data and (not isinstance(data[field], list) or not all(isinstance(rule, str) for rule in data[field])):
            raise ValueError(f"invalid {field} in profile {name}")
    timestamps = data.get("timestamps")
    if timestamps is not None and not isinstance(timestamps, dict):
        raise ValueError(f"invalid timestamps in profile {name}")
    if isinstance(timestamps, dict):
        for field in ("ignore_small", "ignore_exact_hour", "ignore_seconds", "ignore_timestamp", "larger_is_newer"):
            if field in timestamps and not isinstance(timestamps[field], bool):
                raise ValueError(f"invalid timestamp {field} in profile {name}")
    try:
        return Profile.from_json(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid profile {name}: {exc}") from exc


@dataclass(frozen=True)
class ImportResult:
    saved: int
    skipped: int
    renamed: int


def install_profile_bundle(profiles: list[Profile], *, duplicates: str) -> ImportResult:
    """duplicates is skip, rename, or replace; there is no implicit merge."""

    if duplicates not in {"skip", "rename", "replace"}:
        raise ValueError("choose skip, rename, or replace for duplicate names")
    existing = {profile.name: profile for profile in load_profiles()}
    names = set(existing)
    saved = skipped = renamed_count = 0
    for incoming in profiles:
        profile = Profile.from_json(incoming.to_json())
        if profile.name in existing:
            if duplicates == "skip":
                skipped += 1
                continue
            if duplicates == "rename":
                base = f"{profile.name} (imported)"
                name = base
                number = 2
                while name in names:
                    name = f"{base} {number}"
                    number += 1
                profile.name = name
                renamed_count += 1
            else:
                old = existing[profile.name]
                if old.left != profile.left or old.right != profile.right:
                    from ofs.history import delete_history

                    delete_history(profile.name)
        save_profile(profile)
        names.add(profile.name)
        existing[profile.name] = profile
        saved += 1
    return ImportResult(saved, skipped, renamed_count)
