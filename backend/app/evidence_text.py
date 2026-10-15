"""Exact display slices shared by extraction and answer evidence reviews.

These algorithms preserve source text. Their segments are references, not semantic assertions.
"""
import re


_SOURCE_SENTENCE_END = re.compile(r"(?<=[.!?。！？])(?=\s)|(?<=\n)")
_FIELD_CLAUSE_END = re.compile(r"[.!?;,:。！？；，：](?=\s|$)|\n+")


def field_segments(text: str, kind: str) -> list[dict]:
    """Reference original slices instead of asking a model to copy punctuation faithfully.

    These are mechanical display segments, not a claim that syntax alone proves atomic meaning.
    Every segment is judged with the complete original field and structured context still present.
    """
    if kind in {"identity", "participants"}:
        pieces = [text]
    else:
        pieces, start = [], 0
        for boundary in _FIELD_CLAUSE_END.finditer(text):
            end = boundary.end()
            piece = text[start:end]
            if piece.strip():
                pieces.append(piece)
                start = end
        if start < len(text):
            tail = text[start:]
            if pieces and not tail.strip():
                pieces[-1] += tail
            else:
                pieces.append(tail)
    return [{"id": index, "text": piece} for index, piece in enumerate(pieces)]


def source_passages(text: str) -> list[dict]:
    """Number exact current-source slices, preserving every byte without duplicating the source.

    Sentence boundaries make evidence inspectable. Short sentences are grouped for long parts so
    numbering cannot turn dense punctuation into unbounded prompt overhead. Long sentences receive
    bounded slices, preferably at whitespace; no source text is dropped or brought in from elsewhere.
    """
    target = max(1, (len(text) + 127) // 128)
    max_slice = max(512, target)
    pieces, pending = [], ""
    for sentence in _SOURCE_SENTENCE_END.split(text):
        while len(sentence) > max_slice:
            cut = sentence.rfind(" ", 0, max_slice + 1)
            if cut < max_slice // 2:
                cut = max_slice
            pending += sentence[:cut]
            pieces.append(pending)
            pending, sentence = "", sentence[cut:]
        pending += sentence
        if len(pending) >= target and pending.strip():
            pieces.append(pending)
            pending = ""
    if pending:
        if pieces and not pending.strip():
            pieces[-1] += pending
        else:
            pieces.append(pending)
    return [{"id": index, "text": piece} for index, piece in enumerate(pieces, 1)]
