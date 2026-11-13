# OFS

OFS is a 64-bit desktop app for grouped local sync jobs. The requirements come from running many local folder jobs, from what FreeFileSync and Syncovery do not cover, and from profiles that can be imported from Super Flexible File Synchronizer 5.71.

OFS does not copy that program's binary, artwork, or profile-file format. Import reads `~/.SuperFlexibleSynchronizer/SuperFlexibleSynchronizer.cfg` and leaves the file unchanged.

## Problem

Super Flexible File Synchronizer 5.71 is a 32-bit GTK2 program. On a current Linux desktop it needs a 32-bit library stack. Fractional display scaling can paint a path on top of the profile name. It can still run the jobs.

FreeFileSync is acceptable for a handful of jobs. It is a poor fit for a large set of local jobs:

- Filters are coarser. This app distinguishes masks that are not copied from masks that are ignored entirely.
- Timestamp comparison cannot ignore small differences, one-hour shifts, seconds, or the timestamp itself.
- There is no overview that groups a large set of jobs by name and runs a checked subset.
- Scans feel slower. The check modes are narrower.
- The preview is missing the right-click actions used on almost every run: open the file or folder, add an exclusion to the profile, and ignore an item only for this run.

Syncovery, from the same author as Super Flexible, is a service with a browser UI at `localhost:8999` on Linux. The Windows and Mac builds still have a native window. The Linux web GUI is thinner, and it is not the desktop app this spec describes.

## User and current workload

One person, many disks and mounted volumes, and a large set of profiles in a proprietary `SuperFlexibleSynchronizer.cfg`. The ini section of that file holds global preferences.

Profiles are grouped by the first word of the name. A few groups hold most of the jobs, and the rest are small.

The config stores, per profile, a left path, a right path, an include mask (usually `(all)`), and exclusion masks. Typical exclusions are `System Volume Information`, `$RECYCLE.BIN`, `@eaDir`, `@tmp`, and `.Trash-1000`. Last-run text records copied, moved, folders moved, and deleted. Versioning uses a folder named `Older`. Partial-update settings can exist globally: a minimum size, and extensions that are always copied whole. SMTP fields may be present and empty. The jobs this spec covers store no `ftp://`, `sftp://`, or `s3://` URLs.

On a typical 5.71 install:

- Logs: `~/.SuperFlexibleSynchronizer/Logs`
- Database: `~/.SuperFlexibleSynchronizer/Database`

## Product goal

A native 64-bit desktop app for this workload: pick a group, check the jobs, scan by size and time, then in the preview open a path, permanently ignore something, or skip it once.

Internet protocols, ZIP packages, and the full SmartTracking database exist in Super Flexible 5.71. They are recorded below so they are not forgotten. They are not required for the first version that can run local folder jobs.

## Where the app stands

The app is this repository. It needs Python 3.11 or newer, GTK 4, and PyGObject. Profiles are JSON under `~/.config/open-flexible-sync` (`OFS_HOME` overrides that directory). How to run it is in `README.md`. Import reads the old `.cfg` and does not modify it.

In the tree now:

- Imported/name-derived grouping uses the first word only when several profiles share that prefix; one-off names stay ungrouped. Optional profile `group_override` accepts arbitrary slash-separated nested groups, including custom single-profile groups. Parent totals include descendants, parent tri-state selection affects visible enabled descendants, and search/Expand all/Collapse all recurse by canonical group path. Group changes preserve paths, history, and file databases; renaming profiles changes automatic grouping only when no custom override exists. A catalog with at least 12 profiles and three real groups places ordinary ungrouped rows in a collapsed `(ungrouped)` section at the top; small catalogs retain plain rows. Its checkbox selects visible enabled ungrouped profiles, search and Expand all can open it, and the virtual section never writes a custom group. Groups start collapsed, with a distinct heading and a profile count. Expand all, Collapse all, search, status filters, and an optional available-paths filter. Search opens the groups that match. Filtering does not change the checked set. Each group heading has a tri-state checkbox that selects or clears visible enabled members only; disabled and filtered-out members are never implicitly included (an already checked hidden member is left unchanged). Fixed columns show profile, last run, result, and actions. Run checked scans the checked profiles. Each row has its own Run, plus Edit, Duplicate, and Delete.
- Include and exclusion masks, Don't Copy versus Ignore Totally, Standard versus Exact Mirror, direction, the timestamp rules that are on the editor, optional binary compare, and `Older` for replaced files. A lone `*` stored in an imported exclusion list matches no path.
- Recoverable deletions, opt-in and sharing the Older retention count. Older files from either side can be listed with path, version, size, and timestamp, then copied atomically to a separate chosen restore folder. Restore never overwrites an existing file or writes into either live sync root.
- Symlink mode per profile: Follow for compatibility with existing jobs, Preserve for atomic link objects (including dangling links), or Skip. Link-target identity is used in comparison, move pairing, Older, Restore, and the file database. Link/directory conflicts are refused before apply.
- Optional file permission and owner/group comparison. Requested owner/group preservation is applied to temporary objects before replacement and during restore; failures leave the old destination intact.
- Optional per-profile file database: first run records only; a later run can turn a rename into a move and a real deletion into a delete, and it asks when both sides changed. A database stored for different folders is ignored. Duplicate leaves the database behind. Delete removes the profile and the database, and leaves the folders on disk.
- Global exclusions in Settings, ignored totally on every profile. The old defaults apply until that list is saved. Per-profile minimum and maximum size and age. Files outside those limits are skipped: listed under Skipped, not copied, and not deleted. `Older` keeps one replaced file, or the number set on the profile, and drops the oldest beyond that.
- A scan window while the folders are read: current profile/path, discovered, filtered, and added counts, pause, Skip Rest, and Cancel. Each profile has independent controls; skipping or canceling one does not stop unrelated parallel scans. Skip Rest opens only a review-only partial comparison for exclusion editing and Rescan; Cancel produces no preview. Neither writes a copy log.
- Comparison window: a folder tree with size and an arrow for direction, filters, per-side copy/move/delete totals, file and folder right-click, Start, the three-way deletion confirmation with a two-minute continue-without-deletions timeout, pause and cancel between files, the results window, and one log file per confirmed copy.
- From a file or folder, **Add exclusion…** opens editable profile/global scope and full-relative-path/name matching options. Ignore for this run remains temporary and separate. Files retain comparison, direction, unchanged, and revert actions; folders can also set the review size-filter scope or point every planned file under them. **Delete folder files on Left/Right…** first confirms relative folder, chosen side, count, and bytes, then converts known eligible file rows, including unchanged rows, to Delete actions. Profile-skipped and unrelated rows remain unchanged. Partial, scanning, and running previews are rejected. There is no direct recursive deletion: Start's review, Older recovery, all proportional/folder/never-delete safeguards, and apply/cache guards still apply. Compare uses `meld` unless Settings or `OFS_COMPARE` names another command.
- Optional left and right volume labels. A scan refuses a folder that is not on that disk and continues with the next checked profile. Replace paths asks, then rewrites the checked profiles and removes their file databases.
- A GNOME launcher named OFS starts this app. It is separate from the 32-bit program and does not use that program's DPI lock.
- Global appearance settings are System, Light, and Dark, persisted in Settings `theme`. System is the backward-compatible default. Apply the preference at startup and when saving Settings; use GTK's per-application dark preference and follow the exposed desktop color-scheme setting where available without changing host desktop settings. All new and older themes use normal GTK theming rather than custom light-only hardcoded window backgrounds.

