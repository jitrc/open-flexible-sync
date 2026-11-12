# Using OFS

How the window behaves. Download and command lines are in the [README](../README.md). Requirements are in [spec.md](spec.md).

## Profiles

JSON files in `~/.config/open-flexible-sync/profiles`, one file per name. `OFS_HOME` overrides `~/.config/open-flexible-sync` for profiles, the file database, and logs.

Import copies the name, both paths, and the masks, and it fills direction, Standard or Exact Mirror, and the four timestamp checkboxes from the old profile. A mode digit of `1` is Exact Mirror and `0` is Standard. Right to left is the `YN` pair; the usual direction is left to right. Exclusions stay Ignore Totally. Versioning is not in those bytes, so `Older` stays off. A second import does not change a profile again, and it does not replace a path, a mask, a note, or a last result you already have. The group is the first word of the name, but only a prefix shared by multiple profiles becomes a collapsible heading. Single-profile groups stay as ordinary ungrouped rows, as in 5.71. Expand all and Collapse all open or close every real group. Search matches the name and the group and opens groups that match. The status filter can show all, never run in OFS, clean, stopped, errors, or selected profiles. Not run in… takes a day count and shows never-run/undated jobs plus dated OFS runs at least that many days old. Available paths only additionally hides a profile unless both folders exist now. Filtering never changes the checked set; the counter still shows the number Run checked will scan. Each real group has a tri-state checkbox for selecting or clearing its visible enabled members. Disabled members are not selected; hidden filtered-out members are unchanged. A partly selected group shows an indeterminate checkbox. The overview has fixed Profile, Last run, Result, and Actions columns. A run scans only the checked profiles.

Edit can set direction, Copy only (no deletions), Exact Mirror, the exclusion mode, the timestamp rules, binary compare, `Older` for replaced files, and the file database. Copy only is stored as `standard`. Include filters keep `(all)` for everything; replacing it with rules such as `*.jpg` includes only matching files and leaves everything else out of copying and mirror deletion. Include and exclusion rules have separate rows with type labels; rules can be added, edited, removed, and tested against a relative path before saving. Min size, max size, min age, and max age leave a file out of the plan when it is outside those limits. Empty means that limit is off. A file outside the limits is listed as skipped: it is not copied and it is not deleted. The Skipped filter shows those rows. Don't Copy leaves the file visible, and Exact Mirror may delete the other side's copy. Ignore Totally hides the file from copy and from delete. In Exact Mirror, one file that exists only on the source and one file that exists only on the destination become a move when the timestamp rules call them the same file and the match is unique. Same-size duplicates stay a copy and a delete. That pairing does not use the file database. Standard copying still copies the new path and leaves the old one.

Settings holds the compare command and the global exclusions. Until you save that list, every profile ignores `.DS_Store`, `Thumbs.db`, `Temporary Internet Files`, `*.tmp`, `*.$$$`, and `*.~*`. Global masks are ignored totally. Saving an empty list turns them off.

`Older versions` is how many copies to keep. The newest is `Older/<path>`. The one before it is `Older/<path>.~1~`, and so on. The default is one, which replaces the previous archive. A higher number rotates them and deletes the oldest. Keep replaced files and Recoverable deletions are separate opt-in settings. The latter moves files deleted by synchronization into the same `Older` tree; unchanged empty folders are not archived. Import and existing profiles keep permanent deletion as the default.

Restore Older… in the header operates on the one checked profile. It is also available in Edit, including for a disabled profile. Select file versions and choose a destination outside both synchronized folders. Restoration copies the archive atomically without changing the archive or either live tree. Existing destination files are not overwritten. Relative paths are recreated below the chosen folder.

A missing folder fails that job with "volume is not mounted" and the next checked job still runs. Left volume and Right volume, when filled in, name the disk that must hold that folder. The scan does not start when that label is absent or when the folder is on a different disk.

Replace paths, in the header, rewrites the left and right folders of the checked profiles. It asks before saving. The file databases of those profiles are removed, because they still describe the old folders. The next run with the file database on records the new folders.

