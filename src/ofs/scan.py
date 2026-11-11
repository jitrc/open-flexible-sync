"""Scan two folders and build a plan. Bytes are read only when binary compare is on."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ofs.apply import files_identical
from ofs.compare import Change, FileStat, build_plan, classify_time
from ofs.history import adjust_plan, load_history, save_history
from ofs.masks import any_match
from ofs.model import ExclusionMode, Profile
from ofs.review import Listed, build_listing
from ofs.settings import global_exclusion_mode, global_exclusions
from ofs.volume import ensure_volume
from ofs.walk import WalkSkipped, WalkStopped, ensure_dir, walk


@dataclass
class ScanResult:
    changes: list[Change]
    left_files: int = 0
    right_files: int = 0
    bytes_read: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""
    listing: list[Listed] = field(default_factory=list)


def _skip_dir(
    profile: Profile,
    rel: str,
    *,
    destination: bool,
    global_masks: list[str],
    global_mode: ExclusionMode = ExclusionMode.IGNORE_TOTALLY,
) -> bool:
    older = profile.older_folder.strip("/") or "Older"
    if destination and (rel == older or rel.startswith(older + "/")):
        return True
    if global_mode == ExclusionMode.IGNORE_TOTALLY and any_match(rel, global_masks):
        return True
    if not any_match(rel, profile.exclusions):
        return False
    return profile.exclusion_mode == ExclusionMode.IGNORE_TOTALLY


def _skip_file(
    profile: Profile,
    rel: str,
    *,
    global_masks: list[str],
    global_mode: ExclusionMode,
) -> bool:
    if global_mode == ExclusionMode.IGNORE_TOTALLY and any_match(rel, global_masks):
        return True
    return profile.exclusion_mode == ExclusionMode.IGNORE_TOTALLY and any_match(rel, profile.exclusions)


def _drop_global(
    files: dict[str, FileStat],
    global_masks: list[str],
    global_mode: ExclusionMode = ExclusionMode.IGNORE_TOTALLY,
) -> dict[str, FileStat]:
    if not global_masks or global_mode == ExclusionMode.DONT_COPY:
        return files
    return {rel: stat for rel, stat in files.items() if not any_match(rel, global_masks)}


def scan_profile(
    profile: Profile,
    *,
    session_skips: set[str] | None = None,
    session_masks: list[str] | None = None,
    control=None,
    on_progress=None,
    on_stats=None,
) -> ScanResult:
    masks = global_exclusions()
    mode = global_exclusion_mode()
    ensure_volume(profile.left, profile.left_volume)
    ensure_volume(profile.right, profile.right_volume)
    left_root = ensure_dir(profile.left)
    right_root = ensure_dir(profile.right)

    def progress(side: str):
        def report(rel: str, count: int) -> None:
            if on_progress is not None:
                on_progress(side, rel, count)

        return report

    def stats(side: str):
        def report(rel: str, values) -> None:
            if on_stats is not None:
                on_stats(side, rel, values)

        return report

    left = _drop_global(
        walk(
            left_root,
            lambda rel: _skip_dir(profile, rel, destination=False, global_masks=masks, global_mode=mode),
            skip_file=lambda rel: _skip_file(profile, rel, global_masks=masks, global_mode=mode),
            control=control,
            on_progress=progress("Left"),
            on_stats=stats("Left"),
        ),
        masks,
        mode,
    )
    right = _drop_global(
        walk(
            right_root,
            lambda rel: _skip_dir(profile, rel, destination=True, global_masks=masks, global_mode=mode),
            skip_file=lambda rel: _skip_file(profile, rel, global_masks=masks, global_mode=mode),
            control=control,
            on_progress=progress("Right"),
            on_stats=stats("Right"),
        ),
        masks,
        mode,
    )
    bytes_read = 0
    if profile.binary_compare:
        bytes_read = _equalize_identical(profile, left_root, right_root, left, right, control=control)
    changes = build_plan(
        profile,
        left,
        right,
        session_skips=session_skips,
        session_masks=session_masks,
        global_masks=masks,
        global_mode=mode,
    )
    note = ""
    if profile.track:
        history, note = load_history(profile)
        if history is None and not note:
            save_history(profile, left, right)
            note = "Recorded the current files. The next run can detect moves and deletions."
        elif history is not None:
            changes = adjust_plan(profile, changes, left, right, history, global_masks=masks)
    return ScanResult(
        changes,
        len(left),
        len(right),
        bytes_read,
        note=note,
        listing=build_listing(left, right, changes),
    )


def remember(profile: Profile) -> None:
    """Store the trees as they are on disk after a run."""

    if not profile.track:
        return
    masks = global_exclusions()
    mode = global_exclusion_mode()
    left_root = ensure_dir(profile.left)
    right_root = ensure_dir(profile.right)
    left = _drop_global(
        walk(left_root, lambda rel: _skip_dir(profile, rel, destination=False, global_masks=masks, global_mode=mode)),
        masks,
        mode,
    )
    right = _drop_global(
        walk(right_root, lambda rel: _skip_dir(profile, rel, destination=True, global_masks=masks, global_mode=mode)),
        masks,
        mode,
    )
    save_history(profile, left, right)


def _equalize_identical(
    profile: Profile,
    left_root: Path,
    right_root: Path,
    left: dict[str, FileStat],
    right: dict[str, FileStat],
    *,
    control=None,
) -> int:
    """If both sides have the same bytes, pretend the metadata matches too."""

    read = 0
    for rel in set(left) & set(right):
        if control is not None:
            if not control.checkpoint():
                raise WalkStopped
            if control.skipping:
                raise WalkSkipped
        state = classify_time(
            left[rel].mtime,
            right[rel].mtime,
            left[rel].size,
            right[rel].size,
            profile.timestamps,
        )
        if state == "equal" or left[rel].size != right[rel].size:
            continue
        read += left[rel].size + right[rel].size
        if files_identical(left_root / rel, right_root / rel):
            right[rel] = FileStat(left[rel].size, left[rel].mtime)
    return read
