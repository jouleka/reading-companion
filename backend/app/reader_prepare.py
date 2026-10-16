"""Short snapshot leases, bounded background spend, and unchanged reviewed answer preparation."""
from copy import copy
from dataclasses import dataclass, field
import hashlib
import json
from types import SimpleNamespace

from app.answer_bank import answer_scope, valid_answer
from app.ask import AskRequest
from app.cost import budgeted_completion
from app.preparation_plan import (
    PLAN_OUTPUT_TOKENS, PLAN_SYSTEM, PREPARATION_VERSION, REVIEW_OUTPUT_TOKENS, REVIEW_SYSTEM,
    PreparedQuestions, PreparationReview,
    diverse_order, preparation_facts, reviewed_candidates, validated_candidates,
)


class PreparationStale(RuntimeError):
    pass


class PreparationSuperseded(PreparationStale):
    pass


def preparation_key(scope, settings):
    # A larger/new planner gets one new bounded attempt without invalidating already-reviewed
    # answers. Its durable marker survives restart; scrolling cannot repeatedly pay for a plan.
    stamp = hashlib.sha256(json.dumps([scope.answer_key[2], PREPARATION_VERSION,
                                      settings.reader_prepare_max_questions]).encode()).hexdigest()
    return (*scope.answer_key[:2], stamp)


@dataclass(frozen=True)
class PreparationScope:
    book_id: str
    incarnation: str
    epoch: int
    frontier: int
    answer_key: tuple
    cancelled: object = field(default=None, compare=False, repr=False)

    @classmethod
    def capture(cls, app, book_id, incarnation):
        from app.api.views import _effective
        st = app.state
        book = st.catalog.get_book(book_id)
        if book is None or book["incarnation"] != incarnation:
            raise PreparationStale()
        eff, state, manifest = _effective(book_id, None, st.catalog, st.store, st.settings, st.worker)
        eff = min(eff, state["ingest_progress"])
        if eff <= 0:
            raise PreparationStale()
        key = answer_scope(book_id, eff, state, manifest, catalog=st.catalog, store=st.store,
                           settings=st.settings, client=st.reader_client)
        return cls(book_id, incarnation, state["position_epoch"], eff, key)

    def check(self, app, *, snapshot=False, priority=True):
        if getattr(getattr(app.state, "recap_warmer", None), "closed", False):
            raise PreparationStale()
        st = app.state
        book, state = st.catalog.get_book(self.book_id), st.catalog.get_state(self.book_id)
        if (book is None or state is None or book["incarnation"] != self.incarnation
                or state["position_epoch"] != self.epoch
                or min(state["bookmark"], state["ingest_progress"]) < self.frontier):
            raise PreparationStale()
        if priority and ((self.cancelled is not None and self.cancelled.is_set())
                         or state["bookmark"] != self.frontier):
            raise PreparationSuperseded()
        if snapshot:
            from app.ingest.manifest import load_manifest
            key = answer_scope(self.book_id, self.frontier, state,
                               load_manifest(st.settings.data_dir, self.book_id),
                               catalog=st.catalog, store=st.store, settings=st.settings,
                               client=st.reader_client)
            if key != self.answer_key:
                raise PreparationStale()


