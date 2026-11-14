# Changelog

All notable changes to OFS are documented here. Entries are grouped by public release and consolidated from commit history; see the comparison links for individual commits.

## [Unreleased]

### Fixed

- Closing the **Copying Results** window after a clean set copy no longer cancels the next set's discovery or discards a next set that is already prepared. The report belongs to the set that was copied; the preview, which now scans or holds the next set, stays open.

## [0.5.0] - 2026-11-14

### Added

- Settings has **Stop a scan that makes no progress for (minutes, 0 = never)**, five minutes by default. An unavailable mount now stops with the path named and that side review-only, instead of the window appearing to hang; copying and deleting stay disabled until a rescan completes.
- A folder that cannot be read is no longer treated as an empty folder. OFS now records it, leaves every entry inside it undecided, and says which folder failed and why, so a mirror run cannot delete a good copy because the source folder could not be listed, and cannot try to write into a destination folder it cannot open. The destination cache is not saved while a folder is unreadable.
- Settings has **Scan in sets of (files, 0 = one complete scan)** for very large trees. Each set walks whole root names in sorted order up to the limit and is a complete plan for those names, so Start, deletes, moves, and safeguards work normally while memory follows the set instead of the whole tree. After a set is copied cleanly, OFS scans the next one in the same window; its copy still needs the normal Start. Set scans skip the destination cache and the file-history baseline, and a rename crossing two sets becomes a copy plus a delete.

### Changed

- Excluding a row or folder while a scan is still running now removes those rows from the live list in place instead of clearing and refilling the comparison, so the view stops jumping during discovery; entries found mid-rebuild are added afterwards rather than starting another rebuild.
- **Start** now checks the destination's free space first. When the plan copies more than the destination can hold, a confirmation shows the free and needed bytes and copies nothing until **Continue anyway** is chosen.
- Returning from a finished copy keeps the overview scrolled where it was; only a partly visible finished row is nudged into view, and a profile that is entirely off-screen never receives focus or moves the list, because GTK animates a focus scroll even for a row the user cannot see.
- Opening a folder in a large comparison now shows **1,000 files** before offering **Show N more file(s)…** (it was 200), and that row streams 1,000 per click. **Show all files** in the folder toolbar loads the selected folder completely and keeps it complete across sort changes; **Expand all** on such a plan opens the folders already loaded, one level per click.
- The scan result supports selecting several rows and folders at once. Right-clicking the selection offers **Add exclusions for N selected…** (one editable rule window for all of them), **Ignore N selected for this run**, and direction/unchanged/revert for every selected row. Keyboard shortcuts: Ctrl+E exclusions, Ctrl+I ignore, Ctrl+Shift+I ignore extensions, Ctrl+U unchanged, Ctrl+R revert, Ctrl+→/Ctrl+← point right/left, Ctrl+Shift+A show all files; a help button beside the folder controls lists them.
- A large comparison's **Show N more file(s)…** row now opens on a single click, and its right-click menu can stream **all** remaining files of that folder in the background. Repeat clicks no longer duplicate a folder's subfolder rows, and **Expand all** on such a plan opens the folders already loaded (one level per click) instead of collapsing them. Showing more still only adds rows; the synchronization plan never changes.
- **Exclude failing paths…** now opens an editable rule window seeded with suggestions instead of confirming one fixed list. Repeated failures that share a folder collapse into that folder (for example `config/systemd/user` instead of five files), unshared failures stay exact paths, and **Exclude common folder…** starts from the single shared folder when one exists. Rules can be edited, added, removed, and tested before saving; nothing is excluded before **Save exclusions**.
- Copy progress and Copying Results windows no longer block the rest of OFS. A modal window would make every other window unusable, so another profile's scan could not be started or cancelled while a copy ran or a result waited for an answer; per-profile locks still prevent duplicate work on the same profile.
- Changing a row, pointing a folder either way, or adding folder deletions now updates those rows and their folder totals in place instead of rebuilding the comparison tree, so a single action no longer costs a full pass over the plan on large previews.
- A comparison of 20,000 files or more now opens collapsed with only its top-level folders. Expanding a folder loads its children (the first 200, then **Show N more file(s)…** streams the next batch), so a huge plan costs rows only where you look; the plan, Start, and the deletion safeguards are unchanged.
- Folder rows name the work in the Status column ("427 undecided", "12 to copy to the right") instead of repeating a byte total that the Size column already shows.
- Exclusion masks are prepared once per scan instead of being re-parsed for every entry, which makes a large walk with many rules dramatically faster on the same trees; the matching rules are unchanged and checked against the original algorithm, including a randomized comparison. The live preview also stops adding rows after 50,000 paths and says so, so a million-entry scan no longer pins gigabytes of live rows before the comparison finishes; the completed plan still shows every row.
- Copying results group repeated failures by folder and reason, and a folder that cannot be opened fails once instead of once per file inside it. The remaining entries under it are skipped and counted, and the detail stays in the log and the tooltip.
- Rows, plan changes, and file metadata use slotted records, cutting the per-file object overhead for large comparisons.