Still open, and still in the sections below: scheduler, unattended runs, partial files, ZIP, internet protocols, and mail. Import reads direction, Standard versus Exact Mirror, and the four timestamp checkboxes. Versioning on or off is not a varying per-profile flag in that `.cfg`, so `Older` stays off. The comparison filters hide rows. Start still runs the whole remaining plan.

## Requirements added after the first draft

These were missing from the first write-up. They come from using the preview and the scan, and they are required.

- Groups are headings. They start collapsed. Expand all and Collapse all act on every group. Search opens each group that matches.
- Run checked scans the checked profiles. A Run control on the profile row scans that profile only.
- While the folders are being read, a window stays up for each active profile. It names the profile, says whether it is reading the left folder or the right folder, and shows the path inside that folder. It keeps combined discovered, filtered, and added counts. The copy, move, and delete totals belong on the comparison window, after the scan. Pause holds that profile's walk. Skip Rest skips that profile; Cancel, or closing the window, stops that profile. Other independent scans continue. Skip Rest preserves known entries in a partial comparison marked review-only: Start is replaced by Rescan, power/apply are blocked, no history/cache/ETA baseline is updated, and no copy log is written. A stopped/canceled scan does not open a partial comparison.
- The comparison is a folder tree. Folders come before the files inside them. Each file row shows the name, the size, a direction arrow, and the reason. Each folder row shows that folder's total size beside the name and again in the size column. Its direction cell counts the arrows under it, for example `L ──▶ R  12`. Its status cell shows the bytes for each of those groups.
- Direction is an arrow diagram. `L ──▶ R` copies onto the right. `L ◀── R` copies onto the left. `L ──✕ R` deletes on the right. `L ✕── R` deletes on the left. `L ──? R` is undecided. `L ─── R` is unchanged. `L ··· R` is skipped. A move uses the same arrow as a copy on that side. The action name (`copy_l2r` and the rest) stays in the log line, not in the direction column.
- Right-click a folder to open either side, configure a profile/global exclusion, or ignore it for this run. The exclusion dialog offers a full relative path or a name at any depth, with an editable rule before save; adding a global rule appends without clobbering existing masks or changing Ignore Totally / Don't Copy mode. The same menu can point every planned file in that folder and below it with `L ──▶ R` or `L ◀── R`. A copy or an undecided file becomes a copy. A move stays a move. A delete stays a delete, on the side the arrow points at. Unchanged and skipped files stay. A file menu uses the same two arrows for that one file.
- A lone `*` in an exclusion list matches no path. Super Flexible 5.71 stores that star between the junk-folder masks. An include list of `(all)` still includes every file. `*.tmp` matches that name in any folder. A mask that names a folder matches that folder and everything under it.
- A log file is written when a confirmed Start begins copying, and when `run --apply` begins copying. Scanning, looking at the preview, canceling a scan, and choosing Do Not Continue do not write a log.
- The desktop entry is named OFS and launches this app on its own. It does not launch the 32-bit program and it does not set that program's DPI lock. Its New Window action invokes `ofs --new-window`; the application handles this command line locally and when forwarded to its single registered bus instance. New main windows run in that same process with independent selections/views. Application-wide, thread-safe per-profile claims cover scanning, comparison copying, and whole-folder copying from claim through completion/cancel/error cleanup. Reject a duplicate without starting a thread or changing files; release on every completion path. Different profiles retain independent parallel scans, and per-plan copy mechanics stay unchanged. Closing a main window does not cancel its active scan. Retain application lifetime through the scan completion callback, attach its preview/results even when the originating main window is no longer visible, and release the application hold/profile claim after that transition.

## Users

The person who keeps the profiles edits exclusions during a preview more often than they edit a profile form. They run many jobs, not a few.

## P0 — required for the first usable version

These are the requirements added after the first draft, restated with the behavior found in the 5.71 UI.

### Profile overview for many jobs