class PreparationCatalog:
    def __init__(self, app, scope):
        self.app, self.scope = app, scope

    def __getattr__(self, name):
        return getattr(self.app.state.catalog, name)

    def reserve_cost(self, book_id, **kwargs):
        self.scope.check(self.app, snapshot=True)
        if book_id != self.scope.book_id:
            raise PreparationStale()
        # Preparation must not consume the last allowance needed by a foreground question.
        # Keep up to four ordinary call token limits and $0.05, bounded to a quarter of each
        # configured book ceiling. The catalog checks these lower ceilings atomically against
        # all settled usage AND pending calls; foreground requests retain the original limits.
        settings = self.app.state.settings
        limits = dict(kwargs)
        for name, headroom in (
            ("max_input_tokens", 4*settings.cost_max_input_tokens_per_call),
            ("max_output_tokens", 4*settings.cost_max_output_tokens_per_call),
        ):
            limits[name] -= min(limits[name]//4, headroom)
        limits["max_usd"] -= min(limits["max_usd"]/4, .05)
        return self.app.state.catalog.reserve_cost(
            book_id, **{**limits, "phase": "reader-prepare:" + kwargs["phase"]},
            expected_reading_scope=(self.scope.incarnation, self.scope.epoch, self.scope.frontier),
            max_preparation_usd=self.app.state.settings.reader_prepare_max_usd_per_book,
        )

    def settle_cost(self, book_id, reservation_id, *, phase=None):
        # Failed calls remain part of the background cap, including conservative charges.
        if phase is not None and not phase.startswith("reader-prepare:"):
            phase = "reader-prepare:" + phase
        return self.app.state.catalog.settle_cost(book_id, reservation_id, phase=phase)


class GuardedCache:
    def __init__(self, cache, app, scope):
        self.cache, self.app, self.scope = cache, app, scope

    def __getattr__(self, name):
        return getattr(self.cache, name)

    def set(self, *args, **kwargs):
        # A fully reviewed call already in flight may finish after a focus change. Retain its
        # original scoped result, but never publish across reset, deletion or source correction.
        self.scope.check(self.app, snapshot=True, priority=False)
        return self.cache.set(*args, **kwargs)


def prepare(app, scope):
    """Run outside request gates: scrolling/reset/delete may proceed during every provider call."""
    from app.api.views import ask_the_book, catch_me_up
    st = app.state
    scope.check(app)
    catalog = PreparationCatalog(app, scope)
    client = copy(st.reader_client)
    client.preparing = True
    client.answers = GuardedCache(st.reader_client.answers, app, scope)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        recaps=GuardedCache(st.recaps, app, scope))))
    if st.settings.recap_prewarm:
        try:
            catch_me_up(scope.book_id, request, scope.frontier, catalog, st.store,
                        st.settings, client, st.worker)
        except Exception:
            scope.check(app)  # A failed recap does not prevent independently reviewed answers.
    if not st.settings.reader_prepare_enabled or not st.settings.reader_answer_cache:
        return
    bank = st.reader_client.answers
    attempt_key = preparation_key(scope, st.settings)
    if bank.prepared(attempt_key):
        return
    try:
        scope.check(app, snapshot=True)
        with st.store.book(scope.book_id) as mem:
            facts = preparation_facts(mem, scope.frontier,
                byte_limit=min(18000, st.settings.cost_max_input_tokens_per_call//2))
        if not facts:
            return
        facts_hash = hashlib.sha256(bank._encoded(facts)).hexdigest()
        saved = bank.plan(attempt_key, facts_hash)
        if saved is not None and any(not set(c["basis_ids"]) <= {f["id"] for f in facts}
                                     for c in saved["questions"]):
            saved = None

        def checkpoint(questions, *, reviewed):
            scope.check(app, snapshot=True, priority=False)
            if st.catalog.get_state(scope.book_id)["bookmark"] == scope.frontier:
                bank.set_plan(attempt_key, {"facts_hash": facts_hash, "reviewed": reviewed,
                                             "questions": questions})

        if saved is None:
            plan = budgeted_completion(
                catalog, st.settings.model_copy(update={"cost_max_output_tokens_per_call": min(
                    PLAN_OUTPUT_TOKENS, st.settings.cost_max_output_tokens_per_call)}),
                client, scope.book_id, phase="questions", tier="cheap", schema=PreparedQuestions,
                system=PLAN_SYSTEM,
                user=json.dumps({"completed_chapter": scope.frontier, "facts": facts}, ensure_ascii=False),
            )
            candidates = validated_candidates(plan.value, facts)
            checkpoint(candidates, reviewed=False)
        else:
            candidates = saved["questions"]
        if saved is not None and saved["reviewed"]:
            safe = candidates
        else:
            if not candidates:
                return
            scope.check(app, snapshot=True)
            audit = budgeted_completion(
                catalog, st.settings.model_copy(update={"cost_max_output_tokens_per_call": min(
                    REVIEW_OUTPUT_TOKENS, st.settings.cost_max_output_tokens_per_call)}),
                client, scope.book_id, phase="question-review", tier="large", schema=PreparationReview,
                system=REVIEW_SYSTEM,
                user=json.dumps({"completed_chapter": scope.frontier, "facts": facts,
                                 "questions": candidates}, ensure_ascii=False),
            )
            safe = reviewed_candidates(audit.value, candidates, facts)
            checkpoint(safe, reviewed=True)
        completed = set()
        for item in diverse_order(safe, limit=st.settings.reader_prepare_max_questions):
            scope.check(app, snapshot=True)
            if item["kind"] == "follow_up" and item["follow_up_to"] not in completed:
                continue
            try:
                question = item["question"]
                value = ask_the_book(scope.book_id, AskRequest(question=question, bookmark=scope.frontier),
                                     catalog, st.store, st.settings, client, st.worker)
                # Only a premise-reviewed question with a cleared complete answer is offered.
                # A refusal, failed answer or unrelated foreground cache row grants no suggestion.
                if value.get("as_of_chapter") == scope.frontier and valid_answer(value):
                    scope.check(app, snapshot=True)
                    bank.mark_suggestion((*scope.answer_key, question))
                    completed.add(item["id"])
            except Exception:
                scope.check(app)
    finally:
        scope.check(app, snapshot=True)
        bank.mark_prepared(attempt_key)
