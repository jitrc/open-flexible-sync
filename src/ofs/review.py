"""Preview filters, deletion safety text, and the run log."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ofs.compare import Action, Change, FileStat, equal_reason, format_bytes
from ofs.model import Direction, Profile
from ofs.store import config_dir

DELETE = {Action.DELETE_LEFT, Action.DELETE_RIGHT}
MOVE = {Action.MOVE_ON_LEFT, Action.MOVE_ON_RIGHT}
COPY_LEFT = {Action.COPY_RIGHT_TO_LEFT}
COPY_RIGHT = {Action.COPY_LEFT_TO_RIGHT}
LEFT_ACTIONS = {Action.COPY_RIGHT_TO_LEFT, Action.MOVE_ON_LEFT, Action.DELETE_LEFT}
SAFETY_SECONDS = 120


@dataclass(slots=True)
class Listed:
    change: Change
    on_left: bool
    on_right: bool
    size_relation: str = ""
    binary_diff: bool = False


def _size_relation(profile: Profile, rel: str, left: dict, right: dict, change: Change) -> str:
    """Bigger/smaller from the copy source's point of view, or an empty string."""

    if rel not in left or rel not in right:
        return ""
    if left[rel].size == right[rel].size:
        return ""
    if change.action not in (Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT, Action.ASK):
        return ""
    source_left = profile.direction != Direction.RIGHT_TO_LEFT
    if profile.direction == Direction.BOTH:
        source_left = change.action != Action.COPY_RIGHT_TO_LEFT
    source, other = (left[rel], right[rel]) if source_left else (right[rel], left[rel])
    return "bigger" if source.size > other.size else "smaller"


def build_listing(
    left: dict[str, FileStat],
    right: dict[str, FileStat],
    changes: list[Change],
    profile: Profile | None = None,
    *,
    binary_diff: frozenset[str] | set[str] = frozenset(),
) -> list[Listed]:
    """Every file the scan saw, with unchanged rows included so the filters can hide them."""

    by_rel: dict[str, Change] = {}
    for change in changes:
        by_rel.setdefault(change.rel, change)
    rows: list[Listed] = []
    for rel in sorted(set(left) | set(right) | set(by_rel)):
        change = by_rel.get(rel)
        if change is None:
            stat = left.get(rel) or right[rel]
            reason = "unchanged"
            if profile is not None and rel in left and rel in right:
                reason = equal_reason(profile, left[rel], right[rel])
            change = Change(rel, Action.UNCHANGED, reason, stat.size)
        relation = "" if profile is None else _size_relation(profile, rel, left, right, change)
        rows.append(Listed(change, rel in left, rel in right, relation, rel in binary_diff))
    return rows


def matches(
    row: Listed,
    *,
    state: str = "all",
    existence: str = "all",
    moved: str = "show",
    deletes: str = "show",
    adjust: str = "right",
    reason: str = "all",
) -> bool:
    action = row.change.action
    if state == "affected" and action in (Action.UNCHANGED, Action.SKIPPED):
        return False
    if state == "del_mov" and action not in DELETE | MOVE:
        return False
    if state == "to_left" and action not in LEFT_ACTIONS:
        return False
    if state == "unaffected" and action != Action.UNCHANGED:
        return False
    if state == "skipped" and action != Action.SKIPPED:
        return False
    if existence == "one" and not (row.on_left ^ row.on_right):
        return False
    if existence == "both" and not (row.on_left and row.on_right):
        return False
    is_move = action in MOVE
    if moved == "only" and not is_move:
        return False
    if moved == "hide" and is_move:
        return False
    if is_move and adjust == "left" and action != Action.MOVE_ON_LEFT:
        return False
    if is_move and adjust == "right" and action != Action.MOVE_ON_RIGHT:
        return False
    is_delete = action in DELETE
    if deletes == "only" and not is_delete:
        return False
    if deletes == "hide" and is_delete:
        return False
    if reason == "time" and "newer" not in row.change.reason:
        return False
    if reason == "size" and not row.change.reason.startswith("same time, different size"):
        return False
    if reason == "bigger" and row.size_relation != "bigger":
        return False
    if reason == "smaller" and row.size_relation != "smaller":
        return False
    if reason == "binary" and not row.binary_diff:
        return False
    if reason == "only_source" and row.change.reason not in ("only on source", "only on left"):
        return False
    if reason == "only_destination" and row.change.reason not in ("only on destination", "only on right"):
        return False
    return True


