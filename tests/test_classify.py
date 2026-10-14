import json
import threading
from pathlib import Path

from cc_sessions_man.classify import Cache
from cc_sessions_man.scan import Session


def test_cache_is_safe_across_threads(tmp_path):
    cache = Cache(tmp_path / "labels.json")
    errors = []

    def writer(prefix):
        try:
            for i in range(300):
                s = Session(session_id=f"{prefix}{i}", path=Path("x"), mtime=1.0)
                cache.put(s, "done", "")
                if i % 3 == 0:
                    cache.drop(f"{prefix}{i - 1}")
                cache.save()
        except Exception as exc:  # e.g. "dictionary changed size during iteration"
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    on_disk = json.loads((tmp_path / "labels.json").read_text())
    assert on_disk == cache.data
    assert list(tmp_path.glob("*.tmp")) == []  # no temp files left behind
