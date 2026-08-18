"""LIT-6 — cast-roster entity resolution (the anti-drift core). Lifted near-verbatim from the
twice-reviewed spike (``spikes/lit-6-extraction/resolve.py``) per ADR 0007 D-A1 group (a): pure,
behaviour-defining safety logic. The ONLY production change is the import seam — ``cosine`` now comes
from ``app.llm.client`` (the re-ported LLM module that preserves the spike's ``from llm import cosine``).

For each entity the extractor emits this chapter, decide: SAME canonical entity as one already in the
roster (merge -> reuse entity_id) or new (create). Layered, cheapest first:

  1. roster-link   — the extractor was given the roster and set matched_roster=true. PRIMARY (iText2KG):
                       1a exact canonical match;
                       1b token-subset fuzzy match across the WHOLE roster (cross-type), so
                          'Ivan Karamazov' links to 'Ivan Fyodorovitch Karamazov' instead of spawning a
                          second Ivan. An unmatched matched_roster=true is WARNED, not silently duplicated.
  2. exact         — canonical_name matches a roster canonical_name (name-like only).
  3. alias overlap — name-like aliases only (proper names/nicknames), NEVER role epithets
                     ('the elder', 'Mother', 'the Superior') — those falsely merge distinct people.
  4. embedding KNN — cosine >= threshold AND a clear margin over the 2nd-best. DISABLED unless a REAL
                     semantic embedding backend is supplied (the lexical stand-in over-merges siblings —
                     Dmitri/Ivan cosine ~ 0.824 — so it must not be a merge authority).
"""
from app.llm.client import cosine
from app.unicode_text import normalize_text

# Role/relationship nouns: a surface form built only from these (+ determiners) is an EPITHET, not a
# name, and must never be a merge key. (Reproduced: 'the elder' merged Ivan & Zossima; 'Mother' merged
# the two wives.)
ROLE_NOUNS = {
    "mother", "father", "brother", "sister", "son", "daughter", "wife", "husband", "child",
    "children", "parent", "elder", "superior", "abbot", "monk", "novice", "priest", "nun",
    "servant", "cousin", "uncle", "aunt", "grandmother", "grandfather", "widow", "widower",
    "lady", "gentleman", "man", "woman", "boy", "girl", "old", "young", "little", "general",
    "captain", "colonel", "madame", "madam", "mister", "mr", "mrs", "doctor", "dr", "narrator",
    "author", "hero", "heroine", "family", "the", "a", "an", "this", "that", "his", "her",
    "their", "our", "my", "your", "its", "one", "same", "first", "second", "third", "eldest",
    "youngest", "former", "late", "dead", "deceased", "crazy", "poor", "drunken", "of", "and",
    "to", "in", "buffoon", "landowner", "benefactor", "patron", "teacher", "student", "people",
    "madre", "padre", "hermano", "hermana", "hijo", "hija", "esposa", "esposo",
    "mère", "père", "frère", "sœur", "fils", "fille", "mari", "femme",
    "mutter", "vater", "bruder", "schwester", "sohn", "tochter", "ehefrau", "ehemann",
    "мать", "отец", "брат", "сестра", "сын", "дочь", "жена", "муж", "монах", "священник",
    "母亲", "父亲", "哥哥", "弟弟", "姐姐", "妹妹", "儿子", "女儿", "妻子", "丈夫",
}
_PRONOUNS = {"i", "us", "we", "he", "she", "they", "me", "him", "them", "you", "it", "my"}
_NAME_HONORIFICS = {
    "father", "elder", "brother", "sister", "abbot", "priest", "madame", "madam", "mister",
    "mr", "mrs", "doctor", "dr", "captain", "colonel", "general",
}


def _norm(s):
    return normalize_text((s or "").replace("‐", "-"))


def _depossess(t):
    """Strip a trailing possessive so 'Mother's' / 'the General's' classify by their HEAD noun against
    ROLE_NOUNS (review fix): without this, a possessive epithet reads as name-like and false-merges
    distinct people who share it (the exact 'Mother merged the two wives' failure, in possessive form)."""
    for suf in ("'s", "’s", "'", "’"):
        if t.endswith(suf):
            return t[: -len(suf)]
    return t


