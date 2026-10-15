"""Full-source local retrieval: source geometry, atomic publication, budgets, and resumability.

The embedding double below is a tiny hand-labelled semantic oracle for orchestration only. It does
not evaluate a provider's semantic quality; root-owned live tests cover that separately.
"""
import copy
import json
import sqlite3

import pytest

from app.catalog.catalog import Catalog
from app.config import Settings
from app.cost import CostCeilingExceeded, usd_of
from app.ingest.extraction.chapter_text import content_hash_of
from app.ingest.extraction.pipeline import ingest_chapter, prepare_chapter
from app.ingest.manifest import _version, write_manifest
from app.llm.client import LLMClient
from app.llm import versioning
from app.memory import migrations
from app.memory.store import Store
from app.retrieval_index import IndexRecoveryRequired, backfill_source_index
from app.retrieval_passages import (
    PASSAGE_CHARS, PASSAGE_INDEX_VERSION, embedding_batches, embedding_text, normalize_source,
    passage_windows, source_digest,
)


MODEL = "openai-compatible@https://api.openai.com/v1:text-embedding-3-small"
BID = "bkindexfixture"
DEEP = "He hit him with the iron paper-weight and fled."


class SemanticDouble:
    provider = "stub"
    version = {"provider": "stub", "cheap": "new-extractor", "large": "new-synth", "embed": MODEL}

    def __init__(self):
        self.calls = []

    def embed_identity(self):
        return MODEL

    def extractor_version(self, tier):
        return f"stub:{self.version['cheap' if tier == 'cheap' else 'large']}"

    def embed(self, texts):
        self.calls.append(list(texts))
        vectors = [[0, 0, 1] if text == versioning.EMBED_CANARY_TEXT else
                   [1, 0.01, 0] if DEEP in text else [0.01, 1, 0] for text in texts]
        return vectors, {"in": max(1, sum(len(text.encode('utf-8')) for text in texts) // 4)}


@pytest.fixture
def setup(tmp_path):
    opened = []

    def seed(*, backend="vec0", bookmark=1, progress=2, text=None, input_limit=6000):
        data = tmp_path / f"data-{len(opened)}"
        settings = Settings(_env_file=None, data_dir=str(data), allow_stub=True,
                            openai_api_key=None, anthropic_api_key=None, embed_api_key=None,
                            cost_max_input_tokens_per_call=input_limit)
        catalog = Catalog(str(data / "catalog.db"), schema_version_default=migrations.CURRENT_VERSION)
        catalog.add_book(BID, title="Fixture", schema_version=migrations.CURRENT_VERSION)
        store = Store(str(data), vector_backend=backend, schema_version_callback=catalog.set_schema_version)
        client = SemanticDouble()
        texts = [text if text is not None else "Aldric read ordinary records. " * 260 + DEEP,
                 "Future hidden testimony. " * 100 + DEEP]
        atoms = []
        with store.book(BID) as mem:
            mem.pin_models(extractor_model="old:extraction", synth_model="old:synthesis",
                           embed_model=MODEL, embed_dim=3, embed_canary=[0, 0, 1])
            for ordinal, raw in enumerate(texts, 1):
                key = f"{BID}:chapter-{ordinal}"
                title = f"Chapter {ordinal}"
                atoms.append({"key": key, "ordinal": ordinal, "title": title,
                              "href": f"chapter-{ordinal}.xhtml", "char_len": len(raw)})
                digest = content_hash_of(raw)
                with mem.transaction():
                    mem.add_chapter(key, ordinal, atoms[-1]["href"], title=title, content_hash=digest)
                    mem.add_raw(key, ordinal, raw, content_hash=digest)
                    mem.add_summary(key, ordinal, "An unchanged extracted summary.")
                    mem.add_chunk(key, ordinal, raw[:500], [0.01, 1, 0])
                    mem.mark_chapter_ingested(key, digest, cost={"model": "old:extraction",
                                                               "input_tokens": 5, "output_tokens": 2,
                                                               "usd": 0.001})
        manifest = {"book_id": BID, "mode": "file-driven", "flags": [], "atoms": atoms,
                    "atom_set_version": _version(atoms)}
        write_manifest(str(data), manifest)
        catalog.set_position(BID, "saved-cfi", bookmark)
        for ordinal in range(1, progress + 1):
            catalog.finalize_ingest(BID, ordinal, cost={"model": "old:extraction", "input_tokens": 5,
                                                        "output_tokens": 2, "usd": 0.001})
        opened.append((store, catalog))
        return store, catalog, client, settings, atoms

    yield seed
    for store, catalog in opened:
        store.close()
        catalog.close()


def run(env, **kwargs):
    store, catalog, client, settings, _atoms = env
    return backfill_source_index(store, catalog, client, settings, BID,
                                 max_additional_usd=kwargs.pop("max_additional_usd", 0.10), **kwargs)


@pytest.mark.parametrize("raw", [" word\n" * 1200, "\u6771\u4eac\U0001f642" * 1800, "x" * 4200, "", "a b"],
                         ids=["prose", "unicode", "long-word", "empty", "short"])
def test_shared_windows_cover_exact_normalized_source_and_bound_batches(raw):
    text = normalize_source(raw)
    windows = list(passage_windows(raw))
    covered = set()
    for start, end, value in windows:
        assert 0 < len(value) <= PASSAGE_CHARS
        assert text[start:end] == value
        covered.update(range(start, end))
    assert covered == set(range(len(text)))
    passages = [{"start": start, "end": end, "text": value} for start, end, value in windows]
    batches = embedding_batches("A title", passages, 7000)
    assert [passage for batch in batches for passage in batch] == passages
    assert all(sum(len(embedding_text("A title", p["text"]).encode("utf-8")) for p in batch) + 32
               <= 7000 for batch in batches)


def test_extreme_titles_cannot_unbound_an_individual_embedding_input():
    passage = "\U0001f642" * PASSAGE_CHARS
    value = embedding_text("\U0001f642" * 10000, passage)
    assert value.endswith(passage)
    assert len(value.encode("utf-8")) < 7200


@pytest.mark.parametrize("backend", ["vec0", "bruteforce"])
def test_deep_semantic_hit_exact_provenance_no_future_no_fact_or_pin_mutation(setup, backend):
    env = setup(backend=backend)
    store, catalog, client, settings, atoms = env
    with store.book(BID) as mem:
        before = {table: [dict(row) for row in mem._audit_all(table)] for table in
                  ("book_meta", "chapters", "ingested_chapters", "raw_chapters", "chapter_summaries")}
        assert all(DEEP not in hit[1] for hit in mem.view(1).search([1, 0, 0], k=6))
    state = catalog.get_state(BID)
    report = run(env, through=99)
    assert report["through"] == 1 and report["indexed_chapters"] == 1
    assert report["canary_calls"] == 1
    assert sum(text == versioning.EMBED_CANARY_TEXT for call in client.calls for text in call) == 1
    assert not any("Future hidden" in text for call in client.calls for text in call)
    with store.book(BID) as mem:
        hit = mem.view(1).search([1, 0, 0], k=1)[0]
        assert DEEP in hit[1] and hit[2:] == (1, atoms[0]["key"])
        assert mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]
        raw = normalize_source(mem.view(1).raw_text(atoms[0]["key"]))
        rows = [row for row in mem._audit_all("chunks") if row["retracted_at"] is None
                and row["chapter_key"] == atoms[0]["key"]]
        assert len(rows) == len(list(passage_windows(raw))) > 1
        assert all(row["text"] == raw[row["source_start"]:row["source_end"]] for row in rows)
        assert all(row["source_version"] == PASSAGE_INDEX_VERSION for row in rows)
        assert all(row["source_hash"] == source_digest("Chapter 1", raw) for row in rows)
        for table, previous in before.items():
            assert [dict(row) for row in mem._audit_all(table)] == previous
        assert mem._audit_all("retrieval_index_batches") == []
        for table in ("retrieval_index_chapters", "retrieval_index_batches"):
            with pytest.raises(sqlite3.DatabaseError):
                mem._conn.execute(f"SELECT * FROM {table}").fetchall()
    assert catalog.get_state(BID) == state
    assert report["additional_exposure_usd"] == pytest.approx(sum(
        usd_of(MODEL, {"in": row["input_tokens"], "out": 0}) for row in catalog.get_costs(BID)
        if row["model"] == MODEL))
    charges, calls = copy.deepcopy(catalog.get_costs(BID)), copy.deepcopy(client.calls)
    assert run(env)["already_indexed"] == 1
    assert catalog.get_costs(BID) == charges and client.calls == calls


def test_atomic_publish_failure_keeps_prefix_and_resumes_paid_stages_without_any_io(setup, monkeypatch):
    env = setup()
    store, catalog, client, settings, atoms = env
    with store.book(BID) as mem:
        original = mem.add_chunk
        count = 0

        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("publication interrupted")
            return original(*args, **kwargs)

        monkeypatch.setattr(mem, "add_chunk", fail_second)
    with pytest.raises(RuntimeError, match="interrupted"):
        run(env)
    charges, calls = copy.deepcopy(catalog.get_costs(BID)), copy.deepcopy(client.calls)
    with store.book(BID) as mem:
        assert mem.source_index_snapshot(atoms[0]["key"], 1)["staged_batches"]
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]
        live = [row for row in mem._audit_all("chunks") if row["retracted_at"] is None]
        assert len(live) == 2 and all(DEEP not in row["text"] for row in live)
        assert all(DEEP not in hit[1] for hit in mem.view(1).search([1, 0, 0], k=10))
        monkeypatch.setattr(mem, "add_chunk", original)
    report = run(env)
    assert report["reused_batches"] > 0 and report["paid_batches"] == report["canary_calls"] == 0
    assert catalog.get_costs(BID) == charges and client.calls == calls


