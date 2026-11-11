"""Include and exclusion masks.

A mask matches a relative path, any parent folder of that path, or a single
path component. `*.tmp` matches in any folder. `*/$RECYCLE.BIN` matches that
folder and everything under it. A leading slash is ignored so
`/System Volume Information` matches the folder of that name.
"""

from __future__ import annotations

import fnmatch


def split_masks(text: str) -> list[str]:
    # 5.71 stores text-list rows with NBSP separators as well as semicolons.
    # Some rows use NBSP-hyphen-NBSP between the file and folder lists.
    text = text.replace("\xa0-\xa0", "\n").replace("\xa0", "\n")
    parts: list[str] = []
    for chunk in text.replace(";", "\n").splitlines():
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def _normalize(mask: str) -> str:
    mask = mask.replace("\\", "/").strip()
    while mask.startswith("./"):
        mask = mask[2:]
    if mask.startswith("/"):
        mask = mask[1:]
    return mask.rstrip("/")


def _has_glob(mask: str) -> bool:
    return "*" in mask or "?" in mask


def _escape_brackets(mask: str) -> str:
    escaped = []
    for char in mask:
        if char == "[":
            escaped.append("[[]")
        elif char == "]":
            escaped.append("[]]")
        else:
            escaped.append(char)
    return "".join(escaped)


def path_matches(rel: str, mask: str) -> bool:
    return any(_path_matches(rel, item) for item in split_masks(mask))


def _path_matches(rel: str, mask: str) -> bool:
    rel = rel.replace("\\", "/").strip("/")
    mask = _normalize(mask)
    if not rel or not mask:
        return False
    if mask == "(all)":
        return True
    if mask == "(none)" or mask == "*":
        # A lone * is stored in many 5.71 exclusion lists, between the
        # junk-folder masks. It does not mean "hide the whole tree".
        # *.tmp and */folder still match.
        return False
    # A typed folder path is literal. fnmatch would treat [FLAC] as a
    # character class and then miss every file in that folder.
    if not _has_glob(mask):
        if "/" in mask:
            return rel == mask or rel.startswith(mask + "/")
        return any(part == mask for part in rel.split("/"))
    parts = rel.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    component_mask = "/" not in mask
    for prefix in prefixes:
        pattern = _escape_brackets(mask)
        patterns = [pattern]
        if mask.startswith("**/"):
            # fnmatch's stars still require the slash, so **/.venv otherwise
            # misses a .venv directly in the profile folder.
            patterns.append(_escape_brackets(mask[3:]))
        if any(fnmatch.fnmatch(prefix, item) for item in patterns):
            return True
        if component_mask and fnmatch.fnmatch(prefix.rsplit("/", 1)[-1], pattern):
            return True
    return False


def any_match(rel: str, masks: list[str]) -> bool:
    return any(path_matches(rel, mask) for mask in masks if mask and mask != "(none)")


def included(rel: str, masks: list[str]) -> bool:
    useful = [item for mask in masks for item in split_masks(mask) if item and item != "(none)"]
    if not useful or useful == ["(all)"] or useful == ["*"]:
        return True
    return any_match(rel, useful)
