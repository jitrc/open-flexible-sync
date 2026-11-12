"""Per-profile SQLite record of path, size, and time.

The first scan only stores the two trees. Later scans use that record to tell a
rename from a new file, and a deletion from a file that was never there.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import quote

from ofs.compare import Action, Change, FileStat, classify_time, outside_filter
from ofs.masks import any_match, included
from ofs.model import Direction, ExclusionMode, Profile
from ofs.store import config_dir


@dataclass
class History:
    left: dict[str, FileStat]
    right: dict[str, FileStat]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Extend earlier OFS databases in place; old records remain regular files."""

    columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    for name, kind, default in (
        ("kind", "TEXT", "'file'"),
        ("target", "TEXT", "''"),
        ("mode", "INTEGER", "0"),
        ("uid", "INTEGER", "0"),
        ("gid", "INTEGER", "0"),
    ):
        if columns and name not in columns:
            conn.execute(f"ALTER TABLE files ADD COLUMN {name} {kind} NOT NULL DEFAULT {default}")


def database_path(profile: Profile):
    folder = config_dir() / "database"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{quote(profile.name, safe='')}.sqlite"


def load_history(profile: Profile) -> tuple[History | None, str]:
    """Return the saved trees, or a note when the file cannot be used."""

    path = database_path(profile)
    if not path.exists():
        return None, ""
    try:
        with sqlite3.connect(path) as conn:
            _ensure_columns(conn)
            rows = dict(conn.execute("SELECT key, value FROM meta"))
            if rows.get("left") != profile.left or rows.get("right") != profile.right:
                return None, "File database belongs to different folders and was ignored."
            left: dict[str, FileStat] = {}
            right: dict[str, FileStat] = {}
            for row in conn.execute("SELECT side, rel, size, mtime, kind, target, mode, uid, gid FROM files"):
                side, rel, size, mtime, kind, target_name, file_mode, uid, gid = row
                target = left if side == "left" else right
                target[rel] = FileStat(int(size), float(mtime), kind, target_name, int(file_mode), int(uid), int(gid))
    except sqlite3.Error:
        return None, "File database could not be read and was ignored."
    return History(left, right), ""


def save_history(profile: Profile, left: dict[str, FileStat], right: dict[str, FileStat]) -> None:
    path = database_path(profile)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS files (side TEXT, rel TEXT, size INTEGER, mtime REAL, kind TEXT NOT NULL DEFAULT 'file', target TEXT NOT NULL DEFAULT '', mode INTEGER NOT NULL DEFAULT 0, uid INTEGER NOT NULL DEFAULT 0, gid INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (side, rel))")
        _ensure_columns(conn)
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM files")
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [("left", profile.left), ("right", profile.right)])
        conn.executemany(
            "INSERT INTO files (side, rel, size, mtime, kind, target, mode, uid, gid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [("left", rel, stat.size, stat.mtime, stat.kind, stat.target, stat.mode, stat.uid, stat.gid) for rel, stat in left.items()]
            + [("right", rel, stat.size, stat.mtime, stat.kind, stat.target, stat.mode, stat.uid, stat.gid) for rel, stat in right.items()],
        )


def delete_history(name: str) -> None:
    folder = config_dir() / "database"
    path = folder / f"{quote(name, safe='')}.sqlite"
    if path.exists():
        path.unlink()


def adjust_plan(
    profile: Profile,
    changes: list[Change],
    left: dict[str, FileStat],
    right: dict[str, FileStat],
    history: History,
    *,
    global_masks: list[str] | None = None,
    now: float | None = None,
    session_skips: set[str] | None = None,
    session_masks: list[str] | None = None,
) -> list[Change]:
    rules = profile.timestamps
    masks = list(global_masks or []) + list(session_masks or [])
    session_skips = set(session_skips or set())
    if session_skips:
        history = History(
            {rel: stat for rel, stat in history.left.items() if rel not in session_skips},
            {rel: stat for rel, stat in history.right.items() if rel not in session_skips},
        )
    clock = time.time() if now is None else now
    left_moves = _moves(history.left, left, rules)
    right_moves = _moves(history.right, right, rules)
    conflict_rels = _conflict_rels(profile, left, right, history, masks)
    kept = [
        change
        for change in changes
        if change.rel not in conflict_rels
        and not _replaced_by_move(change, left_moves, right_moves, right, left, profile.direction)
    ]
    for old, new in left_moves.items():
        if profile.direction == Direction.RIGHT_TO_LEFT or _filtered(profile, left.get(new), clock):
            continue
        kept.extend(_move_change(Action.MOVE_ON_RIGHT, old, new, right, left.get(new)))
    for old, new in right_moves.items():
        if profile.direction == Direction.LEFT_TO_RIGHT or _filtered(profile, right.get(new), clock):
            continue
        kept.extend(_move_change(Action.MOVE_ON_LEFT, old, new, left, right.get(new)))
    kept.extend(_deletions(profile, left, right, history, left_moves, right_moves, kept, masks, clock))
    for rel in sorted(conflict_rels):
        kept.append(Change(rel, Action.ASK, "changed on both sides since last run", left[rel].size))
    return [
        change for change in kept
        if change.rel not in session_skips
        and not any_match(change.rel, session_masks or [])
        and (not change.dest_rel or not any_match(change.dest_rel, session_masks or []))
    ]