## [0.4.8] - 2026-11-13

### Added

- A one-way profile whose destination folder is missing now asks whether to create it. **Create and copy all** creates only that folder inside its existing parent, forgets any destination snapshot from before it existed, and treats it as empty, so every source file that is not excluded is copied and nothing is deleted. Missing parent folders are never created, so an unmounted disk cannot be filled on the wrong filesystem; a missing source is still an error. `ofs scan` and `ofs run` accept `--create-destination` for the confirmed form.
- A profile that has never been copied now shows its last scan attempt in the overview row, with date and colour, so a folder that was renamed or moved reaches a visible error instead of an empty "Never run" row. A newer failed scan also outranks an older successful run, because it describes the profile's current state; a completed scan stays informational and never claims a clean sync.

### Fixed

- Closing every OFS window while a scan or copy is still running now ends the process once that operation finishes, instead of leaving an invisible instance running in the background.
- Clicking Scan no longer moves the profile overview back to the first profile. Every overview rebuild (scan start, scan finish, checkbox toggle) restores the saved scroll position and the focused row's offset; only filter and Expand-all changes start at the top.
- A scan that cannot run now says **Scan failed** in the window title, shows a red banner with the exact reason and path, names the unavailable side, and points at Edit for the path; a user-stopped scan still says **Scan stopped** and explains the review-only rows.
- A missing folder no longer reports "Volume unavailable" without a path. The row and the global history now say `folder not found: <path>`, while a genuinely absent or different disk still reports `volume is not mounted: <label> (<path>)`.
- A copy is no longer stopped by cache bookkeeping when the profile's destination cache is off: invalidation is skipped in that case and never creates the `cache/` folder. An enabled cache that cannot be invalidated still stops the copy before the first write.

### Changed

- Turning the destination cache Off now removes the stored snapshot, and renaming or deleting a profile removes it too, so a later re-enable cannot reuse metadata written before the switch.

## [0.4.7] - 2026-11-12

### Fixed

- Exact Mirror now detects a renamed folder as moves even when the archive holds thousands of same-named, same-sized files. Multiple candidates are told apart by how many trailing folders they share with the old path, so a folder renamed with a prefix or suffix still pairs file-for-file instead of becoming a whole-tree copy plus delete. True ties still stay copy/delete.

### Changed

- The scan and copy panels no longer repeat the profile name. The window title carries it once, the expanders read Scanning and Copying, and the scan panel leads with Reading left or Reading right.

## [0.4.6] - 2026-11-12

### Added

- The comparison has a **Reason** selector beside the other filters: Show All, Time, Size, Bigger source, Smaller source, Binary, Only source, and Only destination. It only changes which rows are shown, keeps parent folders while any matching file under them is visible, and never changes the plan.

### Fixed

- Pointing a folder at the side that holds the file now turns its delete rows into copies from that side. Reversing a destination-only folder brings the files back instead of only flipping the red delete arrow to the other side.

## [0.4.5] - 2026-11-12

### Added

- The scan preview is an ordinary top-level window, so it now shows minimize, maximize/restore, and close buttons next to each other.

### Fixed

