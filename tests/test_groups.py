import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ofs.bulk import prepare_bulk_edit
from ofs.compare import FileStat
from ofs.history import database_path, save_history
from ofs.model import Profile
from ofs.store import load_profiles, save_profile
from ofs.ui import MainWindow, SfsApplication, _group_parts


def _widgets(widget):
    yield widget
    children = []
    child = widget.get_first_child()
    while child is not None:
        children.append(child)
        yield from _widgets(child)
        child = child.get_next_sibling()
    extra = widget.get_child() if hasattr(widget, "get_child") else None
    if extra is not None and extra not in children:
        yield from _widgets(extra)


def _group_title(expander):
    heading = expander.get_label_widget()
    widget = heading.get_first_child()
    while widget is not None and not isinstance(widget, Gtk.Label):
        widget = widget.get_next_sibling()
    return widget.get_text()


def _named_group(window, name):
    return next(item for item in _widgets(window.list_box) if isinstance(item, Gtk.Expander) and _group_title(item) == name)


def test_imported_name_grouping_remains_default_and_custom_paths_are_preserved():
    legacy = Profile.from_json({"name": "alpha original", "left": "l", "right": "r"})
    assert legacy.group_override == ""
    assert legacy.group == "alpha"
    assert _group_parts(legacy) == ("alpha",)
    custom = Profile(name="manual", left="l", right="r", group_override=" Work / Photos / Archive ")
    assert custom.group == "Work/Photos/Archive"
    assert _group_parts(custom) == ("Work", "Photos", "Archive")
    assert Profile.from_json(custom.to_json()).to_json() == custom.to_json()
    reset = prepare_bulk_edit([custom], "group_override", "")[0]
    assert reset.group == "manual"
    assert reset.left == "l" and reset.right == "r"


def test_multi_edit_can_assign_a_custom_nested_group_without_touching_paths():
    first = Profile(name="one", left="left/one", right="right/one")
    second = Profile(name="two", left="left/two", right="right/two", imported_settings=True)
    changed = prepare_bulk_edit([first, second], "group_override", " Work / Personal ")
    assert [item.group for item in changed] == ["Work/Personal", "Work/Personal"]
    assert changed[0].left == first.left and changed[1].right == second.right
    assert changed[1].imported_settings is True
    assert first.group_override == second.group_override == ""


def test_custom_nested_groups_and_database_preservation(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestCustomGroupsEditor")
    original = Profile(name="custom job", left="l", right="r", imported_settings=True)
    save_profile(original)
    save_history(original, {"a": FileStat(1, 1)}, {})
    old_database = database_path(original)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            editor = window._editor(window.profiles[0])
            group = next(item for item in _widgets(editor) if isinstance(item, Gtk.Entry) and (item.get_placeholder_text() or "").startswith("Automatic from name"))
            group.set_text("Work / Photos / Archive")
            next(item for item in _widgets(editor) if isinstance(item, Gtk.Button) and item.get_label() == "Save").emit("clicked")
            seen["saved"] = load_profiles()[0]
            seen["database"] = old_database.exists()
            self.quit()

    assert App().run(None) == 0
    assert seen["saved"].group_override == "Work / Photos / Archive"
    assert seen["saved"].group == "Work/Photos/Archive"
    assert seen["saved"].imported_settings is True
    assert seen["database"] is True


def test_nested_groups_search_counts_and_parent_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestNestedGroups")
    for profile in (
        Profile(name="alpha first", left="l", right="r"),
        Profile(name="alpha second", left="l", right="r"),
        Profile(name="archive photos", left="l", right="r", group_override="Work / Photos"),
        Profile(name="archive years", left="l", right="r", group_override="Work / Photos / Years"),
        Profile(name="archive private", left="l", right="r", group_override="Work / Photos / Private", disabled=True),
        Profile(name="single job", left="l", right="r"),
    ):
        save_profile(profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["initial"] = [(item, _named_group(window, item).get_expanded()) for item in ("alpha", "Work")]
            seen["ungrouped"] = not any(isinstance(item, Gtk.Expander) and _group_title(item) == "single" for item in _widgets(window.list_box))
            window.search.set_text("Years")
            seen["search_open"] = [_named_group(window, item).get_expanded() for item in ("Work", "Photos", "Years")]
            window.search.set_text("")
            window._set_groups(True)
            seen["all_open"] = [_named_group(window, item).get_expanded() for item in ("Work", "Photos", "Years")]
            parent = _named_group(window, "Work")
            parent.get_label_widget().get_first_child().set_active(True)
            seen["selected_descendants"] = set(window.checked)
            rows = [item for item in _widgets(window.list_box) if isinstance(item, Gtk.CheckButton) and item.get_label() == "archive photos"]
            assert rows and rows[0].get_active()
            rows[0].set_active(False)
            seen["parent_partial"] = _named_group(window, "Work").get_label_widget().get_first_child().get_inconsistent()
            self.quit()

    assert App().run(None) == 0
    assert seen["initial"] == [("alpha", False), ("Work", False)]
    assert seen["ungrouped"] is True
    assert seen["search_open"] == [True, True, True]
    assert seen["all_open"] == [True, True, True]
    assert seen["selected_descendants"] == {"archive photos", "archive years"}
    assert seen["parent_partial"] is True


def test_profile_rows_have_theme_aware_separators_and_alternating_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestProfileStripes")
    for index, name in enumerate(("alpha one", "beta two", "gamma three")):
        save_profile(Profile(name=name, left="l", right="r", disabled=index == 2))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            rows = list(_widgets(window.list_box))
            del rows[0]
            seen["classes"] = ["profile-row-alt" in row.get_css_classes() for row in rows if "profile-row" in row.get_css_classes()]
            self.quit()

    assert App().run(None) == 0
    assert seen["classes"] == [True, False, True]


def test_group_bulk_and_json_transfer_validation(tmp_path, monkeypatch):
    from ofs.transfer import export_profiles, read_profile_bundle

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    profile = Profile(name="portfolio", left="l", right="r", group_override="Work / Photos")
    bundle = tmp_path / "profiles.json"
    export_profiles(bundle, [profile])
    assert read_profile_bundle(bundle)[0].group_override == profile.group_override
    path = tmp_path / "invalid.json"
    path.write_text('{"format":"ofs-profiles","version":1,"profiles":[{"name":"bad","group_override":2}]}')
    with pytest.raises(ValueError, match="group_override"):
        read_profile_bundle(path)
