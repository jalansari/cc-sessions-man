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


def sess(sid, mtime=1.0):
    return Session(session_id=sid, path=Path(sid), mtime=mtime)


def test_save_keeps_labels_saved_by_another_process(tmp_path):
    path = tmp_path / "labels.json"
    ui_cache = Cache(path)  # opened first, like the UI
    cli_cache = Cache(path)  # e.g. cc-sessions in another terminal
    cli_cache.put(sess("from-cli"), "done", "")
    cli_cache.save()

    ui_cache.put(sess("from-ui"), "decision", "")
    ui_cache.save()  # must not wipe the CLI's label

    on_disk = json.loads(path.read_text())
    assert set(on_disk) == {"from-cli", "from-ui"}


def test_save_does_not_resurrect_dropped_labels(tmp_path):
    path = tmp_path / "labels.json"
    cache = Cache(path)
    cache.put(sess("gone"), "done", "")
    cache.put(sess("kept"), "done", "")
    cache.save()
    cache.drop("gone")  # e.g. the session was deleted
    cache.save()
    assert set(json.loads(path.read_text())) == {"kept"}
    cache.put(sess("gone"), "action", "")  # re-classified later: allowed back
    cache.save()
    assert json.loads(path.read_text())["gone"]["label"] == "action"


def test_merge_keeps_the_label_for_the_newer_transcript(tmp_path):
    path = tmp_path / "labels.json"
    ui_cache = Cache(path)
    ui_cache.put(sess("x", mtime=1.0), "decision", "")  # labelled before the session continued
    cli_cache = Cache(path)
    cli_cache.put(sess("x", mtime=2.0), "done", "")  # CLI labelled the newer transcript
    cli_cache.save()

    ui_cache.reload()  # pressing r in the UI
    assert ui_cache.get(sess("x", mtime=2.0))["label"] == "done"
    ui_cache.save()
    assert json.loads(path.read_text())["x"]["mtime"] == 2.0  # and a save doesn't undo it
