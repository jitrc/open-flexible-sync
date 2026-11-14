"""Include and exclusion masks.

A mask matches a relative path, any parent folder of that path, or a single
path component. `*.tmp` matches in any folder. `*/$RECYCLE.BIN` matches that
folder and everything under it. A leading slash is ignored so
`/System Volume Information` matches the folder of that name.
"""

from __future__ import annotations

import fnmatch
import re


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
        if mask.startswith(("**/", "*/")):
            # fnmatch's stars still require the slash, so */cache otherwise
            # misses a cache directly in the profile folder.
            patterns.append(_escape_brackets(mask.split("/", 1)[1]))
        if any(fnmatch.fnmatch(prefix, item) for item in patterns):
            return True
        if component_mask and fnmatch.fnmatch(prefix.rsplit("/", 1)[-1], pattern):
            return True
    return False


class MaskSet:
    """A mask list prepared once, so a walk does not re-parse it for every entry.

    The matching rules are exactly those of `_path_matches`: a mask matches the
    entry itself, any parent folder, or a single path component, and a lone `*`
    matches nothing. Patterns are translated once and combined into one regular
    expression per kind, which keeps a walk with a hundred masks fast.
    """

    __slots__ = ("all", "names", "paths", "prefix", "name", "parents")

    def __init__(self, masks) -> None:
        match_all = False
        names: set[str] = set()
        paths: set[str] = set()
        prefix: list[str] = []
        name: list[str] = []
        for mask in masks:
            if not mask or mask == "(none)":
                continue
            for item in split_masks(mask):
                normalized = _normalize(item)
                if not normalized:
                    continue
                if normalized == "(all)":
                    match_all = True
                    continue
                if normalized in {"(none)", "*"}:
                    continue
                if not _has_glob(normalized):
                    if "/" in normalized:
                        paths.add(normalized)
                    else:
                        names.add(normalized)
                    continue
                prefix.append(fnmatch.translate(_escape_brackets(normalized)))
                if normalized.startswith(("**/", "*/")):
                    # A leading star must also match at the profile root.
                    prefix.append(fnmatch.translate(_escape_brackets(normalized.split("/", 1)[1])))
                if "/" not in normalized:
                    name.append(fnmatch.translate(_escape_brackets(normalized)))
        self.all = match_all
        self.names = frozenset(names)
        self.paths = frozenset(paths)
        self.prefix = re.compile("|".join(prefix)) if prefix else None
        self.name = re.compile("|".join(name)) if name else None
        # Files arrive grouped by folder, so the ancestor answer is reused instead of
        # being recomputed for every entry in the same folder.
        self.parents: dict[str, bool] = {}

    def _ancestor_hit(self, parts: list[str]) -> bool:
        prefix = self.prefix
        name = self.name
        paths = self.paths
        if prefix is None and name is None and not paths:
            return False
        current = ""
        for part in parts:
            current = part if not current else current + "/" + part
            if paths and current in paths:
                return True
            if prefix is not None and prefix.match(current):
                return True
            if name is not None and name.match(part):
                return True
        return False

    def matches(self, rel: str) -> bool:
        rel = rel.replace("\\", "/").strip("/")
        if not rel:
            return False
        if self.all:
            return True
        parts = rel.split("/")
        if self.names and not self.names.isdisjoint(parts):
            return True
        prefix = self.prefix
        name = self.name
        paths = self.paths
        if prefix is None and name is None and not paths:
            return False
        if len(parts) > 1:
            parent = rel.rsplit("/", 1)[0]
            known = self.parents.get(parent)
            if known is None:
                known = self._ancestor_hit(parts[:-1])
                if len(self.parents) > 1024:
                    self.parents.clear()
                self.parents[parent] = known
            if known:
                return True
        if paths and rel in paths:
            return True
        if prefix is not None and prefix.match(rel):
            return True
        if name is not None and name.match(parts[-1]):
            return True
        return False


_recent_sets: list[tuple[tuple[str, ...], MaskSet]] = []


def mask_set(masks) -> MaskSet:
    """The prepared matcher for a mask list, reused across entries.

    The key is the mask contents, so editing a rule list in place (a live
    exclusion) takes effect immediately; comparing tuples of the same strings is
    cheap because each element compares by identity first.
    """

    key = tuple(masks)
    for known, prepared in _recent_sets:
        if known == key:
            return prepared
    prepared = MaskSet(key)
    _recent_sets.insert(0, (key, prepared))
    del _recent_sets[4:]
    return prepared


def any_match(rel: str, masks) -> bool:
    if not masks:
        return False
    return mask_set(masks).matches(rel)


def included(rel: str, masks: list[str]) -> bool:
    useful = [item for mask in masks for item in split_masks(mask) if item and item != "(none)"]
    if not useful or useful == ["(all)"] or useful == ["*"]:
        return True
    return any_match(rel, useful)
