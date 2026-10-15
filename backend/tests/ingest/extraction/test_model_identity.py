"""The database commit is a second model-space fence even for already-prepared chapters."""
import pytest

from app.ingest.extraction.pipeline import ingest_chapter, prepare_chapter
from app.llm.client import LLMClient
from app.llm.versioning import ModelMigrationRequired
from app.memory.store import Store


@pytest.mark.parametrize("change", [
    {"extractor_model": "other-extractor"},
    {"embed_model": "other-same-dimension-space"},
    {"embed_canary": [1.0, 0.0]},
])
def test_incompatible_prepared_identity_cannot_publish_a_receipt(tmp_path, change):
    client = LLMClient(provider="stub", allow_stub=True, env={})
    store = Store(data_dir=str(tmp_path))
    extraction = {"chapter_summary": "Aldric arrived.", "entities": [],
                  "relationships": [], "events": [], "themes": []}
    first = {"key": "b:c1.xhtml", "ordinal": 1, "href": "c1.xhtml", "text": "Aldric arrived."}
    second = {"key": "b:c2.xhtml", "ordinal": 2, "href": "c2.xhtml", "text": "Aldric returned."}
    first_prepared = prepare_chapter(first, extraction, client, roster=[])
    with store.book("b") as memory:
        ingest_chapter(memory, first, first_prepared)
        original_pin = memory.pinned_identity()
    second_prepared = prepare_chapter(second, extraction, client, roster=[])
    second_prepared["identity"] = {**second_prepared["identity"], **change}
    with store.book("b") as memory:
        with pytest.raises(ModelMigrationRequired):
            ingest_chapter(memory, second, second_prepared)
        assert len(memory._audit_all("chapters")) == 1
        assert len(memory._audit_all("chunks")) == 1
        assert len(memory._audit_all("ingested_chapters")) == 1
        assert memory.pinned_identity() == original_pin
    store.close()
