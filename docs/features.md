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
- Group heading checkboxes are tri-state. Parents select or clear visible enabled descendants, including nested subgroups; filtered-out members retain their selection, and Disabled profiles are never selected. Reloads after completed run results preserve the current vertical scroll position and expanded groups; superseded restores are ignored and a shorter profile catalog clamps the stale offset to its valid range.
- Search can match custom group/subgroup names and opens each required ancestor. **Expand all** and **Collapse all** also apply to subgroups.
- Profile rows have subtle separators and alternate theme-derived surfaces, including during Light/Dark switching. Row checks immediately refresh the parent group's partial-selection state.
- With at least 12 profiles and three real groups, ordinary ungrouped profiles are placed in a collapsed **(ungrouped)** section at the top. It supports the same visible-enabled tri-state selection and expands with search/Expand all. Small catalogs keep plain ungrouped rows, and no profile receives a virtual group setting.
- Filter by status, never-run state, clean/stopped/error result, current selection, disabled state, minimum days since the last OFS result, or currently available paths. Filtering does not silently change the checked set.
- A persistent **Disabled** setting blocks GUI, CLI, and batch execution without deleting the profile, its paths, history, exclusions, or last results.

## Profile management and migration

- **New profile** is the first header action.
- **Profiles → New window**, or the desktop launcher's **New Window** action, opens an independent main view in the same OFS process. Selections, filters, scrolling, and dialogs are per window. Shared per-profile operation locks reject duplicate scans, whole-folder copies, and comparison-plan copies across windows; different profiles can proceed independently. Existing serial/chunked behavior within each copy is unchanged. Closing an overview does not cancel its active scan or strand the resulting preview: the operation retains OFS until its result window is attached and its profile claim is released.
- Profile Editor groups existing controls into **General**, **Filters**, **Comparison**, and **Recovery & Safety** tabs. Only the current tab's content scrolls. **Cancel**, **Save**, and (for existing profiles) **Restore Older…** remain in a fixed footer outside the tab. Cancel saves nothing; saving from any tab preserves noncurrent-tab settings, imported flags, notes, and path/history behavior.
- **Multi Edit** previews and confirms one field across checked profiles. Its visible **Search settings** field filters the setting list; `cache` exposes persistent destination metadata Off/On. Keyboard search remains available too. It includes cache Off/On, sync/exclusion modes, Older/deletion safeguards, symlink and metadata controls, timestamps, volume labels, size limits, and fractional-day age limits. Other fields remain unchanged. Masks can be added without duplicating existing rules or can replace the entire mask list.
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
- Include and exclusion editors support typed add/edit/remove rules and relative-path matching tests before saving. Rule changes retain the rule-list and surrounding page scroll positions after GTK layout, with newer edits superseding pending restores and short lists clamped to the valid range.
- **Ignore Totally** hides matches from copying and mirror deletion.
- **Don't Copy** prevents transfer, while Exact Mirror can still remove an extra destination copy after the normal safety confirmation.
- Global exclusions apply to every profile. The default behavior remains Ignore Totally; Settings can instead select Don't Copy.
- File and folder **Add exclusion…** dialogs let the user choose profile/global scope, a full relative path or name at any depth, and edit the rule before saving. Global additions preserve existing rules and mode; current live previews recheck displayed, queued, future, and final rows.
- Leading `*/folder` masks match both a root-level folder and nested copies: for example `*/cache` stops discovery at both `cache` and `folder/cache`, including their descendants.
- Per-profile minimum/maximum size and minimum/maximum age limits leave out-of-range files under **Skipped**, without copying or deleting them.

## Global activity history

- The overview **History** button lists scans, comparison copies, and explicit new-folder copies across profiles, newest first. Compact records persist across restarts in `run-history.jsonl`; GUI and CLI use the same store.
- Read-only backfill includes existing OFS copy logs without modifying or duplicating them. Search and Refresh retain context. View Log opens only an existing selected log; there is no replay or history deletion. Scan summaries do not create full copy logs or change last-copy results/profile file-history databases.

