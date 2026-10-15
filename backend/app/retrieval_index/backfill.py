"""Resume a derived source index without re-extracting facts or changing their model pins."""
from __future__ import annotations

import hashlib
import json
import math

from app.cost import CostCeilingExceeded, budgeted_embedding, pricing_known
from app.ingest.manifest import assert_matches_store, load_manifest, validated_book_id
from app.llm import versioning
from app.memory.vectors import normalize
from app.retrieval_passages import embedding_batches, embedding_text


class IndexRecoveryRequired(RuntimeError):
    """A previous paid batch has no recoverable result; never automatically pay for it again."""

    def public_detail(self):
        return {"code": "source_index_recovery_required", "message": str(self)}


def _phase_prefix(source_hash):
    return f"source-index:{source_hash[:16]}:"


def _batch_id(source_hash, model, passages):
    payload = [source_hash, model, [(item["start"], item["end"]) for item in passages]]
    digest = hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()[:24]
    return _phase_prefix(source_hash) + digest


def _exposure(catalog, book_id):
    return (sum(row["usd"] for row in catalog.get_costs(book_id))
            + sum(row["reserved_usd"] for row in catalog.get_cost_reservations(book_id)))


def backfill_source_index(store, catalog, client, settings, book_id, *, max_additional_usd,
                          through=None):
    """Run with exclusive data-directory ownership (the CLI takes DataDirLock).

    Normal per-call and per-book token/USD limits remain active. The extra cap is an additional
    incremental ceiling, not a replacement for them. A durable paid stage is reused on rerun; a crash
    between provider settlement and staging fails closed instead of silently buying the batch twice.
    """
    validated_book_id(book_id)
    if (isinstance(max_additional_usd, bool) or not isinstance(max_additional_usd, (int, float))
            or not math.isfinite(max_additional_usd) or max_additional_usd <= 0):
        raise ValueError("max_additional_usd must be finite and positive")
    if settings.deployment_mode != "local":
        raise ValueError("full-source backfill supports local SQLite books only")
    book = catalog.get_book(book_id)
    state = catalog.get_state(book_id)
    if book is None or state is None:
        raise ValueError("unknown book")
    manifest = load_manifest(settings.data_dir, book_id)
    if through is None:
        through = state["bookmark"]
    if isinstance(through, bool) or not isinstance(through, int) or through < 0:
        raise ValueError("through must be a non-negative chapter ordinal")
    with store.book(book_id) as mem:
        assert_matches_store(manifest, mem)
        durable = mem.completion_frontier(manifest["atoms"])
        frontier = min(through, state["bookmark"], state["ingest_progress"], durable)
        pinned = mem.pinned_identity()
        chapters = [(atom, mem.source_index_snapshot(atom["key"], atom["ordinal"]))
                    for atom in manifest["atoms"] if atom["ordinal"] <= frontier]
    versioning.assert_compatible_config(pinned, client, check_extractor=False)
    if not pinned or not pinned.get("embed_model"):
        raise versioning.ModelMigrationRequired([("embed_model", versioning.FORCE_RE_EMBED)])
    versioning.assert_compatible_identity(pinned, pinned, check_extractor=False)
    if not pricing_known(client.embed_identity()):
        raise CostCeilingExceeded("source indexing requires a known embedding price for its USD cap")
    initial_exposure = _exposure(catalog, book_id)
    bounded = settings.model_copy(update={
        "cost_max_usd_per_book": min(settings.cost_max_usd_per_book,
                                     initial_exposure + max_additional_usd),
    })
    report = {"book_id": book_id, "through": frontier, "durable_frontier": durable,
              "already_indexed": 0, "indexed_chapters": 0, "indexed_passages": 0,
              "reused_batches": 0, "paid_batches": 0, "canary_calls": 0}
    identity = None
    for atom, source in chapters:
        if source["complete"]:
            report["already_indexed"] += 1
            continue
        # Plan all windows before the first paid call, including oversized-title refusal.
        embedding_batches(source["title"], source["passages"],
                          settings.cost_max_input_tokens_per_call)
        prefix = _phase_prefix(source["source_hash"])
        stages = {row["batch_id"]: row for row in source["staged_batches"]}
        costs = [row for row in catalog.get_costs(book_id)
                 if row["chapter_ordinal"] == atom["ordinal"] and row["phase"].startswith(prefix)]
        pending = [row for row in catalog.get_cost_reservations(book_id)
                   if row["chapter_ordinal"] == atom["ordinal"] and row["phase"].startswith(prefix)]
        if pending or any(row["phase"] not in stages for row in costs):
            raise IndexRecoveryRequired(
                "A previous source-index call has no staged result. Inspect its cost records and "
                "recover the interrupted batch explicitly; it will not be charged again automatically."
            )
        by_start = {}
        expected = {item["start"]: item for item in source["passages"]}
        for row in stages.values():
            if row["embed_model"] != pinned["embed_model"] or row["embed_dim"] != pinned["embed_dim"]:
                raise IndexRecoveryRequired("staged source vectors disagree with the book embedding pin")
            for passage in json.loads(row["passages_json"]):
                target = expected.get(passage.get("start"))
                if target is None or any(passage.get(key) != target[key] for key in ("start", "end", "text")):
                    raise IndexRecoveryRequired("staged source passage does not match current chapter text")
                if passage["start"] in by_start and by_start[passage["start"]] != passage:
                    raise IndexRecoveryRequired("conflicting staged source vectors require explicit recovery")
                try:
                    if len(normalize(passage.get("vec"))) != pinned["embed_dim"]:
                        raise ValueError("dimension mismatch")
                except (ValueError, TypeError) as exc:
                    raise IndexRecoveryRequired("staged source vector is invalid; recover it explicitly") from exc
                by_start[passage["start"]] = passage
            report["reused_batches"] += 1
        missing = [item for item in source["passages"] if item["start"] not in by_start]
        for batch in embedding_batches(source["title"], missing,
                                       settings.cost_max_input_tokens_per_call):
            if identity is None:
                def canary_embed(texts):
                    result = budgeted_embedding(catalog, bounded, client, book_id,
                                                phase="source-index-canary", texts=texts)
                    return result.value, result.usage
                identity = versioning.current_identity(client, embed_call=canary_embed)
                report["canary_calls"] += 1
                versioning.assert_compatible_identity(pinned, identity, check_extractor=False)
            phase = _batch_id(source["source_hash"], pinned["embed_model"], batch)
            result = budgeted_embedding(
                catalog, bounded, client, book_id, phase=phase,
                texts=[embedding_text(source["title"], item["text"]) for item in batch],
                chapter_ordinal=atom["ordinal"],
            )
            if len(result.value) != len(batch):
                raise IndexRecoveryRequired("source embedding batch returned the wrong vector count")
            prepared = [{**item, "vec": value} for item, value in zip(batch, result.value, strict=True)]
            with store.book(book_id) as mem:
                mem.stage_source_index_batch(atom["key"], atom["ordinal"], source["source_hash"],
                                              phase, prepared, identity)
            by_start.update((item["start"], item) for item in prepared)
            report["paid_batches"] += 1
        passages = [by_start[item["start"]] for item in source["passages"]]
        with store.book(book_id) as mem:
            mem.publish_source_index(atom["key"], atom["ordinal"], source["source_hash"],
                                     passages, identity or pinned)
        report["indexed_chapters"] += 1
        report["indexed_passages"] += len(passages)
    report["additional_exposure_usd"] = _exposure(catalog, book_id) - initial_exposure
    return report
