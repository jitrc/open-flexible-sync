# PRD: OFS

OFS is a 64-bit desktop app for grouped local sync jobs. The requirements come from the jobs on this machine, from what FreeFileSync and Syncovery do not cover, and from profiles that can be imported from Super Flexible File Synchronizer 5.71.

OFS does not copy that program's binary, artwork, or profile-file format. Import reads `~/.SuperFlexibleSynchronizer/SuperFlexibleSynchronizer.cfg` and leaves the file unchanged.

## Problem

Super Flexible 5.71, already installed beside OFS, is a 32-bit GTK2 program. On a current Linux desktop it needs a 32-bit library stack, a dialog-size workaround, and a font-DPI lock so fractional scaling does not paint the path on top of the profile name. It still runs the real jobs.

FreeFileSync is already installed and is acceptable for a handful of jobs. It is not acceptable for this workload:

- Filters are coarser. This app distinguishes masks that are not copied from masks that are ignored entirely.
- Timestamp comparison cannot ignore small differences, one-hour shifts, seconds, or the timestamp itself.
- There is no overview that groups on the order of 170 jobs by name and runs a checked subset.
- Scans feel slower. The check modes are narrower.
- The preview is missing the right-click actions used on almost every run: open the file or folder, add an exclusion to the profile, and ignore an item only for this run.

Syncovery, from the same author as Super Flexible, is a service with a browser UI at `localhost:8999` on Linux. The Windows and Mac builds still have a native window. The Linux web GUI is thinner, and it is not the desktop app this PRD describes.

## User and current workload

One person, many disks and mounted volumes, on the order of 170 profiles in a proprietary `SuperFlexibleSynchronizer.cfg`. The ini file only holds global preferences.

Profiles are grouped by the first word of the name. A few groups hold most of the jobs, and the rest are small.

The config stores, per profile, a left path, a right path, an include mask (usually `(all)`), and exclusion masks. Typical exclusions are `System Volume Information`, `$RECYCLE.BIN`, `@eaDir`, `@tmp`, and `.Trash-1000`. Last-run text records copied, moved, folders moved, and deleted. The word `Older` appears throughout the file (versioning folder). Partial-update settings exist globally (minimum 512 KB, some extensions excluded from partial update). SMTP fields exist and are empty. No `ftp://`, `sftp://`, or `s3://` URLs are stored.

Global paths today:

- Logs: `~/.SuperFlexibleSynchronizer/Logs`
- Database: `~/.SuperFlexibleSynchronizer/Database`
- Memory cap 2047 MB, copy buffer 1024 KB

## Product goal

A native 64-bit desktop app for this workload: pick a group, check the jobs, scan by size and time, then in the preview open a path, permanently ignore something, or skip it once.

Internet protocols, ZIP packages, and the full SmartTracking database exist in Super Flexible 5.71. They are recorded below so they are not forgotten. They are not required for the first version that can run these jobs.

## Where `sfs/` stands

The 64-bit app lives in `sfs/`. Python 3.14 and GTK 4. Profiles are JSON under `~/.config/sfs` (`SFS_HOME` overrides that directory). How to run it is in `sfs/README.md`. Import reads the old `.cfg` and does not modify it or the 32-bit binary.

In the tree now:

