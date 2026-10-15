"""Narrow routes from an unhelpful lexical rejection to mandatory semantic review.

This module never clears an answer. Pure named identity clauses contain no event vocabulary, while
faithful speech and family paraphrases can use words absent from a source's phrasing. Those narrow
shapes may reach the independent cited-evidence judge; all other deterministic gates still apply.
Never pass a compound claim merely because one clause is an identity, speech, or family statement.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

from app.eval.spoiler_gate.grounding import EVENT_STEMS, STOPWORDS
from app.unicode_text import normalize_text, proper_words, word_tokens, words


_COPULA = re.compile(r"\s+(?:is|was|are|were)\s+", re.IGNORECASE)
_QUOTATION = re.compile(r'“([^”\n]+)”|"([^"\n]+)"')
_QUOTE_MARKER = "@EXACT_QUOTE@"
_SPEECH_VERB = (
    r"says?|said|shouts?|shouted|cries|cried|calls?|called|chants?|chanted|"
    r"repeats?|repeated|echoes|echoed|exclaims?|exclaimed|yells?|yelled"
)
_REPEAT_VERB = r"repeats?|repeated|echoes|echoed|take\s+up|takes\s+up|took\s+up"
_COLLECTIVE = frozenset({"the boys", "the children", "the crowd", "the group"})
_REPETITION = re.compile(
    rf"(?:and\s+)?(?P<speaker>.+?)\s+(?:{_REPEAT_VERB})\s+"
    r"(?:it|the\s+(?:cheer|chant|exclamation|words))", re.IGNORECASE,
)
_SPEECH_PREFIX = re.compile(
    rf"(?P<speaker>.+?)\s+(?:{_SPEECH_VERB})\s*[:,]?\s*{_QUOTE_MARKER}", re.IGNORECASE,
)
_CHEER_PREFIX = re.compile(
    rf"(?P<speaker>.+?)\s+(?:leads?|led)\s+(?:the\s+|a\s+)?"
    rf"(?:(?:last|final|closing)\s+)?(?:cheer|chant|chorus)\s*[:,]?\s*{_QUOTE_MARKER}",
    re.IGNORECASE,
)
_SPEECH_SUFFIX = re.compile(
    rf"{_QUOTE_MARKER}\s*,?\s*(?P<speaker>.+?)\s+(?:{_SPEECH_VERB})", re.IGNORECASE,
)
_KINSHIP_ROLES = frozenset("""
parent parents father fathers mother mothers son sons daughter daughters child children
brother brothers sister sisters sibling siblings spouse spouses wife wives husband husbands
uncle uncles aunt aunts nephew nephews niece nieces cousin cousins
grandparent grandparents grandfather grandfathers grandmother grandmothers
grandson grandsons granddaughter granddaughters grandchild grandchildren
stepfather stepfathers stepmother stepmothers stepson stepsons stepdaughter stepdaughters
stepbrother stepbrothers stepsister stepsisters stepsibling stepsiblings
""".split())
_KINSHIP_ORDER = frozenset("""
one two three four five six seven eight nine ten first second third fourth fifth sixth seventh
eighth ninth tenth eldest oldest youngest half
""".split()) | frozenset(str(n) for n in range(1, 11)) | frozenset({
    "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th",
})
_KINSHIP_LINKS = frozenset({"is", "are", "was", "were", "has", "have", "had"})
_KINSHIP_ATTACHMENTS = frozenset("""
because while when where after before then since until during therefore thus hence consequently
so if for as once yet now today yesterday tomorrow later earlier previously subsequently
""".split())
_KINSHIP_FUNCTION = STOPWORDS - _KINSHIP_ATTACHMENTS - frozenset("""
do does did done will would shall should can could may might must
""".split())
_POSSESSIVE = re.compile(r"['’]s\b", re.IGNORECASE)
_MODAL_MODIFIER = r"not|never|already|just|well|surely|certainly|possibly|probably|perhaps|really|also|only"
_MODAL_PERFECT = re.compile(
    rf"\b(?:must|might|may|could|would|should)\s+(?:(?:{_MODAL_MODIFIER})\s+)*"
    rf"have\s+(?:(?:{_MODAL_MODIFIER})\s+)*(?P<predicate>[^\W\d_]+)\b", re.IGNORECASE,
)
_COMMON_AUXILIARIES = frozenset({"be", "been", "being", "have", "has", "had", "do", "does", "did", "done"})
_CLAUSE_END = re.compile(r"[.!?;…\n]+|,\s*(?:but|yet)\b|\bhowever\b", re.IGNORECASE)
_INFERENCE_WORDING = re.compile(
    r"\b(?:infer|infers|inferred|inference|surmise|surmises|surmised|conjecture|conjectures|"
    r"conjectured|speculate|speculates|speculated|speculation|suppose|supposes|supposed|"
    r"perhaps|possibly|probably|possible|possibility|likely|uncertain|uncertainty)\b", re.IGNORECASE,
)


def _space(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _modal_predicates(text: str) -> dict[tuple[int, int], str]:
    return {
        match.span("predicate"): normalize_text(match["predicate"])
        for match in _MODAL_PERFECT.finditer(text)
        if normalize_text(match["predicate"]) not in STOPWORDS | _COMMON_AUXILIARIES
    }


def preserves_modal_qualification(
    claim_text: str, cited_sources: Iterable[Mapping[str, object]],
) -> bool:
    """Reject turning an exclusively modal-perfect source predicate into an unqualified fact.

    This is a narrow necessary check, not entailment. Derive predicates from the actual own cited
    text, never from book-specific words. An independent direct occurrence leaves that predicate to
    the semantic judge, which must still check its subject, negation, and meaning. Modal evidence
    about common auxiliaries does not restrict unrelated uses of words such as had/been/done.
    """
    if (not isinstance(claim_text, str) or not claim_text.strip() or len(claim_text) > 800
            or not isinstance(cited_sources, Iterable)):
        return False
    sources = []
    for source in cited_sources:
        if not isinstance(source, Mapping):
            return False
        value = source.get("text", source.get("excerpt", ""))
        if not isinstance(value, str):
            return False
        sources.append(unicodedata.normalize("NFC", value))
    occurrences: dict[str, list[bool]] = {}
    modal_names = set()
    for source in sources:
        modal = _modal_predicates(source)
        modal_names.update(modal.values())
        for word in words(source):
            token = normalize_text(word.text)
            occurrences.setdefault(token, []).append((word.start, word.end) in modal)
    guarded = {token for token in modal_names if all(occurrences[token])}
    if not guarded:
        return True
    # Sentence, semicolon, and clear adversative boundaries start fresh assertions. "Perhaps X,
    # but the dog spat ..." cannot borrow an earlier inference marker. Nor does "says/explains"
    # express uncertainty. Exact quotations retaining their original modal wording still pass.
    for clause in _CLAUSE_END.split(unicodedata.normalize("NFC", claim_text)):
        modal = _modal_predicates(clause)
        inference = bool(_INFERENCE_WORDING.search(clause))
        for word in words(clause):
            if (normalize_text(word.text) in guarded and (word.start, word.end) not in modal
                    and not inference):
                return False
    return True


def _name(value: str) -> bool:
    """Conservative named labels, not predicates/possessives disguised by capitalization."""
    value = value.strip()
    tokens = list(words(value))
    if not tokens or len(tokens) > 6 or value.casefold().endswith(("'s", "’s", "'", "’")):
        return False
    proper = {(word.start, word.end) for word in proper_words(value)}
    if any((word.start, word.end) not in proper or normalize_text(word.text) in STOPWORDS
           for word in tokens):
        return False
    # A capitalized event word is still an event, not an identity exception ("Aldric is Murdered").
    if any(any(token == stem or (len(stem) >= 4 and token.startswith(stem))
               for stem in EVENT_STEMS) for token in word_tokens(value)):
        return False
    remainder = value
    for word in reversed(tokens):
        remainder = remainder[:word.start] + remainder[word.end:]
    return all(char.isspace() or char in "-'’‐" for char in remainder)


def _subject(value: str, available: set[str]) -> bool:
    if normalize_text(value) in _COLLECTIVE:
        return set(word_tokens(value)) <= available
    return _name(value)


def _identity(text: str) -> bool:
    parts = _COPULA.split(text.rstrip(" .!"))
    return len(parts) == 2 and _name(parts[0]) and _name(parts[1])


def _kinship(text: str, sources: list[str]) -> bool:
    """Route only whole family statements, never infer the family tree from shared names.

    A source can list a father's sons while a faithful answer calls them brothers. The lexical
    mismatch must still reach an independent semantic judge, which can reject a wrong family pair.
    The finite vocabulary here cannot carry an added event, temporal/causal join, or description.
    """
    plain = _POSSESSIVE.sub("", text)
    tokens = list(words(plain))
    normalized = [normalize_text(word.text) for word in tokens]
    if (not set(normalized) & _KINSHIP_ROLES or not set(normalized) & _KINSHIP_LINKS
            or not set(word_tokens(" ".join(sources))) & _KINSHIP_ROLES):
        return False
    if any(token in _KINSHIP_ATTACHMENTS or any(
        token == stem or (len(stem) >= 4 and token.startswith(stem))
        for stem in EVENT_STEMS
    ) for token in normalized):
        return False
    proper_spans = {(word.start, word.end) for word in proper_words(plain)}
    observed_names = {normalize_text(word.text) for source in sources for word in proper_words(source)}
    named = False
    for word, token in zip(tokens, normalized, strict=True):
        if token in _KINSHIP_FUNCTION or token in _KINSHIP_ROLES or token in _KINSHIP_ORDER:
            continue
        if (word.start, word.end) not in proper_spans or token not in observed_names:
            return False
        named = True
    if not named:
        return False
    remainder = plain
    for word in reversed(tokens):
        remainder = remainder[:word.start] + remainder[word.end:]
    return all(char.isspace() or char in ".,:;()'-’‐–—" for char in remainder)


def _speech(text: str, sources: list[str]) -> bool:
    quotes = list(_QUOTATION.finditer(text))
    if len(quotes) != 1 or _QUOTE_MARKER in text:
        return False
    quote = quotes[0]
    body = _space(next(group for group in quote.groups() if group is not None))
    if not word_tokens(body):
        return False
    # Require an entire supplied quoted utterance, not words cut out of a negated/larger sentence.
    # The small eligibility exception need not handle every legitimate partial-quotation style.
    if not any(body == _space(next(group for group in match.groups() if group is not None))
               for source in sources for match in _QUOTATION.finditer(source)):
        return False
    available = set(word_tokens(" ".join(sources)))
    frame = _space(text[:quote.start()] + _QUOTE_MARKER + text[quote.end():]).rstrip(" .!")
    # At most one explicitly parsed repetition may follow. Everything else must be speech grammar;
    # an actual quotation never licenses an extra murder/action/cause/time assertion beside it.
    head, marker, tail = frame.partition(_QUOTE_MARKER)
    if tail.strip():
        repetition = _REPETITION.fullmatch(tail.strip(" ,;"))
        if not repetition or not _subject(repetition["speaker"], available):
            # A single postposed speaker attribution is also allowed: “Hello,” Aldric said.
            attributed = _SPEECH_SUFFIX.fullmatch(frame)
            return bool(attributed and _subject(attributed["speaker"], available))
    main = head + marker
    for pattern in (_SPEECH_PREFIX, _CHEER_PREFIX):
        matched = pattern.fullmatch(main)
        if matched and _subject(matched["speaker"], available):
            return True
    return False


def semantic_review_eligible(
    claim_text: str, cited_sources: Iterable[Mapping[str, object]],
) -> bool:
    """Whether this entire lexical-hard unit may reach the mandatory independent semantic judge.

    The caller may test the whole original claim for speech/kinship, or an explicit lexical-hard
    clause for a pure identity. Every hard unit must be eligible; this function never approves
    remaining actions/clauses, citation validity, truth, chronology or speaker/name equivalence.
    """
    if (not isinstance(claim_text, str) or not claim_text.strip() or len(claim_text) > 800
            or not isinstance(cited_sources, Iterable)):
        return False
    sources = []
    for source in cited_sources:
        if not isinstance(source, Mapping):
            return False
        value = source.get("text", source.get("excerpt", ""))
        if isinstance(value, str) and value.strip():
            sources.append(unicodedata.normalize("NFC", value))
    if not sources:
        return False
    text = _space(claim_text)
    if _kinship(text, sources):
        return True
    available = set(word_tokens(" ".join(sources)))
    if any(normalize_text(word.text) not in available for word in proper_words(claim_text)):
        return False
    return _identity(text) or _speech(text, sources)