- A profile has a name, a left folder, and a right folder.
- The main window is a checklist. The group is the first word of the profile name. Each group is a heading, collapsed until opened, and visually separate from the profile rows. The heading shows how many profiles are in the group. Expand all and Collapse all open or close every group. A search opens the groups that match.
- Check profiles and run only the checked set. Each profile row has Run, Edit, Duplicate, and Delete. Run on the row scans that profile only. Run checked scans the checked rows.
- Show only scheduled profiles, and schedule only checked profiles, once scheduling exists.
- Show the last result on the row: counts and bytes for copied, moved, folders moved, and deleted.
- Import existing jobs far enough to keep working: name, both paths, include mask, exclude mask. The old `.cfg` is a binary blob; mode flags are not plain text. A new profile store is required after import. Do not depend on the old 32-bit program to edit jobs.

### Profile editor organization

Organize the existing profile controls into General, Filters, Comparison, and Recovery & Safety notebook tabs. Individual tabs may scroll when necessary, but Cancel and Save remain in a fixed footer outside those scroll areas on every tab. Existing profile fields, imported flags, notes, path-change history behavior, and Restore Older stay intact. Cancel closes without saving; Enter can invoke the default Save except where a child editor handles its own activation. Inclusion/exclusion rule changes also preserve the rule list's own scroll adjustment and each scrolled ancestor adjustment, including the notebook page. Restore after layout, clamp to shortened content, and cancel superseded pending restores.

### Filters

Result reloads in the profile overview preserve the user's vertical scroll adjustment, selected/current profile, and existing expanded-group state. Store a profile name, not ephemeral GTK row references; highlight its replacement row without changing geometry. Use list-content coordinates for that row's anchor, not an assumed screen coordinate. Restore after GTK has laid out the rebuilt rows (below the GTK resize priority), discard superseded restores, and clamp only when the new list is shorter. Preserve checked profiles, filters, groups, and plan behavior during normal copy or explicit whole-folder result refreshes. Closing a completed preview/result presents a still-visible owner overview and focuses its selected checkbox after restoration, then scrolls by the minimum amount that makes the whole completed profile row readable; checkbox focus must not leave that row clipped. Never reopen a deliberately closed owner, and never leave preview cancellation unsafe. If the current filters hide the selected profile, keep those filters and restore the available scroll range instead.


- Include masks and exclusion masks for files and folders. Both are editable as typed rule lists with relative-path match tests. Keep `(all)` in includes for no restriction; replacing it makes nonmatching files invisible to copy and mirror deletion. Several masks may be separated by semicolon or newline. A mask may point at a text file that holds more masks.
- A lone `*` in an exclusion list matches no path. The imported lists keep that star. `(all)` on an include list includes every file. `*.tmp` matches that name in any folder. A mask that names a folder matches that folder and everything under it.
- Folder masks are separate from file masks.
- Each exclusion is one of two modes:
  - **Don't Copy.** The file is still visible to the job. Exact Mirror may delete the copy on the other side.
  - **Ignore Totally.** The file is invisible. It is not copied, and it is not deleted on the other side.
- Global exclusion masks apply to every profile. The old defaults include `.DS_Store`, `Thumbs.db`, `Temporary Internet Files`, `*.tmp`, `*.$$$`, `*.~*`.
- General filters: file age, file size, and an option to apply timestamp filters to folders too.
- Option to search hidden folders.
- Subfolder scope per side: all, selected, or none.
- The preview can list items skipped by a filter, separate from items skipped by a mask.
- Minimum size, maximum size, minimum age, and maximum age are per profile. Empty means that limit is off. A file outside the limits is skipped: it stays in the comparison under Skipped, it is not copied, and it is not deleted.

### Comparison and timestamp rules

Default scan compares name, size, and last-modified time. It does not read file contents. That is the fast path and must stay the default.

Per profile, any combination of:

- Ignore small time differences (about 2 seconds, FAT versus NTFS).
- Ignore a difference of exactly one hour (daylight saving or timezone).
- Ignore seconds.
- Ignore the timestamp altogether.
- If the size differs, treat the larger file as the newer one.
- A rule for “same timestamp, different size” (copy, skip, or ask).
- Optional binary comparison while building the file list. The old program labels this slow. It must be off unless the profile asks for it. OFS compares candidate file bytes directly without trusting cached size/time metadata. Equal bytes become an equal file without copying. This is whole-file binary comparison, not a stored SHA checksum, content-addressed dedupe, or automatic post-copy verification; those are possible later additions.
- Cross-filesystem time comparison relies on filesystem-reported modification timestamps. The small-difference and exact-hour rules cover normal NTFS/FAT rounding and DST/timezone shifts. OFS does not auto-detect a remote server's clock offset or invent a timestamp when the filesystem does not provide one.
- Optional compare of file permissions and numeric owner/group, stored separately in FileStat metadata. Equal-time metadata-only changes copy from the source in one-way mode and ask rather than inventing a winner in both-way mode. Binary comparison must not discard metadata. Optional atomic owner/group preservation occurs on temporary files or links before replacement; inability to preserve requested ownership is an item error, not silently successful. Directory ownership, ACLs, and extended attributes are not covered.
- Optional “always copy even when the timestamp is unchanged”.

A content hash or remote MD5 is not the default. Byte-for-byte compare is a separate, explicit mode.

### Preview right-click

The preview lists files on the left, the right, or both. Right-click is part of the normal run, not a rare extra.

- **Open file** on the left, on the right, or both.
- **Open folder** on the left, on the right, or both. A folder row opens that folder on the left and on the right as two commands.
- **Apply New Exclusion Mask…** asks for the mask and stores it on that profile.
- **Add exclusion…** on a file or folder opens the shared editable scope/match dialog. Profile or confirmed global rules drop matching current/live/final rows; global mode and existing other-scope masks remain unchanged.
- **Remove From List** drops the selected item for this run only. The profile on disk does not change. On a folder row the same command is **Ignore for this run**: the folder and everything under it leave this comparison, and the profile is not saved.
- **Remove From List All Files With This Extension…** drops that extension for this run only.
- **Remove This Folder from Profile…** is permanent.
- **Compare Files…** opens the two sides in an external compare program named in settings.
- The direction column is an arrow diagram: `L ──▶ R`, `L ◀── R`, `L ──✕ R`, `L ✕── R`, `L ──? R`, `L ─── R`, and `L ··· R`. A folder row shows how many files sit on each arrow. The reason stays in the status column. The action name stays in the log.
- Per file, override the proposed action with `L ──▶ R` or `L ◀── R`, or leave it alone. Revert a row to the scanned action.
- Per folder, the same two arrows point every planned file in that folder and below it. A copy or an undecided file becomes a copy. A move stays a move. A delete stays a delete, on the side the arrow points at. Unchanged and skipped files stay.

