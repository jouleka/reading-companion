"""LIT-8 — the spoiler-GATE merge test (ADR 0004 + ADR 0007 D-A9).

Builds a REAL production store (the 5 committed Karamazov chapters ingested through Module C on the
offline stub — _fixture.build_fixture_store) and runs the four leak vectors + the reveal-correctness
signal + the pinned D-A9 runtime-gate contract + the D-A4 BruteForce probes against it. Every check is
falsifiable: dropping a filter (alias filter / search funnel) makes the harness REPORT leaks, proving
the 0-leak result is a real filter and not an empty store.

This file BLOCKS MERGE: a regression that re-opens any leak vector turns it red.
"""
import re
import sqlite3

import pytest

from app.eval.spoiler_gate import (
    SYNTH_SYSTEM,
    SpoilerGateError,
    assert_recap_safe,
    cache_key,
    rag_eval,
    read_text_upto,
    reveal_correctness_eval,
    score_recap,
    structured_eval,
    supplied_facts,
    synth_prompt,
    validity_snapshot,
)

from _fixture import BOOK_ID, build_fixture_store


@pytest.fixture
def gate(tmp_path):
    """A fresh, isolated fixture store per test (the safety vectors MUTATE the store)."""
    store, client, max_bm = build_fixture_store(tmp_path)
    return store, client, max_bm


# =========================================================================== vector 3: synthesis scorer
def test_scorer_passes_a_grounded_recap(gate):
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        recap = ("Fyodor Pavlovitch Karamazov is a buffoonish landowner; "
                 "his son Dmitri was left to a servant.")
        sc = score_recap(mem, 2, recap, read_text=read_text_upto(mem, 2))
    assert not sc["future_entity_leaks"], sc["future_entity_leaks"]
    assert not sc["prolepsis_hits"], sc["prolepsis_hits"]


def test_read_text_upto_is_funnel_bounded(gate):
    """read_text_upto(db, bm) is assembled ONLY from raw_chapters with revealed_at <= bm through the
    funnel (D-A9): a name first appearing in a LATER chapter must NOT be in the bm-bounded prose."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        upto2 = read_text_upto(mem, 2)
        upto4 = read_text_upto(mem, 4)
        upto_all = read_text_upto(mem, max_bm)
    assert "Karamazov" in upto2                       # a ch1/2 name is present
    assert "Zossima" not in upto2                      # the elder Zossima first appears @ ch4 -> absent @2
    assert "Zossima" in upto4                          # present once the chapter is read
    assert len(upto2) < len(upto4) < len(upto_all)     # strictly grows with the bookmark


def test_scorer_catches_capitalized_future_entity(gate):
    # "the elder Zossima" is revealed @ ch4 -> naming him in a bookmark-2 recap is a hard leak. (The
    # leak is ATTRIBUTED to whichever future entity owns the shared token "zossima"; assert the
    # behaviour — a leak is caught and it is a Zossima-bearing future entity — not a specific name.)
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        sc = score_recap(mem, 2, "The elder Zossima guides Alyosha at the monastery.",
                         read_text=read_text_upto(mem, 2))
    assert sc["future_entity_leaks"], sc
    assert any("Zossima" in n for n in sc["future_entity_leaks"]), sc["future_entity_leaks"]


def test_scorer_catches_lowercased_future_entity(gate):
    # case-insensitive: "sofya" (Sofya Ivanovna, revealed @ ch3) lowercased must still be caught.
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        sc = score_recap(mem, 2, "His second wife sofya was a meek orphan whom he tormented.",
                         read_text=read_text_upto(mem, 2))
    assert sc["future_entity_leaks"], sc


@pytest.mark.parametrize("future_name", ["Дмитрий", "Δημήτρης", "阿廖沙"])
def test_scorer_catches_future_entity_names_in_non_latin_scripts(gate, future_name):
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity(future_name, "character", revealed_at=max_bm)
        sc = score_recap(
            mem,
            2,
            f"The visitor {future_name} arrived at the monastery.",
            read_text=read_text_upto(mem, 2),
        )
    assert future_name in sc["future_entity_leaks"], sc


def test_scorer_catches_prolepsis(gate):
    # a future-tense modal is a structural tell of a paraphrased future EVENT the name check can't see.
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        sc = score_recap(mem, 2, "Dmitri would eventually be murdered and wrongly convicted.",
                         read_text=read_text_upto(mem, 2))
    assert sc["prolepsis_hits"], sc


def test_no_false_leak_on_role_word_superior(gate):
    # "the Superior" is revealed @ ch5, but a grounded recap using the ROLE-word "superior" must NOT
    # false-flag (rev-2 regression fix): role nouns are subtracted from future-entity tokens.
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        sc = score_recap(mem, 3, "Dmitri felt his claim was superior to his brother's.",
                         read_text=read_text_upto(mem, 3))
    assert not sc["future_entity_leaks"], sc["future_entity_leaks"]


def test_fail_safe_future_name_colliding_with_common_word(gate):
    # rev-2 FAIL-SAFE: a future entity whose SOLE name token is a common word the reader saw only
    # LOWERCASE ("town") must STILL be caught, never silently dropped by reader-parity.
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity("Town", "place", revealed_at=max_bm)
        sc = score_recap(mem, 2, "Then the town itself was destroyed.",
                         read_text=read_text_upto(mem, 2))
    assert "Town" in sc["future_entity_leaks"], sc["future_entity_leaks"]


# =========================================================================== vector 1: structured reads
def test_structured_eval_zero_leaks_and_non_vacuous(gate):
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        reads, leaks, examples = structured_eval(mem, max_bm)
    assert leaks == 0, examples[:5]
    assert reads > 100, f"vacuous: only {reads} reads checked"


def test_structured_eval_drop_alias_filter_detects_leak(gate, monkeypatch):
    """FALSIFIABILITY (ADR 0004 vector 1): drop the spoiler clause for ONE table (aliases) and the
    harness MUST report leaks — proving each path's revealed_at is genuinely checked, not just present."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        orig = mem._select

        def broken(table, cols, bookmark, where_extra="", params=(), order=""):
            if table == "aliases":                                # drop the filter for aliases only
                with mem._writer():
                    return mem._conn.execute(
                        f"SELECT {cols} FROM aliases WHERE book_id=?", (mem._book_id,)).fetchall()
            return orig(table, cols, bookmark, where_extra, params, order)

        monkeypatch.setattr(mem, "_select", broken)
        _, bleaks, _ = structured_eval(mem, max_bm)
    assert bleaks > 0, "dropping the alias filter must surface future-revealed aliases (suite detects leaks)"