- Overview grouped by the first word. Groups start collapsed, with a distinct heading and a profile count. Expand all, Collapse all, and search. Search opens the groups that match. Run checked scans the checked profiles. Each row has its own Run, plus Edit, Duplicate, and Delete.
- Include and exclusion masks, Don't Copy versus Ignore Totally, Standard versus Exact Mirror, direction, the timestamp rules that are on the editor, optional binary compare, and `Older` for replaced files. A lone `*` stored in an imported exclusion list matches no path.
- Optional per-profile file database: first run records only; a later run can turn a rename into a move and a real deletion into a delete, and it asks when both sides changed. A database stored for different folders is ignored. Duplicate leaves the database behind. Delete removes the profile and the database, and leaves the folders on disk.
- Global exclusions in Settings, ignored totally on every profile. The old defaults apply until that list is saved. Per-profile minimum and maximum size and age. Files outside those limits are skipped: listed under Skipped, not copied, and not deleted. `Older` keeps one replaced file, or the number set on the profile, and drops the oldest beyond that.
- A scan window while the folders are read: which side is being read, files found on the left, files found on the right, pause, and cancel. A stopped scan does not open a partial comparison and does not write a log.
- Comparison window: a folder tree with size and an arrow for direction, filters, per-side copy/move/delete totals, file and folder right-click, Start, the three-way deletion confirmation with a two-minute continue-without-deletions timeout, pause and cancel between files, the results window, and one log file per confirmed copy.
- From a file: add that file name to the exclusions, type a mask, exclude the file's folder, ignore it for this run, compare the two sides, or point the action with `L ──▶ R` or `L ◀── R`, leave it unchanged, or revert. From a folder: open either side, add that folder to the exclusions, ignore it for this run, or point every planned file under it. Compare uses `meld` unless Settings or `SFS_COMPARE` names another command.
- Optional left and right volume labels. A scan refuses a folder that is not on that disk and continues with the next checked profile. Replace paths asks, then rewrites the checked profiles and removes their file databases.
- A GNOME launcher named OFS starts this app. It is separate from the 32-bit program and does not use that program's DPI lock.

Still open, and still in the sections below: scheduler, unattended runs, multi-edit of one field across profiles, partial files, ZIP, internet protocols, and mail. Import now reads direction, Standard versus Exact Mirror, and the four timestamp checkboxes. Versioning bytes do not vary in this `.cfg`, so `Older` stays off. The comparison filters hide rows. Start still runs the whole remaining plan.

## Requirements added after the first draft

These were missing from the first write-up. They come from using the preview and the scan on the imported jobs, and they are required.

- Groups are headings. They start collapsed. Expand all and Collapse all act on every group. Search opens each group that matches.
- Run checked scans the checked profiles. A Run control on the profile row scans that profile only.
- While the folders are being read, a window stays up. It names the profile, says whether it is reading the left folder or the right folder, and shows the path inside that folder. It keeps two counts: files found on the left, and files found on the right. The left count climbs first and stays on screen while the right folder is read. The right count climbs after that. The copy, move, and delete totals belong on the comparison window, after the scan. Pause holds the walk. Cancel, or closing the window, stops it. A stopped scan does not open a comparison of the partial tree, does not start the later checked profiles, and does not write a log.
- The comparison is a folder tree. Folders come before the files inside them. Each file row shows the name, the size, a direction arrow, and the reason. Each folder row shows that folder's total size beside the name and again in the size column. Its direction cell counts the arrows under it, for example `L ──▶ R  12`. Its status cell shows the bytes for each of those groups.
- Direction is an arrow diagram. `L ──▶ R` copies onto the right. `L ◀── R` copies onto the left. `L ──✕ R` deletes on the right. `L ✕── R` deletes on the left. `L ──? R` is undecided. `L ─── R` is unchanged. `L ··· R` is skipped. A move uses the same arrow as a copy on that side. The action name (`copy_l2r` and the rest) stays in the log line, not in the direction column.
- Right-click a folder to open the left folder, open the right folder, add that folder to the profile exclusions, or ignore it for this run. The same menu can point every planned file in that folder and below it with `L ──▶ R` or `L ◀── R`. A copy or an undecided file becomes a copy. A move stays a move. A delete stays a delete, on the side the arrow points at. Unchanged and skipped files stay. A file menu uses the same two arrows for that one file.
- A lone `*` in an exclusion list matches no path. Super Flexible 5.71 stores that star between the junk-folder masks, including on the music profiles. An include list of `(all)` still includes every file. `*.tmp` matches that name in any folder. A mask that names a folder matches that folder and everything under it.
- A log file is written when a confirmed Start begins copying, and when `run --apply` begins copying. Scanning, looking at the preview, canceling a scan, and choosing Do Not Continue do not write a log.
- The desktop entry is named OFS and launches this app on its own. It does not launch the 32-bit program and it does not set that program's DPI lock.

