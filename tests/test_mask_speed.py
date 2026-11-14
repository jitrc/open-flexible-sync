"""The compiled mask matcher must agree with the original rules, and be much faster."""

from __future__ import annotations

import fnmatch
import time

from ofs.masks import (
    _escape_brackets,
    _has_glob,
    _normalize,
    any_match,
    included,
    mask_set,
    split_masks,
)


def _reference_path_matches(rel: str, mask: str) -> bool:
    """The original per-mask algorithm, kept here as the definition to match."""

    rel = rel.replace("\\", "/").strip("/")
    mask = _normalize(mask)
    if not rel or not mask:
        return False
    if mask == "(all)":
        return True
    if mask == "(none)" or mask == "*":
        return False
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
            patterns.append(_escape_brackets(mask.split("/", 1)[1]))
        if any(fnmatch.fnmatch(prefix, item) for item in patterns):
            return True
        if component_mask and fnmatch.fnmatch(prefix.rsplit("/", 1)[-1], pattern):
            return True
    return False


def _reference_path_matches_split(rel: str, mask: str) -> bool:
    return any(_reference_path_matches(rel, item) for item in split_masks(mask))


PATHS = [
    "",
    ".",
    "a",
    "a/b",
    "a/b/c.txt",
    "a/b/c.tmp",
    "cache",
    "cache/package",
    "cache/package-store/package/index.js",
    "cache/work-output/data/file",
    "cache-other/file",
    "nested/cache",
    "nested/cache/package.deb",
    "other/cache.txt",
    "Artists/Album [FLAC]/a.mp3",
    "Artists/Album [FLAC]/nested/b.mp3",
    "System Volume Information/x",
    "vault/p2/d/FX/x.c9r",
    "vault/p2/d/FX/deep/y.c9r",
    "a/.Trash-1000/files/x",
    "album/#recycle/song.mp3",
    "project/node_modules/package/index.js",
    "project/.venv/bin/python",
    "project/src/app.py",
    "datasets/images/.ipynb_checkpoints/a",
    "other/datasets/images/.ipynb_checkpoints/a",
    "source/Project/build/a",
    "Project/build/a",
    "x/downloads/y",
    "x/downloads-other/y",
    "deeper/still/x/node_modules/y/z",
    "abc/def",
    "abc.tmp/def",
    "a/b.tmp",
    "weird [dir]/x",
    "a?c/x",
    "Upper/Case.TMP",
]

MASKS = [
    "*.tmp",
    "*/.Trash-1000",
    "*@eaDir",
    "/System Volume Information",
    "Artist/Album [FLAC]",
    "Artists/Album [FLAC]",
    "*",
    "(none)",
    "(all)",
    "**/.venv",
    "*/downloads",
    "*/package-store/",
    "*/work-output/",
    "*/cache",
    "cache",
    ".venv",
    "**/Project/build",
    "Project/build",
    "a*",
    "*/#recycle",
    "a/b",
    "vault/**",
    "*/*.c9r",
    "?",
    "[ab]",
    "a?c",
    "**/*.tmp",
    "Upper",
    "*.[tT][mM][pP]",
    "a/b/c.txt",
    "x/[ab]c",
]


def test_the_compiled_matcher_agrees_with_the_original_rules():
    for mask in MASKS:
        for path in PATHS:
            expected = _reference_path_matches_split(path, mask)
            actual = any_match(path, [mask])
            assert actual == expected, (mask, path, expected, actual)

    combined = MASKS + ["(none)\xa0*/logs", "*/downloads\xa0**/.venv\xa0**/venv\xa0**/node_modules"]
    for path in PATHS:
        expected = any(_reference_path_matches_split(path, mask) for mask in combined)
        assert any_match(path, combined) == expected, path


def test_a_compound_mask_still_splits_into_its_rules():
    mask = "*/downloads\xa0**/.venv\xa0**/venv\xa0**/node_modules"
    for path in PATHS:
        expected = _reference_path_matches_split(path, mask)
        assert any_match(path, [mask]) == expected
        assert mask_set([mask]).matches(path) == expected


def test_included_keeps_its_shortcuts():
    assert included("a/b.txt", ["(all)"])
    assert included("a/b.txt", [])
    assert included("a/b.txt", ["*"])
    assert included("a/b.jpg", ["*.txt"]) is False
    assert included("a/b.txt", ["(none)", "*.txt"]) is True


def test_a_walk_sized_mask_list_stays_fast():
    masks = [f"*/.cache-{index}" for index in range(80)] + [
        "*.tmp",
        "*/.Trash-1000",
        "*/node_modules",
        "**/.venv",
        "*/cache",
        "*/package-store/",
        "Artist/Album [FLAC]",
        "/System Volume Information",
        "*",
        "*.lock",
        "*.$$$",
        "*/@eaDir",
        "*/#recycle",
        "*/.git",
        "*/.dropbox",
        "*/.ros/log",
        "*/.vscode",
        "*/__pycache__",
        "*.~*",
    ]
    paths = [f"folder{index}/sub/deeper/file-{index}.bin" for index in range(20000)]
    started = time.monotonic()
    hits = sum(1 for path in paths if any_match(path, masks))
    elapsed = time.monotonic() - started
    assert hits == 0
    # The per-entry algorithm took about 30 s for this list; the compiled one
    # is expected to stay in the low seconds even on a slow machine.
    assert elapsed < 8, f"{20000 / elapsed:.0f} entries/s"


def test_random_masks_and_paths_agree_with_the_original_rules():
    import random

    generator = random.Random(20261113)
    tokens = ["a", "b", "cache", "Cache", "x1", ".tmp", "notes.txt", "ab", "c", "build", "Project"]

    def component() -> str:
        token = generator.choice(tokens)
        style = generator.randrange(5)
        if style == 0:
            return token
        if style == 1:
            return f"*{token}"
        if style == 2:
            return f"{token}*"
        if style == 3:
            return f"*{token}*"
        return f"{token}?{generator.choice(tokens)}"

    def mask() -> str:
        depth = generator.randrange(1, 4)
        text = "/".join(component() for _ in range(depth))
        prefix = generator.randrange(5)
        if prefix == 1:
            text = "*/" + text
        elif prefix == 2:
            text = "**/" + text
        elif prefix == 3:
            text = "/" + text
        if generator.randrange(6) == 0:
            text += "/"
        return text

    def path() -> str:
        depth = generator.randrange(1, 6)
        return "/".join(generator.choice(tokens) for _ in range(depth))

    for _ in range(4000):
        one_mask = mask()
        one_path = path()
        assert any_match(one_path, [one_mask]) == _reference_path_matches_split(one_path, one_mask), (
            one_mask,
            one_path,
        )


def test_editing_a_mask_list_in_place_takes_effect_immediately():
    masks = ["*.tmp"]
    assert any_match("a/notes.txt", masks) is False
    masks.append("*.txt")
    assert any_match("a/notes.txt", masks) is True
    masks[1] = "*.bin"
    assert any_match("a/notes.txt", masks) is False