def test_structured_eval_scores_the_rolling_recap_and_catches_a_planted_leak(gate):
    """The catch_me_up branch of structured_eval is NOT dormant. Pass-2 F6 strengthened: the planted
    recap has a future NAME but NO future-tense modal, so the future-entity leg alone must catch it
    (prolepsis can't mask a broken name check); and a clean rolling-recap yields no recap leak."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        key2 = next(ch["chapter_key"] for ch in mem.view(2).chapters() if ch["revealed_at"] == 2)
        mem.add_summary(key2, 2, "The elder Zossima guided Alyosha at the monastery.",
                        kind="rolling-recap")                      # future name, past tense, no modal
        _, leaks, examples = structured_eval(mem, max_bm)
        recap_leaks = [e for e in examples if e[1] == "catch_me_up-recap-leak"]
        assert leaks >= 1 and recap_leaks, "the future-name-only rolling-recap must be caught"
        assert any(e[2]["future_entity_leaks"] for e in recap_leaks), \
            "it must be the future-entity leg (not prolepsis) that fires"

    store2, _, max_bm2 = build_fixture_store(str(store._data_dir) + "_clean")
    with store2.book(BOOK_ID) as mem:
        key2 = next(ch["chapter_key"] for ch in mem.view(2).chapters() if ch["revealed_at"] == 2)
        mem.add_summary(key2, 2, "Fyodor married twice and had three sons.",
                        kind="rolling-recap")                      # clean, grounded, past-tense
        _, leaks2, examples2 = structured_eval(mem, max_bm2)
        assert not [e for e in examples2 if e[1] == "catch_me_up-recap-leak"], examples2


def test_late_entity_hidden_then_appears(gate):
    """FALSIFIABILITY: a name-like entity revealed only late (the elder Zossima @ ch4) is HIDDEN at an
    earlier bookmark and APPEARS at its reveal chapter — proving the 0-leak result is a real filter,
    not an empty store."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        zoss = [r for r in mem._audit_all("entities")
                if "Zossima" in r["canonical_name"] and r["revealed_at"] == 4 and r["retracted_at"] is None]
        assert zoss, "fixture must contain a Zossima entity revealed @ ch4"
        eid = zoss[0]["entity_id"]
        assert mem.view(2).bio(eid) is None                       # hidden before its reveal chapter
        assert mem.view(4).bio(eid) is not None                   # appears at its reveal chapter


# =========================================================================== vector 2: RAG / quote path
def _embed(client):
    return lambda texts: client.embed(texts)[0]


def test_rag_eval_zero_leaks_and_non_vacuous(gate):
    store, client, max_bm = gate
    with store.book(BOOK_ID) as mem:
        reads, leaks, foreshadow = rag_eval(mem, max_bm, _embed(client))
    assert leaks == 0, f"a future-chapter chunk was returned by search() ({leaks} leaks)"
    assert reads > 0, "vacuous: RAG eval retrieved no chunks"


def test_rag_search_filter_is_load_bearing_drop_leaks(gate, monkeypatch):
    """D-A4 conjunct 1 (reused): drop the funnel filter inside search() and a FUTURE-chapter chunk MUST
    surface — proving the spoiler FILTER lives in BookmarkView.search()/the funnel, not the ranker."""
    store, client, _ = gate
    qv = client.embed(["the elder at the monastery and the murder"])[0][0]
    with store.book(BOOK_ID) as mem:
        # This mutation targets the retained exact fallback's _select funnel. Production vec0 has its
        # own drop-bound falsifiability test and rejects a corrupted canonical recheck fail-closed.
        mem._vector_backend = "bruteforce"
        baseline = mem.view(2).search(qv, k=5)
        assert all(rev <= 2 for (_s, _t, rev, _k) in baseline)     # filter intact: nothing past bm 2

        def leaky_select(table, cols, bookmark, where_extra="", params=(), order=""):
            prev = mem._engaged                                    # TOTAL funnel-filter drop
            mem._engaged = True
            try:
                return mem._conn.execute(
                    f"SELECT {cols} FROM {table} WHERE book_id = ?", [mem._book_id]).fetchall()
            finally:
                mem._engaged = prev

        monkeypatch.setattr(mem, "_select", leaky_select)
        leaked = mem.view(2).search(qv, k=5)
    assert any(rev > 2 for (_s, _t, rev, _k) in leaked), \
        "dropping the funnel filter must surface a future-chapter chunk (the suite can detect a leak)"