def test_crash_after_provider_before_stage_never_automatically_charges_again(setup, monkeypatch):
    env = setup()
    store, catalog, client, _settings, atoms = env
    with store.book(BID) as mem:
        monkeypatch.setattr(mem, "stage_source_index_batch",
                            lambda *args: (_ for _ in ()).throw(RuntimeError("stage crash")))
    with pytest.raises(RuntimeError, match="stage crash"):
        run(env)
    charges, calls = copy.deepcopy(catalog.get_costs(BID)), copy.deepcopy(client.calls)
    monkeypatch.undo()
    with pytest.raises(IndexRecoveryRequired, match="not be charged again"):
        run(env)
    assert catalog.get_costs(BID) == charges and client.calls == calls
    with store.book(BID) as mem:
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]


def test_known_model_mismatch_and_missing_canary_fail_before_paid_io(setup):
    env = setup()
    store, _catalog, client, _settings, _atoms = env
    client.embed_identity = lambda: "some-other-model"
    with pytest.raises(versioning.ModelMigrationRequired):
        run(env)
    assert client.calls == []
    client.embed_identity = lambda: MODEL
    with store.book(BID) as mem:
        with mem._writer():
            mem._conn.execute("UPDATE book_meta SET embed_canary=NULL")
    with pytest.raises(versioning.ModelMigrationRequired):
        run(env)
    assert client.calls == []


