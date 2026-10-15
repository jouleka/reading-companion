"""Offline evidence bookkeeping only; canned choices are not provider-quality evidence."""
import copy

import pytest

from app.api import views
from app.ask_selection import SourceSelection, SourceSelectionError
from app.catalog.catalog import Catalog
from app.cost import budgeted_completion
from scripts import live_book_audit as audit
from scripts import live_quality_audit as quality
from tests.scripts.test_audit_reporting import _settings


SOURCES = [{"id": index, "ordinal": 1, "chapter_key": "c1", "href": "c1.xhtml",
            "title": "The interview", "text": f"Passage {index}: Rowan spoke to Mara."}
           for index in range(1, 25)]
ATOMS = {"c1": {"ordinal": 1, "href": "c1.xhtml", "title": "The interview"}}


def _citation(source):
    return {**{key: source[key] for key in ("id", "ordinal", "chapter_key", "href", "title")},
            "excerpt": source["text"]}


def test_capture_observes_both_view_symbols_and_compatibility_alias_means_selected(monkeypatch):
    # Compatibility with the earlier selector-only route remains explicit, rather than depending on
    # whether the current application imports its newer context-completion symbol.
    monkeypatch.delattr(views, "complete_ask_context", raising=False)
    candidates = copy.deepcopy(SOURCES)
    selected = [candidates[16], candidates[1]]
    calls = []

    def retrieve(*args, **kwargs):
        calls.append(("retrieve", args, kwargs))
        return candidates

    def validate(*args, **kwargs):
        calls.append(("validate", args, kwargs))
        return selected

    monkeypatch.setattr(views, "retrieve_ask_sources", retrieve)
    monkeypatch.setattr(views, "validate_selection", validate)
    with audit.capture_ask_sources() as captured:
        assert views.retrieve_ask_sources("Question?", bookmark=1, candidate_pool=True) is candidates
        assert views.validate_selection({"source_ids": [17, 2]}, candidates) is selected
        evidence = captured.snapshot()
        assert evidence["candidate_sources"] == SOURCES
        assert [source["id"] for source in evidence["selected_sources"]] == [17, 2]
        assert evidence["retrieved_sources"] == evidence["selected_sources"]
        assert evidence["selected_capture_method"] == "validated_selection"
        assert evidence["candidate_sources_captured"] and evidence["selected_sources_captured"]
        assert audit.selected_subset_check(evidence["candidate_sources"], evidence["selected_sources"])
        # App return values remain untouched; later mutations cannot rewrite the recorded evidence.
        candidates[0]["text"] = "later mutation"
        assert evidence["candidate_sources"] == SOURCES
        captured.reset()
        missing = captured.snapshot()
        assert missing["candidate_sources"] is missing["selected_sources"] is missing["retrieved_sources"] is None
        assert not missing["candidate_sources_captured"] and not missing["selected_sources_captured"]
    assert [kind for kind, _args, _kwargs in calls] == ["retrieve", "validate"]


def test_empty_selected_set_is_distinct_from_no_selection_capture(monkeypatch):
    monkeypatch.delattr(views, "complete_ask_context", raising=False)
    monkeypatch.setattr(views, "retrieve_ask_sources", lambda *args, **kwargs: SOURCES)
    monkeypatch.setattr(views, "validate_selection", lambda *args, **kwargs: [])
    with audit.capture_ask_sources() as captured:
        views.retrieve_ask_sources("Question?", bookmark=1, candidate_pool=True)
        before = captured.snapshot()
        assert before["candidate_sources"] and before["selected_sources"] is None
        assert not before["selected_sources_captured"]
        views.validate_selection({"source_ids": []}, SOURCES)
        after = captured.snapshot()
        assert after["selected_sources"] == after["retrieved_sources"] == []
        assert after["selected_sources_captured"] and after["selected_capture_method"] == "validated_selection"
        assert audit.source_pool_checks([], effective=1, atoms=ATOMS, read_raw=lambda key: "") == {
            "boundary_and_normalized_excerpt": None, "check_count": 0,
        }
        assert audit.source_pool_checks(None, effective=1, atoms=ATOMS, read_raw=lambda key: "") == {
            "boundary_and_normalized_excerpt": None, "check_count": None,
        }


