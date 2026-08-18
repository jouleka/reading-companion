"""Module C / chapter_text.py — clean per-ATOM reading text over the PRODUCTIONIZED segmenter
(app.ingest.segmentation), with content_hash for append-once. Pins the cross-module invariants:
  * a no-text atom (segmenter content-less) yields "" here too -> no facts, never a leak;
  * an anchor-driven per-fragment atom gets ONLY its own section's text — never a LATER fragment's
    (a within-file forward leak), the safe-empty fallback when a fragment subtree is absent;
  * the PG START/END boilerplate is trimmed at the text layer (ADR 0001 routed this to LIT-6).
"""
import io
import time
import zipfile
from pathlib import Path

import pytest

from app.ingest.extraction.chapter_text import (
    _clean,
    chapter_texts,
    content_hash_of,
    segment_for_ingest,
)

KARA = Path(__file__).resolve().parents[4] / "books" / "pg28054.epub"

_CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
              'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
              '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
              '</rootfiles></container>')


def _epub_ncx(docs, *, raw=None):
    """docs = [(filename, label, heading, body)]; EPUB2 + NCX. `raw` overrides a doc's bytes."""
    items, spine, navpoints = [], [], []
    for i, (fn, label, _h, _b) in enumerate(docs):
        items.append(f'<item id="d{i}" href="{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="d{i}"/>')
        navpoints.append(f'<navPoint id="n{i}"><navLabel><text>{label}</text></navLabel>'
                         f'<content src="{fn}"/></navPoint>')
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>Synthetic</dc:title></metadata><manifest>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           f'{"".join(items)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<navMap>{"".join(navpoints)}</navMap></ncx>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        for fn, _label, heading, body in docs:
            if raw and fn in raw:
                z.writestr(fn, raw[fn])
            else:
                z.writestr(fn, f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                               f'<body><h1>{heading}</h1><p>{body}</p></body></html>')
    return buf.getvalue()


def _epub_anchors(sections):
    blocks = "".join(f'<section id="{fid}"><h2>{lab}</h2><p>{txt}</p></section>'
                     for fid, lab, txt in sections)
    doc = f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{blocks}</body></html>'
    navpoints = "".join(f'<navPoint id="n{i}"><navLabel><text>{lab}</text></navLabel>'
                        f'<content src="book.xhtml#{fid}"/></navPoint>'
                        for i, (fid, lab, _t) in enumerate(sections))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>Synthetic</dc:title></metadata><manifest>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           '<item id="d0" href="book.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine toc="ncx"><itemref idref="d0"/></spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<navMap>{navpoints}</navMap></ncx>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("book.xhtml", doc)
    return buf.getvalue()


def _epub_anchors_bare_first(sections):
    """Like _epub_anchors but the FIRST ToC leaf references the BARE file (no #frag) while the rest use
    #frag — the pass-1 BLOCKER shape (a frag='' head atom alongside #frag atoms on one spine file)."""
    blocks = "".join(f'<section id="{fid}"><h2>{lab}</h2><p>{txt}</p></section>'
                     for fid, lab, txt in sections)
    doc = f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{blocks}</body></html>'
    nav = []
    for i, (fid, lab, _t) in enumerate(sections):
        src = "book.xhtml" if i == 0 else f"book.xhtml#{fid}"     # FIRST leaf is the bare file
        nav.append(f'<navPoint id="n{i}"><navLabel><text>{lab}</text></navLabel>'
                   f'<content src="{src}"/></navPoint>')
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>Synthetic</dc:title></metadata><manifest>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           '<item id="d0" href="book.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine toc="ncx"><itemref idref="d0"/></spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<navMap>{"".join(nav)}</navMap></ncx>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("book.xhtml", doc)
    return buf.getvalue()