def test_same_name_canary_drift_records_only_canary_and_preserves_prefix(setup):
    env = setup()
    store, catalog, _client, _settings, atoms = env
    with store.book(BID) as mem:
        with mem._writer():
            mem._conn.execute("UPDATE book_meta SET embed_canary=?", (json.dumps([1, 0, 0]),))
    with pytest.raises(versioning.ModelMigrationRequired):
        run(env)
    assert [row["phase"] for row in catalog.get_costs(BID) if row["model"] == MODEL] == ["source-index-canary"]
    with store.book(BID) as mem:
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]


def test_input_and_incremental_usd_ceilings_refuse_before_provider_io(setup):
    env = setup(input_limit=100)
    with pytest.raises(CostCeilingExceeded):
        run(env)
    assert env[2].calls == []
    env = setup()
    with pytest.raises(CostCeilingExceeded):
        run(env, max_additional_usd=1e-9)
    assert env[2].calls == []


def test_existing_book_token_ceiling_stops_backfill_without_complete_marker(setup):
    env = setup()
    store, catalog, client, settings, atoms = env
    settings.cost_max_input_tokens_per_book = 100
    with pytest.raises(CostCeilingExceeded):
        run(env)
    assert len(client.calls) == 1  # only the separately budgeted canary fit
    assert not catalog.get_cost_reservations(BID)
    with store.book(BID) as mem:
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]


