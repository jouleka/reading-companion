"""The ingestion WORKER (ADR 0007 D-A3 + LIT-7): turns a bookmark advance into
per-chapter Module C ingestion, off the request path, serialized per book, with the LLM strictly
OUTSIDE the per-book lock:

    (1) read inputs under the lock  ->  (2) release + LLM/embedding work  ->  (3) short commit under
    the lock. ``_engaged``/the lock are never held across IO.

The preparation phase computes model identity, the RAG chunk vector, and layer-4 resolution through a
per-chapter ``_MemoEmbed`` outside the lock. The commit API then accepts prepared values only: no model,
embedding, resolver, or catalog callback is reachable while ``Store.book()`` is held.

GATES (fail closed, surfaced via GET /ingest — never silently skipped):
  * a segmentation flag of the COVERAGE-GAP / ANCHOR-RESOLUTION-FAILURE class BLOCKS ingestion
    (facts stamped against a wrong atom could leak a later chapter's prose — the ADR-routed gate);
  * the fresh segmentation of the stored source.epub must MATCH the import-time manifest by
    ``(ordinal, key, char_len)`` (the worker-side D-A10 atom-set check) — drift blocks.

Crash-resume (LIT-7): every chapter's memory rows and append-once marker commit in one transaction.
A re-run skips fully committed chapters and retries an interrupted chapter from zero derived rows.
Malformed structured output is schema-validated before any write, retried once after bounded backoff,
then surfaced as an error; the next enqueue resumes at the same chapter.

Concurrency: single-flight per book — one running task per book_id; a new enqueue only raises the
target the running task re-checks each loop. Different books ingest in parallel (the executor pool).
"""

import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from app.cost import (
    budgeted_completion,
    budgeted_embedding,
    merge_extractions,
    split_text_for_prompt,
    usd_of,
)
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.extraction.pipeline import all_entities, content_hash_of, ingest_chapter, prepare_chapter
from app.ingest.extraction.prompts import extract_system_for, extract_user_prompt
from app.ingest.extraction.schema import Extraction
from app.ingest.manifest import load_manifest
from app.llm import versioning

_EMBED_CHARS = 2000                              # pinned to the ingest_chapter call below — the memo
_RESOLVE_THRESHOLD = 0.82                        # is warmed with EXACTLY the texts the lock phase uses
_MALFORMED_RETRY_DELAYS = (0.25,)                # initial attempt + one bounded corrective retry


class _MemoEmbed:
    """Per-chapter memoizing wrapper over ``client.embed`` (signature ``texts -> vecs``): every text
    is embedded ONCE, outside the lock, during the warm-up; the locked commit phase then only takes
    cache HITS — no network IO under the per-book lock (review pass-1 BLOCKER: the chunk embed and the
    layer-4 resolution embeds ran inside ``ingest_chapter``'s locked section). The memo is sealed before
    entering the lock, so an unexpected miss fails closed instead of silently performing network IO."""

    def __init__(self, client, embed_call=None):
        self._client = client
        self._embed_call = embed_call or client.embed
        self._cache = {}
        self._sealed = False

    def seal(self):
        self._sealed = True

    def __call__(self, texts):
        missing = [t for t in texts if t not in self._cache]
        if missing:
            if self._sealed:
                raise RuntimeError("sealed embedding memo cache miss during lock/commit phase")
            vecs = self._embed_call(missing)[0]
            self._cache.update(zip(missing, vecs))
        return [self._cache[t] for t in texts]

_GATING = ("coverage gap", "anchor resolution")


class _LifecycleLockEntry:
    """A per-book lifecycle lock plus references held by both holders and blocked waiters."""

    def __init__(self):
        self.lock = threading.RLock()
        self.refcount = 0


def _usd_of(model, usage):
    """Compatibility seam retained for callers/tests; LIT-21 owns the pricing table."""
    return usd_of(model, usage)


