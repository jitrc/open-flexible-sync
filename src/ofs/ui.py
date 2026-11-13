"""GTK 4 window: profile groups, a dry-run scan, and the preview actions."""

from __future__ import annotations

import math
import os
import re
import shlex
import subprocess
import threading
import time
from collections import Counter, deque
from pathlib import Path, PurePosixPath

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from ofs.appearance import appearance_is_dark, apply_appearance
from ofs.apply import ApplyReport, CopyProgress, RunControl, apply_plan
from ofs.archive import older_files, restore_version
from ofs.bulk import FIELDS, bulk_preview, prepare_bulk_edit
from ofs.cache import destination_sides
from ofs.compare import Action, Change, comparison_rule_text, format_bytes, plan_summary
from ofs.jobs import JobResult, run_checked
from ofs.folder_copy import FolderCopyOutcome, copy_new_folder
from ofs.history import delete_history
from ofs.legacy import import_cfg, import_global_masks
from ofs.masks import any_match, included, path_matches, split_masks
from ofs.model import Direction, ExclusionMode, Profile, SizeConflict, SymlinkMode, SyncMode, TimestampRules, parse_size
from ofs.power import PowerActionError, run_completion_action
from ofs.run_history import list_history, record as record_run
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
from ofs.scan import ScanResult, invalidate_cached_destinations, remember
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


def _group_parts(profile: Profile) -> tuple[str, ...]:
    custom = tuple(part.strip() for part in profile.group_override.strip().strip("/").split("/") if part.strip())
    if custom:
        return custom
    return (profile.group,) if profile.group else ()


def _fixed_path_label(text: str) -> Gtk.Label:
    label = Gtk.Label(xalign=0, selectable=True, wrap=False, lines=1, width_chars=52, max_width_chars=52)
    label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    label.set_tooltip_text(text)
    label.set_text(text)
    return label


def _path_line(caption: str, label: Gtk.Label) -> Gtk.Box:
    """One compact row for the current path; no card, fixed height, or extra line."""

    row = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
    row.add_css_class("scan-path")
    heading = Gtk.Label(label=caption, xalign=0, valign=Gtk.Align.CENTER, width_chars=12)
    heading.add_css_class("dim-label")
    heading.set_tooltip_text("The entry being read or copied right now")
    label.set_hexpand(True)
    row.append(heading)
    row.append(label)
    return row