def test_frontier_uses_smallest_catalog_and_durable_boundary(setup):
    env = setup(bookmark=2, progress=1)
    assert run(env, through=2)["through"] == 1
    assert not any("Future hidden" in text for call in env[2].calls for text in call)


def test_future_ingest_prepares_all_passages_and_commits_them_with_completion_receipt(tmp_path):
    client = LLMClient(provider="stub", allow_stub=True, env={})
    text = "ordinary notes " * 400 + DEEP
    ch = {"key": "b:c1", "ordinal": 1, "title": "An informative title", "text": text, "href": "c1"}
    extraction = {"chapter_summary": "Notes are read.", "entities": [], "relationships": [], "events": [], "themes": []}
    inputs = []

    def embed(texts):
        inputs.extend(texts)
        return client.embed(texts)[0]

    prepared = prepare_chapter(ch, extraction, client, roster=[], embed_fn=embed,
                               embedding_input_limit=3200)
    assert DEEP in inputs[-1] and len(inputs) > 1
    assert all(value.startswith("Chapter: An informative title\n\n") for value in inputs)
    store = Store(str(tmp_path))
    try:
        with store.book("b") as mem:
            ingest_chapter(mem, ch, prepared)
            assert mem.chapter_completion(ch["key"], 1, content_hash_of(text))
            assert mem.source_index_snapshot(ch["key"], 1)["complete"]
            assert len(mem._audit_all("chunks")) == len(list(passage_windows(text)))
    finally:
        store.close()


def test_provider_usage_overflow_records_exposure_and_never_publishes_or_replays(setup):
    env = setup()
    store, catalog, client, settings, atoms = env
    original = client.embed

    def overflow(texts):
        values, usage = original(texts)
        if texts != [versioning.EMBED_CANARY_TEXT]:
            usage = {"in": sum(len(text.encode("utf-8")) for text in texts) + 33}
        return values, usage

    client.embed = overflow
    with pytest.raises(CostCeilingExceeded, match="provider usage exceeded"):
        run(env)
    charges, calls = copy.deepcopy(catalog.get_costs(BID)), copy.deepcopy(client.calls)
    assert any(row["phase"].endswith("provider-limit-violation") for row in charges)
    assert not catalog.get_cost_reservations(BID)
    with store.book(BID) as mem:
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]
    with pytest.raises(IndexRecoveryRequired):
        run(env)
    assert catalog.get_costs(BID) == charges and client.calls == calls


def test_v6_upgrade_preserves_legacy_chunks_pins_and_receipts_without_embedding_io(setup):
    env = setup()
    store, catalog, client, settings, atoms = env
    with store.book(BID) as mem:
        before = {table: [dict(row) for row in mem._audit_all(table)] for table in
                  ("chapters", "ingested_chapters", "raw_chapters", "chapter_summaries")}
        pin = mem.pinned_identity()
    store.close()
    connection = sqlite3.connect(store._path(BID))
    try:
        connection.execute("DROP TABLE retrieval_index_chapters")
        connection.execute("DROP TABLE retrieval_index_batches")
        for column in ("source_version", "source_hash", "source_start", "source_end"):
            connection.execute(f"ALTER TABLE chunks DROP COLUMN {column}")
        connection.execute("UPDATE book_meta SET schema_version=6")
        connection.commit()
    finally:
        connection.close()
    with store.book(BID) as mem:
        assert mem.pinned_identity() == pin
        for table, rows in before.items():
            assert [dict(row) for row in mem._audit_all(table)] == rows
        assert mem.view(1).search([1, 0, 0], k=1)
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]
        assert all(row["source_version"] is None for row in mem._audit_all("chunks"))
    assert client.calls == []
    assert catalog.get_book(BID)["schema_version"] == migrations.CURRENT_VERSION


