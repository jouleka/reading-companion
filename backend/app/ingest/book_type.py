"""LIT-9 deterministic, advisory book-type detection over already-segmented chapter text.

Detection runs after the bounded EPUB parser has produced immutable atoms.  It never edits, drops, or
reorders those atoms and its evidence contains only named aggregate signal codes—not headings or prose.
When strong signals conflict or are absent, ``unknown`` is the supported answer.
"""
from __future__ import annotations

import re

from app.book_types import BookProfile


DETECTOR_VERSION = "lit9-structural-v1"

_DRAMA_TITLE = re.compile(r"\b(?:act|scene)\s+(?:[ivxlcdm]+|\d+)\b", re.I)
_VERSE_TITLE = re.compile(r"\b(?:sonnet|ode|canto|poem|verse|stanza)\b", re.I)
_COLLECTION_TITLE = re.compile(r"\b(?:story|stories|tale|tales|adventure|adventures|essay|essays)\b", re.I)
_REFERENCE_TITLE = re.compile(
    r"\b(?:lesson|exercise|glossary|reference|definition|procedure|tutorial|manual|index|appendix)\b",
    re.I,
)
_NONFICTION_TITLE = re.compile(r"\b(?:lecture|memoir|history|biography|treatise|meditation)\b", re.I)
_SPEAKER_CUE = re.compile(r"\b[A-Z][A-Z' -]{2,24}:\s")
_STAGE_CUE = re.compile(r"\b(?:enter|exit|exeunt)\b|\[(?:scene|stage|aside)\b", re.I)
_NARRATIVE_VERB = re.compile(
    r"\b(?:said|asked|answered|replied|arrived|left|went|came|met|remembered|thought|felt|"
    r"saw|heard|told|summoned|spoke|waited|returned|walked|stood|sat)\b",
    re.I,
)


def _ratio(matches: int, count: int) -> float:
    return matches / max(count, 1)


def detect_book_type(result, chapters) -> BookProfile:
    """Classify from bounded structure/content signals without provider calls.

    The returned confidence is a detector-strength estimate, not permission to discard content.  A
    margin is required between the top two candidates; otherwise the result deliberately stays
    ``unknown``.
    """
    chapters = tuple(chapters)
    count = len(chapters)
    titles = [str(chapter.get("title") or "") for chapter in chapters]
    texts = [str(chapter.get("text") or "") for chapter in chapters]
    word_counts = [len(text.split()) for text in texts] or [0]

    drama_titles = sum(bool(_DRAMA_TITLE.search(title)) for title in titles)
    verse_titles = sum(bool(_VERSE_TITLE.search(title)) for title in titles)
    collection_titles = sum(bool(_COLLECTION_TITLE.search(title)) for title in titles)
    reference_titles = sum(bool(_REFERENCE_TITLE.search(title)) for title in titles)
    nonfiction_titles = sum(bool(_NONFICTION_TITLE.search(title)) for title in titles)
    chapter_titles = sum(bool(re.search(r"\bchapter\s+(?:[ivxlcdm]+|\d+)\b", title, re.I))
                         for title in titles)
    speaker_cues = sum(len(_SPEAKER_CUE.findall(text)) for text in texts)
    stage_cues = sum(len(_STAGE_CUE.findall(text)) for text in texts)
    narrative_verbs = sum(len(_NARRATIVE_VERB.findall(text)) for text in texts)
    short_ratio = _ratio(sum(words_ <= 300 for words_ in word_counts), count)
    long_ratio = _ratio(sum(words_ >= 500 for words_ in word_counts), count)

    scores: dict[str, float] = {
        "drama": min(0.99, 0.35 + 0.65 * _ratio(drama_titles, count)
                     + min(0.2, (speaker_cues + stage_cues) / max(count * 10, 1))),
        "poetry": min(0.98, 0.3 + 0.65 * _ratio(verse_titles, count)
                      + (0.1 if short_ratio >= 0.75 else 0.0)),
        "anthology": min(0.96, 0.25 + 0.7 * _ratio(collection_titles, count)),
        "reference": min(0.98, 0.3 + 0.68 * _ratio(reference_titles, count)
                         + (0.05 if short_ratio >= 0.5 else 0.0)),
        "nonfiction": min(0.9, 0.25 + 0.65 * _ratio(nonfiction_titles, count)),
        "novel": 0.0,
    }
    if long_ratio >= 0.6 and narrative_verbs >= count * 2:
        scores["novel"] = 0.82 + min(0.12, narrative_verbs / max(count * 100, 1))
    elif _ratio(chapter_titles, count) >= 0.75 and narrative_verbs >= count * 2:
        scores["novel"] = 0.76
    # Long prose is expected inside a story/essay collection; it must not make the collection's
    # strong repeated title structure look ambiguous merely because its individual entries narrate.
    if _ratio(collection_titles, count) >= 0.75:
        scores["novel"] = min(scores["novel"], 0.5)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    winner, top = ordered[0]
    second = ordered[1][1]
    conflicting = top - second < 0.15
    weak = top < 0.65
    if weak or conflicting or count == 0:
        winner = "unknown"
        confidence = max(0.0, min(0.64, top if count else 0.0))
    else:
        confidence = top

    signals = []
    for present, name in (
        (drama_titles > 0, "act_scene_titles"),
        (speaker_cues > 0, "speaker_cues"),
        (stage_cues > 0, "stage_directions"),
        (verse_titles > 0, "verse_titles"),
        (collection_titles > 0, "collection_titles"),
        (reference_titles > 0, "reference_titles"),
        (nonfiction_titles > 0, "nonfiction_titles"),
        (long_ratio >= 0.6, "long_prose_sections"),
        (short_ratio >= 0.75, "short_sections"),
        (narrative_verbs >= count * 2 and count > 0, "narrative_verbs"),
        (getattr(result, "mode", None) == "anchor-driven", "anchor_driven_structure"),
        (bool(getattr(result, "flags", ())), "segmentation_advisories"),
    ):
        if present:
            signals.append(name)
    if weak:
        signals.append("weak_signals")
    if conflicting:
        signals.append("conflicting_signals")
    return BookProfile(winner, round(confidence, 3), DETECTOR_VERSION, tuple(signals[:12]))