def _button_icon(button, label: str, icon_name: str, *, icon_only: bool = False) -> None:
    """Use a symbolic icon without losing the action's accessible name."""

    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    if icon_only:
        button.set_icon_name(icon_name)
        return
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_icon_size(Gtk.IconSize.NORMAL)
    content = Gtk.Box(spacing=6)
    content.append(image)
    content.append(Gtk.Label(label=label))
    button.set_child(content)


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
_UNGROUPED = "(ungrouped)"
_UNGROUPED_PROFILE_MINIMUM = 12
_UNGROUPED_GROUP_MINIMUM = 3
_PREVIEW_BATCH_SIZE = 250
_PREVIEW_ASYNC_THRESHOLD = 1000


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="OFS")
        self._ofs_app = app
        self.set_default_size(1100, 740)
        self.profiles: list[Profile] = []
        self.checked: set[str] = set()
        self.session_skips: dict[str, set[str]] = {}
        self.session_masks: dict[str, list[str]] = {}
        self._session_rule_locks: dict[str, threading.RLock] = {}
        self._labels: dict[str, Gtk.Label] = {}
        self._group_sizes: Counter = Counter()
        self._group_expanded: dict[str, bool] = {}
        self._applying_groups = False
        self._scan_running: set[str] = set()
        self._scan_batches: dict[int, dict] = {}
        self._scan_previews: dict[str, Preview] = {}
        self._live_completed: dict[str, Preview] = {}
        self._next_scan_batch = 0
        self._folder_copy_running: set[str] = set()
        self._path_available: dict[str, bool] = {}
        self._visible_count = 0
        self._profile_scroll_generation = 0
        self._focused_profile: str | None = None
        self._profile_rows: dict[str, Gtk.Widget] = {}
        self._profile_check_buttons: dict[str, Gtk.CheckButton] = {}
        self._profile_return_pending = False
        self._updating_appearance = False

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        new_button = Gtk.Button()
        _button_icon(new_button, "New profile", "document-new-symbolic")
        new_button.connect("clicked", self._new_profile)
        restore_button = Gtk.Button()
        _button_icon(restore_button, "Restore Older…", "document-revert-symbolic")
        restore_button.set_tooltip_text("Restore an archived file to a separate folder")
        restore_button.connect("clicked", self._restore_older)
        history_button = Gtk.Button()
        _button_icon(history_button, "History", "document-open-recent-symbolic")
        history_button.set_tooltip_text("Show scan and copy history across all profiles")
        history_button.connect("clicked", self._run_history)
        self.profiles_menu = Gtk.MenuButton()
        _button_icon(self.profiles_menu, "Profiles", "open-menu-symbolic")
        self.profiles_menu.set_tooltip_text("Import, export, and edit checked profiles")
        popover = Gtk.Popover()
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        for side in ("start", "end", "top", "bottom"):
            getattr(choices, f"set_margin_{side}")(8)
        new_window = Gtk.Button(label="New window")
        new_window.connect("clicked", lambda _button: (popover.popdown(), self._new_window()))
        choices.append(new_window)
        choices.append(Gtk.Separator())
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
            ("Copy new folder…", self._copy_new_folder),
            ("Multi Edit checked profiles…", self._multi_edit),
            ("Replace paths in checked profiles…", self._replace_paths),
            ("Rename checked profiles…", self._rename_checked),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, fn=callback: (popover.popdown(), fn()))
            choices.append(button)
        popover.set_child(choices)
        self.profiles_menu.set_popover(popover)
        self.appearance_button = Gtk.ToggleButton()
        _button_icon(self.appearance_button, "Toggle dark mode", "weather-clear-symbolic", icon_only=True)
        self.appearance_button.connect("toggled", self._toggle_appearance)
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            gtk_settings.connect("notify::gtk-application-prefer-dark-theme", lambda *_: self._refresh_appearance())
            gtk_settings.connect("notify::gtk-theme-name", lambda *_: self._refresh_appearance())
        self._refresh_appearance()
        self.run_button = Gtk.Button()
        _button_icon(self.run_button, "Run checked", "media-playback-start-symbolic")
        self.run_button.set_tooltip_text("Scan the checked profiles")
        self.run_button.add_css_class("run")
        self.run_button.connect("clicked", self._run)
        self.uncached_button = Gtk.Button()
        _button_icon(self.uncached_button, "Run uncached", "view-refresh-symbolic")
        self.uncached_button.set_tooltip_text("Freshly scan checked profiles without destination caches; saved cache settings stay enabled")
        self.uncached_button.connect("clicked", self._run_uncached)
        settings_button = Gtk.Button()
        _button_icon(settings_button, "Settings", "emblem-system-symbolic", icon_only=True)
        settings_button.set_tooltip_text("Settings")
        settings_button.connect("clicked", self._settings)
        header.pack_start(new_button)
        header.pack_start(self.profiles_menu)
        header.pack_start(restore_button)
        header.pack_start(history_button)
        header.pack_end(self.run_button)
        header.pack_end(self.uncached_button)
        header.pack_end(settings_button)
        header.pack_end(self.appearance_button)

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
        self.profiles_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.profiles_scroll.set_child(self.list_box)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(tools)
        root.append(filters)
        self.column_headers = self._column_headers()
        root.append(self.column_headers)
        root.append(self.profiles_scroll)
        root.append(self.status)
        self.set_child(root)
        _install_css()
        self.reload()

    def _profile_anchor(self) -> tuple[str | None, float | None]:
        name = self._focused_profile
        row = self._profile_rows.get(name)
        if row is None or not row.get_mapped():
            return name, None
        point = row.translate_coordinates(self.list_box, 0, 0)
        return name, point[1] if point is not None else None

    def reload(self) -> None:
        adjustment = self.profiles_scroll.get_vadjustment()
        saved_scroll = adjustment.get_value()
        profile, old_offset = self._profile_anchor()
        self._profile_scroll_generation += 1
        generation = self._profile_scroll_generation
        self._profile_restore_pending = True
        self.profiles = load_profiles()
        self._path_available.clear()
        self._rebuild()
        folder = config_dir() / "profiles"
        self.status.set_text(f"{len(self.profiles)} profiles in {folder}")
        # GTK resizes/allocates rebuilt children at a higher priority than this restore.
        GLib.idle_add(
            self._restore_profile_scroll, generation, saved_scroll, profile, old_offset,
            priority=GLib.PRIORITY_LOW,
        )

    def _restore_profile_scroll(
        self, generation: int, saved_scroll: float, profile: str | None, old_offset: float | None,
    ) -> bool:
        if generation != self._profile_scroll_generation:
            return False
        adjustment = self.profiles_scroll.get_vadjustment()
        maximum = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        requested = saved_scroll
        row = self._profile_rows.get(profile)
        if row is not None and row.get_mapped() and old_offset is not None:
            point = row.translate_coordinates(self.list_box, 0, 0)
            if point is not None:
                requested += point[1] - old_offset
        adjustment.set_value(min(max(0.0, requested), maximum))
        self._profile_restore_pending = False
        if self._profile_return_pending:
            self._profile_return_pending = False
            check = self._profile_check_buttons.get(self._focused_profile)
            if check is not None and check.get_mapped():
                check.grab_focus()
            # Focus scrolling reveals only the checkbox, which can leave the
            # rest of the finished profile's row below the viewport.
            self._scroll_profile_into_view(self._profile_rows.get(self._focused_profile))
        return False

    def _scroll_profile_into_view(self, row: Gtk.Widget | None) -> None:
        """Move as little as needed so the whole profile row is readable."""

        if row is None or not row.get_mapped():
            return
        adjustment = self.profiles_scroll.get_vadjustment()
        page = adjustment.get_page_size()
        point = row.translate_coordinates(self.list_box, 0, 0)
        if page <= 0 or point is None:
            return
        top = point[1] - adjustment.get_value()
        height = row.get_height()
        value = adjustment.get_value()
        if top < 0:
            value += top
        elif top + height > page:
            value += top + height - page
        else:
            return
        maximum = max(0.0, adjustment.get_upper() - page)
        adjustment.set_value(min(max(0.0, value), maximum))

    def _return_to_profile(self, name: str) -> None:
        self._focused_profile = name
        if not self.get_visible():
            return
        self._profile_return_pending = True
        self.present()
        if not self._profile_restore_pending:
            self.reload()

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

    def _is_grouped(self, profile: Profile) -> bool:
        return bool(profile.group_override.strip()) or self._group_sizes[profile.group] > 1

    def _ungrouped_enabled(self) -> bool:
        real_groups = {
            _group_parts(profile)[0]
            for profile in self.profiles
            if _group_parts(profile) and self._is_grouped(profile)
        }
        return (
            len(self.profiles) >= _UNGROUPED_PROFILE_MINIMUM
            and len(real_groups) >= _UNGROUPED_GROUP_MINIMUM
        )

    def _shown(self, profile: Profile, query: str, status: str) -> bool:
        group_text = " ".join(_group_parts(profile)).lower()
        unmatched_group = self._ungrouped_enabled() and not self._is_grouped(profile) and query in _UNGROUPED
        if query not in profile.name.lower() and query not in profile.group.lower() and query not in group_text and not unmatched_group:
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
        self._profile_rows.clear()
        self._profile_check_buttons.clear()
        self._group_sizes = Counter(profile.group for profile in self.profiles)
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
        def grouped(profile: Profile) -> bool:
            return self._is_grouped(profile)

        ungrouped = [profile for profile in shown if not _group_parts(profile) or not grouped(profile)]
        root_sizes = Counter(
            _group_parts(profile)[0]
            for profile in self.profiles
            if _group_parts(profile) and grouped(profile)
        )
        roots: dict[str, dict] = {}

        def new_node() -> dict:
            return {"profiles": [], "children": {}}

        for profile in shown:
            parts = _group_parts(profile)
            if not parts or not grouped(profile):
                continue
            node = roots.setdefault(parts[0], new_node())
            for part in parts[1:]:
                node = node["children"].setdefault(part, new_node())
            node["profiles"].append(profile)
        if ungrouped and self._ungrouped_enabled():
            self.list_box.append(
                self._group(
                    _UNGROUPED,
                    ungrouped,
                    len(ungrouped),
                    group_key=_UNGROUPED,
                )
            )
        else:
            for profile in ungrouped:
                self.list_box.append(self._row(profile))

        def descendants(node: dict) -> list[Profile]:
            members = list(node["profiles"])
            for child in node["children"].values():
                members.extend(descendants(child))
            return members

        def emit(node: dict, path: str, total: int) -> Gtk.Widget:
            children = [
                emit(node["children"][name], f"{path}/{name}", len(descendants(node["children"][name])))
                for name in sorted(node["children"], key=str.lower)
            ]
            return self._group(
                path.rsplit("/", 1)[-1],
                node["profiles"],
                total,
                all_members=descendants(node),
                children=children,
                group_key=path,
            )

        self._applying_groups = True
        try:
            for root in sorted(roots, key=str.lower):
                self.list_box.append(emit(roots[root], root, root_sizes[root]))
        finally:
            self._applying_groups = False

    def _on_search(self) -> None:
        query = self.search.get_text().strip().lower()
        if query:
            if self._ungrouped_enabled() and query in _UNGROUPED:
                self._group_expanded[_UNGROUPED] = True
            for profile in self.profiles:
                if query in profile.name.lower() or query in profile.group.lower() or query in " ".join(_group_parts(profile)).lower():
                    parts = _group_parts(profile)
                    if self._ungrouped_enabled() and not self._is_grouped(profile):
                        self._group_expanded[_UNGROUPED] = True
                    for index in range(1, len(parts) + 1):
                        self._group_expanded["/".join(parts[:index])] = True
        self._rebuild()

    def _set_groups(self, expanded: bool) -> None:
        self._group_expanded[_UNGROUPED] = expanded
        for profile in self.profiles:
            parts = _group_parts(profile)
            for index in range(1, len(parts) + 1):
                self._group_expanded["/".join(parts[:index])] = expanded
        self._rebuild()

    def _remember_group(self, expander: Gtk.Expander, _pspec, group: str) -> None:
        if self._applying_groups:
            return
        self._group_expanded[group] = expander.get_expanded()

    def _group(
        self,
        group: str,
        members: list[Profile],
        total: int,
        *,
        all_members: list[Profile] | None = None,
        children: list[Gtk.Widget] | None = None,
        group_key: str | None = None,
    ) -> Gtk.Widget:
        group_key = group if group_key is None else group_key
        all_members = members if all_members is None else all_members
        children = children or []
        count = len(all_members)
        name = Gtk.Label(label=group, xalign=0, hexpand=True)
        name.add_css_class("group-name")
        amount = str(count) if count == total else f"{count}/{total}"
        tally = Gtk.Label(label=f"{amount} profile" if total == 1 else f"{amount} profiles")
        tally.add_css_class("group-count")
        selectable = [profile for profile in all_members if not profile.disabled]
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
        expander.connect("notify::expanded", self._remember_group, group_key)
        expander.set_expanded(self._group_expanded.get(group_key, False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(28)
        box.set_margin_top(4)
        box.set_margin_bottom(8)
        for profile in members:
            box.append(self._row(profile))
        for child in children:
            box.append(child)
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
        local_scan = profile.name in self._scan_running
        app = self._ofs_app
        active_elsewhere = isinstance(app, SfsApplication) and app.profile_is_active(profile.name)
        scanning = local_scan or active_elsewhere
        stamp = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        stamp.add_css_class("monospace")
        stamp.add_css_class(tone)
        stamp.set_size_request(_LAST_RUN_WIDTH, -1)
        stamp.set_text(when or "Not run yet")
        result = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        result.set_ellipsize(Pango.EllipsizeMode.END)
        result.set_max_width_chars(40)
        result.set_size_request(_RESULT_WIDTH, -1)
        result.add_css_class("result-running" if scanning else tone)
        result.set_text("Scanning…" if local_scan else "Running…" if active_elsewhere else body)
        result.set_tooltip_text("This profile is active in an OFS window. Run will be available when it finishes." if scanning else body)
        self._labels[profile.name] = result

        run = Gtk.Button(label="Running" if scanning else "Run")
        run.add_css_class("run")
        run.set_sensitive(not profile.disabled and not scanning)
        run.set_tooltip_text(
            "This profile is already scanning; a duplicate cannot be started."
            if scanning else "Enable the profile in Edit before running it" if profile.disabled else None
        )
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
        row.add_css_class("profile-row")
        if profile.name == self._focused_profile:
            row.add_css_class("profile-selected")
        self._profile_rows[profile.name] = row
        self._profile_check_buttons[profile.name] = check
        if len(self._labels) % 2:
            row.add_css_class("profile-row-alt")
        row.set_margin_top(2)
        row.set_margin_bottom(2)
        text.set_hexpand(True)
        row.append(text)
        row.append(stamp)
        row.append(result)
        row.append(actions)
        return row

    def _toggled(self, button: Gtk.CheckButton, name: str) -> None:
        self._focused_profile = name
        if button.get_active():
            self.checked.add(name)
        else:
            self.checked.discard(name)
        if self.status_filter.get_selected() == 5:
            self._rebuild()
        else:
            self._rebuild()

    def _toggle_appearance(self, *_args) -> None:
        if self._updating_appearance:
            return
        selected = "dark" if self.appearance_button.get_active() else "light"
        set_theme(selected)
        apply_appearance(selected)
        self._refresh_appearance()

    def _refresh_appearance(self) -> None:
        dark = bool(appearance_is_dark())
        self._updating_appearance = True
        try:
            self.appearance_button.set_active(dark)
            self.appearance_button.set_icon_name("weather-clear-night-symbolic" if dark else "weather-clear-symbolic")
            state = "on" if dark else "off"
            self.appearance_button.set_tooltip_text(f"Dark mode is {state}. Click for the opposite appearance; System mode is available in Settings.")
        finally:
            self._updating_appearance = False

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

    def _new_window(self, *_args) -> MainWindow | None:
        app = self._ofs_app
        return app.open_new_window() if isinstance(app, SfsApplication) else None

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
        box.append(Gtk.Label(label="Copy speed limit (MiB/s)", xalign=0))
        speed = Gtk.SpinButton.new_with_range(0, 1024, 0.1)
        speed.set_value(copy_speed_limit() / (1024 * 1024))
        speed.set_tooltip_text("One shared limit for the whole copy run: mebibytes per second, not network megabits")
        speed_presets = Gtk.DropDown.new_from_strings(["Unlimited", "1 MiB/s", "10 MiB/s", "100 MiB/s", "Custom"])
        preset_values = [0.0, 1.0, 10.0, 100.0]

        def choose_speed(*_args) -> None:
            selected = speed_presets.get_selected()
            speed.set_sensitive(selected == len(preset_values))
            if selected < len(preset_values):
                speed.set_value(preset_values[selected])

        speed_presets.set_selected(next((index for index, value in enumerate(preset_values) if speed.get_value() == value), len(preset_values)))
        speed_presets.connect("notify::selected", choose_speed)
        choose_speed()
        box.append(speed_presets)
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
        window.set_default_size(780, 720)
        name = Gtk.Entry(text="" if existing is None else existing.name)
        name.set_hexpand(True)
        group = Gtk.Entry(text="" if existing is None else existing.group_override)
        group.set_hexpand(True)
        group.set_placeholder_text("Automatic from name; optional: Work / Photos / Archive")
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
        cache_destination = Gtk.CheckButton(
            label="Cache destination metadata (until OFS changes it)",
            active=bool(existing and existing.cache_destination),
        )
        cache_destination.set_tooltip_text("For repeat discovery across backup rounds and restarts. OFS writes and changed filters invalidate it. Cached metadata can miss external destination changes, so use Run uncached when needed.")
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
        def browse_directory(_button, entry: Gtk.Entry, title: str) -> None:
            chooser = Gtk.FileChooserDialog(
                title=f"Choose {title} folder",
                transient_for=window,
                modal=True,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Choose folder", Gtk.ResponseType.ACCEPT)
            current_path = Path(entry.get_text().strip())
            if current_path.is_dir():
                chooser.set_current_folder(Gio.File.new_for_path(str(current_path)))

            def chosen(_dialog, response) -> None:
                if response == Gtk.ResponseType.ACCEPT:
                    selected_file = chooser.get_file()
                    path = selected_file.get_path() if selected_file is not None else None
                    if path:
                        entry.set_text(path)
                chooser.close()

            chooser.connect("response", chosen)
            chooser.present()

        def directory_entry(entry: Gtk.Entry, title: str) -> Gtk.Box:
            box = Gtk.Box(spacing=8, hexpand=True)
            entry.set_hexpand(True)
            browse = Gtk.Button()
            _button_icon(browse, "Browse…", "folder-open-symbolic")
            browse.set_tooltip_text(f"Choose an existing {title.lower()} local or mounted folder; typing a path is also supported")
            browse.connect("clicked", browse_directory, entry, title)
            box.append(entry)
            box.append(browse)
            return box

        tabs = Gtk.Notebook(vexpand=True)
        window.editor_tabs = tabs

        def editor_page(title: str, rows: list[tuple[str, Gtk.Widget]], checks: tuple[Gtk.CheckButton, ...] = ()) -> None:
            page = Gtk.Grid(column_spacing=8, row_spacing=8)
            for side in ("start", "end", "top", "bottom"):
                getattr(page, f"set_margin_{side}")(16)
            for index, (label, widget) in enumerate(rows):
                page.attach(Gtk.Label(label=label, xalign=1), 0, index, 1, 1)
                page.attach(widget, 1, index, 1, 1)
            if checks:
                group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                group.add_css_class("editor-checks")
                for index, widget in enumerate(checks):
                    grid_row = len(rows) + index
                    page.attach(Gtk.Label(), 0, grid_row, 1, 1)
                    page.attach(widget, 1, grid_row, 1, 1)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_child(page)
            tabs.append_page(scroll, Gtk.Label(label=title))

        editor_page("General", [
            ("Name", name),
            ("Group / subgroups", group),
            ("Left", directory_entry(left, "Left")),
            ("Right", directory_entry(right, "Right")),
            ("Left volume", left_volume),
            ("Right volume", right_volume),
            ("Mode", mode),
            ("Direction", direction),
        ], (cache_destination, disabled))
        editor_page("Filters", [
            ("Symlinks", symlinks),
            ("Include filters", include_editor),
            ("Exclusions", exclusion),
            ("Exclusion rules", exclusion_editor),
            ("Min size", min_size),
            ("Max size", max_size),
            ("Min age (days)", min_age),
            ("Max age (days)", max_age),
        ])
        editor_page("Comparison", [
            ("Same time, different size", same_time_size),
        ], (small, hour, seconds, ignore_time, larger, binary, track, always_copy, compare_permissions, compare_ownership, preserve_ownership))
        editor_page("Recovery & Safety", [
            ("Older versions", older_keep),
            ("Delete limit (%; 0 = off)", delete_limit),
        ], (older, keep_deleted, delete_all, never_delete))

        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        cancel = Gtk.Button(label="Cancel")
        cancel.set_tooltip_text("Close without saving profile changes")
        cancel.connect("clicked", lambda *_: window.close())
        editor_buttons = Gtk.Box(spacing=8, hexpand=True)
        for side in ("start", "end", "top", "bottom"):
            getattr(editor_buttons, f"set_margin_{side}")(12)
        if existing is not None:
            restore = Gtk.Button(label="Restore Older…")
            restore.connect("clicked", lambda *_: RestoreWindow(self, existing).present())
            editor_buttons.append(restore)
        editor_buttons.append(Gtk.Box(hexpand=True))
        editor_buttons.append(cancel)
        editor_buttons.append(save)
        window.set_default_widget(save)

        def on_save(*_args) -> None:
            chosen = name.get_text().strip()
            if not chosen:
                return
            updated = Profile(
                name=chosen,
                group_override=group.get_text().strip(),
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
                cache_destination=cache_destination.get_active(),
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
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(tabs)
        root.append(Gtk.Separator())
        root.append(editor_buttons)
        window.editor_footer = editor_buttons
        window.set_child(root)
        window.present()
        return window

    def _copy_new_folder(self, *_args) -> Gtk.Window | None:
        enabled = [profile for profile in self.profiles if profile.name in self.checked and not profile.disabled]
        if len(enabled) != 1:
            self.status.set_text("Check exactly one enabled profile, then choose its new source folder.")
            return None
        profile = enabled[0]
        if profile.direction == Direction.BOTH:
            self.status.set_text("Copy new folder needs a one-way profile, so the destination is unambiguous.")
            return None
        app = self._ofs_app
        if profile.name in self._scan_running or profile.name in self._folder_copy_running or (isinstance(app, SfsApplication) and app.profile_is_active(profile.name)):
            self.status.set_text("This profile is already scanning or copying a new folder. Wait for it to finish first.")
            return None
        source_root = Path(profile.left if profile.direction == Direction.LEFT_TO_RIGHT else profile.right)
        if not source_root.is_dir():
            self.status.set_text("The selected profile's source folder is not available.")
            return None
        chooser = Gtk.FileChooserDialog(
            title="Choose a new folder to copy without comparison",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Continue", Gtk.ResponseType.ACCEPT)
        chooser.set_current_folder(Gio.File.new_for_path(str(source_root)))

        def selected(_dialog, response) -> None:
            if response != Gtk.ResponseType.ACCEPT:
                chooser.close()
                return
            selected_file = chooser.get_file()
            try:
                path = Path(selected_file.get_path()).absolute()
                relative = path.relative_to(source_root.absolute()).as_posix()
            except (AttributeError, OSError, TypeError, ValueError):
                self.status.set_text("Choose an existing subfolder inside the source side of the selected profile.")
                return
            chooser.close()
            self._confirm_new_folder(profile, relative)

        chooser.connect("response", selected)
        chooser.present()
        return chooser

    def _confirm_new_folder(self, profile: Profile, relative: str) -> None:
        source_root = Path(profile.left if profile.direction == Direction.LEFT_TO_RIGHT else profile.right)
        destination_root = Path(profile.right if profile.direction == Direction.LEFT_TO_RIGHT else profile.left)
        source = source_root / relative
        destination = destination_root / relative
        if not source.is_dir():
            self.status.set_text("Choose an existing source subfolder inside the selected profile.")
            return
        if os.path.lexists(destination):
            self.status.set_text("The matching destination already exists. Use a normal complete scan instead; nothing was changed.")
            return
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Copy the whole new folder {relative!r}?")
        dialog.set_detail(
            f"From: {source}\nTo: {destination}\n\n"
            "This explicit copy-only action does not compare source and destination files or delete anything. "
            "The destination must be absent. Existing exclusions and symlink options apply. The copy streams internally in chunks to a temporary folder and uses an atomic no-replace rename only after completion."
        )
        dialog.set_buttons(["Cancel", "Copy new folder"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._new_folder_chosen, profile, relative)

    def _new_folder_chosen(self, dialog, result, profile: Profile, relative: str) -> Gtk.Window | None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return None
        if choice != 1:
            return None
        app = self._ofs_app
        if profile.name in self._scan_running or profile.name in self._folder_copy_running or (isinstance(app, SfsApplication) and app.profile_is_active(profile.name)):
            self.status.set_text("The profile became active before this confirmation. Wait for the other operation to finish.")
            return None
        current = self._profile(profile.name)
        if current is None or current.disabled:
            self.status.set_text("The selected profile is no longer available or is disabled. Nothing was copied.")
            return None
        if current.left != profile.left or current.right != profile.right or current.direction != profile.direction:
            self.status.set_text("The profile paths or direction changed after this confirmation. Nothing was copied; confirm the new folder again.")
            return None
        if isinstance(app, SfsApplication) and not app.claim_profile(profile.name):
            self.status.set_text("This profile is active in another OFS window. Nothing was copied.")
            return None
        self._folder_copy_running.add(profile.name)
        progress = WholeFolderWindow(self, current, relative)
        progress.present()
        skips = list(self.session_masks.get(profile.name, []))

        def report_progress(event: CopyProgress) -> None:
            GLib.idle_add(progress.update, event)

        def work() -> None:
            try:
                outcome = copy_new_folder(
                    current,
                    relative,
                    control=progress.control,
                    on_progress=report_progress,
                    session_masks=skips,
                )
            except Exception as exc:
                outcome = FolderCopyOutcome(ApplyReport(errors=[f"{relative}: {exc}"]))
            path = None
            try:
                # Rollback/cleanup failures may leave hidden temporary output behind,
                # even though committed counters were reset to zero. Rescan fresh.
                invalidate_cached_destinations(current)
                remember(current)
                path = write_log(current, log_body(current, outcome.changes, outcome.report, choice="explicit new folder; no comparison or overwrites"))
            except Exception as exc:
                outcome.report.errors.append(f"history/log: {exc}")
            try:
                record_run(current.name, "folder-copy", outcome.report.summary(), path)
            except OSError as exc:
                outcome.report.errors.append(f"run history: {exc}")
            GLib.idle_add(self._whole_folder_finished, progress, outcome, path, priority=GLib.PRIORITY_DEFAULT)

        threading.Thread(target=work, daemon=True, name=f"OFS folder copy: {profile.name}").start()
        return progress

    def _whole_folder_finished(self, progress: WholeFolderWindow, outcome: FolderCopyOutcome, path: Path | None) -> bool:
        progress.allow_close()
        self._folder_copy_running.discard(progress.profile_name)
        app = self._ofs_app
        if isinstance(app, SfsApplication):
            app.release_profile(progress.profile_name)
        progress.job.profile.last_result = f"{time.strftime('%Y-%m-%d %H:%M')}  {outcome.report.summary()}"
        save_profile(progress.job.profile)
        self.reload()
        self.status.set_text(f"{progress.profile_name}: new folder copy stopped." if outcome.report.cancelled else f"{progress.profile_name}: {outcome.report.summary()}.")
        ResultsWindow(progress, outcome.report, path).present()
        return False

    def _run_one(self, _button, name: str) -> None:
        self._focused_profile = name
        self._run_names({name})

    def _run(self, *_args) -> None:
        chosen = {profile.name for profile in self.profiles if profile.name in self.checked}
        if not chosen:
            self.status.set_text("Check one or more profiles.")
            return
        self._run_names(chosen)

    def _run_uncached(self, *_args) -> None:
        chosen = {profile.name for profile in self.profiles if profile.name in self.checked}
        if not chosen:
            self.status.set_text("Check one or more profiles to scan without destination caches.")
            return
        self._run_names(chosen, uncached=True)

    def _run_history(self, *_args):
        window = RunHistoryWindow(self)
        window.present()
        return window

    def _session_rule_lock(self, name: str) -> threading.RLock:
        return self._session_rule_locks.setdefault(name, threading.RLock())

    def _run_names(self, chosen: set[str], *, uncached: bool = False) -> None:
        selected = [profile for profile in self.profiles if profile.name in chosen and not profile.disabled]
        profiles = []
        already_running: set[str] = set()
        app = self._ofs_app
        for profile in selected:
            locally_running = profile.name in self._scan_running
            globally_busy = isinstance(app, SfsApplication) and (app.profile_is_active(profile.name) or not app.claim_profile(profile.name))
            if locally_running:
                already_running.add(profile.name)
            elif globally_busy:
                already_running.add(profile.name)
            else:
                profiles.append(profile)
        if not profiles:
            if already_running:
                self.status.set_text("This profile is already scanning or active in an OFS window; no duplicate was started." if len(already_running) == 1 else "These profiles are already scanning or active in OFS windows; no duplicates were started.")
            else:
                self.status.set_text("The selected profile is disabled. Enable it in Edit first.")
            return

        batch_id = self._next_scan_batch
        self._next_scan_batch += 1
        self._scan_batches[batch_id] = {"remaining": {profile.name for profile in profiles}, "results": []}
        self._scan_running.update(profile.name for profile in profiles)
        self._rebuild()
        started = f"Started {len(profiles)} parallel scans." if len(profiles) > 1 else f"Scanning {profiles[0].name}…"
        if already_running:
            started += f" {len(already_running)} already-running profile(s) were not duplicated."
        self._with_scan_activity(started)

        # Independent windows and controls keep Skip/Cancel scoped to one profile.
        for profile in profiles:
            self._start_profile_scan(profile, batch_id, uncached)

    def _with_scan_activity(self, text: str) -> None:
        count = len(self._scan_running)
        if count:
            noun = "scan" if count == 1 else "scans"
            text += f"  {count} {noun} still running."
        self.status.set_text(text)

    def _start_profile_scan(self, profile: Profile, batch_id: int, uncached: bool) -> None:
        skips = {profile.name: self.session_skips.setdefault(profile.name, set())}
        masks = {profile.name: self.session_masks.setdefault(profile.name, [])}
        progress = ScanWindow(self, f"Scanning {profile.name}", estimates={profile.name: scan_estimate(profile.name)})
        progress.control.rules_lock = self._session_rule_lock(profile.name)
        preview = self._preview(JobResult(profile, ScanResult(changes=[]), None))
        preview._begin_scan(progress, uncached=uncached)
        self._scan_previews[profile.name] = preview
        # Closing the overview must not quit OFS before the result has a window.
        self._ofs_app.hold()

        def on_stats(name: str, side: str, rel: str, values) -> None:
            GLib.idle_add(preview._record_scan_stats, name, side, rel, values, priority=GLib.PRIORITY_DEFAULT)

        def on_entry(name: str, side: str, rel: str, stat) -> None:
            if name == profile.name:
                preview._queue_entry(side, rel, stat)

        def work() -> None:
            try:
                results = run_checked(
                    [profile],
                    checked={profile.name},
                    session_skips=skips,
                    session_masks=masks,
                    control=progress.control,
                    on_stats=on_stats,
                    on_entry=on_entry,
                    force_uncached=uncached,
                )
            except Exception as exc:
                results = [JobResult(profile, None, str(exc))]
            # Completion must not starve behind live-tree batches and animation idle work.
            GLib.idle_add(self._scan_finished, batch_id, profile.name, results, progress, priority=GLib.PRIORITY_DEFAULT)

        threading.Thread(target=work, daemon=True, name=f"OFS scan: {profile.name}").start()

    def _scan_finished(self, batch_id: int, name: str, results: list[JobResult], progress: ScanWindow) -> bool:
        try:
            batch = self._scan_batches.get(batch_id)
            progress.allow_close()
            live = self._scan_previews.pop(name, None)
            if live is not None:
                missing = JobResult(live.job.profile, None, "scan produced no result")
                live._finish_scan(results[0] if results else missing)
                self._live_completed[name] = live
            self._scan_running.discard(name)
            app = self._ofs_app
            if isinstance(app, SfsApplication):
                app.release_profile(name)
            self._rebuild()
            if batch is None:
                return False
            batch["remaining"].discard(name)
            batch["results"].extend(results)
            if batch["remaining"]:
                self._with_scan_activity(f"Scanned {name}; other selected profiles are still running.")
                return False

            completed = batch["results"]
            del self._scan_batches[batch_id]
            order = {profile.name: index for index, profile in enumerate(self.profiles)}
            completed.sort(key=lambda job: order.get(job.profile.name, len(order)))
            self._show_results(completed)
            return False
        finally:
            self._ofs_app.release()

    def _show_results(self, results: list[JobResult]) -> bool:
        if results and all(job.error == "scan stopped" for job in results):
            for job in results:
                self._live_completed.pop(job.profile.name, None)
            self._with_scan_activity("Scan stopped.")
            return False
        ok = sum(1 for job in results if job.error is None)
        bad = len(results) - ok
        planned = [change for job in results if job.result is not None for change in job.result.changes]
        summary_text = plan_summary(planned)
        partial = sum(bool(job.result and job.result.partial_sides) for job in results)
        completion = f"Scanned {ok}. {bad} skipped. {summary_text}" if bad else f"Scanned {ok}. {summary_text}"
        if partial:
            completion += f" {partial} partial scan(s): review exclusions only, then Rescan; applying partial results is disabled."
        self._with_scan_activity(completion)
        for job in results:
            if job.result is not None and job.profile.name in self._labels:
                text = "Partial scan — review only" if job.result.partial_sides else plan_summary(job.result.changes)
                self._labels[job.profile.name].set_text(text)
                if job.result.partial_sides:
                    self._labels[job.profile.name].set_tooltip_text(job.result.note)
        if len(results) == 1 and results[0].error is None and results[0].result is not None:
            self._preview(results[0])
            return False
        if len(results) == 1 and results[0].error is not None:
            name = results[0].profile.name
            self._live_completed.pop(name, None)
            if name in self._labels:
                self._labels[name].set_text(results[0].error or "scan unavailable")
            return False
        if len(results) > 1:
            for job in results:
                live = self._live_completed.get(job.profile.name)
                if live is not None and not live._running:
                    live.close()
        summary = Gtk.Window(transient_for=self, title="Scan")
        summary.set_application(self._ofs_app)
        summary.set_default_size(760, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.append(Gtk.Label(label=summary_text, xalign=0))
        if partial:
            box.append(Gtk.Label(label=f"{partial} partial scan(s) are review-only. Add exclusions, then rescan; no partial plan can be applied.", xalign=0, wrap=True, css_classes=["result-warn"]))
        for job in results:
            line = Gtk.Box(spacing=8)
            if job.error:
                label = Gtk.Label(label=f"{job.profile.name}: {job.error}", xalign=0, hexpand=True)
            else:
                assert job.result is not None
                result_text = "Partial scan — review only" if job.result.partial_sides else plan_summary(job.result.changes)
                label = Gtk.Label(label=f"{job.profile.name}: {result_text}", xalign=0, hexpand=True)
                button = Gtk.Button(label="Preview")
                button.connect("clicked", lambda _b, item=job: self._preview(item))
                line.append(button)
            line.prepend(label)
            box.append(line)
            if job.error and job.profile.name in self._labels:
                self._labels[job.profile.name].set_text(job.error)
        summary.set_child(box)
        summary.present()
        return False

    def _preview(self, job: JobResult) -> None:
        assert job.result is not None
        self._focused_profile = job.profile.name
        window = self._live_completed.pop(job.profile.name, None)
        if window is None:
            window = Preview(self, job)
        window.present()
        return window


def _compact(button: Gtk.Button) -> None:
    button.add_css_class("compact")


def _owner_application(owner: Gtk.Window) -> Gtk.Application | None:
    # A closed overview retains its app reference for detached operation results.
    return getattr(owner, "_ofs_app", None) or owner.get_application()


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


def _failed_relative_paths(errors: list[str]) -> list[str]:
    """Recover trusted Change.rel prefixes, never an absolute path from an OS error."""

    paths = []
    for message in errors:
        rel, separator, _detail = message.partition(": ")
        if not separator:
            continue
        rel = rel.replace("\\", "/").strip()
        parts = PurePosixPath(rel).parts
        if not rel or "\n" in rel or "\x00" in rel or rel.startswith("/") or ".." in parts:
            continue
        if rel not in paths:
            paths.append(rel)
    return paths


def _failed_common_folder(paths: list[str]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        parent = str(PurePosixPath(paths[0]).parent)
        return "" if parent == "." else parent
    try:
        common = os.path.commonpath(paths)
    except ValueError:
        return ""
    return "" if common in {"", "."} else common


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
        self._refresh_generation = 0
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
        # Removing the focused Remove button can scroll both this list and the
        # surrounding editor page. Restore their positions after GTK lays out
        # the replacement rows, and discard obsolete restores after another edit.
        self._refresh_generation += 1
        generation = self._refresh_generation
        positions: list[tuple[Gtk.Adjustment, float]] = []
        # ListBox may sit behind an internal GtkViewport before its ScrolledWindow.
        widget: Gtk.Widget | None = self.list
        while widget is not None:
            parent = widget.get_parent()
            if isinstance(parent, Gtk.ScrolledWindow):
                adjustment = parent.get_vadjustment()
                positions.append((adjustment, adjustment.get_value()))
            widget = parent
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
        GLib.idle_add(self._restore_refresh_scroll, generation, positions)

    def _restore_refresh_scroll(self, generation: int, positions: list[tuple[Gtk.Adjustment, float]]) -> bool:
        if generation != self._refresh_generation:
            return False
        for adjustment, value in positions:
            maximum = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
            adjustment.set_value(min(value, maximum))
        return False

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
        "box.profile-row { padding: 7px 10px; border-bottom: 1px solid alpha(@theme_fg_color, 0.12); border-radius: 6px; }"
        "box.profile-row-alt { background-color: alpha(@theme_fg_color, 0.035); }"
        "box.profile-row:hover { background-color: alpha(@accent_bg_color, 0.12); }"
        "box.profile-row.profile-selected { background-color: alpha(@accent_bg_color, 0.16); outline: 2px solid alpha(@accent_color, 0.7); outline-offset: -2px; }"
        ".history-list row:selected label { color: @theme_selected_fg_color; }"
        "box.scan-path { padding: 2px 2px 4px 2px; border-bottom: 1px solid alpha(@theme_fg_color, 0.10); }"
        "label.result-ok { color: @success_color; }"
        "label.result-error { color: @error_color; }"
        "label.result-warn { color: @warning_color; }"
        "label.result-running { color: @accent_color; font-weight: 700; }"
    )
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


class Preview(Gtk.Window):
    def __init__(self, owner: MainWindow, job: JobResult):
        super().__init__(transient_for=owner, title=job.profile.name)
        app = _owner_application(owner)
        if app is not None:
            self.set_application(app)
        _install_css()
        self.owner = owner
        self.job = job
        self.set_default_size(1100, 720)
        # Large trees benefit from the available screen; smaller restored windows stay usable.
        self.maximize()
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
        self._scan_active = False
        self._scan_complete = True
        self._scan_progress: ScanWindow | None = None
        self._copy_progress: ProgressWindow | None = None
        self.scan_details: Gtk.Expander | None = None
        self.copy_details: Gtk.Expander | None = None
        self._entry_queue = deque()
        self._entry_lock = threading.Lock()
        self._entry_refresh_scheduled = False
        self._live_refill_pending = False
        self._force_uncached = False
        self._cache_sides: set[str] = set()
        self._cached_discovery: set[str] = set()
        self._fresh_discovery: set[str] = set()
        self._copy_cache_invalidated = False
        self._live_seen: set[str] = set()
        self._live_rows: dict[str, Listed] = {}
        self._live_permanent_masks: set[str] = set()
        self._file_iters = {}
        self._completed_rows: dict[str, str] = {}
        self._last_copy_done = 0
        self._last_copy_errors = 0
        self._popover: Gtk.Popover | None = None
        self._fill_generation = 0
        self._pending_tree = []
        self._replace_tree_model = False
        self._visible_changes = {}
        self._tree_default_expanded = True
        self._tree_sort = "name"
        self.review_min_size = 0
        self.review_size_target = "files"
        self.review_size_scope = ""
        self._tree_state_overrides: dict[str, bool] = {}
        self._folder_iters = {}
        self._record_tree_state = True
        self.partial = bool(job.result.partial_sides)
        if self.partial:
            self.set_title(f"{job.profile.name} — Partial scan (review only)")
        self._build()
        self._refill()
        self.connect("close-request", self._close_request)

    def _build(self) -> None:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(8)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_size_request(220, -1)
        self.tree_controls = Gtk.Box(spacing=6)
        self.tree_controls.set_margin_start(12)
        self.tree_controls.set_margin_end(12)
        self.tree_controls.set_margin_bottom(8)
        folder_heading = Gtk.Label(label="Folders", xalign=0, css_classes=["heading"])
        folder_heading.set_margin_end(6)
        self.tree_controls.append(folder_heading)
        self.expand_all_button = Gtk.Button()
        _button_icon(self.expand_all_button, "Expand all", "pan-down-symbolic")
        self.expand_all_button.set_tooltip_text("Show every nested folder and file")
        self.expand_all_button.connect("clicked", self._set_tree_expanded, True)
        self.collapse_all_button = Gtk.Button()
        _button_icon(self.collapse_all_button, "Collapse all", "pan-up-symbolic")
        self.collapse_all_button.set_tooltip_text("Show only top-level folders and files; the copy plan does not change")
        self.collapse_all_button.connect("clicked", self._set_tree_expanded, False)
        for button in (self.expand_all_button, self.collapse_all_button):
            self.tree_controls.append(button)
        sort_heading = Gtk.Label(label="Sort by", xalign=0)
        sort_heading.set_margin_start(12)
        self.tree_controls.append(sort_heading)
        self.sort_results = Gtk.DropDown.new_from_strings(["Name", "Size: largest first", "Size: smallest first"])
        self.sort_results.set_tooltip_text("Order folders and files within each parent; the synchronization plan does not change")
        self.sort_results.connect("notify::selected", self._sort_tree)
        self.tree_controls.append(self.sort_results)
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
        sidebar.append(self._size_filter_controls())
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
        action_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        action_area.set_margin_start(12)
        action_area.set_margin_end(8)
        action_area.set_margin_top(8)
        action_area.set_margin_bottom(12)
        self.shown = Gtk.Label(xalign=0, wrap=True)
        action_area.append(self.shown)
        action_area.append(Gtk.Label(label="After completed copy", xalign=0, css_classes=["dim-label"]))
        self.completion_action = Gtk.DropDown.new_from_strings(["Do nothing", "Sleep", "Hibernate", "Shut down"])
        self.completion_values = ["none", "sleep", "hibernate", "shutdown"]
        self.completion_action.set_selected(0)
        self.completion_action.set_sensitive(not self.partial)
        self.completion_action.set_tooltip_text("Apply only after this copy finishes without errors or cancellation. Other active copies prevent the power action.")
        action_area.append(self.completion_action)
        self.start_button = Gtk.Button(label="Rescan" if self.partial else "Start")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._start_or_rescan)
        if self.partial:
            self.start_button.set_tooltip_text("After adding exclusions, start a complete fresh scan. Partial results cannot be copied or deleted.")
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.close())
        action_area.append(self.start_button)
        action_area.append(close_button)

        self.left_status = Gtk.Label(xalign=0, wrap=True)
        self.right_status = Gtk.Label(xalign=0, wrap=True)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(8)
        if self.partial:
            warning = Gtk.Label(
                label="PARTIAL SCAN — REVIEW ONLY\nOnly entries discovered before Skip Rest are shown. Copying and deleting are disabled. Right-click a folder or file to add an exclusion, then choose Rescan.",
                xalign=0,
                wrap=True,
                css_classes=["result-warn"],
            )
            header.append(warning)
        self.sides_header = Gtk.Box(homogeneous=True, spacing=24)
        self.sides_header.append(self._side_head("Left", self.job.profile.left, self.left_status))
        self.sides_header.append(self._side_head("Right", self.job.profile.right, self.right_status))
        header.append(self.sides_header)
        rule = comparison_rule_text(self.job.profile)
        self.rule_label = Gtk.Label(label=rule, xalign=0, wrap=True, css_classes=["dim-label"])
        if rule:
            header.append(self.rule_label)

        self.tree_store = Gtk.TreeStore.new([str, str, str, str, str, bool])
        self.view = Gtk.TreeView(model=self.tree_store)
        self.view.set_headers_visible(True)
        self.view.set_enable_tree_lines(True)
        self.view.set_enable_search(True)
        self.view.set_search_column(0)
        self.view.connect("row-expanded", self._remember_tree_state, True)
        self.view.connect("row-collapsed", self._remember_tree_state, False)
        for index, title, width in (
            (0, "Name", 320),
            (1, "Size", 80),
            (2, "Direction", 180),
            (3, "Status", 300),
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
            column.set_cell_data_func(renderer, self._result_cell_style)
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
        self.operation_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        listing.append(header)
        listing.append(self.tree_controls)
        listing.append(self.operation_area)
        listing.append(scrolled)
        listing.append(bar)
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar_scroll = Gtk.ScrolledWindow(vexpand=True)
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_child(sidebar)
        sidebar_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_column.append(sidebar_scroll)
        sidebar_column.append(Gtk.Separator())
        sidebar_column.append(action_area)
        root.append(sidebar_column)
        listing.set_hexpand(True)
        root.append(listing)
        self.set_child(root)

    def _attach_operation_panel(self, progress: Gtk.Window, title: str) -> Gtk.Expander:
        panel = progress.get_child()
        assert panel is not None
        # Keep the existing controls/callbacks, but present them above the tree.
        progress.set_child(None)
        # The old top-level must never reappear as an empty standalone window.
        progress.hide()
        expander = Gtk.Expander(label=title, expanded=True)
        expander.set_tooltip_text("Show or collapse operation details without changing the plan")
        expander.set_child(panel)
        self.operation_area.append(expander)
        return expander

    def _begin_scan(self, progress: ScanWindow, *, uncached: bool = False) -> None:
        self._scan_active = True
        self._scan_complete = False
        self._scan_progress = progress
        self._force_uncached = uncached
        self._cache_sides = set(destination_sides(self.job.profile))
        self._cached_discovery.clear()
        self._fresh_discovery.clear()
        self.scan_details = self._attach_operation_panel(progress, f"Scanning {self.job.profile.name}")
        self.start_button.set_label("Scanning…")
        self.start_button.set_sensitive(False)
        self.start_button.set_tooltip_text("Wait for the complete comparison, or cancel discovery")
        self.completion_action.set_sensitive(False)
        self.set_title(f"{self.job.profile.name} — Scanning")
        self._refresh_detail()

    def _show_live_side(self, side: str) -> None:
        if side not in self._cache_sides:
            text = "Fresh source discovery; no source cache is used"
        elif not self.job.profile.cache_destination:
            text = "Destination cache: off · fresh discovery"
        elif self._force_uncached:
            text = "Cache enabled · uncached override · fresh discovery"
        elif side in self._cached_discovery:
            text = "Cache reused · saved destination metadata (age in scan details)"
        elif side in self._fresh_discovery:
            text = "Cache enabled · no compatible snapshot · fresh discovery"
        else:
            text = "Cache enabled · validating destination snapshot"
        (self.left_status if side == "Left" else self.right_status).set_text(text)

    def _record_scan_stats(self, name: str, side: str, rel: str, values) -> bool:
        progress = self._scan_progress
        if progress is None or progress._allow_close:
            return False
        progress.update_stats(name, side, rel, values)
        if side in self._cache_sides:
            if rel.startswith("Using cached destination metadata"):
                self._cached_discovery.add(side)
            else:
                self._fresh_discovery.add(side)
        self._show_live_side(side)
        return False

    def _queue_entry(self, side: str, rel: str, stat) -> None:
        with self._entry_lock:
            self._entry_queue.append((side, rel, stat))
            if self._entry_refresh_scheduled:
                return
            self._entry_refresh_scheduled = True
        GLib.idle_add(self._drain_pending)

    def _live_hidden(self, rel: str) -> bool:
        profile = self.job.profile
        with self.owner._session_rule_lock(profile.name):
            return (
                rel in self.owner.session_skips.get(profile.name, set())
                or any_match(
                    rel,
                    profile.exclusions + list(self._live_permanent_masks) + self.owner.session_masks.get(profile.name, []),
                )
            )

    def _drain_pending(self) -> bool:
        self._entry_refresh_scheduled = False
        if not self._scan_active:
            with self._entry_lock:
                self._entry_queue.clear()
            return False
        for _ in range(_PREVIEW_BATCH_SIZE):
            with self._entry_lock:
                if not self._entry_queue:
                    break
                side, rel, stat = self._entry_queue.popleft()
            if self._live_hidden(rel):
                continue
            existing = self._live_rows.get(rel)
            if existing is None:
                existing = Listed(
                    Change(rel, Action.ASK, f"Discovered on {side.lower()}; awaiting comparison", stat.size),
                    side == "Left", side == "Right",
                )
                self.rows.append(existing)
                self._live_seen.add(rel)
                self._live_rows[rel] = existing
            else:
                existing.on_left = existing.on_left or side == "Left"
                existing.on_right = existing.on_right or side == "Right"
                if existing.on_left and existing.on_right:
                    existing.change = Change(rel, Action.ASK, "Seen on both sides; awaiting comparison", stat.size)
            self._append_pending(existing)
        if self._live_refill_pending:
            self._live_refill_pending = False
            self._refill()
        else:
            self._refresh_detail()
        self.shown.set_text(f"Discovered preview: {len(self._live_seen)} paths. Pending rows make no copy/delete decision.")
        self._refresh_tree_layout()
        if self._scan_active and self._entry_queue:
            with self._entry_lock:
                if self._entry_refresh_scheduled:
                    return False
                self._entry_refresh_scheduled = True
            return True
        return False

    def _append_pending(self, row: Listed) -> None:
        rel = row.change.rel
        if not matches(row, **self.filters):
            return
        self._visible_changes[rel] = row.change
        if self._scan_active and (self.review_min_size or self._tree_sort != "name"):
            # Folder aggregates and sibling order depend on all newly queued rows.
            self._live_refill_pending = True
            return
        if self._scan_active and (self._pending_tree or self._replace_tree_model):
            self._live_refill_pending = True
            return
        if rel in self._file_iters:
            self.tree_store.set_value(self._file_iters[rel], 3, row.change.reason)
            return
        parts = rel.split("/")
        parent = None
        for depth, component in enumerate(parts[:-1], start=1):
            prefix = "/".join(parts[:depth])
            folder = self._folder_iters.get(prefix)
            if folder is None:
                folder = self.tree_store.append(parent, [component, "", "L ··· R", "Discovering…", prefix, False])
                self._folder_iters[prefix] = folder
            parent = folder
        current = self.tree_store.append(
            parent,
            [parts[-1], format_bytes(row.change.size), "L ··· R", row.change.reason, rel, False],
        )
        self._file_iters[rel] = current
        self.store.append(PlanRow(row.change))

    def _filter_current_rules(self, job: JobResult) -> None:
        if job.result is None:
            return
        profile = job.profile
        with self.owner._session_rule_lock(profile.name):
            exact = set(self.owner.session_skips.get(profile.name, set()))
            session = list(self.owner.session_masks.get(profile.name, []))
            permanent = list(self._live_permanent_masks)
        def allowed(relative: str) -> bool:
            return relative not in exact and not any_match(relative, session + permanent)
        job.result.changes = [change for change in job.result.changes if allowed(change.rel) and (not change.dest_rel or allowed(change.dest_rel))]
        job.result.listing = [row for row in job.result.listing if allowed(row.change.rel)]

    def _finish_scan(self, job: JobResult) -> None:
        self.job = job
        self._filter_current_rules(job)
        self._scan_active = False
        self._scan_complete = job.result is not None
        with self._entry_lock:
            self._entry_queue.clear()
        self._live_seen.clear()
        self._live_rows.clear()
        progress = self._scan_progress
        if progress is not None:
            progress.pause_button.set_sensitive(False)
            progress.skip_button.set_sensitive(False)
            progress.cancel_button.set_sensitive(False)
        if self.scan_details is not None:
            self.scan_details.set_expanded(False)
            self.scan_details.set_label("Scan complete" if self._scan_complete else "Scan stopped / unavailable")
        if job.result is not None:
            result = job.result
            self.rows = list(result.listing) if result.listing else [Listed(change, False, False) for change in result.changes]
            self._original = {row.change.rel: row.change for row in self.rows}
            self.partial = bool(result.partial_sides)
            self.set_title(f"{job.profile.name} — Partial scan (review only)" if self.partial else job.profile.name)
            self.start_button.set_label("Rescan" if self.partial else "Start")
            self.start_button.set_sensitive(True)
            if self.partial:
                self.start_button.set_tooltip_text("After adding exclusions, start a complete fresh scan. Partial results cannot be copied or deleted.")
            else:
                self.start_button.set_tooltip_text(None)
            self.completion_action.set_sensitive(not self.partial)
        else:
            self.partial = True
            self.detail.set_text(job.error or "Scan stopped")
            self.start_button.set_label("Scan incomplete")
            self.start_button.set_sensitive(False)
            self.completion_action.set_sensitive(False)
            self.set_title(f"{job.profile.name} — Scan stopped")
        self._refill()

    def _close_request(self, *_args) -> bool:
        if self._scan_active and self._scan_progress is not None:
            self._scan_progress._cancel()
            self.detail.set_text("Canceling discovery… The page stays open until the worker stops.")
            return True
        if self._running and self._copy_progress is not None:
            self._copy_progress._cancel()
            self.detail.set_text("Canceling at the next file/chunk boundary…")
            return True
        if self.owner._live_completed.get(self.job.profile.name) is self:
            self.owner._live_completed.pop(self.job.profile.name, None)
        self.owner._return_to_profile(self.job.profile.name)
        return False

    def _start_or_rescan(self, *_args) -> None:
        if self.partial and self._scan_complete:
            self._rescan()
        else:
            self._start()

    def _result_cell_style(self, _column, renderer, model, tree_iter, _data) -> None:
        renderer.set_property("sensitive", not model.get_value(tree_iter, 5))

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

    def _size_filter_controls(self) -> Gtk.Expander:
        self.size_filter_expander = Gtk.Expander(label="Size filter", expanded=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        minimum = Gtk.Box(spacing=4)
        self.size_amount = Gtk.Entry(text="0", width_chars=7, max_width_chars=7)
        self.size_amount.set_tooltip_text("Minimum size; zero or an empty value disables the size filter")
        self.size_units = Gtk.DropDown.new_from_strings(["B", "KiB", "MiB", "GiB", "TiB"])
        self.size_units.set_selected(2)
        minimum.append(self.size_amount)
        minimum.append(self.size_units)
        box.append(Gtk.Label(label="Minimum size", xalign=0))
        box.append(minimum)
        box.append(Gtk.Label(label="Apply threshold to", xalign=0))
        self.size_apply = Gtk.DropDown.new_from_strings(["Files only", "Folders only", "Files and folders"])
        self.size_apply.set_tooltip_text("Folder size is the total of its currently visible files")
        box.append(self.size_apply)
        self.size_scope_label = Gtk.Label(label="Location: whole preview", xalign=0, wrap=True)
        self.size_scope_label.set_tooltip_text("Right-click a folder to apply the size threshold only below that folder")
        box.append(self.size_scope_label)
        self.size_scope_clear = Gtk.Button(label="Use whole preview")
        _compact(self.size_scope_clear)
        self.size_scope_clear.set_visible(False)
        self.size_scope_clear.connect("clicked", self._set_size_scope, "")
        box.append(self.size_scope_clear)
        self.size_hint = Gtk.Label(label="Display only; Start runs the whole plan.", xalign=0, wrap=True)
        self.size_hint.add_css_class("dim-label")
        box.append(self.size_hint)
        self.size_amount.connect("changed", self._size_review_changed)
        self.size_units.connect("notify::selected", self._size_review_changed)
        self.size_apply.connect("notify::selected", self._size_review_changed)
        self.size_filter_expander.set_child(box)
        return self.size_filter_expander

    def _size_review_changed(self, *_args) -> None:
        try:
            amount = float(self.size_amount.get_text().strip() or 0)
        except ValueError:
            self.size_hint.set_text("Enter a non-negative number, such as 1.5 MiB.")
            return
        if not math.isfinite(amount) or amount < 0:
            self.size_hint.set_text("Enter a non-negative number, such as 1.5 MiB.")
            return
        factors = (1, 1024, 1024**2, 1024**3, 1024**4)
        self.review_min_size = int(amount * factors[self.size_units.get_selected()])
        self.review_size_target = ("files", "folders", "all")[self.size_apply.get_selected()]
        self.size_hint.set_text("Display only; Start runs the whole plan.")
        self._refill()

    def _set_size_scope(self, _button, folder: str = "") -> None:
        self.review_size_scope = folder.strip().strip("/")
        location = f"below {self.review_size_scope}" if self.review_size_scope else "whole preview"
        self.size_scope_label.set_text(f"Location: {location}")
        self.size_scope_clear.set_visible(bool(self.review_size_scope))
        self.size_filter_expander.set_expanded(True)
        self._refill()

    def _sort_tree(self, _drop_down, _param) -> None:
        self._tree_sort = ("name", "size_desc", "size_asc")[self.sort_results.get_selected()]
        self._refill()

    def _tree_nodes(self, visible: list[Listed]):
        nodes = build_tree(
            visible,
            minimum_size=self.review_min_size,
            size_target=self.review_size_target,
            size_scope=self.review_size_scope,
            sort_by=self._tree_sort,
        )
        if (self.review_min_size or self._tree_sort != "name") and len(visible) >= _PREVIEW_ASYNC_THRESHOLD:
            self._pending_tree = []
            self._replace_tree_model = False
            self._live_refill_pending = False
            self._visible_changes = {row.change.rel: row.change for row in visible}
            self.store = Gio.ListStore.new(PlanRow)
            self._reset_tree_model()
            self._refresh_detail()
            GLib.idle_add(self._fill_pending_sized_tree, nodes, self._fill_generation, priority=GLib.PRIORITY_DEFAULT_IDLE)
            return nodes
        return nodes

    def _fill_pending_sized_tree(self, nodes, generation: int) -> bool:
        if generation != self._fill_generation:
            return False
        self._pending_tree = [(None, node) for node in reversed(nodes)]
        GLib.idle_add(self._fill_some, generation)
        return False

    def _set_tree_expanded(self, _button, expanded: bool) -> None:
        self._tree_default_expanded = expanded
        self._tree_state_overrides.clear()
        self._refresh_tree_layout()

    def _remember_tree_state(self, _view, tree_iter, _path, expanded: bool) -> None:
        if self._record_tree_state:
            rel = self.tree_store.get_value(tree_iter, 4)
            if rel:
                self._tree_state_overrides[rel] = expanded

    def _refresh_tree_layout(self) -> None:
        self._record_tree_state = False
        try:
            if self._tree_default_expanded:
                self.view.expand_all()
            else:
                self.view.collapse_all()
            for rel, expanded in self._tree_state_overrides.items():
                tree_iter = self._folder_iters.get(rel)
                if tree_iter is None:
                    continue
                path = self.tree_store.get_path(tree_iter)
                if expanded:
                    self.view.expand_to_path(path)
                else:
                    self.view.collapse_row(path)
        finally:
            self._record_tree_state = True

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
        self._fill_generation += 1
        visible = [row for row in self.rows if matches(row, **self.filters)]
        if len(self.rows) >= _PREVIEW_ASYNC_THRESHOLD and (self.review_min_size or self._tree_sort != "name"):
            self._tree_nodes(visible)
            return
        if len(self.rows) >= _PREVIEW_ASYNC_THRESHOLD:
            # Keep planning cheap, and yield GTK after each small render batch.
            self._pending_tree = [(None, node) for node in reversed(build_tree(visible))]
            self._visible_changes = {row.change.rel: row.change for row in visible}
            self._replace_tree_model = True
            self._refresh_detail()
            self.shown.set_text(f"Updating visible list: {len(visible)} files…")
            GLib.idle_add(self._fill_some, self._fill_generation)
            return
        self._pending_tree = []
        self._replace_tree_model = False
        self._live_refill_pending = False
        if (self.review_min_size or self._tree_sort != "name") and len(self.rows) >= _PREVIEW_ASYNC_THRESHOLD:
            self._tree_nodes([row for row in self.rows if matches(row, **self.filters)])
            return
        self.store = Gio.ListStore.new(PlanRow)
        for row in visible:
            self.store.append(PlanRow(row.change))
        self._fill_tree(visible)
        self._refresh_detail()

    def _reset_tree_model(self) -> None:
        self.tree_store = Gtk.TreeStore.new([str, str, str, str, str, bool])
        # Replace only the GTK model. Exclusion/filter helpers own the flat store.
        self.view.set_model(self.tree_store)
        self._folder_iters = {}
        self._file_iters = {}

    def _node_values(self, node) -> list:
        label = node.name if node.is_file else f"{node.name}    {node.size_text}"
        status = self._completed_rows.get(node.rel, node.status)
        return [label, node.size_text, node.direction, status, node.rel, node.rel in self._completed_rows]

    def _fill_tree(self, visible: list[Listed]) -> None:
        self._reset_tree_model()

        def add(parent, node) -> None:
            current = self.tree_store.append(parent, self._node_values(node))
            if node.is_file:
                self._file_iters[node.rel] = current
            else:
                self._folder_iters[node.rel] = current
            for child in node.children:
                add(current, child)

        for node in self._tree_nodes(visible):
            add(None, node)
        self._refresh_tree_layout()

    def _fill_some(self, generation: int) -> bool:
        if generation != self._fill_generation:
            return False
        if self._replace_tree_model:
            self._reset_tree_model()
            self.store = Gio.ListStore.new(PlanRow)
            self._replace_tree_model = False
        for _ in range(min(_PREVIEW_BATCH_SIZE, len(self._pending_tree))):
            parent, node = self._pending_tree.pop()
            current = self.tree_store.append(parent, self._node_values(node))
            if node.is_file:
                self._file_iters[node.rel] = current
                self.store.append(PlanRow(self._visible_changes[node.rel]))
            else:
                self._folder_iters[node.rel] = current
            self._pending_tree.extend((current, child) for child in reversed(node.children))
        if self._pending_tree:
            return True
        if self._scan_active and self._live_refill_pending:
            self._live_refill_pending = False
            self._refill()
            return False
        self._refresh_tree_layout()
        self._refresh_detail()
        return False

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

    def _confirm_tree_folder_copy(self, relative: str) -> None:
        """Only a complete preview can launch the separate guarded copy action."""

        if self.partial:
            self.detail.set_text("This skipped scan is review-only. Rescan before choosing a separate folder copy.")
            return
        self.owner._confirm_new_folder(self.job.profile, relative)

    def _folder_delete_candidates(self, folder: str, side: str) -> list[Listed]:
        prefix = folder.replace("\\", "/").strip().strip("/") + "/"
        return [
            item for item in self.rows
            if item.change.rel.startswith(prefix)
            and (item.on_left if side == "left" else item.on_right)
            and item.change.action != Action.SKIPPED
        ]

    def _ask_folder_delete(self, folder: str, side: str) -> Gtk.Window | None:
        if self.partial or self._scan_active or not self._scan_complete:
            self.detail.set_text("Complete the scan before adding folder deletions to a reviewed plan.")
            return None
        if self._running:
            self.detail.set_text("Wait for the current operation before changing folder deletions.")
            return None
        selected = self._folder_delete_candidates(folder, side)
        target = side.capitalize()
        window = Gtk.Window(transient_for=self, modal=True, title="Add Folder Deletions to Plan")
        window.set_default_size(620, 250)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{edge}")(18)
        count = len(selected)
        message = (
            f"Add {count} in-scope files ({format_bytes(sum(item.change.size for item in selected))}) "
            f"under {folder} on {target} to the deletion plan?"
        )
        box.append(Gtk.Label(label=message, xalign=0, wrap=True))
        warning = Gtk.Label(
            xalign=0, wrap=True,
            label="Nothing is deleted now. Review the changed rows, then Start opens the normal deletion confirmation. All Older recovery and deletion safeguards still apply.",
        )
        box.append(warning)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: window.close())
        confirm = Gtk.Button(label="Add Deletions to Plan", css_classes=["destructive-action"])
        window.folder_delete_confirm_button = confirm
        confirm.set_sensitive(bool(selected))

        def add_to_plan(*_args) -> None:
            if self._delete_folder_files(folder, side):
                window.close()

        confirm.connect("clicked", add_to_plan)
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)
        window.set_child(box)
        window.set_default_widget(cancel)
        window.present()
        return window

    def _delete_folder_files(self, folder: str, side: str) -> bool:
        if self.partial or self._scan_active or not self._scan_complete or self._running:
            self.detail.set_text("A current, complete, idle preview is required before changing the folder-deletion plan.")
            return False
        selected = self._folder_delete_candidates(folder, side)
        if not selected:
            self.detail.set_text(f"No known in-scope files under {folder} on {side}; nothing was added to the plan.")
            return False
        action = Action.DELETE_LEFT if side == "left" else Action.DELETE_RIGHT
        for item in selected:
            item.change = Change(
                item.change.rel,
                action,
                "chosen folder deletion in preview",
                item.change.size,
            )
        count = len(selected)
        total = sum(item.change.size for item in selected)
        self._refill()
        self.detail.set_text(f"Added {count} folder deletions ({format_bytes(total)}) on {side}. Review them, then Start for the normal deletion confirmation.")
        return True

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
        actions = ([] if self.partial else [
            ("Copy whole new folder…", "Only when the matching destination is absent; uses no scanned plan and never overwrites or deletes", lambda: self._confirm_tree_folder_copy(rel)),
        ]) + [
            ("L ──▶ R", "Copy this folder left to right", lambda: self._set_folder_direction(rel, "right")),
            ("L ◀── R", "Copy this folder right to left", lambda: self._set_folder_direction(rel, "left")),
            ("Delete folder files on Right…", "Add known right-side files to the reviewed deletion plan; nothing is deleted now", lambda: self._ask_folder_delete(rel, "right")),
            ("Delete folder files on Left…", "Add known left-side files to the reviewed deletion plan; nothing is deleted now", lambda: self._ask_folder_delete(rel, "left")),
            ("Open left folder", "", lambda: open_uri(Path(self.job.profile.left) / rel)),
            ("Open right folder", "", lambda: open_uri(Path(self.job.profile.right) / rel)),
            ("Add exclusion…", "Choose profile/global, a relative path or name, and edit the rule", lambda: self._ask_exclusion(rel, folder=True)),
            ("Filter sizes under this folder", "Review only; change what is displayed without changing the synchronization plan", lambda: self._set_size_scope(None, rel)),
            ("Ignore this folder for this run", "", lambda: self._ignore_folder(rel)),
        ]
        for text, tip, callback in actions:
            button = Gtk.Button(label=text)
            if tip:
                button.set_tooltip_text(tip)
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
            ("Add exclusion…", "Choose profile/global, a relative path or name, and edit the rule", lambda: self._ask_exclusion(row)),
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

    def _add_exclusion(self, row: PlanRow) -> Gtk.Window:
        return self._ask_exclusion(row)

    def _ask_exclusion(self, selected: PlanRow | str, *, folder: bool | None = None) -> Gtk.Window:
        rel = selected if isinstance(selected, str) else selected.change.rel
        rel = rel.replace("\\", "/").strip().strip("/")
        is_folder = isinstance(selected, str) if folder is None else folder
        window = Gtk.Window(transient_for=self, modal=True, title="Add Exclusion")
        window.set_default_size(620, 300)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        form = Gtk.Grid(column_spacing=12, row_spacing=10)
        scope = Gtk.DropDown.new_from_strings(["This profile", "Global exclusions (all profiles)"])
        match = Gtk.DropDown.new_from_strings(["Full relative path", "Folder or file name (any location)"])
        entry = Gtk.Entry(text=rel, hexpand=True, activates_default=True)
        window.exclusion_scope = scope
        window.exclusion_match = match
        window.exclusion_entry = entry
        for row, (text, widget) in enumerate((("Save to", scope), ("Match", match), ("Exclusion rule", entry))):
            form.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        box.append(form)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        box.append(hint)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: window.close())
        save = Gtk.Button(label="Add Exclusion", css_classes=["suggested-action"])
        window.exclusion_save_button = save
        buttons.append(cancel)
        buttons.append(save)
        box.append(buttons)

        def explain(*_args) -> None:
            global_scope = scope.get_selected() == 1
            target = "all profiles" if global_scope else f"profile {self.job.profile.name}"
            if match.get_selected() == 0:
                text = f"Relative to each profile root; matches this exact {'folder and its contents' if is_folder else 'path'} on both sides in {target}."
            else:
                text = f"Matches this folder or file name at any depth, on both sides in {target}."
            if global_scope and global_exclusion_mode() == ExclusionMode.DONT_COPY:
                text += " Global Don't Copy still applies; rescan to calculate any Exact Mirror deletions."
            hint.set_text(text)

        def choose_match(*_args) -> None:
            entry.set_text(rel.rsplit("/", 1)[-1] if match.get_selected() else rel)
            explain()

        def on_save(*_args) -> None:
            if not entry.get_text().strip():
                hint.set_text("Enter a relative path or folder/file name before saving.")
                return
            if self._save_exclusion(entry.get_text(), scope="global" if scope.get_selected() else "profile"):
                window.close()

        match.connect("notify::selected", choose_match)
        scope.connect("notify::selected", explain)
        save.connect("clicked", on_save)
        entry.connect("activate", on_save)
        window.set_child(box)
        window.set_default_widget(save)
        explain()
        window.present()
        return window

    def _exclude_folder(self, row: PlanRow) -> None:
        rel = row.change.rel.replace("\\", "/")
        if "/" not in rel:
            self.detail.set_text("This file is in the profile folder. Exclude it by name instead.")
            return
        self._save_exclusion(rel.rsplit("/", 1)[0])

    def _save_exclusion(self, mask: str, *, scope: str = "profile") -> bool:
        mask = mask.strip()
        if not mask:
            return False
        profile = self.job.profile
        with self.owner._session_rule_lock(profile.name):
            self._live_permanent_masks.add(mask)
            if scope == "global":
                masks = global_exclusions()
                if mask not in masks:
                    set_global_exclusions([*masks, mask])
            else:
                if mask not in profile.exclusions:
                    profile.exclusions.append(mask)
                    save_profile(profile)
            mode = global_exclusion_mode() if scope == "global" else profile.exclusion_mode

        def allowed(relative: str) -> bool:
            return not path_matches(relative, mask)

        self.rows = [
            item for item in self.rows
            if allowed(item.change.rel) and (not item.change.dest_rel or allowed(item.change.dest_rel))
        ]
        self._refill()
        self.owner.reload()
        if mode == ExclusionMode.DONT_COPY:
            self.detail.set_text(f"Saved exclusion: {mask}. Rescan to calculate any Exact Mirror Don't Copy deletions.")
        return True

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
        with self.owner._session_rule_lock(self.job.profile.name):
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
            with self.owner._session_rule_lock(name):
                if mask not in masks:
                    masks.append(mask)
            self.rows = [item for item in self.rows if Path(item.change.rel).suffix != suffix]
        else:
            with self.owner._session_rule_lock(name):
                self.owner.session_skips.setdefault(name, set()).add(row.change.rel)
            self._drop(row.change.rel)
            return
        self._refill()

    def _drop(self, rel: str) -> None:
        self.rows = [item for item in self.rows if item.change.rel != rel]
        self._refill()

    def _refresh_detail(self) -> None:
        if self._scan_active:
            self._show_live_side("Left")
            self._show_live_side("Right")
            self.detail.set_text("Live discovery is review-only. Start is unlocked only after the complete comparison.")
            return
        if not self._scan_complete:
            self.detail.set_text(f"{self.job.error or 'Scan stopped'}. Incomplete discovery is review-only and cannot be applied.")
            return
        planned = self._planned()
        left, right = status_lines(planned)
        self.left_status.set_text(left)
        self.right_status.set_text(right)
        visible = [self.store.get_item(index) for index in range(self.store.get_n_items())]
        summary = shown_summary([Listed(item.change, False, False) for item in visible])
        if self.review_min_size:
            target = {"files": "files", "folders": "folders", "all": "files and folders"}[self.review_size_target]
            location = self.review_size_scope or "whole preview"
            amount = self.size_amount.get_text().strip() or "0"
            unit = self.size_units.get_selected_item().get_string()
            summary += f"; minimum {amount} {unit} for {target} ({location})"
        if self._tree_sort != "name":
            summary += "; size-sorted within folders"
        self.shown.set_text(summary)
        if self.partial:
            text = "Review only. Add permanent or session exclusions, then Rescan. Partial plans cannot be applied."
        else:
            text = plan_summary(planned) + "  Filters hide rows. Start runs the whole plan. Right-click a folder to point L ──▶ R or L ◀── R."
        if self.job.result is not None and self.job.result.note:
            text += "  " + self.job.result.note
        self.detail.set_text(text)

    def _start(self, *_args) -> None:
        """A pending or partial listing must never become a file operation."""

        if self._scan_active or not self._scan_complete:
            self.detail.set_text("Discovery is not complete. Pending entries cannot be copied, moved, or deleted.")
            return
        if self.partial:
            self.detail.set_text("This is a partial, review-only scan. Add exclusions, then choose Rescan.")
            return
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

    def _rescan(self, *_args) -> None:
        self.close()
        self.owner._run_names({self.job.profile.name})

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
        if self._scan_active or not self._scan_complete or self.partial:
            self.detail.set_text("Only a complete comparison can be applied. Rescan after incomplete or skipped discovery.")
            return
        app = _owner_application(self.owner)
        if isinstance(app, SfsApplication) and not app.claim_profile(self.job.profile.name):
            self.detail.set_text("This profile is already scanning or copying in another OFS window. Wait for that operation to finish.")
            return
        self._running = True
        self.start_button.set_sensitive(False)
        progress = ProgressWindow(self, self.job.profile.name)
        self._copy_progress = progress
        self.copy_details = self._attach_operation_panel(progress, f"Copying {self.job.profile.name}")
        self._copy_planned = {change.rel: change for change in changes}
        self._copy_cache_invalidated = False
        self._last_copy_done = 0
        self._last_copy_errors = 0
        if self.scan_details is not None:
            self.scan_details.set_expanded(False)

        def work() -> None:
            report = ApplyReport()
            path = None
            try:
                report = apply_plan(
                    self.job.profile,
                    changes,
                    control=progress.control,
                    on_copy_progress=lambda update: GLib.idle_add(self._update_copy, progress, update, priority=GLib.PRIORITY_DEFAULT),
                    bytes_per_second=copy_speed_limit(),
                    listing=self.rows,
                )
            except Exception as exc:
                report.errors.append(str(exc))
            if report.copied or report.deleted or report.moved:
                try:
                    invalidate_cached_destinations(self.job.profile)
                except OSError as exc:
                    report.errors.append(f"destination cache: {exc}")
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
            try:
                record_run(self.job.profile.name, "copy", report.summary(), path)
            except OSError as exc:
                report.errors.append(f"run history: {exc}")
            GLib.idle_add(self._finished, progress, report, path, priority=GLib.PRIORITY_DEFAULT)

        threading.Thread(target=work, daemon=True).start()

    def _update_copy(self, progress: ProgressWindow, event: CopyProgress) -> bool:
        if progress is not self._copy_progress:
            return False
        progress.update(event)
        change = self._copy_planned.get(event.rel)
        mutating = change is not None and change.action in {
            Action.COPY_LEFT_TO_RIGHT, Action.COPY_RIGHT_TO_LEFT,
            Action.MOVE_ON_LEFT, Action.MOVE_ON_RIGHT,
            Action.DELETE_LEFT, Action.DELETE_RIGHT,
        }
        if mutating and self.job.profile.cache_destination and not self._copy_cache_invalidated:
            self._copy_cache_invalidated = True
            if self.copy_details is not None:
                self.copy_details.set_label(f"Copying {self.job.profile.name} · cache invalidated")
        if event.rel and event.completed > self._last_copy_done:
            self._last_copy_done = event.completed
            if change is not None and event.errors == self._last_copy_errors:
                label = {
                    Action.COPY_LEFT_TO_RIGHT: "Copied",
                    Action.COPY_RIGHT_TO_LEFT: "Copied",
                    Action.MOVE_ON_LEFT: "Moved",
                    Action.MOVE_ON_RIGHT: "Moved",
                    Action.DELETE_LEFT: "Deleted",
                    Action.DELETE_RIGHT: "Deleted",
                    Action.ASK: "Skipped",
                }[change.action]
                for rel in (change.rel, change.dest_rel):
                    if not rel:
                        continue
                    self._completed_rows[rel] = f"{label} · {change.reason}"
                    tree_iter = self._file_iters.get(rel)
                    if tree_iter is not None:
                        self.tree_store.set_value(tree_iter, 3, self._completed_rows[rel])
                        self.tree_store.set_value(tree_iter, 5, True)
        self._last_copy_errors = event.errors
        return False

    def _finished(self, progress: ProgressWindow, report: ApplyReport, path: Path | None) -> bool:
        progress.allow_close()
        if self.copy_details is not None:
            self.copy_details.set_expanded(False)
            self.copy_details.set_label("Copy complete")
        if self._copy_progress is not None:
            self._copy_progress.pause_button.set_sensitive(False)
            self._copy_progress.cancel_button.set_sensitive(False)
        app = _owner_application(self.owner)
        if isinstance(app, SfsApplication):
            app.release_profile(self.job.profile.name)
        self._running = False
        text = report.summary()
        self.job.profile.last_result = f"{time.strftime('%Y-%m-%d %H:%M')}  {text}"
        save_profile(self.job.profile)
        self.owner.reload()
        action = self.completion_values[self.completion_action.get_selected()]
        power_note = ""
        if action != "none":
            label = self.completion_action.get_selected_item().get_string().lower()
            app = _owner_application(self.owner)
            active_copies = [
                window for window in (app.get_windows() if app is not None else [])
                if (
                    isinstance(window, ProgressWindow) and window is not progress and not window._allow_close
                ) or (
                    isinstance(window, Preview) and window is not self and window._running
                )
            ]
            if report.cancelled or report.errors:
                power_note = f" {label.capitalize()} was not run because the copy was canceled or had errors."
            elif active_copies:
                power_note = f" {label.capitalize()} was not run because another copy is still active."
            else:
                try:
                    run_completion_action(action)
                except PowerActionError as exc:
                    report.errors.append(f"after-completion power action: {exc}")
                    text = report.summary()
                    self.job.profile.last_result = f"{time.strftime('%Y-%m-%d %H:%M')}  {text}"
                    save_profile(self.job.profile)
                    self.owner.reload()
        self.detail.set_text(text + power_note)
        ResultsWindow(self, report, path).present()
        return False


class ScanWindow(Gtk.Window):
    """Live scan statistics, pause, per-profile skip, and whole-scan cancel."""

    def __init__(self, owner: Gtk.Window, title: str = "Scanning", *, estimates: dict[str, tuple[int, float]] | None = None):
        super().__init__(transient_for=owner, modal=False, title=title)
        app = _owner_application(owner)
        if app is not None:
            self.set_application(app)
        self.set_default_size(700, 280)
        self.control = RunControl()
        self._allow_close = False
        self._place = "Waiting for the first entry…"
        self._name = ""
        self._started = time.monotonic()
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._estimates = estimates or {}
        self._estimate = (0, 0.0)
        self._stats = {side: {"discovered": 0, "filtered": 0, "added": 0} for side in ("Left", "Right")}
        self.profile_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.profile_label.add_css_class("heading")
        self.side_label = Gtk.Label(xalign=0, label="Reading left")
        self.side_label.add_css_class("dim-label")
        heading = Gtk.Box(spacing=12)
        heading.append(self.profile_label)
        heading.append(self.side_label)
        self.file_label = _fixed_path_label(self._place)
        self.file_label.set_text(self._place)
        path_box = _path_line("Current path", self.file_label)
        self.count_label = Gtk.Label(xalign=0, wrap=True)
        self._update_counts()
        self.bar = Gtk.ProgressBar(hexpand=True)
        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.connect("clicked", self._toggle)
        self.skip_button = Gtk.Button(label="Skip Rest")
        self.skip_button.set_tooltip_text("Skip this profile's incomplete scan and continue with the next profile")
        self.skip_button.connect("clicked", self._skip)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        buttons.append(self.pause_button)
        buttons.append(self.skip_button)
        buttons.append(self.cancel_button)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "top", "bottom"):
            getattr(root, f"set_margin_{side}")(16)
        root.append(heading)
        self.scan_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        path_box.set_hexpand(True)
        self.scan_info.append(path_box)
        self.scan_info.append(self.count_label)
        root.append(self.scan_info)
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
        self.profile_label.set_text(name)
        self.side_label.set_text(reading)
        self._place = rel or "."
        self.file_label.set_tooltip_text(self._place)
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
        if self._allow_close:
            return False
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
        app = _owner_application(owner) if owner is not None else None
        if app is not None:
            self.set_application(app)
        self.set_default_size(680, 300)
        self.control = RunControl()
        self._allow_close = False
        self._started = time.monotonic()
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self.latest: CopyProgress | None = None
        self.file_label = _fixed_path_label("Starting…")
        path_box = _path_line("Current file", self.file_label)
        self.file_bar = Gtk.ProgressBar(show_text=True)
        self.file_count = Gtk.Label(xalign=0)
        self.count_label = Gtk.Label(xalign=0)
        self.count_label.add_css_class("monospace")
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
        root.append(path_box)
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
            speed = 0.0
            eta = "--:--"
        else:
            speed = self.latest.bytes_done / elapsed if elapsed > 0 and self.latest.bytes_done else 0.0
            remaining = max(0, self.latest.bytes_total - self.latest.bytes_done)
            eta = _duration(remaining / speed) if speed > 0 and remaining else "--:--"
        state = " · Paused" if self.control.paused else ""
        self.timing_label.set_text(
            f"Elapsed {_duration(elapsed):>8} · Speed {format_bytes(int(speed)):>10}/s · ETA {eta:>8}{state}"
        )

    def update(self, event: CopyProgress) -> bool:
        self.latest = event
        self.bar.set_fraction(event.overall_fraction)
        self.file_bar.set_fraction(event.current_fraction)
        prefix = "Paused. " if self.control.paused else ""
        path = event.rel or "Finishing…"
        self.file_label.set_text(prefix + path)
        self.file_label.set_tooltip_text(path)
        self.bar.set_tooltip_text(f"Overall: {format_bytes(event.bytes_done)} / {format_bytes(event.bytes_total)}")
        self.file_bar.set_tooltip_text(f"Current file: {format_bytes(event.current_bytes)} / {format_bytes(event.current_total)}")
        self.file_count.set_text(f"Current file: {format_bytes(event.current_bytes)} / {format_bytes(event.current_total)}")
        digits = max(1, len(str(event.total)))
        self.count_label.set_text(
            f"Operation {event.completed:>{digits}} / {event.total} · Processed {format_bytes(event.bytes_done):>10} / {format_bytes(event.bytes_total)}\n"
            f"Copied {event.copied:{digits}} · Deleted {event.deleted:{digits}} · Moved {event.moved:{digits}} · Errors {event.errors:{digits}}"
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


class WholeFolderWindow(ProgressWindow):
    """Progress for streaming an explicitly new tree whose total is unknown."""

    def __init__(self, owner: MainWindow, profile: Profile, relative: str):
        super().__init__(owner, f"new folder: {profile.name}")
        self.owner = owner
        self.profile_name = profile.name
        self.job = JobResult(profile, ScanResult([]), None)
        self.relative = relative
        self.set_title(f"Copying new folder: {profile.name}")
        self.bar.set_text("Copying new folder…")
        self.bar.set_tooltip_text("Total size is not precomputed for a no-comparison folder copy")
        self.count_label.set_text("Files copied        0 · Copied           0 B\nErrors               0")
        self.file_label.set_text(relative)
        self.file_label.set_tooltip_text(relative)

    def update(self, event: CopyProgress) -> bool:
        self.latest = event
        prefix = "Paused. " if self.control.paused else ""
        self.file_label.set_text(prefix + event.rel)
        self.file_label.set_tooltip_text(event.rel)
        self.file_bar.set_fraction(event.current_fraction)
        self.file_bar.set_tooltip_text(f"Current file: {format_bytes(event.current_bytes)} / {format_bytes(event.current_total)}")
        self.file_count.set_text(f"Current file: {format_bytes(event.current_bytes):<10} / {format_bytes(event.current_total)}")
        self.count_label.set_text(
            f"Files copied {event.copied:>8} · Copied {format_bytes(event.bytes_done):>10}\n"
            f"Errors {event.errors:>15}"
        )
        self._show_timing()
        self.bar.pulse()
        return False

    def _tick(self) -> bool:
        if self._allow_close:
            return False
        self.bar.pulse()
        self._show_timing()
        return True


class RunHistoryWindow(Gtk.Window):
    """Read-only, searchable activity across profiles; no run can be replayed here."""

    def __init__(self, owner: MainWindow):
        super().__init__(transient_for=owner, modal=False, title="Run History")
        self.set_application(_owner_application(owner))
        self.owner = owner
        self.set_default_size(1050, 600)
        header = Gtk.HeaderBar()
        refresh = Gtk.Button()
        _button_icon(refresh, "Refresh", "view-refresh-symbolic")
        refresh.set_tooltip_text("Reload saved run history")
        refresh.connect("clicked", lambda *_args: self._refresh())
        header.pack_end(refresh)
        self.set_titlebar(header)
        self.search = Gtk.SearchEntry(placeholder_text="Search profiles, dates, types, or results")
        self.search.set_margin_start(12)
        self.search.set_margin_end(12)
        self.search.set_margin_top(10)
        self.search.connect("search-changed", self._filter)
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["history-list"])
        self.list_box.connect("row-selected", self._selected)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=280)
        scroll.set_child(self.list_box)
        scroll.set_margin_start(12)
        scroll.set_margin_end(12)
        self.count = Gtk.Label(xalign=0, hexpand=True)
        self.count.add_css_class("dim-label")
        self.view_log = Gtk.Button(label="View Log…")
        self.view_log.set_tooltip_text("Open the selected complete copy log in the desktop application for text files")
        self.view_log.connect("clicked", self._open_log)
        self.view_log.set_sensitive(False)
        buttons = Gtk.Box(spacing=8)
        buttons.append(self.count)
        buttons.append(self.view_log)
        buttons.set_margin_start(12)
        buttons.set_margin_end(12)
        buttons.set_margin_top(8)
        buttons.set_margin_bottom(12)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        headings = Gtk.Box(spacing=12)
        for edge, size in (("start", 30), ("end", 12)):
            getattr(headings, f"set_margin_{edge}")(size)
        for title, width in (("When", 19), ("Profile", 24), ("Type", 10)):
            headings.append(Gtk.Label(label=title, xalign=0, width_chars=width, css_classes=["column-heading"]))
        headings.append(Gtk.Label(label="Result", xalign=0, hexpand=True, css_classes=["column-heading"]))
        root.append(self.search)
        root.append(headings)
        root.append(scroll)
        root.append(buttons)
        self.set_child(root)
        self._entries = []
        self._filtered = []
        self._selected_entry = None
        self._refresh()

    def _refresh(self) -> None:
        self._entries = list_history()
        self._selected_entry = None
        self._populate()

    def _filter(self, *_args) -> None:
        self._populate()

    def _populate(self) -> None:
        query = self.search.get_text().strip().lower()
        self._filtered = [
            entry for entry in self._entries
            if not query or query in " ".join((entry.profile, entry.when, entry.kind, entry.summary)).lower()
        ]
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        for entry in self._filtered:
            stamp = Gtk.Label(label=entry.when, xalign=0, width_chars=19, css_classes=["monospace"])
            profile = Gtk.Label(label=entry.profile, xalign=0, ellipsize=Pango.EllipsizeMode.END, width_chars=24, max_width_chars=24)
            profile.set_tooltip_text(entry.profile)
            kind = {"scan": "Scan", "copy": "Copy", "folder-copy": "New folder"}[entry.kind]
            kind_label = Gtk.Label(label=kind, xalign=0, width_chars=10, css_classes=["dim-label"])
            summary = Gtk.Label(label=entry.summary, xalign=0, wrap=True, hexpand=True, selectable=True)
            _time, _body, tone = _result_parts(entry.summary, "")
            if entry.kind == "scan" and entry.summary in {"Volume unavailable", "Scan unavailable"}:
                tone = "result-error"
            elif entry.kind == "scan" and (entry.summary == "Skipped" or entry.summary.startswith("Partial scan")):
                tone = "result-warn"
            if tone:
                summary.add_css_class(tone)
            content = Gtk.Box(spacing=12)
            for widget in (stamp, profile, kind_label, summary):
                content.append(widget)
            row = Gtk.ListBoxRow(child=content)
            row.history_entry = entry
            self.list_box.append(row)
        if self._filtered:
            self.list_box.select_row(self.list_box.get_row_at_index(0))
        else:
            self._selected_entry = None
            self.view_log.set_sensitive(False)
        noun = "activity" if len(self._entries) == 1 else "activities"
        self.count.set_text(f"{len(self._filtered)} of {len(self._entries)} {noun}, newest first")

    def _selected(self, _box, row) -> None:
        self._selected_entry = getattr(row, "history_entry", None) if row is not None else None
        path = self._selected_entry.log_path if self._selected_entry is not None else None
        self.view_log.set_sensitive(path is not None and path.is_file())

    def _open_log(self, *_args) -> None:
        if self._selected_entry is not None and self._selected_entry.log_path is not None:
            open_uri(self._selected_entry.log_path)


class ResultsWindow(Gtk.Window):
    def __init__(self, owner: Preview, report: ApplyReport, log_path: Path | None):
        super().__init__(transient_for=owner, modal=True, title="Copying Results")
        self.set_application(_owner_application(owner))
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
            failed_paths = _failed_relative_paths(report.errors)
            common_folder = _failed_common_folder(failed_paths)
            if failed_paths:
                self.exclude_paths_button = Gtk.Button(label=f"Exclude {len(failed_paths)} failing path(s)…")
                self.exclude_paths_button.set_tooltip_text("Add each failing path as a literal exclusion on this profile, after confirmation")
                self.exclude_paths_button.connect("clicked", lambda *_: self._exclude_failed(failed_paths))
                self.exclude_common_button = Gtk.Button(label="Exclude common folder…")
                self.exclude_common_button.set_sensitive(bool(common_folder))
                self.exclude_common_button.set_tooltip_text(f"Exclude {common_folder} and everything under it" if common_folder else "The failures have no common folder below the profile root")
                self.exclude_common_button.connect("clicked", lambda *_: self._exclude_failed([common_folder], folder=True))
                exclusions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
                exclusions.append(self.exclude_paths_button)
                exclusions.append(self.exclude_common_button)
                root.append(exclusions)
            self.exclusion_notice = Gtk.Label(xalign=0, wrap=True, css_classes=["result-ok"])
            root.append(self.exclusion_notice)
        root.append(buttons)
        self.set_child(root)

    def _close_preview(self, *_args) -> bool:
        """One close is enough: neither the preview nor the progress window stay behind."""

        self.owner.close()
        return False

    def _exclude_failed(self, masks: list[str], *, folder: bool = False) -> None:
        if not masks or not masks[0]:
            return
        dialog = Gtk.AlertDialog()
        title = f"Exclude the common folder {masks[0]!r}?" if folder else f"Exclude {len(masks)} failing path(s)?"
        dialog.set_message(title)
        shown = "\n".join(masks[:12])
        if len(masks) > 12:
            shown += f"\n… and {len(masks) - 12} more"
        dialog.set_detail(f"Add to {self.profile_name}'s permanent exclusions:\n\n{shown}\n\nNo files are deleted. Folder exclusions include everything under the folder. After saving, choose Retry Failed… to rescan.")
        dialog.set_buttons(["Cancel", "Save exclusions"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._exclude_failed_chosen, masks)

    def _exclude_failed_chosen(self, dialog, result, masks: list[str]) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        profile = self.owner.job.profile
        added = [mask for mask in masks if mask and mask not in profile.exclusions]
        profile.exclusions.extend(added)
        save_profile(profile)
        self.owner.owner.reload()
        self.exclusion_notice.set_text(
            f"Saved {len(added)} new exclusion(s) on this profile. Choose Retry Failed… for a fresh complete scan."
        )
        self.exclude_paths_button.set_sensitive(False)
        self.exclude_common_button.set_sensitive(False)

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
        self._visible_fields = list(FIELDS)
        self.field_search = Gtk.SearchEntry(placeholder_text="Search settings (e.g. cache)")
        self.field_search.set_tooltip_text("Filter this field picker without changing a profile")
        self.field_search.connect("changed", self._filter_fields)
        self.field = Gtk.DropDown.new_from_strings([item.label for item in FIELDS])
        self.field.set_enable_search(True)
        self.field.set_tooltip_text("Choose one setting to change. Search above for cache to find the persistent destination metadata On/Off setting.")
        self.field.connect("notify::selected", lambda *_: self._show_value())
        self.value_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.value_widget: Gtk.Widget | None = None
        self.mask_operation: Gtk.DropDown | None = None
        self.preview = Gtk.Label(xalign=0, wrap=True)
        self.preview.set_text(f"{len(profiles)} checked profiles. Choose one field to change; other fields are preserved. Search for cache, then choose Off or On.")
        self.apply = Gtk.Button(label="Preview Changes…")
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self._preview_changes)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text("Masks can be added to each profile without removing existing rules, or can replace that profile's whole list. Confirmation is required before anything is saved.")
        hint.add_css_class("dim-label")
        root.append(self.field_search)
        for label, widget in (("Field to change", self.field), ("New value", self.value_box)):
            root.append(Gtk.Label(label=label, xalign=0))
            root.append(widget)
        root.append(self.preview)
        root.append(hint)
        root.append(self.apply)
        self.set_child(root)
        self._show_value()

    def _field_definition(self):
        selected = self.field.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self._visible_fields):
            return None
        return self._visible_fields[selected]

    def _filter_fields(self, *_args) -> None:
        previous = self._field_definition()
        query = self.field_search.get_text().strip().lower()
        self._visible_fields = [
            item for item in FIELDS
            if not query or query in item.label.lower() or query in item.key.replace("_", " ")
        ]
        self.field.set_model(Gtk.StringList.new([item.label for item in self._visible_fields]))
        selected = next(
            (index for index, item in enumerate(self._visible_fields) if previous and item.key == previous.key),
            0,
        )
        self.field.set_selected(selected)
        self._show_value()

    def _show_value(self) -> None:
        while child := self.value_box.get_first_child():
            self.value_box.remove(child)
        definition = self._field_definition()
        self.mask_operation = None
        self.apply.set_sensitive(definition is not None)
        if definition is None:
            self.value_widget = Gtk.Label(label="No matching settings", xalign=0)
            self.preview.set_text("No settings match this search. No profile was changed.")
            self.value_box.append(self.value_widget)
            return
        if definition.kind == "choice":
            self.value_widget = Gtk.DropDown.new_from_strings([label for label, _value in definition.options])
        elif definition.kind in {"number", "decimal"}:
            decimal = definition.kind == "decimal"
            self.value_widget = Gtk.SpinButton.new_with_range(definition.minimum, definition.maximum, 0.1 if decimal else 1)
            if decimal:
                self.value_widget.set_digits(2)
            self.value_widget.set_value(definition.minimum)
        else:
            self.value_widget = Gtk.Entry(hexpand=True)
            if definition.kind == "masks":
                self.value_widget.set_placeholder_text("One or several masks separated by ; or newline")
                self.mask_operation = Gtk.DropDown.new_from_strings(["Add to existing masks", "Replace existing masks"])
                self.value_box.append(self.mask_operation)
            elif definition.kind == "size":
                self.value_widget.set_placeholder_text("512, 1.5 KB, 2M, or empty for no limit")
            elif definition.key == "group_override":
                self.value_widget.set_placeholder_text("Group / subgroup, or empty to use the name-derived group")
            elif definition.key.endswith("_volume"):
                self.value_widget.set_placeholder_text("Required disk volume label, or empty for no label check")
        self.value_box.append(self.value_widget)

    def _preview_changes(self, *_args) -> None:
        definition = self._field_definition()
        if definition is None:
            self.preview.set_text("No setting is selected. No profile was changed.")
            return
        if definition.kind == "choice":
            assert isinstance(self.value_widget, Gtk.DropDown)
            value = definition.options[self.value_widget.get_selected()][1]
        elif definition.kind == "number":
            assert isinstance(self.value_widget, Gtk.SpinButton)
            value = self.value_widget.get_value_as_int()
        elif definition.kind == "decimal":
            assert isinstance(self.value_widget, Gtk.SpinButton)
            value = self.value_widget.get_value()
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
        super().__init__(
            application_id=os.environ.get("OFS_APP_ID") or os.environ.get("SFS_APP_ID") or "com.ofs.LocalSync",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._active_profiles: set[str] = set()
        self._profile_operation_lock = threading.Lock()

    def open_new_window(self) -> MainWindow:
        apply_appearance(theme())
        window = MainWindow(self)
        window.present()
        return window

    def profile_is_active(self, name: str) -> bool:
        with self._profile_operation_lock:
            return name in self._active_profiles

    def claim_profile(self, name: str) -> bool:
        """Prevent duplicate scans/copies across every main window in this app."""

        with self._profile_operation_lock:
            if name in self._active_profiles:
                return False
            self._active_profiles.add(name)
            return True

    def release_profile(self, name: str) -> None:
        with self._profile_operation_lock:
            self._active_profiles.discard(name)

    def do_activate(self) -> None:
        apply_appearance(theme())
        active = self.props.active_window
        window = active if isinstance(active, MainWindow) else next(
            (candidate for candidate in self.get_windows() if isinstance(candidate, MainWindow)),
            None,
        )
        if window is None:
            window = MainWindow(self)
        for candidate in self.get_windows():
            if isinstance(candidate, (ScanWindow, ProgressWindow)) and candidate.get_child() is None:
                candidate.hide()
        window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        arguments = command_line.get_arguments()
        if arguments[1:] == ["--new-window"]:
            self.open_new_window()
            return 0
        if arguments[1:]:
            command_line.printerr("Usage: ofs [--new-window]\n")
            return 2
        self.activate()
        return 0


def main(arguments: list[str] | None = None) -> int:
    app = SfsApplication()
    return app.run(arguments)
