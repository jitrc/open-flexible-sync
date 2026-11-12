"""JSON profile directory. Default is ~/.config/open-flexible-sync/profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from ofs.model import Profile


def config_dir() -> Path:
    override = os.environ.get("OFS_HOME") or os.environ.get("SFS_HOME")
    if override:
        return Path(override)
    new = Path.home() / ".config" / "open-flexible-sync"
    old = Path.home() / ".config" / "sfs"
    if old.exists() and not new.exists():
        old.rename(new)
    return new


def profiles_dir() -> Path:
    folder = config_dir() / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _path_for(name: str) -> Path:
    return profiles_dir() / f"{quote(name, safe='')}.json"


def install_imported(profiles: list[Profile]) -> int:
    """Save imported profiles. An existing job only gains flags still at the default."""

    from ofs.legacy import merge_imported

    have = {item.name: item for item in load_profiles()}
    for incoming in profiles:
        current = have.get(incoming.name)
        save_profile(incoming if current is None else merge_imported(current, incoming))
    return len(profiles)


def save_profile(profile: Profile) -> Path:
    path = _path_for(profile.name)
    path.write_text(json.dumps(profile.to_json(), indent=2) + "\n", encoding="utf-8")
    return path


def load_profiles() -> list[Profile]:
    folder = profiles_dir()
    profiles: list[Profile] = []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles.append(Profile.from_json(data))
    profiles.sort(key=lambda item: (item.group.lower(), item.name.lower()))
    return profiles


def delete_profile(name: str) -> None:
    path = _path_for(name)
    if path.exists():
        path.unlink()


def path_replacements(profile: Profile, find: str, replace: str, *, sides: str = "both", prefix_only: bool = False) -> dict[str, str]:
    """Preview changed Left/Right paths without mutating or normalizing them."""

    if sides not in {"both", "left", "right"}:
        raise ValueError("path replacement sides must be both, left, or right")
    if not find:
        return {}

    def replaced(value: str) -> str:
        if not prefix_only:
            return value.replace(find, replace)
        if not value.startswith(find):
            return value
        remainder = value[len(find):]
        if remainder and not remainder.startswith("/") and not find.endswith("/"):
            return value
        if not remainder:
            return replace
        if remainder.startswith("/") or find.endswith("/"):
            return replace.rstrip("/") + "/" + remainder.lstrip("/")
        return replace + remainder

    changed = {}
    for side in ("left", "right"):
        if sides not in {"both", side}:
            continue
        current = getattr(profile, side)
        updated = replaced(current)
        if updated != current:
            changed[side] = updated
    return changed


def replace_in_paths(
    profile: Profile,
    find: str,
    replace: str,
    *,
    sides: str = "both",
    prefix_only: bool = False,
) -> bool:
    """Apply the selected side/prefix replacement. Return whether a path changed."""

    changed = path_replacements(profile, find, replace, sides=sides, prefix_only=prefix_only)
    for side, updated in changed.items():
        setattr(profile, side, updated)
    return bool(changed)


def name_replacements(profiles: list[Profile], find: str, replace: str, *, prefix_only: bool = False) -> list[tuple[str, str]]:
    """Preview checked-profile name changes; validate before any document is written."""

    if not find:
        raise ValueError("enter text to find in profile names")
    changed = []
    names = set()
    for profile in profiles:
        old = profile.name
        if prefix_only:
            new = replace + old[len(find):] if old.startswith(find) else old
        else:
            new = old.replace(find, replace)
        if new == old:
            continue
        if new != new.strip() or not new or "=" in new:
            raise ValueError(f"invalid renamed profile: {new!r}; names cannot be empty, padded, or contain '='")
        if new in names:
            raise ValueError(f"more than one checked profile would be renamed to {new!r}")
        names.add(new)
        changed.append((old, new))
    return changed


def rename_profiles(changes: list[tuple[str, str]]) -> None:
    """Apply a validated set of names; never silently replace another profile."""

    existing = {profile.name: profile for profile in load_profiles()}
    validate_name_replacements(changes, list(existing.values()))
    documents = []
    for old, new in changes:
        if old not in existing or old == new:
            raise ValueError(f"invalid profile rename: {old!r} -> {new!r}")
        if new != new.strip() or not new or "=" in new:
            raise ValueError(f"invalid new profile name: {new!r}")
        if new in existing:
            raise ValueError(f"profile already exists: {new}")
        data = existing[old].to_json()
        data["name"] = new
        documents.append((old, new, Profile.from_json(data)))
    for old, new, document in documents:
        from ofs.history import delete_history

        save_profile(document)
        delete_profile(old)
        delete_history(old)


def validate_name_replacements(changes: list[tuple[str, str]], profiles: list[Profile]) -> None:
    """Perform all collision/name checks before writing any rename document."""

    if not changes:
        return
    if len({old for old, _new in changes}) != len(changes) or len({new for _old, new in changes}) != len(changes):
        raise ValueError("rename source and destination names must be unique")
    existing = {profile.name: profile for profile in profiles}
    for old, new in changes:
        if old not in existing or old == new:
            raise ValueError(f"invalid profile rename: {old!r} -> {new!r}")
        if new != new.strip() or not new or "=" in new:
            raise ValueError(f"invalid new profile name: {new!r}")
        if new in existing:
            raise ValueError(f"profile already exists: {new}")


def duplicate_profile(profile: Profile) -> Profile:
    """Save a copy under a new name. The file database is not copied."""

    names = {item.name for item in load_profiles()}
    base = f"{profile.name} copy"
    name = base
    number = 2
    while name in names:
        name = f"{base} {number}"
        number += 1
    data = profile.to_json()
    data["name"] = name
    data["last_result"] = ""
    copy = Profile.from_json(data)
    save_profile(copy)
    return copy
