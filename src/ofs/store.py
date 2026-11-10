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


def replace_in_paths(profile: Profile, find: str, replace: str) -> bool:
    """Replace `find` in the left and right folders. Return True when a path changed."""

    if not find:
        return False
    left = profile.left.replace(find, replace)
    right = profile.right.replace(find, replace)
    if left == profile.left and right == profile.right:
        return False
    profile.left = left
    profile.right = right
    return True


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