“Add to the profile ignore list” and “temporary ignore” stay separate commands. One is saved. The other only changes the list on screen.

### Comparison list

Preserve user expansion/collapse per relative folder path across filter, exclusion, and in-place folder edits. Global Expand/Collapse provides an explicit default for restored rows and clears prior per-folder overrides. Avoid an all-at-once GTK rebuild of hundreds of thousands of entries: large comparisons update in 250-row batches on the UI idle loop, with a visible progress message. A newer filter/edit invalidates older pending batches. This is presentation-only; Start uses the current planned rows, not rendered visibility. Neither batching nor collapse changes the copy plan. Small known-fixed existing behavior remains synchronous. The scan preview starts maximized. A restored smaller window scrolls only its filter list, keeping shown counts, power selection, Start, and Close fixed below. Left/Right path and summary blocks are two equal columns with ellipsized paths and wrapping totals. The Folders toolbar has normal icon-and-text Expand all / Collapse all controls and Name/size-ascending/size-descending sorting within each parent. Optional minimum-size filtering applies to files, folder totals, or both, over the whole preview or a selected folder subtree. Sizes are exact numeric bytes internally; displayed folder totals follow visible descendants. Size filtering and sorting change presentation only and never remove or reorder planned operations. Existing large-tree batched rendering and folder-expansion preservation apply.


One scanned profile opens the comparison window. Several scanned profiles open a summary first, and each row can open the same window.

The comparison is a folder tree of every file the scan saw, including files that would be left unchanged. Folders are listed before the files inside them. Each file row has a name, a size, a direction arrow, and a reason. Unchanged rows name the comparison reason that kept them out of the plan (for example `unchanged: same size, timestamps ignored`), and a non-default timestamp rule adds one short explanation line above the tree; both are display-only and never alter the plan. Each folder row shows that folder's total size beside the name and in the size column, the arrow counts of the files under it, and the bytes for each arrow group. Filters on the left only hide rows. Start still runs the whole remaining plan, including rows the filter is hiding. Removing a row for this run, or saving an exclusion, takes that file out of the plan and out of the totals.

State, all of which can be combined with the other groups:

- Show All.
- Affected. This is the view the window opens on.
- del./mov. Deletions and moves only.
- R to L. Copies onto the left, moves on the left, and deletions on the left.
- Unaffected.
- Skipped. Files left out by a size or age limit.

Existence: Show All, One Side, or Both Sides.

Moved files: Show, Show Only, or Don't Show.

Adjust Them: On the Left, On the Right, or Either side. This chooses which move rows stay visible. It does not move the rename onto the other folder. A left-to-right job opens on the right, a right-to-left job opens on the left, and a both-ways job opens on Either side.

Files to delete: Show, Show Only, or Don't Show.

A line under the filters counts the visible files and their sizes.

Above the list, each base folder shows how many files would be copied onto that side, moved on that side, and deleted on that side, with sizes. Those three numbers follow the whole remaining plan, not the filtered rows.

### Speed

A scan of a large tree is metadata only unless the profile turned on binary comparison. The UI stays usable with a large profile list loaded. Starting a checked subset does not scan the unchecked jobs.

### Scan progress

Scanning opens an application-owned live preview immediately. Existing scan controls appear in a collapsible panel above its tree; ordinary comparison copying uses the same page and independently collapsible progress panel. An explicit new-folder transfer retains its existing no-comparison progress workflow.

Walk metadata events are queued and drained in 250-row GTK batches, not emitted as one main-loop callback per path. Honor global and manual folder expansion while adding pending rows and keep all result columns readable at the initial width. When a filter/exclusion refresh has a partial replacement model in a batch, defer new insertions and schedule a superseding full refresh; never mix obsolete TreeStore iterators with the new model. Pending entries have no assumed copy direction and cannot be applied. Permanent exclusions and session Ignore actions update the shared current walk, queued rows, final plan, and final listing through a per-main-window/profile RLock. The final adoption filters those current rules again to close the worker-completion race. Never use a session-filtered selective walk to overwrite the persistent destination cache or create a first history baseline; cached maps are session-filtered before their events and final listing. History-derived moves/deletions/conflicts honor temporary rules, including a move's destination path.

At completion, collapse scan details, retain its counts for optional expansion, and replace provisional rows with the real result. Incomplete/error scans cannot Start or apply power actions. Treat direct apply in `_begin()` as partial/incomplete just like the Start wrapper. Closing an active preview requests Cancel and retains the page until the operation boundary; it never lets an unknown pending plan run. A multi-profile batch closes finished, non-copying live windows when its combined summary appears, while retaining each available result for explicit Preview.

Scan stats and completion use the same default priority, avoiding the older idled callbacks starving behind animation/rendering. Late stats cannot revive completed controls. Copy progress and copy completion likewise retain FIFO priority. Keep application claims, preview retention, and cross-window power guards valid when the originating overview is closed using its retained application reference. Release large provisional maps on final adoption and release obsolete preview references on close.

