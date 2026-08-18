"""LIT-4 segmentation + ADR-0007 D-A8 divider-merge, productionized from spikes/lit-4-segmentation.
Synthetic EPUB2/NCX fixtures give deterministic merge/edge/hardening coverage; the real Karamazov
(books/pg28054.epub) is the acceptance: its atoms must pass LIT-12's frontier assert_aligned.
"""
import io
import zipfile
from pathlib import Path

import pytest

from app.ingest.segmentation import ChapterAtom, segment_epub
from app.ingest.segmentation import signals as S
from app.ingest.segmentation.epub_segmenter import EpubSafetyError
from app.reader import frontier

KARA = Path(__file__).resolve().parents[4] / "books" / "pg28054.epub"


def _build_epub_ncx(docs, *, version="2.0", raw=None, language=None):
    """docs = [(filename, ncx_label, heading, body_text)]. `raw` overrides a doc's full bytes
    (filename -> bytes) for malformed/adversarial cases. Returns EPUB zip bytes (EPUB2 + NCX)."""
    items, spine, navpoints = [], [], []
    for i, (fn, label, _heading, _body) in enumerate(docs):
        items.append(f'<item id="d{i}" href="{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="d{i}"/>')
        navpoints.append(f'<navPoint id="n{i}"><navLabel><text>{label}</text></navLabel>'
                         f'<content src="{fn}"/></navPoint>')
    opf = (f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="{version}" '
           f'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>Synthetic</dc:title>{f"<dc:language>{language}</dc:language>" if language else ""}'
           f'</metadata><manifest>'
           f'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           f'{"".join(items)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<navMap>{"".join(navpoints)}</navMap></ncx>')
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        for fn, _label, heading, body in docs:
            if raw and fn in raw:
                z.writestr(fn, raw[fn])
            else:
                z.writestr(fn, f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                               f'<body><h1>{heading}</h1><p>{body}</p></body></html>')
    return buf.getvalue()


def test_declared_language_is_normalized_and_legacy_cyrillic_front_matter_is_excluded():
    epub = _build_epub_ncx(
        [
            ("cast.xhtml", "Действующие лица", "Действующие лица", "Имена персонажей"),
            ("part.xhtml", "Часть I", "Часть I", "Часть первая"),
            ("c1.xhtml", "Глава 1", "Глава 1", "Алёша вернулся домой. " * 20),
        ],
        language="RU_ru",
    )
    result = segment_epub(epub, "b")
    assert result.content_language == "ru-ru"
    assert result.front == ("cast.xhtml",)
    assert len(result.atoms) == 1
    assert result.atoms[0].title == "Глава 1"
    assert result.atoms[0].part_label == "Часть I"


def test_missing_or_malformed_language_degrades_to_undetermined():
    docs = [("c1.xhtml", "Chapter 1", "Chapter 1", "Aldric arrived. " * 20)]
    assert segment_epub(_build_epub_ncx(docs), "b").content_language == "und"
    assert segment_epub(_build_epub_ncx(docs, language="not a language!"), "b").content_language == "und"


def _build_epub_anchors(sections):
    """sections = [(frag_id, label, text)] inside ONE body file 'book.xhtml'; NCX anchors point to
    book.xhtml#frag -> exercises the anchor-driven (many chapters per file) mode."""
    blocks = "".join(f'<section id="{fid}"><h2>{lab}</h2><p>{txt}</p></section>'
                     for fid, lab, txt in sections)
    doc = (f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{blocks}'
           f'</body></html>')
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
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("book.xhtml", doc)
    return buf.getvalue()


def _prose(words):
    return " ".join(["lorem"] * words)


# --------------------------------------------------------------- anchor-driven (many chapters/file)

def test_anchor_driven_splits_at_toc_fragments():
    epub = _build_epub_anchors([("s1", "Chapter I", _prose(300)),
                                ("s2", "Chapter II", _prose(300)),
                                ("s3", "Chapter III", _prose(300))])
    res = segment_epub(epub, "b")
    assert res.mode == "anchor-driven"
    assert len(res.atoms) == 3
    assert res.atoms[0].chapter_key == "b:book.xhtml#s1"
    assert res.atoms[0].frag == "s1"
    bounds = frontier.chapter_bounds([a.char_len for a in res.atoms])     # all > 0
    frontier.assert_aligned(bounds, [a.revealed_at for a in res.atoms])   # 1:1 contiguous


def test_anchor_driven_filters_a_front_like_leaf():
    # the body file classifies as body (first heading = a chapter); a front-like leaf polluting the NCX
    # mid-list (e.g. a "List of Illustrations" caption, like the real P&P) is dropped from the atoms.
    epub = _build_epub_anchors([("s1", "Chapter I", _prose(300)),
                                ("illos", "List of Illustrations", _prose(50)),
                                ("s2", "Chapter II", _prose(300))])
    res = segment_epub(epub, "b")
    assert res.mode == "anchor-driven"
    assert [a.title for a in res.atoms] == ["Chapter I", "Chapter II"]    # front-like leaf dropped


# --------------------------------------------------------------- basic segmentation

def test_plain_chapters_segment_one_atom_each():
    epub = _build_epub_ncx([
        ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
        ("c2.xhtml", "Chapter II", "Chapter II", _prose(300)),
        ("c3.xhtml", "Chapter III", "Chapter III", _prose(300)),
    ])
    res = segment_epub(epub, "b")
    assert [a.title for a in res.atoms] == ["Chapter I", "Chapter II", "Chapter III"]
    assert [a.revealed_at for a in res.atoms] == [1, 2, 3]
    assert all(a.part_label == "" for a in res.atoms)
    assert all(a.char_len > 0 for a in res.atoms)
    assert all(isinstance(a, ChapterAtom) for a in res.atoms)


def test_chapter_keys_are_content_identity_book_prefixed():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
                            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300))])
    res = segment_epub(epub, "kara")
    assert res.atoms[0].chapter_key == "kara:c1.xhtml"
    assert res.atoms[1].chapter_key == "kara:c2.xhtml"


