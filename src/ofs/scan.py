"""Scan two folders and build a plan. Bytes are read only when binary compare is on."""

from __future__ import annotations

import os
import stat
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from ofs.apply import files_identical
from ofs.cache import destination_sides, invalidate_destination, load_destination, save_destination
from ofs.compare import Action, Change, FileStat, build_plan, classify_time
from ofs.history import adjust_plan, load_history, save_history
from ofs.masks import any_match
from ofs.model import ExclusionMode, Profile
from ofs.review import Listed, build_listing
from ofs.settings import global_exclusion_mode, global_exclusions, scan_stall_minutes
from ofs.volume import ensure_volume
from ofs.walk import (
    DestinationMissing,
    MissingFolder,
    WalkSkipped,
    WalkStalled,
    WalkStopped,
    ensure_dir,
    entry_stat,
    walk,
)


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
    set_first: str = ""
    set_last: str = ""
    set_next: str = ""
    set_total: int = 0
    unreadable: tuple[tuple[str, str, str], ...] = ()


def missing_destination(profile: Profile) -> tuple[str, str] | None:
    """(side, path) when a one-way profile's destination root is absent.

    Two-way profiles are excluded: there is no single destination to create.
    """

    sides = destination_sides(profile)
    if len(sides) != 1:
        return None
    side = sides[0]
    path = profile.right if side == "Right" else profile.left
    if not path or os.path.lexists(path):
        return None
    return side, path


def destination_can_be_created(profile: Profile) -> bool:
    """True when only the last folder is missing, so one mkdir is enough."""

    missing = missing_destination(profile)
    return bool(missing) and Path(missing[1]).parent.is_dir()


def create_destination_root(profile: Profile) -> str:
    """Create a confirmed, missing destination root and drop its stale snapshot.

    Never builds intermediate folders: if the parent is gone, the volume is
    probably unmounted, and creating a chain would write onto the wrong disk.
    """

    missing = missing_destination(profile)
    if missing is None:
        raise ValueError("the destination root already exists or the direction is not one-way")
    side, path = missing
    if not Path(path).parent.is_dir():
        raise DestinationMissing(path, side, False)
    # A snapshot taken before the creation would hide that the destination is
    # empty, so remove it before a scan can reuse it.
    invalidate_destination(profile)
    try:
        Path(path).mkdir()
    except OSError as exc:
        raise OSError(f"could not create {path}: {exc}") from exc
    return path


def _scan_roots(profile: Profile, create_destination: bool) -> tuple[Path, Path, str]:
    """Resolve both roots; a one-way destination may be created after confirmation."""

    sides = destination_sides(profile)
    destination = sides[0] if len(sides) == 1 else None
    created = ""
    roots: dict[str, Path] = {}
    for name, path in (("Left", profile.left), ("Right", profile.right)):
        try:
            roots[name] = ensure_dir(path)
        except MissingFolder:
            if name != destination:
                raise
            if not create_destination:
                raise DestinationMissing(path, name, destination_can_be_created(profile)) from None
            created = create_destination_root(profile)
            roots[name] = Path(created)
    return roots["Left"], roots["Right"], created


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


def root_names(left: Path, right: Path) -> list[str]:
    """Sorted names directly under both roots, so sets never overlap or skip one."""

    names: set[str] = set()
    for root in (left, right):
        try:
            with os.scandir(root) as entries:
                names.update(entry.name for entry in entries)
        except OSError:
            continue
    return sorted(names)


def _inside_folder(rel: str, folder: str) -> bool:
    if folder in {"", "."}:
        # An unreadable profile root makes every entry unknown.
        return True
    return rel == folder or rel.startswith(folder + "/")


