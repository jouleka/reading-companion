"""Independent local reader-boundary audit using temporary, synthetic books only."""

from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ingest.extraction.schema import Extraction
from app.ingest.extraction.validation import ExtractionVerdict
from app.ingest import worker as worker_module
from app.main import create_app
from tests.api._epub import three_chapter_book


class InlineAuditExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True):
        pass


@pytest.fixture
def audit_book(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        allow_stub=True,
        openai_api_key=None,
        anthropic_api_key=None,
        openai_base_url=None,
        embed_api_key=None,
        embed_base_url=None,
        embed_provider="stub",
    )
    app = create_app(settings, ingest_executor=InlineAuditExecutor())
    with TestClient(app) as client:
        imported = client.post(
            "/api/books",
            files={"file": ("audit.epub", three_chapter_book(), "application/epub+zip")},
        )
        assert imported.status_code == 201
        book_id = imported.json()["book_id"]
        manifest = json.loads(
            (Path(settings.data_dir) / "books" / book_id / "atoms.json").read_text("utf-8")
        )
        yield client, app, book_id, manifest["atoms"]


def _advance(client, book_id, atoms, chapter, *, epoch=0):
    offset = sum(atom["char_len"] for atom in atoms[:chapter])
    response = client.put(
        f"/api/books/{book_id}/position",
        json={"cfi": f"audit-chapter-{chapter}", "offset": offset, "position_epoch": epoch},
    )
    assert response.status_code == 200
    return response.json()


def test_reader_correction_rejects_a_stale_lower_frontier(audit_book):
    """ADR0039 says a reader correction must use the exact current completed bookmark."""
    client, _app, book_id, atoms = audit_book
    _advance(client, book_id, atoms, 3)
    source = next(
        character
        for character in client.get(f"/api/books/{book_id}/graph").json()["characters"]
        if character["canonical_name"] == "Aldric"
    )
    response = client.post(
        f"/api/books/{book_id}/memory-corrections",
        json={
            "source_entity_id": source["entity_id"],
            "canonical_name": "Aldric Vale",
            "reason": "A stale tab still has the chapter-two Codex open.",
            "bookmark": 2,
        },
    )
    assert response.status_code == 409, response.json()
    assert client.get(
        f"/api/books/{book_id}/memory-corrections", params={"bookmark": 2}
    ).json()["items"] == []


def test_reset_excludes_retained_future_memory_from_every_structured_route(audit_book):
    client, app, book_id, atoms = audit_book
    _advance(client, book_id, atoms, 3)
    with app.state.store.book(book_id) as mem:
        receipts = [dict(row) for row in mem._audit_all("ingested_chapters")]
    response = client.post(
        f"/api/books/{book_id}/position/reset", json={"position_epoch": 0}
    )
    assert response.status_code == 200
    for surface, key in (("graph", "characters"), ("timeline", "events"), ("notes", "chapters")):
        payload = client.get(f"/api/books/{book_id}/{surface}", params={"bookmark": 99}).json()
        assert payload["as_of_chapter"] == 0
        assert payload[key] == []
    assert client.put(
        f"/api/books/{book_id}/position",
        json={"cfi": "stale-before-reset", "offset": 10**8, "position_epoch": 0},
    ).status_code == 409
    _advance(client, book_id, atoms, 1, epoch=1)
    payload = client.get(f"/api/books/{book_id}/graph", params={"bookmark": 99}).json()
    assert payload["as_of_chapter"] == 1
    names = {item["canonical_name"] for item in payload["characters"]}
    assert "Aldric" in names and not names.intersection({"Berenice", "Corvus"})
    assert all(item["revealed_at"] <= 1 for item in payload["characters"])
    with app.state.store.book(book_id) as mem:
        assert [dict(row) for row in mem._audit_all("ingested_chapters")] == receipts


