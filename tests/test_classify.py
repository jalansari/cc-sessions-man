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

    cached, todo = cache.partition(
        [hit, stale, new, sess("run", RUNNING), sess("mid", MID_TASK)]
    )
    assert set(cached) == {"hit"}
    assert [s.session_id for s in todo] == [
        "stale",
        "new",
    ]  # running/interrupted never go to the model


def test_classify_sends_exactly_the_todo_list(tmp_path, monkeypatch):
    import cc_sessions_man.classify as classify_mod
    from cc_sessions_man.scan import ENDED

    sent = []

    def fake_run(prompt, model):
        ids = [
            line.split('"')[1]
            for line in prompt.splitlines()
            if line.startswith("<session id=")
        ]
        sent.extend(ids)
        return [{"id": i, "label": "done", "pending": ""} for i in ids]

    monkeypatch.setattr(classify_mod, "_run_claude", fake_run)
    todo = []
    for i in range(7):
        (tmp_path / f"s{i}.jsonl").write_text("{}\n")
        todo.append(
            Session(
                session_id=f"s{i}",
                path=tmp_path / f"s{i}.jsonl",
                mtime=1.0,
                state=ENDED,
            )
        )
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
    ui_cache.put(
        sess("x", mtime=1.0), "decision", ""
    )  # labelled before the session continued
    cli_cache = Cache(path)
    cli_cache.put(sess("x", mtime=2.0), "done", "")  # CLI labelled the newer transcript
    cli_cache.save()

    ui_cache.reload()  # pressing r in the UI
    assert ui_cache.get(sess("x", mtime=2.0))["label"] == "done"
    ui_cache.save()
    assert (
        json.loads(path.read_text())["x"]["mtime"] == 2.0
    )  # and a save doesn't undo it


def test_session_deleted_mid_batch_is_not_cached(tmp_path, monkeypatch):
    import cc_sessions_man.classify as classify_mod
    from cc_sessions_man.scan import ENDED

    todo = []
    for sid in ("kept", "deleted"):
        p = tmp_path / f"{sid}.jsonl"
        p.write_text("{}\n")
        todo.append(Session(session_id=sid, path=p, mtime=1.0, state=ENDED))

    def run_then_user_deletes(prompt, model):
        (
            tmp_path / "deleted.jsonl"
        ).unlink()  # trashed from the UI while claude was working
        return [{"id": s.session_id, "label": "done", "pending": ""} for s in todo]

    monkeypatch.setattr(classify_mod, "_run_claude", run_then_user_deletes)
    cache = Cache(tmp_path / "labels.json")
    labels = classify_mod.classify(todo, cache, batch_size=5, jobs=1)
    assert set(labels) == {"kept"}
    assert set(json.loads((tmp_path / "labels.json").read_text())) == {"kept"}


def test_cache_file_that_is_not_an_object_starts_empty(tmp_path):
    from cc_sessions_man.scan import ENDED

    for junk in ("[]", "null", "42", '"text"'):
        path = tmp_path / "labels.json"
        path.write_text(junk)
        cache = Cache(path)
        assert cache.data == {}
        cached, todo = cache.partition(
            [Session(session_id="a", path=Path("a"), mtime=1.0, state=ENDED)]
        )
        assert (cached, len(todo)) == ({}, 1)


def test_save_does_not_block_reads_during_file_io(tmp_path, monkeypatch):
    import time

    cache = Cache(tmp_path / "labels.json")
    s = sess("a")
    cache.put(s, "done", "")
    in_io, release = threading.Event(), threading.Event()
    real_read = Cache._read_disk

    def slow_read(self):
        in_io.set()
        release.wait(5)  # pretend the disk is slow
        return real_read(self)

    monkeypatch.setattr(Cache, "_read_disk", slow_read)
    saver = threading.Thread(target=cache.save)
    saver.start()
    assert in_io.wait(5)
    t0 = time.monotonic()
    assert cache.get(s)["label"] == "done"  # the UI thread's read
    waited = time.monotonic() - t0
    release.set()
    saver.join()
    assert waited < 0.5


def test_concurrent_saves_in_one_process_still_merge(tmp_path):
    path = tmp_path / "labels.json"
    caches = [Cache(path) for _ in range(4)]
    threads = []
    for n, c in enumerate(caches):

        def work(c=c, n=n):
            for i in range(50):
                c.put(sess(f"{n}-{i}"), "done", "")
                c.save()

        threads.append(threading.Thread(target=work))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(path.read_text())) == 200  # nobody's labels lost