@dataclass
class TreeNode:
    name: str
    rel: str
    is_file: bool
    size_text: str
    size: int
    direction: str
    status: str
    children: list["TreeNode"]
    # A folder keeps its structure but shows its files on demand: `more` is how
    # many files this folder still holds back, and `more_folder` names it.
    more: int = 0
    more_folder: str = ""
    # Counts and sizes per direction group, so a later single-row change can
    # update this folder's totals without rescanning the whole plan.
    groups: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Large plans render the top level only; a folder's children are built when
    # it is expanded, so the window never holds a row per file up front.
    deferred: bool = False


def direction_mark(action: Action) -> str:
    """Left folder, arrow, right folder. Moves share the copy arrow for that side."""

    return {
        Action.COPY_LEFT_TO_RIGHT: "L ──▶ R",
        Action.COPY_RIGHT_TO_LEFT: "L ◀── R",
        Action.MOVE_ON_RIGHT: "L ──▶ R",
        Action.MOVE_ON_LEFT: "L ◀── R",
        Action.DELETE_RIGHT: "L ──✕ R",
        Action.DELETE_LEFT: "L ✕── R",
        Action.ASK: "L ──? R",
        Action.UNCHANGED: "L ─── R",
        Action.SKIPPED: "L ··· R",
    }.get(action, "L ─── R")


def _active_groups(changes: list[Change]) -> list[tuple[str, list[Change]]]:
    buckets: dict[str, list[Change]] = {}
    for change in changes:
        if change.action == Action.UNCHANGED:
            continue
        buckets.setdefault(direction_mark(change.action), []).append(change)
    ordered: list[tuple[str, list[Change]]] = []
    for action in (
        Action.COPY_LEFT_TO_RIGHT,
        Action.COPY_RIGHT_TO_LEFT,
        Action.DELETE_RIGHT,
        Action.DELETE_LEFT,
        Action.ASK,
        Action.SKIPPED,
    ):
        mark = direction_mark(action)
        chosen = buckets.pop(mark, None)
        if chosen:
            ordered.append((mark, chosen))
    ordered.extend(buckets.items())
    return ordered


def direction_summary(changes: list[Change]) -> str:
    groups = _active_groups(changes)
    if not groups:
        return direction_mark(Action.UNCHANGED)
    return "    ".join(f"{mark}  {len(chosen)}" for mark, chosen in groups)


def file_detail(change: Change) -> str:
    parts = [change.dest_rel] if change.dest_rel else []
    if change.reason:
        parts.append(change.reason)
    return "  ".join(parts)


def point_change(
    change: Change,
    toward: str,
    *,
    on_left: bool | None = None,
    on_right: bool | None = None,
) -> Change:
    """Aim one planned file at the right folder or the left folder.

    Unchanged and skipped files stay. A copy or an ask becomes a copy toward
    the chosen folder. A move stays a move. A delete becomes a copy from that
    folder when the file is really there, so reversing the direction brings a
    destination-only file back instead of deleting the other side. When the
    pointed side has no copy, the delete is left untouched.
    """

    if change.action in (Action.UNCHANGED, Action.SKIPPED):
        return change
    to_right = toward != "left"
    if change.action in DELETE:
        # Deletes only become copies when the pointed side really holds the
        # file. Otherwise the scan's deletion stays exactly as it was.
        source_present = on_left if to_right else on_right
        if source_present is False:
            return change
        new = Action.COPY_LEFT_TO_RIGHT if to_right else Action.COPY_RIGHT_TO_LEFT
    elif to_right:
        mapped = {
            Action.COPY_LEFT_TO_RIGHT: Action.COPY_LEFT_TO_RIGHT,
            Action.COPY_RIGHT_TO_LEFT: Action.COPY_LEFT_TO_RIGHT,
            Action.ASK: Action.COPY_LEFT_TO_RIGHT,
            Action.MOVE_ON_RIGHT: Action.MOVE_ON_RIGHT,
            Action.MOVE_ON_LEFT: Action.MOVE_ON_RIGHT,
        }
        new = mapped.get(change.action, change.action)
    else:
        mapped = {
            Action.COPY_LEFT_TO_RIGHT: Action.COPY_RIGHT_TO_LEFT,
            Action.COPY_RIGHT_TO_LEFT: Action.COPY_RIGHT_TO_LEFT,
            Action.ASK: Action.COPY_RIGHT_TO_LEFT,
            Action.MOVE_ON_RIGHT: Action.MOVE_ON_LEFT,
            Action.MOVE_ON_LEFT: Action.MOVE_ON_LEFT,
        }
        new = mapped.get(change.action, change.action)
    if new == change.action:
        return change
    return Change(change.rel, new, "chosen in preview", change.size, change.dest_rel)


