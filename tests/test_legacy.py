from pathlib import Path

from ofs.legacy import import_cfg, import_global_masks, merge_imported
from ofs.model import Direction, ExclusionMode, Profile, SyncMode, TimestampRules


def _blob(name: str, left: str, right: str, masks: list[str], include: str) -> bytes:
    opt = f"opt_{name}{left}    {right}    d@@@".encode()
    body = "\xa0-\xa0".join(masks).encode("latin1")
    xcl = b"xcl_" + name.encode() + body + b"\x08"
    msk = f"msk_{name}{include}".encode() + b"\x07"
    note = f"rep_{name}4 copied (1KB)".encode()
    return b"....junk...." + opt + b"\x00" + xcl + msk + note + b"\x00"


def test_import_reads_paths_masks_and_leaves_modes_safe(tmp_path):
    cfg = tmp_path / "app.cfg"
    cfg.write_bytes(
        _blob(
            "photos 2017",
            "/data/photos/2017",
            "/backup/photos/2017",
            ["/System Volume Information", "*/$RECYCLE.BIN", "*@eaDir"],
            "(all)",
        )
        + _blob("archive rest", "/data/archive", "/backup/archive", ["(none)"], "(all)")
    )
    profiles = import_cfg(cfg)
    assert [item.name for item in profiles] == ["archive rest", "photos 2017"]
    media = profiles[1]
    assert media.group == "photos"
    assert media.left.endswith("photos/2017")
    assert media.right.endswith("photos/2017")
    assert "*/$RECYCLE.BIN" in media.exclusions
    assert media.include == ["(all)"]
    assert media.mode == SyncMode.STANDARD
    assert media.exclusion_mode == ExclusionMode.IGNORE_TOTALLY
    assert "copied" in media.note
    assert profiles[0].exclusions == []


def _settings_block(mode: str, direction: bytes, times: bytes) -> bytes:
    block = bytearray(b" " * 310)
    block[0:13] = b"1099511627776"
    block[16] = ord(mode)
    block[35:37] = direction
    block[301:305] = times
    return bytes(block)


def test_import_reads_direction_mirror_and_timestamp_flags(tmp_path):
    cfg = tmp_path / "app.cfg"
    right = _blob(
        "videos lessons",
        "/data/videos/lessons",
        "/backup/videos/lessons",
        ["(none)"],
        "(all)",
    )
    # The settings block is inside the opt_ record, before the record ends.
    right = right.replace(b"d@@@", b"d@@@" + _settings_block("1", b"YN", b"YYYN"), 1)
    left = _blob(
        "music library",
        "/data/music",
        "/backup/music",
        ["*"],
        "(all)",
    )
    left = left.replace(b"d@@@", b"d@@@" + _settings_block("1", b"NY", b"YYYN"), 1)
    plain = _blob("archive rest", "/data/archive", "/backup/archive", ["(none)"], "(all)")
    plain = plain.replace(b"d@@@", b"d@@@" + _settings_block("0", b"NY", b"NNNN"), 1)
    cfg.write_bytes(right + left + plain)
    profiles = {item.name: item for item in import_cfg(cfg)}
    learning = profiles["videos lessons"]
    assert learning.mode == SyncMode.EXACT_MIRROR
    assert learning.direction == Direction.RIGHT_TO_LEFT
    assert learning.timestamps.ignore_small
    assert learning.timestamps.ignore_exact_hour
    assert learning.timestamps.ignore_timestamp
    assert not learning.timestamps.ignore_seconds
    music = profiles["music library"]
    assert music.direction == Direction.LEFT_TO_RIGHT
    assert music.timestamps.ignore_timestamp
    assert music.exclusions == ["*"]
    assert profiles["archive rest"].mode == SyncMode.STANDARD
    assert not profiles["archive rest"].timestamps.ignore_small


def test_merge_keeps_an_edited_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path))
    from ofs.store import install_imported, load_profiles, save_profile

    save_profile(
        Profile(
            name="music library",
            left="/data/music",
            right="/backup/music",
            mode=SyncMode.EXACT_MIRROR,
            timestamps=TimestampRules(ignore_timestamp=True),
            last_result="kept",
            note="edited note",
        )
    )
    incoming = Profile(
        name="music library",
        left="/somewhere/else",
        right="/also/else",
        mode=SyncMode.EXACT_MIRROR,
        direction=Direction.RIGHT_TO_LEFT,
        timestamps=TimestampRules(ignore_small=True, ignore_exact_hour=True, ignore_timestamp=True),
        note="from the cfg",
        exclusions=["*.tmp"],
        imported_settings=True,
    )
    install_imported([incoming])
    saved = load_profiles()[0]
    assert saved.left.endswith("music")
    assert saved.right.endswith("backup/music")
    assert saved.mode == SyncMode.EXACT_MIRROR
    assert saved.direction == Direction.RIGHT_TO_LEFT
    assert saved.timestamps.ignore_timestamp
    assert saved.timestamps.ignore_small
    assert saved.timestamps.ignore_exact_hour
    assert saved.last_result == "kept"
    assert saved.note == "edited note"
    assert saved.exclusions == []
    assert saved.imported_settings
    again = merge_imported(saved, Profile(name="music library", left="/x", right="/y", mode=SyncMode.STANDARD))
    assert again.mode == SyncMode.EXACT_MIRROR
    assert again.direction == Direction.RIGHT_TO_LEFT


def test_live_cfg_if_present():
    path = Path.home() / ".SuperFlexibleSynchronizer" / "SuperFlexibleSynchronizer.cfg"
    if not path.exists():
        return
    profiles = import_cfg(path)
    assert len(profiles) >= 150
    assert all(item.left.startswith("/") and item.right.startswith("/") for item in profiles)
    assert any(item.mode == SyncMode.EXACT_MIRROR for item in profiles)
    assert any(item.direction == Direction.RIGHT_TO_LEFT for item in profiles)


def test_import_global_masks_reads_the_dialog_text(tmp_path):
    cfg = tmp_path / "app.cfg"
    cfg.write_bytes(
        _blob("photos 2017", "/data/photos/2017", "/backup/photos/2017", ["*/$RECYCLE.BIN"], "(all)")
        + b"\x01GlobalExcludeMasksIncoming ExtremeSync Packages\xa0*.DS_Store;Thumbs.db;*.$$$"
        + b"\xa0*.ipynb_checkpoints;__pycache__;__pyc\xa0**.sock;*.lock;@eaDir\xa0-\xa0*"
        + b"\x00"
    )
    assert import_global_masks(cfg) == [
        "Incoming ExtremeSync Packages",
        ".DS_Store",
        "Thumbs.db",
        "*.$$$",
        ".ipynb_checkpoints",
        "__pycache__",
        "__pyc",
        "*.sock",
        "*.lock",
        "@eaDir",
    ]


def test_import_global_masks_is_empty_without_the_section(tmp_path):
    cfg = tmp_path / "app.cfg"
    cfg.write_bytes(_blob("photos 2017", "/data/photos/2017", "/backup/photos/2017", ["*/$RECYCLE.BIN"], "(all)"))
    assert import_global_masks(cfg) == []
