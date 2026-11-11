import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from ofs.compare import Action
from ofs.jobs import JobResult
from ofs.model import Profile, SyncMode
from ofs.scan import scan_profile
from ofs.store import load_profiles, save_profile
from ofs.ui import ExclusionEditor, MainWindow, ScanWindow, SfsApplication


def _group_titles(box) -> list[tuple[str, bool]]:
    found = []
    child = box.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Expander):
            title = ""
            heading = child.get_label_widget()
            if heading is not None:
                label = heading.get_first_child()
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
    runs = [button for button in _buttons(widget) if button.get_label() in {"Run", "Run checked"}]
    return bool(runs) and all("run" in button.get_css_classes() for button in runs)


def _button_labels(widget) -> list[str]:
    found = []
    if isinstance(widget, Gtk.Button) and widget.get_label():
        found.append(widget.get_label())
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
            preview._add_exclusion(other)
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
            self.quit()

    assert App().run(None) == 0
    assert seen["kinds"] == ["Name anywhere", "Folder pattern", "Rooted path", "Legacy star"]
    assert seen["added"][-2:] == ["cache/tmp", "*.log"]
    assert seen["excluded"].startswith("Excluded by: .pyenv")
    assert "*/node_modules" not in seen["remaining"]
    assert seen["not_excluded"] == "Not excluded by these rules."


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


def test_scan_counts_stay_for_both_sides(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestScanCounts")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            scan = ScanWindow(window)
            scan.update("job", "Left", "album", 4)
            scan.update("job", "Left", "album/more", 40)
            scan.update("job", "Right", "album", 3)
            seen["counts"] = scan.count_label.get_text()
            seen["place"] = scan.file_label.get_text()
            seen["modal"] = scan.get_modal()
            self.quit()

    assert App().run(None) == 0
    assert seen["counts"] == "Left files found: 40    Right files found: 3"
    assert "Reading right" in seen["place"]
    assert seen["modal"] is False


def test_a_second_scan_waits_behind_the_one_already_running(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTestScanQueue")
    seen = {}

    class App(SfsApplication):
        def do_activate(self):
            window = MainWindow(self)
            window._scan_active = True
            window._scan_label = "Scanning alpha…"
            window._run_names({"beta"})
            window._run_names({"gamma", "delta"})
            seen["waiting"] = [set(item) for item in window._scan_queue]
            seen["status"] = window.status.get_text()
            seen["run_enabled"] = window.run_button.get_sensitive()
            window._scan_active = False
            window._scan_queue.clear()
            self.quit()

    assert App().run(None) == 0
    assert seen["waiting"] == [{"beta"}, {"gamma", "delta"}]
    assert seen["status"] == "Scanning alpha…  2 scans waiting."
    assert seen["run_enabled"] is True


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
    assert seen["status"].startswith("Scanned 1.")
    assert "0 files, 0 B to copy" in seen["status"]


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