_GROUP_VERBS = {
    Action.COPY_LEFT_TO_RIGHT: "to copy to the right",
    Action.COPY_RIGHT_TO_LEFT: "to copy to the left",
    Action.MOVE_ON_RIGHT: "to move on the right",
    Action.MOVE_ON_LEFT: "to move on the left",
    Action.DELETE_RIGHT: "to delete on the right",
    Action.DELETE_LEFT: "to delete on the left",
    Action.ASK: "undecided",
    Action.SKIPPED: "skipped",
}


def group_counts(changes: list[Change]) -> dict[str, tuple[int, int]]:
    """Files and bytes per direction group, the numbers a folder row displays."""

    counts: dict[str, tuple[int, int]] = {}
    for mark, chosen in _active_groups(changes):
        counts[mark] = (len(chosen), sum(change.size for change in chosen))
    return counts


def group_verb(mark: str) -> str:
    """The words for a direction group, keyed by the mark a row displays."""

    return {
        direction_mark(Action.COPY_LEFT_TO_RIGHT): _GROUP_VERBS[Action.COPY_LEFT_TO_RIGHT],
        direction_mark(Action.COPY_RIGHT_TO_LEFT): _GROUP_VERBS[Action.COPY_RIGHT_TO_LEFT],
        direction_mark(Action.DELETE_RIGHT): _GROUP_VERBS[Action.DELETE_RIGHT],
        direction_mark(Action.DELETE_LEFT): _GROUP_VERBS[Action.DELETE_LEFT],
        direction_mark(Action.ASK): _GROUP_VERBS[Action.ASK],
        direction_mark(Action.SKIPPED): _GROUP_VERBS[Action.SKIPPED],
    }.get(mark, "changed")


_GROUP_ORDER = {
    direction_mark(action): index
    for index, action in enumerate(
        (
            Action.COPY_LEFT_TO_RIGHT,
            Action.COPY_RIGHT_TO_LEFT,
            Action.DELETE_RIGHT,
            Action.DELETE_LEFT,
            Action.ASK,
            Action.SKIPPED,
        )
    )
}


def group_rank(mark: str) -> int:
    """Keep folder wording in the same order the direction column uses."""

    return _GROUP_ORDER.get(mark, len(_GROUP_ORDER))


def folder_status(changes: list[Change]) -> tuple[int, str]:
    """Total size under a folder, and what happens there in words.

    The size column already shows the total, so this must not repeat a byte
    count; it names the work instead ("427 undecided", "12 to copy to the right").
    """

    total = sum(change.size for change in changes)
    groups = _active_groups(changes)
    if not groups:
        noun = "file" if len(changes) == 1 else "files"
        return total, f"{len(changes)} {noun} unchanged"
    parts = []
    for _mark, chosen in groups:
        verb = _GROUP_VERBS.get(chosen[0].action, "changed")
        parts.append(f"{len(chosen)} {verb}")
    return total, " · ".join(parts)


