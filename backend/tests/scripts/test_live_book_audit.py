"""Offline checks for the opt-in real-book acceptance harness. No provider credentials or IO."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import live_book_audit as audit


def test_snapshot_includes_uncheckpointed_wal_and_does_not_mutate_source(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "copy" / "catalog.db"
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE facts(value TEXT)")
        writer.execute("INSERT INTO facts VALUES ('retained WAL data')")
        writer.commit()
        audit.snapshot_db(source, target)
        with sqlite3.connect(target) as copied:
            assert copied.execute("SELECT value FROM facts").fetchone() == ("retained WAL data",)
            copied.execute("INSERT INTO facts VALUES ('copy only')")
        assert writer.execute("SELECT COUNT(*) FROM facts").fetchone() == (1,)


def arguments(tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "one", "bookmark": 1,
                                 "question": "Who is mentioned?", "expected": "answer"}]),
                     encoding="utf-8")
    return ["live_book_audit.py", "--data-dir", str(tmp_path / "data"), "--book-id", "book",
            "--cases", str(cases), "--output", str(tmp_path / "result.json")]


def test_configured_credentials_do_not_authorize_paid_calls_without_run(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(audit, "Settings", lambda: SimpleNamespace(openai_api_key="fixture-only"))
    monkeypatch.setattr(audit, "run", lambda *_args: called.append(True))
    monkeypatch.setattr(audit.sys, "argv", arguments(tmp_path))
    assert audit.main() == 0
    assert called == []
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["live_calls_made"] is False
    assert "fixture-only" not in json.dumps(result)


@pytest.mark.parametrize("budget", ["nan", "inf", "0", "-1", "1.01"])
def test_invalid_budget_is_rejected_before_configuration_load(monkeypatch, tmp_path, budget):
    called = []
    monkeypatch.setattr(audit, "Settings", lambda: called.append(True))
    monkeypatch.setattr(audit.sys, "argv", arguments(tmp_path) + ["--run", "--budget-usd", budget])
    with pytest.raises(SystemExit):
        audit.main()
    assert called == []
