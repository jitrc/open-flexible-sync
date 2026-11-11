from ofs.masks import any_match, included, path_matches, split_masks


def test_split_accepts_semicolons_and_lines():
    assert split_masks("*.tmp; */@tmp\n.DS_Store") == ["*.tmp", "*/@tmp", ".DS_Store"]


def test_component_and_subtree_masks():
    assert path_matches("photos/.Trash-1000/files/a", "*/.Trash-1000")
    assert path_matches("a/b/@eaDir/thumb", "*@eaDir")
    assert path_matches("System Volume Information/x", "/System Volume Information")
    assert path_matches("keep/notes.txt", "*.tmp") is False


def test_literal_folder_with_brackets_matches_its_files():
    assert path_matches("Artist/Album [FLAC]/a.mp3", "Artist/Album [FLAC]")
    assert path_matches("Artist/Album [FLAC]/nested/b.mp3", "Artist/Album [FLAC]")
    assert path_matches("other/a.mp3", "Artist/Album [FLAC]") is False


def test_lone_star_does_not_hide_the_tree():
    assert path_matches("album/song.mp3", "*") is False
    assert path_matches("song.mp3", "*") is False
    assert any_match("album/song.mp3", ["*/#recycle", "*", "*/.Trash-1000"]) is False
    assert any_match("album/#recycle/song.mp3", ["*/#recycle", "*"])
    assert included("album/song.mp3", ["*"])


def test_include_all_and_a_real_mask():
    assert included("a/b.txt", ["(all)"])
    assert included("a/b.txt", [])
    assert included("a/b.txt", ["*.txt"])
    assert included("a/b.jpg", ["*.txt"]) is False
    assert any_match("a/b.tmp", ["(none)"]) is False


def test_legacy_compound_exclusions_match_each_rule():
    mask = "*/downloads\xa0**/.venv\xa0**/venv\xa0**/node_modules"
    assert split_masks(mask) == ["*/downloads", "**/.venv", "**/venv", "**/node_modules"]
    assert path_matches("project/.venv/bin/python", mask)
    assert path_matches("project/node_modules/package/index.js", mask)
    assert path_matches("project/src/app.py", mask) is False
    assert path_matches("project/.venv/bin/python", "/datasets/raw;node_modules;.venv")
    assert path_matches("project/node_modules/index.js", "/datasets/raw;node_modules;.venv")
    assert any_match("project/logs/a.txt", ["(none)\xa0*/logs"])


def test_globstar_also_matches_at_the_profile_root():
    assert path_matches(".venv/bin/python", "**/.venv")
    assert path_matches("project/.venv/bin/python", "**/.venv")
    assert path_matches("Project/build/a", "**/Project/build")
    assert path_matches("source/Project/build/a", "**/Project/build")


def test_rooted_full_folder_path_matches_only_that_subtree():
    mask = "/datasets/images/.ipynb_checkpoints"
    assert path_matches("datasets/images/.ipynb_checkpoints/a", mask)
    assert path_matches("other/datasets/images/.ipynb_checkpoints/a", mask) is False


def test_trailing_slash_is_a_folder_mask_not_a_literal_slash():
    assert path_matches("cache/package-store/package/index.js", "*/package-store/")
    assert path_matches("cache/work-output/data/file", "*/work-output/")