def build_tree(
    rows: list[Listed],
    *,
    minimum_size: int = 0,
    size_target: str = "files",
    size_scope: str = "",
    sort_by: str = "name",
    file_limit: int = 0,
    folder_limit: int = 0,
    top_level_only: bool = False,
) -> list[TreeNode]:
    """Review-only size filters and sibling sorting; the caller's rows stay untouched.

    `file_limit` bounds how many file rows are built for the whole tree, and
    `folder_limit` bounds one folder, so a huge plan keeps every folder but
    renders its files on demand. Folder totals always describe the whole plan.
    """

    scope = size_scope.strip().strip("/")
    minimum_size = max(0, minimum_size)

    def in_scope(rel: str) -> bool:
        return not scope or rel == scope or rel.startswith(scope + "/")

    visible = [
        row for row in rows
        if not (
            minimum_size
            and size_target in {"files", "all"}
            and in_scope(row.change.rel)
            and row.change.size < minimum_size
        )
    ]
    root: dict = {"rel": "", "children": {}, "files": []}
    for row in visible:
        parts = row.change.rel.split("/")
        node = root
        acc: list[str] = []
        for part in parts[:-1]:
            acc.append(part)
            node = node["children"].setdefault(part, {"rel": "/".join(acc), "children": {}, "files": []})
        node["files"].append(row)

    def collect(node: dict) -> list[Change]:
        found = [item.change for item in node["files"]]
        for child in node["children"].values():
            found.extend(collect(child))
        return found

    def order(items: list[TreeNode]) -> list[TreeNode]:
        if sort_by in {"size_desc", "size_asc"}:
            reverse = sort_by == "size_desc"
            return sorted(items, key=lambda item: (item.size, item.name.lower()), reverse=reverse)
        return sorted(items, key=lambda item: item.name.lower())

    budget = {"files": file_limit if file_limit > 0 else 0}

    def file_nodes(items: list[Listed]) -> tuple[list[TreeNode], int]:
        """Leaf rows within the remaining budget, plus the count left for later."""

        shown = []
        hidden = 0
        for index, row in enumerate(items):
            if folder_limit and index >= folder_limit:
                hidden = len(items) - index
                break
            if budget["files"] == 0 and (file_limit or folder_limit):
                hidden = len(items) - index
                break
            change = row.change
            shown.append(
                TreeNode(
                    name=change.rel.rsplit("/", 1)[-1],
                    rel=change.rel,
                    is_file=True,
                    size_text=format_bytes(change.size),
                    size=change.size,
                    direction=direction_mark(change.action),
                    status=file_detail(change),
                    children=[],
                )
            )
            if budget["files"]:
                budget["files"] -= 1
        return shown, hidden

    def emit(node: dict, rel: str = "") -> list[TreeNode]:
        folders = []
        for name, child in node["children"].items():
            changes = collect(child)
            total, status = folder_status(changes)
            if minimum_size and size_target in {"folders", "all"} and in_scope(child["rel"]) and total < minimum_size:
                continue
            folders.append(
                TreeNode(
                    name=name,
                    rel=child["rel"],
                    is_file=False,
                    size_text=format_bytes(total),
                    size=total,
                    direction=direction_summary(changes),
                    status=status,
                    children=emit(child, child["rel"]),
                    groups=group_counts(changes),
                )
            )
        files, hidden = file_nodes(node["files"])
        if hidden:
            files.append(
                TreeNode(
                    name=f"Show {hidden:,} more file(s)…",
                    rel="",
                    is_file=False,
                    size_text=format_bytes(sum(item.change.size for item in node["files"][-hidden:])),
                    size=0,
                    direction="",
                    status="Hidden to keep the window light; the plan includes them",
                    children=[],
                    more=hidden,
                    more_folder=rel,
                )
            )
        return order(folders) + order(files)

    if not top_level_only:
        return emit(root)

    folders = []
    for name, child in root["children"].items():
        changes = collect(child)
        total, status = folder_status(changes)
        if minimum_size and size_target in {"folders", "all"} and in_scope(child["rel"]) and total < minimum_size:
            continue
        folders.append(
            TreeNode(
                name=name,
                rel=child["rel"],
                is_file=False,
                size_text=format_bytes(total),
                size=total,
                direction=direction_summary(changes),
                status=status,
                children=[],
                deferred=True,
                groups=group_counts(changes),
            )
        )
    files, hidden = file_nodes(root["files"])
    if hidden:
        files.append(
            TreeNode(
                name=f"Show {hidden:,} more file(s)…",
                rel="",
                is_file=False,
                size_text="",
                size=0,
                direction="",
                status="Hidden to keep the window light; the plan includes them",
                children=[],
                more=hidden,
                more_folder="",
            )
        )
    return order(folders) + order(files)


