"""Module C / pipeline.py — prepare_chapter + ingest_chapter through the production DAL/Catalog.
Deterministic (hand-crafted extraction dicts + offline stub client) so every ADR 0007 §E invariant is
pinned: append-once early-return (D-A3), pin-before-chunk (Inv 6), bookmark-bounded roster + spoiler
bounding (Inv 11), cross-chapter roster-link (anti-drift), state-timeline advance on merge, and
cost/ingest-progress recording.
"""
import multiprocessing
import os
import sqlite3

import pytest

from app.catalog.catalog import Catalog
from app.ingest.extraction.chapter_text import content_hash_of
from app.ingest.extraction.pipeline import all_entities, ingest_chapter, prepare_chapter
from app.llm.client import LLMClient
from app.memory.store import Store


def _client():
    return LLMClient(provider="stub", allow_stub=True)


def test_commit_phase_uses_only_prepared_values(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    prepared = prepare_chapter(_CH1, _EX1, client, roster=[])

    def unexpected_io(*_args, **_kwargs):
        raise AssertionError("model callback reached from the database-only commit phase")

    client.embed = unexpected_io
    client.extractor_version = unexpected_io
    with store.book("b", meta=dict(title="B")) as mem:
        result = ingest_chapter(mem, _CH1, prepared)
    assert result["skipped"] is False


def test_prepared_values_cannot_be_committed_to_a_different_chapter(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    prepared = prepare_chapter(_CH1, _EX1, client, roster=[])
    with store.book("b", meta=dict(title="B")) as mem:
        with pytest.raises(ValueError, match="prepared chapter mismatch"):
            ingest_chapter(mem, _CH2, prepared)
        assert mem._audit_all("chapters") == []


def test_prepared_values_bind_ordinal_and_actual_text_hash(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    original = _ch(1, "b:c1.xhtml", "Chapter I", "alpha")
    prepared = prepare_chapter(original, _EX1, client, roster=[])
    mutated = {**original, "ordinal": 99, "text": "beta", "content_hash": content_hash_of("alpha")}
    with store.book("b", meta=dict(title="B")) as mem:
        with pytest.raises(ValueError, match="prepared chapter mismatch"):
            ingest_chapter(mem, mutated, prepared)
        assert mem._audit_all("chapters") == []


def _ch(ordinal, key, title, text, href=None, part_label=""):
    return {"ordinal": ordinal, "key": key, "title": title, "text": text,
            "href": href or key.split(":", 1)[1], "part_label": part_label}


# chapter 1 introduces Alyosha (+ alias) and his father Fyodor
_CH1 = _ch(1, "b:c1.xhtml", "Chapter I",
           "Alyosha, the son of Fyodor Pavlovitch Karamazov, joins the monastery.",
           part_label="PART I")
_EX1 = {
    "chapter_summary": "Alyosha, son of Fyodor, enters the monastery.",
    "entities": [
        {"canonical_name": "Alexey Fyodorovitch Karamazov", "type": "character",
         "aliases": ["Alyosha"], "matched_roster": False, "state": "a novice"},
        {"canonical_name": "Fyodor Pavlovitch Karamazov", "type": "character",
         "aliases": [], "matched_roster": False, "state": None},
        {"canonical_name": "the monastery", "type": "place",
         "aliases": [], "matched_roster": False, "state": None},
    ],
    "relationships": [
        {"src": "Fyodor Pavlovitch Karamazov", "dst": "Alexey Fyodorovitch Karamazov",
         "rel_type": "family", "label": "father of"},
    ],
    "events": [{"summary": "Alyosha enters the monastery.",
                "participants": ["Alexey Fyodorovitch Karamazov"]}],
    "themes": [{"name": "faith", "description": "Alyosha's calling."}],
}

# chapter 2 RE-mentions Alyosha (must link, not duplicate) and introduces a NEW brother, Ivan
_CH2 = _ch(2, "b:c2.xhtml", "Chapter II",
           "Ivan Fyodorovitch Karamazov, Alyosha's brother, returns to town.")
_EX2 = {
    "chapter_summary": "Ivan, Alyosha's brother, returns.",
    "entities": [
        {"canonical_name": "Alexey Fyodorovitch Karamazov", "type": "character",
         "aliases": ["Alyosha"], "matched_roster": True, "state": "at the monastery"},
        {"canonical_name": "Ivan Fyodorovitch Karamazov", "type": "character",
         "aliases": ["Ivan"], "matched_roster": False, "state": None},
    ],
    "relationships": [
        {"src": "Ivan Fyodorovitch Karamazov", "dst": "Alexey Fyodorovitch Karamazov",
         "rel_type": "family", "label": "brother of"},
    ],
    "events": [{"summary": "Ivan returns home.", "participants": ["Ivan Fyodorovitch Karamazov"]}],
    "themes": [],
}


_CHAPTER_FACT_TABLES = (
    "chapters",
    "ingested_chapters",
    "raw_chapters",
    "chapter_summaries",
    "entities",
    "aliases",
    "edges",
    "events",
    "event_participants",
    "themes",
    "entity_state",
    "chunks",
)


def _prepare_for(store, client, ch, extraction, **kwargs):
    with store.book("b", meta=dict(title="B")) as mem:
        roster = all_entities(mem.view(max(ch["ordinal"] - 1, 0)))
    return prepare_chapter(ch, extraction, client, roster=roster, **kwargs)


def _commit(store, client, ch, extraction, *, catalog=None, before_commit=None, **kwargs):
    prepared = _prepare_for(store, client, ch, extraction, **kwargs)
    content_hash = ch.get("content_hash") or content_hash_of(ch.get("text", "") or "")
    with store.book("b", meta=dict(title="B")) as mem:
        if before_commit is not None:
            before_commit(mem)
        result = ingest_chapter(mem, ch, prepared)
        receipt = mem.chapter_completion(ch["key"], ch["ordinal"], content_hash)
        assert receipt is not None
    if catalog is not None:
        catalog.finalize_ingest("b", ch["ordinal"], cost=receipt["cost"])
    return result


def _kill_during_edge_write(data_dir):
    """Subprocess target: terminate without unwinding while the chapter transaction is open."""
    store, client = Store(data_dir=data_dir), _client()
    prepared = _prepare_for(store, client, _CH1, _EX1)
    with store.book("b", meta=dict(title="B")) as mem:
        mem.add_edge = lambda *args, **kwargs: os._exit(91)
        ingest_chapter(mem, _CH1, prepared)


def _ingest_two(store, client):
    return _commit(store, client, _CH1, _EX1), _commit(store, client, _CH2, _EX2)


def test_end_to_end_ingest_populates_the_store(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    r1, r2 = _ingest_two(store, client)
    assert r1["skipped"] is False and r2["skipped"] is False
    with store.book("b") as mem:
        chars = {c["canonical_name"]: c for c in mem.view(2).characters()}
        assert set(chars) == {"Alexey Fyodorovitch Karamazov", "Fyodor Pavlovitch Karamazov",
                              "Ivan Fyodorovitch Karamazov"}
        assert len(mem.view(2).timeline()) == 2                    # two events
        assert [t["name"] for t in mem.view(2).themes()] == ["faith"]


def test_recurring_entity_links_across_chapters_not_duplicated(tmp_path):
    # the anti-drift core: ch2's matched_roster Alyosha links to ch1's entity (one Alyosha, first seen @1)
    store, client = Store(data_dir=str(tmp_path)), _client()
    _ingest_two(store, client)
    with store.book("b") as mem:
        aly = [c for c in mem.view(2).characters()
               if c["canonical_name"] == "Alexey Fyodorovitch Karamazov"]
        assert len(aly) == 1                                       # NOT duplicated
        assert aly[0]["revealed_at"] == 1                          # keeps its first-seen ordinal


def test_honorific_variant_merges_and_is_retained_as_a_searchable_alias(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    base = {"chapter_summary": "Zossima appears.", "entities": [
        {"canonical_name": "Zossima", "type": "character", "aliases": [],
         "matched_roster": False, "state": None}],
        "relationships": [], "events": [], "themes": []}
    variant = {"chapter_summary": "Father Zossima speaks.", "entities": [
        {"canonical_name": "Father Zossima", "type": "character", "aliases": [],
         "matched_roster": False, "state": None}],
        "relationships": [], "events": [], "themes": []}
    _commit(store, client, _ch(1, "b:c1.xhtml", "Chapter I", "Zossima appears."), base)
    _commit(store, client, _ch(2, "b:c2.xhtml", "Chapter II", "Father Zossima speaks."), variant)
    with store.book("b") as mem:
        characters = mem.view(2).characters()
        assert len(characters) == 1
        aliases = [row["surface_form"] for row in mem.view(2).aliases_of(characters[0]["entity_id"])]
        assert "Father Zossima" in aliases


def test_extraction_is_bookmark_bounded_no_future_leak(tmp_path):
    # Inv 11: ch2's Ivan (revealed_at=2) must NOT be visible at bookmark 1
    store, client = Store(data_dir=str(tmp_path)), _client()
    _ingest_two(store, client)
    with store.book("b") as mem:
        names_at_1 = {c["canonical_name"] for c in mem.view(1).characters()}
        assert "Ivan Fyodorovitch Karamazov" not in names_at_1     # future character not revealed early
        assert names_at_1 == {"Alexey Fyodorovitch Karamazov", "Fyodor Pavlovitch Karamazov"}


def test_state_timeline_advances_on_merge(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    _ingest_two(store, client)
    with store.book("b") as mem:
        aly_id = next(c["entity_id"] for c in mem.view(2).characters()
                      if c["canonical_name"] == "Alexey Fyodorovitch Karamazov")
        assert mem.view(1).bio(aly_id)["state"] == {"note": "a novice"}        # ch1 state
        assert mem.view(2).bio(aly_id)["state"] == {"note": "at the monastery"}  # advanced in ch2


def test_append_once_double_ingest_is_a_skip_with_no_duplicate_rows(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    _ingest_two(store, client)
    prepared = _prepare_for(store, client, _CH1, _EX1)
    with store.book("b") as mem:
        before = (len(mem._audit_all("entities")), len(mem._audit_all("edges")),
                  len(mem._audit_all("events")), len(mem._audit_all("themes")),
                  len(mem._audit_all("chunks")), len(mem._audit_all("chapter_summaries")),
                  len(mem._audit_all("aliases")), len(mem._audit_all("entity_state")))
        again = ingest_chapter(mem, _CH1, prepared)                 # re-ingest ch1, unchanged content
        assert again["skipped"] is True
        after = (len(mem._audit_all("entities")), len(mem._audit_all("edges")),
                 len(mem._audit_all("events")), len(mem._audit_all("themes")),
                 len(mem._audit_all("chunks")), len(mem._audit_all("chapter_summaries")),
                 len(mem._audit_all("aliases")), len(mem._audit_all("entity_state")))
        assert before == after                                      # exactly one live row per derived fact


def test_completion_receipt_rejects_a_parent_chapter_ordinal_mismatch(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    _commit(store, client, _CH1, _EX1)
    expected_hash = content_hash_of(_CH1["text"])

    with store.book("b") as mem:
        with mem._writer():
            mem._conn.execute(
                "UPDATE chapters SET revealed_at=? WHERE chapter_key=?",
                (2, _CH1["key"]),
            )
        assert mem.chapter_completion(_CH1["key"], 1, expected_hash) is None


def test_completion_receipt_rejects_a_raw_chapter_ordinal_mismatch(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    _commit(store, client, _CH1, _EX1)
    expected_hash = content_hash_of(_CH1["text"])

    with store.book("b") as mem:
        assert mem.chapter_completion(_CH1["key"], 1, expected_hash) is not None
        with mem._writer():
            mem._conn.execute(
                "UPDATE raw_chapters SET revealed_at=? WHERE chapter_key=?",
                (99, _CH1["key"]),
            )
        assert mem.chapter_completion(_CH1["key"], 1, expected_hash) is None


def test_mid_chapter_write_failure_rolls_back_every_fact_and_is_resumable(tmp_path, monkeypatch):
    store, client = Store(data_dir=str(tmp_path)), _client()
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="B")
    prepared = _prepare_for(store, client, _CH1, _EX1, usage={"in": 100, "out": 20})

    with store.book("b", meta=dict(title="B")) as mem:
        original_add_chunk = mem.add_chunk

        def fail_at_final_chunk(*args, **kwargs):
            raise RuntimeError("fault injected after all earlier chapter writes")

        monkeypatch.setattr(mem, "add_chunk", fail_at_final_chunk)
        with pytest.raises(RuntimeError, match="fault injected"):
            ingest_chapter(mem, _CH1, prepared)

        for table in _CHAPTER_FACT_TABLES:
            assert mem._audit_all(table) == [], table
        assert mem.chapter_is_ingested(_CH1["key"], content_hash_of(_CH1["text"])) is False
        pin = mem.pinned_identity()
        assert pin is not None and pin["embed_model"] is None
        failed_state = catalog.get_state("b")
        assert failed_state is not None and failed_state["ingest_progress"] == 0
        assert catalog.get_costs("b") == []

        monkeypatch.setattr(mem, "add_chunk", original_add_chunk)
        result = ingest_chapter(mem, _CH1, prepared)
        assert result["skipped"] is False
        assert mem.chapter_is_ingested(_CH1["key"], content_hash_of(_CH1["text"])) is True
        receipt = mem.chapter_completion(
            _CH1["key"], _CH1["ordinal"], content_hash_of(_CH1["text"])
        )

    assert receipt is not None
    catalog.finalize_ingest("b", 1, cost=receipt["cost"])
    state = catalog.get_state("b")
    assert state is not None and state["ingest_progress"] == 1
    assert len(catalog.get_costs("b")) == 1


def test_process_death_mid_chapter_leaves_no_partial_state_and_retry_succeeds(tmp_path):
    data_dir = str(tmp_path / "store")
    process = multiprocessing.get_context("spawn").Process(
        target=_kill_during_edge_write,
        args=(data_dir,),
    )
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("fault-injection subprocess did not exit")
    assert process.exitcode == 91

    store, client = Store(data_dir=data_dir), _client()
    with store.book("b") as mem:
        for table in _CHAPTER_FACT_TABLES:
            assert mem._audit_all(table) == [], table
        assert mem.chapter_is_ingested(_CH1["key"], content_hash_of(_CH1["text"])) is False

    result = _commit(store, client, _CH1, _EX1)
    with store.book("b") as mem:
        assert result["skipped"] is False
        assert mem.chapter_is_ingested(_CH1["key"], content_hash_of(_CH1["text"])) is True
        assert len(mem._audit_all("chapters")) == 1
        assert len(mem._audit_all("raw_chapters")) == 1
        assert len(mem._audit_all("edges")) == 1


def test_pins_before_first_chunk_and_chunk_is_same_space_searchable(tmp_path):
    # Inv 6: the pipeline pins the embedding model before any chunk; chunks are stamped + searchable
    store, client = Store(data_dir=str(tmp_path)), _client()
    _ingest_two(store, client)
    with store.book("b") as mem:
        pin = mem.pinned_identity()
        assert pin["embed_model"] == "stub:lexical-stub-256" and pin["embed_dim"] == 256
        chunks = [c for c in mem._audit_all("chunks") if c["retracted_at"] is None]
        assert chunks and all(c["embed_model"] == "stub:lexical-stub-256" for c in chunks)
        qvec = client.embed(["Karamazov monastery"])[0][0]
        hits = mem.view(2).search(qvec, k=3)                        # same-space search returns chunks
        assert hits


def test_embedding_callback_runs_before_the_chapter_transaction(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    with store.book("b", meta=dict(title="B")):
        pass
    handle = store._handles["b"]
    transaction_states = []

    def probed_embed(texts):
        transaction_states.append(handle._transaction_active)
        with pytest.raises(sqlite3.DatabaseError):
            handle._conn.execute("SELECT canonical_name FROM entities").fetchall()
        return client.embed(texts)[0]

    prepared = prepare_chapter(_CH1, _EX1, client, roster=[], embed_fn=probed_embed)
    with store.book("b") as mem:
        ingest_chapter(mem, _CH1, prepared)

    assert transaction_states == [False]


def test_no_chunk_for_a_content_less_atom(tmp_path):
    # a content-less atom (empty text) yields no raw row and no chunk -> no facts, never a leak
    store, client = Store(data_dir=str(tmp_path)), _client()
    empty_ex = {"chapter_summary": "", "entities": [], "relationships": [], "events": [], "themes": []}
    _commit(store, client, _ch(1, "b:img.xhtml", "Chapter I", ""), empty_ex)
    with store.book("b") as mem:
        assert mem._audit_all("chunks") == []
        assert mem._audit_all("raw_chapters") == []
        assert len(mem._audit_all("chapters")) == 1                 # the chapter row itself still exists


def test_cost_and_ingest_progress_are_recorded(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    catalog = Catalog(str(tmp_path / "catalog.db"))
    catalog.add_book("b", title="B")
    _commit(store, client, _CH1, _EX1, catalog=catalog, usage={"in": 1000, "out": 200})
    _commit(store, client, _CH2, _EX2, catalog=catalog, usage={"in": 800, "out": 150})
    assert catalog.get_state("b")["ingest_progress"] == 2
    costs = catalog.get_costs("b")
    assert len(costs) == 2
    assert sum(c["input_tokens"] for c in costs) == 1800
    assert all(c["phase"] == "extraction" for c in costs)


def test_all_entities_returns_the_bookmark_bounded_roster(tmp_path):
    store, client = Store(data_dir=str(tmp_path)), _client()
    _ingest_two(store, client)
    with store.book("b") as mem:
        roster1 = {e["canonical_name"] for e in all_entities(mem.view(1))}
        roster2 = {e["canonical_name"] for e in all_entities(mem.view(2))}
        assert "Ivan Fyodorovitch Karamazov" not in roster1         # roster @1 excludes the ch2 entity
        assert "Ivan Fyodorovitch Karamazov" in roster2
        assert "the monastery" in roster1                           # places are in the roster too
        # the roster carries aliases (used for resolution linkage)
        aly = next(e for e in all_entities(mem.view(2))
                   if e["canonical_name"] == "Alexey Fyodorovitch Karamazov")
        assert "Alyosha" in aly["aliases"]


def test_changed_content_reingest_fails_loud_and_writes_nothing(tmp_path):
    # pass-1 HIGH: re-ingesting the SAME chapter_key with CHANGED content must NOT crash mid-write +
    # corrupt state. It fails loud (first-ingest only; re-extraction retracts first, LIT-19) and writes
    # nothing — the live rows still reflect the ORIGINAL content.
    store, client = Store(data_dir=str(tmp_path)), _client()
    ch_v1 = _ch(1, "b:c1.xhtml", "Chapter I", "version one: Alyosha appears.")
    ch_v2 = _ch(1, "b:c1.xhtml", "Chapter I", "version two: a COMPLETELY different text.")
    _commit(store, client, ch_v1, _EX1)
    prepared_v2 = _prepare_for(store, client, ch_v2, _EX2)
    with store.book("b") as mem:
        before = mem.view(1).raw_text("b:c1.xhtml")
        with pytest.raises(ValueError):
            ingest_chapter(mem, ch_v2, prepared_v2)         # changed content -> fail loud, no crash
        # state is intact: still the ORIGINAL content, no partial/duplicate derived rows
        assert mem.view(1).raw_text("b:c1.xhtml") == before
        assert len([r for r in mem._audit_all("raw_chapters") if r["retracted_at"] is None]) == 1
        live_chap = [r for r in mem._audit_all("chapters") if r["retracted_at"] is None]
        assert len(live_chap) == 1 and live_chap[0]["content_hash"] == content_hash_of(before)


def test_last_same_chapter_state_wins_no_silent_drop(tmp_path):
    # pass-1 MEDIUM: two mentions of ONE entity in a chapter each carrying a state -> the LAST (most
    # current) wins; the earlier is not silently kept. (Was: the second was dropped.)
    store, client = Store(data_dir=str(tmp_path)), _client()
    ex = {"chapter_summary": "s",
          "entities": [
              {"canonical_name": "Dmitri Fyodorovitch Karamazov", "type": "character",
               "aliases": ["Mitya"], "matched_roster": False, "state": "arrives in town"},
              {"canonical_name": "Mitya", "type": "character",            # same entity, later mention
               "aliases": [], "matched_roster": False, "state": "is arrested"}],
          "relationships": [], "events": [], "themes": []}
    _commit(store, client, _ch(1, "b:c1.xhtml", "Ch I", "Mitya text."), ex)
    with store.book("b") as mem:
        chars = mem.view(1).characters()
        assert len(chars) == 1                                # collapsed to one entity
        assert mem.view(1).bio(chars[0]["entity_id"])["state"] == {"note": "is arrested"}   # LAST wins
        live_states = [r for r in mem._audit_all("entity_state") if r["retracted_at"] is None]
        assert len(live_states) == 1                          # exactly one live state for the chapter


def test_resolve_embed_stub_is_rejected():
    # pass-1 LOW: layer-4 resolution embedding must never be the lexical stub (it over-merges siblings).
    # Passing a resolve_embed while the client's embedder is the stub must fail loud, not silently drift.
    client = _client()
    assert client.embed_identity().startswith("stub:")
    with pytest.raises(ValueError):
        prepare_chapter(
            _CH1,
            _EX1,
            client,
            roster=[],
            resolve_embed=lambda ts: client.embed(ts)[0],
        )


def test_unresolved_relationship_ref_is_reported_not_crashed(tmp_path):
    # an edge naming an entity that wasn't extracted is counted (observability), not written/crashed
    store, client = Store(data_dir=str(tmp_path)), _client()
    ex = {"chapter_summary": "s", "entities": [
            {"canonical_name": "Alyosha", "type": "character", "aliases": [],
             "matched_roster": False, "state": None}],
          "relationships": [{"src": "Alyosha", "dst": "A Ghost Who Does Not Exist",
                             "rel_type": "social", "label": "knows"}],
          "events": [], "themes": []}
    r = _commit(store, client, _ch(1, "b:c1.xhtml", "Chapter I", "Alyosha walks."), ex)
    with store.book("b") as mem:
        assert r["unresolved_rel_refs"] == 1
        assert mem._audit_all("edges") == []                        # the dangling edge was not written
