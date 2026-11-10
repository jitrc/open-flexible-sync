"""Small local preferences. Profiles stay in their own files."""

from __future__ import annotations

import json
import os

from sfs.masks import split_masks
from sfs.model import ExclusionMode
from sfs.store import config_dir

_COMPARE_DEFAULT = "meld"
DEFAULT_GLOBAL_EXCLUSIONS = [
    ".DS_Store",
    "Thumbs.db",
    "Temporary Internet Files",
    "*.tmp",
    "*.$$$",
    "*.~*",
]


def load_settings() -> dict:
    path = config_dir() / "settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def compare_command() -> str:
    """Program used by Compare files. `SFS_COMPARE` wins over the saved setting."""

    override = os.environ.get("SFS_COMPARE", "").strip()
    if override:
        return override
    saved = str(load_settings().get("compare_command") or "").strip()
    return saved or _COMPARE_DEFAULT


def set_compare_command(command: str) -> None:
    _save("compare_command", command.strip() or _COMPARE_DEFAULT)


def global_exclusions() -> list[str]:
    """Masks ignored on every profile. The old defaults apply until Settings saves a list."""

    data = load_settings()
    if "global_exclusions" not in data:
        return list(DEFAULT_GLOBAL_EXCLUSIONS)
    raw = data["global_exclusions"]
    if isinstance(raw, str):
        return split_masks(raw)
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return list(DEFAULT_GLOBAL_EXCLUSIONS)


def set_global_exclusions(masks: list[str]) -> None:
    _save("global_exclusions", [mask.strip() for mask in masks if mask.strip()])


def global_exclusion_mode() -> ExclusionMode:
    """Ignore Totally keeps the pre-mode behavior: global matches vanish from the scan."""

    raw = str(load_settings().get("global_exclusion_mode") or "").strip()
    try:
        return ExclusionMode(raw)
    except ValueError:
        return ExclusionMode.IGNORE_TOTALLY


def set_global_exclusion_mode(mode: ExclusionMode) -> None:
    _save("global_exclusion_mode", mode.value)


def _save(key: str, value) -> None:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    data = load_settings()
    data[key] = value
    (folder / "settings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
