# OFS feature reference

Detailed behavior for the current source. For a shorter overview, see the [README](../README.md).

- [Window guide](usage.md) — actions and everyday workflows
- [Specification](spec.md) — requirements, compatibility decisions, and future scope

## Profile overview

- Imported/name-derived profiles with a shared first-word prefix form collapsible groups; singleton prefixes remain ordinary ungrouped rows. An optional **Group / subgroups** field in Edit allows arbitrary custom groups even for single profiles.
- Use a path such as `Work / Photos / Archive` for nested groups. Parent counts include subgroups, and each parent has its own tri-state selection checkbox. Grouping is organizational only: changing it does not modify sync paths or invalidate the file database. Clearing the custom field restores name-based grouping.
- Groups start collapsed. **Expand all**, **Collapse all**, and search help navigate the profile list; search opens matching groups.
- Each profile row shows its name, Left/Right paths, last OFS run, and color-coded result, with **Run**, **Edit**, **Duplicate**, and **Delete** actions.
- Header actions use standard symbolic icons with accessible names and tooltips. Play starts checked scans, refresh forces uncached scans, the gear opens Settings, and the sun/moon toggle shows and changes the appearance.
- Run an individual profile or scan only the enabled checked profiles. Different profiles scan in parallel. A profile already scanning shows **Scanning…**, its row button becomes **Running** and is disabled, and duplicate requests are ignored. A checked batch retains one combined summary when its parallel scans finish.
- **Run uncached** is a visible header action for checked profiles. It performs fresh discovery without changing their saved cache preferences.
- Group heading checkboxes are tri-state. Parents select or clear visible enabled descendants, including nested subgroups; filtered-out members retain their selection, and Disabled profiles are never selected.
- Search can match custom group/subgroup names and opens each required ancestor. **Expand all** and **Collapse all** also apply to subgroups.
- Profile rows have subtle separators and alternate theme-derived surfaces, including during Light/Dark switching. Row checks immediately refresh the parent group's partial-selection state.
- With at least 12 profiles and three real groups, ordinary ungrouped profiles are placed in a collapsed **(ungrouped)** section at the top. It supports the same visible-enabled tri-state selection and expands with search/Expand all. Small catalogs keep plain ungrouped rows, and no profile receives a virtual group setting.
- Filter by status, never-run state, clean/stopped/error result, current selection, disabled state, minimum days since the last OFS result, or currently available paths. Filtering does not silently change the checked set.
- A persistent **Disabled** setting blocks GUI, CLI, and batch execution without deleting the profile, its paths, history, exclusions, or last results.

## Profile management and migration

- **New profile** is the first header action.
- The expanded **Profiles** menu organizes original 5.71 import, OFS profile import/export, Multi Edit, path replacement, and checked-profile renaming.
- **Multi Edit** previews and confirms one field across checked profiles. Other fields remain unchanged. Masks can be added without duplicating existing rules or can replace the entire mask list.
- **Replace paths in checked profiles…** handles changed mount points and moved folders:
  - Replace matching text in both paths, Left only, or Right only.
  - Use ordinary case-sensitive text replacement or complete-folder-prefix matching.
  - A prefix such as `/mnt/old` does not also match `/mnt/old-archive`.
  - Review every affected side's complete old/new path before confirming.
  - Only profile configuration changes; folders and volumes are not modified on disk.
  - Stale file databases are removed for changed profiles. Volume labels remain separate settings.
- **Rename checked profiles…** supports substring or name-prefix replacement, a full old/new preview, and confirmation. Duplicate, existing, empty, padded, or `=`-containing names are rejected before anything is written. Other profile settings survive, old-name databases are removed, and renamed profiles remain checked.
- Export selected profiles or entire checked groups to a validated, versioned JSON bundle. Import offers explicit **Skip**, **Rename**, or **Replace** handling for duplicate names. Replacing a profile with changed paths removes its stale file database.
- Original **Import 5.71** reads `~/.SuperFlexibleSynchronizer/SuperFlexibleSynchronizer.cfg` without changing it. OFS imports names, paths, masks, direction, mode, and supported comparison fields; existing OFS fields are preserved when merging legacy settings. Global masks are unioned rather than clobbering local settings.
- Legacy semicolon/NBSP-separated masks, rooted paths, folder masks, and `**/` patterns are handled. A legacy lone `*` exclusion remains intentionally nonmatching so importing it cannot hide an entire profile.

## Include rules and exclusions