## Scan

The default scan compares name, size, and last-modified time. It reads file bytes only when Binary compare is checked, and only for pairs whose size matches and whose metadata differs. While the folders are read, a window shows the current path and combined live totals for folders and files discovered, excluded by Ignore Totally, and added to the file list. It also shows elapsed time and discovered entries per second. The first completed scan stores an entry-count/duration estimate for that profile. Later repeat scans show an approximate `~` ETA: initially the previous time remaining, refined from measured pace and the fraction of previous entries discovered. Estimates can be wrong when the tree changes; paused time is excluded. Skipped or canceled scans do not replace the baseline. Those are discovery counts, not files to copy. The scan window stays in front of the overview without locking it, so you can start another profile. That scan waits and starts when the current one finishes. Pause holds the walk. Skip Rest abandons the current profile's incomplete scan and continues with the next checked profile. Cancel, or closing that window, stops the current checked batch. Neither a skipped nor a stopped scan opens a comparison of its partial tree, and the next waiting scan still starts. The bottom line of the comparison window is the plan summary: how many files and how many bytes would be copied, deleted, moved, or left to decide.

## File database

"Remember files and detect moves and deletions" is off until you check it. The database is SQLite at `database/<quoted name>.sqlite` under the config directory.

The first scan with the option on records the current files and does not invent deletions. A later scan treats one missing path and one new path with the same size and time as a move, and renames on the destination when that side still has the old name. A source file that disappeared, while the other side still matches the database, becomes a delete of the other side. If the other side changed after that, or both sides changed, the row is ask. A database stored for different folders is ignored and is not overwritten. After a run, the trees are recorded again from disk.

## Comparison window

One scanned profile opens this window. Several profiles open a summary first, and each row has Preview.

The comparison is a folder tree. Each file row shows its size, a direction arrow, and the reason. The arrow is a small diagram: `L ──▶ R` copies onto the right, `L ◀── R` copies onto the left, `L ──✕ R` deletes on the right, `L ✕── R` deletes on the left, `L ──? R` is undecided, `L ─── R` is unchanged, and `L ··· R` is skipped. A move uses the same arrow as a copy on that side. A folder row shows the folder size beside the name and in the Size column. Its direction cell counts the arrows under it, for example `L ──▶ R  12`, and Status shows the bytes for each of those groups. The filters on the left hide rows. Start runs the whole remaining plan, including rows the filter is hiding. Right-click a file to open it, ignore it for this run, or add its name to the exclusions. Right-click a folder to open it, point every planned file under it, add that folder to the exclusions, or ignore it for this run. Ignore for this run is not saved. Add to exclusions is saved on the profile. Unchanged and skipped files stay put when a folder is pointed.

Right-click also changes the plan:

- Add this name to exclusions saves the file name on the profile and drops matching rows.
- Apply exclusion mask… asks for the mask first. `*.tmp` matches a name in any folder. A path such as `cache/tmp` matches that folder and everything under it. Imported 5.71 semicolon and NBSP-separated lists are treated as separate masks. `**/.venv` matches both a root-level and a nested `.venv`; a trailing slash still means the whole folder. A lone `*` in an imported exclusion list does not hide the tree. 5.71 stores that star between the junk-folder masks.
- Remove this folder from profile saves the parent folder as a mask. A file sitting directly in the profile folder is left for a name exclusion.
- Ignore for this run, and ignore this extension for this run, drop rows without saving the profile.
- Compare files runs the compare command with the left path and the right path. Both files have to exist. The command is `meld` until you change it in Settings. `OFS_COMPARE` overrides the saved command for one launch, and the two paths are added after whatever words you set (`nvim -d` works).
- `L ──▶ R` and `L ◀── R` on a file override that row. The same two arrows on a folder point every planned file in that folder and below it. A copy or an undecided file becomes a copy. A move stays a move. A delete stays a delete, on the side the arrow points at. Leave unchanged overrides one file. Revert to scanned action puts the scan's choice back. Leave unchanged takes the row out of Start.

