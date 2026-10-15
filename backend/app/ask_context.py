"""Deterministic, additive evidence context; no IO, inferred facts, or answer clearance.

The caller supplies only completed, source-verified candidates. Context IDs are server-derived hints
to other exact windows in that same chapter. Their inclusion establishes presence only: the normal
claim-specific semantic judge must still verify identity, attribution, and the actual assertion.
"""
from __future__ import annotations

from collections.abc import Iterable

from app.ask_selection import MAX_SELECTED_SOURCES, MAX_SOURCE_CANDIDATES, SourceSelectionError, validate_selection
from app.eval.spoiler_gate.grounding import STOPWORDS
from app.ingest.extraction.resolve import ROLE_NOUNS
from app.unicode_text import normalize_text, proper_words, word_tokens


class AskContextError(SourceSelectionError):
    """A malformed source/context relationship; safe for the existing generic selection error path."""


_QUESTION_WORDS = STOPWORDS | frozenset({
    "according", "please", "explain", "describe", "tell", "give", "find", "summarize",
    "summarise", "name", "how", "why", "where", "when", "book", "chapter", "source",
    "text", "read", "reading", "far",
})


def _invalid():
    return AskContextError("source context could not be completed")


def _validated_pool(selected, candidates):
    try:
        ids = [source["id"] for source in selected]
        canonical = validate_selection({"source_ids": ids}, candidates)
    except (SourceSelectionError, KeyError, TypeError, ValueError) as exc:
        raise _invalid() from exc
    if any(source != original for source, original in zip(selected, canonical, strict=True)):
        raise _invalid()
    available = {source["id"]: source for source in candidates}
    context = {}
    for source_id, source in available.items():
        if (not isinstance(source.get("chapter_key"), str) or not source["chapter_key"]
                or not isinstance(source.get("href"), str)):
            raise _invalid()
        references = source.get("context_ids", [])
        if not isinstance(references, list) or len(references) > MAX_SOURCE_CANDIDATES:
            raise _invalid()
        for reference in references:
            if isinstance(reference, bool) or not isinstance(reference, int) or reference not in available:
                raise _invalid()
            target = available[reference]
            if (target["chapter_key"] != source["chapter_key"]
                    or target["ordinal"] != source["ordinal"]):
                raise _invalid()
        continuity = source.get("continuity_ids", [])
        if not isinstance(continuity, list) or len(continuity) > 1:
            raise _invalid()
        for reference in continuity:
            if isinstance(reference, bool) or not isinstance(reference, int) or reference not in available:
                raise _invalid()
            target = available[reference]
            if (target["chapter_key"] == source["chapter_key"]
                    or target["ordinal"] != source["ordinal"] - 1):
                raise _invalid()
        context[source_id] = references + continuity
    return canonical, available, context


def _query_parts(question, known_names):
    if not isinstance(question, str) or not 2 <= len(question) <= 500 or not question.strip():
        raise _invalid()
    if isinstance(known_names, (str, bytes)) or not isinstance(known_names, Iterable):
        raise _invalid()
    try:
        visible = set(word_tokens(" ".join(known_names), min_length=2))
    except TypeError as exc:
        raise _invalid() from exc
    proper = {normalize_text(word.text) for word in proper_words(question) if len(word.text) >= 2}
    return proper, visible


def _query_names(question, known_names, observed):
    proper, visible = _query_parts(question, known_names)
    # Registered names such as May/Will must not disappear as modal stopwords. The fallback has no
    # identity roster, so remove ordinary question/discourse words before checking observed text.
    return ((proper & visible) if visible else (proper - _QUESTION_WORDS)) & observed


def _named_fallback(question, known_names, available, terms):
    proper, visible = _query_parts(question, known_names)
    # A partial identity roster must not silently erase an absent query name. Registered May/Will
    # remain possible names, while question words, roles, and honorifics cannot meet the two-part
    # minimum. Every required part must occur in quoted prose, never only in a chapter title.
    needed = ((proper - _QUESTION_WORDS) | (proper & visible & {"may", "will"})) - ROLE_NOUNS - {"grand"}
    observed = set().union(*terms.values())
    if len(needed & observed) < 2 or not needed <= observed:
        return []
    result, seen_evidence, missing = [], set(), set(needed)
    while missing and len(result) < 3:
        options = [source for source in available.values()
                   if missing & terms[source["id"]]
                   and (source["chapter_key"], source["ordinal"], source["text"]) not in seen_evidence]
        if not options:
            return []
        # max keeps the earlier candidate for an exact tie; shorter whole windows reduce the
        # later prompt without altering a byte or changing the candidate's stable source ID.
        best = max(options, key=lambda source: (len(missing & terms[source["id"]]), -len(source["text"])))
        result.append(dict(best))
        seen_evidence.add((best["chapter_key"], best["ordinal"], best["text"]))
        missing -= terms[best["id"]]
    return [] if missing else result


