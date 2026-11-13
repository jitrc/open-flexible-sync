# Changelog

All notable changes to OFS are documented here. Entries are grouped by public release and consolidated from commit history; see the comparison links for individual commits.

## [Unreleased]

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

[Unreleased]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.4...HEAD
[0.4.4]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/jitrc/open-flexible-sync/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jitrc/open-flexible-sync/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jitrc/open-flexible-sync/releases/tag/v0.1.0