def block_unknown_subtrees(changes: list[Change], unreadable) -> list[Change]:
    """Leave entries inside unreadable folders as ASK: their contents are unknown, not absent."""

    if not unreadable:
        return changes
    result = []
    for change in changes:
        hit = None
        for side, folder, message in unreadable:
            if _inside_folder(change.rel, folder) or (change.dest_rel and _inside_folder(change.dest_rel, folder)):
                hit = (side, folder, message)
                break
        if change.action == Action.ASK or hit is None:
            result.append(change)
            continue
        side, folder, _message = hit
        result.append(
            replace(
                change,
                action=Action.ASK,
                reason=f"contents unknown: {folder} could not be read on the {side.lower()}, so this is left as it is",
            )
        )
    return result


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
    create_destination: bool = False,
    set_limit: int = 0,
    after_name: str = "",
) -> ScanResult:
    masks = global_exclusions()
    mode = global_exclusion_mode()
    ensure_volume(profile.left, profile.left_volume)
    ensure_volume(profile.right, profile.right_volume)
    left_root, right_root, created_destination = _scan_roots(profile, create_destination)
    walked = {"Left": 0, "Right": 0}
    rules_lock = getattr(control, "rules_lock", None) or nullcontext()
    session_filters_used = False
    stall_seconds = scan_stall_minutes() * 60

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
    unreadable: list[tuple[str, str, str]] = []
    stalled_sides: list[tuple[str, str, float]] = []

    def read_names(side: str, root: Path, destination: bool, names: list[str]) -> dict[str, FileStat]:
        """Read only the named root children. A set scan never uses or saves the cache."""

        files: dict[str, FileStat] = {}
        for name in names:
            path = root / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                if profile.symlinks.value == "skip":
                    continue
                if profile.symlinks.value == "preserve" or not path.is_dir():
                    if skip_file(name):
                        continue
                    found = entry_stat(path, symlinks=profile.symlinks.value)
                    if found is not None:
                        files[name] = found
                    continue
            elif not stat.S_ISDIR(info.st_mode):
                if skip_file(name):
                    continue
                found = entry_stat(path, symlinks=profile.symlinks.value)
                # `walk` records regular files, folders, and links; it ignores sockets, pipes, and
                # devices. A set must build the same plan, so skip the same special files.
                if found is None or (found.kind == "unsupported" and not stat.S_ISLNK(info.st_mode)):
                    continue
                files[name] = found
                continue
            if skip_dir(name, destination):
                continue
            base = walked[side]

            def scoped_progress(rel: str, count: int, name: str = name) -> None:
                if on_progress is not None:
                    on_progress(side, f"{name}/{rel}" if rel else name, len(files) + count)

            def scoped_stats(rel: str, values, name: str = name, base: int = base) -> None:
                from ofs.walk import WalkStats

                walked[side] = base + values.discovered
                if on_stats is not None:
                    on_stats(
                        side,
                        f"{name}/{rel}" if rel else name,
                        WalkStats(base + values.discovered, values.filtered, base + values.added),
                    )

            def scoped_entry(rel: str, item, name: str = name) -> None:
                if on_entry is not None:
                    on_entry(side, f"{name}/{rel}", item)

            def scoped_unreadable(rel: str, message: str, name: str = name) -> None:
                unreadable.append((side, name if rel == "." else f"{name}/{rel}", message))

            try:
                subtree = walk(
                    path,
                    lambda rel, name=name: skip_dir(f"{name}/{rel}", destination),
                    skip_file=lambda rel, name=name: skip_file(f"{name}/{rel}"),
                    control=control,
                    on_progress=scoped_progress,
                    on_stats=scoped_stats,
                    on_entry=None if on_entry is None else scoped_entry,
                    symlinks=profile.symlinks.value,
                    on_unreadable=scoped_unreadable,
                    stall_seconds=stall_seconds,
                )
            except WalkStalled as exc:
                partial_sides.append(side)
                stalled_sides.append((side, exc.rel, exc.seconds))
                return files
            except WalkSkipped as exc:
                partial_sides.append(side)
                walked[side] = exc.discovered
                files.update({f"{name}/{rel}": item for rel, item in exc.files.items()})
                if on_stats is not None:
                    from ofs.walk import WalkStats

                    on_stats(side, name, WalkStats(exc.discovered, exc.filtered, len(files)))
                if control is not None:
                    control.clear_skip()
                return files
            files.update({f"{name}/{rel}": item for rel, item in subtree.items()})
        return drop_session(files)

    def read_side(side: str, root: Path, destination: bool, names: list[str] | None = None) -> dict[str, FileStat]:
        if names is not None:
            return read_names(side, root, destination, names)
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
                    for rel, info in files.items():
                        on_entry(side, rel, info)
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
                on_unreadable=lambda rel, message: unreadable.append((side, rel, message)),
                stall_seconds=stall_seconds,
            )
            files = drop_session(files)
        except WalkStalled as exc:
            partial_sides.append(side)
            stalled_sides.append((side, exc.rel, exc.seconds))
            return {}
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
        if destination and profile.cache_destination and unreadable and not partial_sides:
            # A manifest that is missing an unreadable folder would claim that folder is empty later.
            cache_notes.append(
                "The destination cache was not saved because a folder could not be read; a later scan must read the destination fresh."
            )
        elif destination and profile.cache_destination and not partial_sides and not session_filters_used:
            try:
                save_destination(profile, side, masks, mode, files)
            except OSError as exc:
                cache_notes.append(f"Could not save the destination cache: {exc}")
        return files

    names: list[str] | None = None
    set_first = set_last = set_next = ""
    set_total = 0
    if set_limit > 0:
        available = root_names(left_root, right_root)
        set_total = len(available)
        pending = [name for name in available if name > after_name] if after_name else list(available)
        names = []
        left: dict[str, FileStat] = {}
        right: dict[str, FileStat] = {}
        for name in pending:
            names.append(name)
            left.update(_drop_global(read_side("Left", left_root, False, [name]), masks, mode))
            right.update(_drop_global(read_side("Right", right_root, True, [name]), masks, mode))
            if partial_sides or len(left) + len(right) >= set_limit:
                break
        if names:
            set_first, set_last = names[0], names[-1]
            if set_last != available[-1]:
                set_next = set_last
        cache_notes.append(
            "Set scan: the destination cache and the file-history baseline are not used or saved."
        )
    else:
        left = _drop_global(read_side("Left", left_root, False), masks, mode)
        right = _drop_global(read_side("Right", right_root, True), masks, mode)
    bytes_read = 0
    binary_diff: set[str] = set()
    if profile.binary_compare:
        bytes_read, binary_diff = _equalize_identical(profile, left_root, right_root, left, right, control=control)
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
    changes = block_unknown_subtrees(changes, unreadable)
    note = ""
    if stalled_sides:
        side, rel, seconds = stalled_sides[0]
        minutes = max(1, round(seconds / 60))
        note = (
            f"Scan stopped: the {side.lower()} side made no progress for {minutes} minute(s) at {rel}. "
            "That path may be unavailable rather than slow, so the listing is incomplete and review only; "
            "copying and deleting are disabled until a rescan completes."
        )
    if partial_sides and not stalled_sides:
        sides = " and ".join(partial_sides)
        note = f"Partial scan: skipped {sides} discovery. Review only; copying and deleting are disabled. Add exclusions, then rescan. No new cache manifest or file history was saved."
    if profile.track and not partial_sides and names is None:
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
                changes = block_unknown_subtrees(
                    adjust_plan(
                        profile, changes, left, right, history, global_masks=masks,
                        session_skips=session_skips, session_masks=session_masks,
                    ),
                    unreadable,
                )
    unreadable_note = ""
    if unreadable:
        side, folder, message = unreadable[0]
        more = "" if len(unreadable) == 1 else f" (and {len(unreadable) - 1} more)"
        count = "1 path" if len(unreadable) == 1 else f"{len(unreadable)} paths"
        unreadable_note = (
            f"{count} could not be read, so the contents are unknown: "
            f"{folder} on the {side.lower()} ({message}){more}. Entries inside are left as they are: "
            "no copy, move, or delete touches an unreadable path. Repair or remount that folder, or exclude it, then rescan."
        )
    return ScanResult(
        changes,
        len(left),
        len(right),
        bytes_read,
        discovered=sum(walked.values()),
        note=" ".join(
            part
            for part in (
                note,
                unreadable_note,
                *cache_notes,
                f"Created the missing destination folder {created_destination}; this first copy is a full copy of the source."
                if created_destination
                else "",
            )
            if part
        ),
        cached=bool(cached_sides),
        partial_sides=tuple(partial_sides),
        listing=build_listing(left, right, changes, profile, binary_diff=binary_diff),
        set_first=set_first,
        set_last=set_last,
        set_next=set_next,
        set_total=set_total,
        unreadable=tuple(unreadable),
    )


def remember(profile: Profile) -> None:
    """Record the file-history snapshot for a profile that tracks one.

    Destination-cache invalidation is not done here; the copy paths invalidate
    before they write, so history can still be recorded after a run.
    """

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
) -> tuple[int, set[str]]:
    """If both sides have the same bytes, pretend the metadata matches too.

    Returns the bytes read and the relative paths whose bytes differ.
    """

    read = 0
    differing: set[str] = set()
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
        else:
            differing.add(rel)
    return read, differing