A saved exclusion is still there after you reopen the profile. An ignore for this run is not. Run on a row scans that profile. Run checked still scans only the checked rows. Duplicate on the overview copies the job to a new name and leaves the file database behind. Delete asks, then removes the profile and its database. The folders on disk stay.

The window opens on Affected. The other State choices are Show All, del./mov., R to L, and Unaffected. R to L means copies onto the left, moves on the left, and deletions on the left.

Existence is Show All, One Side, or Both Sides. Moved files is Show, Show Only, or Don't Show. Files to delete is Show, Show Only, or Don't Show. The groups combine.

Adjust Them is On the Left, On the Right, or Either side. It hides move rows on the other side. It does not perform the rename on the other folder. Left-to-right opens on the right, right-to-left opens on the left, and both-ways opens on Either side.

Files shown, under the filters, counts the visible rows and their sizes.

Above the list, each base folder shows Copy, Move, and Delete for that side: how many files, and their sizes. Copy on the left is files that would be copied onto the left. Those totals follow the whole remaining plan. Ignoring a row or saving an exclusion removes it from the plan and from the totals.

## Safety confirmation

Start opens Safety Confirmations Required when the plan still deletes at least one file. Nothing is copied or deleted until you choose.

The text names the profile and, for each side that would lose files, the folder and `N of M (percent)`. `M` is every file in the comparison list, including unchanged files. The first 200 paths are listed. A folder is listed when the deletions on that side are at least half of the files in that folder.

- Confirm Deletions runs the plan, including deletes.
- Continue Without Deletions runs the copies and the moves and leaves the planned deletes in place.
- Do Not Continue changes nothing. Closing the window does the same.

If the window is still open after 120 seconds, the run continues without deletions. Enter is that same choice.

When the plan deletes nothing, Start goes straight to progress. The comparison list is the confirmation for those copies and moves.

A row marked ask is skipped and reported as left undecided. `run --apply` refuses the whole run while any ask remains, and it writes no log in that case.

## Progress

Copying runs off the UI thread and stays serial. The progress window shows the current relative path, operation count, processed/planned copy bytes, copied/deleted/moved/error counts, elapsed time, active-run speed, ETA, an overall byte-weighted bar, and a current-file bar. Move/delete-only plans use operation count for the overall bar.

Pause and Resume work between copy chunks, including inside a large file. Cancel can stop an in-progress copy. Each file is written to a unique temporary sibling and atomically replaces the destination only when complete, with metadata preserved. With Older enabled, the old destination is archived after the new copy succeeds. Cancellation removes the incomplete temporary file and leaves the prior destination intact. Closing the progress window is Cancel, and it stays until the run has stopped.

Settings has a shared copy speed limit in MiB/s; zero is unlimited. It applies to GUI runs and `run --apply`.

## Results and logs

Copying Results shows whether the run finished, finished with errors, or was stopped. Failed runs offer Retry Failed with confirmation; OFS then rescans the current folders and builds a fresh preview instead of replaying stale operations. Retry follows the normal deletion safety checks. Left and Right columns show files copied to that side, files moved on that side, and files deleted on that side, each as a count and a size. Folders created and folders removed are single totals. New parent folders for a copy, a move, or an `Older` archive count as created. Empty folders left behind by a delete or a move are removed. The profile folder itself stays.

View Log opens the text file for that run. Each run writes one file in `logs/` under the config directory:

`<profile> - YYYY-MM-DD HH.MM.SS.log`

`/`, `\`, and `:` in the profile name are replaced. The text has the profile name, both paths, the safety choice, one line per planned change, and the same totals. `run --apply` prints the totals and the log path.

The overview row stores a short last result: how many files were copied, deleted, and moved.

## Not in this version

Scheduler, unattended runs, multi-edit, partial files, ZIP, internet protocols, and mail. Versioning on or off is not stored as a per-profile flag in this `.cfg`, so import leaves `Older` off. The deletion timeout and the half-folder callout are fixed, not profile settings.