- Scanning a very large tree no longer freezes the preview window. Live discovery appends rows without rebuilding the whole tree layout for every batch, and newly found folders are expanded in small idle batches instead of one whole-model pass. A folder you collapse stays collapsed even when deeper folders appear under it later.

## [0.4.4] - 2026-11-12

### Fixed

- A stopped scan no longer leaves a disabled **Scan incomplete** button or a permanent `Updating visible list…` message. The page now states that nothing can be copied from an incomplete scan, keeps the discovered rows as review-only, and offers a working **Rescan**.

## [0.4.3] - 2026-11-12

### Added

- The comparison window explains itself. Unchanged rows now name the reason they stay out of the plan, such as `unchanged: same size, timestamps ignored`, and a non-default timestamp rule adds one short line above the tree, for example that ignoring timestamps decides from size alone and does not read contents. Both are display-only and never change the plan.

## [0.4.2] - 2026-11-12

### Changed

- The scan and copy panels show the current path as one compact row with an inline caption instead of a bordered card, freeing about 60 px of height for the result tree while keeping the path ellipsized, selectable, and fully available in its tooltip.

## [0.4.1] - 2026-11-12

### Fixed

- Returning from a finished preview or result keeps the completed profile's whole row readable. The restored scroll position is now adjusted by the minimum amount needed, so the row is no longer left clipped at the bottom edge with only its name and checkbox visible.

## [0.4.0] - 2026-11-12

### Added

- Global run history lists scans and copy operations across profiles, survives restarts, backfills existing copy logs read-only, and offers search, refresh, and guarded log opening.
- Merged scanning, comparison, and comparison-copy progress into a live preview. Pending metadata fills the result tree without becoming a copy/delete decision.
- Live Ignore/Exclude works across current discovery, queued rows, and final results, with synchronized temporary rules and separate cache/history protection.
- Collapsible scan and copy panels provide more result-tree room. Completed operations are marked and use subdued rows without changing the plan.
- Closing an active live preview requests safe cancellation rather than losing track of the running operation.
- Live discovery now explicitly shows cache off, fresh discovery, uncached override, and actual cache reuse per destination side. Copy status shows when metadata has been invalidated.
- Scan-result exclusions offer editable profile/global scope and full-relative-path/name matching. Global additions preserve existing rules and mode and are rechecked against displayed, queued, future, and final live rows.
- Display-only minimum-size filtering supports files, folders, or both and whole-preview/selected-folder scope. Name and size sorting retain the directory hierarchy and plan.
- Folder results can add eligible known files on a chosen side to a reviewed per-file deletion plan after explicit scope/count confirmation, retaining all existing deletion safeguards and Older recovery.

### Changed

- Scan comparisons open maximized, with two-column Left/Right headers. Restored windows keep Start, Close, counts, and power choices pinned while filters scroll independently. Current paths are single-line and width-bounded, with statistics below.

- Exact Mirror automatic move inference now requires the same filename. Different basenames remain explicit copy and delete operations.

### Fixed

- Indexed move candidates by filename, kind, link target, and size before applying timestamp rules, eliminating quadratic comparisons on large move sets and avoiding oversized same-profile comparison delays.
- Adding, editing, or removing an include/exclusion rule retains the rule list and surrounding editor-page scroll positions, including when the focused Remove button is replaced.
- Scan/copy completion is ordered with its progress events and cannot starve behind lower-priority live-list rendering. Late statistics cannot re-enable completed scan controls.
- Live rule sharing keeps the worker's final plan consistent with visible Ignore/Exclude actions; cached metadata and history-derived deletes are filtered appropriately.
- Detached-overview copying retains profile lock/release and cross-window power safeguards; the common copy entry point directly refuses incomplete or partial scans.
- Normal activation restores or creates the main OFS window rather than focusing an empty detached scan-control window.
- Keep scan paths one line without resizing the path box from changing content, and keep context-menu action fonts consistent.
- Completed preview/result closure returns to the existing owner overview's selected profile and scroll position using a profile-name anchor and post-layout restoration, without changing checked profiles or reopening deliberately closed overviews.
- Multi Edit's visible Search settings field filters settings and exposes persistent cache Off/On without relying solely on keyboard search.

## [0.3.0] - 2026-11-12

### Added

