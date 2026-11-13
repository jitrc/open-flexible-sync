from ofs.compare import Action, Change, FileStat, format_bytes
from ofs.model import Profile
from ofs.review import (
    Listed,
    build_listing,
    build_tree,
    direction_mark,
    log_body,
    matches,
    point_change,
    results_text,
    safety_text,
    shown_summary,
    status_lines,
    without_deletes,
    write_log,
)


def _row(rel, action, size=1, *, left=True, right=True):
    return Listed(Change(rel, action, "because", size), left, right)


def test_listing_keeps_unchanged_files():
    left = {"a": FileStat(1, 1), "b": FileStat(2, 2)}
    right = {"a": FileStat(1, 1)}
    rows = build_listing(left, right, [Change("b", Action.COPY_LEFT_TO_RIGHT, "only on source", 2)])
    assert [(row.change.rel, row.change.action, row.on_left, row.on_right) for row in rows] == [
        ("a", Action.UNCHANGED, True, True),
        ("b", Action.COPY_LEFT_TO_RIGHT, True, False),
    ]


def test_tree_shows_folder_size_and_status():
    rows = [
        _row("1960s/a.mp3", Action.COPY_LEFT_TO_RIGHT, 1000, left=True, right=False),
        _row("1960s/b.mp3", Action.COPY_LEFT_TO_RIGHT, 500, left=True, right=False),
        _row("root.mp3", Action.DELETE_RIGHT, 20, left=False, right=True),
    ]
    tree = build_tree(rows)
    assert [node.name for node in tree] == ["1960s", "root.mp3"]
    folder = tree[0]
    assert folder.is_file is False
    assert folder.size_text == format_bytes(1500)
    assert folder.direction == "L ──▶ R  2"
    assert folder.status == format_bytes(1500)
    assert [child.name for child in folder.children] == ["a.mp3", "b.mp3"]
    assert folder.children[0].size_text == format_bytes(1000)
    assert folder.children[0].direction == direction_mark(Action.COPY_LEFT_TO_RIGHT)
    assert "because" in folder.children[0].status
    assert tree[1].is_file
    assert tree[1].direction == direction_mark(Action.DELETE_RIGHT)
    assert "copy_l2r" not in tree[1].status


def test_tree_size_filter_sort_and_scope_do_not_change_source_rows():
    rows = [
        _row("small/tiny.txt", Action.COPY_LEFT_TO_RIGHT, 10, right=False),
        _row("small/big.txt", Action.COPY_RIGHT_TO_LEFT, 1500, left=False),
        _row("large/mid.txt", Action.COPY_LEFT_TO_RIGHT, 500, right=False),
        _row("large/big.txt", Action.MOVE_ON_RIGHT, 4000),
        _row("root_small.txt", Action.DELETE_RIGHT, 20, left=False),
        _row("root_big.txt", Action.UNCHANGED, 100),
    ]
    before = [(row.change.rel, row.change.action, row.change.size) for row in rows]

    name_tree = build_tree(rows)
    assert [node.name for node in name_tree[:2]] == ["large", "small"]
    assert name_tree[0].size == 4500

    largest = build_tree(rows, sort_by="size_desc")
    assert [node.name for node in largest[:2]] == ["large", "small"]
    assert largest[0].children[0].name == "big.txt"
    smallest = build_tree(rows, sort_by="size_asc")
    assert [node.name for node in smallest[:2]] == ["small", "large"]
    assert smallest[1].children[0].name == "mid.txt"

    files = build_tree(rows, minimum_size=1000)
    assert [node.name for node in files] == ["large", "small"]
    assert files[0].size == 4000 and [node.name for node in files[0].children] == ["big.txt"]
    assert files[1].size == 1500 and [node.name for node in files[1].children] == ["big.txt"]

    folders = build_tree(rows, minimum_size=2000, size_target="folders")
    assert folders[0].name == "large"
    assert [node.name for node in folders[0].children] == ["big.txt", "mid.txt"]
    assert {node.name for node in folders if node.is_file} == {"root_big.txt", "root_small.txt"}

    every_level = build_tree(rows, minimum_size=2000, size_target="all")
    assert [node.name for node in every_level] == ["large"]
    assert every_level[0].size == 4000
    assert [node.name for node in every_level[0].children] == ["big.txt"]

    scoped = build_tree(rows, minimum_size=1000, size_target="all", size_scope="small")
    remaining = {node.rel for node in scoped for node in node.children}
    assert "small/big.txt" in remaining
    assert "small/tiny.txt" not in remaining
    assert "large/mid.txt" in remaining
    assert {node.name for node in scoped if node.is_file} == {"root_big.txt", "root_small.txt"}
    assert [(row.change.rel, row.change.action, row.change.size) for row in rows] == before