def _proper_tokens(form):
    """Tokens that are actual name material (drop role nouns, determiners, pronouns, short bits).
    Possessives are de-possessed BEFORE the ROLE_NOUNS test so 'mother's' -> 'mother' is recognised as
    an epithet, not a name."""
    return [d for t in _norm(form).split()
            if (d := _depossess(t)) and len(d) >= 2 and d not in ROLE_NOUNS and d not in _PRONOUNS]


def _namelike(form):
    """True if a surface form is a usable coreference key (a proper name/nickname), not a role epithet.
    Case-INSENSITIVE (Russian diminutives appear lowercased: 'grushenka', 'mitya')."""
    return len(_proper_tokens(form)) >= 1


def _norm_key(f):
    """Normalized MERGE KEY with possessives stripped PER TOKEN, so the alias/exact layers key a
    possessive of a real name the same as its base — agreeing with the de-possessing name-likeness check
    (review fix): 'Mitya's' and 'Mitya' both key to 'mitya' (was: the alias layer under-merged the
    possessive into a duplicate while the fuzzy layer merged it). Role nouns are NOT stripped here (the
    key keeps its shape), only possessives."""
    return " ".join(_depossess(t) for t in _norm(f).split())


def _match_forms(name, aliases):
    """The normalized forms safe to MERGE on: name-like canonical + name-like aliases only, de-possessed
    so a possessive keys like its base. If even the canonical is a pure epithet ('the Superior'), the
    entity has NO merge key and can only be created — safe (no false merge) at the cost of leaving an
    ambiguous epithet unlinked."""
    forms = set()
    for f in [name] + list(aliases or []):
        if _namelike(f):
            forms.add(_norm_key(f))
    return forms


def _honorific_keys(name, aliases):
    """Narrow identity keys for a named honorific variant (Father Zossima <-> Zossima).

    Bare forms contribute only when they are exactly one proper-name token; titled forms contribute
    only when removing one leading honorific leaves that same shape. Thus ``Father Karamazov`` does
    not reduce to either ``Fyodor Karamazov`` or ``Ivan Karamazov``.
    """
    keys = set()
    for form in [name] + list(aliases or []):
        tokens = [_depossess(token) for token in _norm(form).split()]
        if not tokens or tokens[0] not in _NAME_HONORIFICS:
            continue
        tokens = tokens[1:]
        stripped = " ".join(tokens)
        if len(tokens) == 1 and len(_proper_tokens(stripped)) == 1:
            keys.add(_norm_key(stripped))
    return keys


def _bare_name_keys(name, aliases):
    keys = set()
    for form in [name] + list(aliases or []):
        tokens = [_depossess(token) for token in _norm(form).split()]
        if len(tokens) == 1 and len(_proper_tokens(tokens[0])) == 1:
            keys.add(_norm_key(tokens[0]))
    return keys


def _ambiguous():
    """A name/alias that matches 2+ DISTINCT same-type roster entities is not a safe merge key -> create a
    distinct entity + warn, rather than binding to an arbitrary (roster-order-dependent) hit (review LOW)."""
    return {"action": "create", "entity_id": None, "method": "new", "score": 0.0, "warn_ambiguous": True}