## Scanning and comparison

- The default comparison uses name, size, and modification time without reading file contents.
- Timestamp options cover approximately two-second filesystem rounding, exact-hour differences, seconds truncation, and ignoring timestamps entirely. Optional rules cover larger-is-newer, same-time/different-size decisions, and always-copy behavior.
- Cross-filesystem comparison uses filesystem-reported timestamps; OFS does not guess a remote server's clock offset.
- Optional binary comparison directly compares candidate file bytes. This is not a stored SHA checksum database or automatic post-copy verification.
- Optional per-profile SQLite history first records the trees, then helps identify later renames, moves, and deletions. Changed-on-both-sides and undecided rows remain explicit decisions rather than automatic overwrites.
- Exact Mirror can pair a source addition and destination deletion as a move when their filename, kind, link target, size, and timestamp policy match, even without the file database. Common names and sizes in an archive do not defeat this: when several candidates match, the one that shares the deepest chain of trailing folders wins, so renaming a folder still produces moves instead of a whole-tree copy plus delete. Different filenames, different types, and true ties (equally deep or no shared folder) remain separate copy and delete operations. Candidates are indexed by filename and metadata instead of comparing every addition with every deletion.
- Scan progress separates profile and reading side from the path. A fixed-height, theme-aware path panel uses bounded middle truncation instead of wrap-driven resizing. Full path text remains selectable/copyable and available in the tooltip. Statistics, progress, and controls have consistent spacing.
- Scan progress includes discovered/filtered/added counts, elapsed time, and entries per second.
- The first completed scan has no ETA. Later scans show an approximate history-based `~` ETA, refined using current discovery pace and expected remaining entries. Paused time is excluded, and skipped or canceled scans do not replace a previous estimate.
- Each profile's live preview has independent **Pause**, **Skip Rest**, and **Cancel** controls. It fills a pending metadata tree in bounded batches and expands new folders in separate small idle batches, so very large scans keep responding. Live Ignore/Exclude applies across the remaining walk, queued rows, and final plan. Start and power choices stay disabled before a complete comparison. Scan controls collapse at completion while remaining available for expansion. Temporary exclusions do not overwrite persistent cache/history with incomplete walks. Closing the main overview still preserves detached results and their operation safeguards. Unrelated parallel scans continue.
- **Skip Rest** preserves discovered entries in a clearly marked **Partial scan (review only)** preview. Add permanent or session exclusions there, then choose **Rescan**. Copying/deleting and power actions are disabled, no history/cache/ETA estimate is replaced, and CLI `--apply` also refuses partial results. **Cancel** leaves no applicable preview: the page stays with the discovered rows marked review-only, states that nothing can be copied from an incomplete scan, and offers **Rescan**. Neither action writes a copy log.

- Normal comparison copying shares the preview page and an independently collapsible byte/progress panel. Completed rows are labeled and use subdued styling without disappearing or changing the plan. Closing active work requests cancellation and retains the page until its safe boundary; canceled partial copies and failed entries are not falsely marked successful.

## Destination metadata cache

- Enable **Cache destination metadata (until OFS changes it)** independently per profile. Existing/imported profiles default to uncached discovery. Multi Edit offers explicit Off/On for checked profiles.
- Live discovery clearly distinguishes **cache off**, **fresh discovery**, **uncached override**, and **cache reused**, independently for each possible destination side. Actual reuse—not merely enabling cache or failing to save it—is reported as cached.
- Copy, move, delete, and explicit new-folder transfers invalidate destination metadata at the first mutating operation, before targets change, rather than waiting for a whole batch. Failure to invalidate stops with an error. Pre-work cancellation, ASK/no-op rows, and rejected guarded plans do not falsely claim a write.
- A completed destination walk is saved as an atomic JSON manifest under the OFS `cache/` directory. The manifest stores paths, timestamps, size, kind, link target, permissions, and UID/GID; it never stores file contents.
- The following fresh destination scan can reuse that metadata and is labeled as cached. The other/source tree is always read fresh. In both-direction mode the two possible destinations have independently validated metadata snapshots.
- The cache has no time-based expiry and survives restarts for later backup rounds. Its root path and filter settings must match; folder/mask changes or OFS copy/move/delete operations invalidate it. Canceled/skipped scans never become a cached manifest. An uncached refresh creates a fresh completed manifest.
- **Cached metadata can miss changes made by other programs while the cache is valid.** After external changes, choose **Run uncached** in the header, or use `scan NAME --uncached` / `run NAME --uncached [--apply]`. This is a one-run override and does not turn off the saved setting.
- A cache is not the file-history SQLite database, stored hashes, content-addressed storage, or post-copy verification. Metadata-only comparison cannot prove equal contents; optional binary comparison still performs its direct byte comparison on candidates.