def test_a_missing_middle_receipt_clamps_later_facts_despite_catalog_progress(audit_book):
    client, app, book_id, atoms = audit_book
    _advance(client, book_id, atoms, 3)
    with app.state.store.book(book_id) as mem:
        with mem._writer():
            mem._conn.execute(
                "DELETE FROM ingested_chapters WHERE book_id=? AND chapter_key=?",
                (book_id, atoms[1]["key"]),
            )
    payload = client.get(f"/api/books/{book_id}/graph", params={"bookmark": 99}).json()
    assert payload["as_of_chapter"] == 1
    names = {item["canonical_name"] for item in payload["characters"]}
    assert "Aldric" in names and not names.intersection({"Berenice", "Corvus"})
    assert all(item["revealed_at"] <= 1 for item in payload["characters"])
    assert client.get(f"/api/books/{book_id}/position").json()["ingest_progress"] == 1


def test_worker_reprepares_a_corrected_roster_without_a_second_completion(audit_book, monkeypatch):
    client, app, book_id, atoms = audit_book
    _advance(client, book_id, atoms, 2)
    original_prepare = worker_module.prepare_chapter
    targets, completion_calls = [], []

    def extracted(_system, user, **kwargs):
        if kwargs["schema"] is ExtractionVerdict:
            from tests.ingest._grounding import canned_grounding_verdict

            return canned_grounding_verdict(user), {"in": 10, "out": 10}
        assert kwargs["schema"] is Extraction
        completion_calls.append(1)
        return {
            "chapter_summary": "Aldric works at the forge.",
            "entities": [{"canonical_name": "Aldric", "type": "character", "aliases": [],
                          "matched_roster": True, "state": "at the forge"}],
            "relationships": [],
            "events": [{"summary": "Aldric works at the forge.", "participants": ["Aldric"]}],
            "themes": [],
        }, {"in": 10, "out": 10}

    def correct_after_preparation(chapter, *args, **kwargs):
        prepared = original_prepare(chapter, *args, **kwargs)
        if chapter["ordinal"] == 3 and not targets:
            with app.state.store.book(book_id) as mem:
                source = next(row for row in mem.view(2).characters()
                              if row["canonical_name"] == "Aldric")
                targets.append(mem.replace_entity(
                    source["entity_id"], effective_at=2, canonical_name="Aldric Vale",
                    reason="A correction commits while chapter-three provider work is unlocked.",
                )["target_entity_id"])
        return prepared

    monkeypatch.setattr(app.state.client, "complete", extracted)
    monkeypatch.setattr(worker_module, "prepare_chapter", correct_after_preparation)
    _advance(client, book_id, atoms, 3)
    assert completion_calls == [1]
    assert app.state.worker.status(book_id)["status"] == "done"
    with app.state.store.book(book_id) as mem:
        assert mem.completion_frontier(atoms) == 3
        event = mem.view(3).timeline()[0]
        assert [row["entity_id"] for row in mem.view(3).participants_of(event["event_id"])] == targets
        assert mem.view(3).bio(targets[0])["state"] == {"note": "at the forge"}


def test_last_visible_page_remains_pending_until_an_explicit_end_report(audit_book):
    client, _app, book_id, atoms = audit_book
    prior = sum(atom["char_len"] for atom in atoms[:-1])
    # Foliate's final section with two content pages has page=2,pages=4 (two padding pages).
    last_visible_start = prior + atoms[-1]["char_len"] // 4
    pending = client.put(
        f"/api/books/{book_id}/position",
        json={"cfi": "final-visible-page", "offset": last_visible_start, "position_epoch": 0},
    )
    assert pending.status_code == 200
    assert pending.json()["bookmark"] == 2
    assert client.get(f"/api/books/{book_id}/notes").json()["as_of_chapter"] == 2
    # An explicit reader completion, rather than merely arriving on that page, authorizes chapter3.
    finished = _advance(client, book_id, atoms, 3)
    assert finished["bookmark"] == 3
    assert client.get(f"/api/books/{book_id}/notes").json()["as_of_chapter"] == 3


