"""LIT-4 segmentation data model — the chapter ATOM (the unit `revealed_at` is stamped on) and the
whole-book result. Frozen dataclasses: an atom is an immutable fact about the book's structure."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterAtom:
    """One included, POST-MERGE chapter atom (ADR 0001 + ADR 0007 D-A8).

    `revealed_at` is the 1-based ordinal among included atoms (the bitemporal filter key). `chapter_key`
    is content-identity, book_id-prefixed (`{book_id}:{href}` file-driven, `…#{frag}` anchor-driven) —
    NOT positional, so a classification change doesn't renumber every key. A merged atom takes the
    FOLLOWING body chapter's key/href/title and captures the absorbed divider label(s) in `part_label`;
    `source_files` are the spine hrefs it covers (absorbed dividers first, then the body chapter) so the
    coverage assertion can treat a merged divider as covered-by-its-successor. `char_len` (>0) is the
    atom's text length INCLUDING any absorbed divider span (it absorbs the divider's start anchor) — a
    provisional monotonic length the frontier consumes until LIT-13 supplies real CFI ranges."""
    revealed_at: int
    chapter_key: str
    href: str
    frag: str
    title: str
    part_label: str
    char_len: int
    source_files: tuple


@dataclass(frozen=True)
class SegmentResult:
    book_id: str
    mode: str                       # 'file-driven' | 'anchor-driven' | 'none' (no body detected)
    atoms: tuple                    # tuple[ChapterAtom, ...] in revealed_at order
    front: tuple                    # basenames stripped as front-matter
    back: tuple                     # basenames stripped as back-matter
    flags: tuple                    # non-fatal advisories (coverage, trailing divider, approximations)
    content_language: str = "und"  # normalized EPUB dc:language; und when absent/malformed
    title: str | None = None        # bounded OPF dc:title; presentation metadata, never a fact gate
    author: str | None = None       # bounded first OPF dc:creator; presentation metadata only
