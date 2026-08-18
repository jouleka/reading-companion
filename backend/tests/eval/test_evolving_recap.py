"""LIT-29 (piece 1) — the evolving flowing recap + the 'right now' one-liner.

These cover the store-read + prompt-shape units the endpoint composes:
  * ``delta_facts(db, bm)`` = ONLY the facts first revealed at chapter ``bm`` (the increment folded
    into the evolving recap) — a spoiler-bounded subset of ``supplied_facts``.
  * the flowing / evolve / now prompt builders — content shifts from a roster to what happened + why,
    the evolve prompt carries the prior recap + the delta, and every builder stays grounded.

The endpoint-level behaviour (evolve path taken, one-liner gated + cached, cast field, spoiler-safety
preserved) lives in tests/api/test_views.py against the real service.

NB the fixture's extractor emits generic per-chapter epithets ("the narrator") as fresh distinct
entities every chapter, so a by-NAME set-difference on characters is not clean. Chapter summaries are
per-chapter (collision-free), so the strict equality is anchored there; characters get subset +
new-material checks that the epithet repetition does not perturb.
"""
from _fixture import build_fixture_store, BOOK_ID

from app.eval.spoiler_gate.synthesis import (
    FLOWING_SYSTEM,
    NOW_SYSTEM,
    SYNTH_SYSTEM,
    delta_facts,
    evolve_prompt,
    flowing_system_for,
    now_prompt,
    supplied_facts,
    synth_prompt,
)


def _chars(f):
    return set(f["characters"])


def test_delta_facts_is_the_increment_not_the_whole_history(tmp_path):
    """The delta at N is what N ADDS, read through the SAME funnel so it can never widen past the
    bookmark. The chapter-N summary (collision-free) is EXACTLY the set difference; characters carry a
    genuinely new person (Sofya Ivanovna@3) without dragging the whole cast along."""
    store, _client, _max = build_fixture_store(tmp_path / "data")
    try:
        with store.book(BOOK_ID) as mem:
            full3 = supplied_facts(mem, 3)
            full2 = supplied_facts(mem, 2)
            d3 = delta_facts(mem, 3)
        # bounded: the delta is a subset of the bookmark-visible facts (never a future entity)
        assert _chars(d3) <= _chars(full3)
        # it carries genuinely NEW people, not merely the carried-over cast
        assert _chars(d3) - _chars(full2), "chapter 3 introduces a new character; the delta must show it"
        # the chapter summary delta is EXACTLY the newly-completed chapter's summary (per-chapter keyed,
        # so no epithet collision) — falsifiable: an unfiltered delta_facts would fail this equality
        assert set(d3["chapter_summaries"]) == set(full3["chapter_summaries"]) - set(full2["chapter_summaries"])
        assert len(d3["chapter_summaries"]) == 1, "one chapter completes at 3 -> one new summary"
    finally:
        store.close()


def test_delta_facts_at_a_later_chapter_does_not_re_tell_earlier_chapters(tmp_path):
    """The delta at 4 (elder Zossima@4) must not re-carry chapter 3's summary — evolve folds ONLY the
    new material; the prior recap already told the rest."""
    store, _client, _max = build_fixture_store(tmp_path / "data")
    try:
        with store.book(BOOK_ID) as mem:
            d4 = delta_facts(mem, 4)
            full3 = supplied_facts(mem, 3)
        assert d4["chapter_summaries"], "chapter 4 completed -> it has a new summary"
        assert set(d4["chapter_summaries"]).isdisjoint(set(full3["chapter_summaries"]))
    finally:
        store.close()


# ---- prompt builders (pure) -------------------------------------------------


def test_flowing_system_keeps_the_spoiler_contract_and_shifts_to_events():
    """The flowing recap system prompt MUST carry the ``SYNTH_SYSTEM`` anti-foreshadow contract through
    verbatim (the deterministic gate + judge assume the synthesizer was told not to foreshadow), and
    ADD the content shift: what happened + why, not a roster that re-introduces the cast each chapter."""
    assert SYNTH_SYSTEM in FLOWING_SYSTEM, "the spoiler-safe contract must not be dropped when reframing"
    low = FLOWING_SYSTEM.lower()
    assert "what has happened" in low
    assert "re-introduce" in low or "reintroduce" in low or "roster" in low


def test_evolve_prompt_carries_the_prior_recap_and_only_the_delta():
    """recap(N) = recap(N-1) + the delta, with an EXTEND (not restate) instruction. The prior recap and
    the new material are both present; the delta summary sits under a header the offline stub echoes so
    stub output stays grounded."""
    prior = "Fyodor set up a chaotic household after two failed marriages."
    delta = {"characters": ["Sofya Ivanovna"],
             "chapter_summaries": ["Sofya, the second wife, endured a wretched marriage."],
             "events": ["Sofya married Fyodor."]}
    p = evolve_prompt(5, prior, delta)
    assert prior in p                                              # extend the story as last told
    assert "Sofya Ivanovna" in p and "wretched marriage" in p     # fold in ONLY the new facts
    assert "extend" in p.lower() or "continue" in p.lower()       # extend/adjust, don't restate
    assert "CHAPTER SUMMARIES:" in p                              # stub-groundable
    assert p.index("CHAPTER SUMMARIES:") < p.index("wretched marriage")


