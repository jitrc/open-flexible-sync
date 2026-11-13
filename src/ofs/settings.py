"""Small local preferences. Profiles stay in their own files."""

from __future__ import annotations

import json
import os
import threading

from ofs.masks import split_masks
from ofs.model import ExclusionMode
from ofs.store import config_dir

_COMPARE_DEFAULT = "meld"
DEFAULT_GLOBAL_EXCLUSIONS = [
    ".DS_Store",
    "Thumbs.db",
    "Temporary Internet Files",
    "*.tmp",
    "*.$$$",
    "*.~*",
]


_SETTINGS_LOCK = threading.RLock()


def load_settings() -> dict:
    """Serialize reads with writes so parallel scans never see a partial JSON file."""

    with _SETTINGS_LOCK:
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
    """Program used by Compare files. `OFS_COMPARE` wins over the saved setting."""

    override = os.environ.get("OFS_COMPARE") or os.environ.get("SFS_COMPARE") or ""
    override = override.strip()
    if override:
        return override
    saved = str(load_settings().get("compare_command") or "").strip()
    return saved or _COMPARE_DEFAULT


def theme() -> str:
    """Desktop appearance: system, light, or dark. Older settings use system."""

    value = load_settings().get("theme", "system")
    return value if value in {"system", "light", "dark"} else "system"


def set_theme(value: str) -> None:
    if value not in {"system", "light", "dark"}:
        raise ValueError("appearance must be system, light, or dark")
    _save("theme", value)


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


def copy_speed_limit() -> int:
    """Bytes per second across one copy run. Zero is unlimited."""

    try:
        return max(0, int(load_settings().get("copy_speed_limit") or 0))
    except (TypeError, ValueError):
        return 0


def set_copy_speed_limit(bytes_per_second: float) -> None:
    _save("copy_speed_limit", max(0, int(bytes_per_second)))


def scan_set_limit() -> int:
    """Files per bounded scan set. Zero keeps one complete scan."""

    try:
        return max(0, int(load_settings().get("scan_set_limit") or 0))
    except (TypeError, ValueError):
        return 0


def set_scan_set_limit(entries: float) -> None:
    _save("scan_set_limit", max(0, int(entries)))


def record_scan_estimate(name: str, entries: int, seconds: float) -> None:
    if entries <= 0 or seconds <= 0:
        return
    # This read-modify-write must be one transaction, not separate locked calls.
    with _SETTINGS_LOCK:
        estimates = load_settings().get("scan_estimates")
        estimates = dict(estimates) if isinstance(estimates, dict) else {}
        estimates[name] = {"entries": int(entries), "seconds": float(seconds)}
        _save("scan_estimates", estimates)


def scan_estimate(name: str) -> tuple[int, float]:
    raw = load_settings().get("scan_estimates")
    found = raw.get(name) if isinstance(raw, dict) else None
    if not isinstance(found, dict):
        return 0, 0.0
    try:
        entries = int(found.get("entries") or 0)
        seconds = float(found.get("seconds") or 0)
    except (TypeError, ValueError):
        return 0, 0.0
    return (entries, seconds) if entries > 0 and seconds > 0 else (0, 0.0)


def _save(key: str, value) -> None:
    # Completed parallel scans can record their estimates at the same time.
    with _SETTINGS_LOCK:
        folder = config_dir()
        folder.mkdir(parents=True, exist_ok=True)
        data = load_settings()
        data[key] = value
        (folder / "settings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