def test_chapter_keys_are_stable_when_revealed_at_shifts():
    # the load-bearing property: a classification change that renumbers revealed_at must NOT change a
    # body chapter's key (key = content identity, not positional). Prepend a front doc -> revealed_at
    # shifts by 0 (front is stripped) but keys are byte-stable regardless.
    body = [("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300))]
    base = segment_epub(_build_epub_ncx(body), "b")
    withfront = segment_epub(_build_epub_ncx([("intro.xhtml", "Introduction", "Introduction",
                                               _prose(80))] + body), "b")
    assert [a.chapter_key for a in base.atoms] == [a.chapter_key for a in withfront.atoms]
    assert "intro.xhtml" in withfront.front           # the prepended intro was stripped, not chapter 1


# --------------------------------------------------------------- the divider-merge (D-A8)

def test_part_divider_merges_into_following_chapter():
    epub = _build_epub_ncx([
        ("part1.xhtml", "PART I", "PART I", ""),                 # <200w label-only divider
        ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
        ("c2.xhtml", "Chapter II", "Chapter II", _prose(300)),
    ])
    res = segment_epub(epub, "b")
    assert len(res.atoms) == 2                                   # divider folded, not its own atom
    a0 = res.atoms[0]
    assert a0.part_label == "PART I"                            # Part captured as the grouping attribute
    assert a0.title == "Chapter I"                              # successor's title/key, not the divider's
    assert a0.chapter_key == "b:c1.xhtml"
    assert a0.href == "c1.xhtml"
    assert "part1.xhtml" in a0.source_files and "c1.xhtml" in a0.source_files
    assert [a.revealed_at for a in res.atoms] == [1, 2]         # renumbered contiguous


def test_merged_atom_absorbs_the_divider_span():
    # the merged atom's length = divider chars + successor chars (it absorbs the divider's start anchor)
    epub = _build_epub_ncx([("part1.xhtml", "PART I", "PART I", ""),
                            ("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    div_only = segment_epub(_build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))]), "b")
    merged = segment_epub(epub, "b")
    assert merged.atoms[0].char_len > div_only.atoms[0].char_len   # strictly longer by the divider span


def test_consecutive_dividers_all_fold_into_next_chapter():
    epub = _build_epub_ncx([
        ("part1.xhtml", "PART I", "PART I", ""),
        ("book1.xhtml", "Book One", "Book One", _prose(10)),     # also tiny + divider-labelled
        ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
    ])
    res = segment_epub(epub, "b")
    assert len(res.atoms) == 1
    assert "PART I" in res.atoms[0].part_label and "Book One" in res.atoms[0].part_label
    assert set(res.atoms[0].source_files) == {"part1.xhtml", "book1.xhtml", "c1.xhtml"}


def test_short_titled_chapter_is_NOT_merged():
    # a <200-word doc whose label is a real chapter title (not Part/Book) is a real (short) chapter
    epub = _build_epub_ncx([
        ("c1.xhtml", "Chapter I. A Brief Note", "Chapter I. A Brief Note", _prose(40)),
        ("c2.xhtml", "Chapter II", "Chapter II", _prose(300)),
    ])
    res = segment_epub(epub, "b")
    assert len(res.atoms) == 2
    assert res.atoms[0].part_label == ""


def test_trailing_divider_with_no_successor_is_kept_not_lost():
    epub = _build_epub_ncx([
        ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
        ("partz.xhtml", "PART V", "PART V", ""),                 # divider at the very end, no successor
    ])
    res = segment_epub(epub, "b")
    assert "partz.xhtml" in {f for a in res.atoms for f in a.source_files}   # not silently dropped
    assert any("trailing divider" in f.lower() for f in res.flags)
    assert [a.revealed_at for a in res.atoms] == list(range(1, len(res.atoms) + 1))


# --------------------------------------------------------------- coverage (no silent chapter loss)

def test_every_body_file_is_covered_by_exactly_one_atom():
    epub = _build_epub_ncx([
        ("part1.xhtml", "PART I", "PART I", ""),
        ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
        ("c2.xhtml", "Chapter II", "Chapter II", _prose(300)),
    ])
    res = segment_epub(epub, "b")
    covered = [f for a in res.atoms for f in a.source_files]
    assert sorted(covered) == ["c1.xhtml", "c2.xhtml", "part1.xhtml"]
    assert len(covered) == len(set(covered))                    # no double-cover
    assert not any("COVERAGE GAP" in f for f in res.flags)


# --------------------------------------------------------------- lxml hardening (untrusted EPUBs)

def test_malformed_xhtml_still_yields_a_chapter_not_silent_loss():
    bad = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter I</h1><p>hello ' \
          b'world <b>unclosed bold and a stray & ampersand</p></body></html>'   # imperfect XHTML
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", ""),
                            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300))],
                           raw={"c1.xhtml": bad})
    res = segment_epub(epub, "b")
    assert len(res.atoms) == 2                                   # lxml recover -> chapter not dropped
    assert res.atoms[0].char_len > 0


def test_internal_entity_is_not_expanded_into_text():
    # an EPUB is untrusted input: a declared entity must NOT be expanded (XXE / entity-expansion guard).
    payload = "X" * 5000
    bomb = (f'<?xml version="1.0"?><!DOCTYPE html [<!ENTITY boom "{payload}">]>'
            f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter I</h1>'
            f'<p>&boom;</p></body></html>').encode()
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", ""),
                            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300))],
                           raw={"c1.xhtml": bomb})
    res = segment_epub(epub, "b")
    assert len(res.atoms) == 2
    assert res.atoms[0].char_len < 1000                         # the 5000-char entity was NOT expanded


