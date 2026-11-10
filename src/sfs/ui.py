"""GTK 4 window: profile groups, a dry-run scan, and the preview actions."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from sfs.apply import ApplyReport, RunControl, apply_plan
from sfs.compare import Action, Change, format_bytes, plan_summary
from sfs.jobs import JobResult, run_checked
from sfs.history import delete_history
from sfs.legacy import import_cfg, import_global_masks
from sfs.masks import path_matches
from sfs.model import Direction, ExclusionMode, Profile, SyncMode, parse_size
from sfs.review import (
    SAFETY_SECONDS,
    Listed,
    log_body,
    matches,
    outcome_headline,
    result_rows,
    safety_text,
    build_tree,
    point_change,
    shown_summary,
    status_lines,
    without_deletes,
    write_log,
)
from sfs.scan import remember
from sfs.settings import (
    compare_command,
    global_exclusion_mode,
    global_exclusions,
    set_compare_command,
    set_global_exclusion_mode,
    set_global_exclusions,
)
from sfs.store import config_dir, delete_profile, duplicate_profile, install_imported, load_profiles, replace_in_paths, save_profile

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

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        import_button = Gtk.Button(label="Import")
        import_button.connect("clicked", self._import)
        new_button = Gtk.Button(label="New")
        new_button.connect("clicked", self._new_profile)
        replace_button = Gtk.Button(label="Replace paths")
        replace_button.connect("clicked", self._replace_paths)
        self.run_button = Gtk.Button(label="Run checked")
        self.run_button.add_css_class("run")
        self.run_button.connect("clicked", self._run)
        settings_button = Gtk.Button(label="Settings")
        settings_button.connect("clicked", self._settings)
        header.pack_start(import_button)
        header.pack_start(new_button)
        header.pack_start(replace_button)
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
        root.append(scrolled)
        root.append(self.status)
        self.set_child(root)
        _install_css()
        self.reload()

    def reload(self) -> None:
        self.profiles = load_profiles()
        self._rebuild()
        folder = config_dir() / "profiles"
        self.status.set_text(f"{len(self.profiles)} profiles in {folder}")

    def _rebuild(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        self._labels.clear()
        query = self.search.get_text().strip().lower()
        shown = [profile for profile in self.profiles if query in profile.name.lower() or query in profile.group.lower()]
        if not shown:
            text = "No profiles yet. Import reads SuperFlexibleSynchronizer.cfg and does not change it."
            if self.profiles:
                text = "No profiles match."
            self.list_box.append(Gtk.Label(label=text, xalign=0))
            return
        groups: dict[str, list[Profile]] = {}
        for profile in shown:
            groups.setdefault(profile.group, []).append(profile)
        self._applying_groups = True
        try:
            for group, members in groups.items():
                self.list_box.append(self._group(group, members))
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

    def _group(self, group: str, members: list[Profile]) -> Gtk.Widget:
        count = len(members)
        name = Gtk.Label(label=group, xalign=0, hexpand=True)
        name.add_css_class("group-name")
        tally = Gtk.Label(label=f"{count} profile" if count == 1 else f"{count} profiles")
        tally.add_css_class("group-count")
        heading = Gtk.Box(spacing=12)
        heading.add_css_class("group-heading")
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

    def _row(self, profile: Profile) -> Gtk.Widget:
        check = Gtk.CheckButton(label=profile.name)
        check.set_active(profile.name in self.checked)
        check.connect("toggled", self._toggled, profile.name)
        paths = Gtk.Label(xalign=0)
        paths.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        paths.add_css_class("dim-label")
        paths.set_text(f"{profile.left}    →    {profile.right}")
        result = Gtk.Label(xalign=0)
        result.add_css_class("dim-label")
        result.set_text(profile.last_result or profile.note)
        self._labels[profile.name] = result
        run = Gtk.Button(label="Run")
        run.add_css_class("run")
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
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.append(check)
        text.append(paths)
        if result.get_text():
            text.append(result)
        row = Gtk.Box(spacing=4)
        row.set_margin_top(2)
        row.set_margin_bottom(2)
        text.set_hexpand(True)
        row.append(text)
        row.append(run)
        row.append(edit)
        row.append(duplicate)
        row.append(delete)
        return row

    def _toggled(self, button: Gtk.CheckButton, name: str) -> None:
        if button.get_active():
            self.checked.add(name)
        else:
            self.checked.discard(name)

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

    def _new_profile(self, *_args) -> None:
        self._editor(None)

    def _edit(self, _button, name: str) -> None:
        profile = self._profile(name)
        if profile is not None:
            self._editor(profile)

    def _replace_paths(self, *_args) -> None:
        chosen = [profile for profile in self.profiles if profile.name in self.checked]
        if not chosen:
            self.status.set_text("Check the profiles whose paths should change.")
            return
        window = Gtk.Window(transient_for=self, modal=True, title="Replace paths")
        window.set_default_size(560, 220)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        find = Gtk.Entry()
        replace = Gtk.Entry()
        preview = Gtk.Label(xalign=0, wrap=True)
        preview.set_text(f"{len(chosen)} checked profiles. Type the folder text to replace.")

        def refresh(*_args) -> None:
            needle = find.get_text()
            count = 0
            sample = ""
            for profile in chosen:
                left = profile.left.replace(needle, replace.get_text()) if needle else profile.left
                right = profile.right.replace(needle, replace.get_text()) if needle else profile.right
                if needle and (left != profile.left or right != profile.right):
                    count += 1
                    if not sample:
                        sample = f"{profile.left}  →  {left}"
            preview.set_text(f"{count} of {len(chosen)} checked profiles would change. {sample}".strip())

        find.connect("changed", refresh)
        replace.connect("changed", refresh)
        apply = Gtk.Button(label="Replace")

        def on_apply(*_args) -> None:
            needle = find.get_text()
            if not needle:
                return
            changed = []
            for profile in chosen:
                if replace_in_paths(profile, needle, replace.get_text()):
                    changed.append(profile)
            if not changed:
                preview.set_text("No paths contained that text.")
                return
            window.close()
            self._confirm_path_replace(changed, needle, replace.get_text())

        apply.connect("clicked", on_apply)
        for label, widget in (("Find", find), ("Replace", replace)):
            box.append(Gtk.Label(label=label, xalign=0))
            box.append(widget)
        box.append(preview)
        box.append(apply)
        window.set_child(box)
        window.present()

    def _confirm_path_replace(self, changed: list[Profile], find: str, replace: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Replace paths in {len(changed)} profiles?")
        names = ", ".join(profile.name for profile in changed[:8])
        extra = "" if len(changed) <= 8 else f" and {len(changed) - 8} more"
        dialog.set_detail(
            f"“{find}” becomes “{replace}” in {names}{extra}. "
            "Their file databases are removed so the next run records the new folders."
        )
        dialog.set_buttons(["Cancel", "Replace"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        dialog.choose(self, None, self._path_replace_chosen, changed)

    def _path_replace_chosen(self, dialog, result, changed: list[Profile]) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            self.reload()
            return
        self._save_replaced_paths(changed)

    def _save_replaced_paths(self, changed: list[Profile]) -> None:
        from sfs.history import delete_history

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

    def _settings(self, *_args) -> None:
        window = Gtk.Window(transient_for=self, modal=True, title="Settings")
        window.set_default_size(560, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.append(Gtk.Label(label="Compare files with", xalign=0))
        entry = Gtk.Entry(text=compare_command())
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_text("The two file paths are added after this command. SFS_COMPARE overrides it for one launch.")
        hint.add_css_class("dim-label")
        box.append(entry)
        box.append(hint)
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
            set_compare_command(entry.get_text())
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

    def _editor(self, profile: Profile | None) -> None:
        existing = profile
        window = Gtk.Window(transient_for=self, modal=True, title="Profile")
        window.set_default_size(680, 760)
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
        mode = Gtk.DropDown.new_from_strings(["standard", "exact_mirror"])
        mode.set_selected(0 if existing is None or existing.mode == SyncMode.STANDARD else 1)
        direction = Gtk.DropDown.new_from_strings(["left_to_right", "right_to_left", "both"])
        direction_values = [Direction.LEFT_TO_RIGHT, Direction.RIGHT_TO_LEFT, Direction.BOTH]
        direction.set_selected(0 if existing is None else direction_values.index(existing.direction))
        exclusion = Gtk.DropDown.new_from_strings(["ignore_totally", "dont_copy"])
        exclusion.set_selected(0 if existing is None or existing.exclusion_mode == ExclusionMode.IGNORE_TOTALLY else 1)
        rules = None if existing is None else existing.timestamps
        small = Gtk.CheckButton(label="Ignore differences of 2 seconds", active=bool(rules and rules.ignore_small))
        hour = Gtk.CheckButton(label="Ignore a difference of exactly one hour", active=bool(rules and rules.ignore_exact_hour))
        seconds = Gtk.CheckButton(label="Ignore seconds", active=bool(rules and rules.ignore_seconds))
        ignore_time = Gtk.CheckButton(label="Ignore the timestamp", active=bool(rules and rules.ignore_timestamp))
        larger = Gtk.CheckButton(label="A larger file is newer", active=bool(rules and rules.larger_is_newer))
        older = Gtk.CheckButton(label="Keep replaced files under Older", active=bool(existing and existing.keep_older))
        older_keep = Gtk.SpinButton.new_with_range(1, 99, 1)
        older_keep.set_value(1 if existing is None or existing.older_keep < 1 else existing.older_keep)
        binary = Gtk.CheckButton(label="Binary compare when size matches (reads file bytes)", active=bool(existing and existing.binary_compare))
        track = Gtk.CheckButton(label="Remember files and detect moves and deletions", active=bool(existing and existing.track))
        masks = Gtk.TextView()
        masks.set_monospace(True)
        buffer = masks.get_buffer()
        if existing is not None:
            buffer.set_text("\n".join(existing.exclusions))
        scroll = Gtk.ScrolledWindow(min_content_height=100)
        scroll.set_child(masks)
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
            ("Exclusions", exclusion),
            ("Masks", scroll),
            ("Min size", min_size),
            ("Max size", max_size),
            ("Min age (days)", min_age),
            ("Max age (days)", max_age),
            ("Older versions", older_keep),
        ]
        for index, (label, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=1), 0, index, 1, 1)
            grid.attach(widget, 1, index, 1, 1)
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for widget in (small, hour, seconds, ignore_time, larger, older, binary, track):
            checks.append(widget)
        grid.attach(checks, 1, len(rows), 1, 1)
        save = Gtk.Button(label="Save")
        grid.attach(save, 1, len(rows) + 1, 1, 1)

        def on_save(*_args) -> None:
            from sfs.model import TimestampRules

            chosen = name.get_text().strip()
            if not chosen:
                return
            updated = Profile(
                name=chosen,
                left=left.get_text().strip(),
                right=right.get_text().strip(),
                left_volume=left_volume.get_text().strip(),
                right_volume=right_volume.get_text().strip(),
                include=["(all)"] if existing is None else list(existing.include),
                exclusions=[line.strip() for line in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).splitlines() if line.strip()],
                exclusion_mode=ExclusionMode.IGNORE_TOTALLY if exclusion.get_selected() == 0 else ExclusionMode.DONT_COPY,
                direction=direction_values[direction.get_selected()],
                mode=SyncMode.STANDARD if mode.get_selected() == 0 else SyncMode.EXACT_MIRROR,
                timestamps=TimestampRules(
                    ignore_small=small.get_active(),
                    ignore_exact_hour=hour.get_active(),
                    ignore_seconds=seconds.get_active(),
                    ignore_timestamp=ignore_time.get_active(),
                    larger_is_newer=larger.get_active(),
                ),
                keep_older=older.get_active(),
                older_keep=int(older_keep.get_value()),
                min_size=parse_size(min_size.get_text()),
                max_size=parse_size(max_size.get_text()),
                min_age_days=_days(min_age.get_text()),
                max_age_days=_days(max_age.get_text()),
                binary_compare=binary.get_active(),
                track=track.get_active(),
                last_result="" if existing is None else existing.last_result,
                note="" if existing is None else existing.note,
                imported_settings=False if existing is None else existing.imported_settings,
            )
            if existing is not None and existing.name != chosen:
                from sfs.history import delete_history
                from sfs.store import delete_profile

                delete_profile(existing.name)
                delete_history(existing.name)
            elif existing is not None and (existing.left != updated.left or existing.right != updated.right):
                from sfs.history import delete_history

                delete_history(chosen)
            save_profile(updated)
            window.close()
            self.reload()

        save.connect("clicked", on_save)
        window.set_child(grid)
        window.present()

    def _run_one(self, _button, name: str) -> None:
        self._run_names({name})

    def _run(self, *_args) -> None:
        chosen = {profile.name for profile in self.profiles if profile.name in self.checked}
        if not chosen:
            self.status.set_text("Check one or more profiles.")
            return
        self._run_names(chosen)

    def _run_names(self, chosen: set[str]) -> None:
        if not chosen:
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
        progress = ScanWindow(self, self._scan_label.removesuffix("…"))
        progress.present()

        def on_progress(name: str, side: str, rel: str, count: int) -> None:
            GLib.idle_add(progress.update, name, side, rel, count)

        def work() -> None:
            try:
                results = run_checked(
                    profiles,
                    checked=chosen,
                    session_skips=skips,
                    session_masks=masks,
                    control=progress.control,
                    on_progress=on_progress,
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
        self._safety(warning, changes)

    def _safety(self, text: str, changes: list[Change]) -> None:
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
                    on_progress=lambda index, total, rel, copied, deleted, moved: GLib.idle_add(
                        progress.update, index, total, rel, copied, deleted, moved
                    ),
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
    """Shown while folders are being read. Pause and Cancel act between files."""

    def __init__(self, owner: Gtk.Window, title: str = "Scanning"):
        super().__init__(transient_for=owner, modal=False, title=title)
        self.set_default_size(640, 180)
        self.control = RunControl()
        self._allow_close = False
        self._place = "Starting…"
        self.left_found = 0
        self.right_found = 0
        self.file_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.file_label.set_text(self._place)
        self.count_label = Gtk.Label(xalign=0)
        self.count_label.set_text("Left files found: 0    Right files found: 0")
        self.bar = Gtk.ProgressBar()
        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.connect("clicked", self._toggle)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel)
        buttons = Gtk.Box(spacing=8)
        buttons.append(self.pause_button)
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

    def update(self, name: str, side: str, rel: str, count: int) -> bool:
        if side == "Right":
            self.right_found = count
            reading = "Reading right"
        else:
            self.left_found = count
            reading = "Reading left"
        self._place = f"{name}    {reading}    {rel or '.'}"
        if not self.control.paused:
            self.file_label.set_text(self._place)
        self.count_label.set_text(
            f"Left files found: {self.left_found}    Right files found: {self.right_found}"
        )
        return False

    def _pulse(self) -> bool:
        if self._allow_close:
            return False
        self.bar.pulse()
        return True

    def _toggle(self, *_args) -> None:
        if self.control.paused:
            self.control.resume()
            self.pause_button.set_label("Pause")
            self.file_label.set_text(self._place)
        else:
            self.control.pause()
            self.pause_button.set_label("Resume")
            self.file_label.set_text("Paused. " + self._place)

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


class ProgressWindow(Gtk.Window):
    def __init__(self, owner: Preview, title: str):
        super().__init__(transient_for=owner, modal=True, title=f"Copying {title}")
        self.set_default_size(640, 180)
        self.control = RunControl()
        self._allow_close = False
        self.file_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.file_label.set_text("Starting…")
        self.count_label = Gtk.Label(xalign=0)
        self.bar = Gtk.ProgressBar()
        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.connect("clicked", self._toggle)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel)
        buttons = Gtk.Box(spacing=8)
        buttons.append(self.pause_button)
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

    def update(self, index: int, total: int, rel: str | None, copied: int, deleted: int, moved: int) -> bool:
        fraction = 0.0 if not total else index / total
        self.bar.set_fraction(min(1.0, fraction))
        if rel:
            self.file_label.set_text(rel)
        elif not self.control.paused:
            self.file_label.set_text("Finishing…")
        self.count_label.set_text(f"{index} / {total}    copied {copied}    deleted {deleted}    moved {moved}")
        return False

    def _toggle(self, *_args) -> None:
        if self.control.paused:
            self.control.resume()
            self.pause_button.set_label("Pause")
        else:
            self.control.pause()
            self.pause_button.set_label("Resume")
            self.file_label.set_text("Paused. " + self.file_label.get_text())

    def _cancel(self, *_args) -> None:
        self.control.stop()
        self.cancel_button.set_sensitive(False)
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
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        buttons.append(ok)
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


class SfsApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=os.environ.get("SFS_APP_ID", "com.sfs.LocalSync"))

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
        window.present()


def main() -> int:
    app = SfsApplication()
    return app.run(None)
