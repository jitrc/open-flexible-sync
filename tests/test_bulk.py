import pytest

from ofs.bulk import bulk_preview, prepare_bulk_edit
from ofs.model import Direction, ExclusionMode, Profile, SizeConflict, SyncMode


def test_bulk_mode_changes_only_checked_profiles_and_preserves_other_settings():
    first = Profile(name="alpha one", left="/one left", right="/one right", imported_settings=True, exclusions=["keep"], older_keep=4, last_result="history")
    second = Profile(name="alpha two", left="/two left", right="/two right")
    untouched = Profile(name="beta other", left="/other left", right="/other right")
    changed = prepare_bulk_edit([first, second], "mode", SyncMode.EXACT_MIRROR)
    assert [item.name for item in changed] == ["alpha one", "alpha two"]
    assert changed[0].left == first.left and changed[0].right == first.right
    assert changed[0].exclusions == ["keep"]
    assert changed[0].older_keep == 4 and changed[0].imported_settings is True
    assert changed[0].last_result == "history"
    assert first.mode == second.mode == untouched.mode == SyncMode.STANDARD
    assert first.name in bulk_preview(changed, "mode")
    assert "alpha two" in bulk_preview(changed, "mode")


def test_bulk_exclude_add_preserves_existing_rules_and_replace_does_not_add_duplicates():
    profile = Profile(name="job", left="l", right="r", exclusions=["keep"])
    added = prepare_bulk_edit([profile], "exclusions", "*.tmp; cache ;*.tmp", add_masks=True)[0]
    assert added.exclusions == ["keep", "*.tmp", "cache"]
    assert prepare_bulk_edit([profile], "exclusions", "keep", add_masks=True) == []
    replaced = prepare_bulk_edit([profile], "exclusions", "*.tmp", add_masks=False)[0]
    assert replaced.exclusions == ["*.tmp"]
    assert profile.exclusions == ["keep"]
    profile.include = ["*.jpg"]
    empty_include = prepare_bulk_edit([profile], "include", "")[0]
    assert empty_include.include == ["(all)"]


def test_bulk_nested_timestamp_edit_does_not_reset_other_rules():
    original = Profile(name="job", left="l", right="r")
    original.timestamps.ignore_small = True
    original.timestamps.same_time_different_size = SizeConflict.ASK
    changed = prepare_bulk_edit([original], "timestamps.ignore_seconds", True)[0]
    assert changed.timestamps.ignore_small is True
    assert changed.timestamps.ignore_seconds is True
    assert changed.timestamps.same_time_different_size == SizeConflict.ASK


def test_bulk_validates_counts_and_values():
    profile = Profile(name="job", left="l", right="r")
    assert prepare_bulk_edit([profile], "mode", SyncMode.STANDARD) == []
    assert prepare_bulk_edit([], "mode", SyncMode.EXACT_MIRROR) == []
    with pytest.raises(ValueError):
        prepare_bulk_edit([profile], "older_keep", 0)
    with pytest.raises(ValueError):
        prepare_bulk_edit([profile], "missing", True)
    with pytest.raises(ValueError):
        prepare_bulk_edit([profile], "direction", "left")
