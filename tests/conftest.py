import pytest

import cc_sessions_man.cli as cli
import cc_sessions_man.scan as scan
import cc_sessions_man.ui as ui


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path, monkeypatch):
    """Keep tests away from the real caches in ~/.cache/cc-sessions-man."""
    monkeypatch.setattr(scan, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cli, "CACHE_PATH", tmp_path / "cache" / "labels.json")
    monkeypatch.setattr(ui, "CACHE_PATH", tmp_path / "cache" / "labels.json")