def test_rag_raw_chunks_select_denied(gate):
    """D-A4 conjunct 2 (reused): a raw read of the chunks fact table is authorizer-DENIED."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        with pytest.raises(sqlite3.DatabaseError):
            mem._conn.execute("SELECT text FROM chunks").fetchall()


def test_rag_eval_own_counter_detects_leak_when_filter_dropped(gate, monkeypatch):
    """FALSIFIABILITY of rag_eval ITSELF (mutation M7b): with the funnel filter dropped, rag_eval —
    not just a direct search() call — must REPORT leaks > 0. Pins rag_eval's own leak counter, so a
    neutered `if rev > bm` cannot survive."""
    store, client, max_bm = gate
    with store.book(BOOK_ID) as mem:
        mem._vector_backend = "bruteforce"  # exercise rag_eval's leak counter with the reference path

        def leaky_select(table, cols, bookmark, where_extra="", params=(), order=""):
            prev = mem._engaged                                    # TOTAL funnel-filter drop
            mem._engaged = True
            try:
                return mem._conn.execute(
                    f"SELECT {cols} FROM {table} WHERE book_id = ?", [mem._book_id]).fetchall()
            finally:
                mem._engaged = prev

        monkeypatch.setattr(mem, "_select", leaky_select)
        _, leaks, _ = rag_eval(mem, max_bm, _embed(client))
    assert leaks > 0, "with the filter dropped rag_eval must COUNT the surfaced future chunks"


# =========================================================================== D-A9 runtime gate contract
def test_runtime_gate_passes_a_clean_recap(gate):
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        recap = ("Fyodor Pavlovitch Karamazov is a buffoonish landowner; "
                 "his son Dmitri was left to a servant.")
        sc = assert_recap_safe(mem, 2, recap, read_text=read_text_upto(mem, 2))
    assert sc["future_entity_leaks"] == [] and sc["prolepsis_hits"] == []


def test_runtime_gate_rejects_future_entity_recap(gate):
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "The elder Zossima guides Alyosha.",
                              read_text=read_text_upto(mem, 2))


def test_runtime_gate_rejects_read_text_covering_chapters_past_the_bookmark(gate):
    """D-A9 falsifiability: a read_text covering chapters PAST the bookmark (which would make
    reader-parity fail OPEN) is REJECTED — the wrapper validates read_text == read_text_upto(db, bm)."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        future_covering = read_text_upto(mem, max_bm)        # covers all 5 chapters
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "A grounded recap.", read_text=future_covering)


def test_runtime_gate_requires_read_text(gate):
    """read_text is a REQUIRED (keyword-only, no default) arg so an omission fails LOUD rather than
    silently over-blocking (D-A9)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        with pytest.raises(TypeError):
            assert_recap_safe(mem, 2, "A grounded recap.")   # read_text omitted


def test_runtime_gate_rejects_prolepsis(gate):
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "Dmitri would eventually be murdered.",
                              read_text=read_text_upto(mem, 2))


def test_runtime_gate_does_not_leak_future_names_on_reject(gate):
    """On a hard leak the wrapper RAISES (the recap is DISCARDED) rather than returning it — the
    forbidden future names never reach a caller's return value."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        result = "untouched"
        with pytest.raises(SpoilerGateError):
            result = assert_recap_safe(mem, 2, "The elder Zossima guides Alyosha.",
                                       read_text=read_text_upto(mem, 2))
        assert result == "untouched"                          # never assigned -> recap was discarded


def test_supplied_facts_are_bookmark_bounded(gate):
    # the facts a grounded recap MAY use are themselves funnel-bounded (no future character).
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        facts2 = supplied_facts(mem, 2)
        blob = " ".join(facts2["characters"])
    assert not re.search(r"\bZossima\b", blob), blob       # Zossima (@4) not in the bm-2 supplied cast
    assert not any("Zossima" in entity["canonical_name"] for entity in facts2["_entities"]), \
        "the binding identity map must be bounded by the same bookmark funnel"


# =========================================================================== synthesis prompt surface
def test_synth_system_is_anti_foreshadow():
    """ADR 0004 close-out: SYNTH_SYSTEM carries the anti-foreshadow wording (the real-model gpt-4o
    'sets the stage for an impending gathering' over-reach was eliminated by this prompt + the gate)."""
    s = SYNTH_SYSTEM.lower()
    assert "foreshadow" in s
    assert "already happened" in s or "already" in s


def test_synth_prompt_is_built_from_bookmark_bounded_facts(gate):
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        prompt = synth_prompt(2, supplied_facts(mem, 2))
    assert "Karamazov" in prompt                            # a visible character is in the prompt
    assert "Zossima" not in prompt                          # a future (@4) character is NOT


# =========================================================================== LIT-25: sentence grounding
def test_subject_binding_catches_grounded_vocabulary_attached_to_the_wrong_person(gate):
    """LIT-27: once 'murder' appears in an unrelated visible event, LIT-25's token coverage is 1.0.
    The deterministic role binding must still reject 'Fyodor was murdered' without relying on the
    LLM judge."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        mem.add_event("A mysterious visitor confessed to committing a murder years ago.", 2, 999)
        rt2 = read_text_upto(mem, 2)
        sc = score_recap(mem, 2, "Fyodor was murdered.", read_text=rt2)
        assert not sc["future_entity_leaks"] and not sc["prolepsis_hits"], sc
        assert not sc["ungrounded_sentences"], "the attack must isolate the LIT-25 lexical residual"
        assert sc["unsupported_event_bindings"], sc
        with pytest.raises(SpoilerGateError) as caught:
            assert_recap_safe(mem, 2, "Fyodor was murdered.", read_text=rt2)
        assert caught.value.details["unsupported_event_bindings"]


def test_grounding_catches_a_past_tense_no_name_future_event(gate):
    """LIT-25 (the ADR 0004 HIGH #1 residual, now closed deterministically): a paraphrased FUTURE
    EVENT in PAST tense with NO future name and NO modal ('Dmitri was murdered and wrongly convicted')
    slips the name check and the prolepsis tripwire — the sentence-GROUNDING gate must catch it: its
    content words are not traceable to the bookmark-bounded supplied facts."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        sc = score_recap(mem, 2, "Dmitri was murdered and wrongly convicted.", read_text=rt2)
        assert sc["ungrounded_sentences"], sc
        assert not sc["future_entity_leaks"] and not sc["prolepsis_hits"], \
            "the attack must be exactly the class the old checks cannot see"
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "Dmitri was murdered and wrongly convicted.", read_text=rt2)


