"""Decide what a profile would do. This module does not touch the disk."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from ofs.masks import any_match, included
from ofs.model import Direction, ExclusionMode, Profile, SizeConflict, SyncMode, TimestampRules

SMALL_SECONDS = 2.0
HOUR_SECONDS = 3600.0


class Action(str, Enum):
    COPY_LEFT_TO_RIGHT = "copy_l2r"
    COPY_RIGHT_TO_LEFT = "copy_r2l"
    DELETE_LEFT = "delete_left"
    DELETE_RIGHT = "delete_right"
    MOVE_ON_LEFT = "move_on_left"
    MOVE_ON_RIGHT = "move_on_right"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    ASK = "ask"


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime: float


@dataclass(frozen=True)
class Change:
    rel: str
    action: Action
    reason: str
    size: int = 0
    dest_rel: str = ""


_COPY = {Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT}
_DELETE = {Action.DELETE_LEFT, Action.DELETE_RIGHT}
_MOVE = {Action.MOVE_ON_LEFT, Action.MOVE_ON_RIGHT}


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} B"
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        value /= 1024
    return f"{size} B"


def plan_summary(changes: list[Change]) -> str:
    """How much the remaining plan would copy and delete. Nothing is touched."""

    copy_count = copy_bytes = delete_count = delete_bytes = 0
    move_count = move_bytes = ask_count = ask_bytes = 0
    for change in changes:
        if change.action in _COPY:
            copy_count += 1
            copy_bytes += change.size
        elif change.action in _DELETE:
            delete_count += 1
            delete_bytes += change.size
        elif change.action in _MOVE:
            move_count += 1
            move_bytes += change.size
        elif change.action == Action.ASK:
            ask_count += 1
            ask_bytes += change.size

    def files(count: int) -> str:
        return "1 file" if count == 1 else f"{count} files"

    text = (
        f"{files(copy_count)}, {format_bytes(copy_bytes)} to copy. "
        f"{files(delete_count)}, {format_bytes(delete_bytes)} to delete."
    )
    if move_count:
        text += f" {files(move_count)}, {format_bytes(move_bytes)} to move."
    if ask_count:
        text += f" {files(ask_count)}, {format_bytes(ask_bytes)} to decide."
    return text


def classify_time(
    left_mtime: float,
    right_mtime: float,
    left_size: int,
    right_size: int,
    rules: TimestampRules,
) -> str:
    """Return equal, left_newer, right_newer, or size_conflict."""

    if rules.ignore_timestamp:
        if left_size == right_size:
            return "equal"
        if rules.larger_is_newer:
            return "left_newer" if left_size > right_size else "right_newer"
        return "size_conflict"

    left_time = left_mtime
    right_time = right_mtime
    if rules.ignore_seconds:
        left_time = float(int(left_time) // 60 * 60)
        right_time = float(int(right_time) // 60 * 60)
    delta = left_time - right_time
    if rules.ignore_exact_hour and abs(abs(delta) - HOUR_SECONDS) <= SMALL_SECONDS:
        delta = 0.0
    if rules.ignore_small and abs(delta) <= SMALL_SECONDS:
        delta = 0.0
    if delta == 0.0:
        if left_size == right_size:
            return "equal"
        if rules.larger_is_newer:
            return "left_newer" if left_size > right_size else "right_newer"
        return "size_conflict"
    return "left_newer" if delta > 0 else "right_newer"


def outside_filter(stat: FileStat, profile: Profile, now: float) -> str | None:
    """Why this file is outside the profile's size and age limits. None when it is in range.

    Zero means that limit is off. Age is in days.
    """

    if profile.min_size and stat.size < profile.min_size:
        return "smaller than the minimum size"
    if profile.max_size and stat.size > profile.max_size:
        return "larger than the maximum size"
    age = now - stat.mtime
    if profile.min_age_days and age < profile.min_age_days * 86400:
        return "newer than the minimum age"
    if profile.max_age_days and age > profile.max_age_days * 86400:
        return "older than the maximum age"
    return None


def _limits_on(profile: Profile) -> bool:
    return bool(profile.min_size or profile.max_size or profile.min_age_days or profile.max_age_days)


def _hidden(
    rel: str,
    profile: Profile,
    session_masks: list[str],
    global_masks: list[str],
    global_mode: ExclusionMode = ExclusionMode.IGNORE_TOTALLY,
) -> str | None:
    """ignore = drop from both sides. dont_copy = do not transfer, but Exact Mirror may delete the other side."""

    if rel in _skip_names(profile) or any_match(rel, session_masks):
        return "ignore"
    if any_match(rel, global_masks):
        return "dont_copy" if global_mode == ExclusionMode.DONT_COPY else "ignore"
    if not included(rel, profile.include):
        return "ignore"
    if any_match(rel, profile.exclusions):
        if profile.exclusion_mode == ExclusionMode.IGNORE_TOTALLY:
            return "ignore"
        return "dont_copy"
    return None


def _skip_names(profile: Profile) -> set[str]:
    return set()


def build_plan(
    profile: Profile,
    left: Mapping[str, FileStat],
    right: Mapping[str, FileStat],
    *,
    session_skips: set[str] | None = None,
    session_masks: list[str] | None = None,
    global_masks: list[str] | None = None,
    global_mode: ExclusionMode = ExclusionMode.IGNORE_TOTALLY,
    now: float | None = None,
) -> list[Change]:
    skips = session_skips or set()
    masks = list(session_masks or [])
    globals_ = list(global_masks or [])
    clock = time.time() if now is None else now
    older = profile.older_folder.strip("/") or "Older"
    changes: list[Change] = []
    paths = sorted(set(left) | set(right))
    for rel in paths:
        if rel == older or rel.startswith(older + "/"):
            continue
        if rel in skips:
            continue
        kind = _hidden(rel, profile, masks, globals_, global_mode)
        left_stat = left.get(rel)
        right_stat = right.get(rel)
        if kind == "ignore":
            continue
        if kind == "dont_copy":
            if profile.mode == SyncMode.EXACT_MIRROR and profile.direction != Direction.BOTH:
                if profile.direction == Direction.LEFT_TO_RIGHT and right_stat is not None:
                    changes.append(Change(rel, Action.DELETE_RIGHT, "excluded, destination copy is an extra", right_stat.size))
                elif profile.direction == Direction.RIGHT_TO_LEFT and left_stat is not None:
                    changes.append(Change(rel, Action.DELETE_LEFT, "excluded, destination copy is an extra", left_stat.size))
            continue
        if profile.direction == Direction.BOTH:
            decided = _both(profile, rel, left_stat, right_stat)
        elif profile.direction == Direction.RIGHT_TO_LEFT:
            decided = _one_way(profile, rel, right_stat, left_stat, to_left=True)
        else:
            decided = _one_way(profile, rel, left_stat, right_stat, to_left=False)
        changes.extend(_guard_limits(profile, rel, decided, left_stat, right_stat, clock))
    return _pair_mirror_moves(profile, changes, left, right)


def _pair_mirror_moves(
    profile: Profile,
    changes: list[Change],
    left: Mapping[str, FileStat],
    right: Mapping[str, FileStat],
) -> list[Change]:
    """Turn a unique copy-plus-delete pair into a move. No file database required.

    Exact Mirror would copy the new path and delete the old one. When one file
    that exists only on the source matches one file that exists only on the
    destination, under the profile's timestamp rules, that pair is a move.
    Two files of the same size stay a copy and a delete.
    """

    if profile.mode != SyncMode.EXACT_MIRROR or profile.direction == Direction.BOTH:
        return changes
    to_left = profile.direction == Direction.RIGHT_TO_LEFT
    if to_left:
        copy_action = Action.COPY_RIGHT_TO_LEFT
        delete_action = Action.DELETE_LEFT
        move_action = Action.MOVE_ON_LEFT
        source, dest = right, left
    else:
        copy_action = Action.COPY_LEFT_TO_RIGHT
        delete_action = Action.DELETE_RIGHT
        move_action = Action.MOVE_ON_RIGHT
        source, dest = left, right
    copies = [change.rel for change in changes if change.action == copy_action and change.reason == "only on source"]
    deletes = [change.rel for change in changes if change.action == delete_action and change.reason == "only on destination"]
    pairs: list[tuple[str, str]] = []
    for old in deletes:
        old_stat = dest.get(old)
        if old_stat is None:
            continue
        hits = []
        for new in copies:
            new_stat = source.get(new)
            if new_stat is None:
                continue
            state = classify_time(old_stat.mtime, new_stat.mtime, old_stat.size, new_stat.size, profile.timestamps)
            if state == "equal":
                hits.append(new)
        if len(hits) == 1:
            pairs.append((old, hits[0]))
    used = [new for _, new in pairs]
    unique = [(old, new) for old, new in pairs if used.count(new) == 1]
    if not unique:
        return changes
    drop_old = {old for old, _new in unique}
    drop_new = {new for _old, new in unique}
    kept = [
        change
        for change in changes
        if not (
            (change.action == delete_action and change.rel in drop_old)
            or (change.action == copy_action and change.rel in drop_new)
        )
    ]
    for old, new in unique:
        kept.append(Change(old, move_action, f"moved to {new}", source[new].size, new))
    return kept


def _guard_limits(
    profile: Profile,
    rel: str,
    decided: list[Change],
    left: FileStat | None,
    right: FileStat | None,
    now: float,
) -> list[Change]:
    if not decided or not _limits_on(profile):
        return decided
    guarded: list[Change] = []
    for change in decided:
        stat = _limit_stat(change.action, left, right)
        if stat is not None:
            reason = outside_filter(stat, profile, now)
            if reason:
                guarded.append(Change(rel, Action.SKIPPED, reason, stat.size))
                continue
        guarded.append(change)
    return guarded


def _limit_stat(action: Action, left: FileStat | None, right: FileStat | None) -> FileStat | None:
    if action in (Action.COPY_LEFT_TO_RIGHT, Action.DELETE_LEFT, Action.ASK):
        return left or right
    if action in (Action.COPY_RIGHT_TO_LEFT, Action.DELETE_RIGHT):
        return right or left
    return None


def _one_way(
    profile: Profile,
    rel: str,
    source: FileStat | None,
    dest: FileStat | None,
    *,
    to_left: bool,
) -> list[Change]:
    copy_action = Action.COPY_RIGHT_TO_LEFT if to_left else Action.COPY_LEFT_TO_RIGHT
    delete_action = Action.DELETE_LEFT if to_left else Action.DELETE_RIGHT
    mirror = profile.mode == SyncMode.EXACT_MIRROR
    if source is None and dest is None:
        return []
    if source is None:
        if mirror and dest is not None:
            return [Change(rel, delete_action, "only on destination", dest.size)]
        return []
    if dest is None:
        return [Change(rel, copy_action, "only on source", source.size)]
    # Arguments are source then destination, so left_newer means the source is newer.
    state = classify_time(source.mtime, dest.mtime, source.size, dest.size, profile.timestamps)
    if state == "equal":
        if profile.always_copy:
            return [Change(rel, copy_action, "always copy", source.size)]
        return []
    if state == "left_newer":
        return [Change(rel, copy_action, "source is newer", source.size)]
    if state == "size_conflict":
        return _size_conflict(profile, rel, copy_action, source.size)
    return []


def _size_conflict(profile: Profile, rel: str, copy_action: Action, size: int) -> list[Change]:
    policy = profile.timestamps.same_time_different_size
    if policy == SizeConflict.SKIP:
        return []
    if policy == SizeConflict.ASK:
        return [Change(rel, Action.ASK, "same time, different size", size)]
    return [Change(rel, copy_action, "same time, different size", size)]


def _both(profile: Profile, rel: str, left: FileStat | None, right: FileStat | None) -> list[Change]:
    if left is None and right is None:
        return []
    if left is None and right is not None:
        return [Change(rel, Action.COPY_RIGHT_TO_LEFT, "only on right", right.size)]
    if right is None and left is not None:
        return [Change(rel, Action.COPY_LEFT_TO_RIGHT, "only on left", left.size)]
    assert left is not None and right is not None
    state = classify_time(left.mtime, right.mtime, left.size, right.size, profile.timestamps)
    if state == "equal":
        if profile.always_copy:
            return [Change(rel, Action.COPY_LEFT_TO_RIGHT, "always copy", left.size)]
        return []
    if state == "left_newer":
        return [Change(rel, Action.COPY_LEFT_TO_RIGHT, "left is newer", left.size)]
    if state == "right_newer":
        return [Change(rel, Action.COPY_RIGHT_TO_LEFT, "right is newer", right.size)]
    policy = profile.timestamps.same_time_different_size
    if policy == SizeConflict.SKIP:
        return []
    if policy == SizeConflict.ASK:
        return [Change(rel, Action.ASK, "same time, different size", left.size)]
    return [Change(rel, Action.COPY_LEFT_TO_RIGHT, "same time, different size, left wins", left.size)]