- Include filters use `(all)` for no restriction. Replacing `(all)` with masks limits both copying and mirror deletion to matching files.
- Include and exclusion editors support typed add/edit/remove rules and relative-path matching tests before saving.
- **Ignore Totally** hides matches from copying and mirror deletion.
- **Don't Copy** prevents transfer, while Exact Mirror can still remove an extra destination copy after the normal safety confirmation.
- Global exclusions apply to every profile. The default behavior remains Ignore Totally; Settings can instead select Don't Copy.
- Per-profile minimum/maximum size and minimum/maximum age limits leave out-of-range files under **Skipped**, without copying or deleting them.

## Scanning and comparison

- The default comparison uses name, size, and modification time without reading file contents.
- Timestamp options cover approximately two-second filesystem rounding, exact-hour differences, seconds truncation, and ignoring timestamps entirely. Optional rules cover larger-is-newer, same-time/different-size decisions, and always-copy behavior.
- Cross-filesystem comparison uses filesystem-reported timestamps; OFS does not guess a remote server's clock offset.
- Optional binary comparison directly compares candidate file bytes. This is not a stored SHA checksum database or automatic post-copy verification.
- Optional per-profile SQLite history first records the trees, then helps identify later renames, moves, and deletions. Changed-on-both-sides and undecided rows remain explicit decisions rather than automatic overwrites.
- Exact Mirror can pair a unique source addition and destination deletion as a move, even without the file database. Ambiguous or different-type pairs remain separate operations.
- Scan progress separates profile and reading side from the path. A fixed-height, theme-aware path panel uses bounded middle truncation instead of wrap-driven resizing. Full path text remains selectable/copyable and available in the tooltip. Statistics, progress, and controls have consistent spacing.
- Scan progress includes discovered/filtered/added counts, elapsed time, and entries per second.
- The first completed scan has no ETA. Later scans show an approximate history-based `~` ETA, refined using current discovery pace and expected remaining entries. Paused time is excluded, and skipped or canceled scans do not replace a previous estimate.
- Each profile's scan window has independent **Pause**, **Skip Rest**, and **Cancel** controls. They affect that profile only; unrelated parallel scans continue. Neither skipping nor canceling opens a partial comparison or writes a copy log.

## Destination metadata cache

- Enable **Cache destination metadata (up to 5 minutes)** independently per profile. Existing/imported profiles default to uncached discovery.
- A completed destination walk is saved as an atomic JSON manifest under the OFS `cache/` directory. The manifest stores paths, timestamps, size, kind, link target, permissions, and UID/GID; it never stores file contents.
- The following fresh destination scan can reuse that metadata and is labeled as cached. The other/source tree is always read fresh. In both-direction mode the two possible destinations have independently validated metadata snapshots.
- The cache expires after five minutes. Its root path and filter settings must match; folder/mask changes or OFS copy/move/delete operations invalidate it. Canceled/skipped scans never become a cached manifest. An uncached refresh creates a fresh completed manifest.
- **Cached metadata can miss changes made by other programs while the cache is valid.** After external changes, choose **Run uncached** in the header, or use `scan NAME --uncached` / `run NAME --uncached [--apply]`. This is a one-run override and does not turn off the saved setting.
- A cache is not the file-history SQLite database, stored hashes, content-addressed storage, or post-copy verification. Metadata-only comparison cannot prove equal contents; optional binary comparison still performs its direct byte comparison on candidates.

## Preview and one-run actions

- The comparison is a folder tree with file sizes, `L ──▶ R` / `L ◀── R` direction diagrams, folder action counts, and per-side copy/move/delete byte totals.
- State, existence, move, deletion, and adjust-side filters combine. Unchanged and skipped rows can be shown separately.
- **Filters change visibility, not what Start applies.** Start runs the whole remaining reviewed plan, including hidden rows.
- File and folder context menus can open paths, launch the configured external comparison command (Meld by default), add permanent exclusions, type a mask, or ignore an item only for this run.
- One-run actions cover an individual file, an extension, or a folder subtree without changing saved profile rules.
- Override a file or folder's direction, leave a file unchanged, or revert it to the scanned action.
- `OFS_COMPARE` can override the saved comparison command for one launch. Deletion, size, and direction totals update when rows leave the plan.

## Copying and deletion safety

