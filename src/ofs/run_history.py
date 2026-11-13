"""Small, persistent run history plus read-only import of legacy copy logs."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ofs.store import config_dir

_KINDS = {"scan", "copy", "folder-copy"}
_HISTORY_NAME = "run-history.jsonl"
_LOG_NAME = re.compile(r"^.+ - (\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})\.log$")
_FIRST_LINE_LIMIT = 4096
_TAIL_BYTES = 64 * 1024
_HEADLINES = (
    "Synchronization was stopped before any files changed.",
    "Synchronization was stopped.",
    "Synchronization finished with errors.",
    "Synchronization has completed successfully.",
)
_LOCK = threading.RLock()


@dataclass(frozen=True)
class RunEntry:
    profile: str
    when: str
    kind: str
    summary: str
    log_path: Path | None
    epoch: float


def _history_path() -> Path:
    return config_dir() / _HISTORY_NAME


def _log_path(name: str) -> Path:
    return config_dir() / "logs" / name


def _valid_log_name(name) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and not name.endswith(".tmp")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
    )


def record(profile: str, kind: str, summary: str, log_path=None) -> RunEntry:
    """Append one concise run record; an optional log is referenced by basename only."""

    if not isinstance(profile, str) or not profile:
        raise ValueError("profile name must be nonempty text")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError(f"unknown run kind: {kind!r}")
    if not isinstance(summary, str):
        raise TypeError("summary must be text")
    log_name = None
    if log_path is not None:
        log_name = Path(log_path).name
        if not _valid_log_name(log_name):
            raise ValueError("log path must name a file")
    now = datetime.now().astimezone()
    entry = RunEntry(
        profile=profile,
        when=now.strftime("%Y-%m-%d %H:%M:%S"),
        kind=kind,
        summary=summary,
        log_path=_log_path(log_name) if log_name else None,
        epoch=now.timestamp(),
    )
    document = {
        "epoch": entry.epoch,
        "when": entry.when,
        "profile": entry.profile,
        "kind": entry.kind,
        "summary": entry.summary,
        "log": log_name,
    }
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    with _LOCK:
        path = _history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("refusing to append through a symbolic-link run history")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
    return entry


def _structured_entry(data) -> RunEntry | None:
    if not isinstance(data, dict):
        return None
    try:
        profile = data["profile"]
        when = data["when"]
        kind = data["kind"]
        summary = data["summary"]
        epoch = data["epoch"]
        log_name = data.get("log")
    except KeyError:
        return None
    if (
        not isinstance(profile, str)
        or not profile
        or not isinstance(when, str)
        or not isinstance(kind, str)
        or kind not in _KINDS
        or not isinstance(summary, str)
        or isinstance(epoch, bool)
        or not isinstance(epoch, (int, float))
        or (log_name is not None and not _valid_log_name(log_name))
    ):
        return None
    try:
        epoch = float(epoch)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(epoch):
        return None
    return RunEntry(profile, when, kind, summary, _log_path(log_name) if log_name else None, epoch)


def _read_structured() -> tuple[list[RunEntry], set[str]]:
    entries: list[RunEntry] = []
    log_names: set[str] = set()
    path = _history_path()
    if path.is_symlink():
        return entries, log_names
    try:
        with path.open("rb") as stream:
            for line in stream:
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, UnicodeError, ValueError):
                    continue
                entry = _structured_entry(data)
                if entry is None:
                    continue
                entries.append(entry)
                if entry.log_path is not None:
                    log_names.add(entry.log_path.name)
    except OSError:
        pass
    return entries, log_names


def _read_legacy(path: Path, stamp: str) -> RunEntry | None:
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%d %H.%M.%S")
        epoch = parsed.timestamp()
        with path.open("rb") as stream:
            first = stream.readline(_FIRST_LINE_LIMIT + 1)
            if len(first) > _FIRST_LINE_LIMIT:
                return None
            profile = first.rstrip(b"\r\n").decode("utf-8")
            if not profile:
                return None
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _TAIL_BYTES))
            tail = stream.read(_TAIL_BYTES).decode("utf-8", errors="replace")
    except (OSError, UnicodeError, ValueError, OverflowError):
        return None
    summary = next((headline for headline in _HEADLINES if headline in tail), "See log")
    return RunEntry(profile, parsed.strftime("%Y-%m-%d %H:%M:%S"), "copy", summary, path, epoch)


def _read_legacy_logs(recorded_logs: set[str]) -> list[RunEntry]:
    folder = config_dir() / "logs"
    if folder.is_symlink():
        return []
    try:
        candidates = list(folder.iterdir())
    except OSError:
        return []
    entries = []
    for path in candidates:
        match = _LOG_NAME.fullmatch(path.name)
        if match is None or path.name in recorded_logs or path.is_symlink() or not path.is_file():
            continue
        entry = _read_legacy(path, match.group(1))
        if entry is not None:
            entries.append(entry)
    return entries


def list_history() -> list[RunEntry]:
    """Return global history latest-first, with old copy logs included but never modified."""

    with _LOCK:
        structured, recorded_logs = _read_structured()
        ordered = [(entry, 1, index) for index, entry in enumerate(structured)]
        for entry in _read_legacy_logs(recorded_logs):
            ordered.append((entry, 0, entry.log_path.name if entry.log_path else ""))
        ordered.sort(key=lambda item: (item[0].epoch, item[1], item[2]), reverse=True)
        return [entry for entry, _source, _order in ordered]
