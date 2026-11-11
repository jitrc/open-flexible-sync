"""Read profiles from a Super Flexible 5.71 .cfg blob.

Settings are aligned on the ASCII size ``1099511627776`` (1 TiB).
The mode digit at +16 is ``1`` for Exact Mirror and ``0`` for Standard.
SmartTracking has no mode here. ``NY`` / ``YN`` at +35 is left-to-right /
right-to-left. The four Y/N letters at +301 are the comparison checkboxes,
in form order: 2 seconds, exact hour, ignore timestamp, ignore seconds.
Versioning and Don't Copy versus Ignore Totally stay at the defaults.
"""

from __future__ import annotations

import re
from pathlib import Path

from ofs.masks import split_masks
from ofs.model import Direction, ExclusionMode, Profile, SyncMode, TimestampRules

_SEP = b"\xa0-\xa0"
_ANCHOR = b"1099511627776"
_MODE_AT = 16
_DIRECTION_AT = 35
_TIME_AT = 301


def import_cfg(path: Path) -> list[Profile]:
    data = path.read_bytes()
    records = _path_records(data)
    exclusions = _mask_records(data, b"xcl_", set(records))
    includes = _mask_records(data, b"msk_", set(records))
    notes = _notes(data, set(records))
    bodies = _opt_bodies(data)
    profiles: list[Profile] = []
    for name, left, right in records.values():
        include = includes.get(name) or ["(all)"]
        if include == ["(none)"]:
            include = ["(all)"]
        excluded = [item for item in exclusions.get(name, []) if item != "(none)"]
        mode, direction, timestamps = _settings(bodies.get(name, b""))
        profiles.append(
            Profile(
                name=name,
                left=left,
                right=right,
                include=include,
                exclusions=excluded,
                exclusion_mode=ExclusionMode.IGNORE_TOTALLY,
                direction=direction,
                mode=mode,
                timestamps=timestamps,
                note=notes.get(name, ""),
                imported_settings=True,
            )
        )
    profiles.sort(key=lambda item: (item.group.lower(), item.name.lower()))
    return profiles


def import_global_masks(path: Path) -> list[str]:
    """Masks from the cfg's GlobalExcludeMasks section, in dialog order. Empty when absent.

    The section stores the dialog text: lines after the first begin with the
    byte pair 0xA0 '*', and the section ends at 0xA0 '-' 0xA0. The mode radio
    after the terminator is not decoded; the mode stays a Settings choice.
    """

    data = path.read_bytes()
    # The schema names the field first; the populated value record comes last.
    index = data.rfind(b"GlobalExcludeMasks")
    if index < 0:
        return []
    start = index + len(b"GlobalExcludeMasks")
    rest = data[start : start + 4000]
    end = rest.find(_SEP)
    if end >= 0:
        rest = rest[:end]
    nul = rest.find(b"\x00")
    if nul >= 0:
        rest = rest[:nul]
    text = rest.decode("latin1")
    lines = text.replace("\xa0*", "\n").replace("\xa0", "\n").splitlines()
    masks: list[str] = []
    for line in lines:
        for part in line.split(";"):
            item = part.strip()
            if item and item not in masks:
                masks.append(item)
    return masks


def merge_imported(existing: Profile, incoming: Profile) -> Profile:
    """Fill mode, direction, and timestamp flags that are still at the defaults.

    ``imported_settings`` makes a second import leave the profile alone, so a
    job edited back to Standard stays Standard. Paths, masks, the note, and
    the last result are not replaced.
    """

    if existing.imported_settings:
        return existing
    if existing.mode == SyncMode.STANDARD:
        existing.mode = incoming.mode
    if existing.direction == Direction.LEFT_TO_RIGHT:
        existing.direction = incoming.direction
    rules = existing.timestamps
    found = incoming.timestamps
    if not rules.ignore_small:
        rules.ignore_small = found.ignore_small
    if not rules.ignore_exact_hour:
        rules.ignore_exact_hour = found.ignore_exact_hour
    if not rules.ignore_seconds:
        rules.ignore_seconds = found.ignore_seconds
    if not rules.ignore_timestamp:
        rules.ignore_timestamp = found.ignore_timestamp
    if not rules.larger_is_newer:
        rules.larger_is_newer = found.larger_is_newer
    existing.imported_settings = True
    return existing


