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


def test_partition_splits_finished_sessions(tmp_path):
    from cc_sessions_man.scan import ENDED, MID_TASK, RUNNING

    def sess(sid, state, mtime=1.0):
        return Session(session_id=sid, path=Path(sid), mtime=mtime, state=state)

    cache = Cache(tmp_path / "labels.json")
    hit, stale, new = sess("hit", ENDED), sess("stale", ENDED), sess("new", ENDED)
    cache.put(hit, "done", "")
    cache.put(sess("stale", ENDED, mtime=0.5), "done", "")  # transcript changed since

    cached, todo = cache.partition([hit, stale, new, sess("run", RUNNING), sess("mid", MID_TASK)])
    assert set(cached) == {"hit"}
    assert [s.session_id for s in todo] == ["stale", "new"]  # running/interrupted never go to the model


def test_classify_sends_exactly_the_todo_list(tmp_path, monkeypatch):
    import cc_sessions_man.classify as classify_mod
    from cc_sessions_man.scan import ENDED

    sent = []

    def fake_run(prompt, model):
        ids = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<session id=")]
        sent.extend(ids)
        return [{"id": i, "label": "done", "pending": ""} for i in ids]

    monkeypatch.setattr(classify_mod, "_run_claude", fake_run)
    todo = [Session(session_id=f"s{i}", path=Path("x"), mtime=1.0, state=ENDED) for i in range(7)]
    cache = Cache(tmp_path / "labels.json")
    labels = classify_mod.classify(todo, cache, batch_size=3, jobs=2)
    assert sorted(sent) == sorted(labels) == [s.session_id for s in todo]
    assert cache.partition(todo) == ({s.session_id: cache.get(s) for s in todo}, [])