def _epub_anchor_raw_body(body_html, toc_frags):
    """Full control of the spine file's <body> HTML + which #frags the NCX references, to build the
    unresolved/duplicate-anchor shapes (pass-3 BLOCKER): a ToC frag that the DOM lacks or carries twice."""
    doc = f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{body_html}</body></html>'
    nav = "".join(f'<navPoint id="n{i}"><navLabel><text>{frag}</text></navLabel>'
                  f'<content src="book.xhtml#{frag}"/></navPoint>' for i, frag in enumerate(toc_frags))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>Synthetic</dc:title></metadata><manifest>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           '<item id="d0" href="book.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine toc="ncx"><itemref idref="d0"/></spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<navMap>{nav}</navMap></ncx>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("book.xhtml", doc)
    return buf.getvalue()


_PROSE = " ".join(["lorem"] * 300)


def _sec(sid, txt):
    return f'<section id="{sid}"><h2>{sid}</h2><p>{txt}</p></section>'


def test_anchor_unresolved_boundary_fails_closed_no_leak():
    # pass-3 BLOCKER: a ToC frag 'c2' that does NOT resolve to a DOM element makes the document-order
    # split unsound — chapter II's prose would bucket into the EARLIER chapter I atom. Must fail closed.
    body = _sec("c1", "SECRET_ALPHA " + _PROSE) + _sec("x2", "SECRET_BETA " + _PROSE) \
        + _sec("c3", "SECRET_GAMMA " + _PROSE)                       # 'c2' is declared in ToC but DOM has 'x2'
    result, chs = segment_for_ingest(_epub_anchor_raw_body(body, ["c1", "c2", "c3"]), "b")
    assert result.mode == "anchor-driven"
    assert all(c["text"] == "" for c in chs)                        # fail closed: every atom safe-emptied
    assert any("ANCHOR RESOLUTION FAILURE" in f for f in result.flags)
    head = next(c for c in chs if c["key"].endswith("#c1"))
    assert "SECRET_BETA" not in head["text"] and "SECRET_GAMMA" not in head["text"]   # NO forward leak


def test_anchor_duplicate_boundary_fails_closed():
    # pass-3 BLOCKER (2nd shape): a frag id appearing TWICE in the DOM re-opens its bucket, so a later
    # chapter's trailing prose lands in an earlier atom. A duplicated boundary must fail closed too.
    body = _sec("c1", "SECRET_ALPHA " + _PROSE) + _sec("c2", "SECRET_BETA " + _PROSE) \
        + '<span id="c2">stray</span>' + _sec("c3", "SECRET_GAMMA " + _PROSE)
    result, chs = segment_for_ingest(_epub_anchor_raw_body(body, ["c1", "c2", "c3"]), "b")
    assert all(c["text"] == "" for c in chs)
    assert any("ANCHOR RESOLUTION FAILURE" in f for f in result.flags)


def test_clean_anchor_book_is_not_failed_closed():
    # the happy path must be UNAFFECTED: when every frag resolves to exactly one element, atoms get their
    # own text and NO anchor-resolution flag fires (mirrors the real, clean P&P book).
    body = _sec("c1", "SECRET_ALPHA " + _PROSE) + _sec("c2", "SECRET_BETA " + _PROSE) \
        + _sec("c3", "SECRET_GAMMA " + _PROSE)
    result, chs = segment_for_ingest(_epub_anchor_raw_body(body, ["c1", "c2", "c3"]), "b")
    assert not any("ANCHOR RESOLUTION FAILURE" in f for f in result.flags)
    assert "SECRET_ALPHA" in chs[0]["text"] and "SECRET_BETA" not in chs[0]["text"]
    assert "SECRET_BETA" in chs[1]["text"] and "SECRET_GAMMA" in chs[2]["text"]


