"""Independent temporal correction and preparation-boundary audit; no provider I/O."""

import pytest

from app.eval.spoiler_gate.cache import validity_snapshot
from app.ingest.extraction.pipeline import (
    StalePreparedRoster, all_entities, ingest_chapter, prepare_chapter,
)
from app.llm.client import LLMClient
from app.memory.store import Store


def _chapter(number):
    return {
        "ordinal": number,
        "key": f"audit:c{number}.xhtml",
        "href": f"c{number}.xhtml",
        "title": f"Chapter {number}",
        "text": (f"In chapter {number}, Aldric and Berenice are in the valley. "
                 + ("Aldric works at the forge alongside Berenice." if number == 3 else "")),
    }


def _extraction(number):
    def entity(name, state=None):
        return {
            "canonical_name": name, "type": "character", "aliases": [],
            "matched_roster": number > 1, "state": state,
        }

    return {
        "chapter_summary": f"Synthetic chapter {number} summary.",
        "entities": [entity("Aldric", "at the forge" if number == 3 else "in the valley"),
                     entity("Berenice")],
        "relationships": ([{"src": "Aldric", "dst": "Berenice", "rel_type": "social",
                            "label": "works alongside"}] if number == 3 else []),
        "events": ([{"summary": "Aldric works at the forge.", "participants": ["Aldric"]}]
                   if number == 3 else []),
        "themes": [],
    }


def _prepare(store, client, number):
    with store.book("audit", meta={"title": "Synthetic audit"}) as mem:
        roster = all_entities(mem.view(number - 1))
    return prepare_chapter(_chapter(number), _extraction(number), client, roster=roster)


def _seed(tmp_path, count):
    store = Store(str(tmp_path))
    client = LLMClient(provider="stub", embed_provider="stub", allow_stub=True, env={})
    for number in range(1, count + 1):
        prepared = _prepare(store, client, number)
        with store.book("audit") as mem:
            ingest_chapter(mem, _chapter(number), prepared)
    return store, client


def _correct(mem):
    source = next(row for row in mem.view(2).characters() if row["canonical_name"] == "Aldric")
    return mem.replace_entity(
        source["entity_id"], effective_at=2, canonical_name="Aldric Vale",
        reason="The reader corrects the displayed full name.",
    )["target_entity_id"]


def test_correction_between_prepare_and_commit_keeps_new_facts_visible(tmp_path):
    """Provider work happens outside the lock; its roster can be retired before commit."""
    store, client = _seed(tmp_path, 2)
    prepared = _prepare(store, client, 3)
    with store.book("audit") as mem:
        target = _correct(mem)
        with pytest.raises(StalePreparedRoster):
            ingest_chapter(mem, _chapter(3), prepared)
        assert len(mem.view(3).chapters()) == 2
    prepared = _prepare(store, client, 3)
    with store.book("audit") as mem:
        result = ingest_chapter(mem, _chapter(3), prepared)
        assert result["skipped"] is False
        event = mem.view(3).timeline()[0]
        assert [row["entity_id"] for row in mem.view(3).participants_of(event["event_id"])] == [target]
        assert mem.view(3).bio(target)["state"] == {"note": "at the forge"}


def test_reread_name_correction_preserves_already_ingested_later_participation(tmp_path):
    """Reset retains future receipts: a name-only correction must retain later event identity."""
    store, _client = _seed(tmp_path, 3)
    with store.book("audit") as mem:
        target = _correct(mem)
        event = mem.view(3).timeline()[0]
        assert [row["entity_id"] for row in mem.view(3).participants_of(event["event_id"])] == [target]
        assert mem.view(3).bio(target)["state"] == {"note": "at the forge"}
        assert len(mem.view(3).relationships()) == 1


def test_retained_dependency_projection_preserves_every_reveal_and_validity_boundary(tmp_path):
    store, _client = _seed(tmp_path, 3)
    with store.book("audit") as mem:
        source = next(row["entity_id"] for row in mem.view(1).characters()
                      if row["canonical_name"] == "Aldric")
        mem.add_alias(source, "The Master Smith", 4)
        state = mem.view(3).current_state(source)
        with mem._writer():
            mem._conn.execute("UPDATE entity_state SET invalid_at=4 WHERE state_id=?",
                              (state["state_id"],))
        before = validity_snapshot(mem, 1)
        receipts = [dict(row) for row in mem._audit_all("ingested_chapters")]
        target = _correct(mem)
        assert validity_snapshot(mem, 1) == before
        assert mem.view(1).bio(target) is None
        assert "The Master Smith" not in mem.view(3).bio(target)["aliases"]
        assert "The Master Smith" in mem.view(4).bio(target)["aliases"]
        assert mem.view(3).bio(target)["state"] == {"note": "at the forge"}
        assert mem.view(4).bio(target)["state"] is None
        assert [dict(row) for row in mem._audit_all("ingested_chapters")] == receipts
        second = mem.replace_entity(
            target, effective_at=3, canonical_name="Aldric of the Vale", reason="A second name correction."
        )["target_entity_id"]
        assert mem.view(2).bio(target) is not None
        assert mem.view(2).bio(second) is None
        assert "The Master Smith" in mem.view(4).bio(second)["aliases"]
        assert mem.view(4).bio(second)["state"] is None


def test_fresh_preparation_after_correction_preserves_new_facts(tmp_path):
    """Control: the same extracted old name resolves through the corrected identity's alias."""
    store, client = _seed(tmp_path, 2)
    with store.book("audit") as mem:
        target = _correct(mem)
    prepared = _prepare(store, client, 3)
    with store.book("audit") as mem:
        ingest_chapter(mem, _chapter(3), prepared)
        event = mem.view(3).timeline()[0]
        assert [row["entity_id"] for row in mem.view(3).participants_of(event["event_id"])] == [target]
        assert mem.view(3).bio(target)["state"] == {"note": "at the forge"}


def test_relationship_payload_does_not_disclose_a_future_expiry(tmp_path):
    store = Store(str(tmp_path))
    with store.book("audit", meta={"title": "Synthetic audit"}) as mem:
        source = mem.add_entity("Aldric", "character", 1)
        target = mem.add_entity("Berenice", "character", 1)
        edge = mem.add_edge(source, target, "love", "engaged", 1)
        before = [dict(row) for row in mem.view(1).relationships()]
        mem.replace_edge(edge, at=3, rel_type="rivalry", label="estranged")
        after = [dict(row) for row in mem.view(1).relationships()]
        assert after == before, "a chapter-one response must not say the engagement ends in chapter three"