## Preview and one-run actions

- The comparison is a folder tree with file sizes, `L ──▶ R` / `L ◀── R` direction diagrams, folder action counts, and per-side copy/move/delete byte totals. **Expand all / Collapse all** affect only the tree presentation, not filters or the plan. Individual folder expansion/collapse state is retained by relative path across exclusion/filter refreshes, and the global choice is retained for newly restored folders. A deliberate collapse also wins over folders discovered underneath it later. Large comparisons (at least 1,000 rows) update GTK in cancelable 250-row batches and expand new folders in separate small idle batches, invalidating stale batches after another edit, with a visible updating indicator instead of blocking the event loop through a whole-tree rebuild.
- State, existence, reason, move, deletion, and adjust-side filters combine. The single-choice **Reason** selector shows rows by why they are planned: Time (newer/older), Size (same time, different size), Bigger/Smaller source compared with the other side, Binary (contents read and different), and Only source/Only destination. Parent folders stay visible while any matching file under them is shown. Unchanged and skipped rows can be shown separately.
- Unchanged rows state why the scan left them alone, for example `unchanged: same size, timestamps ignored` or `unchanged: same size and time`. A short line above the tree names any non-default timestamp rule, including that ignoring timestamps decides from size alone and does not read contents. This text is display-only.
- **Filters change visibility, not what Start applies.** Start runs the whole remaining reviewed plan, including hidden rows.
- File and folder context menus can open paths, launch the configured external comparison command (Meld by default), configure permanent exclusions, or ignore an item only for this run.
- One-run actions cover an individual file, an extension, or a folder subtree without changing saved profile rules.
- The scan preview opens maximized as a normal top-level window, so minimize, maximize/restore, and close work. It has two-column Left/Right totals, a bounded one-line current path with statistics below, and a Folders toolbar. Only filters scroll on a restored smaller window; counts, power options, Start, and Close stay pinned.
- A display-only minimum-size filter targets files, folder aggregates, or both, across the whole preview or under a chosen folder. Name and ascending/descending size sorting reorder siblings without flattening the hierarchy. Neither one removes planned operations. Large result batched rendering remains active. for a known-new subtree. Check exactly one enabled one-way profile and choose a source subfolder, or right-click a discovered folder. Confirmation shows the exact source and matching destination. It does not compare those trees or use a partial scanned plan; it refuses a destination that already exists. The tree streams through the existing chunked copier into a temporary sibling, honoring masks, include/size/age rules, symlinks, ownership options, Pause, and Cancel. Empty folders and directory metadata are retained. Final installation uses Linux `renameat2(..., RENAME_NOREPLACE)` so a concurrently created destination is not overwritten. Failure/cancel removes incomplete output. Unless/until a normal rescan confirms both trees, counts and generated logs do not prove regular synchronization has run.
- Folder context actions can add known in-scope Left- or Right-side files to the deletion plan after a side/path/count/byte confirmation. Unchanged eligible files are included; profile-skipped and unrelated files stay untouched. Nothing is deleted immediately or recursively; Start still requires review and all normal deletion safeguards and Older recovery apply.
- Override a file or folder's direction, leave a file unchanged, or revert it to the scanned action. Pointing a folder at the side that holds a delete's file turns that delete into a copy from that side, so reversing the direction brings destination-only files back instead of removing the other copy.
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