def test_pointing_a_folder_keeps_unchanged_files():
    copied = Change("1960s/a.mp3", Action.COPY_LEFT_TO_RIGHT, "left is newer", 10, "elsewhere")
    asked = Change("1960s/b.mp3", Action.ASK, "both changed", 4)
    deleted = Change("1960s/c.mp3", Action.DELETE_RIGHT, "extra", 2)
    moved = Change("1960s/d.mp3", Action.MOVE_ON_RIGHT, "renamed", 8, "1960s/e.mp3")
    same = Change("1960s/f.mp3", Action.UNCHANGED, "unchanged", 1)
    skipped = Change("1960s/g.mp3", Action.SKIPPED, "too small", 1)
    pointed = [point_change(item, "left") for item in (copied, asked, deleted, moved, same, skipped)]
    assert [item.action for item in pointed] == [
        Action.COPY_RIGHT_TO_LEFT,
        Action.COPY_RIGHT_TO_LEFT,
        Action.DELETE_LEFT,
        Action.MOVE_ON_LEFT,
        Action.UNCHANGED,
        Action.SKIPPED,
    ]
    assert pointed[0].reason == "chosen in preview"
    assert pointed[0].dest_rel == "elsewhere"
    assert pointed[3].dest_rel == "1960s/e.mp3"
    assert point_change(pointed[0], "left") is pointed[0]


def test_filters_combine():
    deleted = _row("gone.txt", Action.DELETE_RIGHT, left=False)
    copied = _row("note.txt", Action.COPY_LEFT_TO_RIGHT, left=True, right=False)
    same = _row("same.txt", Action.UNCHANGED)
    moved = _row("old.txt", Action.MOVE_ON_RIGHT, right=True, left=False)
    assert matches(deleted, state="del_mov")
    assert not matches(copied, state="del_mov")
    assert matches(copied, state="affected")
    assert not matches(same, state="affected")
    assert matches(same, state="unaffected")
    assert matches(deleted, existence="one")
    assert not matches(same, existence="one")
    assert matches(same, existence="both")
    assert matches(deleted, deletes="only")
    assert not matches(copied, deletes="only")
    assert not matches(deleted, deletes="hide")
    assert matches(moved, moved="only", adjust="right")
    assert not matches(moved, adjust="left")
    assert not matches(copied, state="to_left")
    assert matches(_row("back.txt", Action.COPY_RIGHT_TO_LEFT), state="to_left")
    assert "1 file" in shown_summary([copied])


def test_status_lines_are_per_side():
    changes = [
        Change("a", Action.COPY_RIGHT_TO_LEFT, "", 100),
        Change("b", Action.MOVE_ON_LEFT, "", 50, dest_rel="c"),
        Change("d", Action.DELETE_RIGHT, "", 10),
    ]
    left, right = status_lines(changes)
    assert left == "Copy: 1 (100 B)    Move: 1 (50 B)    Delete: 0 (0 B)"
    assert right == "Copy: 0 (0 B)    Move: 0 (0 B)    Delete: 1 (10 B)"


def test_safety_names_the_heavy_folder_and_the_share():
    profile = Profile(name="videos lessons", left="/data/left", right="/data/right")
    rows = [_row(f"keep/{index}.txt", Action.UNCHANGED) for index in range(3)]
    rows.extend(_row(f"drop/{index}.txt", Action.DELETE_RIGHT, left=False) for index in range(2))
    rows.append(_row("drop/stay.txt", Action.UNCHANGED))
    text = safety_text(profile, rows)
    assert text is not None
    assert "Profile: videos lessons" in text
    assert "RIGHT SIDE: /data/right" in text
    assert "2 of 6 (33.33%)" in text
    assert "drop/0.txt" in text
    assert "Folders with a large percentage of deletions: 1" in text
    assert "drop: 2 File(s)" in text
    assert "keep:" not in text
    assert "120 seconds" in text
    assert safety_text(profile, [_row("a", Action.UNCHANGED)]) is None
    kept = without_deletes([row.change for row in rows])
    assert all(change.action != Action.DELETE_RIGHT for change in kept)
    assert len(kept) == 4


def test_results_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    from ofs.apply import ApplyReport, SideStats

    report = ApplyReport(
        copied=1,
        deleted=2,
        right=SideStats(copied=1, copied_bytes=2300, deleted=2, deleted_bytes=50),
        folders_created=1,
        folders_removed=2,
    )
    text = results_text(report)
    assert "Synchronization has completed successfully." in text
    assert "Files Copied To" in text
    assert "2.2 KB" in text
    assert "Folders Removed" in text
    profile = Profile(name="data/win", left="/l", right="/r")
    body = log_body(profile, [Change("a.txt", Action.COPY_LEFT_TO_RIGHT, "newer", 4)], report, choice="confirm deletions")
    path = write_log(profile, body)
    saved = path.read_text(encoding="utf-8")
    assert "Choice: confirm deletions" in saved
    assert "copy_l2r" in saved
    assert path.parent == tmp_path / "home" / "logs"
    assert "data_win" in path.name


def test_unchanged_listing_rows_name_the_comparison_reason():
    from ofs.model import TimestampRules

    ignored = Profile(
        name="timestamps ignored",
        left="/l",
        right="/r",
        timestamps=TimestampRules(ignore_timestamp=True),
    )
    left = {"same.txt": FileStat(120, 1_700_000_000, "file")}
    right = {"same.txt": FileStat(120, 1_500_000_000, "file")}
    rows = build_listing(left, right, [], ignored)
    assert rows[0].change.action == Action.UNCHANGED
    assert rows[0].change.reason == "unchanged: same size, timestamps ignored"

    plain = Profile(name="plain", left="/l", right="/r")
    rows = build_listing({"a": left["same.txt"]}, {"a": left["same.txt"]}, [], plain)
    assert rows[0].change.reason == "unchanged: same size and time"
