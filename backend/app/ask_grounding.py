"""Narrow routes from an unhelpful lexical rejection to mandatory semantic review.

This module never clears an answer. Pure named identity clauses contain no event vocabulary, while
faithful speech paraphrases can use a verb absent from a quoted source. Those two shapes may reach
the independent cited-evidence judge; all other deterministic gates still apply. Never pass a whole
compound claim merely because one of its clauses is an identity or contains a real quotation.
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


def _space(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


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

    The caller may test the whole original claim for quoted speech, or an explicit lexical-hard
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
    available = set(word_tokens(" ".join(sources)))
    if any(normalize_text(word.text) not in available for word in proper_words(claim_text)):
        return False
    text = _space(claim_text)
    return _identity(text) or _speech(text, sources)