def _append_member(epub, name, data, *, compression=zipfile.ZIP_STORED):
    buf = io.BytesIO(epub)
    with zipfile.ZipFile(buf, "a", compression=compression) as z:
        z.writestr(name, data)
    return buf.getvalue()


def _rewrite_member(epub, name, transform):
    src, out = io.BytesIO(epub), io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            raw = zin.read(info)
            zout.writestr(info, transform(raw) if info.filename == name else raw)
    return out.getvalue()


def _drop_member(epub, name):
    src, out = io.BytesIO(epub), io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            if info.filename != name:
                zout.writestr(info, zin.read(info))
    return out.getvalue()


def test_high_ratio_zip_bomb_is_rejected_before_segmentation():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    epub = _append_member(epub, "unused.bin", b"0" * (2 * 1024 * 1024),
                          compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(EpubSafetyError, match="compression-ratio"):
        segment_epub(epub, "b")


def test_oversized_central_directory_is_rejected_before_zipfile_allocation():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    buf = io.BytesIO(epub)
    with zipfile.ZipFile(buf, "a") as z:
        for index in range(4096):
            z.writestr(f"unused/{index}.txt", b"")
    with pytest.raises(EpubSafetyError, match="central directory"):
        segment_epub(buf.getvalue(), "b")


def test_unsafe_archive_member_path_is_rejected():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    epub = _append_member(epub, "../outside.xhtml", b"not extracted, but still ambiguous")
    with pytest.raises(EpubSafetyError, match="member path"):
        segment_epub(epub, "b")


def test_duplicate_archive_member_is_rejected():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    with pytest.warns(UserWarning):
        epub = _append_member(epub, "c1.xhtml", b"replacement")
    with pytest.raises(EpubSafetyError, match="duplicate"):
        segment_epub(epub, "b")


def test_zip_encryption_flag_is_rejected_before_member_read():
    epub = bytearray(_build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))]))
    central = epub.find(b"PK\x01\x02")
    assert central >= 0
    flags = int.from_bytes(epub[central + 8:central + 10], "little") | 0x1
    epub[central + 8:central + 10] = flags.to_bytes(2, "little")
    with pytest.raises(EpubSafetyError, match="encrypted ZIP"):
        segment_epub(bytes(epub), "b")