- The window names the profile and reading side in their own heading. A fixed-height, theme-aware path panel shows the path on one bounded, middle-truncated line so changing filename length does not resize the dialog. The full path stays as label/selectable text and in its tooltip for copying or reference. The current path uses a fixed character bound, a single nonwrapping line, and middle ellipsis; short and long paths retain the same allocation and the full text stays selectable and in the tooltip. Discovered/filtered/added counts, elapsed time, speed, and ETA are below the path rather than competing horizontally. Context-menu actions use the normal interface font regardless of tooltip presence. Progress and controls use clear, even spacing. Detached embedded scan/copy windows are hidden as top-level widgets. Normal activation focuses/creates a MainWindow instead of presenting a childless operation window; it must not cancel active work.
- Combined counts stay on screen: folders and files discovered, rules filtered by Ignore Totally, and files added to the scan. Elapsed time and discovered entries/second are shown. Only complete scans save that profile's discovered count and duration in Settings. Repeat scans display a clearly approximate ETA using prior duration, measured pace, and expected remaining entries, excluding pauses. The first scan has no ETA; skipped/canceled scans do not replace an existing estimate.
- Copy, move, and delete totals are shown on the comparison window after the scan.
- Pause holds the walk between entries. Resume continues. Skip Rest, Cancel, and closing the window affect only their own profile's scan.
- The scan windows do not lock the overview. Different profiles start scanning in parallel, including members of one checked batch. Row results show **Scanning…** and the row Run control becomes insensitive **Running** until completion; requests for that same profile do not start duplicates. Independent scans retain independent progress and cancel/skip controls. One checked batch opens one combined summary after all of its scans finish. A stopped scan opens no partial comparison. Skipped scans are review-only comparisons for exclusions and rescanning, never an applicable plan; neither writes logs, and unrelated scans continue.

### Directions and the two modes these jobs need

- Copy left to right, right to left, or both.
- **Standard copying.** Copy new and changed files. Do not delete extras on the destination unless a profile option says so.
- **Exact Mirror.** The destination matches the source, including deletions, subject to Ignore Totally and to the safety checks below. A file that is only on the source and a same-named file that is only on the destination are a move when the filename, kind, link target, size, and selected timestamp rules match and no other file shares that match. Different basenames always remain separate copy and delete decisions. The file database is not required for that pair. Ambiguous same-sized candidates stay a copy and a delete. Index candidates by filename/kind/link target/size instead of testing every addition against every deletion. Standard copying does not turn that pair into a move.

## P1 — required for the jobs to behave as they do now

Found in 5.71. Not called out by name in the later notes, but the stored jobs use them.

### Deletions and safety

- Attended runs ask before deletions. Unattended runs follow the profile and do not stop for each file. The OFS window is attended only.
- Exact Mirror can warn before overwriting a newer file with an older one, before deleting more than a set percentage of a folder, and before deleting every file in a folder.
- Deleted files can be removed permanently, or moved into a specified folder.
- Per-profile safeguards for a maximum deletion percentage, blocking deletion of every scanned file on a side of a folder, and never deleting files. Zero disables the percentage cap. Calculate against the complete remaining listing, not visible preview-filter rows. A blocked plan disables Confirm Deletions and can only continue without deletions or be canceled. The apply layer enforces these policies and requires the complete listing for the percentage/folder checks; GUI and CLI cannot silently bypass them. Ordinary empty-folder cleanup after moves is retained. Other options: remove folders that became empty; create empty folders; remove destination files that do not match the include masks or the filters.

Start, when the remaining plan deletes at least one file, opens Safety Confirmations Required before it copies or deletes anything. The text names the profile and, for each side that would lose files, the folder and `N of M (percent)`. `M` is every file in the comparison list, including unchanged files. The path list shows the first 200 and then how many more there are. A folder is called out when the deletions on that side are at least half of the files listed in that folder, with the deletion count.

Three actions:

- Confirm Deletions runs the plan, including the deletes.
- Continue Without Deletions runs the copies and the moves and leaves every planned delete in place.
- Do Not Continue changes nothing.

If the window is still open after 120 seconds, the run continues without deletions. Closing the window does the same as Do Not Continue. Enter is the continue-without-deletions action, so a keypress does not confirm deletions.

When the plan deletes nothing, Start goes to progress. The comparison list is the confirmation for those copies and moves.

A row marked ask is skipped by the windowed run and reported as left undecided. `run --apply` refuses the whole run while any ask remains.

### Progress

Copying runs off the UI thread. Normal comparison copying shares the live preview and shows current relative path, completed/total operations, byte-weighted copy totals, elapsed time, measured speed, estimated time remaining, a total bar, and a current-file bar in an independently collapsible panel. Completed copy/move/delete rows acquire a result label and subdued rendering while remaining in the plan; canceled partial operations or failed items are not falsely marked successful. This is presentation-only and must not change upcoming operations or bypass filter/collapse semantics. Operations that are moves or deletes use the operation count when there are no copy bytes. Pause and Resume operate between copy chunks, and paused wall time does not depress the measured active-run speed. Cancel can stop an in-progress copy; its temporary file is removed and the prior destination remains intact.

Copying uses unique temporary siblings in the destination directory, preserves metadata, and atomically replaces the destination only after the complete file is written. It does not expose an interrupted partial file under the final filename. The global copy speed limit is in MiB/s, with Unlimited, 1, 10, 100, and Custom choices; zero means unlimited. Changing operation counts, speed, and ETA must use fixed-width monospaced fields so neighboring text does not shift; Paused is appended rather than shifting the other timing fields. Copying stays serial in this iteration. A future bounded pool may parallelize independent small files, with 2–4 workers as a candidate NAS optimization, but must not reorder moves/deletes or bypass safety checks.

Each completed-copy preview offers Do nothing (per-run default), Sleep, Hibernate, and Shut down. Only that run's successful completion can invoke the selected Linux `systemctl` action. Canceled/error/partial results do not invoke it. Another active copy also prevents it and explains why; an unavailable or refused `systemctl` command is reported. This is an explicit per-run completion option, not unattended scheduling.

