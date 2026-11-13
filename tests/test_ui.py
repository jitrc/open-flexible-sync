import os
import time

import pytest

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from ofs.apply import CopyProgress
from ofs.compare import Action, Change, FileStat
from ofs.jobs import JobResult
from ofs.model import ExclusionMode, Profile, SyncMode, TimestampRules
from ofs.review import Listed, build_listing
from ofs.scan import ScanResult, scan_profile
from ofs.store import load_profiles, save_profile
from ofs.ui import ExclusionEditor, MainWindow, PlanRow, Preview, ProgressWindow, RestoreWindow, ResultsWindow, ScanWindow, SfsApplication


def _group_titles(box) -> list[tuple[str, bool]]:
    found = []
    child = box.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Expander):
            title = ""
            heading = child.get_label_widget()
            if heading is not None:
                label = heading.get_first_child()
                while label is not None and not isinstance(label, Gtk.Label):
                    label = label.get_next_sibling()
                if isinstance(label, Gtk.Label):
                    title = label.get_label()
            found.append((title or child.get_label() or "", child.get_expanded()))
        child = child.get_next_sibling()
    return found


def _buttons(widget) -> list[Gtk.Button]:
    found = []
    if isinstance(widget, Gtk.Button):
        found.append(widget)
    visited = []
    child = widget.get_first_child()
    while child is not None:
        visited.append(child)
        found.extend(_buttons(child))
        child = child.get_next_sibling()
    extra = widget.get_child() if hasattr(widget, "get_child") else None
    if extra is not None and extra not in visited:
        found.extend(_buttons(extra))
    return found


def _run_buttons_are_marked(widget) -> bool:
    runs = [button for button in _buttons(widget) if "run" in button.get_css_classes()]
    return bool(runs) and all("run" in button.get_css_classes() for button in runs)


def _button_labels(widget) -> list[str]:
    found = []
    if isinstance(widget, (Gtk.Button, Gtk.MenuButton)):
        if widget.get_label():
            found.append(widget.get_label())
        else:
            found.extend(item.get_label() for item in _widgets(widget) if isinstance(item, Gtk.Label) and item.get_label())
    visited = []
    child = widget.get_first_child()
    while child is not None:
        visited.append(child)
        found.extend(_button_labels(child))
        child = child.get_next_sibling()
    extra = widget.get_child() if hasattr(widget, "get_child") else None
    if extra is not None and extra not in visited:
        found.extend(_button_labels(extra))
    return found


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


def _profile_rows(box) -> list[str]:
    return [item.get_label() for item in _widgets(box) if isinstance(item, Gtk.CheckButton)]


def _find_profile_row(root, name: str):
    for widget in _widgets(root):
        if isinstance(widget, Gtk.CheckButton) and widget.get_label() == name:
            while widget is not None and "profile-row" not in widget.get_css_classes():
                widget = widget.get_parent()
            return widget
    return None


def test_result_return_restores_the_same_profile_and_scroll_position(tmp_path, monkeypatch):
    from ofs.apply import ApplyReport

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestResultProfileReturn")
    for index in range(40):
        save_profile(Profile(name=f"group-{index // 2:02d} profile-{index:02d}", left="/sample/left", right="/sample/right"))
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window._set_groups(True)
            window.present()
            settle()
            adjustment = window.profiles_scroll.get_vadjustment()
            adjustment.set_value((adjustment.get_upper() - adjustment.get_page_size()) * 0.5)
            settle()
            target = None
            for profile in window.profiles:
                row = _find_profile_row(window.list_box, profile.name)
                if row is None or not row.get_mapped():
                    continue
                point = row.translate_coordinates(window.list_box, 0, 0)
                offset = point[1] - adjustment.get_value() if point is not None else -1
                if offset >= 0 and offset + row.get_height() <= adjustment.get_page_size():
                    target = profile.name
                    break
            seen["target"] = target
            selected = {"group-00 profile-00", target}
            window.checked = set(selected)
            window._run_names = lambda *_args, **_kwargs: seen.setdefault("scan_requested", target)
            window._run_one(None, target)
            window.reload()
            settle()
            seen["selected_name"] = getattr(window, "_focused_profile", None)
            seen["selected_row"] = "profile-selected" in _find_profile_row(window.list_box, target).get_css_classes()
            seen["before_scroll"] = adjustment.get_value()
            job = next(item for item in window.profiles if item.name == target)
            preview = window._preview(JobResult(job, ScanResult([]), None))
            progress = ProgressWindow(preview, target)
            preview._finished(progress, ApplyReport(), None)
            result = next(item for item in self.get_windows() if isinstance(item, ResultsWindow))
            result.close()
            settle()
            seen["overview_visible"] = window.get_visible()
            seen["focused_name"] = getattr(window, "_focused_profile", None)
            check = window._profile_check_buttons.get(target)
            seen["widget_focus"] = check is not None and check.has_focus()
            seen["after_scroll"] = adjustment.get_value()
            seen["expanded_preserved"] = window._group_expanded.get("group-00") is True
            seen["checked_preserved"] = window.checked == selected
            self.quit()

    assert App().run(None) == 0
    assert seen["target"] is not None
    assert seen["scan_requested"] == seen["target"]
    assert seen["selected_name"] == seen["target"] and seen["selected_row"] is True
    assert seen["overview_visible"] is True
    assert seen["focused_name"] == seen["target"] and seen["widget_focus"] is True
    assert seen["after_scroll"] == pytest.approx(seen["before_scroll"], abs=1)
    assert seen["expanded_preserved"] is True and seen["checked_preserved"] is True


def test_returning_result_reveals_the_whole_finished_profile_row(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFinishedRowVisible")
    for index in range(40):
        save_profile(Profile(name=f"group-{index // 2:02d} profile-{index:02d}", left="/sample/left", right="/sample/right"))
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window._set_groups(True)
            window.present()
            settle()
            adjustment = window.profiles_scroll.get_vadjustment()
            page = adjustment.get_page_size()
            target = next(
                name for name, row in window._profile_rows.items()
                if row.get_mapped()
                and row.translate_coordinates(window.list_box, 0, 0)[1] > page
            )
            row = window._profile_rows[target]
            # Leave the name line inside the viewport and clip the path line below it.
            top = row.translate_coordinates(window.list_box, 0, 0)[1]
            adjustment.set_value(top - (page - 10))
            settle()

            def viewport_top(widget):
                point = widget.translate_coordinates(window.list_box, 0, 0)
                return None if point is None else point[1] - adjustment.get_value()

            clipped = viewport_top(row)
            seen["clipped_before"] = clipped is not None and clipped + row.get_height() > page
            seen["before_value"] = adjustment.get_value()
            window._focused_profile = target
            window._return_to_profile(target)
            settle()
            rebuilt = window._profile_rows[target]
            visible = viewport_top(rebuilt)
            seen["visible_after"] = (
                visible is not None and visible >= 0
                and visible + rebuilt.get_height() <= adjustment.get_page_size()
            )
            seen["moved"] = abs(adjustment.get_value() - seen["before_value"])
            seen["row_height"] = rebuilt.get_height()
            self.quit()

    assert App().run(None) == 0
    assert seen["clipped_before"] is True
    assert seen["visible_after"] is True
    assert seen["moved"] <= seen["row_height"]


def test_completed_run_reload_preserves_scroll_and_expanded_groups(tmp_path, monkeypatch):

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestProfileScroll")
    for index in range(40):
        save_profile(Profile(name=f"group-{index // 2:02d} profile-{index:02d}", left="/sample/left", right="/sample/right"))
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window._set_groups(True)
            window.present()
            settle()
            adjustment = window.profiles_scroll.get_vadjustment()
            seen["has_scrollbar"] = adjustment.get_upper() > adjustment.get_page_size()
            adjustment.set_value((adjustment.get_upper() - adjustment.get_page_size()) * 0.55)
            settle()
            seen["before"] = adjustment.get_value()
            seen["expanded_before"] = window._group_expanded.get("group-00") is True
            window.reload()
            settle()
            seen["after"] = adjustment.get_value()
            seen["expanded_after"] = window._group_expanded.get("group-00") is True
            adjustment.set_value(0)
            window.reload()
            settle()
            seen["top"] = adjustment.get_value()
            self.quit()

    assert App().run(None) == 0
    assert seen["has_scrollbar"] is True
    assert seen["before"] > 0
    assert seen["after"] == pytest.approx(seen["before"], abs=1)
    assert seen["expanded_before"] is True and seen["expanded_after"] is True
    assert seen["top"] == 0


def test_overview_groups_and_preview_actions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "notes.txt").write_text("new")
    (right / "notes.txt").write_text("old")
    (right / "extra.txt").write_text("extra")
    os.utime(left / "notes.txt", (50, 50))
    os.utime(right / "notes.txt", (10, 10))
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTest")
    save_profile(Profile(name="alpha notes", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR))
    save_profile(Profile(name="alpha more", left=str(left), right=str(right)))
    save_profile(Profile(name="beta other", left=str(left), right=str(right)))
    save_profile(Profile(name="beta more", left=str(left), right=str(right)))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["groups"] = [title for title, _expanded in _group_titles(window.list_box)]
            profile = next(item for item in window.profiles if item.name == "alpha notes")
            result = scan_profile(profile)
            preview = window._preview(JobResult(profile, result, None))
            seen["actions"] = {(item.change.rel, item.change.action) for item in (preview.store.get_item(i) for i in range(preview.store.get_n_items()))}
            row = preview.store.get_item(0)
            preview._ignore(row, extension=False)
            seen["skipped"] = set(window.session_skips["alpha notes"])
            seen["masks_after_ignore"] = next(item.exclusions for item in load_profiles() if item.name == "alpha notes")
            other = preview.store.get_item(0)
            exclusion_dialog = preview._add_exclusion(other)
            seen["default_exclusion"] = exclusion_dialog.exclusion_entry.get_text()
            exclusion_dialog.exclusion_save_button.emit("clicked")
            seen["saved_masks"] = next(item.exclusions for item in load_profiles() if item.name == "alpha notes")
            self.quit()

    assert App().run(None) == 0
    assert "alpha" in seen["groups"]
    assert "beta" in seen["groups"]
    assert ("notes.txt", Action.COPY_LEFT_TO_RIGHT) in seen["actions"]
    assert ("extra.txt", Action.DELETE_RIGHT) in seen["actions"]
    assert seen["masks_after_ignore"] == []
    assert seen["skipped"]
    assert seen["saved_masks"]


def test_groups_start_collapsed_and_toggle_together(tmp_path, monkeypatch):
    home = tmp_path / "home"
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestGroups")
    save_profile(Profile(name="alpha notes", left=str(left), right=str(right)))
    save_profile(Profile(name="alpha more", left=str(left), right=str(right)))
    save_profile(Profile(name="beta other", left=str(left), right=str(right)))
    save_profile(Profile(name="beta more", left=str(left), right=str(right)))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["start"] = _group_titles(window.list_box)
            window._set_groups(True)
            seen["open"] = _group_titles(window.list_box)
            window.search.set_text("zzz")
            seen["search_miss"] = _group_titles(window.list_box)
            window.search.set_text("alpha")
            seen["search"] = _group_titles(window.list_box)
            window._set_groups(False)
            seen["closed"] = _group_titles(window.list_box)
            self.quit()

    assert App().run(None) == 0
    assert seen["start"] == [("alpha", False), ("beta", False)]
    assert seen["open"] == [("alpha", True), ("beta", True)]
    assert seen["search_miss"] == []
    assert seen["search"] == [("alpha", True)]
    assert seen["closed"] == [("alpha", False)]


def test_group_checkbox_selects_visible_enabled_members_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestGroupChecks")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    save_profile(Profile(name="alpha one", left=str(left), right=str(right)))
    save_profile(Profile(name="alpha two", left=str(left), right=str(right)))
    save_profile(Profile(name="alpha offline", left=str(tmp_path / "offline"), right=str(right)))
    save_profile(Profile(name="alpha disabled", left=str(left), right=str(right), disabled=True))
    save_profile(Profile(name="beta other", left=str(left), right=str(right)))
    seen = {}

    class App(SfsApplication):
        def alpha_check(self, window):
            child = window.list_box.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Expander):
                    heading = child.get_label_widget()
                    label = heading.get_first_child()
                    while label is not None and not isinstance(label, Gtk.Label):
                        label = label.get_next_sibling()
                    if isinstance(label, Gtk.Label) and label.get_text() == "alpha":
                        return heading.get_first_child()
                child = child.get_next_sibling()
            raise AssertionError("alpha group not shown")

        def do_activate(self):
            window = MainWindow(self)
            window.available_only.set_active(True)
            window._set_groups(True)
            check = self.alpha_check(window)
            seen["initial"] = check.get_active()
            check.set_active(True)
            seen["selected"] = set(window.checked)
            check = self.alpha_check(window)
            seen["all"] = (check.get_active(), check.get_inconsistent())
            check.set_active(False)
            seen["cleared"] = set(window.checked)
            window.checked.add("alpha one")
            window._rebuild()
            seen["partial"] = self.alpha_check(window).get_inconsistent()
            self.quit()

    assert App().run(None) == 0
    assert seen["initial"] is False
    assert seen["selected"] == {"alpha one", "alpha two"}
    assert seen["all"] == (True, False)
    assert seen["cleared"] == set()
    assert seen["partial"] is True


