"""GTK 4 window: profile groups, a dry-run scan, and the preview actions."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from ofs.appearance import apply_appearance
from ofs.apply import ApplyReport, CopyProgress, RunControl, apply_plan
from ofs.archive import older_files, restore_version
from ofs.bulk import FIELDS, bulk_preview, prepare_bulk_edit
from ofs.compare import Action, Change, format_bytes, plan_summary
from ofs.jobs import JobResult, run_checked
from ofs.history import delete_history
from ofs.legacy import import_cfg, import_global_masks
from ofs.masks import included, path_matches, split_masks
from ofs.model import Direction, ExclusionMode, Profile, SizeConflict, SymlinkMode, SyncMode, TimestampRules, parse_size
from ofs.review import (
    SAFETY_SECONDS,
    Listed,
    log_body,
    matches,
    outcome_headline,
    result_rows,
    safety_text,
    build_tree,
    deletion_violations,
    point_change,
    shown_summary,
    status_lines,
    without_deletes,
    write_log,
)
from ofs.scan import remember
from ofs.settings import (
    compare_command,
    global_exclusion_mode,
    global_exclusions,
    scan_estimate,
    copy_speed_limit,
    set_compare_command,
    set_copy_speed_limit,
    set_global_exclusion_mode,
    set_global_exclusions,
    set_theme,
    theme,
)
from ofs.store import (
    config_dir,
    delete_profile,
    duplicate_profile,
    install_imported,
    load_profiles,
    name_replacements,
    path_replacements,
    rename_profiles,
    save_profile,
    validate_name_replacements,
)
from ofs.transfer import export_profiles, install_profile_bundle, read_profile_bundle

_CFG = Path.home() / ".SuperFlexibleSynchronizer" / "SuperFlexibleSynchronizer.cfg"


def _days(text: str) -> float:
    try:
        return max(0.0, float(text.strip() or 0))
    except ValueError:
        return 0.0


def open_uri(path: Path) -> None:
    if not path.exists():
        return
    Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(str(path.resolve()), None), None)


class PlanRow(GObject.Object):
    def __init__(self, change: Change):
        super().__init__()
        self.change = change


_LAST_RUN_WIDTH = 142
_RESULT_WIDTH = 250
_ACTIONS_WIDTH = 260


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="OFS")
        self.set_default_size(1100, 740)
        self.profiles: list[Profile] = []
        self.checked: set[str] = set()
        self.session_skips: dict[str, set[str]] = {}
        self.session_masks: dict[str, list[str]] = {}
        self._labels: dict[str, Gtk.Label] = {}
        self._group_expanded: dict[str, bool] = {}
        self._applying_groups = False
        self._scan_active = False
        self._scan_queue: list[set[str]] = []
        self._scan_label = ""
        self._path_available: dict[str, bool] = {}
        self._visible_count = 0

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        new_button = Gtk.Button(label="New profile")
        new_button.connect("clicked", self._new_profile)
        restore_button = Gtk.Button(label="Restore Older…")
        restore_button.set_tooltip_text("Restore an archived file to a separate folder")
        restore_button.connect("clicked", self._restore_older)
        self.profiles_menu = Gtk.MenuButton(label="Profiles")
        self.profiles_menu.set_tooltip_text("Import, export, and edit checked profiles")
        popover = Gtk.Popover()
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        for side in ("start", "end", "top", "bottom"):
            getattr(choices, f"set_margin_{side}")(8)
        choices.append(Gtk.Label(label="Import / export", xalign=0, css_classes=["dim-label"]))
        for label, callback in (
            ("Import 5.71 profiles…", self._import),
            ("Import OFS profiles…", self._import_bundle),
            ("Export checked profiles…", self._export_bundle),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, fn=callback: (popover.popdown(), fn()))
            choices.append(button)
        choices.append(Gtk.Separator())
        choices.append(Gtk.Label(label="Checked profiles", xalign=0, css_classes=["dim-label"]))
        for label, callback in (
            ("Multi Edit checked profiles…", self._multi_edit),
            ("Replace paths in checked profiles…", self._replace_paths),
            ("Rename checked profiles…", self._rename_checked),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, fn=callback: (popover.popdown(), fn()))
            choices.append(button)
        popover.set_child(choices)
        self.profiles_menu.set_popover(popover)
        self.run_button = Gtk.Button(label="Run checked")
        self.run_button.add_css_class("run")
        self.run_button.connect("clicked", self._run)
        settings_button = Gtk.Button(label="Settings")
        settings_button.connect("clicked", self._settings)
        header.pack_start(new_button)
        header.pack_start(self.profiles_menu)
        header.pack_start(restore_button)
        header.pack_end(self.run_button)
        header.pack_end(settings_button)

        self.search = Gtk.SearchEntry(placeholder_text="Filter profiles")
        self.search.set_hexpand(True)
        self.search.connect("changed", lambda *_: self._on_search())
        expand_all = Gtk.Button(label="Expand all")
        _compact(expand_all)
        expand_all.connect("clicked", lambda *_: self._set_groups(True))
        collapse_all = Gtk.Button(label="Collapse all")
        _compact(collapse_all)
        collapse_all.connect("clicked", lambda *_: self._set_groups(False))
        tools = Gtk.Box(spacing=8)
        tools.set_margin_start(12)
        tools.set_margin_end(12)
        tools.set_margin_top(8)
        tools.append(self.search)
        tools.append(expand_all)
        tools.append(collapse_all)
        self.status_filter = Gtk.DropDown.new_from_strings(
            ["All statuses", "Never run in OFS", "Clean", "Stopped", "Errors", "Selected", "Not run in…", "Disabled"]
        )
        self.status_filter.set_selected(0)
        self.status_filter.connect("notify::selected", self._status_filter_changed)
        self.stale_days = Gtk.SpinButton.new_with_range(1, 3650, 1)
        self.stale_days.set_value(7)
        self.stale_days.set_tooltip_text("Include profiles never run or last run at least this many days ago")
        self.stale_days.connect("value-changed", lambda *_: self._rebuild())
        self.stale_days_label = Gtk.Label(label="days")
        self.available_only = Gtk.CheckButton(label="Available paths only")
        self.available_only.set_tooltip_text("Show only profiles whose left and right folders exist now")
        self.available_only.connect("toggled", lambda *_: self._rebuild())
        self.filter_count = Gtk.Label(xalign=1, hexpand=True)
        self.filter_count.add_css_class("dim-label")
        filters = Gtk.Box(spacing=10)
        filters.set_margin_start(12)
        filters.set_margin_end(12)
        filters.append(self.status_filter)
        filters.append(self.stale_days)
        filters.append(self.stale_days_label)
        filters.append(self.available_only)
        filters.append(self.filter_count)
        self._set_stale_visibility(False)
        self.status = Gtk.Label(xalign=0)
        self.status.set_margin_start(12)
        self.status.set_margin_end(12)
        self.status.set_margin_bottom(8)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.list_box.set_margin_start(12)
        self.list_box.set_margin_end(12)
        self.list_box.set_margin_top(8)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(self.list_box)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(tools)
        root.append(filters)
        self.column_headers = self._column_headers()
        root.append(self.column_headers)
        root.append(scrolled)
        root.append(self.status)
        self.set_child(root)
        _install_css()
        self.reload()

    def reload(self) -> None:
        self.profiles = load_profiles()
        self._path_available.clear()
        self._rebuild()
        folder = config_dir() / "profiles"
        self.status.set_text(f"{len(self.profiles)} profiles in {folder}")

    def _column_headers(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=4)
        row.add_css_class("column-header")
        for text, width, expand in (
            ("Profile", 0, True),
            ("Last run", _LAST_RUN_WIDTH, False),
            ("Result", _RESULT_WIDTH, False),
            ("Actions", _ACTIONS_WIDTH, False),
        ):
            label = Gtk.Label(label=text, xalign=0, hexpand=expand)
            label.add_css_class("column-heading")
            if width:
                label.set_size_request(width, -1)
            row.append(label)
        return row

    def _available(self, profile: Profile) -> bool:
        if profile.name not in self._path_available:
            self._path_available[profile.name] = Path(profile.left).is_dir() and Path(profile.right).is_dir()
        return self._path_available[profile.name]

    def _set_stale_visibility(self, visible: bool) -> None:
        self.stale_days.set_visible(visible)
        self.stale_days_label.set_visible(visible)

    def _status_filter_changed(self, *_args) -> None:
        self._set_stale_visibility(self.status_filter.get_selected() == 6)
        self._rebuild()

    def _shown(self, profile: Profile, query: str, status: str) -> bool:
        if query not in profile.name.lower() and query not in profile.group.lower():
            return False
        if self.available_only.get_active() and not self._available(profile):
            return False
        if status == "selected":
            return profile.name in self.checked
        if status == "disabled":
            return profile.disabled
        if status == "stale":
            return _not_run_since(profile.last_result, self.stale_days.get_value_as_int())
        if status != "all":
            return _result_status(profile.last_result) == status
        return True

    def _update_filter_count(self) -> None:
        self.filter_count.set_text(f"{self._visible_count} of {len(self.profiles)} shown · {len(self.checked)} checked")

    def _rebuild(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        self._labels.clear()
        query = self.search.get_text().strip().lower()
        statuses = ["all", "not_run", "clean", "stopped", "errors", "selected", "stale", "disabled"]
        status = statuses[self.status_filter.get_selected()]
        shown = [profile for profile in self.profiles if self._shown(profile, query, status)]
        self._visible_count = len(shown)
        self._update_filter_count()
        if not shown:
            text = "No profiles yet. Import reads SuperFlexibleSynchronizer.cfg and does not change it."
            if self.profiles:
                text = "No profiles match."
            self.list_box.append(Gtk.Label(label=text, xalign=0))
            return
        group_sizes = Counter(profile.group for profile in self.profiles)
        ungrouped = [profile for profile in shown if group_sizes[profile.group] == 1]
        groups: dict[str, list[Profile]] = {}
        for profile in shown:
            if group_sizes[profile.group] > 1:
                groups.setdefault(profile.group, []).append(profile)
        for profile in ungrouped:
            self.list_box.append(self._row(profile))
        self._applying_groups = True
        try:
            for group in sorted(groups, key=str.lower):
                members = groups[group]
                self.list_box.append(self._group(group, members, group_sizes[group]))
        finally:
            self._applying_groups = False

    def _on_search(self) -> None:
        query = self.search.get_text().strip().lower()
        if query:
            for profile in self.profiles:
                if query in profile.name.lower() or query in profile.group.lower():
                    self._group_expanded[profile.group] = True
        self._rebuild()

    def _set_groups(self, expanded: bool) -> None:
        for profile in self.profiles:
            self._group_expanded[profile.group] = expanded
        self._rebuild()

    def _remember_group(self, expander: Gtk.Expander, _pspec, group: str) -> None:
        if self._applying_groups:
            return
        self._group_expanded[group] = expander.get_expanded()

    def _group(self, group: str, members: list[Profile], total: int) -> Gtk.Widget:
        count = len(members)
        name = Gtk.Label(label=group, xalign=0, hexpand=True)
        name.add_css_class("group-name")
        amount = str(count) if count == total else f"{count}/{total}"
        tally = Gtk.Label(label=f"{amount} profile" if total == 1 else f"{amount} profiles")
        tally.add_css_class("group-count")
        selectable = [profile for profile in members if not profile.disabled]
        group_check = Gtk.CheckButton()
        group_check.set_sensitive(bool(selectable))
        group_check.set_tooltip_text("Select or clear the whole group (only visible members when filtered)" if selectable else "All visible profiles in this group are disabled")
        group_check.set_active(bool(selectable) and all(profile.name in self.checked for profile in selectable))
        group_check.set_inconsistent(any(profile.name in self.checked for profile in selectable) and not all(profile.name in self.checked for profile in selectable))
        group_check.connect("toggled", self._toggle_group, selectable)
        heading = Gtk.Box(spacing=12)
        heading.add_css_class("group-heading")
        heading.append(group_check)
        heading.append(name)
        heading.append(tally)
        expander = Gtk.Expander()
        expander.add_css_class("group-block")
        expander.set_hexpand(True)
        expander.set_label(f"{group}  ({count})")
        expander.set_label_widget(heading)
        expander.connect("notify::expanded", self._remember_group, group)
        expander.set_expanded(self._group_expanded.get(group, False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(28)
        box.set_margin_top(4)
        box.set_margin_bottom(8)
        for profile in members:
            box.append(self._row(profile))
        expander.set_child(box)
        return expander

    def _toggle_group(self, check: Gtk.CheckButton, members: list[Profile]) -> None:
        if self._applying_groups:
            return
        names = {profile.name for profile in members}
        if check.get_active():
            self.checked.update(names)
        else:
            self.checked.difference_update(names)
        self._rebuild()

    def _row(self, profile: Profile) -> Gtk.Widget:
        check = Gtk.CheckButton(label=profile.name)
        check.set_active(profile.name in self.checked)
        check.set_sensitive(not profile.disabled)
        check.connect("toggled", self._toggled, profile.name)
        name_row = Gtk.Box(spacing=6)
        name_row.append(check)
        status = _status_icon(profile.last_result)
        if status is not None:
            name_row.append(status)
        if profile.disabled:
            badge = Gtk.Label(label="Disabled")
            badge.add_css_class("result-warn")
            name_row.append(badge)
        paths = Gtk.Label(xalign=0)
        paths.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        paths.add_css_class("dim-label")
        paths.set_text(f"{profile.left}    →    {profile.right}")
        paths.set_tooltip_text(f"Left: {profile.left}\nRight: {profile.right}")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.append(name_row)
        text.append(paths)
        if profile.disabled:
            text.add_css_class("profile-disabled")

        when, body, tone = _result_parts(profile.last_result, profile.note)
        stamp = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        stamp.add_css_class("monospace")
        stamp.add_css_class(tone)
        stamp.set_size_request(_LAST_RUN_WIDTH, -1)
        stamp.set_text(when or "Not run yet")
        result = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        result.set_ellipsize(Pango.EllipsizeMode.END)
        result.set_max_width_chars(40)
        result.set_size_request(_RESULT_WIDTH, -1)
        result.set_tooltip_text(body)
        result.add_css_class(tone)
        result.set_text(body)
        self._labels[profile.name] = result

        run = Gtk.Button(label="Run")
        run.add_css_class("run")
        run.set_sensitive(not profile.disabled)
        run.set_tooltip_text("Enable the profile in Edit before running it" if profile.disabled else None)
        _compact(run)
        run.connect("clicked", self._run_one, profile.name)
        edit = Gtk.Button(label="Edit")
        _compact(edit)
        edit.connect("clicked", self._edit, profile.name)
        duplicate = Gtk.Button(label="Duplicate")
        _compact(duplicate)
        duplicate.connect("clicked", self._duplicate, profile.name)
        delete = Gtk.Button(label="Delete")
        _compact(delete)
        delete.add_css_class("destructive-action")
        delete.connect("clicked", self._confirm_delete, profile.name)
        actions = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        actions.set_size_request(_ACTIONS_WIDTH, -1)
        for button in (run, edit, duplicate, delete):
            actions.append(button)
        row = Gtk.Box(spacing=4)
        row.set_margin_top(2)
        row.set_margin_bottom(2)
        text.set_hexpand(True)
        row.append(text)
        row.append(stamp)
        row.append(result)
        row.append(actions)
        return row

    def _toggled(self, button: Gtk.CheckButton, name: str) -> None:
        if button.get_active():
            self.checked.add(name)
        else:
            self.checked.discard(name)
        if self.status_filter.get_selected() == 5:
            self._rebuild()
        else:
            self._update_filter_count()

    def _profile(self, name: str) -> Profile | None:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def _import(self, *_args) -> None:
        if not _CFG.exists():
            self.status.set_text(f"No config at {_CFG}")
            return
        count = install_imported(import_cfg(_CFG))
        masks = import_global_masks(_CFG)
        if masks:
            set_global_exclusions(sorted(set(global_exclusions()) | set(masks)))
        self.checked.clear()
        self.reload()
        globals_note = f" Imported {len(masks)} global exclusion masks." if masks else ""
        self.status.set_text(
            f"Imported {count} profiles. Filled direction, Exact Mirror, and timestamp rules that were still at the defaults.{globals_note}"
        )

    def _import_bundle(self, *_args) -> None:
        file_filter = Gtk.FileFilter()
        file_filter.set_name("OFS profile bundles (*.json)")
        file_filter.add_pattern("*.json")
        dialog = Gtk.FileDialog()
        dialog.set_title("Import OFS profiles")
        dialog.set_default_filter(file_filter)
        dialog.open(self, None, self._import_bundle_opened)

    def _import_bundle_opened(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file is None or file.get_path() is None:
            self.status.set_text("Choose a local OFS JSON profile bundle.")
            return
        try:
            profiles = read_profile_bundle(Path(file.get_path()))
        except ValueError as exc:
            self.status.set_text(str(exc))
            return
        self._confirm_bundle_import(profiles)

    def _confirm_bundle_import(self, profiles: list[Profile]) -> None:
        names = {profile.name for profile in self.profiles}
        duplicates = sum(profile.name in names for profile in profiles)
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Import {len(profiles)} OFS profiles?")
        dialog.set_detail(f"{duplicates} name(s) already exist. Choose how to handle them. The original 5.71 configuration is never changed.")
        dialog.set_buttons(["Cancel", "Skip duplicates", "Rename duplicates", "Replace duplicates"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1 if duplicates == 0 else 0)
        dialog.choose(self, None, self._bundle_import_chosen, profiles)

    def _bundle_import_chosen(self, dialog, result, profiles: list[Profile]) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice == 0:
            return
        mode = {1: "skip", 2: "rename", 3: "replace"}[choice]
        try:
            summary = install_profile_bundle(profiles, duplicates=mode)
        except (OSError, ValueError) as exc:
            self.status.set_text(f"Import failed: {exc}")
            return
        self.checked.difference_update(profile.name for profile in profiles if profile.disabled)
        self.reload()
        self.status.set_text(f"Imported {summary.saved} OFS profiles; skipped {summary.skipped}; renamed {summary.renamed}.")

    def _export_bundle(self, *_args) -> None:
        chosen = [profile for profile in self.profiles if profile.name in self.checked]
        if not chosen:
            self.status.set_text("Check the profiles or groups to export.")
            return
        file_filter = Gtk.FileFilter()
        file_filter.set_name("OFS profile bundles (*.json)")
        file_filter.add_pattern("*.json")
        dialog = Gtk.FileDialog()
        dialog.set_title("Export checked OFS profiles")
        dialog.set_initial_name("OFS-profiles.json")
        dialog.set_default_filter(file_filter)
        dialog.save(self, None, self._export_bundle_saved, chosen)

    def _export_bundle_saved(self, dialog: Gtk.FileDialog, result, chosen: list[Profile]) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        if file is None or file.get_path() is None:
            self.status.set_text("Export needs a local destination file.")
            return
        try:
            path = export_profiles(Path(file.get_path()), chosen)
        except OSError as exc:
            self.status.set_text(f"Could not export profiles: {exc}")
            return
        self.status.set_text(f"Exported {len(chosen)} checked profiles to {path}.")

    def _restore_older(self, *_args) -> None:
        checked = [profile for profile in self.profiles if profile.name in self.checked]
        if len(checked) == 1:
            profile = checked[0]
        elif checked:
            self.status.set_text("Check only the profile whose Older files should be restored.")
            return
        elif len(self.profiles) == 1:
            profile = self.profiles[0]
        else:
            self.status.set_text("Check the profile whose Older files should be restored. Disabled profiles can be restored from Edit.")
            return
        RestoreWindow(self, profile).present()

    def _new_profile(self, *_args) -> None:
        self._editor(None)

    def _edit(self, _button, name: str) -> None:
        profile = self._profile(name)
        if profile is not None:
            self._editor(profile)

    def _multi_edit(self, *_args) -> BulkEditWindow | None:
        chosen = [profile for profile in self.profiles if profile.name in self.checked]
        if not chosen:
            self.status.set_text("Check the profiles to change with Multi Edit.")
            return None
        window = BulkEditWindow(self, chosen)
        window.present()
        return window

    def _confirm_bulk_edit(self, changed: list[Profile], field: str, window: BulkEditWindow) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Apply Multi Edit to {len(changed)} profiles?")
        dialog.set_detail(bulk_preview(changed, field))
        dialog.set_buttons(["Cancel", "Apply Changes"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._bulk_chosen, changed, window)

    def _bulk_chosen(self, dialog, result, changed: list[Profile], window: BulkEditWindow) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice == 1:
            self._save_bulk_changes(changed)
            window.close()

    def _save_bulk_changes(self, changed: list[Profile]) -> None:
        for profile in changed:
            save_profile(profile)
            if profile.disabled:
                self.checked.discard(profile.name)
        self.reload()
        self.status.set_text(f"Multi Edit updated {len(changed)} profiles. Unselected fields were preserved.")

    def _replace_paths(self, *_args) -> CheckedReplacementWindow | None:
        chosen = [profile for profile in self.profiles if profile.name in self.checked]
        if not chosen:
            self.status.set_text("Check the profiles whose paths should change.")
            return None
        window = CheckedReplacementWindow(self, chosen, "paths")
        window.present()
        return window

    def _rename_checked(self, *_args) -> CheckedReplacementWindow | None:
        chosen = [profile for profile in self.profiles if profile.name in self.checked]
        if not chosen:
            self.status.set_text("Check the profiles or groups to rename.")
            return None
        window = CheckedReplacementWindow(self, chosen, "names")
        window.present()
        return window

    def _confirm_path_replacements(self, pending: list[tuple[Profile, dict[str, str]]]) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Replace paths in {len(pending)} profiles?")
        names = ", ".join(profile.name for profile, _updates in pending[:8])
        extra = "" if len(pending) <= 8 else f" and {len(pending) - 8} more"
        dialog.set_detail(
            f"Apply the full path preview to {names}{extra}. Only the selected Left/Right fields change. "
            "Folders are not moved on disk. The file databases are removed because they describe the old paths."
        )
        dialog.set_buttons(["Cancel", "Replace Paths"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._path_replace_chosen, pending)

    def _path_replace_chosen(self, dialog, result, pending: list[tuple[Profile, dict[str, str]]]) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        changed = []
        for profile, updates in pending:
            for side, value in updates.items():
                if side not in {"left", "right"}:
                    raise ValueError("only Left and Right paths can be replaced")
                setattr(profile, side, value)
            changed.append(profile)
        self._save_replaced_paths(changed)

    def _confirm_name_replacements(self, changes: list[tuple[str, str]]) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Rename {len(changes)} checked profiles?")
        preview = "\n".join(f"{old} → {new}" for old, new in changes[:12])
        extra = "" if len(changes) <= 12 else f"\n… and {len(changes) - 12} more"
        dialog.set_detail(preview + extra + "\n\nPaths and all other profile settings are preserved. The old file database is removed.")
        dialog.set_buttons(["Cancel", "Rename Profiles"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._name_replace_chosen, changes)

    def _name_replace_chosen(self, dialog, result, changes: list[tuple[str, str]]) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        try:
            rename_profiles(changes)
        except (OSError, ValueError) as exc:
            self.status.set_text(f"Could not rename profiles: {exc}")
            return
        renamed = dict(changes)
        self.checked = {renamed.get(name, name) for name in self.checked}
        self.reload()
        self.status.set_text(f"Renamed {len(changes)} checked profiles. No paths or other settings were changed.")

    def _save_replaced_paths(self, changed: list[Profile]) -> None:
        from ofs.history import delete_history

        for profile in changed:
            save_profile(profile)
            delete_history(profile.name)
        self.reload()
        self.status.set_text(f"Updated paths in {len(changed)} profiles.")

    def _duplicate(self, _button, name: str) -> None:
        profile = self._profile(name)
        if profile is None:
            return
        duplicate_profile(profile)
        self.reload()

    def _confirm_delete(self, _button, name: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Delete {name}?")
        dialog.set_detail("The profile and its file database are removed. The folders on disk stay.")
        dialog.set_buttons(["Cancel", "Delete"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._delete_chosen, name)

    def _delete_chosen(self, dialog, result, name: str) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice == 1:
            self._remove_profile(name)

    def _remove_profile(self, name: str) -> None:
        delete_profile(name)
        delete_history(name)
        self.checked.discard(name)
        self.session_skips.pop(name, None)
        self.session_masks.pop(name, None)
        self.reload()

    def _settings(self, *_args) -> Gtk.Window:
        window = Gtk.Window(transient_for=self, modal=True, title="Settings")
        window.set_default_size(560, 520)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.append(Gtk.Label(label="Appearance", xalign=0))
        appearance = Gtk.DropDown.new_from_strings(["System", "Light", "Dark"])
        appearance_values = ["system", "light", "dark"]
        appearance.set_selected(appearance_values.index(theme()))
        appearance.set_tooltip_text("System follows the desktop color preference where available; Light and Dark override it for OFS.")
        box.append(appearance)
        box.append(Gtk.Label(label="Compare files with", xalign=0))
        entry = Gtk.Entry(text=compare_command())
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text("The two file paths are added after this command. OFS_COMPARE overrides it for one launch.")
        hint.add_css_class("dim-label")
        box.append(entry)
        box.append(hint)
        box.append(Gtk.Label(label="Copy speed limit (MiB/s; 0 = unlimited)", xalign=0))
        speed = Gtk.SpinButton.new_with_range(0, 1024, 0.1)
        speed.set_value(copy_speed_limit() / (1024 * 1024))
        speed.set_tooltip_text("One shared limit for the whole copy run")
        box.append(speed)
        box.append(Gtk.Label(label="Global exclusions", xalign=0))
        masks = Gtk.TextView()
        masks.set_monospace(True)
        masks.get_buffer().set_text("\n".join(global_exclusions()))
        scroll = Gtk.ScrolledWindow(min_content_height=160, vexpand=True)
        scroll.set_child(masks)
        global_hint = Gtk.Label(xalign=0, wrap=True)
        global_hint.set_text(
            "Applied to every profile. Ignore Totally: not copied and not deleted. "
            "Don't Copy: never transferred, but Exact Mirror deletes an extra copy on the destination. "
            "One mask per line. Saving an empty list turns the defaults off."
        )
        global_hint.add_css_class("dim-label")
        dont_copy = Gtk.CheckButton(label="Don't Copy")
        ignore_totally = Gtk.CheckButton(label="Ignore Totally", group=dont_copy)
        ignore_totally.set_active(global_exclusion_mode() == ExclusionMode.IGNORE_TOTALLY)
        dont_copy.set_active(global_exclusion_mode() == ExclusionMode.DONT_COPY)
        save = Gtk.Button(label="Save")

        def on_save(*_args) -> None:
            selected_theme = appearance_values[appearance.get_selected()]
            set_theme(selected_theme)
            apply_appearance(selected_theme)
            set_compare_command(entry.get_text())
            set_copy_speed_limit(speed.get_value() * 1024 * 1024)
            buffer = masks.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            set_global_exclusions([line.strip() for line in text.splitlines() if line.strip()])
            set_global_exclusion_mode(ExclusionMode.DONT_COPY if dont_copy.get_active() else ExclusionMode.IGNORE_TOTALLY)
            window.close()

        save.connect("clicked", on_save)
        box.append(scroll)
        box.append(dont_copy)
        box.append(ignore_totally)
        box.append(global_hint)
        box.append(save)
        window.set_child(box)
        window.present()
        return window

    def _editor(self, profile: Profile | None) -> Gtk.Window:
        existing = profile
        window = Gtk.Window(transient_for=self, modal=True, title="Profile")
        window.set_default_size(780, 840)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        name = Gtk.Entry(text="" if existing is None else existing.name)
        name.set_hexpand(True)
        left = Gtk.Entry(text="" if existing is None else existing.left)
        right = Gtk.Entry(text="" if existing is None else existing.right)
        left_volume = Gtk.Entry(text="" if existing is None else existing.left_volume)
        right_volume = Gtk.Entry(text="" if existing is None else existing.right_volume)
        mode = Gtk.DropDown.new_from_strings(["Copy only (no deletions)", "Exact Mirror (copy/move/delete)"])
        mode.set_selected(0 if existing is None or existing.mode == SyncMode.STANDARD else 1)
        direction = Gtk.DropDown.new_from_strings(["Left to right (L → R)", "Right to left (R → L)", "Both directions"])
        direction_values = [Direction.LEFT_TO_RIGHT, Direction.RIGHT_TO_LEFT, Direction.BOTH]
        direction.set_selected(0 if existing is None else direction_values.index(existing.direction))
        direction.set_tooltip_text("Choose which side supplies changes, or synchronize in both directions.")
        exclusion = Gtk.DropDown.new_from_strings(["Ignore Totally", "Don't Copy"])
        exclusion.set_selected(0 if existing is None or existing.exclusion_mode == ExclusionMode.IGNORE_TOTALLY else 1)
        exclusion.set_tooltip_text("Ignore Totally hides matches from copying and mirror deletion. Don't Copy leaves matches untransferred, but Exact Mirror may delete a destination extra.")
        rules = None if existing is None else existing.timestamps
        small = Gtk.CheckButton(label="Ignore differences of 2 seconds", active=bool(rules and rules.ignore_small))
        hour = Gtk.CheckButton(label="Ignore a difference of exactly one hour", active=bool(rules and rules.ignore_exact_hour))
        seconds = Gtk.CheckButton(label="Ignore seconds", active=bool(rules and rules.ignore_seconds))
        ignore_time = Gtk.CheckButton(label="Ignore the timestamp", active=bool(rules and rules.ignore_timestamp))
        larger = Gtk.CheckButton(label="A larger file is newer", active=bool(rules and rules.larger_is_newer))
        older = Gtk.CheckButton(label="Keep replaced files under Older", active=bool(existing and existing.keep_older))
        keep_deleted = Gtk.CheckButton(label="Recoverable deletions — move deleted files under Older", active=bool(existing and existing.keep_deleted))
        older_keep = Gtk.SpinButton.new_with_range(1, 99, 1)
        older_keep.set_value(1 if existing is None or existing.older_keep < 1 else existing.older_keep)
        binary = Gtk.CheckButton(label="Binary compare when size matches (reads file bytes)", active=bool(existing and existing.binary_compare))
        track = Gtk.CheckButton(label="Remember files and detect moves and deletions", active=bool(existing and existing.track))
        always_copy = Gtk.CheckButton(label="Always copy even when unchanged", active=bool(existing and existing.always_copy))
        compare_permissions = Gtk.CheckButton(label="Compare file permissions", active=bool(existing and existing.compare_permissions))
        compare_ownership = Gtk.CheckButton(label="Compare file owner and group", active=bool(existing and existing.compare_ownership))
        preserve_ownership = Gtk.CheckButton(label="Preserve owner and group when permitted", active=bool(existing and existing.preserve_ownership))
        preserve_ownership.set_tooltip_text("If the filesystem does not permit this, the copy fails safely instead of silently dropping ownership.")
        disabled = Gtk.CheckButton(label="Disabled — keep this profile but do not run it", active=bool(existing and existing.disabled))
        delete_all = Gtk.CheckButton(label="Block deletion of every file in a folder", active=bool(existing and existing.block_delete_all))
        never_delete = Gtk.CheckButton(label="Never delete files (existing empty-folder cleanup only)", active=bool(existing and existing.never_delete_files))
        delete_limit = Gtk.SpinButton.new_with_range(0, 100, 1)
        delete_limit.set_value(0 if existing is None else existing.max_delete_percent)
        symlinks = Gtk.DropDown.new_from_strings(["Follow targets", "Preserve links", "Skip links"])
        symlink_values = [SymlinkMode.FOLLOW, SymlinkMode.PRESERVE, SymlinkMode.SKIP]
        symlinks.set_selected(0 if existing is None else symlink_values.index(existing.symlinks))
        symlinks.set_tooltip_text("Follow reads file/dir targets without following cycles; Preserve transfers the link itself; Skip ignores links entirely.")
        size_policies = [SizeConflict.COPY, SizeConflict.SKIP, SizeConflict.ASK]
        same_time_size = Gtk.DropDown.new_from_strings(["Copy", "Skip", "Ask"])
        same_time_size.set_selected(0 if rules is None else size_policies.index(rules.same_time_different_size))
        include_editor = ExclusionEditor(["(all)"] if existing is None else existing.include, include=True)
        exclusion_editor = ExclusionEditor([] if existing is None else existing.exclusions)
        min_size = Gtk.Entry(text="" if existing is None or not existing.min_size else str(existing.min_size))
        max_size = Gtk.Entry(text="" if existing is None or not existing.max_size else str(existing.max_size))
        min_age = Gtk.Entry(text="" if existing is None or not existing.min_age_days else str(existing.min_age_days))
        max_age = Gtk.Entry(text="" if existing is None or not existing.max_age_days else str(existing.max_age_days))
        rows = [
            ("Name", name),
            ("Left", left),
            ("Right", right),
            ("Left volume", left_volume),
            ("Right volume", right_volume),
            ("Mode", mode),
            ("Direction", direction),
            ("Symlinks", symlinks),
            ("Same time, different size", same_time_size),
            ("Include filters", include_editor),
            ("Exclusions", exclusion),
            ("Exclusion rules", exclusion_editor),
            ("Min size", min_size),
            ("Max size", max_size),
            ("Min age (days)", min_age),
            ("Max age (days)", max_age),
            ("Older versions", older_keep),
            ("Delete limit (%; 0 = off)", delete_limit),
        ]
        for index, (label, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=1), 0, index, 1, 1)
            grid.attach(widget, 1, index, 1, 1)
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for widget in (small, hour, seconds, ignore_time, larger, older, keep_deleted, binary, track, always_copy, compare_permissions, compare_ownership, preserve_ownership, disabled, delete_all, never_delete):
            checks.append(widget)
        grid.attach(checks, 1, len(rows), 1, 1)
        save = Gtk.Button(label="Save")
        editor_buttons = Gtk.Box(spacing=8)
        editor_buttons.append(save)
        if existing is not None:
            restore = Gtk.Button(label="Restore Older…")
            restore.connect("clicked", lambda *_: RestoreWindow(self, existing).present())
            editor_buttons.append(restore)
        grid.attach(editor_buttons, 1, len(rows) + 1, 1, 1)

        def on_save(*_args) -> None:
            chosen = name.get_text().strip()
            if not chosen:
                return
            updated = Profile(
                name=chosen,
                left=left.get_text().strip(),
                right=right.get_text().strip(),
                left_volume=left_volume.get_text().strip(),
                right_volume=right_volume.get_text().strip(),
                include=include_editor.rules(),
                exclusions=exclusion_editor.rules(),
                exclusion_mode=ExclusionMode.IGNORE_TOTALLY if exclusion.get_selected() == 0 else ExclusionMode.DONT_COPY,
                direction=direction_values[direction.get_selected()],
                mode=SyncMode.STANDARD if mode.get_selected() == 0 else SyncMode.EXACT_MIRROR,
                timestamps=TimestampRules(
                    ignore_small=small.get_active(),
                    ignore_exact_hour=hour.get_active(),
                    ignore_seconds=seconds.get_active(),
                    ignore_timestamp=ignore_time.get_active(),
                    larger_is_newer=larger.get_active(),
                    same_time_different_size=size_policies[same_time_size.get_selected()],
                ),
                keep_older=older.get_active(),
                older_folder="Older" if existing is None else existing.older_folder,
                keep_deleted=keep_deleted.get_active(),
                older_keep=int(older_keep.get_value()),
                min_size=parse_size(min_size.get_text()),
                max_size=parse_size(max_size.get_text()),
                min_age_days=_days(min_age.get_text()),
                max_age_days=_days(max_age.get_text()),
                binary_compare=binary.get_active(),
                track=track.get_active(),
                always_copy=always_copy.get_active(),
                disabled=disabled.get_active(),
                symlinks=symlink_values[symlinks.get_selected()],
                compare_permissions=compare_permissions.get_active(),
                compare_ownership=compare_ownership.get_active(),
                preserve_ownership=preserve_ownership.get_active(),
                max_delete_percent=delete_limit.get_value_as_int(),
                block_delete_all=delete_all.get_active(),
                never_delete_files=never_delete.get_active(),
                last_result="" if existing is None else existing.last_result,
                note="" if existing is None else existing.note,
                imported_settings=False if existing is None else existing.imported_settings,
            )
            if existing is not None and existing.name != chosen:
                from ofs.history import delete_history
                from ofs.store import delete_profile

                delete_profile(existing.name)
                delete_history(existing.name)
            elif existing is not None and (existing.left != updated.left or existing.right != updated.right):
                from ofs.history import delete_history

                delete_history(chosen)
            save_profile(updated)
            if updated.disabled:
                self.checked.discard(updated.name)
            window.close()
            self.reload()

        save.connect("clicked", on_save)
        editor_scroll = Gtk.ScrolledWindow(vexpand=True)
        editor_scroll.set_child(grid)
        window.set_child(editor_scroll)
        window.present()
        return window

    def _run_one(self, _button, name: str) -> None:
        self._run_names({name})

    def _run(self, *_args) -> None:
        chosen = {profile.name for profile in self.profiles if profile.name in self.checked}
        if not chosen:
            self.status.set_text("Check one or more profiles.")
            return
        self._run_names(chosen)

    def _run_names(self, chosen: set[str]) -> None:
        chosen.difference_update(profile.name for profile in self.profiles if profile.disabled)
        if not chosen:
            self.status.set_text("The selected profile is disabled. Enable it in Edit first.")
            return
        self._scan_queue.append(set(chosen))
        if self._scan_active:
            self._note_scan_queue()
            return
        self._start_next_scan()

    def _note_scan_queue(self) -> None:
        waiting = len(self._scan_queue)
        base = self._scan_label or "Scanning…"
        if waiting:
            noun = "scan" if waiting == 1 else "scans"
            self.status.set_text(f"{base}  {waiting} {noun} waiting.")
        else:
            self.status.set_text(base)

    def _start_next_scan(self) -> None:
        if self._scan_active or not self._scan_queue:
            return
        chosen = self._scan_queue.pop(0)
        self._scan_active = True
        if len(chosen) == 1:
            self._scan_label = f"Scanning {next(iter(chosen))}…"
        else:
            self._scan_label = f"Scanning {len(chosen)} checked profiles…"
        self._note_scan_queue()
        profiles = list(self.profiles)
        skips = {name: set(values) for name, values in self.session_skips.items()}
        masks = {name: list(values) for name, values in self.session_masks.items()}
        estimates = {name: scan_estimate(name) for name in chosen}
        progress = ScanWindow(self, self._scan_label.removesuffix("…"), estimates=estimates)
        progress.present()

        def on_stats(name: str, side: str, rel: str, values) -> None:
            GLib.idle_add(progress.update_stats, name, side, rel, values)

        def work() -> None:
            try:
                results = run_checked(
                    profiles,
                    checked=chosen,
                    session_skips=skips,
                    session_masks=masks,
                    control=progress.control,
                    on_stats=on_stats,
                )
            except Exception as exc:
                failed = next(profile for profile in profiles if profile.name in chosen)
                results = [JobResult(failed, None, str(exc))]
            GLib.idle_add(self._show_results, results, progress)

        threading.Thread(target=work, daemon=True).start()

    def _show_results(self, results: list[JobResult], progress: ScanWindow | None = None) -> bool:
        if progress is not None:
            progress.allow_close()
        self._scan_active = False
        self._scan_label = ""
        if results and all(job.error == "scan stopped" for job in results):
            self.status.set_text("Scan stopped.")
            self._start_next_scan()
            return False
        ok = sum(1 for job in results if job.error is None)
        bad = len(results) - ok
        planned = [change for job in results if job.result is not None for change in job.result.changes]
        summary_text = plan_summary(planned)
        self.status.set_text(f"Scanned {ok}. {bad} skipped. {summary_text}" if bad else f"Scanned {ok}. {summary_text}")
        for job in results:
            if job.result is not None and job.profile.name in self._labels:
                self._labels[job.profile.name].set_text(plan_summary(job.result.changes))
        if len(results) == 1 and results[0].error is None and results[0].result is not None:
            self._preview(results[0])
            self._start_next_scan()
            return False
        summary = Gtk.Window(transient_for=self, title="Scan")
        summary.set_default_size(760, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.append(Gtk.Label(label=summary_text, xalign=0))
        for job in results:
            line = Gtk.Box(spacing=8)
            if job.error:
                label = Gtk.Label(label=f"{job.profile.name}: {job.error}", xalign=0, hexpand=True)
            else:
                assert job.result is not None
                label = Gtk.Label(label=f"{job.profile.name}: {plan_summary(job.result.changes)}", xalign=0, hexpand=True)
                button = Gtk.Button(label="Preview")
                button.connect("clicked", lambda _b, item=job: self._preview(item))
                line.append(button)
            line.prepend(label)
            box.append(line)
            if job.error and job.profile.name in self._labels:
                self._labels[job.profile.name].set_text(job.error)
        summary.set_child(box)
        summary.present()
        self._start_next_scan()
        return False

    def _preview(self, job: JobResult) -> None:
        assert job.result is not None
        window = Preview(self, job)
        window.present()
        return window


def _compact(button: Gtk.Button) -> None:
    button.add_css_class("compact")


_RUN_RESULT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(.*)$")


def _result_parts(last_result: str, note: str) -> tuple[str, str, str]:
    """Timestamp, body, and CSS tone for the profile row. No result yet uses the note."""

    text = last_result.strip()
    if not text:
        return "", note, "dim-label"
    match = _RUN_RESULT.match(text)
    when, body = match.groups() if match else ("", text)
    lowered = body.lower()
    tone = "result-error" if "error" in lowered else "result-warn" if lowered.startswith("stopped") else "result-ok"
    return when, body, tone


def _result_status(last_result: str) -> str:
    if not last_result.strip():
        return "not_run"
    tone = _result_parts(last_result, "")[2]
    return {"result-error": "errors", "result-warn": "stopped"}.get(tone, "clean")


def _not_run_since(last_result: str, days: int, now: float | None = None) -> bool:
    """Never/undated runs need attention; dated runs are stale after N days."""

    when = _result_parts(last_result, "")[0]
    if not when:
        return True
    try:
        run_at = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M"))
    except ValueError:
        return True
    current = time.time() if now is None else now
    return current - run_at >= days * 86400


def _status_icon(last_result: str) -> Gtk.Image | None:
    """5.71-style row status: green check ran clean, red for errors, orange for stopped, none for never run."""

    _when, body, tone = _result_parts(last_result, "")
    if not body:
        return None
    name = {"result-error": "dialog-error", "result-warn": "dialog-warning"}.get(tone, "emblem-ok")
    icon = Gtk.Image.new_from_icon_name(name)
    icon.set_pixel_size(16)
    icon.set_tooltip_text(body)
    return icon


def _mask_kind(mask: str) -> str:
    if mask == "(all)":
        return "Include all"
    if mask == "*":
        return "Legacy star"
    normalized = mask.replace("\\", "/")
    if normalized.startswith("/"):
        return "Rooted path"
    if "/" in normalized:
        return "Folder pattern"
    if "*" in normalized or "?" in normalized:
        return "Name pattern"
    return "Name anywhere"


class ExclusionEditor(Gtk.Box):
    """Editable rule list with immediate relative-path matching feedback."""

    def __init__(self, masks: list[str], include: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._rules = list(masks)
        self.include_mode = include
        self._editing: int | None = None
        placeholder = "Include mask, for example *.jpg (keep (all) for everything)" if include else "Mask, for example .venv or */node_modules"
        self.entry = Gtk.Entry(placeholder_text=placeholder, hexpand=True)
        self.entry.connect("activate", lambda *_: self._save_rule())
        self.add_button = Gtk.Button(label="Add rule")
        self.add_button.connect("clicked", lambda *_: self._save_rule())
        self.cancel_button = Gtk.Button(label="Cancel edit")
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", lambda *_: self._reset_entry())
        editing = Gtk.Box(spacing=6)
        editing.append(self.entry)
        editing.append(self.add_button)
        editing.append(self.cancel_button)
        self.append(editing)
        self.list = Gtk.ListBox()
        self.list.add_css_class("boxed-list")
        scroll = Gtk.ScrolledWindow(min_content_height=150, max_content_height=240, vexpand=True)
        scroll.set_child(self.list)
        self.append(scroll)
        self.test_entry = Gtk.Entry(placeholder_text="Test a relative path, for example project/.venv/bin/python")
        self.test_entry.connect("changed", lambda *_: self._update_test())
        self.append(self.test_entry)
        self.test_result = Gtk.Label(xalign=0, wrap=True)
        self.test_result.add_css_class("dim-label")
        self.append(self.test_result)
        self._refresh()

    def rules(self) -> list[str]:
        return list(self._rules)

    def _reset_entry(self) -> None:
        self._editing = None
        self.entry.set_text("")
        self.add_button.set_label("Add rule")
        self.cancel_button.set_sensitive(False)

    def _save_rule(self) -> None:
        additions = split_masks(self.entry.get_text())
        if not additions:
            return
        if self._editing is None:
            self._rules.extend(additions)
        else:
            self._rules[self._editing : self._editing + 1] = additions
        self._reset_entry()
        self._refresh()

    def _edit_rule(self, index: int) -> None:
        self._editing = index
        self.entry.set_text(self._rules[index])
        self.entry.grab_focus()
        self.add_button.set_label("Save rule")
        self.cancel_button.set_sensitive(True)

    def _remove_rule(self, index: int) -> None:
        del self._rules[index]
        self._reset_entry()
        self._refresh()

    def _refresh(self) -> None:
        while row := self.list.get_first_child():
            self.list.remove(row)
        for index, mask in enumerate(self._rules):
            kind = Gtk.Label(label=_mask_kind(mask), xalign=0)
            kind.add_css_class("dim-label")
            kind.set_size_request(112, -1)
            label = Gtk.Label(label=mask, xalign=0, hexpand=True, selectable=True)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_tooltip_text("Preserved 5.71 separator; matches nothing" if mask == "*" else mask)
            edit = Gtk.Button(label="Edit")
            _compact(edit)
            edit.connect("clicked", lambda _button, item=index: self._edit_rule(item))
            remove = Gtk.Button(label="Remove")
            _compact(remove)
            remove.connect("clicked", lambda _button, item=index: self._remove_rule(item))
            line = Gtk.Box(spacing=6)
            line.set_margin_start(6)
            line.set_margin_end(6)
            for widget in (kind, label, edit, remove):
                line.append(widget)
            self.list.append(line)
        self._update_test()

    def _update_test(self) -> None:
        path = self.test_entry.get_text().strip()
        if not path:
            hint = "Only matching paths are included; (all) includes everything." if self.include_mode else "Test a path to see what would be excluded."
            self.test_result.set_text(f"{len(self._rules)} rules. {hint}")
            return
        matches = [mask for mask in self._rules if path_matches(path, mask)]
        if self.include_mode:
            if matches and included(path, self._rules):
                self.test_result.set_text("Included by: " + "; ".join(matches))
            elif included(path, self._rules):
                self.test_result.set_text("Included: there is no restrictive include rule.")
            else:
                self.test_result.set_text("Not included; this path is left out.")
        elif matches:
            self.test_result.set_text("Excluded by: " + "; ".join(matches))
        else:
            self.test_result.set_text("Not excluded by these rules.")


_CSS_INSTALLED = False


def _install_css() -> None:
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(
        "label.banner { background-color: alpha(@theme_selected_bg_color, 0.35); padding: 8px; }"
        "button.compact { min-height: 0; min-width: 0; padding: 0 8px; font-size: 0.85em; }"
        "button.run { background-color: #2f9e44; color: white; background-image: none; }"
        "button.run:hover { background-color: #37b24d; color: white; background-image: none; }"
        "button.run:active { background-color: #2b8a3e; color: white; background-image: none; }"
        "button.run:disabled { background-color: alpha(#2f9e44, 0.45); color: alpha(white, 0.8); background-image: none; }"
        "label.group-name { font-weight: 800; font-size: 1.35em; }"
        "label.group-count {"
        " font-weight: 700;"
        " padding: 2px 10px;"
        " border-radius: 999px;"
        " background-color: alpha(@theme_selected_bg_color, 0.55);"
        "}"
        ".group-block { margin-top: 8px; }"
        ".group-block .title {"
        " background-color: alpha(@theme_selected_bg_color, 0.28);"
        " border-radius: 8px;"
        " padding: 8px 12px;"
        " min-height: 40px;"
        "}"
        ".column-header { margin: 6px 12px 2px 12px; padding-bottom: 4px; border-bottom: 1px solid alpha(@theme_fg_color, 0.18); }"
        "label.column-heading { font-weight: 700; font-size: 0.9em; }"
        ".profile-disabled { opacity: 0.55; }"
        "label.result-ok { color: @success_color; }"
        "label.result-error { color: @error_color; }"
        "label.result-warn { color: @warning_color; }"
    )
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


class Preview(Gtk.Window):
    def __init__(self, owner: MainWindow, job: JobResult):
        super().__init__(transient_for=owner, title=job.profile.name)
        _install_css()
        self.owner = owner
        self.job = job
        self.set_default_size(1100, 720)
        assert job.result is not None
        if job.result.listing:
            self.rows = list(job.result.listing)
        else:
            self.rows = [Listed(change, False, False) for change in job.result.changes]
        self._original = {row.change.rel: row.change for row in self.rows}
        direction = job.profile.direction
        if direction == Direction.RIGHT_TO_LEFT:
            adjust = "left"
        elif direction == Direction.BOTH:
            adjust = "either"
        else:
            adjust = "right"
        self.filters = {
            "state": "affected",
            "existence": "all",
            "moved": "show",
            "deletes": "show",
            "adjust": adjust,
        }
        self.store = Gio.ListStore.new(PlanRow)
        self._running = False
        self._popover: Gtk.Popover | None = None
        self._build()
        self._refill()

    def _build(self) -> None:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(8)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_size_request(220, -1)
        sidebar.append(self._radios("State", "state", [
            ("Show All", "all"),
            ("Affected", "affected"),
            ("del./mov.", "del_mov"),
            ("R to L", "to_left"),
            ("Unaffected", "unaffected"),
            ("Skipped", "skipped"),
        ]))
        sidebar.append(self._radios("Existence", "existence", [
            ("Show All", "all"),
            ("One Side", "one"),
            ("Both Sides", "both"),
        ]))
        sidebar.append(self._radios("Moved Files", "moved", [
            ("Show", "show"),
            ("Show Only", "only"),
            ("Don't Show", "hide"),
        ]))
        sidebar.append(self._radios("Adjust Them", "adjust", [
            ("On the Left", "left"),
            ("On the Right", "right"),
            ("Either side", "either"),
        ]))
        sidebar.append(self._radios("Files To Delete", "deletes", [
            ("Show", "show"),
            ("Show Only", "only"),
            ("Don't Show", "hide"),
        ]))
        self.shown = Gtk.Label(xalign=0, wrap=True)
        sidebar.append(self.shown)
        self.start_button = Gtk.Button(label="Start")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._start)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.close())
        sidebar.append(self.start_button)
        sidebar.append(close_button)

        self.left_status = Gtk.Label(xalign=0)
        self.right_status = Gtk.Label(xalign=0)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(8)
        header.append(self._side_head("Left", self.job.profile.left, self.left_status))
        header.append(self._side_head("Right", self.job.profile.right, self.right_status))

        self.tree_store = Gtk.TreeStore.new([str, str, str, str, str])
        self.view = Gtk.TreeView(model=self.tree_store)
        self.view.set_headers_visible(True)
        self.view.set_enable_tree_lines(True)
        self.view.set_enable_search(True)
        self.view.set_search_column(0)
        for index, title, width in (
            (0, "Name", 380),
            (1, "Size", 100),
            (2, "Direction", 220),
            (3, "Status", 240),
        ):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            column.set_fixed_width(width)
            if index == 1:
                renderer.set_property("xalign", 1.0)
            if index == 2:
                renderer.set_property("family", "monospace")
                renderer.set_property("scale", 1.15)
            self.view.append_column(column)
        click = Gtk.GestureClick()
        click.set_button(3)
        click.connect("pressed", self._tree_pressed)
        self.view.add_controller(click)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(self.view)
        self.detail = Gtk.Label(xalign=0, wrap=True)
        bar = Gtk.Box(spacing=8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        self.detail.set_hexpand(True)
        bar.append(self.detail)
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        listing.append(header)
        listing.append(scrolled)
        listing.append(bar)
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.append(sidebar)
        listing.set_hexpand(True)
        root.append(listing)
        self.set_child(root)

    def _side_head(self, title: str, path: str, status: Gtk.Label) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name = Gtk.Label(xalign=0)
        name.set_markup(f"<b>{title}</b>")
        folder = Gtk.Label(label=path, xalign=0)
        folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        folder.set_selectable(True)
        folder.add_css_class("dim-label")
        box.append(name)
        box.append(folder)
        box.append(status)
        return box

    def _radios(self, title: str, key: str, options: list[tuple[str, str]]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        heading = Gtk.Label(xalign=0)
        heading.set_markup(f"<b>{title}</b>")
        box.append(heading)
        group = None
        current = self.filters[key]
        for label, value in options:
            button = Gtk.CheckButton(label=label)
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.set_active(value == current)
            button.connect("toggled", self._on_filter, key, value)
            box.append(button)
        return box

    def _on_filter(self, button: Gtk.CheckButton, key: str, value: str) -> None:
        if button.get_active():
            self._set_filter(**{key: value})

    def _set_filter(self, **kwargs) -> None:
        self.filters.update(kwargs)
        self._refill()

    def _refill(self) -> None:
        while self.store.get_n_items():
            self.store.remove(0)
        visible = []
        for row in self.rows:
            if matches(row, **self.filters):
                visible.append(row)
                self.store.append(PlanRow(row.change))
        self._fill_tree(visible)
        self._refresh_detail()

    def _fill_tree(self, visible: list[Listed]) -> None:
        self.tree_store.clear()

        def add(parent, node) -> None:
            label = node.name if node.is_file else f"{node.name}    {node.size_text}"
            current = self.tree_store.append(
                parent, [label, node.size_text, node.direction, node.status, node.rel]
            )
            for child in node.children:
                add(current, child)

        for node in build_tree(visible):
            add(None, node)
        self.view.expand_all()

    def _rel_at(self, x: float, y: float) -> str | None:
        # Gesture coordinates include the column header. The tree hit test
        # starts below that header, so an unconverted click on a folder lands
        # on the first file in it.
        bx, by = self.view.convert_widget_to_bin_window_coords(int(x), int(y))
        found = self.view.get_path_at_pos(int(bx), int(by))
        if not found:
            return None
        tree_iter = self.tree_store.get_iter(found[0])
        if tree_iter is None:
            return None
        return self.tree_store.get_value(tree_iter, 4)

    def _tree_pressed(self, _gesture, _n, x: float, y: float) -> None:
        rel = self._rel_at(x, y)
        if rel is None:
            return
        listed = next((item for item in self.rows if item.change.rel == rel), None)
        if listed is None:
            self._popup_folder(rel, x, y)
            return
        self._popup(PlanRow(listed.change), self.view, x, y)

    def _popup_folder(self, rel: str, x: float, y: float) -> None:
        if self._popover is not None:
            self._popover.unparent()
        popover = Gtk.Popover()
        popover.set_parent(self.view)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        actions = [
            ("L ──▶ R", "Copy this folder left to right", lambda: self._set_folder_direction(rel, "right")),
            ("L ◀── R", "Copy this folder right to left", lambda: self._set_folder_direction(rel, "left")),
            ("Open left folder", "", lambda: open_uri(Path(self.job.profile.left) / rel)),
            ("Open right folder", "", lambda: open_uri(Path(self.job.profile.right) / rel)),
            ("Add this folder to exclusions", "", lambda: self._save_exclusion(rel)),
            ("Ignore this folder for this run", "", lambda: self._ignore_folder(rel)),
        ]
        for text, tip, callback in actions:
            button = Gtk.Button(label=text)
            if tip:
                button.set_tooltip_text(tip)
                button.add_css_class("monospace")
            button.connect("clicked", lambda _b, fn=callback: (popover.popdown(), fn()))
            box.append(button)
        popover.set_child(box)
        self._popover = popover
        popover.popup()

    def _planned(self) -> list[Change]:
        return [row.change for row in self.rows if row.change.action not in (Action.UNCHANGED, Action.SKIPPED)]

    def _popup(self, row: PlanRow, widget: Gtk.Widget, x: float, y: float) -> None:
        if self._popover is not None:
            self._popover.unparent()
        popover = Gtk.Popover()
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        actions = [
            ("Open left file", "", lambda: self._open_side("left", row.change.rel, folder=False)),
            ("Open right file", "", lambda: self._open_side("right", row.change.rel, folder=False)),
            ("Open left folder", "", lambda: self._open_side("left", row.change.rel, folder=True)),
            ("Open right folder", "", lambda: self._open_side("right", row.change.rel, folder=True)),
            ("Add this name to exclusions", "", lambda: self._add_exclusion(row)),
            ("Apply exclusion mask…", "", lambda: self._ask_exclusion(row)),
            ("Remove this folder from profile", "", lambda: self._exclude_folder(row)),
            ("Ignore for this run", "", lambda: self._ignore(row, extension=False)),
            ("Ignore this extension for this run", "", lambda: self._ignore(row, extension=True)),
            ("Compare files", "", lambda: self._compare(row)),
            ("L ──▶ R", "Copy this file left to right", lambda: self._set_action(row, Action.COPY_LEFT_TO_RIGHT)),
            ("L ◀── R", "Copy this file right to left", lambda: self._set_action(row, Action.COPY_RIGHT_TO_LEFT)),
            ("Leave unchanged", "", lambda: self._set_action(row, Action.UNCHANGED)),
            ("Revert to scanned action", "", lambda: self._set_action(row, None)),
        ]
        for text, tip, callback in actions:
            button = Gtk.Button(label=text)
            if tip:
                button.set_tooltip_text(tip)
                button.add_css_class("monospace")
            button.connect("clicked", lambda _b, fn=callback: (popover.popdown(), fn()))
            box.append(button)
        popover.set_child(box)
        self._popover = popover
        popover.popup()

    def _side_path(self, side: str, rel: str, *, folder: bool) -> Path:
        root = Path(self.job.profile.left if side == "left" else self.job.profile.right)
        path = root / rel
        if folder:
            return path.parent if path.suffix or not path.is_dir() else path
        return path

    def _open_side(self, side: str, rel: str, *, folder: bool) -> None:
        open_uri(self._side_path(side, rel, folder=folder))

    def _add_exclusion(self, row: PlanRow) -> None:
        self._save_exclusion(Path(row.change.rel).name)

    def _ask_exclusion(self, row: PlanRow) -> None:
        window = Gtk.Window(transient_for=self, modal=True, title="Exclusion mask")
        window.set_default_size(520, 180)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        entry = Gtk.Entry(text=Path(row.change.rel).name)
        entry.set_hexpand(True)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text("Saved on this profile. *.tmp matches a name in any folder. A path such as cache/tmp matches that folder and everything under it.")
        hint.add_css_class("dim-label")
        save = Gtk.Button(label="Save")

        def on_save(*_args) -> None:
            self._save_exclusion(entry.get_text())
            window.close()

        save.connect("clicked", on_save)
        box.append(entry)
        box.append(hint)
        box.append(save)
        window.set_child(box)
        window.present()

    def _exclude_folder(self, row: PlanRow) -> None:
        rel = row.change.rel.replace("\\", "/")
        if "/" not in rel:
            self.detail.set_text("This file is in the profile folder. Exclude it by name instead.")
            return
        self._save_exclusion(rel.rsplit("/", 1)[0])

    def _save_exclusion(self, mask: str) -> None:
        mask = mask.strip()
        if not mask:
            return
        profile = self.job.profile
        if mask not in profile.exclusions:
            profile.exclusions.append(mask)
            save_profile(profile)
        self.rows = [item for item in self.rows if not path_matches(item.change.rel, mask)]
        self._refill()
        self.owner.reload()

    def _compare(self, row: PlanRow) -> None:
        left = self._side_path("left", row.change.rel, folder=False)
        right = self._side_path("right", row.change.rel, folder=False)
        if not left.is_file() or not right.is_file():
            self.detail.set_text("Compare needs the file on both sides.")
            return
        command = compare_command()
        parts = shlex.split(command)
        if not parts:
            self.detail.set_text("Set a compare command in Settings.")
            return
        try:
            subprocess.Popen([*parts, str(left), str(right)])
        except OSError as exc:
            self.detail.set_text(f"Could not run {command}: {exc}")

    def _set_action(self, row: PlanRow, action: Action | None) -> None:
        listed = next((item for item in self.rows if item.change.rel == row.change.rel), None)
        if listed is None:
            return
        original = self._original.get(row.change.rel, listed.change)
        if action is None:
            listed.change = original
        else:
            listed.change = Change(
                listed.change.rel, action, "chosen in preview", listed.change.size, listed.change.dest_rel
            )
        self._refill()

    def _set_folder_direction(self, rel: str, toward: str) -> None:
        prefix = rel.strip("/")
        if not prefix:
            return
        prefix += "/"
        for item in self.rows:
            path = item.change.rel
            if path == rel or path.startswith(prefix):
                item.change = point_change(item.change, toward)
        self._refill()

    def _ignore_folder(self, rel: str) -> None:
        folder = rel.strip().strip("/")
        if not folder:
            return
        prefix = folder + "/"
        gone = [
            item.change.rel
            for item in self.rows
            if item.change.rel == folder or item.change.rel.startswith(prefix)
        ]
        if not gone:
            return
        masks = self.owner.session_masks.setdefault(self.job.profile.name, [])
        if folder not in masks:
            masks.append(folder)
        drop = set(gone)
        self.rows = [item for item in self.rows if item.change.rel not in drop]
        self._refill()
        noun = "file" if len(gone) == 1 else "files"
        self.detail.set_text(f"Ignored {len(gone)} {noun} under {folder} for this run.")

    def _ignore(self, row: PlanRow, *, extension: bool) -> None:
        name = self.job.profile.name
        if extension:
            suffix = Path(row.change.rel).suffix
            if not suffix:
                return
            mask = f"*{suffix}"
            masks = self.owner.session_masks.setdefault(name, [])
            if mask not in masks:
                masks.append(mask)
            self.rows = [item for item in self.rows if Path(item.change.rel).suffix != suffix]
        else:
            self.owner.session_skips.setdefault(name, set()).add(row.change.rel)
            self._drop(row.change.rel)
            return
        self._refill()

    def _drop(self, rel: str) -> None:
        self.rows = [item for item in self.rows if item.change.rel != rel]
        self._refill()

    def _refresh_detail(self) -> None:
        planned = self._planned()
        left, right = status_lines(planned)
        self.left_status.set_text(left)
        self.right_status.set_text(right)
        visible = [self.store.get_item(index) for index in range(self.store.get_n_items())]
        self.shown.set_text(shown_summary([Listed(item.change, False, False) for item in visible]))
        text = plan_summary(planned)
        if self.job.result is not None and self.job.result.note:
            text += "  " + self.job.result.note
        self.detail.set_text(
            text + "  Filters hide rows. Start runs the whole plan. Right-click a folder to point L ──▶ R or L ◀── R."
        )

    def _start(self, *_args) -> None:
        if self._running:
            return
        changes = self._planned()
        if not changes:
            self.detail.set_text("Nothing to copy, move, or delete. Recording the run.")
            self._begin([], "empty run")
            return
        warning = safety_text(self.job.profile, self.rows)
        if warning is None:
            self._begin(changes, "no deletions")
            return
        self._safety(warning, changes, blocked=bool(deletion_violations(self.job.profile, self.rows)))

    def _safety(self, text: str, changes: list[Change], *, blocked: bool = False) -> Gtk.Window:
        window = Gtk.Window(transient_for=self, modal=True, title="Safety Confirmations Required")
        window.set_default_size(760, 520)
        choice: dict[str, str | None] = {"value": None}
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(view)
        remaining = {"n": SAFETY_SECONDS}
        countdown = Gtk.Label(xalign=0)
        countdown.set_text(f"Continues without deletions in {remaining['n']} seconds.")
        state = {"id": 0}

        def stop_timer() -> None:
            timer_id = state["id"]
            state["id"] = 0
            if timer_id:
                GLib.source_remove(timer_id)

        def tick() -> bool:
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                state["id"] = 0
                choice["value"] = "timeout"
                window.close()
                return False
            countdown.set_text(f"Continues without deletions in {remaining['n']} seconds.")
            return True

        def finish(*_args) -> bool:
            stop_timer()
            picked = choice["value"]
            if picked == "confirm":
                self._begin(changes, "confirm deletions")
            elif picked in ("without", "timeout"):
                self._begin(without_deletes(changes), "continue without deletions")
            return False

        def choose(name: str) -> None:
            choice["value"] = name
            window.close()

        confirm = Gtk.Button(label="Confirm Deletions")
        confirm.set_sensitive(not blocked)
        if blocked:
            confirm.set_tooltip_text("A profile deletion safeguard blocks this plan. Continue without deletions or change the profile.")
        confirm.connect("clicked", lambda *_: choose("confirm"))
        skip = Gtk.Button(label="Continue Without Deletions")
        skip.connect("clicked", lambda *_: choose("without"))
        stop = Gtk.Button(label="Do Not Continue")
        stop.connect("clicked", lambda *_: choose("stop"))
        buttons = Gtk.Box(spacing=8)
        buttons.append(confirm)
        buttons.append(skip)
        buttons.append(stop)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_start(12)
        root.set_margin_end(12)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.append(scrolled)
        root.append(countdown)
        root.append(buttons)
        window.set_child(root)
        window.set_default_widget(skip)
        window.connect("close-request", finish)
        state["id"] = GLib.timeout_add_seconds(1, tick)
        window.present()
        skip.grab_focus()
        return window

    def _begin(self, changes: list[Change], choice: str) -> None:
        self._running = True
        self.start_button.set_sensitive(False)
        progress = ProgressWindow(self, self.job.profile.name)
        progress.present()

        def work() -> None:
            report = ApplyReport()
            path = None
            try:
                report = apply_plan(
                    self.job.profile,
                    changes,
                    control=progress.control,
                    on_copy_progress=lambda update: GLib.idle_add(progress.update, update),
                    bytes_per_second=copy_speed_limit(),
                    listing=self.rows,
                )
            except Exception as exc:
                report.errors.append(str(exc))
            try:
                remember(self.job.profile)
            except Exception as exc:
                report.errors.append(f"history: {exc}")
            path = None
            if changes:
                try:
                    path = write_log(self.job.profile, log_body(self.job.profile, changes, report, choice=choice))
                except Exception as exc:
                    report.errors.append(f"log: {exc}")
            GLib.idle_add(self._finished, progress, report, path)

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, progress: ProgressWindow, report: ApplyReport, path: Path | None) -> bool:
        progress.allow_close()
        self._running = False
        text = report.summary()
        self.job.profile.last_result = f"{time.strftime('%Y-%m-%d %H:%M')}  {text}"
        save_profile(self.job.profile)
        self.owner.reload()
        self.detail.set_text(text)
        ResultsWindow(self, report, path).present()
        return False


class ScanWindow(Gtk.Window):
    """Live scan statistics, pause, per-profile skip, and whole-scan cancel."""

    def __init__(self, owner: Gtk.Window, title: str = "Scanning", *, estimates: dict[str, tuple[int, float]] | None = None):
        super().__init__(transient_for=owner, modal=False, title=title)
        self.set_default_size(640, 260)
        self.control = RunControl()
        self._allow_close = False
        self._place = "Starting…"
        self._name = ""
        self._started = time.monotonic()
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._estimates = estimates or {}
        self._estimate = (0, 0.0)
        self._stats = {side: {"discovered": 0, "filtered": 0, "added": 0} for side in ("Left", "Right")}
        self.file_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.file_label.set_text(self._place)
        self.count_label = Gtk.Label(xalign=0)
        self._update_counts()
        self.bar = Gtk.ProgressBar()
        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.connect("clicked", self._toggle)
        self.skip_button = Gtk.Button(label="Skip Rest")
        self.skip_button.set_tooltip_text("Skip this profile's incomplete scan and continue with the next profile")
        self.skip_button.connect("clicked", self._skip)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel)
        buttons = Gtk.Box(spacing=8)
        buttons.append(self.pause_button)
        buttons.append(self.skip_button)
        buttons.append(self.cancel_button)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_start(12)
        root.set_margin_end(12)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.append(self.file_label)
        root.append(self.count_label)
        root.append(self.bar)
        root.append(buttons)
        self.set_child(root)
        self.connect("close-request", self._cancel_close)
        GLib.timeout_add(120, self._pulse)

    def _elapsed(self) -> float:
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0.0, now - self._started - self._paused_total)

    def _update_counts(self) -> None:
        discovered = sum(values["discovered"] for values in self._stats.values())
        filtered = sum(values["filtered"] for values in self._stats.values())
        added = sum(values["added"] for values in self._stats.values())
        elapsed = self._elapsed()
        speed = int(discovered / elapsed) if elapsed else 0
        eta = _estimate_eta(discovered, elapsed, self._estimate)
        eta_text = f"ETA ~{_duration(eta)}" if eta is not None else "ETA after first completed scan"
        self.count_label.set_text(
            f"Folders and files found: {discovered}\nFiltered (ignored): {filtered}\nAdded to file list: {added}\n"
            f"Elapsed {_duration(elapsed)} · {speed} entries/s · {eta_text}"
        )

    def _new_profile(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self._estimate = self._estimates.get(name, (0, 0.0))
            self._started = time.monotonic()
            self._paused_total = 0.0
            self._paused_at = self._started if self.control.paused else None
            self.skip_button.set_sensitive(True)
            self._stats = {side: {"discovered": 0, "filtered": 0, "added": 0} for side in ("Left", "Right")}

    def _update_location(self, name: str, side: str, rel: str) -> None:
        self._new_profile(name)
        reading = "Reading right" if side == "Right" else "Reading left"
        self._place = f"{name}    {reading}    {rel or '.'}"
        if not self.control.paused:
            self.file_label.set_text(self._place)

    def update(self, name: str, side: str, rel: str, count: int) -> bool:
        self._update_location(name, side, rel)
        values = self._stats[side]
        values["discovered"] = count
        values["added"] = count
        self._update_counts()
        return False

    def update_stats(self, name: str, side: str, rel: str, values) -> bool:
        self._update_location(name, side, rel)
        self._stats[side] = {
            "discovered": values.discovered,
            "filtered": values.filtered,
            "added": values.added,
        }
        self._update_counts()
        return False

    def _pulse(self) -> bool:
        if self._allow_close:
            return False
        self.bar.pulse()
        self._update_counts()
        return True

    def _toggle(self, *_args) -> None:
        if self.control.paused:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self.control.resume()
            self.pause_button.set_label("Pause")
            self.file_label.set_text(self._place)
        else:
            self._paused_at = time.monotonic()
            self.control.pause()
            self.pause_button.set_label("Resume")
            self.file_label.set_text("Paused. " + self._place)

    def _skip(self, *_args) -> None:
        self.control.skip_current()
        self.skip_button.set_sensitive(False)
        self.file_label.set_text("Skipping rest of current profile…")

    def _cancel(self, *_args) -> None:
        self.control.stop()
        self.cancel_button.set_sensitive(False)
        self.pause_button.set_sensitive(False)
        self.skip_button.set_sensitive(False)
        self.file_label.set_text("Canceling…")

    def allow_close(self) -> None:
        self._allow_close = True
        self.close()

    def _cancel_close(self, *_args) -> bool:
        if self._allow_close:
            return False
        self._cancel()
        return True


def _estimate_eta(discovered: int, elapsed: float, estimate: tuple[int, float]) -> float | None:
    """Previous duration initially, then a live-pace estimate after ten entries."""

    expected_entries, previous_seconds = estimate
    if expected_entries <= 0 or previous_seconds <= 0:
        return None
    if discovered < 10 or elapsed <= 0:
        return max(0.0, previous_seconds - elapsed)
    remaining = max(0, expected_entries - discovered)
    return remaining * elapsed / discovered


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


class ProgressWindow(Gtk.Window):
    def __init__(self, owner: Preview, title: str):
        super().__init__(transient_for=owner, modal=True, title=f"Copying {title}")
        self.set_default_size(680, 300)
        self.control = RunControl()
        self._allow_close = False
        self._started = time.monotonic()
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self.latest: CopyProgress | None = None
        self.file_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.file_label.set_text("Starting…")
        self.file_bar = Gtk.ProgressBar(show_text=True)
        self.file_count = Gtk.Label(xalign=0)
        self.count_label = Gtk.Label(xalign=0)
        self.bar = Gtk.ProgressBar(show_text=True)
        self.timing_label = Gtk.Label(xalign=0)
        self.timing_label.add_css_class("monospace")
        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.connect("clicked", self._toggle)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel)
        buttons = Gtk.Box(spacing=8)
        buttons.append(self.pause_button)
        buttons.append(self.cancel_button)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(root, f"set_margin_{side}")(12)
        root.append(self.file_label)
        root.append(self.file_bar)
        root.append(self.file_count)
        root.append(self.bar)
        root.append(self.count_label)
        root.append(self.timing_label)
        root.append(buttons)
        self.set_child(root)
        self.connect("close-request", self._cancel_close)
        self._show_timing()
        GLib.timeout_add(250, self._tick)

    def _elapsed(self) -> float:
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0.0, now - self._started - self._paused_total)

    def _tick(self) -> bool:
        if self._allow_close:
            return False
        self._show_timing()
        return True

    def _show_timing(self) -> None:
        elapsed = self._elapsed()
        if self.latest is None:
            self.timing_label.set_text(f"Elapsed {_duration(elapsed)} · Speed 0 B/s · ETA --:--")
            return
        speed = self.latest.bytes_done / elapsed if elapsed > 0 and self.latest.bytes_done else 0.0
        remaining = max(0, self.latest.bytes_total - self.latest.bytes_done)
        eta = f"ETA {_duration(remaining / speed)}" if speed > 0 and remaining else "ETA --:--"
        state = "Paused · " if self.control.paused else ""
        self.timing_label.set_text(
            f"{state}Elapsed {_duration(elapsed)} · Speed {format_bytes(int(speed))}/s · {eta}"
        )

    def update(self, event: CopyProgress) -> bool:
        self.latest = event
        self.bar.set_fraction(event.overall_fraction)
        self.file_bar.set_fraction(event.current_fraction)
        prefix = "Paused. " if self.control.paused else ""
        self.file_label.set_text(prefix + (event.rel or "Finishing…"))
        self.bar.set_tooltip_text(f"Overall: {format_bytes(event.bytes_done)} / {format_bytes(event.bytes_total)}")
        self.file_bar.set_tooltip_text(f"Current file: {format_bytes(event.current_bytes)} / {format_bytes(event.current_total)}")
        self.file_count.set_text(f"Current file: {format_bytes(event.current_bytes)} / {format_bytes(event.current_total)}")
        self.count_label.set_text(
            f"Operation {event.completed} / {event.total} · Processed {format_bytes(event.bytes_done)} / {format_bytes(event.bytes_total)}\n"
            f"Copied {event.copied} · Deleted {event.deleted} · Moved {event.moved} · Errors {event.errors}"
        )
        self._show_timing()
        return False

    def _toggle(self, *_args) -> None:
        if self.control.paused:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self.control.resume()
            self.pause_button.set_label("Pause")
        else:
            self._paused_at = time.monotonic()
            self.control.pause()
            self.pause_button.set_label("Resume")
        if self.latest is not None:
            self.update(self.latest)
        self._show_timing()

    def _cancel(self, *_args) -> None:
        self.control.stop()
        self.cancel_button.set_sensitive(False)
        self.pause_button.set_sensitive(False)
        self.file_label.set_text("Canceling…")

    def allow_close(self) -> None:
        self._allow_close = True
        self.close()

    def _cancel_close(self, *_args) -> bool:
        if self._allow_close:
            return False
        self._cancel()
        return True


class ResultsWindow(Gtk.Window):
    def __init__(self, owner: Preview, report: ApplyReport, log_path: Path | None):
        super().__init__(transient_for=owner, modal=True, title="Copying Results")
        _install_css()
        self.owner = owner
        self.profile_name = owner.job.profile.name
        self.connect("close-request", self._close_preview)
        self.set_default_size(640, 320)
        self.log_path = log_path
        headline = Gtk.Label(xalign=0)
        headline.set_text(outcome_headline(report))
        headline.add_css_class("banner")
        grid = Gtk.Grid(column_spacing=24, row_spacing=6)
        left_head = Gtk.Label(xalign=0)
        left_head.set_markup("<b>Left</b>")
        right_head = Gtk.Label(xalign=0)
        right_head.set_markup("<b>Right</b>")
        grid.attach(left_head, 1, 0, 1, 1)
        grid.attach(right_head, 2, 0, 1, 1)
        for row_index, (label, left, right) in enumerate(result_rows(report), start=1):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row_index, 1, 1)
            grid.attach(self._count(left), 1, row_index, 1, 1)
            grid.attach(self._count(right), 2, row_index, 1, 1)
        ok = Gtk.Button(label="OK")
        ok.connect("clicked", lambda *_: self.close())
        view = Gtk.Button(label="View Log…")
        view.set_sensitive(log_path is not None and log_path.exists())
        view.connect("clicked", self._view_log)
        retry = Gtk.Button(label="Retry Failed…")
        retry.set_sensitive(bool(report.errors))
        retry.set_tooltip_text("Confirm, rescan the current folders, and build a fresh preview")
        retry.connect("clicked", self._retry)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        buttons.append(ok)
        buttons.append(retry)
        buttons.append(view)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_start(16)
        root.set_margin_end(16)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.append(headline)
        root.append(grid)
        if report.errors:
            errors = Gtk.Label(label="\n".join(report.errors), xalign=0, wrap=True)
            errors.add_css_class("error")
            root.append(errors)
        root.append(buttons)
        self.set_child(root)

    def _close_preview(self, *_args) -> bool:
        """One close is enough: neither the preview nor the progress window stay behind."""

        self.owner.close()
        return False

    def _retry(self, *_args) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message("Retry failed operations?")
        dialog.set_detail(
            f"OFS will rescan {self.profile_name} and show a fresh preview. "
            "The previous plan is not replayed; already successful operations should disappear."
        )
        dialog.set_buttons(["Cancel", "Rescan and Retry"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._retry_chosen)

    def _retry_chosen(self, dialog, result) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        owner = self.owner
        preview_owner = owner.owner
        self.close()
        preview_owner._run_names({self.profile_name})

    def _count(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        if text and not text.startswith("0"):
            label.add_css_class("error")
        return label

    def _view_log(self, *_args) -> None:
        if self.log_path is None:
            return
        window = Gtk.Window(transient_for=self, title=self.log_path.name)
        window.set_default_size(760, 480)
        view = Gtk.TextView(editable=False, monospace=True)
        view.get_buffer().set_text(self.log_path.read_text(encoding="utf-8"))
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(view)
        window.set_child(scrolled)
        window.present()


class CheckedReplacementWindow(Gtk.Window):
    """Live, read-only previews for checked profile path or name replacements."""

    def __init__(self, owner: MainWindow, profiles: list[Profile], operation: str):
        names = operation == "names"
        super().__init__(transient_for=owner, modal=True, title="Rename checked profiles" if names else "Replace checked profile paths")
        self.owner = owner
        self.profiles = profiles
        self.operation = operation
        self.set_default_size(720, 480)
        self.find = Gtk.Entry(hexpand=True, placeholder_text="Old profile text" if names else "Old mount point or folder, for example /mnt/old-disk")
        self.replace = Gtk.Entry(hexpand=True, placeholder_text="New profile text" if names else "New mount point or folder, for example /mnt/new-disk")
        self.scope = Gtk.DropDown.new_from_strings(["Left and Right paths", "Left only", "Right only"]) if not names else None
        self.sides = ["both", "left", "right"]
        self.prefix = Gtk.CheckButton(label="Only match at the beginning of the name" if names else "Only replace a leading folder path (not a partial folder name)")
        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.items.set_margin_bottom(8)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=180)
        scroll.set_child(self.items)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text(
            "Only profile names change. Paths, masks, and other settings are preserved. The old file database is removed because it belongs to the old name."
            if names
            else "Only profile configuration changes; folders or volumes are not moved on disk. Preview every affected side before confirming. The old file database is removed. Volume labels are unchanged."
        )
        hint.add_css_class("dim-label")
        self.review = Gtk.Button(label="Review Changes…")
        self.review.add_css_class("suggested-action")
        self.review.connect("clicked", self._review)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(root, f"set_margin_{side}")(12)
        for label, widget in (("Find", self.find), ("Replace with", self.replace)):
            root.append(Gtk.Label(label=label, xalign=0))
            root.append(widget)
        if self.scope is not None:
            root.append(self.scope)
            self.scope.connect("notify::selected", lambda *_: self._refresh())
        root.append(self.prefix)
        root.append(self.summary)
        root.append(scroll)
        root.append(hint)
        root.append(self.review)
        self.set_child(root)
        self.find.connect("changed", lambda *_: self._refresh())
        self.replace.connect("changed", lambda *_: self._refresh())
        self.prefix.connect("toggled", lambda *_: self._refresh())
        self._refresh()

    def _changes(self) -> list[tuple[Profile, dict[str, str]]]:
        if self.operation == "paths":
            sides = self.sides[self.scope.get_selected()]
            prefix = self.prefix.get_active()
            return [
                (profile, updates)
                for profile in self.profiles
                if (updates := path_replacements(profile, self.find.get_text(), self.replace.get_text(), sides=sides, prefix_only=prefix))
            ]
        renamed = name_replacements(
            self.profiles,
            self.find.get_text(),
            self.replace.get_text(),
            prefix_only=self.prefix.get_active(),
        )
        validate_name_replacements(renamed, self.owner.profiles)
        names = dict(renamed)
        return [(profile, {"name": names[profile.name]}) for profile in self.profiles if profile.name in names]

    def _refresh(self) -> None:
        while child := self.items.get_first_child():
            self.items.remove(child)
        if not self.find.get_text():
            self.summary.set_text(f"{len(self.profiles)} checked profiles. Enter text to replace.")
            return
        try:
            changes = self._changes()
        except ValueError as exc:
            self.summary.set_text(str(exc))
            return
        for profile, updates in changes:
            for field, updated in updates.items():
                original = getattr(profile, field)
                label = "Name" if field == "name" else field.title()
                row = Gtk.Label(xalign=0, wrap=True, selectable=True)
                row.set_text(f"{profile.name} · {label}\n{original}\n→ {updated}")
                self.items.append(row)
        self.summary.set_text(f"{len(changes)} of {len(self.profiles)} checked profiles would change. Every changed side is listed below.")

    def _review(self, *_args) -> None:
        try:
            changes = self._changes()
        except ValueError as exc:
            self.summary.set_text(str(exc))
            return
        if not changes:
            self.summary.set_text("No profile would change with these options.")
            return
        if self.operation == "names":
            self.owner._confirm_name_replacements([(profile.name, changes_by_side["name"]) for profile, changes_by_side in changes])
        else:
            self.owner._confirm_path_replacements(changes)
        self.close()


class BulkEditWindow(Gtk.Window):
    """Change one field; show the exact profile/value set before saving."""

    def __init__(self, owner: MainWindow, profiles: list[Profile]):
        super().__init__(transient_for=owner, modal=True, title=f"Multi Edit — {len(profiles)} checked profiles")
        self.owner = owner
        self.profiles = profiles
        self.set_default_size(640, 310)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(root, f"set_margin_{side}")(12)
        self.field = Gtk.DropDown.new_from_strings([item.label for item in FIELDS])
        self.field.connect("notify::selected", lambda *_: self._show_value())
        self.value_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.value_widget: Gtk.Widget | None = None
        self.mask_operation: Gtk.DropDown | None = None
        self.preview = Gtk.Label(xalign=0, wrap=True)
        self.preview.set_text(f"{len(profiles)} checked profiles. Choose one field to change; other fields are preserved.")
        self.apply = Gtk.Button(label="Preview Changes…")
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self._preview_changes)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text("Masks can be added to each profile without removing existing rules, or can replace that profile's whole list. Confirmation is required before anything is saved.")
        hint.add_css_class("dim-label")
        for label, widget in (("Field to change", self.field), ("New value", self.value_box)):
            root.append(Gtk.Label(label=label, xalign=0))
            root.append(widget)
        root.append(self.preview)
        root.append(hint)
        root.append(self.apply)
        self.set_child(root)
        self._show_value()

    def _show_value(self) -> None:
        while child := self.value_box.get_first_child():
            self.value_box.remove(child)
        definition = FIELDS[self.field.get_selected()]
        self.mask_operation = None
        if definition.kind == "choice":
            self.value_widget = Gtk.DropDown.new_from_strings([label for label, _value in definition.options])
        elif definition.kind == "number":
            self.value_widget = Gtk.SpinButton.new_with_range(definition.minimum, definition.maximum, 1)
            self.value_widget.set_value(definition.minimum)
        else:
            self.value_widget = Gtk.Entry(hexpand=True)
            self.value_widget.set_placeholder_text("One or several masks separated by ; or newline")
            self.mask_operation = Gtk.DropDown.new_from_strings(["Add to existing masks", "Replace existing masks"])
            self.value_box.append(self.mask_operation)
        self.value_box.append(self.value_widget)

    def _preview_changes(self, *_args) -> None:
        definition = FIELDS[self.field.get_selected()]
        if definition.kind == "choice":
            assert isinstance(self.value_widget, Gtk.DropDown)
            value = definition.options[self.value_widget.get_selected()][1]
        elif definition.kind == "number":
            assert isinstance(self.value_widget, Gtk.SpinButton)
            value = self.value_widget.get_value_as_int()
        else:
            assert isinstance(self.value_widget, Gtk.Entry)
            value = self.value_widget.get_text()
        add_masks = self.mask_operation is not None and self.mask_operation.get_selected() == 0
        try:
            changed = prepare_bulk_edit(self.profiles, definition.key, value, add_masks=add_masks)
        except ValueError as exc:
            self.preview.set_text(str(exc))
            return
        if not changed:
            self.preview.set_text("No profile would change.")
            return
        self.owner._confirm_bulk_edit(changed, definition.key, self)


class RestoreWindow(Gtk.Window):
    """Recover selected Older versions into a separate, explicitly chosen tree."""

    def __init__(self, owner: MainWindow, profile: Profile):
        super().__init__(transient_for=owner, modal=True, title=f"Restore Older — {profile.name}")
        self.profile = profile
        self.entries = older_files(profile)
        self.set_default_size(760, 580)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.destination = Gtk.Entry(hexpand=True, placeholder_text="Restore destination outside both synchronized folders")
        self.rows: list[tuple[Gtk.CheckButton, object]] = []
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for item in self.entries:
            date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.modified))
            check = Gtk.CheckButton(label=f"{item.label} · {format_bytes(item.size)} · {date}")
            check.set_hexpand(True)
            listing.append(check)
            self.rows.append((check, item))
        select_all = Gtk.CheckButton(label="Select all archived files")
        select_all.connect("toggled", self._select_all)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(listing)
        restore = Gtk.Button(label="Restore selected")
        restore.add_css_class("suggested-action")
        restore.connect("clicked", self._restore)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text("Copies the selected archived versions into the destination. Existing files there are not overwritten; the Older archive and live sync folders stay unchanged.")
        hint.add_css_class("dim-label")
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(root, f"set_margin_{side}")(12)
        if not self.entries:
            self.status.set_text("No archived files. Enable Older or Recoverable deletions in the profile before a run that needs them.")
        root.append(self.status)
        root.append(scroll)
        root.append(select_all)
        root.append(hint)
        root.append(self.destination)
        root.append(restore)
        self.set_child(root)

    def _select_all(self, button: Gtk.CheckButton) -> None:
        for check, _item in self.rows:
            check.set_active(button.get_active())

    def _restore(self, *_args) -> None:
        destination_text = self.destination.get_text().strip()
        if not destination_text:
            self.status.set_text("Choose a restore folder outside both synchronized folders first.")
            return
        selected = [item for check, item in self.rows if check.get_active()]
        if not selected:
            self.status.set_text("Select at least one archived file to restore.")
            return
        restored = 0
        errors = []
        for item in selected:
            try:
                restore_version(item, Path(destination_text).expanduser())
                restored += 1
            except (OSError, ValueError) as exc:
                errors.append(f"{item.relative}: {exc}")
        if errors:
            self.status.set_text(f"Restored {restored}. Errors: " + " | ".join(errors[:8]))
        else:
            self.status.set_text(f"Restored {restored} file(s) to {destination_text}. The originals remain under Older.")


class SfsApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=os.environ.get("OFS_APP_ID") or os.environ.get("SFS_APP_ID") or "com.ofs.LocalSync")

    def do_activate(self) -> None:
        apply_appearance(theme())
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
        window.present()


def main() -> int:
    app = SfsApplication()
    return app.run(None)