class IngestWorker:
    def __init__(self, store, catalog, client, settings, executor=None, sleep=None):
        self._store = store
        self._catalog = catalog
        self._client = client
        self._settings = settings
        self._own_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest")
        self._sleep = sleep or time.sleep
        self._mu = threading.Lock()                  # guards _state/_running (not the per-book lock)
        self._state = {}                             # book_id -> {status, error, flags, target, generation}
        self._running = set()
        self._verified = {}                          # book_id -> (incarnation, generation, validated ordinal)
        self._lifecycle_locks = {}                   # book_id -> lifecycle lock entry
        self._segcache_max = getattr(settings, "segmentation_cache_max_entries", 8)
        self._segcache = OrderedDict()                # (book_id, incarnation) -> (result, chapters)
        self._segcache_mu = threading.Lock()

    # ---- public ------------------------------------------------------------
    def enqueue(self, book_id, target_ordinal):
        """Called by PUT /position to validate/ingest through the bookmark. Single-flight: raises the
        target of a running task instead of stacking a second one."""
        submit = False
        with self.book_lifecycle(book_id):
            book = self._catalog.get_book(book_id) if self._catalog is not None else None
            incarnation = book["incarnation"] if book is not None else None
            with self._mu:
                st = self._state.setdefault(book_id, {"status": "idle", "error": None,
                                                      "flags": [], "target": 0, "generation": 0,
                                                      "incarnation": incarnation})
                if st["incarnation"] != incarnation:
                    st = {"status": "idle", "error": None, "flags": [], "target": 0,
                          "generation": 0, "incarnation": incarnation}
                    self._state[book_id] = st
                    self.invalidate_book(book_id)
                target_ordinal = int(target_ordinal)
                st["target"] = max(st["target"], target_ordinal)
                st["generation"] += 1
                self._verified[book_id] = (incarnation, st["generation"], 0)
                st["error"] = None
                st["flags"] = []
                st["status"] = "running"
                if book_id not in self._running:
                    self._running.add(book_id)
                    submit = True
                generation, incarnation = st["generation"], st["incarnation"]
        if submit:
            self._submit(book_id, generation, incarnation)

    def _submit(self, book_id, generation, incarnation):
        while True:
            try:
                self._executor.submit(self._run, book_id)
                return
            except Exception as e:
                successor = self._after_failure(
                    book_id, generation, incarnation, f"submit failed: {type(e).__name__}: {e}"
                )
                if successor is None:
                    return
                generation, incarnation = successor

    def _after_failure(self, book_id, generation, incarnation, error):
        with self._mu:
            st = self._state[book_id]
            self._running.discard(book_id)
            if st["incarnation"] != incarnation or st["generation"] > generation:
                st["status"], st["error"] = "running", None
                self._running.add(book_id)
                return st["generation"], st["incarnation"]
            st["status"], st["error"] = "error", error
            return None

    def status(self, book_id):
        book = self._catalog.get_book(book_id) if self._catalog is not None else None
        incarnation = book["incarnation"] if book is not None else None
        with self._mu:
            st = dict(self._state.get(book_id) or {"status": "idle", "error": None, "flags": []})
        if st.get("incarnation") != incarnation:
            return {"status": "idle", "error": None, "flags": []}
        st.pop("target", None)
        st.pop("generation", None)
        st.pop("incarnation", None)
        return st

    def validated_frontier(self, book_id):
        """Highest contiguous marker-validated ordinal for this catalog incarnation."""
        book = self._catalog.get_book(book_id)
        if book is None:
            return 0
        with self._mu:
            cached = self._verified.get(book_id)
            state = self._state.get(book_id)
            if (cached is None or state is None or cached[0] != book["incarnation"]
                    or cached[1] != state["generation"]):
                return 0
            return cached[2]

    def _require_incarnation(self, book_id, incarnation):
        if self._catalog is None:
            return
        book = self._catalog.get_book(book_id)
        if book is None or book["incarnation"] != incarnation:
            raise RuntimeError(f"stale catalog incarnation for book {book_id!r}")
        self._require_worker_incarnation(book_id, incarnation)

    def _require_worker_incarnation(self, book_id, incarnation):
        with self._mu:
            if self._state[book_id]["incarnation"] != incarnation:
                raise RuntimeError(f"stale worker incarnation for book {book_id!r}")

    def _publish_validated(self, book_id, incarnation, generation, ordinal):
        self._require_incarnation(book_id, incarnation)
        with self._mu:
            state = self._state[book_id]
            if state["incarnation"] != incarnation or state["generation"] != generation:
                raise RuntimeError(f"stale worker generation for book {book_id!r}")
            self._verified[book_id] = (incarnation, generation, ordinal)

    @contextmanager
    def book_lifecycle(self, book_id):
        """Order enqueue/worker commit against deletion without nesting either database lock."""
        with self._mu:
            entry = self._lifecycle_locks.get(book_id)
            if entry is None:
                entry = self._lifecycle_locks[book_id] = _LifecycleLockEntry()
            entry.refcount += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._mu:
                entry.refcount -= 1
                if entry.refcount == 0 and self._lifecycle_locks.get(book_id) is entry:
                    del self._lifecycle_locks[book_id]

    def shutdown(self):
        if self._own_executor:
            self._executor.shutdown(wait=True)

    def invalidate_book(self, book_id):
        """Drop segmentation results for every incarnation of one book (delete/re-import hook)."""
        with self._segcache_mu:
            for key in [candidate for candidate in self._segcache if candidate[0] == book_id]:
                del self._segcache[key]

    def segmentation_cache_size(self):
        with self._segcache_mu:
            return len(self._segcache)

    # ---- the worker task -----------------------------------------------------
    def _run(self, book_id):
        """The exit decision and the ``_running`` discard are ONE ``_mu`` critical section (review
        pass-1 HIGH — the lost wakeup): an ``enqueue`` that raises the target either lands before the
        section (the loop re-checks and continues) or after it (the book is no longer in ``_running``,
        so the enqueue submits a fresh task). There is no window in which a raised target is dropped
        with ``status='done'``. If a newer enqueue generation arrives during a failing call, it is
        resubmitted exactly once; the same failed generation is never blindly retried."""
        generation = -1
        incarnation = None
        try:
            while True:
                with self._mu:
                    target = self._state[book_id]["target"]
                    generation = self._state[book_id]["generation"]
                    incarnation = self._state[book_id]["incarnation"]
                validated = self._ingest_upto(book_id, target, incarnation, generation)
                with self._mu:
                    state = self._state[book_id]
                    if state["incarnation"] != incarnation or state["generation"] != generation:
                        continue
                    if validated is None:                         # blocked (status already set)
                        self._running.discard(book_id)
                        return
                    if self._state[book_id]["target"] <= validated:
                        self._state[book_id]["status"] = "done"
                        self._state[book_id]["error"] = None      # a successful run clears stale errors
                        self._running.discard(book_id)
                        return
        except Exception as e:                       # surface, never crash the pool silently
            successor = self._after_failure(
                book_id, generation, incarnation, f"{type(e).__name__}: {e}"
            )
            if successor is not None:
                self._submit(book_id, *successor)

    def _segmented(self, book_id, incarnation=None):
        if incarnation is None and self._catalog is not None:
            book = self._catalog.get_book(book_id)
            incarnation = book["incarnation"] if book is not None else None
        cache_key = (book_id, incarnation)
        with self._segcache_mu:
            cached = self._segcache.get(cache_key)
            if cached is not None:
                self._segcache.move_to_end(cache_key)
                return cached
        src = os.path.join(self._settings.data_dir, "books", book_id, "source.epub")
        segmented = segment_for_ingest(src, book_id)       # expensive parse stays outside cache lock
        with self._segcache_mu:
            cached = self._segcache.get(cache_key)
            if cached is not None:
                self._segcache.move_to_end(cache_key)
                return cached
            self._segcache[cache_key] = segmented
            while len(self._segcache) > self._segcache_max:
                self._segcache.popitem(last=False)
            return segmented

    def _gate(self, book_id, result, chapters):
        """The fail-closed pre-ingest gates. Returns the blocking reason or None."""
        gating = [str(f) for f in result.flags if any(g in str(f).lower() for g in _GATING)]
        if gating:
            return gating
        manifest = load_manifest(self._settings.data_dir, book_id)
        if (result.content_language != manifest["content_language"]
                and manifest["_content_language_recorded"]):
            return ["ATOM-SET MISMATCH: the stored source.epub declares a different content "
                    "language than the import-time manifest (D-A10) — re-import the book"]
        fresh = [(c["ordinal"], c["key"], len(c.get("text", "") or "")) for c in chapters]
        want = [(a["ordinal"], a["key"], a["char_len"]) for a in manifest["atoms"]]
        if fresh != want:
            return ["ATOM-SET MISMATCH: the stored source.epub segments differently than the "
                    "import-time manifest (D-A10) — re-import the book"]
        return None

    def _complete_extraction(self, book_id, ordinal, title, roster, text, *, book_type="novel",
                             content_language="und"):
        """Chunk huge chapters, budget every call, and merge validated parts before any write."""
        system = extract_system_for(book_type, content_language)
        empty_prompt = extract_user_prompt(
            title, roster, "", book_type=book_type, content_language=content_language
        )
        chunk_input_limit = self._settings.cost_max_input_tokens_per_call
        if (self._client.provider == "openai-compatible"
                and not getattr(self._client, "_is_native_openai", False)):
            chunk_input_limit -= self._settings.cost_max_output_tokens_per_call + 512
        chunks = split_text_for_prompt(
            text,
            prompt_without_text=empty_prompt,
            system=system,
            max_input_tokens=chunk_input_limit,
            schema=Extraction,
        )
        parts, reservations = [], []
        usage_total = {"in": 0, "out": 0}
        usd_total = 0.0
        try:
            for chunk in chunks:
                prompt = extract_user_prompt(
                    title, roster, chunk, book_type=book_type,
                    content_language=content_language,
                )
                for attempt in range(len(_MALFORMED_RETRY_DELAYS) + 1):
                    result = None
                    try:
                        result = budgeted_completion(
                            self._catalog,
                            self._settings,
                            self._client,
                            book_id,
                            phase="extraction",
                            system=system,
                            user=prompt,
                            tier="cheap",
                            schema=Extraction,
                            chapter_ordinal=ordinal,
                            defer_settlement=True,
                        )
                        validated = Extraction.model_validate(result.value).model_dump(mode="json")
                        parts.append(validated)
                        reservations.append(result.reservation_id)
                        usage_total["in"] += result.usage["in"]
                        usage_total["out"] += result.usage["out"]
                        usd_total += result.usd
                        break
                    except (TypeError, ValueError) as exc:
                        if result is not None:
                            self._catalog.settle_cost(
                                book_id, result.reservation_id, phase="extraction-invalid"
                            )
                        if attempt == len(_MALFORMED_RETRY_DELAYS):
                            raise ValueError(
                                f"malformed extraction after {attempt + 1} attempts: {exc}"
                            ) from exc
                        self._sleep(_MALFORMED_RETRY_DELAYS[attempt])
            return merge_extractions(parts), usage_total, usd_total, reservations
        except Exception:
            for reservation_id in reservations:
                self._catalog.settle_cost(book_id, reservation_id, phase="extraction-partial")
            raise

    def _ingest_upto(self, book_id, target, incarnation=None, generation=None):
        """Validate/ingest chapters through ``target`` per D-A3. Returns the validated frontier reached
        in this call—not potentially stale catalog progress—or None when blocked."""
        if incarnation is None:
            incarnation = self._catalog.get_book(book_id)["incarnation"]
        if generation is None:
            with self._mu:
                generation = self._state[book_id]["generation"]
        self._require_incarnation(book_id, incarnation)
        self._publish_validated(book_id, incarnation, generation, 0)
        result, chapters = self._segmented(book_id, incarnation)
        blocked = self._gate(book_id, result, chapters)
        self._require_incarnation(book_id, incarnation)
        highest = max((ch["ordinal"] for ch in chapters), default=0)
        if target > highest:
            raise ValueError(f"ingest target {target} exceeds final chapter ordinal {highest}")
        if blocked:
            with self._mu:
                state = self._state[book_id]
                if state["incarnation"] == incarnation and state["generation"] == generation:
                    state["status"] = "blocked"
                    state["flags"] = blocked
            return None
        identity = None
        real_embed = None
        done = self._catalog.get_state(book_id)["ingest_progress"]
        validated = 0
        for ch in chapters:
            if ch["ordinal"] > target:
                continue
            self._require_incarnation(book_id, incarnation)
            text = ch.get("text", "") or ""
            content_hash = content_hash_of(text)
            if ch.get("content_hash") not in (None, content_hash):
                raise ValueError(f"chapter {ch['key']!r} content_hash does not match its text")
            roster = []
            book_type = "novel"
            content_language = "und"
            with self._store.book(book_id) as mem:                       # (1) inputs under the lock
                receipt = mem.chapter_completion(ch["key"], ch["ordinal"], content_hash)
                book_type = mem.book_profile()["book_type"]
                content_language = mem.content_language()
                if receipt is None:
                    roster = all_entities(mem.view(max(ch["ordinal"] - 1, 0)))
            if receipt is not None:
                self._catalog.finalize_ingest(
                    book_id, ch["ordinal"], cost=receipt["cost"], incarnation=incarnation
                )
                done = max(done, ch["ordinal"])
                self._publish_validated(book_id, incarnation, generation, ch["ordinal"])
                validated = ch["ordinal"]
                continue
            if self._catalog.cost_reservation_ids(
                book_id, phase="extraction", chapter_ordinal=ch["ordinal"]
            ):
                raise RuntimeError(
                    f"chapter {ch['ordinal']} has outstanding extraction cost reservations from an "
                    "interrupted attempt; run the explicit cost status/reconcile command before retrying"
                )
            if ch["ordinal"] <= done:
                raise RuntimeError(
                    f"catalog ingest_progress={done} but chapter {ch['key']!r} has no matching LIT-7 "
                    "completion marker — re-import/rebuild the book rather than trusting legacy partial state"
                )
            if identity is None:
                def embed_call(texts):
                    result = budgeted_embedding(
                        self._catalog, self._settings, self._client, book_id,
                        phase="embedding", texts=texts, chapter_ordinal=ch["ordinal"],
                    )
                    return result.value, result.usage

                identity = versioning.current_identity(self._client, embed_call=embed_call)
                real_embed = not self._client.embed_identity().startswith("stub:")
            extraction, usage, usd, reservation_ids = self._complete_extraction(
                book_id, ch["ordinal"], ch.get("title", ""), roster, text, book_type=book_type,
                content_language=content_language,
            )                                                           # (2) ALL IO OUTSIDE the lock
            memo = _MemoEmbed(self._client, embed_call=embed_call)
            try:
                prepared = prepare_chapter(
                    ch,
                    extraction,
                    self._client,
                    roster=roster,
                    usage=usage,
                    usd=usd,
                    embed_fn=memo,
                    resolve_embed=memo if real_embed else None,
                    identity=identity,
                    threshold=_RESOLVE_THRESHOLD,
                    embed_chars=_EMBED_CHARS,
                )
            except Exception:
                for reservation_id in reservation_ids:
                    self._catalog.settle_cost(
                        book_id, reservation_id, phase="extraction-preparation-failed"
                    )
                raise
            memo.seal()
            committed = False
            try:
                with self.book_lifecycle(book_id):                       # order commit against deletion
                    self._require_incarnation(book_id, incarnation)
                    with self._store.book(book_id) as mem:               # (3) short commit under it
                        self._require_worker_incarnation(book_id, incarnation)
                        ingest_chapter(mem, ch, prepared)
                        receipt = mem.chapter_completion(ch["key"], ch["ordinal"], content_hash)
                        if receipt is None:
                            raise RuntimeError(
                                f"chapter {ch['key']!r} committed without its LIT-7 completion marker"
                            )
                        committed = True
            except Exception:
                if not committed:
                    for reservation_id in reservation_ids:
                        self._catalog.settle_cost(
                            book_id, reservation_id, phase="extraction-commit-aborted"
                        )
                raise
            self._require_incarnation(book_id, incarnation)
            self._catalog.finalize_ingest(
                book_id, ch["ordinal"], cost=receipt["cost"], incarnation=incarnation
            )
            done = ch["ordinal"]
            self._publish_validated(book_id, incarnation, generation, ch["ordinal"])
            validated = ch["ordinal"]
        self._require_incarnation(book_id, incarnation)
        return validated