def test_source_window_mismatch_and_vector_mismatch_cannot_retire_old_prefix(setup):
    env = setup()
    store, _catalog, _client, _settings, atoms = env
    with store.book(BID) as mem:
        source = mem.source_index_snapshot(atoms[0]["key"], 1)
        pin = mem.pinned_identity()
        passages = [{**item, "vec": [1, 0, 0]} for item in source["passages"]]
        for bad in ({**passages[0], "text": "invented excerpt"},
                    {**passages[0], "vec": [1, 0]}, {**passages[0], "vec": [float("nan"), 0, 1]}):
            with pytest.raises(ValueError):
                mem.publish_source_index(atoms[0]["key"], 1, source["source_hash"],
                                         [bad, *passages[1:]], pin)
            assert all(DEEP not in hit[1] for hit in mem.view(1).search([1, 0, 0], k=10))
        assert not mem.source_index_snapshot(atoms[0]["key"], 1)["complete"]


@pytest.mark.parametrize("text", ["", "Aldric read a completed nonempty chapter."],
                         ids=["genuinely-empty", "nonempty-source"])
def test_zero_count_marker_cannot_certify_missing_nonempty_source_windows(setup, text):
    store, _catalog, client, _settings, atoms = setup(text=text)
    key = atoms[0]["key"]
    with store.book(BID) as mem:
        source = mem.source_index_snapshot(key, 1)
        pin = mem.pinned_identity()
        # Simulate corrupted/restored derived metadata, bypassing the normal publisher which
        # correctly refuses incomplete coverage. The extracted source/receipt itself stays intact.
        with mem._writer():
            mem._conn.execute("DELETE FROM chunks WHERE book_id=? AND chapter_key=?", (BID, key))
            mem._conn.execute(
                "INSERT INTO retrieval_index_chapters(chapter_key,book_id,revealed_at,index_version,"
                "source_hash,embed_model,embed_dim,chunk_count,completed_at,retracted_at) "
                "VALUES (?,?,?,?,?,?,?,0,?,NULL)",
                (key, BID, 1, PASSAGE_INDEX_VERSION, source["source_hash"], pin["embed_model"],
                 pin["embed_dim"], "2026-10-15T00:00:00+00:00"),
            )
        checked = mem.source_index_snapshot(key, 1)
        assert checked["complete"] is (not bool(source["passages"]))
        assert mem.view(1).raw_text(key) == text
        assert mem.chapter_completion(key, 1, content_hash_of(text)) is not None
    assert client.calls == [], "readiness checks must not repair or call an embedding provider"


@pytest.mark.parametrize("wrong_ordinal", [0, 2], ids=["too-early", "too-late"])
@pytest.mark.parametrize("keep_marker", [True, False], ids=["local-marker", "portable-no-marker"])
def test_complete_canonical_source_windows_require_the_exact_chapter_stamp(
        setup, wrong_ordinal, keep_marker):
    store, _catalog, client, _settings, atoms = setup()
    key = atoms[0]["key"]
    with store.book(BID) as mem:
        source = mem.source_index_snapshot(key, 1)
        pin = mem.pinned_identity()
        mem.publish_source_index(key, 1, source["source_hash"],
                                 [{**passage, "vec": [1, 0, 0]} for passage in source["passages"]],
                                 pin)
        assert mem.source_index_snapshot(key, 1)["complete"]
        with mem._writer():
            if not keep_marker:
                mem._conn.execute(
                    "DELETE FROM retrieval_index_chapters WHERE book_id=? AND chapter_key=?",
                    (BID, key),
                )
            mem._conn.execute(
                "UPDATE chunks SET revealed_at=? WHERE book_id=? AND chapter_key=? "
                "AND retracted_at IS NULL", (wrong_ordinal, BID, key),
            )
        checked = mem.source_index_snapshot(key, 1)
        assert not checked["complete"], "source offsets/hash alone cannot certify a wrong reveal stamp"
        assert mem.view(1).raw_text(key) == source["text"]
        assert mem.chapter_completion(key, 1, content_hash_of(source["text"])) is not None
    assert client.calls == [], "canonical readiness validation must be provider-free"


