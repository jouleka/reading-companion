"""Audit isolation, independent hit-rate accounting and observers must not alter provider guards."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.cost import budgeted_completion
from app.ingest.manifest import AtomSetMismatch
from scripts.real_book_audit import (
    CANARY, aggregate, clone_book, fingerprint, install_observers, latency, restore_rows, run_session,
)
from test_views import env as env
from test_reader_preparation import capture, payload


def test_live_snapshot_captures_wal_and_never_changes_original(env, tmp_path):
    _, bid, app, _ = env
    source = Path(app.state.settings.data_dir)
    before = fingerprint(source, bid)
    destination = tmp_path/"snapshot"
    assert clone_book(source, destination, bid) == before
    with sqlite3.connect(destination/"catalog.db") as db:
        assert db.execute("SELECT bookmark FROM reading_state WHERE book_id=?", (bid,)).fetchone()[0] == 0
        db.execute("UPDATE reading_state SET bookmark=9 WHERE book_id=?", (bid,))
    with sqlite3.connect(destination/"books"/bid/"memory.db") as db:
        assert db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0] == 2
    assert fingerprint(source, bid) == before


@pytest.mark.parametrize("destination", ["source", "inside", "ancestor", "existing"])
def test_snapshot_rejects_existing_or_overlapping_paths(env, tmp_path, destination):
    _, bid, app, _ = env
    source = Path(app.state.settings.data_dir)
    choices = {"source": source, "inside": source/"nested", "ancestor": source.parent,
               "existing": tmp_path}
    before = fingerprint(source, bid)
    with pytest.raises(ValueError, match="new and separate"):
        clone_book(source, choices[destination], bid)
    assert fingerprint(source, bid) == before


def test_snapshot_rejects_path_traversal_before_creating_files(env, tmp_path):
    _, _, app, _ = env
    destination = tmp_path/"snapshot"
    with pytest.raises(AtomSetMismatch):
        clone_book(app.state.settings.data_dir, destination, "../another-book")
    assert not destination.exists()


def test_snapshot_refuses_an_inflight_original_book(env, tmp_path):
    _, bid, app, _ = env
    app.state.catalog.reserve_cost(bid, phase="test", model="stub", input_tokens=1, output_tokens=1,
        usd=.01, max_input_tokens=10**8, max_output_tokens=10**8, max_usd=10)
    with pytest.raises(ValueError, match="in-flight spend"):
        clone_book(app.state.settings.data_dir, tmp_path/"snapshot", bid)


def test_observers_preserve_native_complete_identity_and_forward_budget_cap(env):
    _, bid, app, _ = env
    client = app.state.reader_client
    identity = client.complete.__func__
    client._complete_stub = lambda *args: ("bounded test reply", {"in": 1, "out": 1})
    report, phase = {"provider_calls": []}, {"kind": "test", "frontier": 2}
    install_observers(client, report, phase)
    assert client.complete.__func__ is identity
    value = budgeted_completion(app.state.catalog, app.state.settings.model_copy(update={
        "cost_max_output_tokens_per_call": 17}), client, bid, phase="audit-test",
        system="Keep it short.", user="Alyosha is here.", tier="cheap")
    assert value.value
    assert report["provider_calls"][0]["output_cap"] == 17
    assert report["provider_calls"][0]["kind"] == "test"
    phase.update(kind="other")
    assert report["provider_calls"][0]["kind"] == "test"


@pytest.mark.parametrize("provider", ["generative", "embedding", "jev"])
def test_unread_canary_is_recorded_and_blocked_before_provider_call(env, provider):
    _, _, app, _ = env
    client = app.state.reader_client
    sent = []
    client.jev = SimpleNamespace(evaluate=lambda *a, **k: sent.append(a), close=lambda: None)
    report = {"provider_calls": []}
    install_observers(client, report, {"kind": "test", "frontier": 2})
    with pytest.raises(RuntimeError, match="unread memory"):
        if provider == "generative":
            client.complete("Keep it short", CANARY)
        elif provider == "embedding":
            client.embed([CANARY])
        else:
            client.jev.evaluate({"future": CANARY}, {"q": {}})
    assert not sent
    assert report["provider_calls"][0]["unread_canary"]
    assert report["provider_calls"][0]["error"] == "RuntimeError"


def test_probe_bank_restore_prevents_counting_prior_probe_answers_as_preparation(env):
    _, bid, app, _ = env
    bank, scope = app.state.reader_client.answers, capture(app, bid)
    prepared = (*scope.answer_key, "Where was the meeting?")
    bank.set(prepared, payload())
    bank.mark_suggestion(prepared)
    baseline = deepcopy(bank.rows)
    fresh = (*scope.answer_key, "A novel held-out question?")
    bank.set(fresh, payload())
    restore_rows(bank, baseline)
    assert bank.get(prepared) and not bank.get(fresh)
    assert bank.suggestions(scope.answer_key) == [prepared[3]]
    assert bank.rows is not baseline


def test_hit_rate_excludes_controls_and_failed_future_calls_are_not_refusals():
    report = {"provider_calls": [], "checkpoints": [{"page_saves": [{"seconds": .01}],
        "suggestion_control": {"cached": True, "seconds": .001}, "probes": [
            {"expected": "answer", "seconds": .05, "cached": True, "answered": True, "status": 200},
            {"expected": "answer", "seconds": 4, "answered": True, "status": 200},
            {"expected": "refuse", "seconds": .02, "status": 503},
            {"expected": "refuse", "seconds": .02, "status": 200,
             "insufficient_evidence": True, "claims": []}]}]}
    value = aggregate(report)
    assert value["independent_questions"] == 2 and value["preparation_hits"] == 1
    assert value["answered_under_1_5_seconds"] == 1
    assert value["future_questions"] == 2 and value["future_refusals"] == 1
    assert value["api_errors"] == 1
    assert value["preparation_miss_latency"]["max"] == 4
    assert latency([])["median"] is None


def test_real_book_harness_replays_snapshot_and_reset_without_writing_original(env, tmp_path):
    api, bid, app, atoms = env
    api.put(f"/api/books/{bid}/position", json={"cfi": "test-only-end", "offset": sum(a["char_len"] for a in atoms)})
    source, data = Path(app.state.settings.data_dir), tmp_path/"snapshot"
    before = clone_book(source, data, bid)
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(data),
        reader_prepare_enabled=True, reader_answer_cache=True, recap_prewarm=True,
        recap_disk_cache=True, reader_prepare_delay_seconds=0)
    output = tmp_path/"audit.json"
    report = run_session(settings, bid, output,
        cases={2: [("Where did Berenice meet Aldric?", "At the forge.")]})
    assert report["summary"]["independent_questions"] == 1
    assert report["reset_safe"] and report["reset_restart_safe"]
    assert report["summary"]["provider_canary_leaks"] == 0
    assert report["summary"]["future_citations"] == 0
    assert fingerprint(source, bid) == before
    assert json.loads(output.read_text())["summary"] == report["summary"]


@pytest.mark.parametrize("limit", ["input", "output", "usd"])
def test_preparation_keeps_foreground_headroom_under_shared_atomic_book_caps(env, limit):
    from app.reader_prepare import PreparationCatalog
    _, bid, app, _ = env
    catalog = app.state.catalog
    scope = capture(app, bid)
    costs = catalog.get_costs(bid)
    kwargs = dict(phase="questions", model="test", input_tokens=1, output_tokens=1, usd=.001,
                  max_input_tokens=10**8, max_output_tokens=10**8, max_usd=10)
    if limit == "input":
        kwargs["max_input_tokens"] = sum(r["input_tokens"] for r in costs)+1
    elif limit == "output":
        kwargs["max_output_tokens"] = sum(r["output_tokens"] for r in costs)+1
    else:
        catalog.record_cost(bid, phase="audit-history", model="test", usd=.08)
        kwargs["max_usd"] = .10
    with pytest.raises(RuntimeError, match="cost ceiling"):
        PreparationCatalog(app, scope).reserve_cost(bid, **kwargs)
    assert not catalog.get_cost_reservations(bid)
    reservation = catalog.reserve_cost(bid, **{**kwargs, "phase": "foreground"})
    assert reservation
    catalog.settle_cost(bid, reservation)


def test_pending_preparation_cannot_take_foreground_cash_headroom(env):
    from app.reader_prepare import PreparationCatalog
    _, bid, app, _ = env
    proxy, catalog = PreparationCatalog(app, capture(app, bid)), app.state.catalog
    kwargs = dict(phase="questions", model="test", input_tokens=1, output_tokens=1, usd=.04,
                  max_input_tokens=10**8, max_output_tokens=10**8, max_usd=.10)
    first = proxy.reserve_cost(bid, **kwargs)
    with pytest.raises(RuntimeError, match="book USD cost ceiling"):
        proxy.reserve_cost(bid, **kwargs)
    second = catalog.reserve_cost(bid, **{**kwargs, "phase": "foreground"})
    assert len(catalog.get_cost_reservations(bid)) == 2
    catalog.settle_cost(bid, first)
    catalog.settle_cost(bid, second)


@pytest.mark.parametrize("configured,expected", [(4096, [4096, 3072]), (1000, [1000, 1000])])
def test_preparation_token_allowance_respects_configured_hard_limit(env, monkeypatch, configured, expected):
    from app.reader_prepare import prepare
    _, bid, app, _ = env
    app.state.settings.reader_prepare_enabled = app.state.settings.reader_answer_cache = True
    app.state.settings.cost_max_output_tokens_per_call = configured
    caps = []
    def complete(catalog, settings, client, book_id, **kwargs):
        caps.append(settings.cost_max_output_tokens_per_call)
        if kwargs["schema"].__name__ == "PreparedQuestions":
            return SimpleNamespace(value={"questions": [{"question": "Who is Berenice?", "kind": "character",
                "basis_ids": [1], "follow_up_to": None}]})
        return SimpleNamespace(value={"audits": [{"question_id": 1, "safe_to_prepare": False, "basis_ids": []}]})
    monkeypatch.setattr("app.reader_prepare.budgeted_completion", complete)
    prepare(app, capture(app, bid))
    assert caps == expected
