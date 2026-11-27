"""`python -m ofs` opens the window. `python -m ofs scan <name>` stays on the command line."""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1:] != ["--new-window"]:
        from ofs.cli import main as cli_main

        return cli_main()
    # GTK 4's Vulkan renderer aborts the process if a window is closed while a
    # frame is still being presented. Copying Results does that, and GNOME
    # then offers Force Quit. OpenGL does not take that path. An explicit
    # GSK_RENDERER in the environment still wins.
    os.environ.setdefault("GSK_RENDERER", "gl")
    from ofs.ui import main as ui_main

    return ui_main(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