def test_drm_encryption_metadata_is_rejected_explicitly():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    encryption = b'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
      xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>
      <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
      <enc:CipherData><enc:CipherReference URI="c1.xhtml"/></enc:CipherData>
      </enc:EncryptedData></encryption>'''
    epub = _append_member(epub, "META-INF/encryption.xml", encryption)
    with pytest.raises(EpubSafetyError, match="DRM-protected"):
        segment_epub(epub, "b")


def test_standard_font_obfuscation_is_allowed():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    epub = _append_member(epub, "fonts/book.ttf", b"obfuscated-font-placeholder")
    encryption = b'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
      xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>
      <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
      <enc:CipherData><enc:CipherReference URI="fonts/book.ttf"/></enc:CipherData>
      </enc:EncryptedData></encryption>'''
    epub = _append_member(epub, "META-INF/encryption.xml", encryption)
    assert len(segment_epub(epub, "b").atoms) == 1


def test_missing_spine_document_fails_loud_instead_of_becoming_empty_text():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    with pytest.raises(ValueError, match="spine references a document missing"):
        segment_epub(_drop_member(epub, "c1.xhtml"), "b")


def test_toc_referenced_non_linear_spine_item_is_preserved_and_flagged():
    epub = _build_epub_ncx([
        ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
        ("c2.xhtml", "Chapter II", "Chapter II", _prose(300)),
    ])
    epub = _rewrite_member(
        epub, "content.opf",
        lambda raw: raw.replace(b'<itemref idref="d1"/>', b'<itemref idref="d1" linear="no"/>'),
    )
    result = segment_epub(epub, "b")
    assert [atom.title for atom in result.atoms] == ["Chapter I", "Chapter II"]
    assert any("included 1 non-linear" in flag for flag in result.flags)