def test_grounding_passes_grounded_recaps(gate):
    """No over-block: a recap whose sentences trace to the supplied facts (verbatim or lightly
    paraphrased) passes; the canonical clean recap stays accepted end-to-end."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        clean = ("Fyodor Pavlovitch Karamazov is a buffoonish landowner; "
                 "his son Dmitri was left to a servant. "
                 "Adelaïda ran away to Petersburg with a divinity student.")
        sc = assert_recap_safe(mem, 2, clean, read_text=rt2)     # must NOT raise
    assert sc["ungrounded_sentences"] == []


def test_grounding_catches_pure_invention_and_flags_only_the_bad_sentence(gate):
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        mixed = ("Fyodor Pavlovitch Karamazov is a buffoonish landowner. "
                 "A dragon burned the village to ashes.")
        sc = score_recap(mem, 2, mixed, read_text=rt2)
    assert len(sc["ungrounded_sentences"]) == 1
    assert "dragon" in sc["ungrounded_sentences"][0]["sentence"].lower()


def test_grounding_catches_a_short_event_leak(gate):
    # 'He was murdered.' — one content word, zero grounding: must be caught, never skipped as 'too short'
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        sc = score_recap(mem, 2, "He was murdered.", read_text=read_text_upto(mem, 2))
    assert sc["ungrounded_sentences"], sc


def test_grounding_is_not_diluted_by_visible_names(gate):
    """A name-dense event sentence must not ground itself on the names alone: 'Fyodor Pavlovitch
    Karamazov, Dmitri and Ivan were all murdered.' is 5 grounded name tokens + 1 ungrounded event word
    — the EVENT words are what must trace to the facts (names are the entity gate's job)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        sc = score_recap(mem, 2, "Fyodor Pavlovitch Karamazov, Dmitri and Ivan were all murdered.",
                         read_text=rt2)
    assert sc["ungrounded_sentences"], "visible names must not dilute the grounding ratio"


def test_clause_dilution_is_caught(gate):
    """LIT-25 review pass-1 BLOCKER: a grounded clause must not launder a leak clause inside one
    sentence — the most realistic synth over-reach shape ('…grounded material, and X was murdered.')
    passed the whole gate at coverage 0.889. Per-clause: an ungrounded clause carrying a
    high-consequence EVENT stem hard-rejects."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        diluted = ("Fyodor Pavlovitch rose from next to nothing to amass a hundred thousand "
                   "roubles, and Dmitri was murdered.")
        sc = score_recap(mem, 2, diluted, read_text=rt2)
        assert sc["ungrounded_sentences"], sc
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, diluted, read_text=rt2)


def test_bullet_recap_does_not_dilute_across_lines(gate):
    # newline is a sentence boundary: a leak bullet can't hide between grounded bullets
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        bullets = ("- Fyodor amassed a hundred thousand roubles\n"
                   "- Dmitri was murdered\n"
                   "- Adelaïda ran away to Petersburg")
        sc = score_recap(mem, 2, bullets, read_text=rt2)
    assert sc["ungrounded_sentences"], sc


def test_name_only_event_leak_is_flagged(gate):
    """LIT-25 review pass-1 HIGH: death/culprit is expressible in names + stopwords alone
    ('Smerdyakov did it.' / 'Fyodor was no more.') — invisible to every other check. A sentence
    that strips to NOTHING but still carries a visible name is hard-flagged (measured: 0/502
    grounded live sentences are name-only, and there is no runtime judge yet for a soft tier)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        for leak in ("Fyodor was no more.", "It was Dmitri."):
            sc = score_recap(mem, 2, leak, read_text=rt2)
            assert sc["ungrounded_sentences"], f"{leak!r} must be flagged, got {sc}"


def test_honorific_abbreviations_do_not_over_block(gate):
    # 'Mr. Karamazov …' must not split into a 'Mr.' fragment that hard-rejects (review pass-1 MEDIUM)
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        sc = assert_recap_safe(mem, 2, "Mr. Karamazov was a buffoonish landowner.", read_text=rt2)
    assert sc["ungrounded_sentences"] == []


def test_structured_eval_catches_an_untraceable_rolling_recap(gate):
    """LIT-25 review pass-1 LOW hardening: vector 1's rolling-recap scoring now includes the
    grounding hard tier — a stored recap describing an untraceable event must count as a leak."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        key2 = next(ch["chapter_key"] for ch in mem.view(2).chapters() if ch["revealed_at"] == 2)
        mem.add_summary(key2, 2, "A monk was murdered in the cell.", kind="rolling-recap")
        _, leaks, examples = structured_eval(mem, max_bm)
    assert any(e[1] == "catch_me_up-recap-leak" for e in examples), examples


def test_alias_name_only_leak_is_caught(gate):
    """LIT-25 pass-2 F2 (BLOCKER on live): the name-only strip keyed on CANONICAL tokens only — on
    the live book 'Dmitri' is an alias ('Mitya' is canonical) so 'Dmitri did it.' passed clean.
    Fixture inverse: 'Mitya' is the alias here — 'Mitya did it.' must be hard-flagged."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt4 = read_text_upto(mem, 4)
        sc = score_recap(mem, 4, "Mitya did it.", read_text=rt4)
    assert sc["ungrounded_sentences"], sc


def test_alias_tokens_do_not_ground_a_leak_sentence(gate):
    """Pass-2 F1 (alias leg): an alias token must not GROUND the ratio either — 'Alyosha perished at
    dawn.' scored 0.33 (soft) because 'alyosha' grounded as a content word; with aliases stripped the
    event words stand alone (0.0 -> hard)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt4 = read_text_upto(mem, 4)
        sc = score_recap(mem, 4, "Alyosha perished at dawn.", read_text=rt4)
    assert sc["ungrounded_sentences"], sc


def test_name_only_leak_clause_is_caught(gate):
    """Pass-2 F3 (HIGH): a name-only leak CLAUSE behind a grounded clause was skipped by
    `if not ccw: continue` — 'Alyosha wept; Mitya did it.' passed clean."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt4 = read_text_upto(mem, 4)
        sc = score_recap(mem, 4, "Alyosha wept; Mitya did it.", read_text=rt4)
    assert sc["ungrounded_sentences"], sc