def test_overview_status_availability_filters_columns_and_ungrouped_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestOverviewFilters")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    missing = tmp_path / "missing"
    save_profile(Profile(name="clean job", left=str(left), right=str(right), last_result="2026-11-11 10:00  2 copied"))
    save_profile(Profile(name="pending job", left=str(left), right=str(right)))
    save_profile(Profile(name="stopped job", left=str(left), right=str(right), last_result="2026-11-11 10:01  stopped, 1 copied"))
    save_profile(Profile(name="broken job", left=str(missing), right=str(right), last_result="2026-11-11 10:02  1 copied, 2 errors"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["ungrouped"] = _group_titles(window.list_box)
            seen["all"] = _profile_rows(window.list_box)
            seen["headers"] = [item.get_text() for item in _widgets(window.column_headers) if isinstance(item, Gtk.Label)]
            window.status_filter.set_selected(1)
            seen["not_run"] = _profile_rows(window.list_box)
            window.status_filter.set_selected(2)
            seen["clean"] = _profile_rows(window.list_box)
            window.status_filter.set_selected(3)
            seen["stopped"] = _profile_rows(window.list_box)
            window.status_filter.set_selected(4)
            seen["errors"] = _profile_rows(window.list_box)
            window.status_filter.set_selected(0)
            window.available_only.set_active(True)
            seen["available"] = _profile_rows(window.list_box)
            seen["count"] = window.filter_count.get_text()
            window.available_only.set_active(False)
            window.checked.add("pending job")
            window.status_filter.set_selected(5)
            seen["selected"] = _profile_rows(window.list_box)
            self.quit()

    assert App().run(None) == 0
    assert seen["ungrouped"] == []
    assert set(seen["all"]) == {"clean job", "pending job", "stopped job", "broken job"}
    assert seen["headers"] == ["Profile", "Last run", "Result", "Actions"]
    assert seen["not_run"] == ["pending job"]
    assert seen["clean"] == ["clean job"]
    assert seen["stopped"] == ["stopped job"]
    assert seen["errors"] == ["broken job"]
    assert set(seen["available"]) == {"clean job", "pending job", "stopped job"}
    assert seen["count"] == "3 of 4 shown · 0 checked"
    assert seen["selected"] == ["pending job"]


def test_not_run_in_days_filter_includes_unknown_and_old_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestStale")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    now = time.time()
    recent = time.strftime("%Y-%m-%d %H:%M", time.localtime(now - 86400))
    old = time.strftime("%Y-%m-%d %H:%M", time.localtime(now - 10 * 86400))
    save_profile(Profile(name="never job", left=str(left), right=str(right)))
    save_profile(Profile(name="recent job", left=str(left), right=str(right), last_result=f"{recent}  1 copied"))
    save_profile(Profile(name="old job", left=str(left), right=str(right), last_result=f"{old}  1 copied"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["days_initially_visible"] = window.stale_days.get_visible()
            window.status_filter.set_selected(6)
            seen["days_visible"] = window.stale_days.get_visible()
            seen["seven"] = _profile_rows(window.list_box)
            window.stale_days.set_value(30)
            seen["thirty"] = _profile_rows(window.list_box)
            self.quit()

    assert App().run(None) == 0
    assert seen["days_initially_visible"] is False
    assert seen["days_visible"] is True
    assert set(seen["seven"]) == {"never job", "old job"}
    assert seen["thirty"] == ["never job"]


def test_exclusion_editor_splits_edits_removes_and_tests_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestExclusionEditor")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            editor = ExclusionEditor([".venv", "*/node_modules", "/datasets/raw", "*"])
            from ofs.ui import _mask_kind

            seen["kinds"] = [_mask_kind(mask) for mask in editor.rules()]
            editor.entry.set_text("cache/tmp;*.log")
            editor._save_rule()
            seen["added"] = editor.rules()
            editor._edit_rule(0)
            editor.entry.set_text(".pyenv")
            editor._save_rule()
            editor.test_entry.set_text("project/.pyenv/bin/python")
            seen["excluded"] = editor.test_result.get_text()
            editor._remove_rule(1)
            editor.test_entry.set_text("project/node_modules/index.js")
            seen["remaining"] = editor.rules()
            seen["not_excluded"] = editor.test_result.get_text()
            includes = ExclusionEditor(["(all)"], include=True)
            includes.test_entry.set_text("photos/a.txt")
            seen["include_all"] = includes.test_result.get_text()
            includes._edit_rule(0)
            includes.entry.set_text("*.jpg;*.png")
            includes._save_rule()
            includes.test_entry.set_text("photos/a.jpg")
            seen["include_jpg"] = includes.test_result.get_text()
            includes.test_entry.set_text("photos/a.txt")
            seen["include_txt"] = includes.test_result.get_text()
            seen["include_rules"] = includes.rules()
            self.quit()

    assert App().run(None) == 0
    assert seen["kinds"] == ["Name anywhere", "Folder pattern", "Rooted path", "Legacy star"]
    assert seen["added"][-2:] == ["cache/tmp", "*.log"]
    assert seen["excluded"].startswith("Excluded by: .pyenv")
    assert "*/node_modules" not in seen["remaining"]
    assert seen["not_excluded"] == "Not excluded by these rules."
    assert seen["include_all"] == "Included by: (all)"
    assert seen["include_jpg"] == "Included by: *.jpg"
    assert seen["include_txt"] == "Not included; this path is left out."
    assert seen["include_rules"] == ["*.jpg", "*.png"]


def test_profile_editor_tabs_keep_save_and_cancel_fixed(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestEditorTabs")
    save_profile(Profile(name="original", left="/sample/left", right="/sample/right", note="preserve"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            editor = window._editor(window.profiles[0])
            seen["tabs"] = [editor.editor_tabs.get_tab_label_text(editor.editor_tabs.get_nth_page(index)) for index in range(editor.editor_tabs.get_n_pages())]
            buttons = [item for item in _widgets(editor.editor_footer) if isinstance(item, Gtk.Button)]
            seen["footer"] = [item.get_label() for item in buttons]
            seen["save_parent_is_footer"] = next(item for item in buttons if item.get_label() == "Save").get_parent() is editor.editor_footer
            editor.editor_tabs.set_current_page(2)
            name = next(item for item in _widgets(editor) if isinstance(item, Gtk.Entry) and item.get_text() == "original")
            name.set_text("cancelled")
            next(item for item in buttons if item.get_label() == "Cancel").emit("clicked")
            seen["cancel_closed"] = not editor.get_visible()
            seen["after_cancel"] = load_profiles()[0].name

            editor = window._editor(window.profiles[0])
            editor.editor_tabs.set_current_page(3)
            name = next(item for item in _widgets(editor) if isinstance(item, Gtk.Entry) and item.get_text() == "original")
            name.set_text("renamed")
            save = next(item for item in _widgets(editor.editor_footer) if isinstance(item, Gtk.Button) and item.get_label() == "Save")
            save.emit("clicked")
            seen["save_closed"] = not editor.get_visible()
            seen["saved"] = load_profiles()[0].to_json()
            self.quit()

    assert App().run(None) == 0
    assert seen["tabs"] == ["General", "Filters", "Comparison", "Recovery & Safety"]
    assert "Cancel" in seen["footer"] and "Save" in seen["footer"] and "Restore Older…" in seen["footer"]
    assert seen["save_parent_is_footer"] is True
    assert seen["cancel_closed"] is True and seen["after_cancel"] == "original"
    assert seen["save_closed"] is True and seen["saved"]["name"] == "renamed"
    assert seen["saved"]["left"] == "/sample/left" and seen["saved"]["right"] == "/sample/right"
    assert seen["saved"]["note"] == "preserve"


def test_removing_exclusion_preserves_page_list_and_main_scroll(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestExclusionScroll")
    for index in range(30):
        rules = [f"build/cache-{rule}" for rule in range(50)] if index == 0 else []
        save_profile(Profile(name=f"sample-{index:02d} profile", left=f"/sample/{index}", right=f"/backup/{index}", exclusions=rules))
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    def outer_scroll(widget):
        while widget is not None and not isinstance(widget, Gtk.ScrolledWindow):
            widget = widget.get_parent()
        return widget

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window.present()
            settle()
            main = window.profiles_scroll.get_vadjustment()
            main.set_value((main.get_upper() - main.get_page_size()) * 0.4)
            editor = window._editor(window.profiles[0])
            editor.editor_tabs.set_current_page(1)
            settle()
            rules = next(item for item in _widgets(editor) if isinstance(item, ExclusionEditor) and not item.include_mode)
            page = outer_scroll(rules).get_vadjustment()
            row_list = outer_scroll(rules.list).get_vadjustment()
            page.set_value((page.get_upper() - page.get_page_size()) * 0.4)
            row_list.set_value((row_list.get_upper() - row_list.get_page_size()) * 0.6)
            remove = next(item for item in _widgets(rules.list.get_row_at_index(10)) if isinstance(item, Gtk.Button) and item.get_label() == "Remove")
            remove.grab_focus()
            settle()
            seen["before"] = (main.get_value(), page.get_value(), row_list.get_value())
            remove.emit("clicked")
            window.reload()
            settle()
            seen["after"] = (main.get_value(), page.get_value(), row_list.get_value())
            seen["rules_left"] = len(rules.rules())
            while len(rules.rules()) > 2:
                rules._remove_rule(0)
            settle()
            seen["clamped"] = row_list.get_value() == 0 and row_list.get_upper() <= row_list.get_page_size()
            editor.close()
            self.quit()

    assert App().run(None) == 0
    assert all(value > 0 for value in seen["before"])
    for before, after in zip(seen["before"], seen["after"]):
        assert after == pytest.approx(before, abs=1)
    assert seen["rules_left"] == 49
    assert seen["clamped"] is True


def test_profile_editor_saves_copy_only_and_include_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestIncludeSave")
    save_profile(Profile(name="selection job", left="/left", right="/right", mode=SyncMode.EXACT_MIRROR))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            dialog = window._editor(window.profiles[0])
            dropdowns = [item for item in _widgets(dialog) if isinstance(item, Gtk.DropDown)]
            seen["original_mode"] = dropdowns[0].get_selected_item().get_string()
            dropdowns[0].set_selected(0)
            editors = [item for item in _widgets(dialog) if isinstance(item, ExclusionEditor)]
            includes = next(item for item in editors if item.include_mode)
            includes._edit_rule(0)
            includes.entry.set_text("*.jpg;*.png")
            includes._save_rule()
            disabled = next(item for item in _widgets(dialog) if isinstance(item, Gtk.CheckButton) and item.get_label().startswith("Disabled"))
            disabled.set_active(True)
            save = next(item for item in _widgets(dialog) if isinstance(item, Gtk.Button) and item.get_label() == "Save")
            save.emit("clicked")
            window.status_filter.set_selected(7)
            seen["disabled_rows"] = _profile_rows(window.list_box)
            self.quit()

    assert App().run(None) == 0
    saved = load_profiles()[0]
    assert seen["original_mode"] == "Exact Mirror (copy/move/delete)"
    assert saved.mode == SyncMode.STANDARD
    assert saved.include == ["*.jpg", "*.png"]
    assert saved.disabled is True
    assert seen["disabled_rows"] == ["selection job"]
    assert saved.name == "selection job"


def test_direction_and_exclusion_dropdown_labels_are_readable_and_mapped(tmp_path, monkeypatch):
    from ofs.model import Direction, ExclusionMode

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestDropdownLabels")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    original = Profile(name="labels", left=str(left), right=str(right), direction=Direction.RIGHT_TO_LEFT, exclusion_mode=ExclusionMode.DONT_COPY, imported_settings=True)
    save_profile(original)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            editor = window._editor(window.profiles[0])
            for dropdown in [item for item in _widgets(editor) if isinstance(item, Gtk.DropDown)]:
                model = dropdown.get_model()
                labels = [model.get_string(index) for index in range(model.get_n_items())]
                if "Both directions" in labels:
                    seen["direction"] = labels
                if "Don't Copy" in labels:
                    seen["exclusion"] = labels
            save = next(item for item in _buttons(editor) if item.get_label() == "Save")
            save.emit("clicked")
            self.quit()

    assert App().run(None) == 0
    assert seen["direction"] == ["Left to right (L → R)", "Right to left (R → L)", "Both directions"]
    assert seen["exclusion"] == ["Ignore Totally", "Don't Copy"]
    saved = load_profiles()[0]
    assert saved.direction == Direction.RIGHT_TO_LEFT
    assert saved.exclusion_mode == ExclusionMode.DONT_COPY
    assert saved.to_json() == original.to_json()


def test_profile_editor_preserves_symlink_metadata_and_other_policy_fields(tmp_path, monkeypatch):
    from ofs.model import SizeConflict, SymlinkMode, TimestampRules

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestPolicyFields")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    original = Profile(
        name="preserved fields",
        left=str(left),
        right=str(right),
        timestamps=TimestampRules(same_time_different_size=SizeConflict.SKIP),
        keep_deleted=True,
        older_folder="LegacyOld",
        max_delete_percent=35,
        block_delete_all=True,
        never_delete_files=True,
        symlinks=SymlinkMode.PRESERVE,
        compare_permissions=True,
        compare_ownership=True,
        preserve_ownership=True,
        always_copy=True,
        imported_settings=True,
    )
    save_profile(original)

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            editor = window._editor(window.profiles[0])
            save = next(item for item in _buttons(editor) if item.get_label() == "Save")
            save.emit("clicked")
            self.quit()

    assert App().run(None) == 0
    assert load_profiles()[0].to_json() == original.to_json()


def test_restore_browser_recovers_a_disabled_profiles_older_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestRestore")
    left = tmp_path / "left"
    right = tmp_path / "right"
    archive = right / "Older" / "folder" / "saved.txt"
    left.mkdir()
    archive.parent.mkdir(parents=True)
    archive.write_text("recover me")
    save_profile(Profile(name="disabled job", left=str(left), right=str(right), disabled=True))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            editor = window._editor(window.profiles[0])
            seen["editor_restore"] = "Restore Older…" in _button_labels(editor)
            browser = RestoreWindow(window, window.profiles[0])
            select_all = next(item for item in _widgets(browser) if isinstance(item, Gtk.CheckButton) and item.get_label() == "Select all archived files")
            select_all.set_active(True)
            destination = tmp_path / "restored"
            browser.destination.set_text(str(destination))
            browser._restore()
            seen["target"] = (destination / "folder" / "saved.txt").read_text()
            seen["archive"] = archive.read_text()
            seen["live_missing"] = not (right / "folder" / "saved.txt").exists()
            seen["status"] = browser.status.get_text()
            self.quit()

    assert App().run(None) == 0
    assert seen["editor_restore"] is True
    assert seen["target"] == seen["archive"] == "recover me"
    assert seen["live_missing"] is True
    assert "Restored 1" in seen["status"]


def test_multi_edit_previews_then_saves_only_the_checked_field(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestBulk")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    save_profile(Profile(name="alpha one", left=str(left), right=str(right), exclusions=["keep"]))
    save_profile(Profile(name="alpha two", left=str(left), right=str(right)))
    save_profile(Profile(name="beta other", left=str(left), right=str(right)))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window.checked = {"alpha one", "alpha two"}
            dialog = window._multi_edit()
            dialog.field.set_selected(1)  # Sync mode
            dialog.value_widget.set_selected(1)  # Exact Mirror
            window._confirm_bulk_edit = lambda changed, field, source: seen.update(changed=changed, field=field, source=source)
            dialog.apply.emit("clicked")
            changed = seen["changed"]
            seen["unchanged_before_save"] = [item.mode for item in load_profiles()]
            window._save_bulk_changes(changed)
            seen["saved"] = {item.name: item for item in load_profiles()}
            self.quit()

    assert App().run(None) == 0
    assert seen["field"] == "mode"
    assert seen["unchanged_before_save"] == [SyncMode.STANDARD] * 3
    assert seen["saved"]["alpha one"].mode == SyncMode.EXACT_MIRROR
    assert seen["saved"]["alpha two"].mode == SyncMode.EXACT_MIRROR
    assert seen["saved"]["beta other"].mode == SyncMode.STANDARD
    assert seen["saved"]["alpha one"].exclusions == ["keep"]


def test_profile_bundle_ui_exports_checked_and_imports_only_after_choice(tmp_path, monkeypatch):
    from ofs.transfer import read_profile_bundle

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestTransfer")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    save_profile(Profile(name="alpha one", left=str(left), right=str(right), imported_settings=True))
    save_profile(Profile(name="alpha two", left=str(left), right=str(right)))
    save_profile(Profile(name="beta other", left=str(left), right=str(right)))
    path = tmp_path / "profiles.json"
    seen = {}

    class LocalDialog:
        def __init__(self, choice=None):
            self.choice = choice

        def save_finish(self, _result):
            return Gio.File.new_for_path(str(path))

        def choose_finish(self, _result):
            return self.choice

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            chosen = [item for item in window.profiles if item.name.startswith("alpha ")]
            window.checked = {item.name for item in chosen}
            window._export_bundle_saved(LocalDialog(), None, chosen)
            exported = read_profile_bundle(path)
            seen["exported"] = [item.name for item in exported]
            seen["export_status"] = window.status.get_text()
            seen["before_cancel"] = [item.name for item in load_profiles()]
            window._bundle_import_chosen(LocalDialog(0), None, exported)
            seen["after_cancel"] = [item.name for item in load_profiles()]
            window._bundle_import_chosen(LocalDialog(2), None, exported)
            seen["after_rename"] = [item.name for item in load_profiles()]
            self.quit()

    assert App().run(None) == 0
    assert seen["exported"] == ["alpha one", "alpha two"]
    assert seen["before_cancel"] == seen["after_cancel"]
    assert "alpha one (imported)" in seen["after_rename"]
    assert "alpha two (imported)" in seen["after_rename"]
    assert "Exported 2" in seen["export_status"]


def test_deletion_safeguard_disables_confirmation_but_allows_safe_continuation(tmp_path, monkeypatch):
    from ofs.review import deletion_violations, safety_text

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestDeleteLimits")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "same").write_text("same")
    (right / "same").write_bytes((left / "same").read_bytes())
    (right / "extra one").write_text("one")
    (right / "extra two").write_text("two")
    profile = Profile(name="guarded", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR, max_delete_percent=10)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            result = scan_profile(profile)
            preview = window._preview(JobResult(profile, result, None))
            seen["violations"] = deletion_violations(profile, preview.rows)
            safety = preview._safety(safety_text(profile, preview.rows), preview._planned(), blocked=bool(seen["violations"]))
            labels = {button.get_label(): button for button in _buttons(safety)}
            seen["confirm_sensitive"] = labels["Confirm Deletions"].get_sensitive()
            seen["continue_sensitive"] = labels["Continue Without Deletions"].get_sensitive()
            safety.close()
            seen["extra_survives"] = (right / "extra one").exists()
            self.quit()

    assert App().run(None) == 0
    assert seen["violations"]
    assert seen["confirm_sensitive"] is False
    assert seen["continue_sensitive"] is True
    assert seen["extra_survives"] is True


def test_copy_stays_in_preview_and_fades_completed_rows_without_changing_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestUnifiedCopy")
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "nested").mkdir(parents=True)
    right.mkdir()
    (left / "a.txt").write_text("alpha")
    (left / "nested" / "b.txt").write_text("beta")
    profile = Profile(name="alpha copy", left=str(left), right=str(right))
    save_profile(profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            result = scan_profile(profile)
            preview = window._preview(JobResult(profile, result, None))
            seen["before"] = [change.rel for change in preview._planned()]
            window.close()
            seen["overview_closed"] = not window.get_visible()
            preview._begin(preview._planned(), "test embedded progress")
            seen["locked_with_closed_overview"] = self.profile_is_active("alpha copy")
            seen["panel"] = preview.copy_details.get_child() is not None
            GLib.timeout_add(20, self.watch, preview)

        def watch(self, preview):
            from ofs.run_history import list_history

            if preview._running:
                return True
            history = list_history()
            seen["history"] = [(entry.profile, entry.kind, entry.log_path.is_file()) for entry in history]
            seen["released_with_closed_overview"] = not self.profile_is_active("alpha copy")
            seen["after"] = [change.rel for change in preview._planned()]
            seen["copied"] = set(preview._completed_rows)
            seen["flags"] = {rel: preview.tree_store.get_value(tree_iter, 5) for rel, tree_iter in preview._file_iters.items()}
            name_column = next(column for column in preview.view.get_columns() if column.get_title() == "Name")
            renderer = name_column.get_cells()[0]
            tree_iter = preview._file_iters["a.txt"]
            preview._result_cell_style(name_column, renderer, preview.tree_store, tree_iter, None)
            seen["subdued"] = not renderer.get_property("sensitive")
            seen["collapsed"] = not preview.copy_details.get_expanded()
            seen["source_survives"] = (left / "a.txt").read_text() == "alpha"
            seen["destinations"] = ((right / "a.txt").read_text(), (right / "nested" / "b.txt").read_text())
            seen["results"] = any(isinstance(item, ResultsWindow) for item in self.get_windows())
            self.quit()
            return False

    assert App().run(None) == 0
    assert seen["panel"] is True
    assert seen["before"] == seen["after"] == ["a.txt", "nested/b.txt"]
    assert seen["copied"] == {"a.txt", "nested/b.txt"}
    assert seen["flags"] == {"a.txt": True, "nested/b.txt": True}
    assert seen["subdued"] is True and seen["collapsed"] is True
    assert seen["overview_closed"] is True
    assert seen["locked_with_closed_overview"] is True and seen["released_with_closed_overview"] is True
    assert seen["source_survives"] is True and seen["destinations"] == ("alpha", "beta")
    assert seen["results"] is True
    assert seen["history"] == [("alpha copy", "copy", True)]


def test_copy_progress_window_shows_bytes_current_file_and_operation_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestCopyProgress")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            progress = ProgressWindow(window, "job")
            progress.update(CopyProgress(1, 4, "large.bin", 50, 100, 150, 300, 1, 0, 0, 0))
            seen["overall"] = progress.bar.get_fraction()
            seen["current"] = progress.file_bar.get_fraction()
            seen["counts"] = " ".join(progress.count_label.get_text().split())
            seen["timing"] = progress.timing_label.get_text()
            progress._toggle()
            seen["paused"] = progress.control.paused
            seen["paused_label"] = progress.pause_button.get_label()
            progress._toggle()
            seen["resumed"] = not progress.control.paused
            progress.update(CopyProgress(1, 2, "", 0, 0, 0, 0, 0, 1, 0, 0))
            seen["delete_move_overall"] = progress.bar.get_fraction()
            progress.allow_close()
            self.quit()

    assert App().run(None) == 0
    assert seen["overall"] == 0.5
    assert seen["current"] == 0.5
    assert "Operation 1 / 4" in seen["counts"]
    assert "Processed 150 B / 300 B" in seen["counts"]
    assert "Speed" in seen["timing"] and "Elapsed" in seen["timing"]
    assert seen["paused"] is True
    assert seen["paused_label"] == "Resume"
    assert seen["resumed"] is True
    assert seen["delete_move_overall"] == 0.5


def test_copy_path_uses_one_bounded_line_without_resizing_the_window(tmp_path, monkeypatch):
    from gi.repository import Pango

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestCopyPath")
    seen = {}
    long_path = "Projects / A long descriptive folder / Archived files / " * 3 + "final file.txt"

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            progress = ProgressWindow(window, "demo")
            progress.present()
            progress.update(CopyProgress(0, 1, "short.txt", 0, 10, 0, 10, 0, 0, 0, 0))
            seen["short"] = progress.get_height()
            progress.update(CopyProgress(0, 1, long_path, 0, 10, 0, 10, 0, 0, 0, 0))
            seen["long"] = progress.get_height()
            seen["path"] = progress.file_label.get_text()
            seen["tooltip"] = progress.file_label.get_tooltip_text()
            seen["lines"] = progress.file_label.get_lines()
            seen["ellipsize"] = progress.file_label.get_ellipsize()
            progress.allow_close()
            self.quit()

    assert App().run(None) == 0
    assert seen["short"] == seen["long"]
    assert seen["path"] == seen["tooltip"] == long_path
    assert seen["lines"] == 1
    assert seen["ellipsize"] == Pango.EllipsizeMode.MIDDLE


def test_copy_stat_columns_have_stable_width_and_paused_state_does_not_shift_them(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestStableStats")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            progress = ProgressWindow(owner, "demo")
            progress._started = time.monotonic() - 10
            progress.update(CopyProgress(2, 26539, "a", 100, 1000, 32 * 1024 ** 2, 8 * 1024 ** 3, 2, 0, 0, 0))
            seen["count_size"] = progress.count_label.get_layout().get_pixel_size()
            seen["timing_size"] = progress.timing_label.get_layout().get_pixel_size()
            progress.update(CopyProgress(20000, 26539, "b", 900, 1000, 4 * 1024 ** 3, 8 * 1024 ** 3, 20000, 12, 0, 1))
            seen["large_count_size"] = progress.count_label.get_layout().get_pixel_size()
            seen["large_timing_size"] = progress.timing_label.get_layout().get_pixel_size()
            progress._toggle()
            seen["paused_timing"] = progress.timing_label.get_text()
            seen["monospace"] = "monospace" in progress.count_label.get_css_classes()
            self.quit()

    assert App().run(None) == 0
    assert seen["count_size"] == seen["large_count_size"]
    assert seen["timing_size"] == seen["large_timing_size"]
    assert seen["paused_timing"].endswith("Paused")
    assert "Elapsed" in seen["paused_timing"] and seen["monospace"] is True


def test_after_copy_power_choice_is_per_run_and_guards_errors_and_cancel(tmp_path, monkeypatch):
    import ofs.ui as ui
    from ofs.apply import ApplyReport
    from ofs.scan import ScanResult

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestPower")
    profile = Profile(name="backup", left="/sample/left", right="/sample/right")
    save_profile(profile)
    calls = []

    class ClosedProgress:
        def allow_close(self):
            pass

    monkeypatch.setattr(ui, "run_completion_action", lambda action: calls.append(action))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            seen["default"] = preview.completion_action.get_selected()
            seen["labels"] = [preview.completion_action.get_model().get_string(index) for index in range(preview.completion_action.get_model().get_n_items())]
            preview._finished(ClosedProgress(), ApplyReport(), None)
            preview.completion_action.set_selected(1)
            preview._finished(ClosedProgress(), ApplyReport(copied=1), None)
            preview.completion_action.set_selected(2)
            preview._finished(ClosedProgress(), ApplyReport(errors=["copy failed"]), None)
            preview.completion_action.set_selected(3)
            preview._finished(ClosedProgress(), ApplyReport(cancelled=True), None)
            other_copy = ProgressWindow(preview, "other")
            seen["registered"] = other_copy in owner.get_application().get_windows()
            preview.completion_action.set_selected(1)
            preview._finished(ClosedProgress(), ApplyReport(copied=2), None)
            seen["blocked"] = "another copy" in preview.detail.get_text()
            other_copy.allow_close()
            other_preview = owner._preview(JobResult(profile, ScanResult([]), None))
            other_preview._running = True
            owner.close()
            preview._finished(ClosedProgress(), ApplyReport(copied=2), None)
            seen["closed_overview_another_preview"] = "another copy" in preview.detail.get_text()
            other_preview._running = False
            self.quit()

    assert App().run(None) == 0
    assert seen["default"] == 0
    assert seen["labels"] == ["Do nothing", "Sleep", "Hibernate", "Shut down"]
    assert calls == ["sleep"]
    assert seen["registered"] is True
    assert seen["blocked"] is True
    assert seen["closed_overview_another_preview"] is True


def test_copy_speed_presets_cover_common_mib_per_second_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestSpeedPresets")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            settings = owner._settings()
            preset = next(item for item in _widgets(settings) if isinstance(item, Gtk.DropDown) and item.get_model().get_n_items() == 5 and item.get_model().get_string(1) == "1 MiB/s")
            for index, speed_name in ((1, "one"), (2, "ten"), (3, "hundred")):
                preset.set_selected(index)
                seen[speed_name] = preset.get_selected_item().get_string()
            seen["save"] = next(item for item in _widgets(settings) if isinstance(item, Gtk.Button) and item.get_label() == "Save")
            seen["save"].emit("clicked")
            self.quit()

    assert App().run(None) == 0
    assert (seen["one"], seen["ten"], seen["hundred"]) == ("1 MiB/s", "10 MiB/s", "100 MiB/s")
    from ofs.settings import copy_speed_limit
    assert copy_speed_limit() == 100 * 1024 ** 2


def test_failed_results_can_exclude_paths_or_their_common_folder(tmp_path, monkeypatch):
    from ofs.apply import ApplyReport
    from ofs.scan import ScanResult
    from ofs.ui import _failed_common_folder, _failed_relative_paths

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFailedExclusions")
    errors = [
        "project/service/volumes/redis/dump.db: Permission denied",
        "project/service/volumes/app/modules.db: Permission denied",
    ]
    assert _failed_relative_paths(errors) == [
        "project/service/volumes/redis/dump.db",
        "project/service/volumes/app/modules.db",
    ]
    assert _failed_common_folder(_failed_relative_paths(errors)) == "project/service/volumes"
    assert _failed_relative_paths(["/absolute/private/path: permission denied", "../escape: denied"]) == []
    profile = Profile(name="failing job", left="/sample/left", right="/sample/right", exclusions=["existing"])
    save_profile(profile)
    seen = {}

    class Dialog:
        def __init__(self, choice):
            self.choice = choice

        def choose_finish(self, _result):
            return self.choice

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            results = ResultsWindow(preview, ApplyReport(errors=errors), None)
            seen["button"] = results.exclude_common_button.get_label()
            results._exclude_failed_chosen(Dialog(0), None, ["project/service/volumes"])
            seen["cancel"] = load_profiles()[0].exclusions
            results._exclude_failed_chosen(Dialog(1), None, ["project/service/volumes"])
            seen["saved"] = load_profiles()[0].exclusions
            seen["notice"] = results.exclusion_notice.get_text()
            seen["disabled"] = not results.exclude_paths_button.get_sensitive() and not results.exclude_common_button.get_sensitive()
            self.quit()

    assert App().run(None) == 0
    assert seen["button"] == "Exclude common folder…"
    assert seen["cancel"] == ["existing"]
    assert seen["saved"] == ["existing", "project/service/volumes"]
    assert "Retry Failed" in seen["notice"]
    assert seen["disabled"] is True


def test_confirmed_new_folder_copy_uses_chunked_engine_and_invalidates_cache(tmp_path, monkeypatch):
    from ofs.cache import _path, save_destination
    from ofs.store import config_dir

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestWholeFolder")
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "inbox").mkdir(parents=True)
    right.mkdir()
    (left / "inbox" / "document.txt").write_text("new folder content")
    profile = Profile(name="known new folder", left=str(left), right=str(right), cache_destination=True)
    save_profile(profile)
    save_destination(profile, "Right", [], profile.exclusion_mode, {})
    seen = {}

    class Dialog:
        def __init__(self, choice):
            self.choice = choice

        def choose_finish(self, _result):
            return self.choice

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["canceled"] = window._new_folder_chosen(Dialog(0), None, profile, "inbox")
            seen["canceled_absent"] = not (right / "inbox").exists()
            seen["progress"] = window._new_folder_chosen(Dialog(1), None, profile, "inbox")

            def watch() -> bool:
                if profile.name not in window._folder_copy_running:
                    seen["status"] = window.status.get_text()
                    seen["copied"] = (right / "inbox" / "document.txt").read_text()
                    seen["cache_exists"] = _path(profile).exists()
                    seen["logs"] = list((config_dir() / "logs").glob("*.log"))
                    self.quit()
                    return False
                return True

            GLib.timeout_add(20, watch)

    assert App().run(None) == 0
    assert seen["canceled"] is None
    assert seen["canceled_absent"] is True
    assert seen["progress"].relative == "inbox"
    assert seen["progress"].bar.get_text() == "Copying new folder…"
    assert seen["copied"] == "new folder content"
    assert seen["cache_exists"] is False
    assert seen["logs"] and "explicit new folder" in seen["logs"][0].read_text()
    assert "1 copied" in seen["status"]


def test_profile_left_and_right_browsers_choose_without_saving_automatically(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestBrowsePaths")
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    profile = Profile(name="browse paths", left="/sample/old-left", right="/sample/old-right")
    save_profile(profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            editor = owner._editor(profile)
            browsers = [button for button in _buttons(editor) if _button_labels(button) == ["Browse…"]]
            seen["count"] = len(browsers)
            browsers[0].emit("clicked")
            chooser = next(Gtk.Window.get_toplevels().get_item(index) for index in range(Gtk.Window.get_toplevels().get_n_items()) if isinstance(Gtk.Window.get_toplevels().get_item(index), Gtk.FileChooserDialog))
            seen["title"] = chooser.get_title()
            chooser.set_file(Gio.File.new_for_path(str(chosen)))
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            selected_file = chooser.get_file()
            seen["selected"] = selected_file is not None and selected_file.get_path() == str(chosen)
            chooser.emit("response", Gtk.ResponseType.ACCEPT)
            seen["entry_updated"] = any(isinstance(item, Gtk.Entry) and item.get_text() == str(chosen) for item in _widgets(editor))
            seen["saved_left"] = load_profiles()[0].left
            self.quit()

    assert App().run(None) == 0
    assert seen["count"] == 2
    assert seen["title"] == "Choose Left folder"
    assert seen["selected"] is True
    assert seen["entry_updated"] is True
    assert seen["saved_left"] == "/sample/old-left"


def test_whole_folder_error_still_invalidates_destination_cache(tmp_path, monkeypatch):
    import ofs.ui as ui
    from ofs.cache import _path, save_destination
    from ofs.folder_copy import FolderCopyOutcome
    from ofs.apply import ApplyReport

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFailedFolderCache")
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "inbox").mkdir(parents=True)
    right.mkdir()
    profile = Profile(name="copy cache", left=str(left), right=str(right), cache_destination=True)
    save_profile(profile)
    save_destination(profile, "Right", [], profile.exclusion_mode, {})
    outcome = FolderCopyOutcome(ApplyReport(errors=["inbox: cleanup failed; hidden temporary output remains"]))
    monkeypatch.setattr(ui, "copy_new_folder", lambda *_args, **_kwargs: outcome)
    seen = {}

    class Dialog:
        def choose_finish(self, _result):
            return 1

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            owner._new_folder_chosen(Dialog(), None, profile, "inbox")

            def watch() -> bool:
                if profile.name not in owner._folder_copy_running:
                    seen["cache_exists"] = _path(profile).exists()
                    seen["errors"] = outcome.report.errors
                    self.quit()
                    return False
                return True

            GLib.timeout_add(20, watch)

    assert App().run(None) == 0
    assert seen["cache_exists"] is False
    assert outcome.report.copied == 0 and outcome.report.folders_created == 0
    assert "cleanup failed" in seen["errors"][0]


def test_retry_requires_confirmation_and_rescans_instead_of_replaying(tmp_path, monkeypatch):
    from ofs.apply import ApplyReport
    from ofs.scan import ScanResult

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestRetry")
    left = tmp_path / "left"
    left.mkdir()
    right = tmp_path / "right"
    right.mkdir()
    save_profile(Profile(name="retry job", left=str(left), right=str(right)))
    seen = {}

    class Dialog:
        def __init__(self, choice):
            self.choice = choice

        def choose_finish(self, _result):
            return self.choice

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            profile = window.profiles[0]
            preview = window._preview(JobResult(profile, ScanResult([]), None))
            results = ResultsWindow(preview, ApplyReport(errors=["copy failed"]), None)
            seen["retry"] = next(item for item in _buttons(results) if item.get_label() == "Retry Failed…").get_sensitive()
            window._run_names = lambda names: seen.setdefault("runs", set(names))
            results._retry_chosen(Dialog(0), None)
            seen["cancelled"] = seen.get("runs")
            results._retry_chosen(Dialog(1), None)
            seen["accepted"] = seen.get("runs")
            self.quit()

    assert App().run(None) == 0
    assert seen["retry"] is True
    assert seen["cancelled"] is None
    assert seen["accepted"] == {"retry job"}


def test_preview_filters_keep_the_plan_and_safety_window_opens(tmp_path, monkeypatch):
    home = tmp_path / "home"
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "notes.txt").write_text("new")
    (right / "notes.txt").write_text("old")
    (left / "same.txt").write_text("same")
    (right / "same.txt").write_text("same")
    (right / "extra.txt").write_text("extra")
    os.utime(left / "notes.txt", (50, 50))
    os.utime(right / "notes.txt", (10, 10))
    os.utime(left / "same.txt", (10, 10))
    os.utime(right / "same.txt", (10, 10))
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFilters")
    save_profile(Profile(name="alpha notes", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            profile = window.profiles[0]
            result = scan_profile(profile)
            preview = window._preview(JobResult(profile, result, None))
            shown = [preview.store.get_item(index).change.rel for index in range(preview.store.get_n_items())]
            seen["default"] = shown
            seen["right"] = preview.right_status.get_text()
            seen["unchanged"] = any(row.change.action == Action.UNCHANGED for row in result.listing)
            preview._set_filter(deletes="only")
            seen["deletes"] = [preview.store.get_item(index).change.rel for index in range(preview.store.get_n_items())]
            seen["still_planned"] = [change.rel for change in preview._planned()]
            safety = preview._safety("Files to be deleted: 1", preview._planned())
            seen["safety"] = _button_labels(safety)
            safety.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["unchanged"]
    assert "same.txt" not in seen["default"]
    assert set(seen["default"]) == {"notes.txt", "extra.txt"}
    assert "Copy: 1" in seen["right"]
    assert "Delete: 1" in seen["right"]
    assert seen["deletes"] == ["extra.txt"]
    assert "notes.txt" in seen["still_planned"]
    assert "Confirm Deletions" in seen["safety"]
    assert "Continue Without Deletions" in seen["safety"]
    assert "Do Not Continue" in seen["safety"]


def test_live_preview_batches_entries_and_keeps_start_disabled_until_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestLivePreview")
    save_profile(Profile(name="alpha", left="/sample/left", right="/sample/right"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            profile = window.profiles[0]
            preview = window._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(window)
            preview._begin_scan(progress)
            seen["controls_shared"] = preview.scan_details.get_child() is not None and preview._scan_progress is progress
            preview._queue_entry("Left", "root.txt", FileStat(4, 1))
            for index in range(500):
                preview._queue_entry("Left", f"folder/item-{index}", FileStat(index + 1, 2))
            preview._drain_pending()
            seen["first_batch"] = preview.store.get_n_items()
            while preview._entry_queue:
                preview._drain_pending()
            seen["pending_count"] = len(preview.rows)
            seen["pending_status"] = preview.tree_store.get_value(preview._file_iters["folder/item-0"], 3)
            preview._start()
            seen["direct_start_blocked"] = not preview._running and "not complete" in preview.detail.get_text()
            seen["button_disabled"] = not preview.start_button.get_sensitive()
            preview._save_exclusion("folder")
            assert "folder" in profile.exclusions
            seen["excluded_removed"] = all(not row.change.rel.startswith("folder/") for row in preview.rows)
            preview._queue_entry("Left", "folder/after.txt", FileStat(1, 2))
            preview._drain_pending()
            seen["queued_exclusion"] = all(row.change.rel != "folder/after.txt" for row in preview.rows)

            result = ScanResult([Change("root.txt", Action.COPY_LEFT_TO_RIGHT, "only on source", 4)])
            preview._finish_scan(JobResult(profile, result, None))
            seen["complete"] = preview._scan_complete and not preview._running
            seen["collapsed"] = not preview.scan_details.get_expanded()
            seen["start_enabled"] = preview.start_button.get_sensitive()
            seen["final_paths"] = [row.change.rel for row in preview.rows]
            progress.allow_close()
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["controls_shared"] is True
    assert seen["first_batch"] == 250 and seen["pending_count"] == 501
    assert "awaiting comparison" in seen["pending_status"]
    assert seen["direct_start_blocked"] is True and seen["button_disabled"] is True
    assert seen["excluded_removed"] is True and seen["queued_exclusion"] is True
    assert seen["complete"] and seen["collapsed"] and seen["start_enabled"]
    assert seen["final_paths"] == ["root.txt"]


def test_scan_reports_cache_off_fresh_and_reused_states(tmp_path, monkeypatch):
    from ofs.walk import WalkStats

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestVisibleCacheState")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "entry.txt").write_text("same")
    (right / "entry.txt").write_text("same")
    profile = Profile(name="cache status", left=str(left), right=str(right), cache_destination=True)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            result = scan_profile(profile, on_stats=lambda side, rel, values: preview._record_scan_stats(profile.name, side, rel, values))
            seen["fresh"] = preview.right_status.get_text()
            progress.allow_close()
            preview._finish_scan(JobResult(profile, result, None))
            preview.close()

            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            result = scan_profile(profile, on_stats=lambda side, rel, values: preview._record_scan_stats(profile.name, side, rel, values))
            seen["reused"] = preview.right_status.get_text()
            progress.allow_close()
            preview._finish_scan(JobResult(profile, result, None))
            preview.close()

            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress, uncached=True)
            preview._record_scan_stats(profile.name, "Right", "fresh/path", WalkStats(1, 0, 1))
            seen["override"] = preview.right_status.get_text()
            progress.allow_close()
            preview._finish_scan(JobResult(profile, ScanResult([]), None))
            preview.close()

            profile.cache_destination = False
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            preview._record_scan_stats(profile.name, "Right", "fresh/path", WalkStats(1, 0, 1))
            seen["off"] = preview.right_status.get_text()
            progress.allow_close()
            preview._finish_scan(JobResult(profile, ScanResult([]), None))
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert "no compatible snapshot" in seen["fresh"]
    assert "Cache reused" in seen["reused"]
    assert "uncached override" in seen["override"]
    assert "cache: off" in seen["off"]


def test_scan_counts_stay_for_both_sides(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestScanCounts")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            from ofs.walk import WalkStats

            scan = ScanWindow(window)
            scan.update_stats("job", "Left", "album/more", WalkStats(45, 5, 40))
            scan.update_stats("job", "Right", "album", WalkStats(4, 1, 3))
            seen["counts"] = scan.count_label.get_text()
            seen["place"] = scan.file_label.get_text()
            seen["reading"] = scan.side_label.get_text()
            seen["path_wrap"] = scan.file_label.get_wrap()
            scan._skip()
            seen["skip"] = scan.file_label.get_text()
            seen["skipping"] = scan.control.skipping
            seen["skip_enabled"] = scan.skip_button.get_sensitive()
            seen["modal"] = scan.get_modal()
            estimated = ScanWindow(window, estimates={"job": (1000, 20.0)})
            estimated.update_stats("job", "Left", "folder", WalkStats(450, 0, 450))
            seen["estimated"] = estimated.count_label.get_text()
            estimated.allow_close()
            self.quit()

    assert App().run(None) == 0
    assert seen["counts"].splitlines()[:3] == [
        "Folders and files found: 49",
        "Filtered (ignored): 6",
        "Added to file list: 43",
    ]
    assert "entries/s" in seen["counts"]
    assert "ETA after first completed scan" in seen["counts"]
    assert seen["place"] == "album"
    assert seen["reading"] == "Reading right"
    assert seen["path_wrap"] is False
    assert seen["skip"].startswith("Skipping rest")
    assert seen["skipping"] is True
    assert seen["skip_enabled"] is False
    assert seen["modal"] is False
    assert "ETA ~" in seen["estimated"]
    assert "ETA after first" not in seen["estimated"]


def test_scan_path_truncation_keeps_the_dialog_height_stable(tmp_path, monkeypatch):
    from gi.repository import Pango

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestStableScanPath")
    seen = {}
    long_path = (
        "Library with a deliberately long folder name / Archived collections / "
        "Several nested levels / A document with a descriptive name that should be truncated at a fixed width.txt"
    )

    class App(SfsApplication):
        def do_activate(self):
            from ofs.walk import WalkStats

            owner = MainWindow(self)
            scan = ScanWindow(owner)
            scan.present()
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            scan.update_stats("demo archive", "Left", "short/path.txt", WalkStats(5, 1, 4))
            seen["short_height"] = scan.get_height()
            scan.update_stats("demo archive", "Left", long_path, WalkStats(5, 1, 4))
            seen["long_height"] = scan.get_height()
            seen["full_path"] = scan.file_label.get_text()
            seen["tooltip"] = scan.file_label.get_tooltip_text()
            seen["ellipsize"] = scan.file_label.get_ellipsize()
            seen["lines"] = scan.file_label.get_lines()
            scan.allow_close()
            self.quit()

    assert App().run(None) == 0
    assert seen["short_height"] == seen["long_height"]
    assert seen["full_path"] == seen["tooltip"] == long_path
    assert seen["ellipsize"] == Pango.EllipsizeMode.MIDDLE
    assert seen["lines"] == 1


def test_different_profiles_scan_in_parallel_but_same_profile_cannot_duplicate(tmp_path, monkeypatch):
    import threading
    import ofs.ui as ui
    from ofs.scan import ScanResult

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestParallelScans")
    for name in ("alpha", "beta", "gamma"):
        save_profile(Profile(name=name, left=str(left), right=str(right)))
    started = {name: threading.Event() for name in ("alpha", "beta", "gamma")}
    release = threading.Event()
    calls = []
    uncached = {}
    seen = {}

    def controlled_scan(profiles, **kwargs):
        name = profiles[0].name
        calls.append(name)
        uncached[name] = kwargs["force_uncached"]
        started[name].set()
        assert release.wait(5), name
        return [JobResult(profiles[0], ScanResult(changes=[]), None)]

    monkeypatch.setattr(ui, "run_checked", controlled_scan)

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            original_show_results = window._show_results

            def record_results(results):
                seen.setdefault("batch_sizes", []).append(len(results))
                return original_show_results(results)

            window._show_results = record_results
            seen["uncached_button"] = _button_labels(window.uncached_button)[0]
            seen["uncached_icon"] = next(item.get_icon_name() for item in _widgets(window.uncached_button) if isinstance(item, Gtk.Image))
            seen["uncached_tooltip"] = window.uncached_button.get_tooltip_text()
            window._run_names({"alpha"})
            seen["alpha_status"] = window._labels["alpha"].get_text()
            window._run_names({"alpha"})
            seen["duplicate_status"] = window.status.get_text()
            window._run_names({"beta", "gamma"}, uncached=True)
            seen["running_buttons"] = [button for button in _buttons(window) if button.get_label() == "Running"]

            def watch() -> bool:
                if all(event.is_set() for event in started.values()):
                    seen["parallel"] = True
                    release.set()
                if release.is_set() and not window._scan_running:

                    self.quit()
                    return False
                return True

            GLib.timeout_add(20, watch)

    assert App().run(None) == 0
    assert seen["uncached_button"] == "Run uncached"
    assert seen["uncached_icon"] == "view-refresh-symbolic"
    assert "without destination caches" in seen["uncached_tooltip"]
    assert seen["alpha_status"] == "Scanning…"
    assert "already scanning" in seen["duplicate_status"]
    assert len(seen["running_buttons"]) == 3
    assert all(not button.get_sensitive() for button in seen["running_buttons"])
    assert seen["parallel"] is True
    assert sorted(calls) == ["alpha", "beta", "gamma"]
    assert uncached == {"alpha": False, "beta": True, "gamma": True}
    assert sorted(seen["batch_sizes"]) == [1, 2]


def test_preview_actions_stay_visible_and_folder_controls_have_a_toolbar(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestPreviewControlLayout")
    profile = Profile(name="control layout", left="/sample/left", right="/sample/right")
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            preview.set_default_size(1100, 720)
            preview.present()
            settle()
            seen["maximized"] = preview.is_maximized()
            preview.unmaximize()
            preview.set_default_size(1100, 720)
            settle()
            close = next(button for button in _buttons(preview) if button.get_label() == "Close")
            seen["scrolled_actions"] = [
                widget.get_ancestor(Gtk.ScrolledWindow) is not None
                for widget in (preview.shown, preview.completion_action, preview.start_button, close)
            ]
            seen["folder_controls_scrolled"] = preview.expand_all_button.get_ancestor(Gtk.ScrolledWindow) is not None
            tree_controls = getattr(preview, "tree_controls", preview.expand_all_button.get_parent())
            seen["folder_heading"] = any(
                isinstance(widget, Gtk.Label) and widget.get_text() == "Folders"
                for widget in _widgets(tree_controls)
            )
            seen["window_height"] = preview.get_height()
            left_point = preview.left_status.translate_coordinates(preview, 0, 0)
            right_point = preview.right_status.translate_coordinates(preview, 0, 0)
            seen["side_columns"] = preview.sides_header.get_orientation() == Gtk.Orientation.HORIZONTAL
            seen["side_positions"] = right_point[0] > left_point[0] and right_point[1] == left_point[1]
            for name, widget in (("start", preview.start_button), ("close", close)):
                point = widget.translate_coordinates(preview, 0, 0)
                seen[f"{name}_bottom"] = point[1] + widget.get_height() if point is not None else -1
            self.quit()

    assert App().run(None) == 0
    assert seen["scrolled_actions"] == [False, False, False, False]
    assert seen["folder_controls_scrolled"] is False
    assert seen["folder_heading"] is True
    assert seen["side_columns"] is True and seen["side_positions"] is True
    assert seen["maximized"] is True
    assert 0 < seen["start_bottom"] <= seen["window_height"]
    assert 0 < seen["close_bottom"] <= seen["window_height"]


def test_scan_path_is_one_line_fixed_width_with_stats_below(tmp_path, monkeypatch):
    from ofs.walk import WalkStats

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestScanInfoLayout")
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            scan = ScanWindow(owner, estimates={"activation": (50000, 20)})
            scan.set_default_size(900, 400)
            scan.present()
            path_box = scan.file_label.get_parent()
            short = "short/file.txt"
            long = "/".join(["an-extremely-long-directory-component"] * 90) + "/last-file-with-a-long-name.txt"
            for marker, path, found in (("short", short, 23), ("long", long, 100000)):
                scan.update_stats("activation", "Left", path, WalkStats(found, 18, found - 81))
                settle()
                seen[f"{marker}_width"] = path_box.get_width()
                seen[f"{marker}_lines"] = scan.file_label.get_layout().get_line_count()
                seen[f"{marker}_height"] = scan.file_label.get_height()
            seen["no_wrap"] = not scan.file_label.get_wrap()
            seen["bounded"] = scan.file_label.get_max_width_chars() <= 52
            seen["ellipsized"] = scan.file_label.get_layout().is_ellipsized()
            caption = next(
                child for child in _widgets(path_box)
                if isinstance(child, Gtk.Label) and child.get_text() == "Current path"
            )
            caption_point = caption.translate_coordinates(path_box, 0, 0)
            label_point = scan.file_label.translate_coordinates(path_box, 0, 0)
            seen["compact"] = path_box.get_height() < 40
            seen["inline_caption"] = abs(caption_point[1] - label_point[1]) < 6
            path_point = path_box.translate_coordinates(scan, 0, 0)
            count_point = scan.count_label.translate_coordinates(scan, 0, 0)
            seen["totals_below"] = count_point[1] >= path_point[1] + path_box.get_height()
            scan.allow_close()
            self.quit()

    assert App().run(None) == 0
    assert seen["short_width"] == seen["long_width"]
    assert seen["short_lines"] == seen["long_lines"] == 1
    assert seen["short_height"] == seen["long_height"]
    assert seen["no_wrap"] is True
    assert seen["bounded"] is True and seen["ellipsized"] is True
    assert seen["compact"] is True and seen["inline_caption"] is True
    assert seen["totals_below"] is True


def test_exclusion_dialog_supports_editing_path_name_and_global_scope(tmp_path, monkeypatch):
    from ofs.settings import global_exclusion_mode, global_exclusions, set_global_exclusion_mode, set_global_exclusions

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestExclusionChoices")
    profile = Profile(name="exclusion choices", left="/sample/left", right="/sample/right", imported_settings=True, note="Keep this note")
    set_global_exclusions(["existing.tmp"])
    set_global_exclusion_mode(ExclusionMode.DONT_COPY)
    save_profile(profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            row = PlanRow(Change("nested/cache/item.tmp", Action.COPY_LEFT_TO_RIGHT, "new", 12))
            dialog = preview._ask_exclusion(row)
            seen["path_default"] = dialog.exclusion_entry.get_text()
            dialog.exclusion_save_button.emit("clicked")
            seen["profile_rule"] = list(profile.exclusions)

            before_global = profile.to_json()
            dialog = preview._ask_exclusion(row)
            dialog.exclusion_scope.set_selected(1)
            seen["scope_preserves_path"] = dialog.exclusion_entry.get_text()
            dialog.exclusion_match.set_selected(1)
            seen["name_default"] = dialog.exclusion_entry.get_text()
            dialog.exclusion_entry.set_text("shared-name")
            dialog.exclusion_save_button.emit("clicked")
            seen["global_rules"] = global_exclusions()
            seen["global_mode"] = global_exclusion_mode()
            seen["profile_preserved"] = profile.to_json() == before_global

            dialog = preview._ask_exclusion("other/data", folder=True)
            seen["folder_default"] = dialog.exclusion_entry.get_text()
            dialog.exclusion_entry.set_text("  ")
            dialog.exclusion_save_button.emit("clicked")
            seen["blank_saved"] = "other/data" in global_exclusions() or not global_exclusions()
            next(button for button in _buttons(dialog) if button.get_label() == "Cancel").emit("clicked")
            seen["after_cancel"] = global_exclusions()
            self.quit()

    assert App().run(None) == 0
    assert seen["path_default"] == "nested/cache/item.tmp"
    assert seen["profile_rule"] == ["nested/cache/item.tmp"]
    assert seen["scope_preserves_path"] == "nested/cache/item.tmp"
    assert seen["name_default"] == "item.tmp"
    assert seen["global_rules"] == ["existing.tmp", "shared-name"]
    assert seen["global_mode"] == ExclusionMode.DONT_COPY
    assert seen["profile_preserved"] is True
    assert seen["folder_default"] == "other/data"
    assert seen["blank_saved"] is False
    assert seen["after_cancel"] == ["existing.tmp", "shared-name"]


def test_global_exclusion_filters_live_displayed_queued_and_final_rows(tmp_path, monkeypatch):
    from ofs.settings import global_exclusions, set_global_exclusions

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestGlobalLiveExclusion")
    set_global_exclusions(["existing.tmp"])
    save_profile(Profile(name="live global", left="/sample/left", right="/sample/right"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            profile = owner.profiles[0]
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            preview._queue_entry("Left", "reports/drop/first.tmp", FileStat(10, 1))
            preview._drain_pending()
            preview._queue_entry("Left", "reports/drop/queued.tmp", FileStat(20, 1))
            dialog = preview._ask_exclusion("reports/drop", folder=True)
            dialog.exclusion_scope.set_selected(1)
            dialog.exclusion_save_button.emit("clicked")
            seen["visible_removed"] = all(not item.change.rel.startswith("reports/drop/") for item in preview.rows)
            preview._drain_pending()
            preview._queue_entry("Right", "reports/drop/future.tmp", FileStat(30, 2))
            preview._drain_pending()
            seen["queued_and_future_removed"] = all(not item.change.rel.startswith("reports/drop/") for item in preview.rows)
            result = ScanResult([
                Change("reports/drop/final.tmp", Action.COPY_LEFT_TO_RIGHT, "new", 40),
                Change("reports/keep.txt", Action.COPY_LEFT_TO_RIGHT, "new", 50),
            ])
            preview._finish_scan(JobResult(profile, result, None))
            seen["final_changes"] = [change.rel for change in result.changes]
            seen["final_rows"] = [row.change.rel for row in preview.rows]
            seen["global_rules"] = global_exclusions()
            seen["profile_rules"] = list(profile.exclusions)
            progress.allow_close()
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["visible_removed"] is True
    assert seen["queued_and_future_removed"] is True
    assert seen["final_changes"] == ["reports/keep.txt"]
    assert seen["final_rows"] == ["reports/keep.txt"]
    assert seen["global_rules"] == ["existing.tmp", "reports/drop"]
    assert seen["profile_rules"] == []


def test_size_filter_scope_and_sort_change_display_but_not_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestSizeReviewControls")
    changes = [
        Change("small/tiny.txt", Action.COPY_LEFT_TO_RIGHT, "new", 10),
        Change("small/big.txt", Action.COPY_LEFT_TO_RIGHT, "new", 2000),
        Change("other/mid.txt", Action.COPY_LEFT_TO_RIGHT, "new", 1500),
        Change("other/tiny.txt", Action.DELETE_RIGHT, "extra", 50),
    ]
    profile = Profile(name="size review", left="/sample/left", right="/sample/right", mode=SyncMode.EXACT_MIRROR)
    save_profile(profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult(changes), None))
            original = [(row.change.rel, row.change.action, row.change.size) for row in preview.rows]
            preview.size_amount.set_text("1")
            preview.size_units.set_selected(1)
            preview.size_apply.set_selected(2)
            seen["all_level_files"] = sorted(preview._file_iters)
            preview._set_size_scope(None, "small")
            seen["scoped_files"] = sorted(preview._file_iters)
            preview.sort_results.set_selected(1)
            seen["largest_first"] = preview.tree_store.get_value(preview.tree_store.get_iter_first(), 0).split()[0]
            preview.sort_results.set_selected(2)
            seen["smallest_first"] = preview.tree_store.get_value(preview.tree_store.get_iter_first(), 0).split()[0]
            seen["plan_after_review"] = [(row.change.rel, row.change.action, row.change.size) for row in preview.rows]
            seen["before"] = original
            seen["shown"] = preview.shown.get_text()
            seen["review_hint"] = preview.size_hint.get_text()
            seen["minimum_before_invalid"] = preview.review_min_size
            preview.size_amount.insert_text("x", -1)
            seen["invalid_text"] = preview.size_amount.get_text()
            seen["invalid_preserved_minimum"] = preview.review_min_size
            seen["invalid_hint"] = preview.size_hint.get_text()
            preview.size_amount.set_text("0")
            preview._set_size_scope(None, "")
            seen["restored_files"] = sorted(preview._file_iters)
            self.quit()

    assert App().run(None) == 0
    assert seen["all_level_files"] == ["other/mid.txt", "small/big.txt"]
    assert seen["scoped_files"] == ["other/mid.txt", "other/tiny.txt", "small/big.txt"]
    assert seen["largest_first"] == "small"
    assert seen["smallest_first"] == "other"
    assert seen["plan_after_review"] == seen["before"]
    assert "minimum 1 KiB" in seen["shown"]
    assert "Display only" in seen["review_hint"] and "Start" in seen["review_hint"]
    assert seen["invalid_preserved_minimum"] == 1024, seen
    assert "non-negative number" in seen["invalid_hint"]
    assert seen["restored_files"] == ["other/mid.txt", "other/tiny.txt", "small/big.txt", "small/tiny.txt"]

def test_live_folder_size_filter_recalculates_aggregates_without_changing_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestLiveSizeFolders")
    save_profile(Profile(name="live sizes", left="/sample/left", right="/sample/right"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            profile = owner.profiles[0]
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            preview.size_amount.set_text("1")
            preview.size_units.set_selected(1)
            preview.size_apply.set_selected(1)
            preview.sort_results.set_selected(1)
            preview._queue_entry("Left", "tiny/small.bin", FileStat(512, 1))
            preview._drain_pending()
            seen["small_folder_hidden"] = "tiny" not in preview._folder_iters
            preview._queue_entry("Left", "big/large.bin", FileStat(2048, 1))
            preview._drain_pending()
            seen["large_folder_size"] = preview.tree_store.get_value(preview._folder_iters["big"], 1)
            seen["large_folder_first"] = preview.tree_store.get_value(preview.tree_store.get_iter_first(), 0).split()[0]
            preview._queue_entry("Left", "tiny/second.bin", FileStat(1024, 1))
            preview._drain_pending()
            seen["grown_folder_size"] = preview.tree_store.get_value(preview._folder_iters["tiny"], 1)
            seen["display_plan"] = {row.change.rel: row.change.size for row in preview.rows}
            result = ScanResult([
                Change("tiny/small.bin", Action.COPY_LEFT_TO_RIGHT, "new", 512),
                Change("tiny/second.bin", Action.COPY_LEFT_TO_RIGHT, "new", 1024),
                Change("big/large.bin", Action.COPY_LEFT_TO_RIGHT, "new", 2048),
            ])
            preview._finish_scan(JobResult(profile, result, None))
            seen["final_plan"] = [row.change.rel for row in preview.rows]
            seen["final_sorted_root"] = preview.tree_store.get_value(preview.tree_store.get_iter_first(), 0).split()[0]
            progress.allow_close()
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["small_folder_hidden"] is True
    assert seen["large_folder_size"] == "2 KB"
    assert seen["large_folder_first"] == "big"
    assert seen["grown_folder_size"] == "1.5 KB"
    assert seen["display_plan"] == {"tiny/small.bin": 512, "tiny/second.bin": 1024, "big/large.bin": 2048}
    assert seen["final_plan"] == ["tiny/small.bin", "tiny/second.bin", "big/large.bin"]
    assert seen["final_sorted_root"] == "big"


def test_large_size_sort_still_uses_batched_tree_and_preserves_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestLargeSizeReview")
    changes = [
        Change(f"group-{index // 50:02d}/file-{index:04d}.bin", Action.COPY_LEFT_TO_RIGHT, "new", index + 1)
        for index in range(1200)
    ]
    profile = Profile(name="large size review", left="/sample/left", right="/sample/right")
    save_profile(profile)
    seen = {}

    def settle():
        ticks = 0
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
            ticks += 1
        return ticks

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult(changes), None))
            preview.size_amount.set_text("1000")
            preview.size_units.set_selected(0)
            preview.sort_results.set_selected(1)
            seen["ticks"] = settle()
            seen["rows"] = len(preview.rows)
            seen["planned"] = len(preview._planned())
            seen["visible"] = len(preview._file_iters)
            seen["first_folder"] = preview.tree_store.get_value(preview.tree_store.get_iter_first(), 0).split()[0]
            seen["minimum_visible"] = min(row.change.size for row in preview.rows if row.change.rel in preview._file_iters)
            self.quit()

    assert App().run(None) == 0
    assert seen["ticks"] >= 5
    assert seen["rows"] == 1200
    assert seen["planned"] == 1200
    assert seen["visible"] == 201
    assert seen["first_folder"] == "group-23"
    assert seen["minimum_visible"] == 1000


def test_partial_scan_allows_exclusions_and_rescan_but_never_start(tmp_path, monkeypatch):
    from ofs.apply import RunControl

    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "cache").mkdir(parents=True)
    right.mkdir()
    (left / "cache" / "first.bin").write_text("one")
    (left / "cache" / "second.bin").write_text("two")
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestPartialResults")
    profile = Profile(name="partial job", left=str(left), right=str(right))
    control = RunControl()

    def skip_first(side: str, rel: str, count: int) -> None:
        if side == "Left" and count == 0:
            time.sleep(0.11)
        if side == "Left" and count == 1:
            control.skip_current()

    result = scan_profile(profile, control=control, on_progress=skip_first)
    assert result.partial_sides == ("Left",)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            preview = window._preview(JobResult(profile, result, None))
            seen["title"] = preview.get_title()
            seen["button"] = preview.start_button.get_label()
            seen["power_disabled"] = not preview.completion_action.get_sensitive()
            seen["listed"] = len(preview.rows)
            preview._start()
            preview._begin(preview._planned(), "test partial guard")
            seen["blocked"] = not preview._running and "complete comparison" in preview.detail.get_text()
            preview._save_exclusion("cache")
            seen["saved"] = load_profiles()[0].exclusions
            seen["remaining"] = len(preview.rows)
            window._confirm_new_folder = lambda *_args: seen.setdefault("copy_requested", True)
            preview._confirm_tree_folder_copy("cache")
            seen["guarded_copy"] = "copy_requested" not in seen
            preview._popup_folder("cache", 0, 0)
            seen["folder_actions"] = [button.get_label() for button in _buttons(preview._popover)]
            seen["uniform_menu_font"] = all("monospace" not in button.get_css_classes() for button in _buttons(preview._popover))
            seen["rescan"] = None
            window._run_names = lambda names: seen.update(rescan=names)
            preview.start_button.emit("clicked")
            self.quit()

    assert App().run(None) == 0
    assert "Partial scan" in seen["title"] and "review only" in seen["title"]
    assert seen["button"] == "Rescan"
    assert seen["power_disabled"] is True
    assert seen["listed"] == 1
    assert seen["blocked"] is True
    assert seen["saved"] == ["cache"]
    assert seen["remaining"] == 0
    assert seen["guarded_copy"] is True
    assert "Copy whole new folder…" not in seen["folder_actions"]
    assert "Add exclusion…" in seen["folder_actions"]
    assert "Filter sizes under this folder" in seen["folder_actions"]
    assert seen["uniform_menu_font"] is True
    assert seen["rescan"] == {"partial job"}


def test_folder_row_shows_size_and_can_be_ignored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "2019").mkdir(parents=True)
    right.mkdir()
    (left / "2019" / "song.mp3").write_text("new")
    (left / "keep.txt").write_text("stay")
    os.utime(left / "2019" / "song.mp3", (50, 50))
    os.utime(left / "keep.txt", (50, 50))
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFolder")
    save_profile(Profile(name="alpha notes", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            profile = window.profiles[0]
            preview = window._preview(JobResult(profile, scan_profile(profile), None))
            folder = preview.tree_store.get_iter_first()
            seen["label"] = preview.tree_store.get_value(folder, 0)
            seen["size"] = preview.tree_store.get_value(folder, 1)
            planned = [change.rel for change in preview._planned()]
            seen["initially_expanded"] = preview.view.row_expanded(Gtk.TreePath.new_first())
            preview.collapse_all_button.emit("clicked")
            seen["collapsed"] = not preview.view.row_expanded(Gtk.TreePath.new_first())
            preview.expand_all_button.emit("clicked")
            seen["expanded"] = preview.view.row_expanded(Gtk.TreePath.new_first())
            seen["plan_unchanged"] = planned == [change.rel for change in preview._planned()]
            preview._ignore_folder("2019")
            seen["left"] = [item.change.rel for item in preview.rows]
            seen["session"] = list(window.session_masks["alpha notes"])
            seen["saved"] = list(load_profiles()[0].exclusions)
            preview._save_exclusion("2019")
            seen["saved_after"] = list(load_profiles()[0].exclusions)
            self.quit()

    assert App().run(None) == 0
    assert seen["label"].startswith("2019")
    assert seen["size"]
    assert seen["size"] in seen["label"]
    assert seen["initially_expanded"] is True
    assert seen["collapsed"] is True
    assert seen["expanded"] is True
    assert seen["plan_unchanged"] is True
    assert seen["left"] == ["keep.txt"]
    assert seen["session"] == ["2019"]
    assert seen["saved"] == []
    assert seen["saved_after"] == ["2019"]


def test_folder_direction_points_the_files_under_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "2019" / "nested").mkdir(parents=True)
    right.mkdir()
    (left / "2019" / "song.mp3").write_text("new")
    (left / "2019" / "nested" / "more.mp3").write_text("more")
    (left / "keep.txt").write_text("stay")
    for path in (left / "2019" / "song.mp3", left / "2019" / "nested" / "more.mp3", left / "keep.txt"):
        os.utime(path, (50, 50))
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFolderDirection")
    save_profile(Profile(name="alpha notes", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            profile = window.profiles[0]
            preview = window._preview(JobResult(profile, scan_profile(profile), None))
            folder = preview.tree_store.get_iter_first()
            seen["arrow"] = preview.tree_store.get_value(folder, 2)

            def check() -> bool:
                seen["tries"] = seen.get("tries", 0) + 1
                view = preview.view
                path = Gtk.TreePath.new_first()
                area = view.get_cell_area(path, view.get_column(0))
                if area.height <= 0 and seen["tries"] < 40:
                    return True
                wx, wy = view.convert_bin_window_to_widget_coords(area.x + 8, area.y + area.height // 2)
                seen["rel"] = preview._rel_at(wx, wy)
                preview._set_folder_direction(seen["rel"], "left")
                seen["actions"] = {item.change.rel: item.change.action for item in preview.rows}
                preview._ignore_folder(seen["rel"])
                seen["left"] = [item.change.rel for item in preview.rows]
                seen["note"] = preview.detail.get_text()
                self.quit()
                return False

            GLib.timeout_add(50, check)

    assert App().run(None) == 0
    assert seen["rel"] == "2019"
    assert seen["arrow"].startswith("L ──▶ R")
    assert seen["actions"]["2019/song.mp3"] == Action.COPY_RIGHT_TO_LEFT
    assert seen["actions"]["2019/nested/more.mp3"] == Action.COPY_RIGHT_TO_LEFT
    assert seen["actions"]["keep.txt"] == Action.COPY_LEFT_TO_RIGHT
    assert seen["left"] == ["keep.txt"]
    assert seen["note"] == "Ignored 2 files under 2019 for this run."


def test_row_run_scans_that_profile_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    changed = tmp_path / "changed"
    other = tmp_path / "other"
    right = tmp_path / "right"
    changed.mkdir()
    other.mkdir()
    right.mkdir()
    (changed / "notes.txt").write_text("new")
    (right / "notes.txt").write_text("old")
    os.utime(changed / "notes.txt", (50, 50))
    os.utime(right / "notes.txt", (10, 10))
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestRowRun")
    save_profile(Profile(name="alpha notes", left=str(changed), right=str(right), mode=SyncMode.EXACT_MIRROR))
    save_profile(Profile(name="beta quiet", left=str(other), right=str(other)))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["runs"] = _button_labels(window).count("Run")
            seen["title"] = window.get_title()
            seen["run_class"] = "run" in window.run_button.get_css_classes() and _run_buttons_are_marked(window)
            seen["run_icon"] = next(item.get_icon_name() for item in _widgets(window.run_button) if isinstance(item, Gtk.Image))
            seen["settings_icon"] = next(button.get_icon_name() for button in _buttons(window.get_titlebar()) if button.get_icon_name() == "emblem-system-symbolic")
            window._run_one(None, "beta quiet")
            ticks = {"n": 0}

            def watch() -> bool:
                ticks["n"] += 1
                text = window.status.get_text()
                if text.startswith("Scanned") or ticks["n"] > 150:
                    seen["status"] = text
                    self.quit()
                    return False
                return True

            GLib.timeout_add(20, watch)

    assert App().run(None) == 0
    assert seen["runs"] == 2
    assert seen["title"] == "OFS"
    assert seen["run_class"]
    assert seen["run_icon"] == "media-playback-start-symbolic"
    assert seen["settings_icon"] == "emblem-system-symbolic"
    assert seen["status"].startswith("Scanned 1.")
    assert "0 files, 0 B to copy" in seen["status"]


def test_exclusion_edit_preserves_individual_folder_collapse_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestTreeState")
    changes = [Change(path, Action.COPY_LEFT_TO_RIGHT, "only on source", 1) for path in ("folder-one/a.txt", "folder-one/b.txt", "folder-two/c.txt")]
    profile = Profile(name="tree state", left="/sample/left", right="/sample/right")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult(changes), None))
            preview.view.collapse_row(Gtk.TreePath.new_from_string("0"))
            seen["collapsed_before"] = not preview.view.row_expanded(Gtk.TreePath.new_from_string("0"))
            preview._save_exclusion("folder-one/a.txt")
            seen["collapsed_after"] = not preview.view.row_expanded(Gtk.TreePath.new_from_string("0"))
            seen["other_expanded"] = preview.view.row_expanded(Gtk.TreePath.new_from_string("1"))
            seen["remaining"] = [item.change.rel for item in preview.rows]
            preview.collapse_all_button.emit("clicked")
            preview._drop("folder-one/b.txt")
            seen["all_collapsed"] = not preview.view.row_expanded(Gtk.TreePath.new_from_string("0"))
            self.quit()

    assert App().run(None) == 0
    assert seen["collapsed_before"] is True
    assert seen["collapsed_after"] is True
    assert seen["other_expanded"] is True
    assert seen["remaining"] == ["folder-one/b.txt", "folder-two/c.txt"]
    assert seen["all_collapsed"] is True


def test_large_live_refill_does_not_mix_arriving_rows_with_replacement_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestLiveRefreshRace")
    save_profile(Profile(name="alpha", left="/sample/left", right="/sample/right"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            profile = window.profiles[0]
            preview = window._preview(JobResult(profile, ScanResult([]), None))
            progress = ScanWindow(window)
            preview._begin_scan(progress)
            for index in range(1510):
                folder = "excluded" if index % 15 == 0 else f"group-{index // 50}"
                preview._queue_entry("Left", f"{folder}/item-{index}", FileStat(index + 1, 2))
            while preview._entry_queue:
                preview._drain_pending()
            preview._save_exclusion("excluded")
            for index in range(30):
                preview._queue_entry("Right", f"incoming/after-{index}", FileStat(index + 3, 3))
            preview._drain_pending()

            def watch():
                if preview._pending_tree or preview._replace_tree_model or preview._entry_queue:
                    return True
                seen["rows"] = len(preview.rows)
                seen["store"] = preview.store.get_n_items()
                seen["iterators"] = len(preview._file_iters)
                seen["expected_paths"] = preview._file_iters.get("incoming/after-29") is not None
                seen["no_excluded"] = all(not row.change.rel.startswith("excluded/") for row in preview.rows)
                preview._finish_scan(JobResult(profile, ScanResult([]), None))
                progress.allow_close()
                preview.close()
                self.quit()
                return False

            GLib.timeout_add(5, watch)

    assert App().run(None) == 0
    assert seen["rows"] == len([index for index in range(1510) if index % 15 != 0]) + 30
    assert seen["store"] == seen["rows"] == seen["iterators"], seen
    assert seen["expected_paths"] is True and seen["no_excluded"] is True


def test_large_tree_updates_batches_and_retains_manual_collapse(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestLargeTreeRefresh")
    count = 1200
    changes = [
        Change(f"folder-{index // 400:03d}/nested/file-{index}.jpg", Action.COPY_LEFT_TO_RIGHT, "only on source", 1)
        for index in range(count)
    ]
    profile = Profile(name="large tree", left="/sample/left", right="/sample/right")
    seen = {"ticks": 0}
    stage = {"value": 0}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult(changes), None))
            preview.present()
            seen["queued"] = bool(preview._pending_tree)

            def watch() -> bool:
                seen["ticks"] += 1
                if preview._pending_tree:
                    seen["saw_batch"] = True
                    return True
                if stage["value"] == 0 and preview.store.get_n_items() == count:
                    seen["expanded_initially"] = preview.view.row_expanded(Gtk.TreePath.new_from_string("0"))
                    preview.view.collapse_row(Gtk.TreePath.new_from_string("0"))
                    preview._save_exclusion("folder-000/nested/file-0.jpg")
                    stage["value"] = 1
                    return True
                if stage["value"] == 1 and preview.store.get_n_items() == count - 1:
                    seen["collapsed_after"] = not preview.view.row_expanded(Gtk.TreePath.new_from_string("0"))
                    self.quit()
                    return False
                return True

            GLib.timeout_add(5, watch)

    assert App().run(None) == 0
    assert seen["queued"] is True
    assert seen["saw_batch"] is True
    assert seen["ticks"] > 1
    assert seen["expanded_initially"] is True
    assert seen["collapsed_after"] is True


def test_folder_delete_requires_scope_review_and_normal_deletion_safeguards(tmp_path, monkeypatch):
    from ofs.review import Listed, deletion_violations, safety_text

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFolderDeleteReview")
    profile = Profile(
        name="folder delete review", left="/sample/left", right="/sample/right",
        mode=SyncMode.EXACT_MIRROR, never_delete_files=True,
    )
    rows = [
        Listed(Change("data/keep.txt", Action.UNCHANGED, "same", 100), True, True),
        Listed(Change("data/copy.txt", Action.COPY_LEFT_TO_RIGHT, "new", 700), True, False),
        Listed(Change("data/conflict.tmp", Action.ASK, "size conflict", 600), True, True),
        Listed(Change("data/out-of-scope.tmp", Action.SKIPPED, "profile filter", 50), True, True),
        Listed(Change("outside/keep.txt", Action.UNCHANGED, "same", 20), True, True),
    ]
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult([item.change for item in rows], listing=rows), None))
            original = [(row.change.rel, row.change.action) for row in preview.rows]
            dialog = preview._ask_folder_delete("data", "right")
            labels = [item.get_text() for item in _widgets(dialog) if isinstance(item, Gtk.Label)]
            seen["confirmation"] = "\n".join(labels)
            next(button for button in _buttons(dialog) if button.get_label() == "Cancel").emit("clicked")
            seen["cancel_preserved"] = [(row.change.rel, row.change.action) for row in preview.rows] == original

            dialog = preview._ask_folder_delete("data", "right")
            dialog.folder_delete_confirm_button.emit("clicked")
            planned = {row.change.rel: row.change.action for row in preview.rows}
            seen["right_deletes"] = sorted(rel for rel, action in planned.items() if action == Action.DELETE_RIGHT)
            seen["unrelated_action"] = planned["outside/keep.txt"]
            seen["skipped_action"] = planned["data/out-of-scope.tmp"]
            seen["other_side_copy"] = planned["data/copy.txt"]

            seen["warning"] = safety_text(profile, preview.rows)
            original_safety = preview._safety

            def retain_safety(*args, **kwargs):
                seen["safety_window"] = original_safety(*args, **kwargs)
                return seen["safety_window"]

            preview._safety = retain_safety
            preview._start()
            safety = seen.get("safety_window")
            if safety is None:
                self.quit()
                return
            confirm = next(button for button in _buttons(safety) if button.get_label() == "Confirm Deletions")
            seen["guarded_confirmation"] = not confirm.get_sensitive()
            seen["not_started"] = not preview._running
            next(button for button in _buttons(safety) if button.get_label() == "Do Not Continue").emit("clicked")

            dialog = preview._ask_folder_delete("data", "left")
            dialog.folder_delete_confirm_button.emit("clicked")
            seen["left_deletes"] = sorted(
                row.change.rel for row in preview.rows if row.change.action == Action.DELETE_LEFT
            )
            self.quit()

    assert App().run(None) == 0
    assert "2 in-scope files (700 B)" in seen["confirmation"]
    assert "under data on Right" in seen["confirmation"]
    assert "Nothing is deleted now" in seen["confirmation"]
    assert seen["cancel_preserved"] is True
    assert seen["right_deletes"] == ["data/conflict.tmp", "data/keep.txt"]
    assert seen["unrelated_action"] == Action.UNCHANGED
    assert seen["skipped_action"] == Action.SKIPPED
    assert seen["other_side_copy"] == Action.COPY_LEFT_TO_RIGHT
    assert seen["guarded_confirmation"] is True
    assert seen["not_started"] is True
    assert seen["left_deletes"] == ["data/conflict.tmp", "data/copy.txt", "data/keep.txt"]


def test_row_actions_and_profile_delete(tmp_path, monkeypatch):
    home = tmp_path / "home"
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "sub").mkdir(parents=True)
    right.mkdir()
    (left / "notes.txt").write_text("new")
    (right / "notes.txt").write_text("old")
    (left / "sub" / "clip.txt").write_text("only")
    os.utime(left / "notes.txt", (50, 50))
    os.utime(right / "notes.txt", (10, 10))
    os.utime(left / "sub" / "clip.txt", (10, 10))
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestRows")
    save_profile(Profile(name="alpha notes", left=str(left), right=str(right), mode=SyncMode.EXACT_MIRROR))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window._duplicate(None, "alpha notes")
            profile = next(item for item in window.profiles if item.name == "alpha notes")
            result = scan_profile(profile)
            preview = window._preview(JobResult(profile, result, None))
            notes = next(preview.store.get_item(index) for index in range(preview.store.get_n_items()) if preview.store.get_item(index).change.rel == "notes.txt")
            preview._set_action(notes, Action.UNCHANGED)
            seen["hidden"] = [change.rel for change in preview._planned()]
            preview._set_action(notes, None)
            restored = next(change for change in preview._planned() if change.rel == "notes.txt")
            seen["restored"] = restored.action
            clip = next(item for item in preview.rows if item.change.rel == "sub/clip.txt")
            preview._compare(type("Row", (), {"change": clip.change})())
            seen["compare"] = preview.detail.get_text()
            preview._exclude_folder(type("Row", (), {"change": clip.change})())
            seen["folder_mask"] = list(profile.exclusions)
            seen["folder_gone"] = [item.change.rel for item in preview.rows]
            window._remove_profile("alpha notes copy")
            seen["names"] = [item.name for item in load_profiles()]
            self.quit()

    assert App().run(None) == 0
    assert "notes.txt" not in seen["hidden"]
    assert "sub/clip.txt" in seen["hidden"]
    assert seen["restored"] == Action.COPY_LEFT_TO_RIGHT
    assert seen["folder_mask"] == ["sub"]
    assert "sub/clip.txt" not in seen["folder_gone"]
    assert seen["compare"] == "Compare needs the file on both sides."
    assert seen["names"] == ["alpha notes"]


def test_profile_menu_path_options_and_batch_rename_previews(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestProfileReplacements")
    first = Profile(name="alpha one", left="/mnt/old/photos", right="/mnt/old-archive/photos")
    second = Profile(name="alpha two", left="/local", right="/mnt/old/photos")
    save_profile(first)
    save_profile(second)
    seen = {}

    class Dialog:
        def __init__(self, choice):
            self.choice = choice

        def choose_finish(self, _result):
            return self.choice

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            seen["toolbar"] = _button_labels(window.get_titlebar())
            seen["menu"] = _button_labels(window.profiles_menu.get_popover())
            window.checked = {first.name, second.name}
            paths = window._replace_paths()
            paths.scope.set_selected(1)
            paths.prefix.set_active(True)
            paths.find.set_text("/mnt/old")
            paths.replace.set_text("/mnt/new")
            preview = "\n".join(item.get_text() for item in _widgets(paths.items) if isinstance(item, Gtk.Label))
            seen["left_preview"] = preview
            seen["left_preview_count"] = paths.summary.get_text()
            seen["left_before_review"] = [(item.left, item.right) for item in load_profiles()]
            window._confirm_path_replacements = lambda pending: seen.update(pending=pending)
            paths.review.emit("clicked")
            seen["left_after_review"] = [(item.left, item.right) for item in load_profiles()]
            paths.scope.set_selected(2)
            paths.review.emit("clicked")
            seen["right_pending"] = seen["pending"]
            window._path_replace_chosen(Dialog(1), None, seen["pending"])
            seen["path_after_save"] = [(item.left, item.right) for item in load_profiles()]
            rename = window._rename_checked()
            rename.prefix.set_active(True)
            rename.find.set_text("alpha")
            rename.replace.set_text("photos")
            window._confirm_name_replacements = lambda changes: seen.update(names=changes)
            rename.review.emit("clicked")
            seen["before_cancel"] = [item.name for item in load_profiles()]
            window._name_replace_chosen(Dialog(0), None, seen["names"])
            seen["after_cancel"] = [item.name for item in load_profiles()]
            window._name_replace_chosen(Dialog(1), None, seen["names"])
            seen["after_rename"] = [item.name for item in load_profiles()]
            seen["checked"] = set(window.checked)
            self.quit()

    assert App().run(None) == 0
    assert seen["toolbar"][0] == "New profile"
    assert "Import 5.71 profiles…" in seen["menu"]
    assert "Import OFS profiles…" in seen["menu"]
    assert "Export checked profiles…" in seen["menu"]
    assert "Multi Edit checked profiles…" in seen["menu"]
    assert "Replace paths in checked profiles…" in seen["menu"]
    assert "Rename checked profiles…" in seen["menu"]
    assert "/mnt/old/photos\n→ /mnt/new/photos" in seen["left_preview"]
    assert "old-archive" not in seen["left_preview"]
    assert "1 of 2" in seen["left_preview_count"]
    assert seen["left_before_review"] == seen["left_after_review"]
    assert [item.name for item, _updates in seen["right_pending"]] == ["alpha two"]
    assert seen["right_pending"][0][1] == {"right": "/mnt/new/photos"}
    assert seen["path_after_save"] == [("/mnt/old/photos", "/mnt/old-archive/photos"), ("/local", "/mnt/new/photos")]
    assert seen["names"] == [("alpha one", "photos one"), ("alpha two", "photos two")]
    assert seen["before_cancel"] == seen["after_cancel"]
    assert seen["after_rename"] == ["photos one", "photos two"]
    assert seen["checked"] == {"photos one", "photos two"}


def test_multi_edit_cache_size_and_fractional_age_controls(tmp_path, monkeypatch):
    from ofs.bulk import FIELDS

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestExpandedBulk")
    save_profile(Profile(name="alpha one", left="/sample/left", right="/sample/right", imported_settings=True))
    save_profile(Profile(name="alpha two", left="/other/left", right="/other/right", cache_destination=True))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window.checked = {"alpha one", "alpha two"}
            editor = window._multi_edit()
            window._confirm_bulk_edit = lambda changed, field, _editor: seen.update(changed=changed, field=field)

            seen["searchable_fields"] = editor.field.get_enable_search()
            seen["cache_hint"] = "cache" in editor.preview.get_text().lower()
            search = getattr(editor, "field_search", None)
            seen["visible_search"] = search is not None
            if search is None:
                self.quit()
                return
            search.set_text("cache")
            seen["search_results"] = ["cache" in item.label.lower() for item in editor._visible_fields]
            seen["cache_options"] = [label for label, _value in editor._field_definition().options]
            editor.value_widget.set_selected(1)
            editor.apply.emit("clicked")
            seen["cache"] = seen["changed"]
            seen["cache_field"] = seen["field"]
            seen["before"] = [(profile.name, profile.cache_destination) for profile in load_profiles()]
            search.set_text("no-such-setting")
            seen["no_matches"] = editor.value_widget.get_text() == "No matching settings" and not editor.apply.get_sensitive()
            search.set_text("")
            seen["all_fields_restored"] = len(editor._visible_fields) == len(FIELDS)

            editor.field.set_selected(next(index for index, item in enumerate(FIELDS) if item.key == "min_age_days"))
            seen["age_digits"] = editor.value_widget.get_digits()
            editor.value_widget.set_value(0.25)
            editor.apply.emit("clicked")
            seen["age"] = seen["changed"]

            editor.field.set_selected(next(index for index, item in enumerate(FIELDS) if item.key == "min_size"))
            seen["size_placeholder"] = editor.value_widget.get_placeholder_text()
            editor.value_widget.set_text("1.5 KB")
            editor.apply.emit("clicked")
            seen["size"] = seen["changed"]
            editor.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["searchable_fields"] is True and seen["cache_hint"] is True
    assert seen["visible_search"] is True
    assert seen["search_results"] == [True]
    assert seen["cache_options"] == ["Off", "On"]
    assert seen["cache_field"] == "cache_destination"
    assert seen["no_matches"] is True and seen["all_fields_restored"] is True
    assert [profile.name for profile in seen["cache"]] == ["alpha one"]
    assert seen["cache"][0].imported_settings is True
    assert seen["before"] == [("alpha one", False), ("alpha two", True)]
    assert seen["age_digits"] == 2
    assert all(profile.min_age_days == 0.25 for profile in seen["age"])
    assert "1.5 KB" in seen["size_placeholder"]
    assert all(profile.min_size == 1536 for profile in seen["size"])


def test_normal_activate_restores_main_window_instead_of_blank_scan_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestActivateMainWindow")
    save_profile(Profile(name="activation", left="/sample/left", right="/sample/right"))
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            owner.present()
            preview = owner._preview(JobResult(owner.profiles[0], ScanResult([]), None))
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            # Reproduce a detached old-style progress top-level left active.
            progress.present()
            settle()
            seen["before"] = isinstance(self.props.active_window, ScanWindow)
            owner.close()
            settle()
            super().do_activate()
            settle()
            seen["main_active"] = isinstance(self.props.active_window, MainWindow)
            seen["main_visible"] = self.props.active_window.get_visible()
            seen["blank_hidden"] = not progress.get_visible()
            progress.allow_close()
            preview._finish_scan(JobResult(owner.profiles[0], ScanResult([]), None))
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["before"] is True
    assert seen["main_active"] is True
    assert seen["main_visible"] is True
    assert seen["blank_hidden"] is True


def test_new_windows_share_profile_locks_but_keep_independent_state(tmp_path, monkeypatch):
    from ofs.scan import ScanResult

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestMultipleWindows")
    for name in ("alpha", "beta"):
        save_profile(Profile(name=name, left=f"/sample/{name}", right=f"/backup/{name}"))
    seen = {}

    class CommandLine:
        def __init__(self, arguments):
            self.arguments = arguments
            self.errors = []

        def get_arguments(self):
            return self.arguments

        def printerr(self, text):
            self.errors.append(text)

    class App(SfsApplication):
        def do_activate(self):
            first = MainWindow(self)
            first.checked = {"alpha"}
            second = first._new_window()
            second.checked = {"beta"}
            seen["first_checked"] = set(first.checked)
            seen["second_checked"] = set(second.checked)
            seen["same_profiles"] = [profile.name for profile in second.profiles]
            seen["main_window_count"] = len([window for window in self.get_windows() if isinstance(window, MainWindow)])
            seen["menu_has_new_window"] = "New window" in _button_labels(first.profiles_menu.get_popover())

            assert self.claim_profile("alpha") is True
            assert self.claim_profile("beta") is True
            second._run_names({"alpha"})
            seen["duplicate_status"] = second.status.get_text()
            seen["scan_claimed_beta"] = self.profile_is_active("beta")
            seen["profile_claim_blocks_copy"] = self.claim_profile("alpha") is False
            job = JobResult(next(profile for profile in second.profiles if profile.name == "alpha"), ScanResult(changes=[]), None)
            preview = second._preview(job)
            preview._begin([], "cross-window test")
            seen["copy_blocked"] = not preview._running and "another OFS window" in preview.detail.get_text()
            preview.close()
            self.release_profile("alpha")
            seen["release_allows_other_window"] = self.claim_profile("alpha") is True
            self.release_profile("alpha")
            self.release_profile("beta")

            command = CommandLine(["ofs", "--new-window"])
            seen["command_status"] = self.do_command_line(command)
            seen["command_errors"] = command.errors
            seen["command_window_count"] = len([window for window in self.get_windows() if isinstance(window, MainWindow)])
            self.quit()

    assert App().run(None) == 0
    assert seen["first_checked"] == {"alpha"}
    assert seen["second_checked"] == {"beta"}
    assert seen["same_profiles"] == ["alpha", "beta"]
    assert seen["main_window_count"] == 2
    assert seen["menu_has_new_window"] is True
    assert "already scanning or active" in seen["duplicate_status"]
    assert seen["scan_claimed_beta"] is True
    assert seen["profile_claim_blocks_copy"] is True
    assert seen["copy_blocked"] is True
    assert seen["release_allows_other_window"] is True
    assert seen["command_status"] == 0 and seen["command_errors"] == []
    assert seen["command_window_count"] == 3


def test_closing_main_window_keeps_scan_and_results_alive(tmp_path, monkeypatch):
    import threading
    import ofs.ui as ui
    from ofs.scan import ScanResult

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestClosedOverview")
    save_profile(Profile(name="alpha", left="/sample/left", right="/sample/right"))
    started = threading.Event()
    release = threading.Event()
    seen = {}

    def controlled_scan(profiles, **_kwargs):
        started.set()
        assert release.wait(5)
        return [JobResult(profiles[0], ScanResult(changes=[]), None)]

    monkeypatch.setattr(ui, "run_checked", controlled_scan)

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window.present()
            window._run_names({"alpha"})
            scan = next(item for item in self.get_windows() if isinstance(item, ScanWindow))
            preview = window._scan_previews["alpha"]
            window.close()
            seen["overview_closed"] = not window.get_visible()
            seen["scan_has_application"] = scan.get_application() is self
            seen["scan_survives"] = preview.get_visible() and preview._scan_active and preview.scan_details.get_expanded()
            seen["claimed_while_closed"] = self.profile_is_active("alpha")
            release.set()
            GLib.timeout_add(20, self.watch)

        def watch(self):
            preview = next((item for item in self.get_windows() if isinstance(item, Preview)), None)
            if preview is None or not preview._scan_complete:
                return True
            seen["preview_has_application"] = preview.get_application() is self
            seen["preview_visible"] = preview.get_visible()
            seen["released_after_scan"] = not self.profile_is_active("alpha")
            self.quit()
            return False

    assert App().run(None) == 0
    assert started.is_set()
    assert seen == {
        "overview_closed": True,
        "scan_has_application": True,
        "scan_survives": True,
        "claimed_while_closed": True,
        "preview_has_application": True,
        "preview_visible": True,
        "released_after_scan": True,
    }


def test_path_replace_saves_and_drops_the_file_database(tmp_path, monkeypatch):
    from ofs.compare import FileStat
    from ofs.history import database_path, save_history
    from ofs.store import replace_in_paths

    home = tmp_path / "home"
    monkeypatch.setenv("OFS_HOME", str(home))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestPaths")
    profile = Profile(name="alpha one", left="/media/old/photos", right="/backup")
    save_profile(profile)
    save_history(profile, {"a.txt": FileStat(1, 1)}, {})
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            current = window.profiles[0]
            replace_in_paths(current, "/media/old", "/media/new")
            window._save_replaced_paths([current])
            seen["left"] = next(item.left for item in load_profiles() if item.name == "alpha one")
            seen["database"] = database_path(profile).exists()
            self.quit()

    assert App().run(None) == 0
    assert seen["left"] == "/media/new/photos"
    assert seen["database"] is False


def test_global_history_searches_refreshes_and_opens_only_saved_logs(tmp_path, monkeypatch):
    from ofs.apply import ApplyReport
    from ofs.review import log_body, write_log
    from ofs.run_history import record
    import ofs.ui as ui

    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestGlobalHistory")
    profile = Profile(name="alpha docs", left="/sample/left", right="/sample/right")
    report = ApplyReport(copied=1)
    path = write_log(profile, log_body(profile, [], report, choice="test record only"))
    record("alpha docs", "copy", "Copied 1 file", path)
    record("beta media", "scan", "Complete scan: no changes")
    record("offline disk", "scan", "Volume unavailable")
    seen = {}

    def settle():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            owner.checked = {"alpha docs"}
            owner._focused_profile = "alpha docs"
            owner.reload()
            history = owner._run_history()
            settle()
            seen["initial"] = [(entry.profile, entry.kind) for entry in history._filtered]
            seen["no_log_disabled"] = not history.view_log.get_sensitive()
            volume = next(widget for widget in _widgets(history.list_box) if isinstance(widget, Gtk.Label) and widget.get_text() == "Volume unavailable")
            seen["volume_tone"] = "result-error" in volume.get_css_classes()

            history.search.set_text("alpha")
            history.search.emit("search-changed")
            seen["filtered"] = [entry.profile for entry in history._filtered]
            seen["log_enabled"] = history.view_log.get_sensitive()
            summary = next(widget for widget in _widgets(history.list_box) if isinstance(widget, Gtk.Label) and widget.get_text() == "Copied 1 file")
            stamp = next(widget for widget in _widgets(history.list_box) if isinstance(widget, Gtk.Label) and widget.get_text() == history._filtered[0].when)
            sample = summary.get_style_context().get_color()
            plain = stamp.get_style_context().get_color()
            seen["selected_text_colors"] = tuple(
                (color.red, color.green, color.blue, color.alpha) for color in (sample, plain)
            )
            monkeypatch.setattr(ui, "open_uri", lambda log_path: seen.setdefault("opened", log_path))
            history._open_log()

            record("gamma backups", "folder-copy", "Copied a new folder")
            history._refresh()
            seen["preserves_query"] = history.search.get_text() == "alpha"
            seen["refresh_count"] = len(history._entries)
            history.search.set_text("")
            history.search.emit("search-changed")
            seen["all_again"] = len(history._filtered)
            seen["owner_selection"] = owner._focused_profile
            seen["owner_checked"] = owner.checked
            history.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["initial"] == [("offline disk", "scan"), ("beta media", "scan"), ("alpha docs", "copy")]
    assert seen["no_log_disabled"] is True and seen["volume_tone"] is True
    assert seen["filtered"] == ["alpha docs"]
    assert seen["log_enabled"] is True
    assert seen["selected_text_colors"][0] == seen["selected_text_colors"][1]
    assert seen["opened"] == path
    assert seen["preserves_query"] is True and seen["refresh_count"] == 4
    assert seen["all_again"] == 4
    assert seen["owner_selection"] == "alpha docs"
    assert seen["owner_checked"] == {"alpha docs"}


def test_scan_eta_uses_previous_duration_then_current_pace():
    from ofs.ui import _estimate_eta

    assert _estimate_eta(0, 1.0, (0, 0.0)) is None
    assert _estimate_eta(0, 2.0, (100, 10.0)) == 8.0
    assert _estimate_eta(50, 5.0, (100, 10.0)) == 5.0
    assert _estimate_eta(150, 10.0, (100, 10.0)) == 0.0


def test_status_icon_reflects_the_last_result():
    from ofs.ui import _status_icon

    assert _status_icon("") is None
    assert _status_icon("   ") is None
    assert _status_icon("2026-11-10 12:03  0 copied, 0 deleted, 0 moved").get_icon_name() == "emblem-ok"
    assert _status_icon("2026-11-10 12:03  1 copied, 2 errors").get_icon_name() == "dialog-error"
    assert _status_icon("stopped, 3 copied").get_icon_name() == "dialog-warning"
    assert _status_icon("2026-11-10 12:03  stopped, 3 copied").get_icon_name() == "dialog-warning"


def test_result_parts_split_timestamp_and_tone():
    from ofs.ui import _result_parts

    assert _result_parts("", "Some note") == ("", "Some note", "dim-label")
    when, body, tone = _result_parts("2026-11-10 12:03  0 copied, 0 deleted, 0 moved", "")
    assert (when, body, tone) == ("2026-11-10 12:03", "0 copied, 0 deleted, 0 moved", "result-ok")
    assert _result_parts("2026-11-10 12:03  stopped, 3 copied", "") == ("2026-11-10 12:03", "stopped, 3 copied", "result-warn")
    assert _result_parts("stopped, 3 copied", "") == ("", "stopped, 3 copied", "result-warn")
    assert _result_parts("2026-11-10 12:03  1 copied, 2 errors", "")[2] == "result-error"


def test_results_close_request_also_closes_the_preview():
    from ofs.ui import ResultsWindow

    class Owner:
        closed = False

        def close(self):
            self.closed = True

    owner = Owner()
    window = ResultsWindow.__new__(ResultsWindow)
    window.owner = owner
    assert window._close_preview() is False
    assert owner.closed


def test_preview_states_the_comparison_rule_that_hides_equal_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestCompareRule")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            ignored = Profile(
                name="ignored timestamps", left="/left", right="/right",
                timestamps=TimestampRules(ignore_timestamp=True),
            )
            preview = owner._preview(JobResult(ignored, ScanResult([]), None))
            seen["rule"] = preview.rule_label.get_text()
            seen["shown"] = preview.rule_label.get_parent() is not None

            plain = Profile(name="plain", left="/left", right="/right")
            other = owner._preview(JobResult(plain, ScanResult([]), None))
            seen["plain_rule"] = other.rule_label.get_text()
            seen["plain_shown"] = other.rule_label.get_parent() is not None
            self.quit()

    assert App().run(None) == 0
    assert "Timestamps ignored" in seen["rule"] and "size matches" in seen["rule"]
    assert seen["shown"] is True
    assert seen["plain_rule"] == "" and seen["plain_shown"] is False


def test_stopped_scan_offers_rescan_and_never_stays_on_updating(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestStoppedScan")
    save_profile(Profile(name="stopped", left="/sample/left", right="/sample/right"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            profile = owner.profiles[0]
            preview = owner._preview(JobResult(profile, ScanResult([]), None))
            preview._scan_active = True
            preview._scan_complete = False
            preview.rows = [
                Listed(Change(f"bulk/{index:04d}.txt", Action.UNCHANGED, "unchanged", 10), True, True)
                for index in range(1200)
            ]
            preview._finish_scan(JobResult(profile, None, "scan stopped"))
            seen["label"] = preview.start_button.get_label()
            seen["sensitive"] = preview.start_button.get_sensitive()
            seen["shown"] = preview.shown.get_text()
            seen["detail"] = preview.detail.get_text()
            seen["pending"] = list(preview._pending_tree)
            preview._rescan = lambda *args: seen.setdefault("rescan", True)
            preview.start_button.emit("clicked")
            seen["clicked_rescan"] = seen.get("rescan", False)
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["label"] == "Rescan"
    assert seen["sensitive"] is True
    assert seen["shown"] == "Scan stopped: no comparison plan. Nothing can be copied from an incomplete scan."
    assert "cannot be applied" in seen["detail"]
    assert seen["pending"] == []
    assert seen["clicked_rescan"] is True


def test_scan_preview_is_a_normal_window_with_minimize_and_maximize(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestPreviewWindow")
    save_profile(Profile(name="window", left="/sample/left", right="/sample/right"))
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(owner.profiles[0], ScanResult([]), None))
            seen["transient"] = preview.get_transient_for()
            seen["modal"] = preview.get_modal()
            seen["resizable"] = preview.get_resizable()
            seen["deletable"] = preview.get_deletable()
            preview.close()
            self.quit()

    assert App().run(None) == 0
    # GTK only draws minimize/maximize controls on sovereign windows.
    assert seen["transient"] is None
    assert seen["modal"] is False
    assert seen["resizable"] is True
    assert seen["deletable"] is True


def test_live_discovery_does_not_rebuild_layout_every_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestLiveLayout")
    save_profile(Profile(name="live layout", left="/sample/left", right="/sample/right"))
    seen = {"expand_all": 0}

    original = Gtk.TreeView.expand_all
    monkeypatch.setattr(Gtk.TreeView, "expand_all", lambda self: seen.__setitem__("expand_all", seen["expand_all"] + 1))

    def settle(rounds=200):
        for _ in range(rounds):
            if not GLib.MainContext.default().pending():
                break
            GLib.MainContext.default().iteration(False)

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(owner.profiles[0], ScanResult([]), None))
            preview.present()
            progress = ScanWindow(owner)
            preview._begin_scan(progress)
            self.preview = preview
            self.progress = progress
            GLib.timeout_add(300, self.queue_rows)

        def queue_rows(self):
            preview = self.preview
            seen["expand_all"] = 0
            for index in range(600):
                preview._queue_entry("Left", f"folder-{index // 50:02d}/file-{index:04d}.txt", FileStat(10, 1))
            settle()
            GLib.timeout_add(300, self.check)
            return False

        def check(self):
            preview = self.preview
            seen["top"] = preview.tree_store.iter_n_children(None)
            first_iter = preview.tree_store.iter_nth_child(None, 0)
            seen["folder_expanded"] = preview.view.row_expanded(preview.tree_store.get_path(first_iter))
            self.progress.allow_close()
            preview.close()
            self.quit()
            return False

    assert App().run(None) == 0
    monkeypatch.setattr(Gtk.TreeView, "expand_all", original)
    # Expanding the whole model per 250-row batch is quadratic on huge trees.
    assert seen["expand_all"] == 0
    assert seen["top"] >= 12
    assert seen["folder_expanded"] is True


def test_reason_filter_dropdown_narrows_rows_without_changing_the_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestReasonFilter")
    profile = Profile(name="reason filter", left="/sample/left", right="/sample/right")
    left = {"big.bin": FileStat(200, 10), "small.bin": FileStat(10, 10)}
    right = {"big.bin": FileStat(100, 10), "small.bin": FileStat(50, 10)}
    changes = [
        Change("big.bin", Action.COPY_LEFT_TO_RIGHT, "same time, different size", 200),
        Change("small.bin", Action.COPY_LEFT_TO_RIGHT, "same time, different size", 10),
    ]
    listing = build_listing(left, right, changes, profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult(changes, listing=listing), None))
            model = preview.reason_filter.get_model()
            seen["labels"] = [model.get_string(index) for index in range(model.get_n_items())]
            seen["all_rows"] = preview.store.get_n_items()
            seen["plan_before"] = [row.change.action for row in preview.rows]

            preview.reason_filter.set_selected(preview.reason_values.index("bigger"))
            seen["bigger_reason"] = preview.filters["reason"]
            seen["bigger_rows"] = [preview.store.get_item(i).change.rel for i in range(preview.store.get_n_items())]

            preview.reason_filter.set_selected(preview.reason_values.index("smaller"))
            seen["smaller_rows"] = [preview.store.get_item(i).change.rel for i in range(preview.store.get_n_items())]

            preview.reason_filter.set_selected(0)
            seen["all_again"] = preview.store.get_n_items()
            seen["plan_after"] = [row.change.action for row in preview.rows]

            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["labels"][0] == "Show All"
    assert seen["labels"][3] == "Bigger source"
    assert seen["all_rows"] == 2
    assert seen["bigger_reason"] == "bigger"
    assert seen["bigger_rows"] == ["big.bin"]
    assert seen["smaller_rows"] == ["small.bin"]
    assert seen["all_again"] == 2
    assert seen["plan_before"] == seen["plan_after"]


def test_folder_direction_copies_destination_files_back(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestFolderDirection")
    profile = Profile(name="folder direction", left="/sample/left", right="/sample/right")
    left = {}
    right = {"AndamanTICKETS/ticket.pdf": FileStat(10, 1)}
    changes = [Change("AndamanTICKETS/ticket.pdf", Action.DELETE_RIGHT, "only on destination", 10)]
    listing = build_listing(left, right, changes, profile)
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            owner = MainWindow(self)
            preview = owner._preview(JobResult(profile, ScanResult(changes, listing=listing), None))
            preview._set_folder_direction("AndamanTICKETS", "left")
            seen["left"] = [(row.change.rel, row.change.action, row.change.reason) for row in preview.rows]
            # Pointing back re-aims the copy, the same way any copy row is aimed.
            preview._set_folder_direction("AndamanTICKETS", "right")
            seen["right"] = [(row.change.rel, row.change.action) for row in preview.rows]
            preview.close()
            self.quit()

    assert App().run(None) == 0
    assert seen["left"] == [("AndamanTICKETS/ticket.pdf", Action.COPY_RIGHT_TO_LEFT, "chosen in preview")]
    assert seen["right"] == [("AndamanTICKETS/ticket.pdf", Action.COPY_LEFT_TO_RIGHT)]
