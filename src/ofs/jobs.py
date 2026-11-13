"""Run a checked subset. Unchecked profiles are not scanned."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ofs.compare import plan_summary
from ofs.model import Profile
from ofs.run_history import record as record_run
from ofs.scan import ScanResult, scan_profile
from ofs.settings import record_scan_estimate
from ofs.walk import DestinationMissing, MissingFolder, MissingVolume, WalkSkipped, WalkStopped


@dataclass
class JobResult:
    profile: Profile
    result: ScanResult | None
    error: str | None


def scan_summary(job: JobResult) -> str:
    """One line for the overview row and the global history list."""

    if job.result is None:
        if job.error == "scan stopped":
            return "Stopped by user"
        if job.error == "scan skipped":
            return "Skipped"
        # Keep the path: "folder not found" is the actionable part, and both
        # the row and the history list colour anything with "error" as failed.
        return f"Scan error: {job.error or 'scan unavailable'}"
    if job.result.partial_sides:
        return f"Partial scan (review only): {plan_summary(job.result.changes)}"
    cache = "cached " if job.result.cached else ""
    return f"Complete {cache}scan: {plan_summary(job.result.changes)}"


def _record_scan_history(job: JobResult) -> None:
    summary = scan_summary(job)
    try:
        record_run(job.profile.name, "scan", summary)
    except OSError as exc:
        warning = f"Could not save global run history: {exc}"
        if job.result is not None:
            job.result.note = (job.result.note + " " if job.result.note else "") + warning
        else:
            job.error = f"{job.error or 'scan unavailable'}; {warning}"


def run_checked(
    profiles: list[Profile],
    *,
    checked: set[str],
    session_skips: dict[str, set[str]] | None = None,
    session_masks: dict[str, list[str]] | None = None,
    control=None,
    on_progress=None,
    on_stats=None,
    on_entry=None,
    force_uncached: bool = False,
    create_destination: bool = False,
) -> list[JobResult]:
    skips = session_skips or {}
    masks = session_masks or {}
    results: list[JobResult] = []
    for profile in profiles:
        if profile.name not in checked or profile.disabled:
            continue

        def report(side: str, rel: str, count: int, name: str = profile.name) -> None:
            if on_progress is not None:
                on_progress(name, side, rel, count)

        def report_stats(side: str, rel: str, values, name: str = profile.name) -> None:
            if on_stats is not None:
                on_stats(name, side, rel, values)

        def report_entry(side: str, rel: str, stat, name: str = profile.name) -> None:
            if on_entry is not None:
                on_entry(name, side, rel, stat)

        started = time.monotonic()
        try:
            result = scan_profile(
                profile,
                session_skips=skips.get(profile.name),
                session_masks=masks.get(profile.name),
                control=control,
                on_progress=report,
                on_stats=report_stats,
                on_entry=None if on_entry is None else report_entry,
                force_uncached=force_uncached,
                create_destination=create_destination,
            )
        except DestinationMissing as exc:
            result = None
            if exc.creatable:
                error = f"destination folder is missing: {exc.path} (it can be created; its parent exists)"
            else:
                error = f"destination folder is missing: {exc.path} (its parent folder is missing too)"
        except MissingFolder as exc:
            result = None
            error = f"folder not found: {exc.path}"
        except MissingVolume as exc:
            result = None
            error = f"volume is not mounted: {exc.path}"
        except WalkSkipped:
            skipped = JobResult(profile, None, "scan skipped")
            _record_scan_history(skipped)
            results.append(skipped)
            if control is not None:
                control.clear_skip()
            continue
        except WalkStopped:
            stopped = JobResult(profile, None, "scan stopped")
            _record_scan_history(stopped)
            results.append(stopped)
            break
        else:
            error = None
            if not result.partial_sides:
                try:
                    record_scan_estimate(profile.name, result.discovered, time.monotonic() - started)
                except OSError as exc:
                    result.note = (result.note + " " if result.note else "") + f"Could not save the scan ETA estimate: {exc}"
        completed = JobResult(profile, result, error)
        _record_scan_history(completed)
        results.append(completed)
    return results