def test_died_and_past_tense_leak_clauses_are_caught(gate):
    """Pass-2 F4 (HIGH): the stem lexicon missed 12/14 realistic leak clauses ('died' itself matched
    no stem — a fold/length bug). An ungrounded clause is now hard when it carries an event stem OR a
    past-tense-ish ungrounded word."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        head = "Fyodor Pavlovitch rose from next to nothing to amass a hundred thousand roubles"
        for tail in ("and the elder died.", "and she was disgraced.", "and she perished at sea."):
            sc = score_recap(mem, 2, f"{head}, {tail}", read_text=rt2)
            assert sc["ungrounded_sentences"], f"{tail!r} escaped: {sc}"


def test_said_no_is_a_sentence_boundary(gate):
    """Pass-2 F5 (MEDIUM fail-open): 'No' in the abbreviation list swallowed a real sentence boundary,
    merging a leak into a grounded sentence. 'No.' splits unless followed by a number."""
    from app.eval.spoiler_gate.grounding import split_sentences
    assert len(split_sentences("He said No. Then he left.")) == 2
    assert len(split_sentences("It was case No. 5 in the file.")) == 1


def test_middle_initials_do_not_over_block():
    """Pass-2 F6 (MEDIUM over-block): 'Pyotr A. Miusov ...' split at the initial into a name-only
    'Pyotr A.' fragment that hard-rejected the whole recap."""
    from app.eval.spoiler_gate import ground_recap
    facts = {"characters": ["Pyotr Alexandrovitch Miusov"],
             "chapter_summaries": ["Pyotr visited the monastery with the family and spoke at length."],
             "events": []}
    out = ground_recap("Pyotr A. Miusov visited the monastery with the family.", facts)
    assert out["hard"] == [], out


def test_unregistered_nickname_name_only_is_caught():
    """Pins pass-2 fix B precisely (mutation P2): a nickname that is NOT in the DB (no alias row) but
    GROUNDS via the summaries — only the proper-noun-subset rule can see it (the alias strip can't,
    and coverage is 1.0 so the floor can't)."""
    from app.eval.spoiler_gate import ground_recap
    facts = {"characters": ["Pavel Orlov"], "aliases": [],
             "chapter_summaries": ["Pasha visited the town square and spoke to the crowd."],
             "events": []}
    out = ground_recap("Pasha did it.", facts)
    assert out["hard"] and out["hard"][0].get("reason") == "name-only", out


def test_name_only_clause_behind_a_grounded_sentence_is_caught():
    """Pins pass-2 fix C-doubleprime precisely (mutation P3): the leak clause strips to name+aux while
    the REST of the sentence grounds above the floor — only the clause-level name-only rule fires."""
    from app.eval.spoiler_gate import ground_recap
    facts = {"characters": ["Alexey Karamazov", "Dmitri Karamazov"],
             "aliases": ["Alyosha", "Mitya"],
             "chapter_summaries": ["Alyosha entered the monastery near the town."],
             "events": []}
    out = ground_recap("Alyosha entered the monastery near the town, and Mitya did it.", facts)
    assert out["hard"] and out["hard"][0].get("reason") == "name-only", out


def test_grounding_with_empty_facts_fails_safe(gate):
    # facts empty (nothing ingested at that bookmark) -> every content sentence is ungrounded, never a pass
    from app.eval.spoiler_gate import ground_recap
    flagged = ground_recap("Something dramatic happened.",
                           {"characters": [], "chapter_summaries": [], "events": []})
    assert flagged["hard"], "empty facts must ground nothing (fail-safe)"
    empty = ground_recap("", {"characters": [], "chapter_summaries": [], "events": []})
    assert empty["hard"] == [] and empty["soft"] == []


def test_partially_grounded_characterization_is_soft_not_hard(gate):
    """ADR 0004 classifies characterization restatement ('proud', 'chaotic lifestyle') as SOFT
    over-reach — proven live: a real gpt-4o recap@48 sentence at coverage 0.286 must NOT hard-reject
    (the hard tier is for essentially-untraceable sentences), it reports as weakly-grounded."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        # 'buffoonish landowner' grounds; the character-judgment adjectives do not: partial band
        recap = ("Fyodor Pavlovitch Karamazov was a buffoonish landowner, "
                 "oddly magnetic, repellent, flamboyant and sly.")
        sc = score_recap(mem, 2, recap, read_text=rt2)
        assert not sc["ungrounded_sentences"], sc               # NOT a hard reject
        assert sc["weakly_grounded_sentences"], sc              # surfaced for the judge
        assert_recap_safe(mem, 2, recap, read_text=rt2)         # runtime gate passes it


# =========================================================================== pass-2 review findings
def test_future_alias_token_is_forbidden(gate):
    """Pass-2 F3: a distinctive FUTURE alias surface form (the fixture's real 'Siberia', an alias of
    Russia@5, absent from every canonical name) must be a hard leak at bookmark 2 — the forbidden set
    consults aliases, not just entity canonicals."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        sc = score_recap(mem, 2, "He was exiled to Siberia.", read_text=read_text_upto(mem, 2))
        assert sc["future_entity_leaks"], sc
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "He was exiled to Siberia.", read_text=read_text_upto(mem, 2))