- Columned overview rows with last-run timestamps, status icons, result tones, and status/date/path availability filters.
- Persistent profile disabling without deleting settings, history, or results.
- Whole-group tri-state selection, custom nested groups, and a collapsible virtual `(ungrouped)` section for large catalogs.
- Rule-based include and exclusion editors with path-match testing.
- Include filters, copy-only operation, and explicit global Ignore Totally / Don't Copy modes.
- Live discovered/filtered/added counters, per-profile Pause, Skip Rest, and Cancel, and approximate completed-run-based ETA.
- Review-only partial previews after Skip Rest, with exclusions and Rescan available but copy, power, CLI apply, history, cache, and ETA updates blocked.
- Serial, chunked, pauseable/cancelable copying with atomic temporary siblings, byte-level progress, optional MiB/s throttling, logs, results, and fresh-scan retry for failures.
- Recoverable deletions and replaced-file Older versions, with no-clobber restoration outside live roots.
- Checked-profile Multi Edit, profile JSON import/export, path replacement, and validated batch renaming.
- Configurable deletion-percentage, delete-all, and never-delete-file safeguards shared by GUI and apply paths.
- Follow, preserve, and skip symlink policies; optional permission and owner/group comparison and preservation.
- Standard copy/Exact Mirror controls, timestamp comparison policies, and optional binary comparison.
- Exact Mirror move pairing for unique source/destination candidates.
- System, Light, and Dark appearance choices, including a header light/dark toggle and standard symbolic header icons.
- Optional per-profile destination metadata caching, reporting cached status and age, with GUI and CLI uncached overrides.
- Independent parallel scans and a shared same-profile lock; a checked batch retains a combined summary.
- Explicit whole-new-folder copying that skips comparison, streams with filters and symlink policies, and installs atomically only into an absent destination.
- Per-preview optional Sleep, Hibernate, and Shut down choices after successful, non-canceled copies when no other copy is active.
- Confirmed relative-file-path and common-relative-folder exclusions from failed results.
- New Window launcher/menu action and multiple independent main views in one process with application-wide profile operation claims.
- Profile Editor tabs for General, Filters, Comparison, and Recovery & Safety, with fixed Cancel/Save controls.
- Expanded Multi Edit fields for cache, volume labels, size limits, and fractional-day age limits.

### Changed

- Renamed the project to OFS, using package `ofs`, application id `com.ofs.LocalSync`, and configuration directory `~/.config/open-flexible-sync` while retaining legacy fallbacks.
- Normalized legacy exclusion separators and wildcard-folder matching while retaining deliberately nonmatching legacy lone-star rules.
- Destination caches persist without time-based expiry across restarts and backup rounds; OFS writes and changed roots/filter identities still invalidate them.
- Large comparisons update in interruptible 250-row batches with visible activity rather than blocking the interface.
- Scan and copy progress use bounded path panels and fixed-width counters for stable layout and readable full-path tooltips.

### Fixed

- Result status wording consistently determines overview icons and colors.
- Closing a copy-results window also closes the now-obsolete preview.
- Completed-run reloads retain vertical scroll position and expanded groups, clamped if the catalog becomes shorter.
- Comparison refreshes retain per-folder expansion/collapse state without changing the plan.
- Closing a main overview does not cancel its active scan, strand its preview/results, or retain its profile claim after completion.

## [0.2.0] - 2026-11-10

### Added

- Don't Copy mode for global exclusions and import of the original 5.71 global masks.
- Dated run results to make the most recent outcome clearer.

### Changed

- Renamed the application and package to OFS and introduced the full-name configuration directory.
- Updated Linux AppImage and tar release downloads for the OFS naming.

## [0.1.0] - 2026-11-09

### Added

- Initial GTK 4 release for local and mounted-folder profiles imported from 5.71 configuration.
- Organized profile overview, dry-run comparison, and synchronization preview.
- Linux AppImage and tar downloads, public project documentation, and CI test/release workflows.

[Unreleased]: https://github.com/jitrc/open-flexible-sync/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.8...v0.5.0
[0.4.8]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.7...v0.4.8
[0.4.7]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jitrc/open-flexible-sync/releases/tag/v0.1.0
