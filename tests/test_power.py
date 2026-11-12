import subprocess

import pytest

from ofs.power import PowerActionError, run_completion_action


@pytest.mark.parametrize("action,command", [("sleep", "suspend"), ("hibernate", "hibernate"), ("shutdown", "poweroff")])
def test_completion_actions_map_to_explicit_systemctl_commands(action, command, monkeypatch):
    calls = {}

    class Result:
        returncode = 0

    def run(argv, **kwargs):
        calls["argv"] = argv
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(subprocess, "run", run)
    run_completion_action(action)
    assert calls["argv"] == ["systemctl", command]
    assert calls["check"] is True
    assert calls["capture_output"] is True


def test_no_action_never_calls_the_operating_system(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected power command"))
    run_completion_action("")
    run_completion_action("none")
    with pytest.raises(ValueError):
        run_completion_action("unknown")


def test_rejected_or_unavailable_power_command_is_reported(monkeypatch):
    def run(_argv, **_kwargs):
        raise subprocess.CalledProcessError(1, ["systemctl", "suspend"], stderr="permission denied")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PowerActionError, match="permission denied"):
        run_completion_action("sleep")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(PowerActionError, match="systemctl is unavailable"):
        run_completion_action("shutdown")
