import configparser
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_staged_release_lists_no_profiles(tmp_path):
    stage = tmp_path / "OFS"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_release.py"), "--stage", str(stage)],
        check=True,
    )
    assert (stage / "src" / "ofs" / "__main__.py").is_file()
    assert (stage / "README.md").is_file()
    assert os.access(stage / "ofs", os.X_OK)
    subprocess.run(["sh", "-n", str(stage / "ofs")], check=True)
    env = os.environ.copy()
    env["OFS_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [str(stage / "ofs"), "list"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "no profiles"


def test_source_and_release_desktop_entries_have_new_window_action():
    desktop = (ROOT / "com.ofs.LocalSync.desktop").read_text(encoding="utf-8")
    release = runpy.run_path(str(ROOT / "scripts" / "build_release.py"))["DESKTOP"]
    for text in (desktop, release):
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read_string(text)
        assert config["Desktop Entry"]["Actions"] == "NewWindow;"
        assert config["Desktop Action NewWindow"]["Name"] == "New Window"
        assert config["Desktop Action NewWindow"]["Exec"] == "ofs --new-window"