# --------------------------------------------------------------- REAL Karamazov acceptance

@pytest.mark.skipif(not KARA.exists(), reason="Karamazov EPUB not present")
def test_karamazov_atoms_align_with_the_frontier():
    res = segment_epub(str(KARA), "kara")
    lengths = [a.char_len for a in res.atoms]
    bounds = frontier.chapter_bounds(lengths)                   # raises on any zero-length atom
    frontier.assert_aligned(bounds, [a.revealed_at for a in res.atoms])   # 1:1, contiguous 1..N
    assert [a.revealed_at for a in res.atoms] == list(range(1, len(res.atoms) + 1))


@pytest.mark.skipif(not KARA.exists(), reason="Karamazov EPUB not present")
def test_karamazov_part_i_divider_merged_and_count():
    res = segment_epub(str(KARA), "kara")
    assert len(res.atoms) == 96                                 # 97 body docs - 1 merged PART I divider
    a0 = res.atoms[0]
    assert a0.part_label == "PART I"
    assert a0.title.startswith("Chapter I")
    assert "28054-h-1.htm" in a0.source_files[0]                # the PART I divider
    assert "28054-h-2.htm" in a0.href                          # successor = Chapter I doc


# --------------------------------------------------------------- pass-1 review regressions

_CAST_LABELS = [
    "Principal Characters in the Story", "List of Characters", "Characters in the Play",
    "Persons of the Play", "The Persons of the Play", "Dramatis Personae",
    "Introduction by Constance Garnett", "Introduction to This Edition",
    "Translator's Note", "A Note on the Translation", "Editorial Note", "Prefatory Note",
]


@pytest.mark.parametrize("label", _CAST_LABELS)
def test_legacy_cast_or_intro_label_does_not_become_chapter_one(label):
    # the CARDINAL spoiler vector: a legacy EPUB2/NCX cast-list / scholarly-intro label whose phrasing
    # dodges the exact allowlist must still be stripped as front, NEVER surface as revealed_at=1.
    spoiler = "ALYOSHA the hero. SMERDYAKOV the murderer. The ending: the father is killed."
    epub = _build_epub_ncx([("frontm.xhtml", label, label, spoiler),
                            ("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
                            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300))])
    res = segment_epub(epub, "b")
    assert "frontm.xhtml" in res.front, f"{label!r} not stripped as front"
    assert res.atoms[0].title == "Chapter I", f"{label!r} leaked as chapter 1"
    assert "frontm.xhtml" not in {f for a in res.atoms for f in a.source_files}


def test_legacy_afterword_is_stripped_as_back_not_a_final_chapter():
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
                            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300)),
                            ("after.xhtml", "Afterword: How It All Ends",
                             "Afterword: How It All Ends", "And so the detective was the killer.")])
    res = segment_epub(epub, "b")
    assert "after.xhtml" in res.back
    assert "after.xhtml" not in {f for a in res.atoms for f in a.source_files}


def test_real_short_chapter_starting_with_book_is_not_over_merged():
    # D-A8: "Book Learning" (a real <200w chapter, not a bare enumerator) must stay its own atom.
    epub = _build_epub_ncx([("c1.xhtml", "Book Learning", "Book Learning", _prose(150)),
                            ("c2.xhtml", "The Reckoning", "The Reckoning", _prose(300))])
    res = segment_epub(epub, "b")
    assert [a.title for a in res.atoms] == ["Book Learning", "The Reckoning"]
    assert res.atoms[0].chapter_key == "b:c1.xhtml"      # its identity survives
    assert res.atoms[0].part_label == ""