def test_future_theme_name_is_a_soft_signal_not_a_hard_block(gate):
    """Pass-3 F-P3-1 (proved on the LIVE book): theme names are extractor-authored Title-Case LABELS
    of common abstract words ('Family Tensions', 'Life and Death'), so hard-folding them rejects
    grounded recaps built from the store's own supplied facts (65 future themes at live bm=10 were
    fully-common-word). Themes are now a SOFT signal (future_theme_hits — judge-reviewable); the hard
    gate stays on entity/alias names + prolepsis. The genuinely-spoilery theme label ('Parricide') is
    the documented future-EVENT residual class (soft judge / routed NLI gate)."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        mem.add_theme("Family Tensions", "abstract meta-label", revealed_at=max_bm)
        mem.add_theme("Parricide", "the crime at the heart of the novel", revealed_at=max_bm)
        grounded = "Adelaïda ran away to Petersburg with the family tensions unresolved."
        sc = score_recap(mem, 2, grounded, read_text=rt2)
        assert not sc["future_entity_leaks"], sc            # NOT a hard leak
        assert not sc["ungrounded_sentences"], sc           # genuinely fact-grounded (LIT-25)
        sc2 = assert_recap_safe(mem, 2, grounded, read_text=rt2)   # the runtime gate PASSES it
        spoilery = score_recap(mem, 2, "The shadow of Parricide hangs over them.", read_text=rt2)
        assert not spoilery["future_entity_leaks"], spoilery
        assert spoilery["future_theme_hits"], spoilery      # surfaced as the SOFT signal
        assert sc2["future_theme_hits"] or True             # field present on the pass path too


def test_late_alias_of_a_visible_entity_is_forbidden(gate):
    """Pass-2 F3 corollary: an alias revealed LATER than the bookmark is future knowledge even when
    its entity is already visible — a bm-2 recap must not use a nickname first revealed at ch5."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        fyodor = next(c["entity_id"] for c in mem.view(1).characters()
                      if "Fyodor Pavlovitch" in c["canonical_name"])
        mem.add_alias(fyodor, "Zmeyulan", revealed_at=max_bm)      # late nickname, distinctive token
        sc = score_recap(mem, 2, "Old Zmeyulan schemed again.", read_text=read_text_upto(mem, 2))
    assert sc["future_entity_leaks"], sc


def test_sentence_initial_capitalization_does_not_whitelist(gate):
    """Pass-2 F2: 'But' begins sentences in ch1-2 prose. A future entity whose sole token is 'but'
    must STILL be caught — sentence-initial capitalization is not proper-noun evidence (the old rule
    (a) fail-open). Control: a REAL proper noun read mid-clause ('in Russia', ch2) is still
    reader-parity-dropped (no over-block regression)."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        mem.add_entity("But", "place", revealed_at=max_bm)         # adversarial future sole-token name
        s_attack = score_recap(mem, 2, "Then the but was destroyed.", read_text=rt2)
        s_control = score_recap(mem, 2, "He left Russia for good.", read_text=rt2)
    assert s_attack["future_entity_leaks"], "sentence-initial 'But' must not whitelist the future token"
    assert not s_control["future_entity_leaks"], \
        f"'Russia' read mid-clause in ch2 must still be parity-dropped, got {s_control['future_entity_leaks']}"


def test_mis_stamped_shared_token_does_not_whitelist(gate):
    """Pass-2 F1: a visible entity's token whitelists a future name ONLY if the reader actually read
    it. Pinned with SYNTHETIC side-channel-free names (mutation N4 survived a Zossima version of this
    test because the fixture's real 'Alyosha'@4 late alias independently flagged the probe recap):
    future 'Qorvath'@max has no aliases and shares its sole token with a planted mis-stamped visible
    'Ivan Qorvath'@1 whose 'qorvath' token appears NOWHERE in read prose — it must not whitelist."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity("Qorvath", "character", revealed_at=max_bm)     # the future entity
        mem.add_entity("Ivan Qorvath", "character", revealed_at=1)     # the planted mis-stamp
        rt2 = read_text_upto(mem, 2)
        sc = score_recap(mem, 2, "Then Qorvath appeared at the gate.", read_text=rt2)
        assert any("Qorvath" in n for n in sc["future_entity_leaks"]), \
            f"the mis-stamp must not disarm the gate for 'qorvath', got {sc['future_entity_leaks']}"
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "Then Qorvath appeared at the gate.", read_text=rt2)
    # The composite real-data variant (fixture reveal structure): the same mis-stamp trick with
    # 'Fyodor Zossima'@1 still yields a rejected recap (via ANY mechanism — defense in depth).
    store2, _, _ = build_fixture_store(str(store._data_dir) + "_zoss")
    with store2.book(BOOK_ID) as mem:
        mem.add_entity("Fyodor Zossima", "character", revealed_at=1)
        rt2 = read_text_upto(mem, 2)
        with pytest.raises(SpoilerGateError):
            assert_recap_safe(mem, 2, "The elder Zossima guides Alyosha.", read_text=rt2)


def test_reveal_correctness_catches_partial_token_mis_stamp(gate):
    """Pass-2 F1 (detector leg): 'Fyodor Zossima'@1 was invisible to the ANY-token check ('fyodor'
    alone validated it). EVERY token locatable somewhere in the book's prose must appear by the
    stamp's ordinal — 'zossima' (first in prose @4) does not appear by ch1, so this IS a mis-stamp."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity("Fyodor Zossima", "character", revealed_at=1)
        checked, bad, bad_ex = reveal_correctness_eval(mem)
    assert any("Fyodor Zossima" in name for name, _ra in bad_ex), bad_ex


def test_spoiler_gate_error_message_is_name_free(gate):
    """Pass-2 DAL-lens finding 4: the rejection's str(e) must NOT carry future entity names (the error
    channel would otherwise spoil MORE than the recap it rejects); the specifics live in e.details."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        rt2 = read_text_upto(mem, 2)
        with pytest.raises(SpoilerGateError) as ei:
            assert_recap_safe(mem, 2, "The elder Zossima guides Alyosha.", read_text=rt2)
    msg = str(ei.value).lower()
    assert "zossima" not in msg and "sofya" not in msg, f"str(e) leaks future names: {msg}"
    assert ei.value.details["future_entity_leaks"], "the specifics must survive in e.details"