## Users

Primary user: the owner of these profiles. They edit exclusions during a preview more often than they edit a profile form. They run many jobs, not a few.

## P0 — required for the first usable version

These are the requirements the user added, restated with the behavior found in the 5.71 UI.

### Profile overview for many jobs

- A profile has a name, a left folder, and a right folder.
- The main window is a checklist. The group is the first word of the profile name. Each group is a heading, collapsed until opened, and visually separate from the profile rows. The heading shows how many profiles are in the group. Expand all and Collapse all open or close every group. A search opens the groups that match.
- Check profiles and run only the checked set. Each profile row has Run, Edit, Duplicate, and Delete. Run on the row scans that profile only. Run checked scans the checked rows.
- Show only scheduled profiles, and schedule only checked profiles, once scheduling exists.
- Show the last result on the row: counts and bytes for copied, moved, folders moved, and deleted.
- Import existing jobs far enough to keep working: name, both paths, include mask, exclude mask. The old `.cfg` is a binary blob; mode flags are not plain text. A new profile store is required after import. Do not depend on the old 32-bit program to edit jobs.

### Filters

- Include masks and exclusion masks for files and folders. Several masks, separated by semicolon or newline. A mask may point at a text file that holds more masks.
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
- Optional binary comparison while building the file list. The old program labels this slow. It must be off unless the profile asks for it.
- Optional compare of attributes and of permissions.
- Optional “always copy even when the timestamp is unchanged”.

A content hash or remote MD5 is not the default. Byte-for-byte compare is a separate, explicit mode.

### Preview right-click

The preview lists files on the left, the right, or both. Right-click is part of the normal run, not a rare extra.

- **Open file** on the left, on the right, or both.
- **Open folder** on the left, on the right, or both. A folder row opens that folder on the left and on the right as two commands.
- **Apply New Exclusion Mask…** asks for the mask and stores it on that profile.
- **Add this folder to exclusions** on a folder row saves that folder on the profile and drops the matching rows.
- **Remove From List** drops the selected item for this run only. The profile on disk does not change. On a folder row the same command is **Ignore for this run**: the folder and everything under it leave this comparison, and the profile is not saved.
- **Remove From List All Files With This Extension…** drops that extension for this run only.
- **Remove This Folder from Profile…** is permanent.
- **Compare Files…** opens the two sides in an external compare program named in settings.
- The direction column is an arrow diagram: `L ──▶ R`, `L ◀── R`, `L ──✕ R`, `L ✕── R`, `L ──? R`, `L ─── R`, and `L ··· R`. A folder row shows how many files sit on each arrow. The reason stays in the status column. The action name stays in the log.
- Per file, override the proposed action with `L ──▶ R` or `L ◀── R`, or leave it alone. Revert a row to the scanned action.
- Per folder, the same two arrows point every planned file in that folder and below it. A copy or an undecided file becomes a copy. A move stays a move. A delete stays a delete, on the side the arrow points at. Unchanged and skipped files stay.

“Add to the profile ignore list” and “temporary ignore” stay separate commands. One is saved. The other only changes the list on screen.

### Comparison list

One scanned profile opens the comparison window. Several scanned profiles open a summary first, and each row can open the same window.

The comparison is a folder tree of every file the scan saw, including files that would be left unchanged. Folders are listed before the files inside them. Each file row has a name, a size, a direction arrow, and a reason. Each folder row shows that folder's total size beside the name and in the size column, the arrow counts of the files under it, and the bytes for each arrow group. Filters on the left only hide rows. Start still runs the whole remaining plan, including rows the filter is hiding. Removing a row for this run, or saving an exclusion, takes that file out of the plan and out of the totals.

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