def folder_children(
    rows: list[Listed],
    folder: str,
    *,
    file_offset: int = 0,
    file_limit: int = 200,
    minimum_size: int = 0,
    size_target: str = "files",
    size_scope: str = "",
    sort_by: str = "name",
) -> tuple[list[TreeNode], list[TreeNode], int]:
    """One folder's immediate children, built on demand.

    Returns the subfolders (still deferred), the next batch of files, and how
    many files this folder still holds back.
    """

    prefix = folder.strip("/")
    scope = size_scope.strip().strip("/")

    def in_scope(rel: str) -> bool:
        return not scope or rel == scope or rel.startswith(scope + "/")

    subtrees: dict[str, list[Change]] = {}
    files: list[Listed] = []
    for row in rows:
        rel = row.change.rel
        if prefix:
            if not rel.startswith(prefix + "/"):
                continue
            rest = rel[len(prefix) + 1:]
        else:
            rest = rel
        if "/" in rest:
            name = rest.split("/", 1)[0]
            subtrees.setdefault(f"{prefix}/{name}" if prefix else name, []).append(row.change)
            continue
        if minimum_size and size_target in {"files", "all"} and in_scope(rel) and row.change.size < minimum_size:
            continue
        files.append(row)

    if sort_by in {"size_desc", "size_asc"}:
        files.sort(key=lambda row: (row.change.size, row.change.rel.lower()), reverse=sort_by == "size_desc")
    else:
        files.sort(key=lambda row: row.change.rel.rsplit("/", 1)[-1].lower())
    batch = files[file_offset:file_offset + max(1, file_limit)] if file_limit else files[file_offset:]
    remaining = max(0, len(files) - file_offset - len(batch))

    folders = []
    for rel, changes in subtrees.items():
        total, status = folder_status(changes)
        if minimum_size and size_target in {"folders", "all"} and in_scope(rel) and total < minimum_size:
            continue
        folders.append(
            TreeNode(
                name=rel.rsplit("/", 1)[-1],
                rel=rel,
                is_file=False,
                size_text=format_bytes(total),
                size=total,
                direction=direction_summary(changes),
                status=status,
                children=[],
                deferred=True,
                groups=group_counts(changes),
            )
        )
    nodes = [
        TreeNode(
            name=row.change.rel.rsplit("/", 1)[-1],
            rel=row.change.rel,
            is_file=True,
            size_text=format_bytes(row.change.size),
            size=row.change.size,
            direction=direction_mark(row.change.action),
            status=file_detail(row.change),
            children=[],
        )
        for row in batch
    ]
    return order_nodes(folders, sort_by), nodes, remaining


def order_nodes(items: list[TreeNode], sort_by: str) -> list[TreeNode]:
    if sort_by in {"size_desc", "size_asc"}:
        return sorted(items, key=lambda item: (item.size, item.name.lower()), reverse=sort_by == "size_desc")
    return sorted(items, key=lambda item: item.name.lower())


def stream_files(
    rows: list[Listed],
    folder: str,
    *,
    offset: int,
    limit: int,
    minimum_size: int = 0,
    size_target: str = "files",
    size_scope: str = "",
    sort_by: str = "name",
) -> tuple[list[TreeNode], int]:
    """The next batch of one folder's files, ordered like `build_tree`.

    Returns the rows and how many files that folder still holds back.
    """

    prefix = folder.strip("/")
    scope = size_scope.strip().strip("/")

    def in_scope(rel: str) -> bool:
        return not scope or rel == scope or rel.startswith(scope + "/")

    items = []
    for row in rows:
        rel = row.change.rel
        if (rel.rsplit("/", 1)[0] if "/" in rel else "") != prefix:
            continue
        if minimum_size and size_target in {"files", "all"} and in_scope(rel) and row.change.size < minimum_size:
            continue
        items.append(row)
    if sort_by in {"size_desc", "size_asc"}:
        items.sort(key=lambda row: (row.change.size, row.change.rel.lower()), reverse=sort_by == "size_desc")
    else:
        items.sort(key=lambda row: row.change.rel.rsplit("/", 1)[-1].lower())
    batch = items[offset:offset + limit]
    nodes = [
        TreeNode(
            name=row.change.rel.rsplit("/", 1)[-1],
            rel=row.change.rel,
            is_file=True,
            size_text=format_bytes(row.change.size),
            size=row.change.size,
            direction=direction_mark(row.change.action),
            status=file_detail(row.change),
            children=[],
        )
        for row in batch
    ]
    return nodes, max(0, len(items) - offset - len(batch))


