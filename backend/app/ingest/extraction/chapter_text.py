"""LIT-6 — clean per-ATOM reading text over the PRODUCTIONIZED segmenter (app.ingest.segmentation),
re-ported from ``spikes/lit-6-extraction/chapter_text.py`` (ADR 0007 D-A1 (b)).

Two named, behaviour-relevant changes vs the spike:
  * it runs over ``segment_epub`` (the productionized LIT-4 + D-A8 divider-merge), NOT a sibling
    ``sys.path`` import of the old stdlib spike segmenter — so the atoms here are EXACTLY the
    ``revealed_at`` units the store/frontier use, dividers ALREADY merged (the merged atom's text
    ABSORBS the divider's span, instead of the spike's skip-the-divider approximation);
  * it reuses the segmenter's HARDENED lxml parse path (``epub_segmenter._read`` capped decompression +
    ``_parse`` with entity/network/DTD off + huge_tree cap), so an unparseable / content-less atom
    yields NO text -> no facts (the cross-module invariant the segmenter review pinned: a no-text atom
    is content-less, never a leak).

Spoiler-safety note for anchor-driven books (many atoms per spine file): the file is split at its
atoms' fragment anchors in DOCUMENT ORDER — each text piece is assigned to the atom whose anchor most
recently PRECEDES it (a frag='' HEAD atom gets the file head, up to the first ``#frag``). This closes the
pass-1 whole-file leak (a frag='' head absorbing the whole file) and FAILS CLOSED on an absent or
duplicated boundary anchor (``_anchor_texts`` — those gross failures would otherwise leak a later
chapter's prose into an earlier atom). It is EXACT when every chapter's anchor precedes that chapter's
content — the corpus case (Karamazov is file-driven; P&P anchors each chapter at its ``<h2>`` heading
with no preceding content). **RESIDUAL, routed to LIT-13 (owner-decided: ship file-driven):** if a book
places a chapter's content BEFORE its anchor (e.g. an epigraph above the ``<h2 id>`` heading), that
content is attributed to the PRECEDING atom — the SAME anchor-position-vs-char-split imprecision the
segmenter already flags as an "even-split approximation refined by LIT-13 CFI ranges". The MVP ships
file-driven books (exact); anchor-book fragment precision is best-effort until LIT-13 supplies exact CFI
ranges. Handles wrapped ``<section id>`` and flat ``<a id>`` anchors; an unparseable file -> content-less
(safe-empty), never the whole file.

Also applies the text-layer trim ADR 0001 routed here: strip the Project Gutenberg START header and the
END/license tail so a chapter's text is the prose only (handles the segmenter's trailing-license flag).
"""
import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import replace

from lxml import etree
from lxml import html as lxml_html

from app.ingest.segmentation import segment_epub
from app.ingest.segmentation.epub_segmenter import _ln, _parse, _read