def test_bare_enumerator_dividers_still_merge():
    for div in ["PART I", "Book One", "Volume II", "Section 3", "BOOK"]:
        epub = _build_epub_ncx([("d.xhtml", div, div, ""),
                                ("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
        res = segment_epub(epub, "b")
        assert len(res.atoms) == 1, f"{div!r} did not merge"
        assert div in res.atoms[0].part_label


def test_deeply_nested_doc_is_kept_as_flagged_atom_not_silently_lost():
    # a >=256-level nested doc truncates to empty text under the libxml2 depth cap; it must NOT be
    # silently folded away as a phantom empty divider (that would drop a real chapter) — keep + flag.
    nested = ("<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'><body>"
              + "<div>" * 320 + "real chapter prose" + "</div>" * 320 + "</body></html>").encode()
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
                            ("lost.xhtml", "Chapter II", "Chapter II", ""),
                            ("c3.xhtml", "Chapter III", "Chapter III", _prose(300))],
                           raw={"lost.xhtml": nested})
    res = segment_epub(epub, "b")
    assert "lost.xhtml" in {a.href for a in res.atoms}     # not silently lost
    assert any("no extractable text" in f.lower() for f in res.flags)
    assert len(res.atoms) == 3
    frontier.assert_aligned(frontier.chapter_bounds([a.char_len for a in res.atoms]),
                            [a.revealed_at for a in res.atoms])


def test_corrupt_opf_fails_loud():
    # a recover-salvageable but content-less OPF must RAISE, not silently produce 0 atoms
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("content.opf", b"<package><<<< not really xml &&& no manifest no spine")
    with pytest.raises(ValueError):
        segment_epub(buf.getvalue(), "b")


def test_anchor_driven_filters_a_prefix_front_leaf():
    # a cast-list ToC leaf whose phrasing dodges the exact allowlist is still dropped in anchor mode
    epub = _build_epub_anchors([("s1", "Chapter I", _prose(300)),
                                ("cast", "Principal Characters in the Story", _prose(40)),
                                ("s2", "Chapter II", _prose(300))])
    res = segment_epub(epub, "b")
    assert "Principal Characters in the Story" not in [a.title for a in res.atoms]


# --------------------------------------------------------------- pass-2 regressions (over/under-correction)

def _classify(label):
    return S.classify("f.xhtml", "", label, label, set(), False, set())     # legacy, label-only


@pytest.mark.parametrize("label", _CAST_LABELS)
def test_cast_and_intro_labels_classify_front(label):
    assert _classify(label) == "front"


@pytest.mark.parametrize("label", [
    "Cast Away", "Cast Iron", "Cast a Long Shadow", "The Characters Assemble", "The Characters We Become",
    "Index Finger", "Index Case", "About the Author of the Manifesto", "Introduction to Part Two",
    "Book Learning", "The Reckoning", "An Onion", "Rebellion", "Chapter I", "Epilogue", "Forewarned",
    "Persons of Interest",
])
def test_real_chapter_titles_are_not_over_stripped(label):
    # pass-2 regression: the broadened front/back heuristics must NOT strip a real body chapter
    assert _classify(label) == "body"


@pytest.mark.parametrize("label", [
    "Dramatis Personæ", "Dramatis Personae", "Persons in the Play", "The Cast", "The Cast of Characters",
    "Characters", "Persons", "List of Characters", "Persons Represented",
])
def test_more_cast_labels_classify_front(label):
    # pass-2b leak gaps: the æ ligature Gutenberg ships, in-the-play / leading-the / bare cast nouns
    assert _classify(label) == "front"


@pytest.mark.parametrize("label", [
    "Dramatis Personae of Book I", "Persons Represented in Act I", "Principal Characters of Part One",
    "List of Characters in Volume II",
])
def test_unambiguous_cast_with_a_structural_word_still_front(label):
    # the CHLABEL gate must NOT block an unambiguous cast head that names an Act/Part (pass-2b leak)
    assert _classify(label) == "front"


def test_ambiguous_intro_front_strip_is_flagged():
    # an in-story "Introduction to Murder" is stripped front (spoiler-safe) but the strip is FLAGGED,
    # never silent (pass-2b silent-loss).
    epub = _build_epub_ncx([("c0.xhtml", "Introduction to Murder", "Introduction to Murder", _prose(300)),
                            ("c1.xhtml", "Chapter I", "Chapter I", _prose(300))])
    res = segment_epub(epub, "b")
    assert "c0.xhtml" in res.front
    assert any("intro/preface" in f.lower() for f in res.flags)


@pytest.mark.parametrize("div", [
    "PART I", "Book One", "Volume II", "Section 3", "BOOK", "BOOK II.", "Book Two:", "PART I:",
    "Book the First", "Section the Second", "PART THE FIRST", "PART XL", "BOOK L", "Part—One", "Part - One",
])
def test_bare_dividers_still_match(div):
    assert S.is_divider(10, div, div) is True


@pytest.mark.parametrize("notdiv", [
    "Book Learning", "Part of My Life", "Volume of Smoke", "Bookends", "Parting", "Chapter I",
    "An Onion", "The Reckoning",
])
def test_non_dividers_do_not_match(notdiv):
    assert S.is_divider(10, notdiv, notdiv) is False


def test_blank_unlabeled_page_is_folded_not_a_phantom_atom():
    # a large blank/decorative page with NO chapter label -> folded forward, not a phantom atom (pass-2)
    blank = ("<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'><body>"
             + "<div class='x'></div>" * 40 + "</body></html>").encode()
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
                            ("blank.xhtml", "", "", ""),
                            ("c2.xhtml", "Chapter II", "Chapter II", _prose(300))],
                           raw={"blank.xhtml": blank})
    res = segment_epub(epub, "b")
    assert [a.title for a in res.atoms] == ["Chapter I", "Chapter II"]