def shown_summary(rows: list[Listed]) -> str:
    total = sum(row.change.size for row in rows)
    noun = "file" if len(rows) == 1 else "files"
    return f"Files shown: {len(rows)} {noun}, {format_bytes(total)}"


def _pair(changes: list[Change], actions: set[Action]) -> tuple[int, int]:
    chosen = [change for change in changes if change.action in actions]
    return len(chosen), sum(change.size for change in chosen)


def status_lines(changes: list[Change]) -> tuple[str, str]:
    """Copy, move, and delete totals for the left side and the right side."""

    copy_left = _pair(changes, COPY_LEFT)
    move_left = _pair(changes, {Action.MOVE_ON_LEFT})
    delete_left = _pair(changes, {Action.DELETE_LEFT})
    copy_right = _pair(changes, COPY_RIGHT)
    move_right = _pair(changes, {Action.MOVE_ON_RIGHT})
    delete_right = _pair(changes, {Action.DELETE_RIGHT})

    def line(copy: tuple[int, int], move: tuple[int, int], delete: tuple[int, int]) -> str:
        return (
            f"Copy: {copy[0]} ({format_bytes(copy[1])})    "
            f"Move: {move[0]} ({format_bytes(move[1])})    "
            f"Delete: {delete[0]} ({format_bytes(delete[1])})"
        )

    return line(copy_left, move_left, delete_left), line(copy_right, move_right, delete_right)


def without_deletes(changes: list[Change]) -> list[Change]:
    return [change for change in changes if change.action not in DELETE]


def deletion_violations(profile: Profile, rows: list[Listed]) -> list[str]:
    """Limits are checked against the complete remaining listing, never filtered rows."""

    deletes = [row for row in rows if row.change.action in DELETE]
    if not deletes:
        return []
    if profile.never_delete_files:
        return ["File deletion is disabled. Continue without deletions or enable deletion in the profile."]
    violations = []
    total = len(rows)
    for side, action in (("Left", Action.DELETE_LEFT), ("Right", Action.DELETE_RIGHT)):
        count = sum(row.change.action == action for row in deletes)
        percent = 100.0 * count / total if total else 0.0
        if count and profile.max_delete_percent and percent > profile.max_delete_percent:
            violations.append(
                f"{side}: {count} of {total} files ({percent:.2f}%) exceeds the "
                f"{profile.max_delete_percent}% deletion limit."
            )
        if count and profile.block_delete_all:
            totals: dict[str, int] = {}
            removed: dict[str, int] = {}
            for row in rows:
                if action == Action.DELETE_LEFT and not row.on_left:
                    continue
                if action == Action.DELETE_RIGHT and not row.on_right:
                    continue
                folder = row.change.rel.rsplit("/", 1)[0] if "/" in row.change.rel else "."
                totals[folder] = totals.get(folder, 0) + 1
                if row.change.action == action:
                    removed[folder] = removed.get(folder, 0) + 1
            for folder in sorted(removed):
                if removed[folder] == totals[folder]:
                    violations.append(f"{side}: delete-all protection includes every scanned file in {folder}/")
    if violations:
        violations.append("Change the profile safeguards or continue without deletions.")
    return violations