# Match only the marker line/position; do not put a greedy ``.*`` in the pattern.  The previous
# ``.*START ...`` DOTALL expression had catastrophic no-match behaviour on Gutenberg EPUB3 chapter
# files that no longer contain a START marker per split document, making import appear to hang in
# ``_clean``.  We trim by match indexes below instead.
START_RE = re.compile(r"START OF (?:THE|THIS) PROJECT GUTENBERG[^\n]*\n", re.I)
END_RE = re.compile(r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG", re.I)


def content_hash_of(text):
    """The append-once content fingerprint (ADR 0007 D-A3). Computed here so chapter_text and the
    pipeline cannot drift on what 'unchanged content' means."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _body_el(root):
    return next((el for el in root.iter() if _ln(el.tag) == "body"), root)


def _body_text(zf, path):
    """Plain text of the <body> only (drops the <head><title> PG/book-title boilerplate). Hardened XML
    parse first (the SAME path the segmenter classifies on), then a lenient HTML fallback so imperfect
    markup is not silently empty; a genuinely unparseable doc -> ''."""
    raw = _read(zf, path)
    root = _parse(raw)
    if root is not None:
        whole = " ".join(_body_el(root).itertext())
        if whole.strip():
            return whole
    try:
        hroot = lxml_html.fromstring(raw) if raw else None
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        hroot = None
    if hroot is not None:
        return " ".join(_body_el(hroot).itertext())
    return ""


def _node_pieces(el, active, boundaries, out):
    """Document-order text walk tagging each text piece with the most-recent anchor boundary, so a
    multi-anchor spine file splits cleanly: a piece belongs to atom `frag` iff the nearest PRECEDING
    boundary anchor (an element whose @id/@name is in `boundaries`) is `frag` ('' = the file head, before
    any boundary). Returns the active frag at the end of `el` so a flat <a id> anchor propagates to its
    following siblings. (Each chapter's prose lands in its own atom WHEN that chapter's anchor precedes
    its content — the corpus case; a chapter whose content precedes its anchor is the LIT-13-routed
    residual documented in the module docstring, not covered here.)"""
    fid = el.get("id") or el.get("name")
    cur = fid if fid in boundaries else active
    if el.text:
        out.append((cur, el.text))
    for child in el:
        cur = _node_pieces(child, cur, boundaries, out)
        if child.tail:
            out.append((cur, child.tail))
    return cur


def _bucket_by_anchor(root, boundaries):
    pieces = []
    _node_pieces(_body_el(root), "", boundaries, pieces)
    buckets = {}
    for frag, txt in pieces:
        if txt and txt.strip():
            buckets.setdefault(frag, []).append(txt)
    return {f: " ".join(ts) for f, ts in buckets.items()}


def _boundary_counts(root, boundaries):
    """How many DOM elements carry each boundary id (same @id/@name rule as _node_pieces). Used to detect
    an ABSENT or DUPLICATED boundary anchor — either makes the document-order split UNSOUND (a later
    chapter's prose would bucket into an earlier atom = a within-file forward leak)."""
    counts = Counter()
    for el in root.iter():
        fid = el.get("id") or el.get("name")
        if fid in boundaries:
            counts[fid] += 1
    return counts


def _anchor_texts(zf, atoms):
    """Per-atom text for anchor-driven books: each spine file is split at its atoms' fragment anchors so
    a frag='' HEAD atom gets ONLY the file head (up to the first #frag) — never the whole file. Returns
    ``(texts, flags)``. If a boundary frag is ABSENT from or DUPLICATED in its spine file (or the file is
    unparseable), the document-order split cannot be trusted, so that file's atoms are FAILED CLOSED (all
    safe-emptied) and a gating flag is emitted — the ingestion worker must NOT ingest until resolved
    (exactly like a COVERAGE GAP). Real, well-formed anchor books (e.g. P&P) resolve cleanly and are
    unaffected; only a typo'd / malformed EPUB trips the guard."""
    out, flags, by_href = {}, [], {}
    for a in atoms:
        by_href.setdefault(a.href, []).append(a)
    for href, group in by_href.items():
        boundaries = {a.frag for a in group if a.frag}
        raw = _read(zf, href)
        root = _parse(raw)
        if root is None:                                    # hardened-XML failed -> lenient HTML, same walk
            try:
                root = lxml_html.fromstring(raw) if raw else None
            except (etree.ParserError, etree.XMLSyntaxError, ValueError):
                root = None
        counts = _boundary_counts(root, boundaries) if root is not None else Counter()
        missing = sorted(boundaries - set(counts))
        dup = sorted(f for f, n in counts.items() if n > 1)
        if root is None or missing or dup:                  # unsound split -> fail closed for THIS file
            for a in group:
                out[a.chapter_key] = ""
            flags.append(
                f"ANCHOR RESOLUTION FAILURE in {href!r}: fragment anchor(s) {missing or '[]'} absent, "
                f"{dup or '[]'} duplicated (or file unparseable) — atoms safe-emptied to avoid a "
                f"within-file forward leak; do NOT ingest this book's text until resolved")
            continue
        buckets = _bucket_by_anchor(root, boundaries)
        for a in group:
            out[a.chapter_key] = buckets.get(a.frag, "")
    return out, flags


def _atom_text(zf, atom):
    """File-driven atom text: the <body> prose of every covered spine file (absorbed dividers + body)."""
    parts = [_body_text(zf, href) for href in atom.source_files]
    return "\n\n".join(p for p in parts if p)


def _clean(text):
    start = START_RE.search(text)
    if start:
        text = text[start.end():]                  # drop up to & incl. the PG START line
    end = END_RE.search(text)
    if end:
        text = text[:end.start()]                  # drop the license tail
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def chapter_texts(epub, book_id, limit=None):
    """Segment an EPUB (path/bytes) then attach cleaned reading text + content_hash per POST-MERGE atom.

    Returns ``[{ordinal, key, href, title, part_label, text, words, content_hash, source_files}]`` in
    ``revealed_at`` order. ``limit`` caps the count (for cheap smokes). NB: the SegmentResult's flags
    (esp. a COVERAGE GAP) are the ingestion worker's gate — see ``segment_for_ingest``."""
    _result, chapters = segment_for_ingest(epub, book_id)
    return chapters if limit is None else chapters[:limit]


def segment_for_ingest(epub, book_id):
    """Segment + attach text, returning BOTH the SegmentResult (so the caller can gate on
    ``result.flags`` — a COVERAGE GAP must block ingestion, ADR 0007 D-A8 routed) and the chapter list."""
    result = segment_epub(epub, book_id)
    src = io.BytesIO(bytes(epub)) if isinstance(epub, (bytes, bytearray)) else str(epub)
    chapters = []
    anchor_texts, anchor_flags = None, []
    with zipfile.ZipFile(src) as zf:
        # Anchor-driven books (many atoms per spine file) split each file at its fragment anchors so a
        # frag='' head atom never absorbs later sections (pass-1 BLOCKER) and an absent/duplicated anchor
        # fails closed (pass-3 BLOCKER); file-driven atoms take whole files.
        if result.mode == "anchor-driven":
            anchor_texts, anchor_flags = _anchor_texts(zf, result.atoms)
        for atom in result.atoms:
            raw = anchor_texts[atom.chapter_key] if anchor_texts is not None else _atom_text(zf, atom)
            text = _clean(raw)
            chapters.append({
                "ordinal": atom.revealed_at,
                "key": atom.chapter_key,
                "href": atom.href,
                "title": atom.title,
                "part_label": atom.part_label,
                "text": text,
                "words": len(text.split()),
                "content_hash": content_hash_of(text),
                "source_files": atom.source_files,
            })
    if anchor_flags:                                        # surface anchor-resolution failures as gating flags
        result = replace(result, flags=result.flags + tuple(anchor_flags))
    return result, chapters
