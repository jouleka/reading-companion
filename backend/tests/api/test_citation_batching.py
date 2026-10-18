"""Offline concurrency, scope and cost contracts; canned audits do not test model accuracy."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from openai import LengthFinishReasonError

from app.api import views
from app.ask_evidence import AskEvidenceError, evidence_prompt
from app.cost import CostCeilingExceeded
from test_views import env as env, _supported_citation_audit


def fixture_draft():
    texts = [
        "Nell returned to the workshop, where Orin was waiting; she placed the key on the desk.",
        "Orin looked at the key, then opened the cupboard; the cupboard contained a sealed letter.",
        "Nell read the letter, which described the journey to the coast; she showed it to Orin.",
        "Orin folded the letter, placed it in his coat pocket; he told Nell he would leave at dawn.",
        "Nell remained by the window, watching the courtyard; she saw the messenger return.",
        "The messenger entered the workshop, carrying a small parcel; Nell accepted it.",
    ]
    value = {"insufficient_evidence": False,
             "claims": [{"text": text, "citation_ids": [1]} for text in texts]}
    sources = [{"id": 1, "ordinal": 2, "title": "The workshop", "text": " ".join(texts)}]
    settings = SimpleNamespace(cost_max_output_tokens_per_call=4096, jev_evidence_enabled=False)
    client = SimpleNamespace(provider="openai-compatible")
    return value, sources, settings, client


def request_data(request):
    return json.loads(request["user"].split("(untrusted JSON data):\n", 1)[1])


def run_review(value, sources, settings, client, remembered, catalog=None, book_id="fixture"):
    return views._complete_citation_evidence(
        value, sources, settings=settings, client=client, catalog=catalog, book_id=book_id,
        remember=remembered.append, question="What happened in the workshop?",
    )


@pytest.mark.parametrize("partial", [False, True])
def test_real_sized_claim_batches_retain_all_context_original_ids_and_order(monkeypatch, partial):
    value, sources, settings, client = fixture_draft()
    original = copy.deepcopy((value, sources))
    all_data = json.loads(evidence_prompt(value, sources).split("(untrusted JSON data):\n")[1])
    expected = [[2, 4], [5, 6]] if partial else [[1, 2], [3, 4], [5, 6]]
    if partial:
        settings.jev_evidence_enabled = settings.jev_partial_evidence_enabled = True
        client.jev = object()
        approvals = _supported_citation_audit(all_data, [1])
        approvals["claim_audits"] = [a for a in approvals["claim_audits"] if a["claim_id"] in [1, 3]]
        monkeypatch.setattr(views.jev_tasks, "evidence_partial", lambda *_a, **_kw: approvals)
    lock, together, second_done = Lock(), Barrier(2), Event()
    entered, finished, remembered = [], [], []
    active = peak = 0

    def complete(_catalog, actual_settings, actual_client, _book, **request):
        nonlocal active, peak
        assert actual_settings is settings and actual_client is client
        assert request["phase"] == "citation-review" and request["tier"] == "cheap"
        data = request_data(request)
        ids = [c["claim_id"] for c in data["claims"]]
        assert data["answer_context"] == all_data["claims"]
        assert data["sources"] == all_data["sources"]
        assert data["reader_question"] == "What happened in the workshop?"
        assert sum(len(c["units"]) for c in data["claims"]) <= 8
        assert len(ids) <= 2 and "approved" not in data
        with lock:
            entered.append(ids)
            active += 1
            peak = max(peak, active)
        if ids in expected[:2]:
            together.wait(timeout=5)
            if ids == expected[0]:
                assert second_done.wait(5)
        with lock:
            finished.append(ids)
            active -= 1
        if ids == expected[1]:
            second_done.set()
        return SimpleNamespace(value=_supported_citation_audit(data, [1]), requested=ids)

    monkeypatch.setattr(views, "budgeted_completion", complete)
    result = run_review(value, sources, settings, client, remembered)
    assert result.model_dump(mode="json") == value
    assert sorted(entered) == expected and finished[0] == expected[1] and peak == 2 and active == 0
    assert [r.requested for r in remembered] == expected
    assert (value, sources) == original


@pytest.mark.parametrize("fault", ["missing", "renumber", "foreign_span", "refusal", "truncation",
                                   "negative_then_malformed", "scope_spill"])
def test_failed_batch_joins_and_accounts_for_sibling_without_starting_later_work(monkeypatch, fault):
    value, sources, settings, client = fixture_draft()
    sibling_entered, release_sibling = Event(), Event()
    entered, remembered = [], []

    def complete(_catalog, _settings, _client, _book, **request):
        data = request_data(request)
        ids = [c["claim_id"] for c in data["claims"]]
        entered.append(ids)
        audit = _supported_citation_audit(data, [1])
        if ids == [1, 2]:
            assert sibling_entered.wait(5)
            if fault == "refusal":
                raise ValueError("private provider refusal")
            if fault == "truncation":
                raise LengthFinishReasonError(completion=SimpleNamespace(usage=None))
            if fault == "missing":
                audit["claim_audits"].pop()
            elif fault == "renumber":
                audit["claim_audits"][0]["claim_id"] = 3
            elif fault == "foreign_span":
                audit["claim_audits"][0]["units"][0]["evidence"][0]["passage_ids"] = [999]
            elif fault == "negative_then_malformed":
                audit["claim_audits"][0]["units"][0].update(supported=False, evidence=[])
            elif fault == "scope_spill":
                # Flattened IDs would still be 1,2,3,4; each exact requested scope must fail.
                context = {**data, "claims": [data["answer_context"][2]]}
                audit["claim_audits"].extend(_supported_citation_audit(context, [1])["claim_audits"])
        else:
            assert ids == [3, 4]
            sibling_entered.set()
            assert release_sibling.wait(5)
            if fault == "negative_then_malformed":
                audit["claim_audits"].pop()
            elif fault == "scope_spill":
                audit["claim_audits"].pop(0)
        return SimpleNamespace(value=audit, requested=ids)

    monkeypatch.setattr(views, "budgeted_completion", complete)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(run_review, value, sources, settings, client, remembered)
        try:
            assert sibling_entered.wait(5)
            assert not future.done()
        finally:
            release_sibling.set()
        with pytest.raises(AskEvidenceError) as caught:
            future.result(timeout=5)
    assert not caught.value.repairable and sorted(entered) == [[1, 2], [3, 4]]
    assert [r.requested for r in remembered] == (
        [[3, 4]] if fault in {"refusal", "truncation"} else [[1, 2], [3, 4]])


def test_batched_audits_use_unchanged_output_limit_and_settle_each_existing_budget_reservation(env, monkeypatch):
    from app.cost import limits

    _api, bid, app, _atoms = env
    value, sources, _settings, _client = fixture_draft()
    settings, client = app.state.settings, app.state.client
    settings.jev_evidence_enabled = False
    client.provider = "openai-compatible"
    client.cheap_model = "gpt-6-luna"
    settings_before = settings.model_dump()
    caps, remembered = [], []
    initial_costs = len(app.state.catalog.get_costs(bid))

    def complete(_client, _system, user, *, tier, schema, max_output_tokens):
        caps.append(max_output_tokens)
        data = request_data({"user": user})
        return _supported_citation_audit(data, [1]), {"in": 100, "out": 80}

    monkeypatch.setattr(limits, "_production_complete", complete)
    result = run_review(value, sources, settings, client, remembered, app.state.catalog, bid)
    assert result.model_dump(mode="json") == value
    assert caps == [4096] * 3 and settings.model_dump() == settings_before
    ledger = app.state.catalog.get_costs(bid)[initial_costs:]
    assert len(ledger) == len(remembered) == 3
    assert all(row["phase"] == "citation-review" and row["output_tokens"] == 80 for row in ledger)
    assert app.state.catalog.get_cost_reservations(bid) == []


def test_batching_cannot_bypass_existing_per_book_budget(env, monkeypatch):
    _api, bid, app, _atoms = env
    value, sources, _settings, _client = fixture_draft()
    settings, client = app.state.settings, app.state.client
    settings.jev_evidence_enabled = False
    client.provider = "openai-compatible"
    client.cheap_model = "gpt-6-luna"
    settings.cost_max_output_tokens_per_book = 1
    called = []
    monkeypatch.setattr(client, "complete", lambda *_args, **_kwargs: called.append(True))
    with pytest.raises(CostCeilingExceeded):
        run_review(value, sources, settings, client, [], app.state.catalog, bid)
    assert not called and settings.cost_max_output_tokens_per_book == 1
    assert app.state.catalog.get_cost_reservations(bid) == []
