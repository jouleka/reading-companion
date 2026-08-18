"""Module C / resolve.py — the layered cast-roster resolver lifted near-verbatim (ADR 0007 D-A1 (a)).
These tests re-prove the exact review findings ADR 0003 rev1/rev2 fixed: roster-link (exact + fuzzy),
exact, name-like alias overlap, the role-epithet stop-list (no false-merge of distinct people), and
layer-4 embedding FORCE-OFF unless a real backend is supplied (+ the margin gate when it is on).
"""
from app.ingest.extraction.resolve import (
    _namelike, _norm, _proper_tokens, resolve_chapter, resolve_one,
)


def _ent(name, type="character", aliases=None, matched=False, state=None):
    return {"canonical_name": name, "type": type, "aliases": aliases or [],
            "matched_roster": matched, "state": state}


def _roster_entry(eid, name, type="character", aliases=None):
    return {"entity_id": eid, "canonical_name": name, "type": type, "aliases": aliases or []}


# ---- layer 1: roster-link --------------------------------------------------

def test_roster_link_exact_canonical_merges():
    roster = [_roster_entry(7, "Alexey Fyodorovitch Karamazov")]
    d = resolve_one(_ent("Alexey Fyodorovitch Karamazov", matched=True), roster)
    assert d == {"action": "merge", "entity_id": 7, "method": "roster-link", "score": 1.0}


def test_roster_link_fuzzy_token_subset_links_partial_name():
    # 'Ivan Karamazov' must link to 'Ivan Fyodorovitch Karamazov' (token subset) not spawn a 2nd Ivan
    roster = [_roster_entry(3, "Ivan Fyodorovitch Karamazov")]
    d = resolve_one(_ent("Ivan Karamazov", matched=True), roster)
    assert d["action"] == "merge" and d["entity_id"] == 3 and d["method"] == "roster-fuzzy"


def test_unmatched_roster_claim_creates_and_warns():
    # matched_roster=true but no roster match -> create, flagged (never silently duplicated)
    d = resolve_one(_ent("Grushenka", matched=True), [_roster_entry(1, "Ivan Fyodorovitch Karamazov")])
    assert d["action"] == "create" and d["warn_unmatched_link"] is True


# ---- layer 2/3: exact + name-like alias ------------------------------------

def test_exact_canonical_merge_without_roster_flag():
    roster = [_roster_entry(5, "Fyodor Pavlovitch Karamazov")]
    d = resolve_one(_ent("Fyodor Pavlovitch Karamazov"), roster)
    assert d["action"] == "merge" and d["entity_id"] == 5 and d["method"] == "exact"


def test_alias_overlap_merges_on_namelike_form():
    roster = [_roster_entry(9, "Dmitri Fyodorovitch Karamazov", aliases=["Mitya"])]
    d = resolve_one(_ent("Mitya"), roster)
    assert d["action"] == "merge" and d["entity_id"] == 9 and d["method"] == "alias"


def test_named_honorific_variant_merges_with_the_unique_bare_name():
    roster = [_roster_entry(7, "Zossima")]
    d = resolve_one(_ent("Father Zossima"), roster)
    assert d["action"] == "merge" and d["entity_id"] == 7
    assert d["method"] == "honorific"


def test_honorific_does_not_reduce_a_family_name_to_an_ambiguous_surname():
    roster = [_roster_entry(1, "Fyodor Karamazov"), _roster_entry(2, "Ivan Karamazov")]
    assert resolve_one(_ent("Father Karamazov"), roster)["action"] == "create"


# ---- the role-epithet stop-list (precision) --------------------------------

def test_shared_role_epithet_does_not_merge_distinct_people():
    # the two wives both carry the epithet 'Mother'; it must NOT be a merge key (they are different people)
    roster = [_roster_entry(1, "Adelaida Ivanovna Miusov", aliases=["Mother"])]
    d = resolve_one(_ent("Sofya Ivanovna", aliases=["Mother"]), roster)
    assert d["action"] == "create"                          # no false merge on the shared epithet


def test_pure_epithet_canonical_has_no_merge_key():
    # 'the Superior' is an epithet, not a name -> no merge key; two of them stay distinct (safe)
    roster = [_roster_entry(2, "the Superior")]
    d = resolve_one(_ent("the Superior"), roster)
    assert d["action"] == "create"
    assert _namelike("the Superior") is False
    assert _proper_tokens("the elder") == []                # determiner + role noun -> nothing name-like


def test_unicode_normalization_merges_canonically_equivalent_names_without_folding_diacritics():
    roster = [_roster_entry(7, "José")]  # decomposed e + combining acute
    assert resolve_one(_ent("José"), roster)["entity_id"] == 7
    assert _norm("Müller") != _norm("Muller")


def test_non_english_role_epithets_are_not_identity_merge_keys():
    for role in ("мать", "madre", "母亲"):
        assert _namelike(role) is False
        assert resolve_one(_ent(role), [_roster_entry(1, role)])["action"] == "create"


# ---- layer 4: embedding KNN is FORCE-OFF without a real backend -------------

def _fake_embed(group):
    """Deterministic: any text whose lowercased form hits a key in `group` gets that key's vector."""
    def emb(texts):
        out = []
        for t in texts:
            tl = t.lower()
            vec = next((v for k, v in group.items() if k in tl), [0.0, 0.0, 1.0])
            out.append(vec)
        return [out[0]] if len(out) == 1 else out
    return emb