def test_rejected_selection_is_not_reported_as_selected_or_as_empty_success(monkeypatch):
    error = SourceSelectionError("private-canary model output must stay private")
    monkeypatch.setattr(views, "retrieve_ask_sources", lambda *args, **kwargs: SOURCES)

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(views, "validate_selection", reject)
    with audit.capture_ask_sources() as captured:
        views.retrieve_ask_sources("Question?", bookmark=1, candidate_pool=True)
        with pytest.raises(SourceSelectionError) as caught:
            views.validate_selection({"source_ids": [24]}, SOURCES)
        assert caught.value is error
        evidence = captured.snapshot()
        assert evidence["candidate_sources"] and evidence["selected_sources"] is None
        assert evidence["selected_capture_method"] == "not_captured"
        assert evidence["selection_failure"]["type"] == "SourceSelectionError"
        assert "private-canary" not in str(evidence["selection_failure"])


@pytest.mark.parametrize("candidates,pool,method", [(SOURCES[:3], False, "direct_retrieval"),
                                                   ([], True, "empty_candidates")])
def test_direct_or_empty_retrieval_has_explicit_final_evidence_without_selector_call(monkeypatch,
                                                                                    candidates, pool, method):
    monkeypatch.setattr(views, "retrieve_ask_sources", lambda *args, **kwargs: candidates)
    with audit.capture_ask_sources() as captured:
        views.retrieve_ask_sources("Question?", bookmark=1, candidate_pool=pool)
        evidence = captured.snapshot()
        assert evidence["candidate_sources"] == evidence["selected_sources"] == candidates
        assert evidence["retrieved_sources"] == candidates
        assert evidence["selected_sources_captured"] and evidence["selected_capture_method"] == method


def test_checks_validate_all_candidates_and_selected_text_metadata_against_same_frontier():
    loaded = []

    def read_raw(key):
        loaded.append(key)
        return "\n  ".join(source["text"] for source in SOURCES)

    selected = [SOURCES[16], SOURCES[1]]
    candidate_checks = audit.source_pool_checks(SOURCES, effective=1, atoms=ATOMS, read_raw=read_raw)
    selected_checks = audit.source_pool_checks(selected, effective=1, atoms=ATOMS, read_raw=read_raw)
    assert candidate_checks == {"boundary_and_normalized_excerpt": True, "check_count": 24}
    assert selected_checks == {"boundary_and_normalized_excerpt": True, "check_count": 2}
    assert len(loaded) == 26
    bad = [dict(SOURCES[0], ordinal=2, chapter_key="future")]
    assert audit.source_pool_checks(bad, effective=1, atoms=ATOMS, read_raw=read_raw) == {
        "boundary_and_normalized_excerpt": False, "check_count": 1,
    }
    assert len(loaded) == 26, "future or unmapped source must not load raw text"
    wrong_metadata = [dict(SOURCES[0], title="another chapter")]
    assert audit.source_pool_checks(wrong_metadata, effective=1, atoms=ATOMS,
                                    read_raw=read_raw)["boundary_and_normalized_excerpt"] is False


def test_citations_can_only_match_selected_ids_even_when_unselected_candidate_is_real():
    selected = [SOURCES[16], SOURCES[1]]
    assert audit.citation_identity_check([_citation(SOURCES[16])], selected) is True
    assert audit.citation_identity_check([_citation(SOURCES[0])], selected) is False
    assert audit.citation_identity_check([_citation(SOURCES[0])], []) is False
    assert audit.citation_identity_check([_citation(SOURCES[0])], None) is False
    assert audit.citation_identity_check([], selected) is None
    assert audit.citation_identity_check([], None) is None


@pytest.mark.parametrize("field", ["excerpt", "ordinal", "chapter_key", "href", "title"])
def test_citation_identity_requires_every_exact_selected_field(field):
    citation = _citation(SOURCES[16])
    citation[field] = "incorrect value"
    assert audit.citation_identity_check([citation], [SOURCES[16]]) is False


def test_selected_subset_requires_original_ids_and_unchanged_evidence_metadata():
    assert audit.selected_subset_check(SOURCES, [SOURCES[16], SOURCES[1]]) is True
    assert audit.selected_subset_check(SOURCES, []) is True
    assert audit.selected_subset_check(SOURCES, None) is None
    assert audit.selected_subset_check(None, []) is None
    assert audit.selected_subset_check(SOURCES, [dict(SOURCES[16], id=1)]) is False
    assert audit.selected_subset_check(SOURCES, [dict(SOURCES[16], text="invented excerpt")]) is False


