from sfs.masks import any_match, included, path_matches, split_masks


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