def safety_text(profile: Profile, rows: list[Listed]) -> str | None:
    """Text for the deletion warning. None when this run deletes nothing."""

    deletes = [row for row in rows if row.change.action in DELETE]
    if not deletes:
        return None
    total = len(rows)
    lines = [
        f"Profile: {profile.name}",
        "",
        "This profile cannot continue without confirmation because of the number of files to be deleted.",
        "",
    ]
    for side, action, root in (
        ("LEFT SIDE", Action.DELETE_LEFT, profile.left),
        ("RIGHT SIDE", Action.DELETE_RIGHT, profile.right),
    ):
        side_rows = [row for row in deletes if row.change.action == action]
        if not side_rows:
            continue
        lines.append(f"{side}: {root}")
        side_percent = (100.0 * len(side_rows) / total) if total else 0.0
        lines.append(f"Files to be deleted: {len(side_rows)} of {total} ({side_percent:.2f}%)")
        lines.extend(row.change.rel for row in side_rows[:200])
        if len(side_rows) > 200:
            lines.append(f"… {len(side_rows) - 200} more")
        folders = _large_folders(rows, action)
        if folders:
            lines.append("")
            lines.append(f"Folders with a large percentage of deletions: {len(folders)}")
            lines.extend(f"{folder}: {count} File(s)" for folder, count in folders)
        lines.append("")
    violations = deletion_violations(profile, rows)
    if violations:
        lines.extend(["DELETION POLICY BLOCKS CONFIRM DELETIONS:", *violations, ""])
    lines.append(
        f"NOTE: in {SAFETY_SECONDS} seconds, the profile will continue without deletions."
    )
    return "\n".join(lines)


def _large_folders(rows: list[Listed], action: Action) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    deleted: dict[str, int] = {}
    for row in rows:
        folder = row.change.rel.rsplit("/", 1)[0] if "/" in row.change.rel else "."
        totals[folder] = totals.get(folder, 0) + 1
        if row.change.action == action:
            deleted[folder] = deleted.get(folder, 0) + 1
    large = [
        (folder, count)
        for folder, count in deleted.items()
        if totals[folder] and count / totals[folder] >= 0.5
    ]
    large.sort()
    return large


def write_log(profile: Profile, body: str) -> Path:
    folder = config_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    safe = "".join(ch if ch not in '/\\:' else "_" for ch in profile.name)
    path = folder / f"{safe} - {stamp}.log"
    path.write_text(body, encoding="utf-8")
    return path


def outcome_headline(report) -> str:
    changed = report.copied or report.deleted or report.moved
    if report.cancelled and not changed:
        return "Synchronization was stopped before any files changed."
    if report.cancelled:
        return "Synchronization was stopped."
    if report.errors:
        return "Synchronization finished with errors."
    return "Synchronization has completed successfully."


def result_rows(report) -> list[tuple[str, str, str]]:
    def cell(count: int, size: int) -> str:
        return f"{count} ({format_bytes(size)})"

    rows = [
        ("Files Copied To", cell(report.left.copied, report.left.copied_bytes), cell(report.right.copied, report.right.copied_bytes)),
        ("Files Moved", cell(report.left.moved, report.left.moved_bytes), cell(report.right.moved, report.right.moved_bytes)),
        ("Files Deleted", cell(report.left.deleted, report.left.deleted_bytes), cell(report.right.deleted, report.right.deleted_bytes)),
        ("Folders Created", str(report.folders_created), ""),
        ("Folders Removed", str(report.folders_removed), ""),
    ]
    if getattr(report, "versions_removed", 0):
        rows.append(("Older versions removed", str(report.versions_removed), ""))
    if getattr(report, "deletions_archived", 0):
        rows.append(("Deletions archived", str(report.deletions_archived), ""))
    return rows


def results_text(report) -> str:
    lines = [outcome_headline(report), ""]
    lines.append(f"{'':22}{'Left':28}Right")
    for label, left, right in result_rows(report):
        lines.append(f"{label:22}{left:28}{right}")
    if report.skipped:
        lines.append("")
        lines.append(f"{report.skipped} undecided file(s) were left as they are.")
    if report.errors:
        lines.append("")
        lines.extend(report.errors)
    return "\n".join(lines)


def log_body(profile: Profile, changes: list[Change], report, *, choice: str) -> str:
    lines = [
        profile.name,
        f"Left: {profile.left}",
        f"Right: {profile.right}",
        f"Choice: {choice}",
        "",
    ]
    for change in changes:
        moved = f" -> {change.dest_rel}" if change.dest_rel else ""
        lines.append(f"{change.action.value}\t{change.size}\t{change.rel}{moved}\t{change.reason}")
    lines.append("")
    lines.append(results_text(report))
    return "\n".join(lines)
