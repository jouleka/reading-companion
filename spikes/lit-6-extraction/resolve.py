#!/usr/bin/env python3
"""LIT-6 — cast-roster entity resolution (the anti-drift core).  [rev 2 — post adversarial review]

For each entity the extractor emits this chapter, decide: SAME canonical entity as one already in
the roster (merge → reuse entity_id) or new (create). Layered, cheapest first:

  1. roster-link   — the extractor was given the roster and set matched_roster=true. PRIMARY
                     (iText2KG). Treated as strong evidence:
                       1a exact canonical match;
                       1b token-subset fuzzy match across the WHOLE roster (cross-type), so
                          'Ivan Karamazov' links to 'Ivan Fyodorovitch Karamazov' instead of
                          spawning a second Ivan. An unmatched matched_roster=true is WARNED, not
                          silently duplicated.
  2. exact         — canonical_name matches a roster canonical_name (name-like only).
  3. alias overlap — name-like aliases only (proper names/nicknames), NEVER role epithets
                     ('the elder', 'Mother', 'the Superior') — those falsely merge distinct people.
  4. embedding KNN — cosine ≥ threshold AND a clear margin over the 2nd-best. DISABLED unless a
                     REAL semantic embedding backend is supplied (the lexical stand-in over-merges
                     siblings — Dmitri/Ivan cosine ≈ 0.824 — so it must not be a merge authority).

Review fixes: role-epithet stop-list (case-insensitive, applied to canonical too); case-insensitive
name-likeness (orthography ≠ identity); matched_roster fuzzy/cross-type linkage; embedding off on the
lexical stand-in + margin requirement.
"""
from llm import cosine

# Role/relationship nouns: a surface form built only from these (+ determiners) is an EPITHET,
# not a name, and must never be a merge key. (Reproduced: 'the elder' merged Ivan & Zossima;
# 'Mother' merged the two wives.)
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
}
_PRONOUNS = {"i", "us", "we", "he", "she", "they", "me", "him", "them", "you", "it", "my"}


def _norm(s):
    return " ".join(s.lower().replace("‐", "-").replace("ï", "i").replace("ü", "u").split())


def _proper_tokens(form):
    """Tokens that are actual name material (drop role nouns, determiners, pronouns, short bits)."""
    return [t for t in _norm(form).split() if len(t) >= 2 and t not in ROLE_NOUNS and t not in _PRONOUNS]


def _namelike(form):
    """True if a surface form is a usable coreference key (a proper name/nickname), not a role
    epithet. Case-INSENSITIVE (Russian diminutives appear lowercased: 'grushenka', 'mitya')."""
    return len(_proper_tokens(form)) >= 1


def _match_forms(name, aliases):
    """The normalized forms safe to MERGE on: name-like canonical + name-like aliases only.
    If even the canonical is a pure epithet ('the Superior'), the entity has NO merge key and can
    only be created — safe (no false merge) at the cost of leaving an ambiguous epithet unlinked."""
    forms = set()
    for f in [name] + list(aliases or []):
        if _namelike(f):
            forms.add(_norm(f))
    return forms


def resolve_one(ent, roster, embed_fn=None, threshold=0.82):
    """roster: [{canonical_name,type,aliases,entity_id,embed?}]. Returns a decision dict.
    embed_fn is None unless a REAL semantic embedding backend is configured (layer 4 stays off
    on the lexical stand-in)."""
    forms = _match_forms(ent["canonical_name"], ent.get("aliases", []))
    cand_tokens = set(_proper_tokens(ent["canonical_name"]))
    same_type = [r for r in roster if r["type"] == ent["type"]]

    # 1a. roster-link, exact canonical (name-like)
    if ent.get("matched_roster"):
        for r in same_type:
            if _namelike(r["canonical_name"]) and _norm(ent["canonical_name"]) == _norm(r["canonical_name"]):
                return {"action": "merge", "entity_id": r["entity_id"], "method": "roster-link", "score": 1.0}
        # 1b. roster-link fuzzy: token-subset across the WHOLE roster (the LLM asserted a link)
        if len(cand_tokens) >= 2:
            hits = [r for r in roster
                    if cand_tokens and cand_tokens <= set(_proper_tokens(r["canonical_name"]))]
            if len(hits) == 1:
                return {"action": "merge", "entity_id": hits[0]["entity_id"], "method": "roster-fuzzy", "score": 0.9}

    # 2. exact canonical (name-like)
    if _namelike(ent["canonical_name"]):
        for r in same_type:
            if _norm(ent["canonical_name"]) == _norm(r["canonical_name"]):
                return {"action": "merge", "entity_id": r["entity_id"], "method": "exact", "score": 1.0}

    # 3. alias overlap (name-like forms only)
    for r in same_type:
        if forms & _match_forms(r["canonical_name"], r.get("aliases", [])):
            return {"action": "merge", "entity_id": r["entity_id"], "method": "alias", "score": 1.0}

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
    """Resolve a chapter's entities IN ORDER, growing a working roster so two new mentions of the
    same entity within one chapter also collapse. Returns [(entity, decision)] aligned to input."""
    work = [dict(r) for r in roster]
    out = []
    for e in entities:
        d = resolve_one(e, work, embed_fn, threshold)
        out.append((e, d))
        if d["action"] == "create":
            work.append({"canonical_name": e["canonical_name"], "type": e["type"],
                         "aliases": list(e.get("aliases", [])), "entity_id": ("PENDING", len(out) - 1)})
    return out