def complete_ask_context(question, selected, candidates, known_names=(), *, allow_named_fallback=False):
    """Append exact existing context within six sources, preserving every initial selection.

    Empty selection remains empty by default. An explicit opt-in may retrieve at most three exact
    windows fully covering at least two proper query-name parts before normal context completion.
    This is retrieval only: all normal answer-generation and claim-review gates remain required.
    Opening-to-previous-chapter continuity IDs must decrease the ordinal by exactly one and still
    refer to exact candidates. They are retrieval context, never proof of the same person or topic.
    Malformed context IDs or changed selected evidence fail closed. Metadata hints are followed
    deterministically (including bounded transitive hints), then missing observed query-name parts
    are covered greedily. This is necessary evidence presence, never proof of who spoke or acted.
    """
    if not isinstance(selected, (list, tuple)) or len(selected) > MAX_SELECTED_SOURCES:
        raise _invalid()
    if not isinstance(allow_named_fallback, bool):
        raise _invalid()
    if not selected and not allow_named_fallback:
        return []
    result, available, context = _validated_pool(selected, candidates)
    if not available:
        return []
    terms = {source_id: set(word_tokens(source["text"])) for source_id, source in available.items()}
    if not result:
        result = _named_fallback(question, known_names, available, terms)
        if not result:
            return []
        needed = set()  # The fallback already covered every required name part, without truncation.
    else:
        observed = set().union(*terms.values())
        needed = _query_names(question, known_names, observed)
    seen = {source["id"] for source in result}
    evidence = {(source["chapter_key"], source["ordinal"], source["text"]) for source in result}

    def append(source):
        key = source["chapter_key"], source["ordinal"], source["text"]
        if source["id"] in seen or key in evidence or len(result) >= MAX_SELECTED_SOURCES:
            return False
        result.append(dict(source))
        seen.add(source["id"])
        evidence.add(key)
        return True

    index = continuity_index = 0

    def close_continuity():
        nonlocal continuity_index
        while continuity_index < len(result) and len(result) < MAX_SELECTED_SOURCES:
            source = result[continuity_index]
            for context_id in source.get("continuity_ids", []):
                append(available[context_id])
            continuity_index += 1

    def close_context():
        nonlocal index
        # An earlier selector choice's ordinary neighbors must not crowd out the previous
        # chapter tail needed by a later selected opening. Resolve those bounded dependencies
        # across the whole current selection before spending spare slots on nearby prose.
        close_continuity()
        while index < len(result) and len(result) < MAX_SELECTED_SOURCES:
            source = result[index]
            for context_id in context[source["id"]]:
                append(available[context_id])
                close_continuity()
            index += 1

    close_context()

    present = set().union(*(terms[source["id"]] for source in result))
    missing = needed - present
    while missing and len(result) < MAX_SELECTED_SOURCES:
        chapters = {source["chapter_key"] for source in result}
        options = [source for source_id, source in available.items()
                   if source_id not in seen and missing & terms[source_id]
                   and (source["chapter_key"], source["ordinal"], source["text"]) not in evidence]
        if not options:
            break
        # Adding a whole exact window consumes its whole length in the next prompt, even where
        # it overlaps an existing source. Exact duplicates are excluded rather than rewritten.
        best = max(options, key=lambda source: (
            len(missing & terms[source["id"]]), source["chapter_key"] in chapters, -len(source["text"]),
        ))
        append(best)
        # A newly found name can be in a chapter opening whose referent needs an earlier exact
        # tail. Follow its validated hints too; that context may itself cover another missing name.
        close_context()
        present = set().union(*(terms[source["id"]] for source in result))
        missing = needed - present
    return result