def test_image_only_labeled_chapter_is_kept_not_folded():
    # a small image-only page WITH a real chapter label -> kept as a content-less atom (identity survives)
    svg = (b"<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'><body>"
           b"<img src='ch2.png'/></body></html>")
    epub = _build_epub_ncx([("c1.xhtml", "Chapter I", "Chapter I", _prose(300)),
                            ("img.xhtml", "Chapter II", "", ""),
                            ("c3.xhtml", "Chapter III", "Chapter III", _prose(300))],
                           raw={"img.xhtml": svg})
    res = segment_epub(epub, "b")
    assert "img.xhtml" in {a.href for a in res.atoms}
    assert len(res.atoms) == 3
    assert any("no extractable text" in f.lower() for f in res.flags)


_CORPUS = {"pg28054.epub": 96, "pg1342.epub": 60, "pg1661.epub": 12, "pg84.epub": 28}


@pytest.mark.parametrize("fname,expected", list(_CORPUS.items()))
def test_real_corpus_counts_unchanged(fname, expected):
    # lock the real-book atom counts so the broadened front/back detection can't regress them
    book = KARA.parent / fname
    if not book.exists():
        pytest.skip(f"{fname} not present")
    res = segment_epub(str(book), "x")
    assert len(res.atoms) == expected
    frontier.assert_aligned(frontier.chapter_bounds([a.char_len for a in res.atoms]),
                            [a.revealed_at for a in res.atoms])


@pytest.mark.skipif(not KARA.exists(), reason="Karamazov EPUB not present")
def test_karamazov_front_and_back_stripped():
    res = segment_epub(str(KARA), "kara")
    front = " ".join(res.front)
    back = " ".join(res.back)
    assert "28054-h-0.htm" in front                            # the PG header page
    assert "28054-h-98.htm" in back                            # the FOOTNOTES back-matter
    body_files = {f for a in res.atoms for f in a.source_files}
    assert not (body_files & set(res.front)) and not (body_files & set(res.back))
