"""Profile documents. One JSON file per job."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class ExclusionMode(str, Enum):
    """How an exclusion mask participates in Exact Mirror."""

    DONT_COPY = "dont_copy"
    IGNORE_TOTALLY = "ignore_totally"


class Direction(str, Enum):
    LEFT_TO_RIGHT = "left_to_right"
    RIGHT_TO_LEFT = "right_to_left"
    BOTH = "both"


class SyncMode(str, Enum):
    STANDARD = "standard"
    EXACT_MIRROR = "exact_mirror"


class SizeConflict(str, Enum):
    """Same timestamp, different size."""

    COPY = "copy"
    SKIP = "skip"
    ASK = "ask"


@dataclass
class TimestampRules:
    ignore_small: bool = False
    ignore_exact_hour: bool = False
    ignore_seconds: bool = False
    ignore_timestamp: bool = False
    larger_is_newer: bool = False
    same_time_different_size: SizeConflict = SizeConflict.COPY

    def to_json(self) -> dict:
        data = asdict(self)
        data["same_time_different_size"] = self.same_time_different_size.value
        return data

    @classmethod
    def from_json(cls, data: dict | None) -> TimestampRules:
        data = dict(data or {})
        raw = data.get("same_time_different_size", SizeConflict.COPY)
        data["same_time_different_size"] = SizeConflict(raw)
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Profile:
    name: str
    left: str
    right: str
    left_volume: str = ""
    right_volume: str = ""
    include: list[str] = field(default_factory=lambda: ["(all)"])
    exclusions: list[str] = field(default_factory=list)
    exclusion_mode: ExclusionMode = ExclusionMode.IGNORE_TOTALLY
    direction: Direction = Direction.LEFT_TO_RIGHT
    mode: SyncMode = SyncMode.STANDARD
    timestamps: TimestampRules = field(default_factory=TimestampRules)
    keep_older: bool = False
    keep_deleted: bool = False
    older_folder: str = "Older"
    older_keep: int = 1
    min_size: int = 0
    max_size: int = 0
    min_age_days: float = 0
    max_age_days: float = 0
    binary_compare: bool = False
    track: bool = False
    always_copy: bool = False
    disabled: bool = False
    last_result: str = ""
    note: str = ""
    imported_settings: bool = False

    @property
    def group(self) -> str:
        stripped = self.name.strip()
        if not stripped:
            return ""
        return stripped.split()[0]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "left": self.left,
            "right": self.right,
            "left_volume": self.left_volume,
            "right_volume": self.right_volume,
            "include": list(self.include),
            "exclusions": list(self.exclusions),
            "exclusion_mode": self.exclusion_mode.value,
            "direction": self.direction.value,
            "mode": self.mode.value,
            "timestamps": self.timestamps.to_json(),
            "keep_older": self.keep_older,
            "keep_deleted": self.keep_deleted,
            "older_folder": self.older_folder,
            "older_keep": self.older_keep,
            "min_size": self.min_size,
            "max_size": self.max_size,
            "min_age_days": self.min_age_days,
            "max_age_days": self.max_age_days,
            "binary_compare": self.binary_compare,
            "track": self.track,
            "always_copy": self.always_copy,
            "disabled": self.disabled,
            "last_result": self.last_result,
            "note": self.note,
            "imported_settings": self.imported_settings,
        }

    @classmethod
    def from_json(cls, data: dict) -> Profile:
        return cls(
            name=data["name"],
            left=data.get("left", ""),
            right=data.get("right", ""),
            left_volume=data.get("left_volume") or "",
            right_volume=data.get("right_volume") or "",
            include=list(data.get("include") or ["(all)"]),
            exclusions=list(data.get("exclusions") or []),
            exclusion_mode=ExclusionMode(data.get("exclusion_mode", ExclusionMode.IGNORE_TOTALLY)),
            direction=Direction(data.get("direction", Direction.LEFT_TO_RIGHT)),
            mode=SyncMode(data.get("mode", SyncMode.STANDARD)),
            timestamps=TimestampRules.from_json(data.get("timestamps")),
            keep_older=bool(data.get("keep_older", False)),
            keep_deleted=bool(data.get("keep_deleted", False)),
            older_folder=data.get("older_folder") or "Older",
            older_keep=_positive_int(data.get("older_keep"), 1),
            min_size=_positive_int(data.get("min_size"), 0),
            max_size=_positive_int(data.get("max_size"), 0),
            min_age_days=_non_negative_float(data.get("min_age_days")),
            max_age_days=_non_negative_float(data.get("max_age_days")),
            binary_compare=bool(data.get("binary_compare", False)),
            track=bool(data.get("track", False)),
            always_copy=bool(data.get("always_copy", False)),
            disabled=bool(data.get("disabled", False)),
            last_result=data.get("last_result") or "",
            note=data.get("note") or "",
            imported_settings=bool(data.get("imported_settings", False)),
        )


def _positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default if default else 0


def _non_negative_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def parse_size(text: str) -> int:
    """Bytes from a field such as `512`, `1.5 KB`, or `2M`. Empty is no limit."""

    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return 0
    factor = 1
    for suffix, scale in (
        ("kb", 1024),
        ("mb", 1024**2),
        ("gb", 1024**3),
        ("k", 1024),
        ("m", 1024**2),
        ("g", 1024**3),
        ("b", 1),
    ):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            factor = scale
            break
    if not raw:
        return 0
    return max(0, int(float(raw) * factor))
