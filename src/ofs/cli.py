"""Command line. `ofs` with no arguments opens the window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ofs.compare import Action, plan_summary
from ofs.jobs import run_checked
from ofs.legacy import import_cfg
from ofs.model import Profile
from ofs.store import install_imported, load_profiles, save_profile
from ofs.walk import MissingVolume


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ofs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    import_cmd = sub.add_parser("import", help="read a SuperFlexibleSynchronizer.cfg into the JSON profile folder")
    import_cmd.add_argument(
        "--cfg",
        type=Path,
        default=Path.home() / ".SuperFlexibleSynchronizer" / "SuperFlexibleSynchronizer.cfg",
    )

    sub.add_parser("list", help="list profiles")

    scan_cmd = sub.add_parser("scan", help="dry-run one profile")
    scan_cmd.add_argument("name")
    scan_cmd.add_argument("--uncached", action="store_true", help="ignore and refresh destination metadata caches")

    run_cmd = sub.add_parser("run", help="apply one profile")
    run_cmd.add_argument("name")
    run_cmd.add_argument("--apply", action="store_true", help="copy and delete. Without this, the command only prints the plan.")
    run_cmd.add_argument("--uncached", action="store_true", help="ignore and refresh destination metadata caches")

    args = parser.parse_args(argv)
    if args.cmd == "import":
        return _import(args.cfg)
    if args.cmd == "list":
        return _list()
    if args.cmd == "scan":
        return _scan(args.name, apply=False, uncached=args.uncached)
    if args.cmd == "run":
        return _scan(args.name, apply=args.apply, uncached=args.uncached)
    return 2


def _import(cfg: Path) -> int:
    if not cfg.exists():
        print(f"no config file at {cfg}", file=sys.stderr)
        return 1
    count = install_imported(import_cfg(cfg))
    print(f"imported {count} profiles")
    return 0


def _list() -> int:
    profiles = load_profiles()
    if not profiles:
        print("no profiles")
        return 0
    current = ""
    for profile in profiles:
        if profile.group != current:
            current = profile.group
            print(f"[{current}]")
        print(f"  {profile.name}")
        print(f"    {profile.left}")
        print(f"    {profile.right}")
    return 0


def _find(name: str) -> Profile | None:
    for profile in load_profiles():
        if profile.name == name:
            return profile
    return None


def _scan(name: str, *, apply: bool, uncached: bool = False) -> int:
    profile = _find(name)
    if profile is None:
        print(f"no profile named {name!r}", file=sys.stderr)
        return 1
    try:
        results = run_checked([profile], checked={profile.name}, force_uncached=uncached)
    except MissingVolume as exc:
        print(f"volume is not mounted: {exc.path}", file=sys.stderr)
        return 1
    if not results:
        print(f"profile is disabled: {name}", file=sys.stderr)
        return 1
    job = results[0]
    if job.error:
        print(job.error, file=sys.stderr)
        return 1
    assert job.result is not None
    for change in job.result.changes:
        print(f"{change.action.value}\t{change.size}\t{change.rel}\t{change.reason}")
    print(
        f"{job.result.left_files} left, {job.result.right_files} right. "
        + plan_summary(job.result.changes)
    )
    if job.result.note:
        print(job.result.note)
    if not apply:
        return 0
    if job.result.partial_sides:
        print("partial scan is review-only; add exclusions and run a complete scan before applying", file=sys.stderr)
        return 1
    from ofs.apply import apply_plan

    if any(change.action == Action.ASK for change in job.result.changes):
        print("unresolved size conflicts; nothing was applied", file=sys.stderr)
        return 1
    from ofs.scan import invalidate_cached_destinations, remember

    from ofs.settings import copy_speed_limit

    from ofs.review import deletion_violations

    violations = deletion_violations(profile, job.result.listing)
    if violations:
        for violation in violations:
            print(f"deletion protection: {violation}", file=sys.stderr)
        print("nothing was applied", file=sys.stderr)
        return 1
    report = apply_plan(
        profile,
        job.result.changes,
        bytes_per_second=copy_speed_limit(),
        listing=job.result.listing,
    )
    if report.copied or report.deleted or report.moved:
        invalidate_cached_destinations(profile)
    remember(profile)
    profile.last_result = report.summary()
    save_profile(profile)
    from ofs.review import log_body, results_text, write_log

    print(results_text(report))
    log_path = write_log(profile, log_body(profile, job.result.changes, report, choice="apply"))
    print(f"log: {log_path}")
    for error in report.errors:
        print(error, file=sys.stderr)
    return 1 if report.errors else 0