A scan of a large tree is metadata only unless the profile turned on binary comparison. The UI stays usable with about 170 profiles loaded. Starting a checked subset does not scan the unchecked jobs.

### Scan progress

Scanning has its own window, separate from the copying progress window.

- The window names the profile, says it is reading the left folder or the right folder, and shows the path inside that folder.
- Two counts stay on screen: files found on the left, and files found on the right. The left count climbs while the left folder is read and remains while the right folder is read. The right count climbs after the left walk finishes.
- Copy, move, and delete totals are shown on the comparison window after the scan.
- Pause holds the walk between files. Resume continues. Cancel stops the walk. Closing the window is Cancel.
- The scan window does not lock the overview. Run on another row, or Run checked, queues that scan. Queued scans start one after another. A stopped scan does not open a comparison of the partial tree and does not write a log. The next waiting scan still starts. Profiles inside one Run checked still stop when that scan is canceled.

### Directions and the two modes these jobs need

- Copy left to right, right to left, or both.
- **Standard copying.** Copy new and changed files. Do not delete extras on the destination unless a profile option says so.
- **Exact Mirror.** The destination matches the source, including deletions, subject to Ignore Totally and to the safety checks below. A file that is only on the source and a file that is only on the destination are a move when the timestamp rules say they are the same file and no other file shares that match. The file database is not required for that pair. Two extras of the same size stay a copy and a delete. Standard copying does not turn that pair into a move.

## P1 — required for the jobs to behave as they do now

Found in the program and in this config. Not called out by name in the later notes, but the stored jobs use them.

### Deletions and safety

- Attended runs ask before deletions. Unattended runs follow the profile and do not stop for each file. The `sfs/` window is attended only.
- Exact Mirror can warn before overwriting a newer file with an older one, before deleting more than a set percentage of a folder, and before deleting every file in a folder.
- Deleted files can be removed permanently, or moved into a specified folder.
- Options: never delete files (folders only); remove folders that became empty; create empty folders; remove destination files that do not match the include masks or the filters.

Start, when the remaining plan deletes at least one file, opens Safety Confirmations Required before it copies or deletes anything. The text names the profile and, for each side that would lose files, the folder and `N of M (percent)`. `M` is every file in the comparison list, including unchanged files. The path list shows the first 200 and then how many more there are. A folder is called out when the deletions on that side are at least half of the files listed in that folder, with the deletion count.

Three actions:

- Confirm Deletions runs the plan, including the deletes.
- Continue Without Deletions runs the copies and the moves and leaves every planned delete in place.
- Do Not Continue changes nothing.

If the window is still open after 120 seconds, the run continues without deletions. Closing the window does the same as Do Not Continue. Enter is the continue-without-deletions action, so a keypress does not confirm deletions.

When the plan deletes nothing, Start goes to progress. The comparison list is the confirmation for those copies and moves.

A row marked ask is skipped by the windowed run and reported as left undecided. `run --apply` refuses the whole run while any ask remains.

### Progress

Copying runs off the UI thread. The progress window shows the path about to be handled, how far the run has got, a bar, and the running copied, deleted, and moved counts. Pause holds the next file. Resume continues. Cancel stops before the next file. A file that has already started is finished. Closing the progress window is Cancel, and the window stays until the run has stopped.

### Versions

The config uses an “Older” tree. The product must be able to keep replaced files as older versions:

- Move the previous file into a folder named `Older`, either as a subfolder of each folder or as one tree that recreates the relative path.
- Cap how many older versions to keep, and prune past that cap.
- Filename versioning is a second, later mechanism (timestamp in the name, `(2)` before the extension). The folder named `Older` is the one this config uses.

### Scheduler

- A profile can be unscheduled or run on a timer.
- Background scheduler can start at login.
- Run checked profiles on their schedule. Do not start a profile whose volume is missing; report that and continue with the next checked profile.
- Log on, log off, and shutdown hooks exist in the old program. They are secondary to the interval timer.