def test_embedding_layer_off_when_no_backend():
    # 'Mitya' vs 'Dmitri' fall through layers 1-3 (no link, no exact, no alias); with embed_fn=None the
    # embedding layer is disabled -> create (the lexical stand-in over-merges siblings, so it must be off)
    roster = [_roster_entry(9, "Dmitri")]
    d = resolve_one(_ent("Mitya"), roster, embed_fn=None)
    assert d["action"] == "create"


def test_embedding_layer_merges_with_clear_winner_and_margin():
    roster = [_roster_entry(9, "Dmitri"), _roster_entry(4, "Ivan")]
    emb = _fake_embed({"mitya": [1.0, 0.0, 0.0], "dmitri": [1.0, 0.0, 0.0], "ivan": [0.0, 1.0, 0.0]})
    d = resolve_one(_ent("Mitya"), roster, embed_fn=emb)
    assert d["action"] == "merge" and d["entity_id"] == 9 and d["method"] == "embedding"


def test_embedding_layer_refuses_an_ambiguous_match_below_margin():
    # two candidates both near the query (margin < 0.05) -> no merge (no false-merge on ambiguity)
    roster = [_roster_entry(9, "Dmitri"), _roster_entry(8, "Dmitri Two")]
    emb = _fake_embed({"mitya": [1.0, 0.0, 0.0], "dmitri two": [0.999, 0.0447, 0.0],
                       "dmitri": [1.0, 0.0, 0.0]})
    d = resolve_one(_ent("Mitya"), roster, embed_fn=emb)
    assert d["action"] == "create"


# ---- pass-1 review regressions: false-merge precision -----------------------

def test_roster_fuzzy_does_not_merge_across_types():
    # pass-1 HIGH: layer-1b token-subset fuzzy was cross-type, so a PLACE sharing tokens with a roster
    # CHARACTER (matched_roster=true) silently merged into it. Cross-type identity is never legitimate.
    roster = [_roster_entry(1, "Ivan Fyodorovitch Karamazov", type="character")]
    d = resolve_one(_ent("Ivan Karamazov", type="place", matched=True), roster)
    assert d["action"] == "create"                          # the place is NOT absorbed into the character


def test_possessive_epithet_is_not_a_merge_key():
    # pass-1 HIGH: 'Mother's' bypassed the role-noun stop-list (only the bare 'Mother' was caught), so two
    # distinct women sharing the alias "Mother's" false-merged. The possessive must classify as an epithet.
    assert _namelike("Mother's") is False
    assert _proper_tokens("the General's") == []
    roster = [_roster_entry(1, "Adelaida Ivanovna Miusov", aliases=["Mother's"])]
    d = resolve_one(_ent("Sofya Ivanovna", aliases=["Mother's"]), roster)
    assert d["action"] == "create"                          # distinct people stay distinct


def test_possessive_of_a_real_name_merges_consistently():
    # pass-2 MEDIUM: the merge key is now de-possessed too, so a possessive of a real name keys like its
    # base and the alias layer agrees with the (de-possessing) fuzzy layer (was: under-merged to a dup).
    roster = [_roster_entry(9, "Dmitri Fyodorovitch Karamazov", aliases=["Mitya"])]
    d = resolve_one(_ent("Mitya's"), roster)
    assert d["action"] == "merge" and d["entity_id"] == 9
    # but a real name ending in 's' is NOT over-stripped into a false merge
    assert _namelike("Boris") and _namelike("Charles")


# ---- ambiguity gate on layers 2/3 (review LOW fix) -------------------------

def test_exact_ambiguous_roster_collision_creates_not_first_hit():
    # two DISTINCT same-type entities share a canonical -> ambiguous -> create (not silently bound to the
    # first roster hit / roster order). Mirrors the layer-1b len(hits)==1 gate.
    roster = [_roster_entry(1, "Ivan"), _roster_entry(2, "Ivan")]
    d = resolve_one(_ent("Ivan"), roster)
    assert d["action"] == "create" and d.get("warn_ambiguous") is True


def test_alias_ambiguous_collision_creates():
    # two distinct entities each carry the same name-like alias -> ambiguous -> create, order-independent
    roster = [_roster_entry(1, "Alexander", aliases=["Sasha"]),
              _roster_entry(2, "Alexandra", aliases=["Sasha"])]
    assert resolve_one(_ent("Sasha"), roster)["action"] == "create"
    assert resolve_one(_ent("Sasha"), list(reversed(roster)))["action"] == "create"  # order-independent


def test_single_hit_still_merges_after_ambiguity_gate():
    # the common case (exactly one match) is unaffected by the new gate
    roster = [_roster_entry(5, "Fyodor"), _roster_entry(6, "Dmitri")]
    assert resolve_one(_ent("Fyodor"), roster)["entity_id"] == 5
    assert resolve_one(_ent("Dmitri", aliases=[]), roster)["entity_id"] == 6


# ---- resolve_chapter: within-chapter dup of a NEW entity collapses ---------

def test_resolve_chapter_collapses_intra_chapter_duplicate_of_new_entity():
    # the rev-2 PENDING-id case: 'Katerina Ivanovna' (new) + 'Katya' in ONE chapter must collapse; the
    # 2nd resolves to a ('PENDING', idx) placeholder the pipeline maps to the real id.
    ents = [_ent("Katerina Ivanovna", aliases=["Katya"]), _ent("Katya")]
    out = resolve_chapter(ents, [])
    assert out[0][1]["action"] == "create"
    assert out[1][1]["action"] == "merge"
    assert out[1][1]["entity_id"] == ("PENDING", 0)         # points at the first decision's index