- Directions are **Left to right (L → R)**, **Right to left (R → L)**, or **Both directions**.
- **Copy only (no deletions)** copies new and changed files without deleting destination extras. **Exact Mirror** supports copy, move, and reviewed deletion operations.
- Copying is serial, chunked, and atomic: a unique temporary sibling is completed and metadata-preserved before `os.replace` commits it. No interrupted partial file is exposed under the final filename.
- Pause and Cancel respond between chunks, including inside large files. Cancellation removes the incomplete temporary file and leaves the previous destination intact.
- If Older archiving is enabled, the previous destination is archived only after the new copy is complete. A final-replacement failure attempts destination and surviving-version rollback.
- Plans containing deletions require an explicit safety dialog:
  - **Confirm Deletions** executes the reviewed plan.
  - **Continue Without Deletions** performs copies and moves only.
  - **Do Not Continue** changes nothing.
  - After 120 seconds, the dialog defaults to continuing without deletions. Enter also selects the no-deletion action.
- Optional per-profile limits can block deletion above a percentage of the complete comparison, deletion of every scanned file on one side of a folder, or all file deletions. The GUI and CLI apply the same safeguards before any operation; blocked dialog plans can still continue without deletions.
- Ordinary empty-folder cleanup after moves remains available when file deletion is disabled.

## Versions and recovery

- **Keep replaced files under Older** and **Recoverable deletions** are separate opt-in settings. Existing/imported profiles retain permanent deletion by default unless recovery is enabled.
- Both use a configurable retained-version count. The current archive is `Older/<relative-path>`; older versions use `.~1~`, `.~2~`, and so on, with the oldest pruned beyond the configured cap.
- **Restore Older…** lists entries from either side with relative path, version, size, and timestamp. It works on the one checked profile and is also available in Edit, including for Disabled profiles.
- Restore to a chosen folder outside both synchronized roots. Restoration recreates relative paths atomically, never silently overwrites an existing destination file, and leaves both the archive and active sync trees unchanged.

## Symlinks and metadata

Each profile has an explicit symlink policy:

- **Follow targets** is the compatibility default for existing profiles. File/directory targets are scanned with cycle protection; final copying must respect the profile-root boundary.
- **Preserve links** atomically copies the link object, including relative/absolute target text and dangling links, rather than following it.
- **Skip links** leaves links out of copying and mirror-delete planning.

Dangling, unsupported, or cyclic targets remain unresolved decisions instead of disappearing from one side and becoming an unintended mirror deletion. Link-target identity participates in comparison and move detection. Preserved-link/real-directory conflicts are refused by the apply preflight before any changes.

Normal file copies preserve file mode and timestamps. Optional permission-bit and numeric UID/GID comparisons are separate from timestamp comparison. Optional owner/group preservation applies to temporary files and links before replacement, and during Restore Older; inability to preserve requested ownership is reported safely rather than silently ignored. Directory ownership, ACLs, and extended attributes are outside these switches.

## Appearance, progress, results, and logs

- A **Dark** quick toggle in the header switches Light/Dark immediately, synchronizes with Settings, and persists the selection. No preceding Settings save is needed for the quick toggle.
- **Settings → Appearance** offers **System** (default), **Light**, and **Dark**. System follows the desktop color scheme where available and restores the desktop's original GTK theme. Explicit Light/Dark overrides also select the appropriate named `-dark`/non-dark theme variant when the desktop uses named variants, rather than changing only a preference flag. These choices affect OFS, not the host desktop settings.
- Copy progress includes a byte-weighted total bar, a current-file bar, operation counts, copied/deleted/moved/error totals, elapsed time, active-run speed, and ETA.
- Move/delete-only plans use operation-count progress. Paused time is excluded from active-run speed and copy ETA calculations.
- An optional shared copy speed limit is configured in MiB/s under Settings; zero means unlimited. It applies to GUI runs and `run --apply`.
- Results distinguish completed, stopped, and error outcomes, including per-side file/byte totals, folders created/removed, Older versions pruned, and archived deletions.
- One text log is created per confirmed copying run. Scans, previews, canceled scans, and Do Not Continue do not create a copy log. Logs contain the profile/paths, safety choice, planned actions and sizes, and totals.
- Results provide **View Log…** and, after errors, **Retry Failed…**. Retry requires confirmation and performs a fresh scan and preview rather than replaying stale operations. Normal deletion safeguards still apply.

## Paths and volume protection

Local and OS-mounted folders work on either side, including SMB/CIFS, NFS, SSHFS, rclone, NAS, and other mounted storage. OFS operates on those ordinary filesystem paths without owning a network protocol or account credentials.

Optional Left/Right volume labels identify the required disk. A missing folder or unexpected volume fails that profile rather than scanning the wrong disk, and a checked batch can continue with the next profile.

