#!/usr/bin/env python3
"""Pack OFS as a tar archive and a Linux AppImage.

Both use the machine's Python 3.11+ and GTK 4. They do not bundle those libraries.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
)

LAUNCHER = """#!/bin/sh
ROOT="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -c 'import sys
if sys.version_info < (3, 11):
    raise SystemExit("OFS needs Python 3.11 or newer.")
try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
except Exception:
    raise SystemExit(
        "OFS needs GTK 4 and PyGObject.\\n"
        "Debian or Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0\\n"
        "Fedora: sudo dnf install python3-gobject gtk4"
    )
' || exit 1
exec python3 -m ofs "$@"
"""

APPRUN = LAUNCHER.replace('ROOT="$(dirname "$(readlink -f "$0")")"', 'ROOT="$(dirname "$(readlink -f "$0")")/usr"')

DESKTOP = """[Desktop Entry]
Type=Application
Name=OFS
GenericName=File sync
Comment=Local folder sync
Exec=ofs
Icon=ofs
Terminal=false
StartupWMClass=com.ofs.LocalSync
Categories=Utility;FileTools;
Actions=NewWindow;

[Desktop Action NewWindow]
Name=New Window
Exec=ofs --new-window
Icon=ofs
"""


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    if match is None:
        raise SystemExit("pyproject.toml has no version")
    found = match.group(1)
    ref = os.environ.get("GITHUB_REF_NAME", "")
    if ref.startswith("v") and ref[1:] != found:
        raise SystemExit(f"tag {ref} does not match pyproject version {found}")
    return found


def copy_package(dest_src: Path) -> None:
    package = dest_src / "ofs"
    package.mkdir(parents=True)
    for path in sorted((ROOT / "src" / "ofs").glob("*.py")):
        shutil.copy2(path, package / path.name)


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def stage(dest: Path) -> None:
    """Write the tar layout: ofs, README, and src/."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    write_executable(dest / "ofs", LAUNCHER)
    shutil.copy2(ROOT / "README.md", dest / "README.md")
    copy_package(dest / "src")


def notes(found: str) -> str:
    return f"""OFS {found} for Linux.

The AppImage and the tar archive use Python 3.11 or newer and GTK 4 from the machine.

    chmod +x OFS-{found}-x86_64.AppImage
    ./OFS-{found}-x86_64.AppImage

Debian or Ubuntu, if the window does not open:

    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0

Without AppImage:

    tar -xzf OFS-{found}-linux-x86_64.tar.gz
    ./OFS-{found}/ofs
"""


def make_tar(found: str, staged: Path, dist: Path) -> Path:
    archive = dist / f"OFS-{found}-linux-x86_64.tar.gz"
    if archive.exists():
        archive.unlink()
    subprocess.run(
        ["tar", "-C", str(staged.parent), "-czf", str(archive), staged.name],
        check=True,
    )
    return archive


def appimagetool(dist: Path) -> Path:
    env = os.environ.get("APPIMAGETOOL")
    if env:
        tool = Path(env)
        if not tool.is_file():
            raise SystemExit(f"APPIMAGETOOL is not a file: {tool}")
        return tool
    found = shutil.which("appimagetool")
    if found:
        return Path(found)
    dest = dist / "appimagetool"
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(APPIMAGETOOL_URL, dest)
        dest.chmod(0o755)
    return dest


def make_appimage(found: str, dist: Path) -> Path:
    appdir = dist / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)
    write_executable(appdir / "AppRun", APPRUN)
    (appdir / "ofs.desktop").write_text(DESKTOP, encoding="utf-8")
    shutil.copy2(ROOT / "icons" / "ofs.png", appdir / "ofs.png")
    shutil.copy2(ROOT / "icons" / "ofs.svg", appdir / "ofs.svg")
    copy_package(appdir / "usr" / "src")
    output = dist / f"OFS-{found}-x86_64.AppImage"
    if output.exists():
        output.unlink()
    env = os.environ.copy()
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    env["ARCH"] = "x86_64"
    subprocess.run([str(appimagetool(dist)), str(appdir), str(output)], check=True, env=env)
    output.chmod(0o755)
    shutil.rmtree(appdir)
    return output


def _env(home: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OFS_HOME"] = home
    env["OFS_APP_ID"] = "com.ofs.BuildCheck"
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    return env


def smoke_list(program: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run([str(program), "list"], env=_env(tmp), capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.strip() != "no profiles":
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"{program} list failed")


def smoke_window(program: Path) -> None:
    xvfb = shutil.which("xvfb-run")
    if xvfb is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [xvfb, "-a", "timeout", "4", str(program)],
            env=_env(tmp),
            capture_output=True,
            text=True,
        )
    combined = result.stdout + result.stderr
    if "Traceback" in combined or result.returncode not in (0, 124):
        sys.stderr.write(combined)
        raise SystemExit(f"{program} did not open a window")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--stage":
        stage(Path(argv[2]))
        return 0
    found = version()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "notes.md").write_text(notes(found), encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / f"OFS-{found}"
        stage(staged)
        archive = make_tar(found, staged, dist)
    extract_dir = dist / "_smoke"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir()
    subprocess.run(["tar", "-C", str(extract_dir), "-xzf", str(archive)], check=True)
    program = extract_dir / f"OFS-{found}" / "ofs"
    smoke_list(program)
    smoke_window(program)
    shutil.rmtree(extract_dir)
    image = make_appimage(found, dist)
    smoke_list(image)
    smoke_window(image)
    print(archive)
    print(image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
