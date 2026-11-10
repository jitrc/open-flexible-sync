"""Run a checked subset. Unchecked profiles are not scanned."""

from __future__ import annotations

from dataclasses import dataclass

from ofs.model import Profile
from ofs.scan import ScanResult, scan_profile
from ofs.walk import MissingVolume, WalkStopped


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
) -> list[JobResult]:
    skips = session_skips or {}
    masks = session_masks or {}
    results: list[JobResult] = []
    for profile in profiles:
        if profile.name not in checked:
            continue

        def report(side: str, rel: str, count: int, name: str = profile.name) -> None:
            if on_progress is not None:
                on_progress(name, side, rel, count)

        try:
            result = scan_profile(
                profile,
                session_skips=skips.get(profile.name),
                session_masks=masks.get(profile.name),
                control=control,
                on_progress=report,
            )
        except MissingVolume as exc:
            result = None
            error = f"volume is not mounted: {exc.path}"
        except WalkStopped:
            results.append(JobResult(profile, None, "scan stopped"))
            break
        else:
            error = None
        results.append(JobResult(profile, result, error))
    return results
