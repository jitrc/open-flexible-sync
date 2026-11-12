"""Preview filters, deletion safety text, and the run log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ofs.compare import Action, Change, FileStat, format_bytes
from ofs.model import Profile
from ofs.store import config_dir

DELETE = {Action.DELETE_LEFT, Action.DELETE_RIGHT}
MOVE = {Action.MOVE_ON_LEFT, Action.MOVE_ON_RIGHT}
COPY_LEFT = {Action.COPY_RIGHT_TO_LEFT}
COPY_RIGHT = {Action.COPY_LEFT_TO_RIGHT}
LEFT_ACTIONS = {Action.COPY_RIGHT_TO_LEFT, Action.MOVE_ON_LEFT, Action.DELETE_LEFT}
SAFETY_SECONDS = 120


@dataclass
class Listed:
    change: Change
    on_left: bool
    on_right: bool


def build_listing(left: dict[str, FileStat], right: dict[str, FileStat], changes: list[Change]) -> list[Listed]:
    """Every file the scan saw, with unchanged rows included so the filters can hide them."""

    by_rel: dict[str, Change] = {}
    for change in changes:
        by_rel.setdefault(change.rel, change)
    rows: list[Listed] = []
    for rel in sorted(set(left) | set(right) | set(by_rel)):
        change = by_rel.get(rel)
        if change is None:
            stat = left.get(rel) or right[rel]
            change = Change(rel, Action.UNCHANGED, "unchanged", stat.size)
        rows.append(Listed(change, rel in left, rel in right))
    return rows


def matches(
    row: Listed,
    *,
    state: str = "all",
    existence: str = "all",
    moved: str = "show",
    deletes: str = "show",
    adjust: str = "right",
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


def point_change(change: Change, toward: str) -> Change:
    """Aim one planned file at the right folder or the left folder.

    Unchanged and skipped files stay. A copy or an ask becomes a copy.
    A move stays a move. A delete stays a delete, on the chosen side.
    """

    if change.action in (Action.UNCHANGED, Action.SKIPPED):
        return change
    if toward == "right":
        mapped = {
            Action.COPY_LEFT_TO_RIGHT: Action.COPY_LEFT_TO_RIGHT,
            Action.COPY_RIGHT_TO_LEFT: Action.COPY_LEFT_TO_RIGHT,
            Action.ASK: Action.COPY_LEFT_TO_RIGHT,
            Action.MOVE_ON_RIGHT: Action.MOVE_ON_RIGHT,
            Action.MOVE_ON_LEFT: Action.MOVE_ON_RIGHT,
            Action.DELETE_RIGHT: Action.DELETE_RIGHT,
            Action.DELETE_LEFT: Action.DELETE_RIGHT,
        }
    else:
        mapped = {
            Action.COPY_LEFT_TO_RIGHT: Action.COPY_RIGHT_TO_LEFT,
            Action.COPY_RIGHT_TO_LEFT: Action.COPY_RIGHT_TO_LEFT,
            Action.ASK: Action.COPY_RIGHT_TO_LEFT,
            Action.MOVE_ON_RIGHT: Action.MOVE_ON_LEFT,
            Action.MOVE_ON_LEFT: Action.MOVE_ON_LEFT,
            Action.DELETE_RIGHT: Action.DELETE_LEFT,
            Action.DELETE_LEFT: Action.DELETE_LEFT,
        }
    new = mapped.get(change.action, change.action)
    if new == change.action:
        return change
    return Change(change.rel, new, "chosen in preview", change.size, change.dest_rel)


def folder_status(changes: list[Change]) -> tuple[int, str]:
    """Total size under a folder, and the byte size of each arrow group."""

    total = sum(change.size for change in changes)
    groups = _active_groups(changes)
    if not groups:
        noun = "file" if len(changes) == 1 else "files"
        return total, f"{len(changes)} {noun} unchanged"
    parts = [format_bytes(sum(change.size for change in chosen)) for _mark, chosen in groups]
    return total, "; ".join(parts)


def build_tree(
    rows: list[Listed],
    *,
    minimum_size: int = 0,
    size_target: str = "files",
    size_scope: str = "",
    sort_by: str = "name",
) -> list[TreeNode]:
    """Review-only size filters and sibling sorting; the caller's rows stay untouched."""

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

    def emit(node: dict) -> list[TreeNode]:
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
                    children=emit(child),
                )
            )
        files = []
        for row in node["files"]:
            change = row.change
            files.append(
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
        return order(folders) + order(files)

    return emit(root)


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