A failed item is recorded without preventing unrelated later items. Results offer Retry Failed with confirmation, then rescan the current profile and present a fresh plan. They must never replay an old, potentially stale plan. Successful earlier operations are naturally omitted by the rescan. A Retry of a mirror destination follows the same deletion safety confirmation as a new run.

### Explicit new-folder transfer

A separate Profiles → Copy new folder… action and complete-comparison folder context action permit known-new source subfolders when one enabled one-way profile is selected. The exact source/destination need explicit confirmation; the destination must be absent both before starting and at final installation. This is not a partial-plan application. It streams through the existing serial, chunked per-file copier into a temporary sibling, honoring global/profile/session masks, include/size/age limits and symlink/ownership policies. Empty folders and directory timestamps survive. Linux `renameat2(..., RENAME_NOREPLACE)` prevents overwriting a concurrently created destination. Pause/Cancel operate between chunks; errors/cancel clean up incomplete temporary output and any still-empty newly added parents. Unknown total work pulses rather than claiming 0% or 100% completion; per-file and copied-byte progress remain available. Results write a normal report/log and refresh profile state without deleting destination extras. Retry performs a normal full rescan and review rather than replaying the shortcut. Use profile-relative paths for include/exclusion filtering, followed-target metadata in Follow mode, and reject the top-level SKIP link or a destination nested inside the selected source. Rollback handles copied read-only directories using the correct Python 3.11+/current `rmtree` callback and never chmods outside its own temporary tree. Newly generated empty parents are removed deepest-first. Uncommitted transactions report zero committed files/folders, and an approved operation invalidates destination caches even if cleanup fails after rollback counters return to zero. Progress/result windows must be associated with the shared application so the completion power guard can identify active copies. The profile editor's Left/Right entries have Browse… directory selectors; selecting does not persist until explicit Save.

### Versions

The config uses an “Older” tree. The product must be able to keep replaced files as older versions:

- Move the previous file into a folder named `Older`, either as a subfolder of each folder or as one tree that recreates the relative path. OFS uses one tree recreating the relative path.
- Optionally archive files removed by a delete action in the same tree, including rotated versions and retention. The default remains permanent deletion so imported/existing profiles do not change behavior.
- Restore versions from either side to a separate folder outside both synchronized roots. Restoration is atomic, preserves metadata, refuses existing destination files, and never silently arms the live sync trees.
- Cap how many older versions to keep, and prune past that cap.
- Filename versioning is a second, later mechanism (timestamp in the name, `(2)` before the extension). The folder named `Older` is the one OFS keeps.

### Scheduler — future scope

Scheduled regular synchronization is not implemented and is deliberately after the copy/progress work. When added, a profile can be unscheduled or run on a timer, while a Disabled profile never starts. Background scheduling can start at login. A scheduled run must skip and report unavailable paths, distinguish scan-only from unattended copying, and require an explicit deletion policy for Exact Mirror. Log on, log off, and shutdown hooks exist in the old program; they are secondary to the interval timer.

### Logs and results

- After a run, show files copied, moved, renamed, deleted, folders created and removed, and older versions removed.
- Log to one shared file, one file per profile, or one file per run. Keep a capped number of log files.
- A run can be attended (preview, then confirm) or unattended (no preview).

Copying Results is the attended report. The headline says the run finished, finished with errors, or was stopped. Left and Right columns show files copied to that side, files moved on that side, and files deleted on that side, each as a count and a size. Folders created and folders removed are single totals for the run. A new parent folder created for a copy, a move, or an `Older` archive counts as created. An empty folder left behind by a delete or a move is removed and counts as removed. The profile folder itself stays.

