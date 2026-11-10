import os
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
    assert (stage / "src" / "sfs" / "__main__.py").is_file()
    assert (stage / "README.md").is_file()
    assert os.access(stage / "ofs", os.X_OK)
    subprocess.run(["sh", "-n", str(stage / "ofs")], check=True)
    env = os.environ.copy()
    env["SFS_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [str(stage / "ofs"), "list"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "no profiles"