def test_continuous_roster_churn_is_bounded_and_never_publishes_a_partial_chapter(audit_book, monkeypatch):
    client, app, book_id, atoms = audit_book
    _advance(client, book_id, atoms, 2)
    original_prepare, original_complete = worker_module.prepare_chapter, app.state.client.complete
    attempts, completion_calls = [], []

    def counted(*args, **kwargs):
        if kwargs["schema"] is Extraction:
            completion_calls.append(1)
        return original_complete(*args, **kwargs)

    def keep_changing(chapter, *args, **kwargs):
        prepared = original_prepare(chapter, *args, **kwargs)
        attempts.append(1)
        with app.state.store.book(book_id) as mem:
            source = next(row["entity_id"] for row in mem.view(2).characters()
                          if row["canonical_name"] == "Aldric")
            mem.add_alias(source, f"Concurrent alias {len(attempts)}", 2)
        return prepared

    monkeypatch.setattr(app.state.client, "complete", counted)
    monkeypatch.setattr(worker_module, "prepare_chapter", keep_changing)
    _advance(client, book_id, atoms, 3)
    assert 1 < len(attempts) <= 3 and completion_calls == [1]
    assert app.state.worker.status(book_id)["status"] == "error"
    with app.state.store.book(book_id) as mem:
        assert mem.completion_frontier(atoms) == 2
        assert len(mem.view(3).chapters()) == 2
        assert all(row["revealed_at"] < 3 for row in mem.view(3).characters())


def test_graph_does_not_disclose_the_expiry_of_a_still_visible_relationship(audit_book):
    client, app, book_id, atoms = audit_book
    _advance(client, book_id, atoms, 3)
    with app.state.store.book(book_id) as mem:
        source = next(row["entity_id"] for row in mem.view(1).characters()
                      if row["canonical_name"] == "Aldric")
        friend = mem.add_entity("Rowan", "character", 1)
        edge = mem.add_edge(source, friend, "love", "engaged", 1)
        mem.replace_edge(edge, at=3, rel_type="rivalry", label="estranged")
    past = client.get(f"/api/books/{book_id}/graph", params={"bookmark": 1}).json()
    relationship = next(row for row in past["relationships"] if row["edge_id"] == edge)
    assert relationship["label"] == "engaged" and relationship["invalid_at"] is None


def test_local_offline_merge_preserves_completed_geometry_and_latest_resume_cursor(audit_book):
    client, _app, book_id, atoms = audit_book
    total = sum(atom["char_len"] for atom in atoms)
    response = client.put(
        f"/api/books/{book_id}/position",
        json={"cfi": "latest-backward-cursor", "offset": 2, "high_water_offset": total,
              "position_epoch": 0},
    )
    assert response.status_code == 200
    assert response.json()["bookmark"] == 3
    assert response.json()["current_chapter"] == 1
    canonical = client.get(f"/api/books/{book_id}/position").json()
    assert canonical["bookmark"] == 3 and canonical["cfi"] == "latest-backward-cursor"
    assert client.post(
        f"/api/books/{book_id}/position/reset", json={"position_epoch": 0}
    ).status_code == 200
    assert client.put(
        f"/api/books/{book_id}/position",
        json={"cfi": "late-prior-pass", "offset": 2, "high_water_offset": total,
              "position_epoch": 0},
    ).status_code == 409
    assert client.get(f"/api/books/{book_id}/position").json()["bookmark"] == 0


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "100", 2**63])
def test_local_offline_high_water_requires_a_bounded_integer(audit_book, invalid):
    client, _app, book_id, _atoms = audit_book
    assert client.put(
        f"/api/books/{book_id}/position",
        json={"cfi": "invalid", "offset": 0, "high_water_offset": invalid, "position_epoch": 0},
    ).status_code == 422
    assert client.get(f"/api/books/{book_id}/position").json()["bookmark"] == 0
