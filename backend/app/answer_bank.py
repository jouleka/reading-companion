"""Bounded, source-scoped complete answers. Only the reviewed route can populate this bank."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

from app.ask import AskDraft
from app.eval.spoiler_gate import validity_snapshot


ANSWER_VERSION = "reviewed-answer-v2"


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
        self.lock = threading.Lock()
        self._load()

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
            if (data["version"] != ANSWER_VERSION or not isinstance(rows, list)
                    or len(rows) > self.limit
                    or not isinstance(attempted, list) or len(attempted) > self.limit
                    or any(not isinstance(k, list) or len(k) != 3
                           or not all(isinstance(p, str) for p in k) for k in attempted)
                    or hashlib.sha256(self._encoded([rows, attempted])).hexdigest() != data["sha256"]):
                return
            for key, row in rows:
                if (not isinstance(key, list) or len(key) != 4
                        or not all(isinstance(k, str) for k in key)
                        or not 2 <= len(key[3]) <= 500
                        or not isinstance(row.get("reviewed_question"), str)
                        or not 2 <= len(row["reviewed_question"]) <= 500
                        or not valid_answer(row["payload"])):
                    return
            self.rows.update((tuple(k), v) for k, v in rows)
            self.attempted.update((tuple(k), True) for k in attempted)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _save(self):
        if self.path is None:
            return
        temporary = self.path.with_suffix(".tmp")
        try:
            rows = [[list(k), v] for k, v in self.rows.items()]
            attempted = [list(k) for k in self.attempted]
            while True:
                raw = self._encoded({"version": ANSWER_VERSION, "rows": rows, "prepared": attempted,
                                     "sha256": hashlib.sha256(self._encoded([rows, attempted])).hexdigest()})
                if len(raw) <= 2 * 1024 * 1024:
                    break
                rows.pop(0)
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
            self.rows[key] = {"reviewed_question": reviewed_question or key[3],
                              "payload": deepcopy(payload)}
            self.rows.move_to_end(key)
            while len(self.rows) > self.limit:
                self.rows.popitem(last=False)
            self._save()

    def candidates(self, scope, question, *, limit=4):
        from app.eval.spoiler_gate.grounding import content_words
        words = content_words(question)
        with self.lock:
            # Always compare against the original GPT-reviewed question, never chain paraphrases.
            unique = {}
            for key, row in reversed(self.rows.items()):
                if key[:3] == scope:
                    unique.setdefault(row["reviewed_question"], row)
            ranked = sorted(unique.values(), key=lambda r: len(
                words & content_words(r["reviewed_question"] + " " +
                    " ".join(c["text"] for c in r["payload"]["claims"]))), reverse=True)
            return deepcopy(ranked[:limit])

    def invalidate_book(self, book_id):
        with self.lock:
            for key in [k for k in self.rows if k[0] == book_id]:
                del self.rows[key]
            for key in [k for k in self.attempted if k[0] == book_id]:
                del self.attempted[key]
            self._save()

    def prepared(self, scope):
        with self.lock:
            return scope in self.attempted

    def mark_prepared(self, scope):
        with self.lock:
            self.attempted[scope] = True
            self.attempted.move_to_end(scope)
            while len(self.attempted) > self.limit:
                self.attempted.popitem(last=False)
            self._save()