def test_raw_select_denied_even_after_audit_all(gate):
    """Pass-2 DAL-lens finding 1 (statement-cache authorizer bypass): after _audit_all has prepared
    'SELECT * FROM entities' under an engaged guard, the IDENTICAL string raw must STILL be denied —
    the authorizer must run on every execution, not only at first prepare."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        mem._audit_all("entities")                                  # arms the statement cache
        with pytest.raises(sqlite3.DatabaseError):
            mem._conn.execute("SELECT * FROM entities").fetchall()  # the exact cached string


def test_validity_snapshot_flips_on_chapter_reveal_move(gate):
    """Pass-2 F4: moving a chapter's revealed_at via the public add_chapter UPDATE branch changes what
    every bookmark sees (the live-chapter semijoin) — the snapshot MUST flip or a stale cached recap
    describing a now-unread chapter would be served."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        ch2 = next(c for c in mem.view(2).chapters() if c["revealed_at"] == 2)
        row = next(r for r in mem._audit_all("chapters") if r["chapter_key"] == ch2["chapter_key"])
        before = validity_snapshot(mem, 4)
        mem.add_chapter(ch2["chapter_key"], 7, href="x", title=row["title"],
                        content_hash=row["content_hash"])           # UPDATE branch: ordinal 2 -> 7
        after = validity_snapshot(mem, 4)
    assert before != after, "a chapter reveal move must invalidate cached recaps"


def test_validity_snapshot_flips_on_event_order_swap(gate):
    """Pass-2 F4 (LOW leg): order_idx drives timeline()/the recap's KEY EVENTS ordering — swapping two
    same-chapter events must flip the snapshot."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        evs = [r for r in mem._audit_all("events")
               if r["revealed_at"] == 1 and r["retracted_at"] is None][:2]
        assert len(evs) == 2, "fixture needs two ch1 events"
        before = validity_snapshot(mem, 4)
        with mem._writer():
            mem._conn.execute("UPDATE events SET order_idx=? WHERE event_id=?",
                              (evs[1]["order_idx"], evs[0]["event_id"]))
            mem._conn.execute("UPDATE events SET order_idx=? WHERE event_id=?",
                              (evs[0]["order_idx"], evs[1]["event_id"]))
        after = validity_snapshot(mem, 4)
    assert before != after


def test_future_event_participants_do_not_flip_an_earlier_snapshot(gate):
    """Pass-2 DAL-lens finding 3: a FUTURE event's participant links must not invalidate an earlier
    bookmark's cached recap (over-invalidation = a paid re-synthesis per ingested chapter)."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        aly = next(c["entity_id"] for c in mem.view(1).characters())
        before = validity_snapshot(mem, 2)
        mem.add_event("a far-future gathering", revealed_at=max_bm, order_idx=99,
                      participants=[(aly, "participant")])
        after = validity_snapshot(mem, 2)
    assert before == after, "future participants must not churn earlier bookmarks' cache keys"


def test_hyphenated_future_name_is_caught(gate):
    """Pass-3 F-P3-3 (fail-open, proved): forbidden tokens kept hyphens/apostrophes while recap words
    split on them — a future 'Eye-Witness' was unenforceable. Forbidden/visible tokens are now
    sub-tokenized the same way as recap words (mirroring reveal_correctness's pass-2 F1 fix)."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity("Eye-Witness", "character", revealed_at=max_bm)
        sc = score_recap(mem, 2, "The eye-witness betrayed them all.", read_text=read_text_upto(mem, 2))
    assert sc["future_entity_leaks"], sc


def test_replace_state_does_not_churn_earlier_snapshots(gate):
    """Pass-3 F-P3-2 (proved): the pipeline's replace_state stamps the OLD state row's invalid_at at
    the NEW chapter's ordinal; entity_state.invalid_at is never surfaced by any view read, so hashing
    the raw future value churned every earlier bookmark's cache key on every ingest (a paid
    re-synthesis per chapter). A future-dated invalid_at on a non-surfaced table must NOT flip an
    earlier snapshot; edges KEEP the raw value (relationships() returns invalid_at)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        aly = next(c["entity_id"] for c in mem.view(1).characters())
        sid = mem.add_state(aly, revealed_at=1, status={"note": "one"})
        before = validity_snapshot(mem, 2)
        mem.replace_state(sid, at=5, status={"note": "advanced later"})   # what ingest of ch5 does
        after = validity_snapshot(mem, 2)
        assert before == after, "a future-dated state supersession must not churn bm=2's cache key"
        # edges: the raw invalid_at IS view-surfaced -> a future-dated end still flips (kept behavior)
        edge = next(r for r in mem._audit_all("edges")
                    if r["revealed_at"] <= 2 and r["invalid_at"] is None and r["retracted_at"] is None)
        b2 = validity_snapshot(mem, 2)
        mem.end_edge(edge["edge_id"], at=50)
        assert validity_snapshot(mem, 2) != b2, "edges surface invalid_at -> must still flip"


def test_validity_snapshot_and_cache_key_reject_bool_bookmark(gate):
    """Pass-2 DAL-lens finding 5: mirror the DAL's int-not-bool guard (uniform fail-closed surface)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        with pytest.raises(ValueError):
            validity_snapshot(mem, True)
        with pytest.raises(ValueError):
            validity_snapshot(mem, 1.5)
    with pytest.raises(ValueError):
        cache_key("b", True, "snap")


# =========================================================================== vector 1b: reveal-correctness
def test_reveal_correctness_no_mis_stamps(gate):
    """INDEPENDENT of the DAL filter (defeats the circular ground truth): every named entity's name
    first appears in PROSE by its revealed_at chapter. 0 mis-stamps + non-vacuous."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        checked, bad, bad_ex = reveal_correctness_eval(mem)
    assert bad == 0, bad_ex[:5]
    assert checked > 5, f"vacuous: only {checked} named entities checked"