### Logs and results

- After a run, show files copied, moved, renamed, deleted, folders created and removed, and older versions removed.
- Log to one shared file, one file per profile, or one file per run. Keep a capped number of log files.
- A run can be attended (preview, then confirm) or unattended (no preview).

Copying Results is the attended report. The headline says the run finished, finished with errors, or was stopped. Left and Right columns show files copied to that side, files moved on that side, and files deleted on that side, each as a count and a size. Folders created and folders removed are single totals for the run. A new parent folder created for a copy, a move, or an `Older` archive counts as created. An empty folder left behind by a delete or a move is removed and counts as removed. The profile folder itself stays.

View Log opens that run's text file. A confirmed Start that begins copying writes one file in the config directory's `logs` folder. `run --apply` prints the totals and writes the same kind of file once copying starts. A scan, a preview, a canceled scan, and Do Not Continue do not write a log. The text has the profile name, both paths, which safety choice was taken, one line per planned change (the action name, the size, and the path), and the same totals. The file name is `<profile> - YYYY-MM-DD HH.MM.SS.log`. `/`, `\`, and `:` in the profile name are replaced. A cap, a shared log, and unattended runs are still open.

### Volume presence

- A side may require a volume label. If that volume is not mounted, or the folder is on a different disk, skip the job with "volume is not mounted" and continue with the next checked profile. Do not scan the wrong disk.
- Paths in this config are local directories and mount points under `/media`, `/run/media`, and `/home`. Import copies each path as stored.

### Profile editing

- Create, rename, delete, and duplicate a profile. Duplicate copies the job and leaves the file database behind. Delete asks, then removes the profile and its database. The folders on disk stay.
- Edit the same field across many selected profiles (the old Multi Edit).
- Search and replace a path across the checked profiles. Ask before saving. Remove the file databases of the profiles that change, because those databases still describe the old folders.
- Import and export the new profile format.
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
- Cache the destination listing in the database so the next scan does not re-list that tree.
- First run only fills the database. It must not be required for Standard or Exact Mirror. Exact Mirror already shows a unique moved file from the two folders alone. The database adds moves and deletes that can only be seen against the previous run.

### Real-time

- Watch folders and sync on change.
- Safety delay before acting on deletions.
- Ignore certain temporary files while watching.
- On this Linux build the old program reports that network monitoring is not implemented. A new watcher would be inotify/fanotify on local filesystems, not a port of that code.

### Partial files, ZIP, encryption

- Partial update of large files above a minimum size (this config: 512 KB), with an exclude list of extensions that are always copied whole.
- ZIP package as the destination, optional encryption phrase, max ZIP size, and masks that are never zipped.
- Synthetic backup: partial update plus ZIP plus versioning, with checkpoints. Not used by the requirement for v1.

### Internet protocols

Present in 5.71, absent from this config: FTP (two client stacks), FTPS, SFTP/SSH with keys, WebDAV, HTTP, Amazon S3, Google storage. Plus proxies, client certificates, upload timestamps, timezone detection, public-ACL flag, and resume with a temporary name. Size limits in this build: 5 GB on S3, 10 GB on Google.

### Other old features

- Copy owner, group, and permissions. Optional compare of those.
- Symlink recorded as a symlink rather than followed.
- Mac resource forks split into AppleDouble files.
- Before and after external commands. A leading `&` skips the after-command. A leading `-` runs the after-command only on error. The old Linux build has a “waiting for external command not implemented” path; the new app should actually run the command.
- SMTP mail: on error, on conflict, when nothing happened, or as a digest, with the log attached. Recipients, sender, server, port, SSL. This config has the fields and no recipients.
- Restore wizard: pick a time, masks, size and time filters, then restore from `Older` or from the backup database.
- Flat right side: collapse the source tree into one destination folder.
- “Obsolete Files” folder as a destination for extras.
- Run several checked profiles in parallel, or strictly one after another.
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

The settings block is aligned on the ASCII size `1099511627776`. Import reads the mode digit (`0` Standard, `1` Exact Mirror), the direction pair (`NY` left to right, `YN` right to left), and the four timestamp letters at the comparison checkboxes (2 seconds, exact hour, ignore timestamp, ignore seconds). In this file the mode digit is `1` on every profile, and every 5.71 log says Exact Mirror. SmartTracking is not a mode in the new app. Don't Copy versus Ignore Totally, and versioning on or off, do not vary here, so exclusions stay Ignore Totally and `Older` stays off. A second import does not write over a profile that already received these flags, and it does not replace paths, masks, notes, or last results.

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

- The overview shows the imported profiles in the same first-word groups. Groups start collapsed, and Expand all and Collapse all open or close every group. Search opens the groups that match. Run checked scans the checked profiles. Run on a row scans that profile only.
- The OFS menu entry starts this app and leaves the 32-bit program alone.
- A profile with Ignore Totally exclusions does not copy those files and does not delete them on the destination in Exact Mirror.
- The same exclusions set to Don't Copy are omitted from copy, and Exact Mirror still treats the destination’s other copy as an extra.
- With default comparison, a tree scan does not read file bytes. Turning on “ignore 2 seconds”, “ignore exactly one hour”, “ignore seconds”, or “ignore timestamp” changes which files are marked changed, in the obvious direction.
- From the preview, open left file, open right folder, save an exclusion onto the profile, and remove one file from this run only. Re-opening the profile shows the saved exclusion and does not show the one-run removal.
- Apply exclusion mask saves the mask you type. Remove this folder from profile saves the parent folder. A row can be forced to copy either way, left unchanged, or reverted to the scan. Compare files launches the configured command when both sides exist. Run on a row scans that profile. Duplicate and delete work from the overview, and delete removes the file database.
- A lone `*` in an imported exclusion list leaves the other files visible.
- The scan window shows Reading left, then Reading right, and keeps both files-found counts. Pause holds the walk. Cancel, or closing the window, stops it. A stopped scan opens no comparison and writes no log.
- The comparison is a folder tree. File rows show size, an arrow, and the reason. Folder rows show the total size beside the name and an arrow count for the files under them. Right-clicking a folder can point those files either way, add the folder to the exclusions, or ignore it for this run. The filters hide rows and leave the per-side copy, move, and delete totals on the whole plan. Start runs that plan, including hidden rows. A log is written when that Start begins copying.
- A plan that deletes files opens the three-way confirmation. Confirm Deletions deletes. Continue Without Deletions, the 120-second timeout, copies and moves only. Do Not Continue, and closing the window, change nothing.
- Progress can be paused and canceled between files. The scan window shows the folder being read and the file count, and Pause and Cancel take effect between files. A stopped scan does not open a partial comparison. The results window shows per-side copied, moved, and deleted sizes, and the folder counts. View Log opens the file written for that run.
- A job whose volume is absent is skipped with an error on that row, and the next checked job still runs.
- Replacing a file in a profile that keeps older versions leaves the previous file under `Older`.
- With the file database on, the first scan only records. A later rename of an unchanged file becomes a move. A deletion of an unchanged file on the other side becomes a delete. Both sides changed becomes an ask, and the windowed run skips that file.

## Open points

- Versioning on or off is still not imported. Those bytes do not vary in this `.cfg`.
- Whether v1 scheduling is a user systemd timer per checked profile, or a single process that sleeps until the next job. Either is fine if the overview still shows the next run.
- The external compare command defaults to `meld`. Settings stores another command, and `SFS_COMPARE` overrides it for one launch.
- Parallel runs: the old program can start checked profiles together. v1 can run them one at a time if the overview stays responsive.
- The deletion timeout is fixed at 120 seconds, and a folder is called out at one half of its listed files. Neither is a profile setting yet.
- Adjust Them filters the move rows. It does not offer to perform the rename on the other side.
