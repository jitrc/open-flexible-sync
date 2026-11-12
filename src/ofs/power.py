"""Explicit, Linux-only actions after a user-approved successful copy."""

from __future__ import annotations

import subprocess


class PowerActionError(Exception):
    pass


def run_completion_action(action: str) -> None:
    """Run only an explicitly selected action; empty/none never touches the OS."""

    command = {"sleep": "suspend", "hibernate": "hibernate", "shutdown": "poweroff"}.get(action)
    if command is None:
        if action in {"", "none"}:
            return
        raise ValueError(f"unknown completion action: {action}")
    try:
        subprocess.run(["systemctl", command], check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise PowerActionError("systemctl is unavailable; no power action was performed") from exc
    except OSError as exc:
        raise PowerActionError(f"could not start systemctl: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise PowerActionError(f"systemctl {command} failed: {detail}") from exc
