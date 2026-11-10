"""`python -m ofs` opens the window. `python -m ofs scan <name>` stays on the command line."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from ofs.cli import main as cli_main

        return cli_main()
    from ofs.ui import main as ui_main

    return ui_main()


if __name__ == "__main__":
    raise SystemExit(main())