def test_exact_and_portable_backups_preserve_full_coverage_without_reembedding(tmp_path):
    from tests.lifecycle.test_archive import _seed
    from app.lifecycle.archive import backup_book, read_portable_archive, restore_book

    source = tmp_path / "source"
    store, catalog, bid = _seed(source, book_id=BID)
    try:
        with store.book(bid) as mem:
            mem.pin_models(embed_model=MODEL, embed_dim=3, embed_canary=[0, 0, 1])
            snapshot = mem.source_index_snapshot(f"{bid}:chapter-1", 1)
            mem.publish_source_index(f"{bid}:chapter-1", 1, snapshot["source_hash"],
                                     [{**item, "vec": [1, 0, 0]} for item in snapshot["passages"]],
                                     mem.pinned_identity())
        archive = tmp_path / "indexed.rcbackup"
        backup_book(source, bid, archive)
        bundle = read_portable_archive(archive)
        assert "source_start" in bundle.export["memory"]["chunks"]["columns"]
        assert "retrieval_index_batches" not in bundle.export["memory"]
        for portable in (False, True):
            target = tmp_path / ("portable" if portable else "exact")
            restore_book(archive, target, portable=portable)
            restored = Store(str(target))
            restored_catalog = Catalog(str(target / "catalog.db"))
            client = SemanticDouble()
            settings = Settings(_env_file=None, data_dir=str(target), allow_stub=True)
            try:
                report = backfill_source_index(restored, restored_catalog, client, settings, bid,
                                                max_additional_usd=0.10)
                assert report["already_indexed"] == 1 and client.calls == []
                with restored.book(bid) as mem:
                    assert mem.source_index_snapshot(f"{bid}:chapter-1", 1)["complete"]
                    assert mem.view(1).search([1, 0, 0], k=1)[0][1] == snapshot["passages"][0]["text"]
            finally:
                restored.close()
                restored_catalog.close()
    finally:
        store.close()
        catalog.close()


@pytest.mark.parametrize("fail", [False, True], ids=["success", "provider-failure"])
def test_cli_disables_sdk_retries_and_reports_incremental_costs_on_all_exit_paths(setup, monkeypatch,
                                                                                tmp_path, fail):
    from app.llm.client import ProviderUnavailable
    from app.retrieval_index import __main__ as command

    env = setup()
    _store, catalog, client, settings, _atoms = env
    original = client.embed
    if fail:
        def reject_source(texts):
            if texts != [versioning.EMBED_CANARY_TEXT]:
                client.calls.append(list(texts))
                raise ProviderUnavailable("auth", service="embedding")
            return original(texts)
        client.embed = reject_source
    constructed = []

    def create_client(**kwargs):
        constructed.append(kwargs["max_retries"])
        return client

    monkeypatch.setattr(command, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(command, "LLMClient", create_client)
    destination = tmp_path / "cli-report.json"
    exit_code = command.main([
        "backfill", "--data-dir", settings.data_dir, "--book-id", BID,
        "--through", "99", "--max-additional-usd", "0.10", "--report", str(destination),
    ])
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert constructed == [0]
    assert exit_code == (1 if fail else 0)
    assert report["historical_usd"] == pytest.approx(0.002)
    assert report["costs"] and all(row["model"] == MODEL for row in report["costs"])
    assert report["additional_usd"] == pytest.approx(sum(row["usd"] for row in report["costs"]))
    assert report["additional_exposure_usd"] == pytest.approx(report["additional_usd"])
    assert report["pending_reservations"] == []
    assert report["costs"] == catalog.get_costs(BID)[2:]
    if fail:
        assert report["error_type"] == "ProviderUnavailable"
        assert report["detail"]["code"] == "provider_unavailable"
        assert report["costs"][-1]["phase"].endswith("failed-reserved")
    else:
        assert report["status"] == "completed" and report["through"] == 1