def test_reveal_correctness_catches_a_mis_stamp(gate):
    """FALSIFIABILITY: an entity stamped EARLIER than its name ever appears in prose (a mis-stamp that a
    self-consistent revealed_at-vs-revealed_at check could never catch) IS flagged."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity("Zzyzxia", "character", revealed_at=1)   # a name in no chapter's prose
        checked, bad, bad_ex = reveal_correctness_eval(mem)
    assert bad >= 1, "a name-not-in-prose stamped @1 must be flagged as a mis-stamp"
    assert any("Zzyzxia" in name for name, _ra in bad_ex), bad_ex


def test_reveal_correctness_flags_a_stamp_past_the_end_of_all_prose(gate):
    """Review pass-1 (correctness lens): an entity stamped at an ordinal BEYOND all prose can never
    appear in prose by its revealed_at — it must be FLAGGED (the spike's behavior), not silently
    skipped. And a gap-ordinal stamp whose name DID appear in earlier prose is NOT a false mis-stamp."""
    store, _, max_bm = gate
    with store.book(BOOK_ID) as mem:
        mem.add_entity("Zphlxia", "character", revealed_at=max_bm + 10)   # past the end of all prose
        mem.add_entity("Alyosha Gapcase", "character", revealed_at=max_bm + 10)  # name IS in ch1-5 prose
        checked, bad, bad_ex = reveal_correctness_eval(mem)
    assert any("Zphlxia" in name for name, _ra in bad_ex), \
        f"an out-of-prose-range stamp of an unseen name must be flagged, got {bad_ex}"
    assert not any("Gapcase" in name for name, _ra in bad_ex), \
        "a name already present in earlier prose must NOT be a false mis-stamp"


def test_reveal_correctness_flags_a_stamp_below_all_prose(tmp_path):
    """The floor-cumulative lookup's other edge (mutation M12): an entity stamped at an ordinal BELOW
    the first prose-bearing chapter (floor=None -> empty prose set) is FLAGGED, not skipped."""
    from app.llm.client import LLMClient
    from app.ingest.extraction.pipeline import all_entities, ingest_chapter, prepare_chapter
    from app.memory.store import Store
    ex_empty = {"chapter_summary": "s", "entities": [], "relationships": [], "events": [], "themes": []}
    ex2 = {"chapter_summary": "s", "entities": [
        {"canonical_name": "Hero Prime", "type": "character", "aliases": [],
         "matched_roster": False, "state": None}], "relationships": [], "events": [], "themes": []}
    store, client = Store(data_dir=str(tmp_path)), LLMClient(provider="stub", allow_stub=True)
    ch1 = {"ordinal": 1, "key": "g:c1", "title": "C1", "text": ""}
    ch2 = {"ordinal": 2, "key": "g:c2", "title": "C2", "text": "Hero Prime walked."}
    prepared1 = prepare_chapter(ch1, ex_empty, client, roster=[])
    with store.book("g", meta=dict(title="G")) as mem:
        ingest_chapter(mem, ch1, prepared1)
        roster = all_entities(mem.view(1))
    prepared2 = prepare_chapter(ch2, ex2, client, roster=roster)
    with store.book("g") as mem:
        ingest_chapter(mem, ch2, prepared2)
        mem.add_entity("Prosequent", "character", revealed_at=1)   # stamped BELOW all prose (ch1 empty)
        checked, bad, bad_ex = reveal_correctness_eval(mem)
    assert any("Prosequent" in name for name, _ra in bad_ex), \
        f"a below-all-prose stamp of an unseen name must be flagged, got {bad_ex}"


# =========================================================================== vector 4: cache coherence
def test_validity_snapshot_changes_on_retraction(gate):
    """A re-extraction (transaction-time retraction) of a fact valid@4 must flip the snapshot -> the
    recap cached at bookmark 4 misses and regenerates (never serves a stale recap)."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        before = validity_snapshot(mem, 4)
        summary = next(r for r in mem._audit_all("chapter_summaries")
                       if r["revealed_at"] <= 4 and r["retracted_at"] is None)
        mem._retract("chapter_summaries", "summary_id=?", (summary["summary_id"],))
        after = validity_snapshot(mem, 4)
    assert before != after, f"{before} -> {after}"


def test_validity_snapshot_changes_on_retroactive_invalid_at(gate):
    """A retroactive story-time supersession (invalid_at backdated to <= bm) on an edge visible@4 must
    flip the snapshot."""
    store, _, _ = gate
    with store.book(BOOK_ID) as mem:
        edge = next((r for r in mem._audit_all("edges")
                     if r["revealed_at"] <= 3 and r["invalid_at"] is None and r["retracted_at"] is None), None)
        assert edge is not None, "fixture must contain a live edge revealed by ch3 to supersede"
        before = validity_snapshot(mem, 4)
        with mem._writer():
            mem._conn.execute("UPDATE edges SET invalid_at=? WHERE edge_id=? AND book_id=?",
                              (edge["revealed_at"] + 1, edge["edge_id"], mem._book_id))
        after = validity_snapshot(mem, 4)
    assert before != after, f"{before} -> {after}"


def test_cache_key_keys_on_model_prompt_and_atomset():
    """Inv 7: the recap-cache key includes the synth model + recap-prompt version + atom-set version,
    so a model/prompt upgrade or a renumber forces a cache miss. Deterministic for identical inputs."""
    base = cache_key("karamazov", 4, "snap", synth_model="m1", recap_prompt_version="v1",
                     atom_set_version="a1")
    assert base == cache_key("karamazov", 4, "snap", synth_model="m1", recap_prompt_version="v1",
                             atom_set_version="a1")                      # stable
    assert base != cache_key("karamazov", 4, "snap", synth_model="m2", recap_prompt_version="v1",
                             atom_set_version="a1")                      # synth model change
    assert base != cache_key("karamazov", 4, "snap", synth_model="m1", recap_prompt_version="v2",
                             atom_set_version="a1")                      # recap prompt change
    assert base != cache_key("karamazov", 4, "snap", synth_model="m1", recap_prompt_version="v1",
                             atom_set_version="a2")                      # renumber (atom-set) change
    assert base != cache_key("karamazov", 5, "snap", synth_model="m1", recap_prompt_version="v1",
                             atom_set_version="a1")                      # different bookmark
