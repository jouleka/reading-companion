from types import SimpleNamespace
import json

import pytest

from app.catalog.catalog import Catalog
from app.cost import (
    budgeted_completion,
    budgeted_embedding,
    estimate_input_tokens,
    pricing_known,
    split_text_for_prompt,
)
from app.cost.__main__ import main as cost_main
from app.ingest.extraction.prompts import EXTRACT_SYSTEM, extract_user_prompt
from app.ingest.extraction.schema import Extraction
from app.ingest.worker import IngestWorker
from app.llm.client import LLMClient


def _catalog(tmp_path):
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="Book")
    return catalog


def _settings(**overrides):
    values = {
        "cost_max_input_tokens_per_call": 2_000,
        "cost_max_output_tokens_per_call": 200,
        "cost_max_input_tokens_per_book": 20_000,
        "cost_max_output_tokens_per_book": 2_000,
        "cost_max_usd_per_book": 5.0,
        "segmentation_cache_max_entries": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeClient:
    def __init__(self):
        self.calls = 0

    def _model_for(self, _tier):
        return "gpt-4o-mini"

    def complete(self, _system, _user, tier="cheap", schema=None):
        self.calls += 1
        return "ok", {"in": 11, "out": 3}

    def embed_identity(self):
        return "openai-compatible@https://api.openai.com/v1:text-embedding-3-small"

    def embed(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts], {"in": 9}


def test_pricing_known_never_presents_an_unknown_model_as_free():
    assert pricing_known("openai-compatible:gpt-4o-mini")
    assert pricing_known("stub:lexical-stub-256")
    assert pricing_known("stub-large")
    assert not pricing_known("vendor:future-model")


def test_openai_54_prices_distinguish_mini_and_large_for_budget_reservations():
    from app.cost.limits import usd_of

    usage = {"in": 1_000_000, "out": 1_000_000}
    assert pricing_known("openai-compatible:gpt-5.4-mini")
    assert usd_of("gpt-5.4-mini", usage) == pytest.approx(5.25)
    assert usd_of("gpt-5.4", usage) == pytest.approx(17.50)


def test_budgeted_completion_settles_actual_usage(tmp_path):
    catalog, client = _catalog(tmp_path), FakeClient()
    result = budgeted_completion(
        catalog, _settings(), client, "b", phase="synthesis", system="system", user="facts",
        tier="large",
    )
    assert result.value == "ok" and result.usage == {"in": 11, "out": 3}
    assert client.calls == 1 and catalog.get_cost_reservations("b") == []
    [row] = catalog.get_costs("b")
    assert row["phase"] == "synthesis" and row["input_tokens"] == 11 and row["output_tokens"] == 3


def test_active_reservations_make_ceiling_check_atomic(tmp_path):
    catalog = _catalog(tmp_path)
    first = catalog.reserve_cost(
        "b", phase="synthesis", model="m", input_tokens=6, output_tokens=1, usd=0,
        max_input_tokens=10, max_output_tokens=10, max_usd=1,
    )
    with pytest.raises(RuntimeError, match="input-token cost ceiling"):
        catalog.reserve_cost(
            "b", phase="judge", model="m", input_tokens=5, output_tokens=1, usd=0,
            max_input_tokens=10, max_output_tokens=10, max_usd=1,
        )
    assert [row["reservation_id"] for row in catalog.get_cost_reservations("b")] == [first]


def test_embedding_usage_uses_the_same_pre_call_reservation_and_ledger(tmp_path):
    catalog, client = _catalog(tmp_path), FakeClient()
    result = budgeted_embedding(
        catalog, _settings(), client, "b", phase="search-embedding", texts=["query"]
    )
    assert result.value == [[1.0, 0.0]] and result.usage == {"in": 9, "out": 0}
    assert catalog.get_cost_reservations("b") == []
    [row] = catalog.get_costs("b")
    assert row["phase"] == "search-embedding" and row["input_tokens"] == 9 and row["usd"] > 0


def test_huge_chapter_is_split_without_dropping_text_and_keeps_one_receipt_cost(tmp_path):
    catalog = _catalog(tmp_path)
    client = LLMClient(provider="stub", allow_stub=True, env={})
    roster = []
    overhead = estimate_input_tokens(
        EXTRACT_SYSTEM, extract_user_prompt("Huge", roster, ""), Extraction
    )
    settings = _settings(
        cost_max_input_tokens_per_call=overhead + 300,
        cost_max_input_tokens_per_book=100_000,
        cost_max_output_tokens_per_book=100_000,
    )
    worker = IngestWorker(None, catalog, client, settings, executor=SimpleNamespace())
    original = client.complete
    prompts = []

    def capture(system, user, tier="cheap", schema=None):
        prompts.append((schema, user))
        return original(system, user, tier=tier, schema=schema)

    client.complete = capture
    text = ("Aldric crossed the long valley and met Berenice.\n\n" * 50)
    extraction, usage, _usd, reservations, clearance = worker._complete_extraction(
        "b", 1, "Huge", roster, text
    )
    sent = [prompt.split("CHAPTER TEXT:\n", 1)[1].split("\n\nExtract entities", 1)[0]
            for schema, prompt in prompts if schema is Extraction]
    assert len(sent) > 1 and "".join(sent) == text
    assert extraction["chapter_summary"] and usage["in"] > 0
    assert len(reservations) == len(sent) * 2            # extraction + review, one receipt downstream
    clearance.assert_matches(text, extraction)
    reviewed_sources = [json.loads(prompt.split("(untrusted JSON data):\n", 1)[1])[
        "current_chapter_text"] for schema, prompt in prompts if schema is not Extraction]
    assert reviewed_sources == sent
    assert len(catalog.get_costs("b")) == 0                 # successful parts await the LIT-7 receipt
    for reservation_id in reservations:
        catalog.settle_cost("b", reservation_id, phase="test-cleanup")


def test_splitter_rejects_prompt_metadata_that_already_exceeds_the_call_limit():
    with pytest.raises(RuntimeError, match="metadata alone"):
        split_text_for_prompt(
            "chapter", prompt_without_text="x" * 100, system="system", max_input_tokens=20
        )


def test_operator_reconcile_converts_crash_reservation_to_conservative_ledger_entry(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    catalog = Catalog(str(data_dir / "catalog.db"))
    catalog.add_book("b", title="Book")
    catalog.reserve_cost(
        "b", phase="synthesis", model="m", input_tokens=12, output_tokens=4, usd=0.1,
        max_input_tokens=100, max_output_tokens=100, max_usd=1,
    )
    catalog.close()

    cost_main(["status", "--data-dir", str(data_dir), "--book-id", "b"])
    assert json.loads(capsys.readouterr().out)["outstanding"] == 1
    cost_main(["reconcile", "--data-dir", str(data_dir), "--book-id", "b"])
    assert json.loads(capsys.readouterr().out)["reconciled"] == 1

    reopened = Catalog(str(data_dir / "catalog.db"))
    assert reopened.get_cost_reservations("b") == []
    [row] = reopened.get_costs("b")
    assert row["phase"] == "synthesis-reconciled-reserved" and row["usd"] == 0.1