def test_anchor_content_before_its_heading_anchor_is_a_documented_lit13_residual():
    # DOCUMENTED residual (owner decision: route to LIT-13, ship file-driven). When a chapter's content
    # sits BEFORE its heading-anchor (an epigraph above <h2 id>), the document-order split attributes it
    # to the PRECEDING atom — the anchor-position-vs-char-split imprecision the segmenter already flags
    # as an even-split approximation; exact CFI ranges are LIT-13. The corpus does NOT hit this (Karamazov
    # is file-driven; P&P anchors at the heading with no preceding content). Pinned so the behaviour is
    # VISIBLE, not silent — this is NOT a spoiler-safety claim for this layout. Update when LIT-13 lands.
    body = ('<h2 id="c1">Chapter One</h2><p>ALPHA body of one</p>'
            '<blockquote>BETA epigraph of chapter two</blockquote>'   # ch-2 content BEFORE its anchor
            '<h2 id="c2">Chapter Two</h2><p>ch two body</p>'
            '<h2 id="c3">Chapter Three</h2><p>GAMMA body</p>')
    result, chs = segment_for_ingest(_epub_anchor_raw_body(body, ["c1", "c2", "c3"]), "b")
    # every anchor resolves uniquely -> the absent/duplicate guard correctly does NOT fire here
    assert not any("ANCHOR RESOLUTION FAILURE" in f for f in result.flags)
    c1 = next(c for c in chs if c["key"].endswith("#c1"))
    assert "BETA epigraph" in c1["text"]                             # the documented LIT-13 residual


def test_file_driven_yields_one_chapter_text_per_atom():
    epub = _epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", "Alyosha " + _PROSE),
                      ("c2.xhtml", "Chapter II", "Chapter II", "Dmitri " + _PROSE)])
    chs = chapter_texts(epub, "b")
    assert [c["ordinal"] for c in chs] == [1, 2]
    assert [c["key"] for c in chs] == ["b:c1.xhtml", "b:c2.xhtml"]
    assert "Alyosha" in chs[0]["text"] and "Dmitri" in chs[1]["text"]
    assert "Dmitri" not in chs[0]["text"]                   # chapter 1 does not contain chapter 2's text
    assert all(c["words"] > 0 and c["content_hash"] for c in chs)


def test_content_hash_is_deterministic_and_text_sensitive():
    assert content_hash_of("hello world") == content_hash_of("hello world")
    assert content_hash_of("hello world") != content_hash_of("hello worlds")
    epub_a = _epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", "alpha " + _PROSE),
                        ("c2.xhtml", "Chapter II", "Chapter II", _PROSE)])
    epub_b = _epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", "beta " + _PROSE),
                        ("c2.xhtml", "Chapter II", "Chapter II", _PROSE)])
    h_a = chapter_texts(epub_a, "b")[0]["content_hash"]
    h_b = chapter_texts(epub_b, "b")[0]["content_hash"]
    assert h_a != h_b                                       # different prose -> different append-once key


def test_merged_divider_atom_absorbs_the_divider_text():
    epub = _epub_ncx([("part1.xhtml", "PART I", "PART I", ""),
                      ("c1.xhtml", "Chapter I", "Chapter I", "Alyosha " + _PROSE)])
    chs = chapter_texts(epub, "b")
    assert len(chs) == 1
    assert chs[0]["part_label"] == "PART I"
    assert "PART I" in chs[0]["text"] and "Alyosha" in chs[0]["text"]
    assert set(chs[0]["source_files"]) == {"part1.xhtml", "c1.xhtml"}


def test_content_less_atom_yields_empty_text_no_facts():
    # an image-only LABELED chapter is kept as a content-less atom by the segmenter; its text must be ""
    # here too (same hardened parse path) so it yields no facts — never a leak.
    svg = (b"<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'><body>"
           b"<img src='ch2.png'/></body></html>")
    epub = _epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _PROSE),
                      ("img.xhtml", "Chapter II", "", ""),
                      ("c3.xhtml", "Chapter III", "Chapter III", _PROSE)],
                     raw={"img.xhtml": svg})
    chs = chapter_texts(epub, "b")
    by_href = {c["href"]: c for c in chs}
    assert by_href["img.xhtml"]["text"] == ""               # content-less, no facts
    assert by_href["img.xhtml"]["words"] == 0


def test_anchor_fragment_text_is_isolated_no_within_file_forward_leak():
    # THE anchor-mode spoiler property: a per-fragment atom gets ONLY its section's prose, never a LATER
    # fragment's text from the same file (which would stamp future content at this atom's revealed_at).
    epub = _epub_anchors([("s1", "Chapter I", "SECRET_ALPHA prose " + _PROSE),
                          ("s2", "Chapter II", "SECRET_BETA prose " + _PROSE),
                          ("s3", "Chapter III", "SECRET_GAMMA prose " + _PROSE)])
    chs = chapter_texts(epub, "b")
    assert len(chs) == 3
    assert "SECRET_ALPHA" in chs[0]["text"]
    assert "SECRET_BETA" not in chs[0]["text"] and "SECRET_GAMMA" not in chs[0]["text"]
    assert "SECRET_BETA" in chs[1]["text"] and "SECRET_ALPHA" not in chs[1]["text"]