def test_source_selection_attempt_keeps_real_usage_trace_and_actual_ledger_phase(tmp_path):
    from types import SimpleNamespace

    seen = []

    def complete(system, user, tier, schema, cap):
        seen.append((tier, schema, cap))
        return {"source_ids": [17]}, {"in": 31, "out": 6}

    # Pure offline provider double, with no SDK construction, environment read, or network method.
    provider = SimpleNamespace(provider="openai-compatible", _is_native_openai=True, complete=complete,
                               embed=lambda texts: ([], {"in": 0}),
                               _model_for=lambda tier: "gpt-6-luna", embed_identity=lambda: "stub:test")
    report = {}
    quality.install_call_recording(provider, report, 2048)
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Offline tracing")
    try:
        result = budgeted_completion(catalog, _settings(tmp_path), provider, "b",
                                     phase="source-selection", system="Source only.", user="Evidence.",
                                     tier="cheap", schema=SourceSelection)
        assert result.value == {"source_ids": [17]}
        assert seen == [("cheap", SourceSelection, 2048)]
        [record] = report["model_calls"]
        assert record["schema"] == "SourceSelection" and record["status"] == "succeeded"
        assert record["usage"] == {"in": 31, "out": 6}
        [entry] = catalog.get_costs("b")
        assert entry["phase"] == "source-selection" and entry["model"] == "gpt-6-luna"
        assert entry["input_tokens"] == 31 and entry["output_tokens"] == 6
        assert not catalog.get_cost_reservations("b")
    finally:
        catalog.close()


def test_final_context_expanded_evidence_is_distinct_from_model_selection(monkeypatch):
    from app.ask_context import complete_ask_context
    from app.ask_selection import validate_selection

    pool = copy.deepcopy(SOURCES)
    pool[16]["context_ids"] = [3]
    monkeypatch.setattr(views, "retrieve_ask_sources", lambda *args, **kwargs: pool)
    monkeypatch.setattr(views, "validate_selection", validate_selection)
    monkeypatch.setattr(views, "complete_ask_context", complete_ask_context, raising=False)
    with audit.capture_ask_sources() as captured:
        candidates = views.retrieve_ask_sources("What did Rowan say?", bookmark=1, candidate_pool=True)
        model = views.validate_selection({"source_ids": [17, 2]}, candidates)
        before = captured.snapshot()
        assert [source["id"] for source in before["model_selected_sources"]] == [17, 2]
        assert before["selected_sources"] is before["retrieved_sources"] is None
        assert not before["selected_sources_captured"]
        final = views.complete_ask_context("What did Rowan say?", model, candidates, ["Rowan"])
        evidence = captured.snapshot()
    assert [source["id"] for source in final] == [17, 2, 3]
    assert evidence["selected_sources"] == evidence["retrieved_sources"] == final
    assert [source["id"] for source in evidence["model_selected_sources"]] == [17, 2]
    assert evidence["context_completion_captured"] and evidence["selected_capture_method"] == "context_completed"
    assert audit.selected_subset_check(evidence["candidate_sources"], evidence["selected_sources"]) is True
    assert audit.citation_identity_check([_citation(pool[2])], evidence["selected_sources"]) is True
    assert audit.citation_identity_check([_citation(pool[2])], evidence["model_selected_sources"]) is False


def test_context_failure_retains_model_choices_but_never_claims_final_evidence_capture(monkeypatch):
    from app.ask_context import AskContextError, complete_ask_context
    from app.ask_selection import validate_selection

    pool = [dict(SOURCES[0], context_ids=[24])]
    monkeypatch.setattr(views, "retrieve_ask_sources", lambda *args, **kwargs: pool)
    monkeypatch.setattr(views, "validate_selection", validate_selection)
    monkeypatch.setattr(views, "complete_ask_context", complete_ask_context, raising=False)
    with audit.capture_ask_sources() as captured:
        candidates = views.retrieve_ask_sources("What did Rowan say?", bookmark=1, candidate_pool=True)
        model = views.validate_selection({"source_ids": [1]}, candidates)
        with pytest.raises(AskContextError):
            views.complete_ask_context("What did Rowan say?", model, candidates, ["Rowan"])
        evidence = captured.snapshot()
    assert evidence["model_selected_sources"] == model
    assert evidence["selected_sources"] is evidence["retrieved_sources"] is None
    assert not evidence["context_completion_captured"] and not evidence["selected_sources_captured"]
    assert evidence["context_failure"]["type"] == "AskContextError"
