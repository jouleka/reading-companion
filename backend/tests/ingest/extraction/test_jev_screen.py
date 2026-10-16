"""The fast screen can request repair, but only the independent review can clear memory."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.cost import CostCeilingExceeded
from app.ingest.extraction.validation import (
    ExtractionGroundingRejected, extraction_review_prompt, review_extraction,
)
from app.llm import jev_tasks
from app.llm.jev import JevUnavailable
from tests.ingest._grounding import canned_grounding_verdict, reviewed_source
from tests.ingest.test_grounding_repair import (
    runtime as runtime, _advance, _assert_no_pending, _assert_uncommitted, _costs, _extraction, _memory,
)
from tests.test_jev import answer


def data(value=None, roster=None):
    return json.loads(extraction_review_prompt(
        "Nell gave Orin a key. Nell stayed at the inn.", value or {
            "chapter_summary": "Nell gave Orin a key; she stayed at the inn.",
            "entities": [], "relationships": [], "events": [], "themes": []}, roster or [],
    ).split("(untrusted JSON data):\n", 1)[1])


@pytest.mark.parametrize("mode", ["supported", "uncertain", "whole_only", "segment_only", "low_p", "low_conf", "reject"])
def test_only_double_high_confidence_negatives_can_request_repair(mode):
    payload = data()
    before = deepcopy(payload)
    def run(state, questions):
        assert "".join(p["text"] for p in state["source_passages"]) == "Nell gave Orin a key. Nell stayed at the inn."
        assert state["fields"][0] == payload["fields"][0]
        assert "state.fields[0]" in questions["f0_whole"]["instructions"]
        values = {k: answer(q["criteria"], choice="supported") for k, q in questions.items()}
        if mode in {"whole_only", "low_p", "low_conf", "reject", "uncertain"}:
            values["f0_whole"] = answer(questions["f0_whole"]["criteria"],
                choice="uncertain" if mode == "uncertain" else "unsupported",
                p=.98 if mode == "low_p" else .999, confidence=.89 if mode == "low_conf" else .98)
        if mode in {"segment_only", "low_p", "low_conf", "reject", "uncertain"}:
            values["f0_1"] = answer(questions["f0_1"]["criteria"], choice="unsupported", p=.999)
        return values
    issues = jev_tasks.extraction_issues(payload, run=run)
    assert bool(issues) == (mode == "reject")
    assert payload == before


@pytest.mark.parametrize("fault", ["missing", "foreign"])
def test_invalid_screen_coverage_is_unavailable_not_implicit_clearance(fault):
    def run(state, questions):
        values = {k: answer(q["criteria"], choice="unsupported", p=.999) for k, q in questions.items()}
        if fault == "missing":
            values.pop(next(iter(values)))
        else:
            values["foreign"] = next(iter(values.values()))
        return values
    with pytest.raises(JevUnavailable):
        jev_tasks.extraction_issues(data(), run=run)


def test_field_budget_keeps_full_context_and_prioritizes_risky_theme_without_truncation():
    value = _extraction()
    value["chapter_summary"] = "Nell waited. " * 40  # Too many segments to screen as one field.
    value["themes"] = [{"name": "Secrecy", "description": "Orin secretly hid the key."}]
    payload = data(value, [{"entity_id": 7, "canonical_name": "Nell", "type": "character",
                            "aliases": ["Rook"], "private_future_state": "must not travel"}])
    def run(state, questions):
        assert len(questions) <= 24
        assert all(f["path"] != "/chapter_summary" for f in state["fields"])
        assert state["fields"][0]["kind"] == "theme"
        assert "private_future_state" not in json.dumps(state) and "entity_id" not in json.dumps(state)
        assert state["prior_visible_identities"] == [{"canonical_name": "Nell", "type": "character", "aliases": ["Rook"]}]
        assert all("never actions" in q["instructions"] for q in questions.values())
        return {k: answer(q["criteria"], choice="supported") for k, q in questions.items()}
    assert jev_tasks.extraction_issues(payload, run=run) == []


def test_byte_budget_packs_complete_fields_and_skips_without_io_when_source_alone_is_too_large():
    value = _extraction()
    value["entities"] *= 12
    payload = data(value)
    requests = []
    def run(state, questions):
        estimated = len(json.dumps({"model": "jev-1.13.0", "state": state, "questions": questions},
                                  ensure_ascii=False).encode("utf-8"))+256
        assert estimated <= 15000 and len(questions) <= 24
        assert state["source_passages"] == payload["source_passages"]
        assert all(f in payload["fields"] for f in state["fields"])
        requests.append(state)
        return {k: answer(q["criteria"], choice="supported") for k, q in questions.items()}
    assert jev_tasks.extraction_issues(payload, run=run, input_limit=15000) == []
    assert requests and len(requests[0]["fields"]) < len(payload["fields"])
    payload["source_passages"][0]["text"] = "源" * 12000
    assert jev_tasks.extraction_issues(payload, run=run) == []
    assert len(requests) == 1


def test_screen_cannot_modify_the_reviewed_extraction_or_skip_independent_review():
    value = {"chapter_summary": "Nell gave Orin a key.", "entities": [],
             "relationships": [], "events": [], "themes": []}
    calls = []
    def screen(payload):
        payload["extraction"]["chapter_summary"] = "Injected story."
        return "cleared"  # A screen's return value has no authority.
    def complete(system, user, **kwargs):
        calls.append(user)
        assert "Injected story" not in user
        return canned_grounding_verdict(user), {"in": 10, "out": 10}
    clearance = review_extraction("Nell gave Orin a key.", value, [], complete=complete, screen=screen)
    clearance.assert_matches("Nell gave Orin a key.", value)
    assert len(calls) == 1


def test_deterministic_absent_name_check_runs_before_any_paid_screen_or_review():
    calls = []
    with pytest.raises(ExtractionGroundingRejected, match="unobserved"):
        review_extraction("Nell waited.", _extraction(), [],
                          complete=lambda *a, **k: calls.append("review"),
                          screen=lambda *_: calls.append("screen"))
    assert calls == []


@pytest.mark.parametrize("mode", ["pass", "uncertain", "outage", "coverage", "disabled",
                                    "reject_once", "reject_always", "gpt_rejects", "budget", "shutdown"])
def test_worker_screen_preserves_atomicity_full_review_frontier_and_separate_chapter_costs(runtime, monkeypatch, mode):
    baseline = _memory(runtime)
    runtime.settings.jev_extraction_enabled = mode != "disabled"
    generation_calls, review_calls, screen_calls = [], [], []
    def evaluate(state, questions):
        _assert_uncommitted(runtime, baseline)
        assert "Corvus" not in json.dumps(state) and "future verdict" not in json.dumps(state)
        assert "".join(p["text"] for p in state["source_passages"]) == runtime.chapter["text"]
        screen_calls.append(state)
        if mode == "outage":
            raise JevUnavailable("private-provider-detail")
        reject = mode == "reject_always" or (mode == "reject_once" and len(screen_calls) == 1)
        if mode == "shutdown":
            runtime.worker._stopping.set()
        values = {key: answer(q["criteria"], choice="unsupported" if reject else "uncertain" if mode == "uncertain" else "supported", p=.999)
                  for key, q in questions.items()}
        if mode == "coverage":
            values.pop(next(iter(values)))
        return values, {"in": 100, "out": 40}
    runtime.client.jev = SimpleNamespace(model="jev-1.13.0", evaluate=evaluate, close=lambda: None)
    def complete(system, user, *, tier, schema):
        _assert_uncommitted(runtime, baseline)
        assert "Corvus" not in system+user
        if schema.__name__ == "Extraction":
            generation_calls.append(user)
            return _extraction(rejected=mode in {"reject_always", "gpt_rejects"}
                               or (mode == "reject_once" and len(generation_calls) == 1)), {"in": 11, "out": 3}
        assert schema.__name__ == "ExtractionVerdict"
        review_calls.append(user)
        assert reviewed_source(user) == runtime.chapter["text"]
        return canned_grounding_verdict(user, unsupported_claims=["Swapped direction"]
                                        if mode == "gpt_rejects" else []), {"in": 13, "out": 5}
    monkeypatch.setattr(runtime.client, "complete", complete)
    if mode == "budget":
        def exhausted(*args, **kwargs):
            raise CostCeilingExceeded("screen book budget exhausted")
        monkeypatch.setattr("app.ingest.worker.budgeted_jev", exhausted)
    result = _advance(runtime)
    failed = mode in {"reject_always", "gpt_rejects", "budget", "shutdown"}
    if failed:
        assert runtime.catalog.get_state(runtime.book_id)["ingest_progress"] == 1
        assert _memory(runtime) == baseline
        assert result["status"] != "done"
    else:
        assert result["status"] == "done"
        assert runtime.catalog.get_state(runtime.book_id)["ingest_progress"] == 2
        assert len(review_calls) == 1  # Every accepted chapter still has a complete review.
    if mode in {"reject_always", "budget", "shutdown"}:
        assert not review_calls
    assert len(generation_calls) == (2 if mode in {"reject_once", "reject_always", "gpt_rejects"} else 1)
    if mode == "disabled":
        assert screen_calls == []
    if mode == "reject_once":
        assert len(screen_calls) == 2 and len(review_calls) == 1
        assert "untrusted repair criticism" in generation_calls[1].lower()
        assert "Recheck /chapter_summary" in generation_calls[1]
    screen_costs = [r for r in _costs(runtime) if r["phase"].startswith("jev-extraction-screen")]
    if screen_calls:
        assert len(screen_costs) == len(screen_calls)
        assert all(r["chapter_ordinal"] == 2 and r["model"] == "jev-1.13.0" and r["usd"] > 0 for r in screen_costs)
    _assert_no_pending(runtime)