def _moves(previous: dict[str, FileStat], current: dict[str, FileStat], rules) -> dict[str, str]:
    missing = [rel for rel in previous if rel not in current]
    added = [rel for rel in current if rel not in previous]
    pairs: list[tuple[str, str]] = []
    for old in missing:
        hits = [
            new
            for new in added
            if previous[old].kind == current[new].kind
            and previous[old].target == current[new].target
            and classify_time(previous[old].mtime, current[new].mtime, previous[old].size, current[new].size, rules) == "equal"
        ]
        if len(hits) == 1:
            pairs.append((old, hits[0]))
    chosen = [new for _, new in pairs]
    return {old: new for old, new in pairs if chosen.count(new) == 1}


def _move_change(action: Action, old: str, new: str, dest: dict[str, FileStat], source: FileStat | None) -> list[Change]:
    """Rename on the other side when it still has the old path and not the new one."""

    if old not in dest or new in dest or source is None:
        return []
    return [Change(old, action, f"renamed to {new}", source.size, new)]


def _replaced_by_move(change, left_moves, right_moves, right, left, direction: Direction) -> bool:
    """Drop the copy-and-delete pair when the other side can rename the file instead."""

    if direction != Direction.RIGHT_TO_LEFT:
        if change.action == Action.MOVE_ON_RIGHT and change.rel in left_moves:
            return True
        old = next((old for old, new in left_moves.items() if new == change.rel), None)
        if (
            change.action == Action.COPY_LEFT_TO_RIGHT
            and old is not None
            and old in right
            and change.rel not in right
        ):
            return True
        if change.action == Action.DELETE_RIGHT and change.rel in left_moves:
            new = left_moves[change.rel]
            if change.rel in right and new not in right:
                return True
    if direction != Direction.LEFT_TO_RIGHT:
        if change.action == Action.MOVE_ON_LEFT and change.rel in right_moves:
            return True
        old = next((old for old, new in right_moves.items() if new == change.rel), None)
        if (
            change.action == Action.COPY_RIGHT_TO_LEFT
            and old is not None
            and old in left
            and change.rel not in left
        ):
            return True
        if change.action == Action.DELETE_LEFT and change.rel in right_moves:
            new = right_moves[change.rel]
            if change.rel in left and new not in left:
                return True
    return False


def _filtered(profile: Profile, stat: FileStat | None, now: float) -> bool:
    return stat is not None and outside_filter(stat, profile, now) is not None


def _conflict_rels(profile, left, right, history, global_masks: list[str]) -> set[str]:
    found: set[str] = set()
    for rel in set(left) & set(right):
        before_left = history.left.get(rel)
        before_right = history.right.get(rel)
        if before_left is None or before_right is None or not _visible(profile, rel, global_masks):
            continue
        if _same(before_left, left[rel], profile) or _same(before_right, right[rel], profile):
            continue
        if _same(left[rel], right[rel], profile):
            continue
        found.add(rel)
    return found


def _visible(profile: Profile, rel: str, global_masks: list[str]) -> bool:
    if any_match(rel, global_masks):
        return False
    if not included(rel, profile.include):
        return False
    if any_match(rel, profile.exclusions) and profile.exclusion_mode == ExclusionMode.IGNORE_TOTALLY:
        return False
    return True


def _same(before: FileStat, after: FileStat, profile: Profile) -> bool:
    if before.kind != after.kind or before.target != after.target:
        return False
    if profile.compare_permissions and before.mode != after.mode:
        return False
    if profile.compare_ownership and (before.uid, before.gid) != (after.uid, after.gid):
        return False
    return classify_time(before.mtime, after.mtime, before.size, after.size, profile.timestamps) == "equal"


def _deletions(profile, left, right, history, left_moves, right_moves, changes, global_masks: list[str], now: float) -> list[Change]:
    extra: list[Change] = []
    already = {(change.action, change.rel) for change in changes}

    def add(action: Action, rel: str, size: int, reason: str) -> None:
        if (action, rel) in already:
            return
        extra.append(Change(rel, action, reason, size))
        already.add((action, rel))

    if profile.direction != Direction.RIGHT_TO_LEFT:
        for rel, stat in history.left.items():
            if rel in left or rel in left_moves or not _visible(profile, rel, global_masks):
                continue
            current = right.get(rel)
            remembered = history.right.get(rel)
            if current is None or remembered is None or _filtered(profile, current, now):
                continue
            if _same(remembered, current, profile):
                add(Action.DELETE_RIGHT, rel, current.size, "deleted on the left since last run")
            else:
                add(Action.ASK, rel, current.size, "deleted on the left, changed on the right")
    if profile.direction != Direction.LEFT_TO_RIGHT:
        for rel, stat in history.right.items():
            if rel in right or rel in right_moves or not _visible(profile, rel, global_masks):
                continue
            current = left.get(rel)
            remembered = history.left.get(rel)
            if current is None or remembered is None or _filtered(profile, current, now):
                continue
            if _same(remembered, current, profile):
                add(Action.DELETE_LEFT, rel, current.size, "deleted on the right since last run")
            else:
                add(Action.ASK, rel, current.size, "deleted on the right, changed on the left")
    return extra