def _settings(payload: bytes) -> tuple[SyncMode, Direction, TimestampRules]:
    anchor = payload.find(_ANCHOR)
    if anchor < 0:
        return SyncMode.STANDARD, Direction.LEFT_TO_RIGHT, TimestampRules()
    mode = SyncMode.EXACT_MIRROR if _at(payload, anchor + _MODE_AT) == ord("1") else SyncMode.STANDARD
    pair = payload[anchor + _DIRECTION_AT : anchor + _DIRECTION_AT + 2]
    direction = Direction.RIGHT_TO_LEFT if pair == b"YN" else Direction.LEFT_TO_RIGHT
    flags = payload[anchor + _TIME_AT : anchor + _TIME_AT + 4]
    yes = [len(flags) > index and flags[index] == ord("Y") for index in range(4)]
    return mode, direction, TimestampRules(
        ignore_small=yes[0],
        ignore_exact_hour=yes[1],
        ignore_timestamp=yes[2],
        ignore_seconds=yes[3],
    )


def _at(payload: bytes, index: int) -> int:
    if 0 <= index < len(payload):
        return payload[index]
    return -1


def _opt_bodies(data: bytes) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    start = 0
    while True:
        index = data.find(b"opt_", start)
        if index < 0:
            break
        start = index + 4
        end = _next_record(data, index + 4)
        payload = data[index:end]
        text = _printable_at(data, index + 4)
        slash = text.find("/")
        if slash <= 0:
            continue
        name = text[:slash]
        previous = found.get(name)
        if previous is None or _ANCHOR in payload:
            found[name] = payload
    return found


def _next_record(data: bytes, start: int) -> int:
    index = start
    while True:
        index = data.find(b"\x01", index)
        if index < 0:
            return len(data)
        tag = data[index + 1 : index + 5]
        if len(tag) == 4 and tag[3:4] == b"_" and tag[:3].isalpha():
            return index
        index += 1


def _printable_at(data: bytes, start: int, limit: int = 2000) -> str:
    chars: list[str] = []
    index = start
    while index < len(data) and 32 <= data[index] < 127 and len(chars) < limit:
        chars.append(chr(data[index]))
        index += 1
    return "".join(chars)


def _path_records(data: bytes) -> dict[str, tuple[str, str, str]]:
    found: dict[str, tuple[str, str, str]] = {}
    start = 0
    while True:
        index = data.find(b"opt_", start)
        if index < 0:
            break
        start = index + 4
        text = _printable_at(data, index + 4)
        slash = text.find("/")
        if slash <= 0:
            continue
        name = text[:slash]
        parts = [part for part in re.split(r" {2,}", text[slash:].strip()) if part.startswith("/")]
        if len(parts) < 2:
            continue
        found[name] = (name, parts[0], parts[1])
    return found


def _mask_records(data: bytes, prefix: bytes, names: set[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    start = 0
    while True:
        index = data.find(prefix, start)
        if index < 0:
            break
        start = index + len(prefix)
        raw = data[index + len(prefix) : index + len(prefix) + 4000]
        # The name is ASCII. Masks may contain the 0xA0 separator byte.
        name = _longest_name(raw, names)
        if not name:
            continue
        rest = raw[len(name) :]
        cut = _end_of_masks(rest)
        text = rest[:cut].decode("latin1")
        masks = split_masks(text)
        if masks:
            found[name] = masks
    return found


def _longest_name(raw: bytes, names: set[str]) -> str:
    best = ""
    for name in names:
        encoded = name.encode("latin1", errors="ignore")
        if raw.startswith(encoded) and len(name) > len(best):
            best = name
    return best


def _end_of_masks(rest: bytes) -> int:
    for index, byte in enumerate(rest):
        if byte == 0xA0 or byte == ord("-") or 32 <= byte < 127 or byte in (ord("*"), ord("/")):
            continue
        return index
    return len(rest)


def _notes(data: bytes, names: set[str]) -> dict[str, str]:
    notes: dict[str, str] = {}
    start = 0
    while True:
        index = data.find(b"rep_", start)
        if index < 0:
            break
        start = index + 4
        raw = data[index + 4 : index + 4 + 500]
        name = _longest_name(raw, names)
        if not name:
            continue
        rest = _printable_at(data, index + 4 + len(name), limit=240).strip()
        if rest:
            notes[name] = rest
    return notes
