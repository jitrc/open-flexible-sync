"""Scan two folders and build a plan. Bytes are read only when binary compare is on."""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from ofs.apply import files_identical
from ofs.cache import destination_sides, invalidate_destination, load_destination, save_destination
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
    discovered: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""
    cached: bool = False
    partial_sides: tuple[str, ...] = ()
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
    on_entry=None,
    force_uncached: bool = False,
) -> ScanResult:
    masks = global_exclusions()
    mode = global_exclusion_mode()
    ensure_volume(profile.left, profile.left_volume)
    ensure_volume(profile.right, profile.right_volume)
    left_root = ensure_dir(profile.left)
    right_root = ensure_dir(profile.right)
    walked = {"Left": 0, "Right": 0}
    rules_lock = getattr(control, "rules_lock", None) or nullcontext()
    session_filters_used = False

    def session_hidden(rel: str) -> bool:
        nonlocal session_filters_used
        with rules_lock:
            if not session_skips and not session_masks:
                return False
            session_filters_used = True
            return rel in (session_skips or set()) or any_match(rel, session_masks or [])

    def drop_session(files: dict[str, FileStat]) -> dict[str, FileStat]:
        return {rel: stat for rel, stat in files.items() if not session_hidden(rel)}

    def skip_dir(rel: str, destination: bool) -> bool:
        with rules_lock:
            return _skip_dir(profile, rel, destination=destination, global_masks=masks, global_mode=mode) or session_hidden(rel)

    def skip_file(rel: str) -> bool:
        with rules_lock:
            return _skip_file(profile, rel, global_masks=masks, global_mode=mode) or session_hidden(rel)

    def progress(side: str):
        def report(rel: str, count: int) -> None:
            if on_progress is not None:
                on_progress(side, rel, count)

        return report

    def stats(side: str):
        def report(rel: str, values) -> None:
            walked[side] = values.discovered
            if on_stats is not None:
                on_stats(side, rel, values)

        return report

    cache_notes = []
    cached_sides: set[str] = set()
    partial_sides = []

    def read_side(side: str, root: Path, destination: bool) -> dict[str, FileStat]:
        if destination and profile.cache_destination and not force_uncached:
            cached = load_destination(profile, side, masks, mode)
            if cached is not None:
                files, saved = cached
                cached_sides.add(side)
                files = drop_session(files)
                cache_notes.append(f"Using cached {side.lower()} destination metadata ({int(time.time() - saved)}s old; use Run uncached to force a fresh read).")
                walked[side] = len(files)
                age_seconds = int(time.time() - saved)
                if on_progress is not None:
                    on_progress(side, f"Cached destination metadata ({age_seconds}s old)", len(files))
                if on_stats is not None:
                    from ofs.walk import WalkStats

                    on_stats(side, f"Using cached destination metadata ({age_seconds}s old)", WalkStats(len(files), 0, len(files)))
                if on_entry is not None:
                    for rel, stat in files.items():
                        on_entry(side, rel, stat)
                return files
        try:
            files = walk(
                root,
                lambda rel: skip_dir(rel, destination),
                skip_file=skip_file,
                control=control,
                on_progress=progress(side),
                on_stats=stats(side),
                on_entry=None if on_entry is None else lambda rel, stat: on_entry(side, rel, stat),
                symlinks=profile.symlinks.value,
            )
            files = drop_session(files)
        except WalkSkipped as exc:
            # Keep the known entries for exclusions, but never treat them as complete.
            partial_sides.append(side)
            files = _drop_global(drop_session(exc.files), masks, mode)
            walked[side] = exc.discovered
            if on_stats is not None:
                from ofs.walk import WalkStats

                on_stats(side, ".", WalkStats(exc.discovered, exc.filtered, len(files)))
            if control is not None:
                control.clear_skip()
            return files
        if destination and profile.cache_destination and not partial_sides and not session_filters_used:
            try:
                save_destination(profile, side, masks, mode, files)
            except OSError as exc:
                cache_notes.append(f"Could not save the destination cache: {exc}")
        return files

    left = _drop_global(read_side("Left", left_root, False), masks, mode)
    right = _drop_global(read_side("Right", right_root, True), masks, mode)
    bytes_read = 0
    if profile.binary_compare:
        bytes_read = _equalize_identical(profile, left_root, right_root, left, right, control=control)
    with rules_lock:
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
    if partial_sides:
        sides = " and ".join(partial_sides)
        note = f"Partial scan: skipped {sides} discovery. Review only; copying and deleting are disabled. Add exclusions, then rescan. No new cache manifest or file history was saved."
    if profile.track and not partial_sides:
        history, history_note = load_history(profile)
        if history is None and not history_note:
            if session_filters_used:
                note = "Session exclusions are active; no partial file-history baseline was saved."
            else:
                save_history(profile, left, right)
                note = "Recorded the current files. The next run can detect moves and deletions."
        elif history_note:
            note = history_note
        elif history is not None:
            with rules_lock:
                changes = adjust_plan(
                    profile, changes, left, right, history, global_masks=masks,
                    session_skips=session_skips, session_masks=session_masks,
                )
    return ScanResult(
        changes,
        len(left),
        len(right),
        bytes_read,
        discovered=sum(walked.values()),
        note=" ".join([part for part in (note, *cache_notes) if part]),
        cached=bool(cached_sides),
        partial_sides=tuple(partial_sides),
        listing=build_listing(left, right, changes, profile),
    )


def remember(profile: Profile) -> None:
    """Record history and invalidate cached destinations after any OFS changes."""

    masks = global_exclusions()
    mode = global_exclusion_mode()
    left_root = ensure_dir(profile.left)
    right_root = ensure_dir(profile.right)
    if not profile.track:
        return
    left = _drop_global(
        walk(
            left_root,
            lambda rel: _skip_dir(profile, rel, destination=False, global_masks=masks, global_mode=mode),
            skip_file=lambda rel: _skip_file(profile, rel, global_masks=masks, global_mode=mode),
            symlinks=profile.symlinks.value,
        ),
        masks,
        mode,
    )
    right = _drop_global(
        walk(
            right_root,
            lambda rel: _skip_dir(profile, rel, destination=True, global_masks=masks, global_mode=mode),
            skip_file=lambda rel: _skip_file(profile, rel, global_masks=masks, global_mode=mode),
            symlinks=profile.symlinks.value,
        ),
        masks,
        mode,
    )
    save_history(profile, left, right)


def invalidate_cached_destinations(profile: Profile) -> None:
    """Invalidate both possible destinations, regardless of the current direction."""

    invalidate_destination(profile)


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
        if left[rel].kind != "file" or right[rel].kind != "file":
            continue
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
            current = right[rel]
            right[rel] = FileStat(left[rel].size, left[rel].mtime, current.kind, current.target, current.mode, current.uid, current.gid)
    return read
