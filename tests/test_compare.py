from ofs.compare import Action, FileStat, build_plan, classify_time, plan_summary
from ofs.model import Direction, ExclusionMode, Profile, SizeConflict, SyncMode, TimestampRules


def rules(**kwargs) -> TimestampRules:
    return TimestampRules(**kwargs)


def test_ignore_small_hour_seconds_and_timestamp():
    assert classify_time(100, 101.5, 5, 5, rules(ignore_small=True)) == "equal"
    assert classify_time(100, 104, 5, 5, rules(ignore_small=True)) == "right_newer"
    assert classify_time(10_000, 10_000 + 3600, 5, 5, rules(ignore_exact_hour=True)) == "equal"
    assert classify_time(10_000, 10_000 + 3660, 5, 5, rules(ignore_exact_hour=True)) == "right_newer"
    assert classify_time(120.2, 179.8, 5, 5, rules(ignore_seconds=True)) == "equal"
    assert classify_time(10, 99, 8, 8, rules(ignore_timestamp=True)) == "equal"
    assert classify_time(10, 99, 8, 3, rules(ignore_timestamp=True, larger_is_newer=True)) == "left_newer"


def test_same_time_different_size_policies():
    assert classify_time(50, 50, 1, 2, rules()) == "size_conflict"
    profile = Profile(
        name="p",
        left="/l",
        right="/r",
        timestamps=TimestampRules(same_time_different_size=SizeConflict.SKIP),
    )
    plan = build_plan(profile, {"a": FileStat(1, 50)}, {"a": FileStat(2, 50)})
    assert plan == []
    profile.timestamps.same_time_different_size = SizeConflict.ASK
    plan = build_plan(profile, {"a": FileStat(1, 50)}, {"a": FileStat(2, 50)})
    assert plan[0].action == Action.ASK


def test_ignore_totally_does_not_copy_or_delete():
    profile = Profile(
        name="p",
        left="/l",
        right="/r",
        mode=SyncMode.EXACT_MIRROR,
        exclusions=["*.tmp"],
        exclusion_mode=ExclusionMode.IGNORE_TOTALLY,
    )
    left = {"a.tmp": FileStat(1, 10), "b.txt": FileStat(1, 10)}
    right = {"a.tmp": FileStat(1, 1), "c.txt": FileStat(1, 1), "d.tmp": FileStat(1, 1)}
    actions = {(item.rel, item.action) for item in build_plan(profile, left, right)}
    assert ("a.tmp", Action.COPY_LEFT_TO_RIGHT) not in actions
    assert ("a.tmp", Action.DELETE_RIGHT) not in actions
    assert ("d.tmp", Action.DELETE_RIGHT) not in actions
    assert ("c.txt", Action.DELETE_RIGHT) in actions
    assert ("b.txt", Action.COPY_LEFT_TO_RIGHT) in actions


def test_dont_copy_still_deletes_the_other_side_in_mirror():
    profile = Profile(
        name="p",
        left="/l",
        right="/r",
        mode=SyncMode.EXACT_MIRROR,
        exclusions=["*.tmp"],
        exclusion_mode=ExclusionMode.DONT_COPY,
    )
    left = {"a.tmp": FileStat(3, 20), "b.txt": FileStat(1, 10)}
    right = {"a.tmp": FileStat(1, 1), "b.txt": FileStat(1, 10)}
    actions = {(item.rel, item.action) for item in build_plan(profile, left, right)}
    assert ("a.tmp", Action.COPY_LEFT_TO_RIGHT) not in actions
    assert ("a.tmp", Action.DELETE_RIGHT) in actions
    assert ("b.txt", Action.COPY_LEFT_TO_RIGHT) not in actions


def test_exact_mirror_pairs_a_unique_move_without_a_database():
    profile = Profile(
        name="p",
        left="/l",
        right="/r",
        mode=SyncMode.EXACT_MIRROR,
        timestamps=rules(ignore_timestamp=True),
    )
    left = {
        "90s/song.mp3": FileStat(1000, 50),
        "new.mp3": FileStat(20, 1),
    }
    right = {
        "old/song.mp3": FileStat(1000, 10),
        "gone.mp3": FileStat(8, 1),
        "also.mp3": FileStat(8, 2),
    }
    plan = {item.rel: item for item in build_plan(profile, left, right)}
    move = plan["old/song.mp3"]
    assert move.action == Action.MOVE_ON_RIGHT
    assert move.dest_rel == "90s/song.mp3"
    assert move.reason == "moved to 90s/song.mp3"
    assert "90s/song.mp3" not in plan
    assert plan["new.mp3"].action == Action.COPY_LEFT_TO_RIGHT
    assert plan["gone.mp3"].action == Action.DELETE_RIGHT
    assert plan["also.mp3"].action == Action.DELETE_RIGHT


def test_exact_mirror_leaves_same_size_files_unpaired():
    profile = Profile(name="p", left="/l", right="/r", mode=SyncMode.EXACT_MIRROR, timestamps=rules(ignore_timestamp=True))
    plan = {(item.rel, item.action) for item in build_plan(
        profile,
        {"a/new.mp3": FileStat(5, 1)},
        {"b/old.mp3": FileStat(5, 9), "c/other.mp3": FileStat(5, 3)},
    )}
    assert plan == {
        ("a/new.mp3", Action.COPY_LEFT_TO_RIGHT),
        ("b/old.mp3", Action.DELETE_RIGHT),
        ("c/other.mp3", Action.DELETE_RIGHT),
    }


