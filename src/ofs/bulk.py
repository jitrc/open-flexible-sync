"""Preview and apply one-field edits to the explicitly checked profiles."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ofs.masks import split_masks
from ofs.model import (
    Direction,
    ExclusionMode,
    Profile,
    SizeConflict,
    SymlinkMode,
    SyncMode,
    TimestampRules,
    parse_size,
)


@dataclass(frozen=True)
class BulkField:
    key: str
    label: str
    kind: str
    options: tuple[tuple[str, object], ...] = ()
    minimum: int = 0
    maximum: int = 0


_ON_OFF = (("Off", False), ("On", True))
FIELDS = (
    BulkField("direction", "Direction", "choice", (("Left to right", Direction.LEFT_TO_RIGHT), ("Right to left", Direction.RIGHT_TO_LEFT), ("Both directions", Direction.BOTH))),
    BulkField("mode", "Sync mode", "choice", (("Copy only (no deletions)", SyncMode.STANDARD), ("Exact Mirror", SyncMode.EXACT_MIRROR))),
    BulkField("exclusion_mode", "Exclusion mode", "choice", (("Ignore Totally", ExclusionMode.IGNORE_TOTALLY), ("Don't Copy", ExclusionMode.DONT_COPY))),
    BulkField("cache_destination", "Destination metadata cache (5 minutes)", "choice", _ON_OFF),
    BulkField("group_override", "Custom group / subgroups", "text"),
    BulkField("left_volume", "Left volume label", "text"),
    BulkField("right_volume", "Right volume label", "text"),
    BulkField("disabled", "Disabled", "choice", _ON_OFF),
    BulkField("keep_older", "Keep replaced files under Older", "choice", _ON_OFF),
    BulkField("keep_deleted", "Recoverable deletions under Older", "choice", _ON_OFF),
    BulkField("older_keep", "Older version count", "number", minimum=1, maximum=99),
    BulkField("max_delete_percent", "Maximum deletion percentage (0 = off)", "number", maximum=100),
    BulkField("block_delete_all", "Block deletion of every file in a folder", "choice", _ON_OFF),
    BulkField("never_delete_files", "Never delete files", "choice", _ON_OFF),
    BulkField("symlinks", "Symlink handling", "choice", (("Follow targets", SymlinkMode.FOLLOW), ("Preserve links", SymlinkMode.PRESERVE), ("Skip links", SymlinkMode.SKIP))),
    BulkField("compare_permissions", "Compare file permissions", "choice", _ON_OFF),
    BulkField("compare_ownership", "Compare file owner/group", "choice", _ON_OFF),
    BulkField("preserve_ownership", "Preserve owner/group on copy/restore", "choice", _ON_OFF),
    BulkField("binary_compare", "Binary comparison", "choice", _ON_OFF),
    BulkField("track", "Remember files and detect moves/deletions", "choice", _ON_OFF),
    BulkField("always_copy", "Always copy even when unchanged", "choice", _ON_OFF),
    BulkField("min_size", "Minimum file size (0 = no limit)", "size"),
    BulkField("max_size", "Maximum file size (0 = no limit)", "size"),
    BulkField("min_age_days", "Minimum file age in days (0 = no limit)", "decimal", maximum=1_000_000),
    BulkField("max_age_days", "Maximum file age in days (0 = no limit)", "decimal", maximum=1_000_000),
    BulkField("timestamps.ignore_small", "Ignore 2-second differences", "choice", _ON_OFF),
    BulkField("timestamps.ignore_exact_hour", "Ignore exact-hour differences", "choice", _ON_OFF),
    BulkField("timestamps.ignore_seconds", "Ignore seconds", "choice", _ON_OFF),
    BulkField("timestamps.ignore_timestamp", "Ignore timestamps", "choice", _ON_OFF),
    BulkField("timestamps.larger_is_newer", "Larger file is newer", "choice", _ON_OFF),
    BulkField("timestamps.same_time_different_size", "Same time, different size", "choice", (("Copy", SizeConflict.COPY), ("Skip", SizeConflict.SKIP), ("Ask", SizeConflict.ASK))),
    BulkField("include", "Include masks", "masks"),
    BulkField("exclusions", "Exclusion masks", "masks"),
)


def prepare_bulk_edit(profiles: list[Profile], field: str, value, *, add_masks: bool = False) -> list[Profile]:
    """Return independent copies that would change. Never mutate the supplied profiles."""

    definition = next((item for item in FIELDS if item.key == field), None)
    if definition is None:
        raise ValueError("choose a field to edit")
    if definition.kind == "choice" and value not in [option[1] for option in definition.options]:
        raise ValueError(f"invalid value for {definition.label}")
    if definition.kind == "number":
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("enter a whole number") from exc
        if not definition.minimum <= value <= definition.maximum:
            raise ValueError(f"{definition.label} must be between {definition.minimum} and {definition.maximum}")
    if definition.kind == "decimal":
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("enter a number of days") from exc
        if not math.isfinite(value) or not definition.minimum <= value <= definition.maximum:
            raise ValueError(f"{definition.label} must be between {definition.minimum} and {definition.maximum}")
    if definition.kind == "size":
        try:
            value = parse_size(str(value))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("enter a size such as 512, 1.5 KB, or 2M") from exc
    if definition.kind == "text":
        value = str(value).strip()
    masks: list[str] = []
    if definition.kind == "masks":
        masks = list(dict.fromkeys(split_masks(str(value))))
        if field == "include" and not masks:
            masks = ["(all)"]
        if add_masks and not masks:
            return []
    changed: list[Profile] = []
    for profile in profiles:
        updated = Profile.from_json(profile.to_json())
        if definition.kind == "masks":
            attribute = "include" if field == "include" else "exclusions"
            existing = list(getattr(updated, attribute))
            replacement = existing + [rule for rule in masks if rule not in existing] if add_masks else list(masks)
            setattr(updated, attribute, replacement)
        elif field.startswith("timestamps."):
            attribute = field.split(".", 1)[1]
            data = updated.timestamps.to_json()
            data[attribute] = value.value if isinstance(value, SizeConflict) else value
            updated.timestamps = TimestampRules.from_json(data)
        else:
            setattr(updated, field, value)
        if updated.to_json() != profile.to_json():
            changed.append(updated)
    return changed


def _value(profile: Profile, field: str):
    if field.startswith("timestamps."):
        return getattr(profile.timestamps, field.split(".", 1)[1])
    return getattr(profile, field)


def bulk_preview(changed: list[Profile], field: str, *, limit: int = 16) -> str:
    """Human-readable before/after confirmation with explicit profile names."""

    definition = next(item for item in FIELDS if item.key == field)
    lines = []
    for profile in changed[:limit]:
        value = _value(profile, field)
        if definition.kind == "masks":
            value = "; ".join(value) or "(none)"
        elif definition.key == "group_override":
            value = value or "(automatic from name)"
        lines.append(f"{profile.name}: {value}")
    if len(changed) > limit:
        lines.append(f"… and {len(changed) - limit} more profiles")
    return f"{definition.label} for {len(changed)} profile(s):\n" + "\n".join(lines)