def test_anchor_bare_file_head_atom_does_not_leak_later_sections():
    # pass-1 BLOCKER: when the first ToC leaf is the BARE file (frag='') and later leaves use #frag, the
    # head atom must get ONLY its own (first) section's prose — never the whole file (later sections).
    epub = _epub_anchors_bare_first([("s1", "Chapter I", "SECRET_ALPHA " + _PROSE),
                                     ("s2", "Chapter II", "SECRET_BETA " + _PROSE),
                                     ("s3", "Chapter III", "SECRET_GAMMA " + _PROSE)])
    chs = chapter_texts(epub, "b")
    assert len(chs) == 3
    head = chs[0]                                            # the bare-file head atom (frag='')
    assert "#" not in head["key"]                           # confirm it is the frag='' atom
    assert "SECRET_ALPHA" in head["text"]
    assert "SECRET_BETA" not in head["text"] and "SECRET_GAMMA" not in head["text"]   # NO forward leak
    assert "SECRET_BETA" in chs[1]["text"] and "SECRET_ALPHA" not in chs[1]["text"]


def test_pg_license_tail_is_trimmed_at_the_text_layer():
    tail = "*** END OF THE PROJECT GUTENBERG EBOOK SYNTHETIC *** license blah blah do not redistribute"
    epub = _epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", "Real prose here. " + _PROSE + " " + tail),
                      ("c2.xhtml", "Chapter II", "Chapter II", _PROSE)])
    chs = chapter_texts(epub, "b")
    assert "Real prose here." in chs[0]["text"]
    assert "END OF THE PROJECT GUTENBERG" not in chs[0]["text"]   # license tail stripped


def test_pg_markers_keep_only_the_reading_text_between_them():
    text = (
        "Project Gutenberg preamble\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK SYNTHETIC ***\n"
        "Chapter prose survives.\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK SYNTHETIC ***\n"
        "License tail"
    )
    assert _clean(text) == "Chapter prose survives."


def test_pg_marker_cleaning_is_linear_when_split_docs_have_no_start_marker():
    # Gutenberg EPUB3 files can be split so most chapter docs have NO START marker. A previous
    # DOTALL ``.*START ...`` regex in _clean was catastrophically slow on that no-match path and made
    # real Karamazov import look hung. This pins the no-marker path to a small, deterministic budget.
    text = ("Karamazov prose without a boilerplate start marker. " * 30_000)
    t0 = time.perf_counter()
    out = _clean(text)
    elapsed = time.perf_counter() - t0
    assert out.startswith("Karamazov prose")
    assert elapsed < 5.0


def test_limit_caps_the_number_of_chapters():
    epub = _epub_ncx([(f"c{i}.xhtml", f"Chapter {i}", f"Chapter {i}", _PROSE) for i in range(1, 6)])
    chs = chapter_texts(epub, "b", limit=2)
    assert [c["ordinal"] for c in chs] == [1, 2]


@pytest.mark.skipif(not KARA.exists(), reason="Karamazov EPUB not present")
def test_karamazov_real_text_extraction():
    chs = chapter_texts(str(KARA), "kara")
    assert len(chs) == 96
    assert [c["ordinal"] for c in chs] == list(range(1, 97))
    assert chs[0]["part_label"] == "PART I"
    assert all(c["content_hash"] for c in chs)
    # chapter 1 (the merged PART I + Book I ch I) is substantial real prose
    assert chs[0]["words"] > 200
    # no body chapter carries the PG license/header boilerplate after trimming
    assert all("START OF THE PROJECT GUTENBERG" not in c["text"] for c in chs)
    assert all("END OF THE PROJECT GUTENBERG" not in c["text"] for c in chs)