def resolve_one(ent, roster, embed_fn=None, threshold=0.82):
    """roster: [{canonical_name,type,aliases,entity_id,embed?}]. Returns a decision dict. embed_fn is
    None unless a REAL semantic embedding backend is configured (layer 4 stays off on the lexical
    stand-in)."""
    forms = _match_forms(ent["canonical_name"], ent.get("aliases", []))
    cand_tokens = set(_proper_tokens(ent["canonical_name"]))
    same_type = [r for r in roster if r["type"] == ent["type"]]

    # 1a. roster-link, exact canonical (name-like)
    if ent.get("matched_roster"):
        for r in same_type:
            if _namelike(r["canonical_name"]) and _norm(ent["canonical_name"]) == _norm(r["canonical_name"]):
                return {"action": "merge", "entity_id": r["entity_id"], "method": "roster-link", "score": 1.0}
        # 1b. roster-link fuzzy: token-subset across SAME-TYPE roster entries (the LLM asserted a link).
        # Type-scoped (review fix): a cross-type token overlap — e.g. a PLACE named after a CHARACTER —
        # is never the same identity; the old whole-roster scan silently absorbed the place into the person.
        if len(cand_tokens) >= 2:
            hits = [r for r in same_type
                    if cand_tokens and cand_tokens <= set(_proper_tokens(r["canonical_name"]))]
            if len(hits) == 1:
                return {"action": "merge", "entity_id": hits[0]["entity_id"], "method": "roster-fuzzy", "score": 0.9}

    # 2. exact canonical (name-like). AMBIGUITY GATE (review LOW fix, mirrors 1b): merge only on exactly
    # one match; if 2+ DISTINCT same-type roster entities share this canonical it is not a safe merge key
    # -> create + warn (don't silently bind to the first / roster order).
    if _namelike(ent["canonical_name"]):
        hits = [r for r in same_type if _norm(ent["canonical_name"]) == _norm(r["canonical_name"])]
        if len(hits) == 1:
            return {"action": "merge", "entity_id": hits[0]["entity_id"], "method": "exact", "score": 1.0}
        if len(hits) > 1:
            return _ambiguous()

    # 2b. named honorific: Father Zossima / Elder Zossima / Zossima are one identity. The deliberately
    # narrow single-token remainder avoids surname-only family collisions such as Father Karamazov.
    honorifics = _honorific_keys(ent["canonical_name"], ent.get("aliases", []))
    bare_names = _bare_name_keys(ent["canonical_name"], ent.get("aliases", []))
    hhits = [r for r in same_type
             if (honorifics & (_honorific_keys(r["canonical_name"], r.get("aliases", []))
                               | _bare_name_keys(r["canonical_name"], r.get("aliases", []))))
             or (bare_names & _honorific_keys(r["canonical_name"], r.get("aliases", [])))]
    if len(hhits) == 1:
        return {"action": "merge", "entity_id": hhits[0]["entity_id"],
                "method": "honorific", "score": 1.0}
    if len(hhits) > 1:
        return _ambiguous()

    # 3. alias overlap (name-like forms only) — same ambiguity gate
    ahits = [r for r in same_type if forms & _match_forms(r["canonical_name"], r.get("aliases", []))]
    if len(ahits) == 1:
        return {"action": "merge", "entity_id": ahits[0]["entity_id"], "method": "alias", "score": 1.0}
    if len(ahits) > 1:
        return _ambiguous()

    # 4. embedding KNN — only with a real backend, with a margin over 2nd-best
    if embed_fn is not None and same_type:
        text = ent["canonical_name"] + " " + " ".join(ent.get("aliases", []))
        vec = embed_fn([text])[0]
        scored = []
        for r in same_type:
            rv = r.get("embed") or embed_fn([r["canonical_name"] + " " + " ".join(r.get("aliases", []))])[0]
            r["embed"] = rv
            scored.append((cosine(vec, rv), r))
        scored.sort(reverse=True, key=lambda t: t[0])
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best[0] >= threshold and (best[0] - second) >= 0.05:   # require a clear margin
            return {"action": "merge", "entity_id": best[1]["entity_id"], "method": "embedding", "score": best[0]}

    warn = bool(ent.get("matched_roster"))   # LLM said it links but we couldn't find the match
    return {"action": "create", "entity_id": None, "method": "new", "score": 0.0, "warn_unmatched_link": warn}


def resolve_chapter(entities, roster, embed_fn=None, threshold=0.82):
    """Resolve a chapter's entities IN ORDER, growing a working roster so two new mentions of the same
    entity within one chapter also collapse. Returns [(entity, decision)] aligned to input."""
    work = [dict(r) for r in roster]
    out = []
    for e in entities:
        d = resolve_one(e, work, embed_fn, threshold)
        out.append((e, d))
        if d["action"] == "create":
            work.append({"canonical_name": e["canonical_name"], "type": e["type"],
                         "aliases": list(e.get("aliases", [])), "entity_id": ("PENDING", len(out) - 1)})
    return out