def test_standard_does_not_invent_a_move():
    profile = Profile(name="p", left="/l", right="/r", timestamps=rules(ignore_timestamp=True))
    plan = {(item.rel, item.action) for item in build_plan(
        profile,
        {"90s/song.mp3": FileStat(1000, 1)},
        {"old/song.mp3": FileStat(1000, 1)},
    )}
    assert plan == {("90s/song.mp3", Action.COPY_LEFT_TO_RIGHT)}


def test_standard_leaves_extra_files():
    profile = Profile(name="p", left="/l", right="/r")
    plan = build_plan(profile, {"a": FileStat(1, 2)}, {"a": FileStat(1, 1), "old": FileStat(1, 1)})
    actions = {(item.rel, item.action) for item in plan}
    assert actions == {("a", Action.COPY_LEFT_TO_RIGHT)}


def test_both_directions_copy_the_newer_side():
    profile = Profile(name="p", left="/l", right="/r", direction=Direction.BOTH)
    plan = build_plan(
        profile,
        {"a": FileStat(1, 5), "b": FileStat(1, 1)},
        {"a": FileStat(1, 1), "b": FileStat(1, 9)},
    )
    actions = {item.rel: item.action for item in plan}
    assert actions["a"] == Action.COPY_LEFT_TO_RIGHT
    assert actions["b"] == Action.COPY_RIGHT_TO_LEFT


def test_session_skip_and_session_mask_drop_the_row():
    profile = Profile(name="p", left="/l", right="/r")
    left = {"a.txt": FileStat(1, 2), "b.log": FileStat(1, 2)}
    right = {}
    plan = build_plan(profile, left, right, session_skips={"a.txt"}, session_masks=["*.log"])
    assert plan == []


def test_summary_adds_copy_and_delete_bytes():
    profile = Profile(name="p", left="/l", right="/r", mode=SyncMode.EXACT_MIRROR)
    plan = build_plan(
        profile,
        {"a.bin": FileStat(1500, 5), "b.bin": FileStat(2500, 5)},
        {"a.bin": FileStat(100, 1), "old.bin": FileStat(4096, 1)},
    )
    by_rel = {item.rel: item for item in plan}
    assert by_rel["a.bin"].size == 1500
    assert by_rel["b.bin"].size == 2500
    assert by_rel["old.bin"].size == 4096
    text = plan_summary(plan)
    assert "2 files, 3.9 KB to copy" in text
    assert "1 file, 4 KB to delete" in text


def test_global_masks_are_ignored_even_by_exact_mirror():
    profile = Profile(name="p", left="/l", right="/r", mode=SyncMode.EXACT_MIRROR)
    plan = build_plan(
        profile,
        {},
        {"notes.tmp": FileStat(4, 1), "keep.txt": FileStat(4, 1)},
        global_masks=["*.tmp"],
    )
    assert [item.rel for item in plan] == ["keep.txt"]


def test_legacy_compound_masks_hide_all_their_rules_from_the_plan():
    profile = Profile(
        name="p",
        left="/l",
        right="/r",
        exclusions=["*/downloads\xa0**/.venv\xa0**/node_modules", "/datasets/raw;node_modules;.venv", "*/package-store/"],
    )
    files = {
        ".venv/bin/python": FileStat(1, 1),
        "project/.venv/bin/python": FileStat(1, 1),
        "project/node_modules/package/index.js": FileStat(1, 1),
        "datasets/raw/model.bin": FileStat(1, 1),
        "cache/package-store/package/index.js": FileStat(1, 1),
        "project/src/app.py": FileStat(1, 1),
    }
    assert [item.rel for item in build_plan(profile, files, {})] == ["project/src/app.py"]


def test_size_and_age_limits_skip_instead_of_copying_or_deleting():
    profile = Profile(name="p", left="/l", right="/r", mode=SyncMode.EXACT_MIRROR, min_size=100, max_age_days=2)
    now = 1_000_000.0
    day = 86400
    plan = build_plan(
        profile,
        {
            "tiny.txt": FileStat(10, now),
            "old.txt": FileStat(200, now - 5 * day),
            "ok.txt": FileStat(200, now - day),
        },
        {"only-right.txt": FileStat(10, now)},
        now=now,
    )
    by_rel = {item.rel: item for item in plan}
    assert by_rel["tiny.txt"].action == Action.SKIPPED
    assert by_rel["old.txt"].action == Action.SKIPPED
    assert by_rel["only-right.txt"].action == Action.SKIPPED
    assert by_rel["ok.txt"].action == Action.COPY_LEFT_TO_RIGHT


def test_older_tree_is_not_a_source_of_deletes():
    profile = Profile(name="p", left="/l", right="/r", mode=SyncMode.EXACT_MIRROR)
    plan = build_plan(profile, {}, {"Older/a.txt": FileStat(1, 1), "gone.txt": FileStat(1, 1)})
    assert [item.rel for item in plan] == ["gone.txt"]
