"""Bounded, source-scoped complete answers. Only the reviewed route can populate this bank."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

from app.ask import AskDraft
from app.eval.spoiler_gate import validity_snapshot
from app.preparation_plan import valid_saved_plan


ANSWER_VERSION = "reviewed-answer-v8-cited-question-bindings"


class AnswerBusy(RuntimeError):
    pass


def answer_scope(book_id, eff, state, manifest, *, catalog, store, settings, client):
    with store.book(book_id) as mem:
        snapshot = validity_snapshot(mem, eff)
    identity = [eff, state["position_epoch"], snapshot, manifest["atom_set_version"],
                client.extractor_version("large"), client.extractor_version("cheap"),
                getattr(client, "base_url", ""), settings.jev_selection_enabled,
                settings.jev_evidence_enabled, settings.jev_partial_evidence_enabled,
                getattr(getattr(client, "jev", None), "model", None),
                settings.jev_min_probability, settings.jev_min_confidence,
                settings.reader_reuse_min_probability, settings.reader_reuse_min_confidence,
                ANSWER_VERSION]
    identity.append(settings.reader_reuse_fast_review)
    return (book_id, catalog.get_book(book_id)["incarnation"],
            hashlib.sha256(json.dumps(identity).encode()).hexdigest())


def valid_answer(payload):
    try:
        eff = payload["as_of_chapter"]
        draft = {k: payload[k] for k in ("insufficient_evidence", "claims")}
        checked = AskDraft.model_validate(draft, strict=True)
        citations = payload["citations"]
        ids = [c["id"] for c in citations]
        return (type(eff) is int and eff > 0 and not checked.insufficient_evidence
                and checked.model_dump(mode="json") == draft
                and isinstance(citations, list) and 1 <= len(citations) <= 6
                and len(set(ids)) == len(ids)
                and all(type(c["id"]) is int and c["id"] > 0
                        and type(c["ordinal"]) is int and 1 <= c["ordinal"] <= eff
                        and all(isinstance(c.get(k), str) for k in ("chapter_key", "href", "title", "excerpt"))
                        and 0 < len(c["excerpt"]) <= 6000 for c in citations)
                and all(set(claim.citation_ids) <= set(ids) for claim in checked.claims))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


class AnswerBank:
    def __init__(self, *, max_entries=128, persist_path=None):
        self.limit = max_entries
        self.path = Path(persist_path) if persist_path is not None else None
        self.rows = OrderedDict()
        self.attempted = OrderedDict()
        self.plans = OrderedDict()
        self.inflight = {}
        self.lock = threading.Lock()
        self._load()

    def run_once(self, key, operation):
        """Share an in-flight result, including refusal/failure, without persisting failures.

        Waiters retain their Future after its owner removes the map entry. No provider IO or
        waiting holds the cache lock; different questions have a bounded concurrency limit.
        """
        with self.lock:
            future = self.inflight.get(key)
            owner = future is None
            if owner:
                if len(self.inflight) >= 8:
                    raise AnswerBusy("answer preparation is busy")
                future = self.inflight[key] = Future()
        if not owner:
            return deepcopy(future.result()), True
        try:
            result = operation()
            future.set_result(deepcopy(result))
            return result, False
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.inflight.pop(key, None)

    @staticmethod
    def _encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    def _load(self):
        if self.path is None:
            return
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                return
            data = json.loads(raw)
            rows = data["rows"]
            attempted = data.get("prepared", [])
            plans = data.get("plans", [])
            signed = [rows, attempted, plans] if "plans" in data else [rows, attempted]
            if (data["version"] != ANSWER_VERSION or not isinstance(rows, list)
                    or len(rows) > self.limit
                    or not isinstance(attempted, list) or len(attempted) > self.limit
                    or any(not isinstance(k, list) or len(k) != 3
                           or not all(isinstance(p, str) for p in k) for k in attempted)
                    or not isinstance(plans, list) or len(plans) > min(self.limit, 16)
                    or hashlib.sha256(self._encoded(signed)).hexdigest() != data["sha256"]):
                return
            for key, row in rows:
                if (not isinstance(key, list) or len(key) != 4
                        or not all(isinstance(k, str) for k in key)
                        or not 2 <= len(key[3]) <= 500
                        or not isinstance(row.get("reviewed_question"), str)
                        or not 2 <= len(row["reviewed_question"]) <= 500
                        or type(row.get("suggested", False)) is not bool
                        or not valid_answer(row["payload"])):
                    return
            for key, plan in plans:
                if (not isinstance(key, list) or len(key) != 3
                        or not all(isinstance(k, str) for k in key) or not valid_saved_plan(plan)):
                    return
            self.rows.update((tuple(k), v) for k, v in rows)
            self.attempted.update((tuple(k), True) for k in attempted)
            self.plans.update((tuple(k), v) for k, v in plans)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _save(self):
        if self.path is None:
            return
        temporary = self.path.with_suffix(".tmp")
        try:
            rows = [[list(k), v] for k, v in self.rows.items()]
            attempted = [list(k) for k in self.attempted]
            plans = [[list(k), v] for k, v in self.plans.items()]
            while True:
                raw = self._encoded({"version": ANSWER_VERSION, "rows": rows, "prepared": attempted,
                                     "plans": plans,
                                     "sha256": hashlib.sha256(self._encoded([rows, attempted, plans])).hexdigest()})
                if len(raw) <= 2 * 1024 * 1024:
                    break
                if plans:
                    plans.pop(0)
                else:
                    # Repeated paraphrases must not evict the broader original episode first.
                    index = next((i for i, (key, row) in enumerate(rows)
                                  if key[3] != row["reviewed_question"]), 0)
                    rows.pop(index)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(raw)
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError):
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, key):
        with self.lock:
            row = self.rows.get(key)
            if row is not None:
                self.rows.move_to_end(key)
                return deepcopy(row["payload"])
            return None

    def set(self, key, payload, *, reviewed_question=None):
        if not valid_answer(payload):
            return
        with self.lock:
            prior = self.rows.get(key, {})
            self.rows[key] = {"reviewed_question": reviewed_question or key[3],
                              "payload": deepcopy(payload),
                              "suggested": bool(prior.get("suggested")
                                  and (reviewed_question or key[3]) == key[3]
                                  and prior.get("reviewed_question") == key[3])}
            self.rows.move_to_end(key)
            while len(self.rows) > self.limit:
                victim = next((k for k, r in self.rows.items() if k[3] != r["reviewed_question"]),
                              next(iter(self.rows)))
                self.rows.pop(victim)
            self._save()

    def candidates(self, scope, question, *, limit=4):
        from app.eval.spoiler_gate.grounding import content_words
        words = content_words(question)
        with self.lock:
            # Always compare against the original GPT-reviewed question, never chain paraphrases.
            unique = {}
            for key, row in reversed(self.rows.items()):
                if key[:3] == scope:
                    # A reuse may return a smaller, independently reviewed claim subset. Prefer
                    # the original complete answer when it is still present; never lose its facts
                    # merely because a narrower paraphrase was requested more recently.
                    if key[3] == row["reviewed_question"] or row["reviewed_question"] not in unique:
                        unique[row["reviewed_question"]] = row
            ranked = sorted(unique.values(), key=lambda r: len(
                words & content_words(r["reviewed_question"] + " " +
                    " ".join(c["text"] for c in r["payload"]["claims"]))), reverse=True)
            return deepcopy(ranked[:limit])

    def mark_suggestion(self, key):
        with self.lock:
            row = self.rows.get(key)
            if row is not None and row["reviewed_question"] == key[3] and valid_answer(row["payload"]):
                row["suggested"] = True
                self._save()

    def suggestions(self, scope, *, frontier=None, limit=8):
        with self.lock:
            return [key[3] for key, row in self.rows.items()
                    if key[:3] == scope and row.get("suggested") is True
                    and row["reviewed_question"] == key[3] and valid_answer(row["payload"])
                    and (frontier is None or row["payload"]["as_of_chapter"] == frontier)][:limit]

    def invalidate_book(self, book_id):
        with self.lock:
            for key in [k for k in self.rows if k[0] == book_id]:
                del self.rows[key]
            for key in [k for k in self.attempted if k[0] == book_id]:
                del self.attempted[key]
            for key in [k for k in self.plans if k[0] == book_id]:
                del self.plans[key]
            self._save()

    def prepared(self, scope):
        with self.lock:
            return scope in self.attempted

    def mark_prepared(self, scope, *, keep_plan=False):
        with self.lock:
            if not keep_plan:
                for key in [k for k in self.plans if k[:2] == scope[:2]]:
                    self.plans.pop(key, None)
            self.attempted[scope] = True
            self.attempted.move_to_end(scope)
            while len(self.attempted) > self.limit:
                self.attempted.popitem(last=False)
            self._save()

    def plan(self, key, facts_hash):
        with self.lock:
            row = self.plans.get(key)
            if row is None:
                # The hash also binds source/frontier and reviewer policy. Only an allowance
                # change may reuse the same stages under a new admission-attempt key.
                row = next((v for k, v in reversed(self.plans.items())
                            if k[:2] == key[:2] and v["facts_hash"] == facts_hash), None)
            if row is not None and row["facts_hash"] == facts_hash and valid_saved_plan(row):
                if key in self.plans:
                    self.plans.move_to_end(key)
                return deepcopy(row)
            return None

    def set_plan(self, key, plan):
        if not valid_saved_plan(plan):
            raise ValueError("invalid preparation checkpoint")
        with self.lock:
            # A monotonic frontier/source correction cannot return to a previous scope. Keep
            # only the current resumable plan per incarnation, without deleting reviewed answers.
            for prior in [k for k in self.plans if k[:2] == key[:2] and k != key]:
                del self.plans[prior]
            self.plans[key] = deepcopy(plan)
            self.plans.move_to_end(key)
            while len(self.plans) > min(self.limit, 16):
                self.plans.popitem(last=False)
            self._save()
