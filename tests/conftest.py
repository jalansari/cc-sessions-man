import pytest

import cc_sessions_man.cli as cli
import cc_sessions_man.delete as delete_mod
import cc_sessions_man.scan as scan
import cc_sessions_man.ui as ui


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path, monkeypatch):
    """Keep tests away from the real caches in ~/.cache/cc-sessions-man."""
    monkeypatch.setattr(scan, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cli, "CACHE_PATH", tmp_path / "cache" / "labels.json")
    monkeypatch.setattr(ui, "CACHE_PATH", tmp_path / "cache" / "labels.json")


@pytest.fixture
def trashed(monkeypatch):
    """Record what would go to the Trash, and actually remove it so rescans see the change."""
    import shutil

    moved = []

    def fake(p):
        moved.append(p)
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    monkeypatch.setattr(delete_mod, "send2trash", fake)
    return moved