def test_now_prompt_is_grounded_in_the_latest_chapter():
    """The 'right now' one-liner is built from the latest chapter's facts (the delta), under a header
    the stub echoes so the offline one-liner is grounded and clears the gate."""
    delta = {"characters": ["Sofya Ivanovna"],
             "chapter_summaries": ["Sofya endured a wretched marriage and fled the household."],
             "events": []}
    p = now_prompt(4, delta)
    assert "wretched marriage" in p
    assert "CHAPTER SUMMARIES:" in p


def test_now_system_is_a_short_anti_foreshadow_line():
    low = NOW_SYSTEM.lower()
    assert "foreshadow" in low or "to come" in low
    assert "sentence" in low                                       # one/two sentences, not a full recap


def test_non_novel_recap_prompts_use_neutral_section_language():
    facts = {"characters": [], "chapter_summaries": ["The section defines torque."], "events": []}
    system = flowing_system_for("reference").lower()
    cumulative = synth_prompt(2, facts, book_type="reference").lower()
    evolving = evolve_prompt(
        2,
        "The first section introduced force.",
        {"characters": [], "chapter_summaries": ["The section defines torque."], "events": []},
        book_type="reference",
    ).lower()
    current = now_prompt(
        2,
        {"characters": [], "chapter_summaries": ["The section defines torque."], "events": []},
        book_type="reference",
    ).lower()
    assert SYNTH_SYSTEM not in flowing_system_for("reference")
    assert "reading recap" in system and "plot" not in system
    assert "section 2" in cumulative
    assert "reading so far" in evolving
    assert "just finished section 2" in current


def test_non_english_flowing_system_keeps_english_output_and_source_spelled_names():
    system = flowing_system_for("novel", "ru").lower()
    assert "source language is ru" in system
    assert "companion prose in english" in system
    assert "preserving proper names in their source spelling" in system
    assert flowing_system_for("novel", "en") == FLOWING_SYSTEM
    assert flowing_system_for("novel", "und") == FLOWING_SYSTEM


# ---- LIT-30: the cast carries name -> entity_id pairs (for the clickable name cards) --------------


def test_visible_cast_pairs_every_surface_form_with_its_entity(tmp_path):
    """A character's canonical name AND each of its visible aliases resolve to the SAME entity_id, so a
    chip on any surface form ('Mitya', 'Dmitri') opens the one card. All bounded to <= bm."""
    from collections import Counter

    from app.api.views import _visible_cast

    store, _client, _max = build_fixture_store(tmp_path / "data")
    try:
        with store.book(BOOK_ID) as mem:
            cast = _visible_cast(mem, 3)
            visible_ids = {r["entity_id"] for r in mem.view(3).characters()}
        assert cast and all(isinstance(it["entity_id"], int) and it["name"] for it in cast)
        # every cast entity is a bookmark-visible character (never a future entity)
        assert {it["entity_id"] for it in cast} <= visible_ids
        # at least one character contributes multiple surface forms (canonical + alias) under one id
        assert any(n >= 2 for n in Counter(it["entity_id"] for it in cast).values())
        # each name appears once (a clean name -> id map for the frontend)
        names = [it["name"] for it in cast]
        assert len(names) == len(set(names))
    finally:
        store.close()


def test_character_card_ties_are_bookmark_bounded_and_closed(tmp_path):
    """LIT-30 card: identity (bio) + ties (relationships touching the entity), every tie endpoint
    bookmark-visible (referential closure — never a future character), each with a name + direction. An
    unknown/future id yields None so the route 404s."""
    from app.api.views import _character_card

    store, _client, _max = build_fixture_store(tmp_path / "data")
    try:
        with store.book(BOOK_ID) as mem:
            fid = next(x["entity_id"] for x in mem.view(5).characters()
                       if x["canonical_name"] == "Fyodor Pavlovitch Karamazov")
            card3, card5 = _character_card(mem, 3, fid), _character_card(mem, 5, fid)
            vis3 = set()
            for t in ("character", "place", "faction", "object"):
                vis3 |= {e["entity_id"] for e in mem.view(3).entities_of_type(t)}
            # a character revealed AFTER bm=3 (Sofya@3 is at 3; pick one strictly later, e.g. Zossima@4)
            future_char = next(x for x in mem.view(5).characters() if x["revealed_at"] > 3)
            card_future_hidden = _character_card(mem, 3, future_char["entity_id"])
            none_card = _character_card(mem, 5, 10_000_000)
        assert card5 and card5["ties"], "Fyodor has visible relationships at bm=5"
        assert card3 is not None and card3["type"] == "character" and card3["first_seen"] <= 3
        # one tie per related person (the extraction emits overlapping edges; the card is deduped)
        ids5 = [t["entity_id"] for t in card5["ties"]]
        assert len(ids5) == len(set(ids5)), "ties are deduped by endpoint — one line per person"
        # spoiler-safe: every tie endpoint at bm=3 is itself revealed by bm=3 (never a future character)
        assert all(t["entity_id"] in vis3 for t in card3["ties"])
        assert all(t["name"] and t["direction"] in ("in", "out") and isinstance(t["entity_id"], int)
                   for t in card5["ties"])
        # a character ingested but revealed later than the requested bookmark is HIDDEN (no leak)
        assert card_future_hidden is None
        assert none_card is None                                   # unknown/future id -> None (route 404s)
    finally:
        store.close()
