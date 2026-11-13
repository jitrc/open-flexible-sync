"""Keep every test off the real OFS configuration directory.

Without this, a test that forgets its own ``OFS_HOME`` writes profiles,
settings, or run history into ``~/.config/open-flexible-sync``. Tests that set
``OFS_HOME`` themselves still override this value inside their own body.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OFS_HOME", str(tmp_path / "ofs-home"))
    monkeypatch.setenv("OFS_APP_ID", "com.ofs.LocalSyncTest")
    monkeypatch.delenv("SFS_HOME", raising=False)
    return tmp_path / "ofs-home"