View Log opens that run's text file. A confirmed Start that begins copying writes one file in the config directory's `logs` folder. `run --apply` prints the totals and writes the same kind of file once copying starts. A scan, a preview, a canceled scan, and Do Not Continue do not write a log. The text has the profile name, both paths, which safety choice was taken, one line per planned change (the action name, the size, and the path), and the same totals. The file name is `<profile> - YYYY-MM-DD HH.MM.SS.log`. `/`, `\`, and `:` in the profile name are replaced. A cap, a shared log, and unattended runs are still open.

The overview has a standard clock-and-label **History** button leading to a read-only global Run History window. Record concise persistent `run-history.jsonl` entries for GUI/CLI scans, comparison copies, and whole-folder copies, with profile, local timestamp, kind, summary, and an optional log basename within config `logs/`. Serialize writes and reads because independent profiles can finish concurrently. Keep separate from per-profile file-history databases, profile last-copy results, and manifests. Scan records are summaries and never full per-file copy logs. Backfill old OFS copy logs by safe basename/timestamp, exact first-line profile, and bounded outcome-tail reads, deduplicated against structured entries. Never modify imported old logs; skip malformed JSONL rows, traversal references, symlinks, temporary files, and unreadable entries without failing the whole view. Search profile/date/kind/summary, retain query across explicit Refresh, and open only an existing selected log through the established file opener. No run may be replayed or deleted from this view.

### Volume presence

- A side may require a volume label. If that volume is not mounted, or the folder is on a different disk, skip the job with "volume is not mounted" and continue with the next checked profile. Do not scan the wrong disk.
- Import copies each local path as stored. A path may be a directory or a mount point.

### Profile editing

- Create, rename, delete, disable, enable, and duplicate a profile. Disabled is persistent, visible in Overview, and blocks GUI/CLI runs without deleting the profile, its paths, history, or results. A future scheduler must honor it too. Duplicate copies the job and leaves the file database behind. Delete asks, then removes the profile and its database. The folders on disk stay.
- Multi Edit changes one field across the checked profiles. It previews affected names and resulting values, requires confirmation, and leaves every unselected field unchanged. The field selector supports type-ahead search and a visible Search settings entry that filters the choices before saving; searching `cache` explicitly presents destination cache Off/On. Fields include cache_destination Off/On, Left/Right volume labels, min/max size with profile-editor size syntax, and min/max fractional-day age; changing one must preserve imported flags, notes, results, paths, and other settings. Include/exclusion masks offer Add (union without duplicate additions) or Replace. Disabling removes affected names from the run selection; disabled profiles can still be re-enabled through Multi Edit.
- New profile is the first header action. An expanded Profiles menu contains 5.71 import, OFS import/export, Multi Edit, checked-path replacement, checked-name replacement, and all use explicit checked scopes.
- Search and replace a path across the checked profiles, using Both/Left/Right and substring or complete-folder-prefix matching. The preview is read-only and lists each affected side's complete old/new path. Ask before saving and never move folders on disk. Remove the file databases of profiles that change, because those databases still describe the old folders. Preserve volume labels unless they are separately edited.
- Rename checked profiles through previewed name text/prefix replacement. Validate every resulting nonempty, unpadded, `=`-free name against all current profiles and each other before any document is written; reject collisions without changing files. Preserve all non-name fields, remove old-name file databases, and keep the renamed profiles checked. Group names are recalculated from the renamed first words.
- Export checked profiles or groups into a versioned JSON bundle, preserving OFS profile fields but not file databases or logs. Import validates the whole bundle before changes and requires confirmation and an explicit skip, rename, or replace policy for duplicate names. Replacement removes a stale file database if its folders changed. Neither operation touches the original 5.71 cfg.
- Optional password before editing profiles.
- Profile names are case-sensitive and must not contain `=`.

### One-run preview actions already listed in P0

Also on that menu, and in scope once the preview exists: delete now from the left only or the right only, rename now on one side, move this row’s action to “move to folder”.

## P2 — in the old program, not in the first version

Recorded so a later version can add them without a new discovery pass. Nothing here is required to run the current local jobs.

### SmartTracking and conflicts

- A SQLite database per profile remembers files so a later run can tell a move or rename from a delete plus a new file.
- Detect moved files and renamed folders; adjust the path on one side and keep it on the other.
- Detect renamed files from timestamp plus size.
- Conflicts when both sides changed: rename and keep both, copy the newer file, or always prefer one side. If the renamed name already exists, follow a second rule.
- The database can be read-only, rebuilt, or rejected when its paths do not match the profile.
- An optional per-profile destination metadata cache uses a separate atomic JSON manifest, not the history database or file contents. Use is opt-in and persists without time-based expiry across backup rounds and app restarts, includes the raw destination path and filter/mask identity, and is invalidated after OFS writes. Each destination side reports cache off, validating/enabled, reused, uncached override, or a fresh walk with no compatible cache. `ScanResult.cached` means actual reuse, not merely an enabled option or cache-write error. Invalidate the manifest at the first actual copy/move/delete operation, not only after the entire batch. Place invalidation before creating parents/temporary output for an explicit whole-folder transfer. Pre-work cancellation, ASK/no-op handling, and safety rejection do not falsely claim or require invalidation. Failure to invalidate stops the operation with an error and a result. Preserve GUI, CLI, and lower-level caller consistency and keep completion power guards intact. Source scans and bidirectional counterpart validation are not silently omitted. Completed scans refresh it; skipped/canceled scans never create it. Cached status/age and the external-change staleness caveat are explicit. The header's `Run uncached` and CLI `--uncached` perform a fresh refresh without changing the saved preference.
- First run only fills the database. It must not be required for Standard or Exact Mirror. Exact Mirror already shows a unique moved file from the two folders alone. The database adds moves and deletes that can only be seen against the previous run.

### Real-time

- Watch folders and sync on change.
- Safety delay before acting on deletions.
- Ignore certain temporary files while watching.
- On this Linux build the old program reports that network monitoring is not implemented. A new watcher would be inotify/fanotify on local filesystems, not a port of that code.

### Partial files, ZIP, encryption

- Partial update of large files above a minimum size, with an exclude list of extensions that are always copied whole.
- ZIP package as the destination, optional encryption phrase, max ZIP size, and masks that are never zipped.
- Synthetic backup: partial update plus ZIP plus versioning, with checkpoints. Not used by the requirement for v1.

### Internet protocols

Potential future scope, not required for the local jobs this spec covers: FTP (two client stacks), FTPS, SFTP/SSH with keys, WebDAV, HTTP, Amazon S3, Google Drive/storage, Dropbox, and other cloud APIs. Plus proxies, client certificates, upload timestamps, timezone detection, public-ACL flag, and resume with a temporary name. OS-mounted SMB/CIFS, NFS, SSHFS, rclone, NAS, and Drive/Dropbox folders remain supported as ordinary local paths, without OFS owning their network protocol or credentials.

### Other old features

- Copy owner, group, and permissions. Optional compare of those. OFS preserves file mode/timestamps by default and has optional owner/group preservation, permission comparison, and UID/GID comparison for files and preserved links.
- Select Follow, Preserve, or Skip per profile. Preserve creates the link object atomically, retains relative/absolute target text, and works with Older and safe Restore without touching an external target. Follow scans file/directory targets with cycle protection and final root-boundary checks. Dangling/unsupported/cyclic links are unresolved, not absent. Preserved-link move identity includes both kind and target. Link/directory conflicts fail the whole apply preflight before any operation, without recursively deleting the destination directory.
- Mac resource forks split into AppleDouble files.
- Before and after external commands. A leading `&` skips the after-command. A leading `-` runs the after-command only on error. The old Linux build has a “waiting for external command not implemented” path; the new app should actually run the command.
- SMTP mail: on error, on conflict, when nothing happened, or as a digest, with the log attached. Recipients, sender, server, port, SSL. OFS does not send mail.
- Restore wizard: pick a time, masks, size and time filters, then restore from `Older` or from the backup database.
- Flat right side: collapse the source tree into one destination folder.
- “Obsolete Files” folder as a destination for extras.
- Parallel profile scanning is implemented; optional parallel copying remains future scope.
- Tips at startup, check for a new version, registration code. Not part of the new app.
- ExtremeSync Windows service. Not part of the Linux app.

## Wizard (old shape, for reference)

The 5.71 wizard is six steps: folders, direction and subfolders, sync mode, file types, timer, then name and confirm. Advanced mode opens the same settings as separate tabs: masks, comparison, deletions, versioning, schedule, database. The new app can use a single editor. The settings in P0 and P1 still have to exist.

## Migration

One-time read of `~/.SuperFlexibleSynchronizer/SuperFlexibleSynchronizer.cfg`:

- Profile name
- Left path and right path, copied exactly
- Include mask
- Exclusion mask string, including a lone `*` where the old file stored one. That star matches no path.
- Group, derived from the first word of the name

The settings block is aligned on the ASCII size `1099511627776`. Import reads the mode digit (`0` Standard, `1` Exact Mirror), the direction pair (`NY` left to right, `YN` right to left), and the four timestamp letters at the comparison checkboxes (2 seconds, exact hour, ignore timestamp, ignore seconds). A profile whose mode digit is `1` imports as Exact Mirror. A profile whose mode digit is `0` imports as Standard. SmartTracking is not a mode in the new app. Don't Copy versus Ignore Totally, and versioning on or off, do not vary in that block, so exclusions stay Ignore Totally and `Older` stays off. A second import does not write over a profile that already received these flags, and it does not replace paths, masks, notes, or last results.

Last-run summary text may be imported as a note. It must not be treated as a live file list.

## Non-goals

- The GTK2 window, the ElPack tree, or the DPI-lock launcher.
- Loading `lock-dpi.so` or scaling widget sizes. Those workarounds exist only because the old binary cannot be rebuilt.
- A web UI as the only interface.
- Launching the 32-bit program, or applying its DPI lock, from the OFS menu entry.
- Cloud accounts, S3, Google, FTP, or SFTP in the first version.
- Syncovery’s web GUI.
- Writing profiles back into the proprietary `.cfg` so the old 32-bit program can keep editing them.
- A background service that listens on a network port.

## Acceptance for the first version

- The overview shows shared first-word prefixes as groups and keeps singleton prefixes ungrouped. Groups start collapsed, and Expand all and Collapse all open or close every group. Search opens the groups that match. Status and path-availability filters change visibility only. Run checked scans the checked profiles. Run on a row scans that profile only.
- The OFS menu entry starts this app and leaves the 32-bit program alone.
- A profile with Ignore Totally exclusions does not copy those files and does not delete them on the destination in Exact Mirror.
- The same exclusions set to Don't Copy are omitted from copy, and Exact Mirror still treats the destination’s other copy as an extra.
- With default comparison, a tree scan does not read file bytes. Turning on “ignore 2 seconds”, “ignore exactly one hour”, “ignore seconds”, or “ignore timestamp” changes which files are marked changed, in the obvious direction.
- From the preview, open left file, open right folder, save an exclusion onto the profile, and remove one file from this run only. Re-opening the profile shows the saved exclusion and does not show the one-run removal.
- Apply exclusion mask saves the mask you type. Remove this folder from profile saves the parent folder. A row can be forced to copy either way, left unchanged, or reverted to the scan. Compare files launches the configured command when both sides exist. Run on a row scans that profile. Duplicate and delete work from the overview, and delete removes the file database.
- A lone `*` in an imported exclusion list leaves the other files visible.
- Each scan window shows Reading left, then Reading right, and keeps discovered, filtered, and added counts. Pause holds that walk. Skip Rest or Cancel, or closing the window, affects only that profile; unrelated parallel scans continue. Skipped known entries retain a review-only partial listing for exclusions and Rescan, without history/cache/ETA updates or apply. A stopped scan opens no partial comparison. Neither writes a log.
- The comparison is a folder tree. File rows show size, an arrow, and the reason. Folder rows show the total size beside the name and an arrow count for the files under them. Right-clicking a folder can point those files either way, add the folder to the exclusions, or ignore it for this run. The filters hide rows and leave the per-side copy, move, and delete totals on the whole plan. Start runs that plan, including hidden rows. A log is written when that Start begins copying.
- A plan that deletes files opens the three-way confirmation. Confirm Deletions deletes. Continue Without Deletions, the 120-second timeout, copies and moves only. Do Not Continue, and closing the window, change nothing.
- Progress can be paused and canceled between files. The scan window shows the folder being read and discovered/filtered/added counts; Pause, Skip Rest, and Cancel take effect between entries. Skip Rest skips only the current profile and retains review-only partial results for exclusions and Rescan; stopped scans have no partial preview. The results window shows per-side copied, moved, and deleted sizes, and the folder counts. View Log opens the file written for that run. After known path failures, confirmed literal-path or common-relative-folder exclusions may be saved before a fresh confirmed retry; absolute error details are never used as rules.
- A job whose volume is absent is skipped with an error on that row, and the next checked job still runs.
- Replacing a file in a profile that keeps older versions leaves the previous file under `Older`.
- With the file database on, the first scan only records. A later rename of an unchanged file becomes a move. A deletion of an unchanged file on the other side becomes a delete. Both sides changed becomes an ask, and the windowed run skips that file.

## Open points

- Versioning on or off is still not imported. Those bytes are not a per-profile flag OFS reads.
- Whether v1 scheduling is a user systemd timer per checked profile, or a single process that sleeps until the next job. Either is fine if the overview still shows the next run.
- The external compare command defaults to `meld`. Settings stores another command, and `OFS_COMPARE` overrides it for one launch.
- Parallel runs: the old program can start checked profiles together. v1 can run them one at a time if the overview stays responsive.
- The deletion timeout is fixed at 120 seconds, and a folder is called out at one half of its listed files. Neither is a profile setting yet.
- Adjust Them filters the move rows. It does not offer to perform the rename on the other side.
