import json

import cc_sessions_man.classify as classify_mod
import cc_sessions_man.cli as cli
from cc_sessions_man.classify import Cache
from cc_sessions_man.scan import parse_transcript


def make_home(tmp_path, ids):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    for i, sid in enumerate(ids):
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"content": f"prompt {sid}"}, "cwd": "/w", "timestamp": f"2026-10-01T10:{i:02d}:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}, "timestamp": f"2026-10-01T10:{i:02d}:30Z"},
        ]))
    (tmp_path / "sessions").mkdir()
    return tmp_path


def fake_claude(label):
    def run(prompt, model):
        ids = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<session id=")]
        return [{"id": i, "label": label, "pending": ""} for i in ids]
    return run


def test_refresh_with_limit_keeps_other_labels(tmp_path, monkeypatch, capsys):
    home = make_home(tmp_path, ["old", "new"])  # "new" is the most recent
    cache = Cache(cli.CACHE_PATH)
    for sid in ("old", "new"):
        cache.put(parse_transcript(home / "projects" / "-w" / f"{sid}.jsonl"), "decision", "")
    cache.save()

    monkeypatch.setattr(classify_mod, "_run_claude", fake_claude("done"))
    cli.main(["--claude-home", str(home), "--refresh", "-n", "1", "--json"])
    capsys.readouterr()

    saved = json.loads(cli.CACHE_PATH.read_text())
    assert saved["new"]["label"] == "done"  # re-classified
    assert saved["old"]["label"] == "decision"  # outside the filter: untouched


def test_cli_reports_failed_batches_as_error(tmp_path, monkeypatch, capsys):
    home = make_home(tmp_path, ["a", "b"])

    def boom(prompt, model):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(classify_mod, "_run_claude", boom)
    cli.main(["--claude-home", str(home), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert {r["group"] for r in rows} == {"error"}
    assert all(r["detail"] == "rate limited" and r["status_rank"] == 6 for r in rows)
    assert not cli.CACHE_PATH.exists() or json.loads(cli.CACHE_PATH.read_text()) == {}
