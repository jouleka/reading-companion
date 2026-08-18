"""Tiny synthetic EPUB2+NCX builder for API tests (the test_chapter_text.py helper, shared)."""
import io
import zipfile
from html import escape

_CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
              'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
              '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
              '</rootfiles></container>')


def epub_ncx(docs, *, language=None, title="Synthetic", author=None):
    """docs = [(filename, label, heading, body)]; returns EPUB bytes."""
    items, spine, navpoints = [], [], []
    for i, (fn, label, _h, _b) in enumerate(docs):
        items.append(f'<item id="d{i}" href="{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="d{i}"/>')
        navpoints.append(f'<navPoint id="n{i}"><navLabel><text>{label}</text></navLabel>'
                         f'<content src="{fn}"/></navPoint>')
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'{f"<dc:title>{escape(title)}</dc:title>" if title else ""}'
           f'{f"<dc:creator>{escape(author)}</dc:creator>" if author else ""}'
           f'{f"<dc:language>{escape(language)}</dc:language>" if language else ""}'
           '</metadata><manifest>'
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
            z.writestr(fn, f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                           f'<body><h1>{heading}</h1><p>{body}</p></body></html>')
    return buf.getvalue()


def three_chapter_book():
    """Chapters introduce DISTINCT names per chapter so extraction/reveal structure is real: Aldric@1,
    Berenice@2, Corvus@3 (capitalized, distinctive, absent from other chapters)."""
    return epub_ncx([
        ("c1.xhtml", "Chapter I", "Chapter I",
         "Aldric the smith arrived in the valley. " * 12),
        ("c2.xhtml", "Chapter II", "Chapter II",
         "Berenice met Aldric at the forge and they spoke. " * 12),
        ("c3.xhtml", "Chapter III", "Chapter III",
         "Corvus the magistrate summoned Berenice for judgment. " * 12),
    ])
