"""Run a checked subset. Unchecked profiles are not scanned."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ofs.model import Profile
from ofs.scan import ScanResult, scan_profile
from ofs.settings import record_scan_estimate
from ofs.walk import MissingVolume, WalkSkipped, WalkStopped


@dataclass
class JobResult:
    profile: Profile
    result: ScanResult | None
    error: str | None


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
            )
        except MissingVolume as exc:
            result = None
            error = f"volume is not mounted: {exc.path}"
        except WalkSkipped:
            results.append(JobResult(profile, None, "scan skipped"))
            if control is not None:
                control.clear_skip()
            continue
        except WalkStopped:
            results.append(JobResult(profile, None, "scan stopped"))
            break
        else:
            error = None
            if not result.partial_sides:
                try:
                    record_scan_estimate(profile.name, result.discovered, time.monotonic() - started)
                except OSError as exc:
                    result.note = (result.note + " " if result.note else "") + f"Could not save the scan ETA estimate: {exc}"
        results.append(JobResult(profile, result, error))
    return results
