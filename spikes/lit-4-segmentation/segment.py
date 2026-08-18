#!/usr/bin/env python3
"""LIT-4 spike: PROPOSED chapter-segmentation algorithm (v2), validated on the
corpus downloaded by inspect_epubs.py. Stdlib only.

Algorithm (evidence-based — see inspect_epubs.py output and ADR 0001):
  1. Parse OPF spine (linear order), manifest, guide, and the ToC
     (EPUB3 nav with epub:type, else EPUB2 NCX navMap) KEEPING fragments+order.
  2. Classify each spine doc by EXPLICIT signals only (never by position):
       front  = the nav doc; guide cover/title/toc; filename cover/title/toc/
                copyright; the PG header page ("START OF THE PROJECT GUTENBERG");
                a doc whose ToC label/heading is Contents / Illustrations / etc.
       back   = a PG license doc (END marker / "PROJECT GUTENBERG LICENSE" /
                "Section 1. General Terms") *only if it has no chapter label*
                (so a chapter file with the license appended stays a chapter).
       body   = everything else.
  3. Body window = first..last body doc. Front/back = the runs outside it.
  4. Granularity:
       anchor-driven  if ToC navPoints into body files >> body files
                      (one file holds many chapters -> split at ToC anchors);
       else file-driven (one spine doc = one chapter).
  5. Title = ToC label, else first body <h1>/<h2>, else "(untitled)".
  6. Chapter key = (spine_index, fragment) -> stable, maps to a CFI range.
"""
import os, posixpath, re, zipfile
import xml.etree.ElementTree as ET

BOOKS = {"karamazov":("books/pg28054.epub",95),"hamlet":("books/pg1524.epub",20),
         "pride":("books/pg1342.epub",61),"holmes":("books/pg1661.epub",12),
         "frankenstein":("books/pg84.epub",28),
         "se-earnest (EPUB3 play)":("books/se-earnest.epub",3),
         "se-pride (EPUB3)":("books/se-pride.epub",61)}

START_RE   = re.compile(r"START OF (THE|THIS) PROJECT GUTENBERG", re.I)
LICENSE_RE = re.compile(r"(END OF (THE|THIS) PROJECT GUTENBERG|PROJECT GUTENBERG.{0,6}LICENSE|Section 1\.\s*General Terms)", re.I)
FRONTNAME  = re.compile(r"(cover|title.?page|halftitle|^toc|contents|copyright|colophon|imprint)", re.I)
FRONTLABEL = re.compile(r"^\s*(contents|table of contents|list of illustrations|illustrations|title page|cover|copyright|frontispiece|introduction|preface|foreword|dramatis personae|persons represented|cast of characters|characters|dedication|acknowledg\w*|translator['’]?s? note|editor['’]?s? note|note on the text|about the author)\s*$", re.I)
# chapter evidence: a chapter keyword, OR a leading roman/arabic enumerator ("I.", "1)", "IV.")
CHLABEL    = re.compile(r"\b(chapter|letter|act|scene|book|part|prologue|epilogue|canto|stave|volume)\b|^\s*(?:[ivxlcdm]+|\d+)\s*[.\):]", re.I)
# EPUB3 epub:type vocabulary — authoritative front/back/body when present
EPUB_FRONT = {'titlepage', 'halftitlepage', 'imprint', 'dedication', 'epigraph', 'foreword',
              'preface', 'introduction', 'preamble', 'toc', 'cover',
              'dramatis-personae', 'z3998:dramatis-personae'}
EPUB_BACK  = {'colophon', 'appendix', 'afterword', 'endnotes', 'rearnotes', 'loi'}
EPUB_BODY  = {'chapter', 'part', 'division', 'volume', 'prologue', 'epilogue', 'scene', 'z3998:scene'}


def ln(t): return t.rsplit('}', 1)[-1].lower() if isinstance(t, str) else ''
def read(zf, n):
    try: return zf.read(n)
    except KeyError: return b''
def join(base, href): return posixpath.normpath(posixpath.join(base, href)).lstrip('/')


def opf_path(zf):
    for el in ET.fromstring(read(zf, 'META-INF/container.xml')).iter():
        if ln(el.tag) == 'rootfile' and el.get('full-path'):
            return el.get('full-path')


def load(zf):
    op = opf_path(zf); base = posixpath.dirname(op); root = ET.fromstring(read(zf, op))
    man = {}; spine = []; guide = set(); nav_id = ncx_id = None; spine_toc = None
    ver = root.get('version', '?'); title = None
    for el in root.iter():
        t = ln(el.tag)
        if t == 'title' and not title:
            title = ''.join(el.itertext()).strip()
        elif t == 'item':
            man[el.get('id')] = {'href': join(base, el.get('href')), 'props': el.get('properties') or '', 'type': el.get('media-type', '')}
            if 'nav' in (el.get('properties') or '').split(): nav_id = el.get('id')
            if el.get('media-type') == 'application/x-dtbncx+xml': ncx_id = el.get('id')
        elif t == 'spine':
            spine_toc = el.get('toc')
            for c in el:
                if ln(c.tag) == 'itemref':
                    spine.append((c.get('idref'), c.get('linear', 'yes')))
        elif t == 'reference':
            if (el.get('type') or '').lower() in ('cover', 'title-page', 'toc', 'copyright-page'):
                guide.add(join(base, (el.get('href') or '')).split('#')[0])
    if not ncx_id and spine_toc: ncx_id = spine_toc
    return {'base': base, 'man': man, 'spine': spine, 'guide': guide,
            'nav_id': nav_id, 'ncx_id': ncx_id, 'ver': ver, 'title': title}


def _ncx_walk(parent, nb, out):
    for np in [c for c in parent if ln(c.tag) == 'navpoint']:
        lab = ''; src = ''
        for c in np:
            if ln(c.tag) == 'navlabel':
                for t in c:
                    if ln(t.tag) == 'text' and not lab: lab = ' '.join(''.join(t.itertext()).split())
            elif ln(c.tag) == 'content' and c.get('src'): src = c.get('src')
        kids = [c for c in np if ln(c.tag) == 'navpoint']
        if src:
            f = join(nb, src.split('#')[0]); fr = src.split('#')[1] if '#' in src else ''
            out.append((f, fr, lab, len(kids) == 0))
        _ncx_walk(np, nb, out)


def _nav_walk(ol, nb, out):
    for li in [c for c in ol if ln(c.tag) == 'li']:
        a = sub = None
        for c in li:
            if ln(c.tag) == 'a': a = c
            elif ln(c.tag) == 'ol': sub = c
        if a is not None and a.get('href'):
            h = a.get('href'); f = join(nb, h.split('#')[0]); fr = h.split('#')[1] if '#' in h else ''
            out.append((f, fr, ' '.join(''.join(a.itertext()).split()), sub is None))
        if sub is not None: _nav_walk(sub, nb, out)


def toc_ordered(zf, info):
    """ordered [(file, frag, label, is_leaf)] preserving fragments + hierarchy."""
    out = []
    if info['nav_id'] and info['nav_id'] in info['man']:
        navh = info['man'][info['nav_id']]['href']; nb = posixpath.dirname(navh)
        try: root = ET.fromstring(read(zf, navh))
        except ET.ParseError: root = None
        if root is not None:
            for nav in root.iter():
                if ln(nav.tag) != 'nav': continue
                ntype = ''
                for k, v in nav.attrib.items():
                    if ln(k) == 'type': ntype = v
                if ntype and 'toc' not in ntype: continue
                for ol in nav:
                    if ln(ol.tag) == 'ol': _nav_walk(ol, nb, out)
                if out: break
    if not out and info['ncx_id'] and info['ncx_id'] in info['man']:
        ncxh = info['man'][info['ncx_id']]['href']; nb = posixpath.dirname(ncxh)
        try: root = ET.fromstring(read(zf, ncxh))
        except ET.ParseError: root = None
        if root is not None:
            for nm in root.iter():
                if ln(nm.tag) == 'navmap': _ncx_walk(nm, nb, out); break
    return out


def doctext(zf, path):
    raw = read(zf, path)
    try:
        root = ET.fromstring(raw); whole = ' '.join(root.itertext()); h = ''
        for el in root.iter():
            if ln(el.tag) in ('h1', 'h2', 'h3'):
                h = ' '.join(''.join(el.itertext()).split()); break
        return whole, h
    except ET.ParseError:
        s = raw.decode('utf-8', 'ignore'); txt = re.sub(r'<[^>]+>', ' ', s)
        m = re.search(r'<h[1-3][^>]*>(.*?)</h[1-3]>', s, re.I | re.S)
        return txt, (re.sub(r'<[^>]+>', ' ', m.group(1)).strip() if m else '')


def etype(el):
    for k, v in el.attrib.items():
        if ln(k) == 'type':
            return v
    return ''


def doc_epubtype(zf, path):
    """EPUB3 epub:type tokens on the doc's <body>/<section> (authoritative front/back/body)."""
    try:
        root = ET.fromstring(read(zf, path))
    except ET.ParseError:
        return set()
    toks = set()
    for el in root.iter():
        if ln(el.tag) in ('body', 'section', 'article'):
            t = etype(el)
            if t:
                toks.update(x.lower() for x in t.split())
        if toks & {'frontmatter', 'bodymatter', 'backmatter'}:
            break
    return toks


def classify(f, text, heading, label, guide, is_nav, etoks):
    # EPUB3 epub:type is the authoritative, fail-closed signal when present
    if 'frontmatter' in etoks: return 'front'
    if 'backmatter' in etoks: return 'back'
    if 'bodymatter' in etoks: return 'body'
    if etoks & EPUB_FRONT: return 'front'
    if etoks & EPUB_BACK: return 'back'
    if etoks & EPUB_BODY: return 'body'
    # --- legacy (no epub:type, e.g. EPUB2/NCX): heuristic signals ---
    b = posixpath.basename(f)
    if is_nav: return 'front'
    if f in guide: return 'front'
    if START_RE.search(text[:4000]): return 'front'
    has_ch = bool(CHLABEL.search(label or '') or CHLABEL.search(heading or ''))
    if LICENSE_RE.search(text):
        return 'body' if has_ch else 'back'   # chapter+appended-license stays a chapter
    if FRONTNAME.search(b): return 'front'
    if FRONTLABEL.match(label or '') or FRONTLABEL.match(heading or ''): return 'front'
    return 'body'


def segment(slug, path, expected):
    with zipfile.ZipFile(path) as zf:
        info = load(zf); toc = toc_ordered(zf, info)
        navh = info['man'].get(info['nav_id'], {}).get('href')
        order = [info['man'][i]['href'] for i, lin in info['spine'] if lin != 'no' and i in info['man']]
        label_for_file = {}
        for f, fr, lab, leaf in toc:
            if not fr: label_for_file.setdefault(f, lab)
        cls = {}; words = {}; head = {}; trailing_lic = []
        for f in order:
            txt, h = doctext(zf, f); words[f] = len(txt.split()); head[f] = h
            cls[f] = classify(f, txt, h, label_for_file.get(f, ''), info['guide'], f == navh, doc_epubtype(zf, f))
            if cls[f] == 'body' and LICENSE_RE.search(txt):
                trailing_lic.append(posixpath.basename(f))
        body_idx = [k for k, f in enumerate(order) if cls[f] == 'body']
        if not body_idx:
            return {'slug': slug, 'mode': 'NONE', 'count': 0, 'expected': expected,
                    'front': [], 'back': [], 'chapters': [], 'flags': ['no body detected']}
        lo, hi = body_idx[0], body_idx[-1]
        body_files = [order[k] for k in range(lo, hi + 1)]
        front = [posixpath.basename(order[k]) for k in range(0, lo)]
        back = [posixpath.basename(order[k]) for k in range(hi + 1, len(order))]
        bodyset = set(body_files)
        toc_body = [(f, fr, lab, leaf) for (f, fr, lab, leaf) in toc if f in bodyset]
        toc_leaves = [t for t in toc_body if t[3]]
        flags = []
        if len(toc_leaves) > max(1, len(body_files)) * 1.3:
            mode = 'anchor-driven (leaf ToC navPoints)'
            seen = set(); chapters = []
            for f, fr, lab, leaf in toc_leaves:
                if (f, fr) in seen: continue
                seen.add((f, fr))
                if (FRONTLABEL.match(lab or '') or LICENSE_RE.search(lab or '')
                        or 'project gutenberg' in (lab or '').lower()):
                    continue
                chapters.append({'title': lab or '(untitled)', 'file': posixpath.basename(f), 'frag': fr,
                                 'key': f + ('#' + fr if fr else '')})
            illos = [c for c in chapters if re.search(r'\b(screen|fire|river|post|portrait|miniature)\b', c['title'], re.I)]
            if illos:
                flags.append(f"{len(illos)} ToC entries look like List-of-Illustrations captions polluting labels "
                             f"(legacy NCX lacks epub:type to filter cleanly; e.g. {illos[0]['title']!r})")
        else:
            mode = 'file-driven (one spine doc = one chapter)'
            chapters = []; seen_f = set()
            for f in body_files:
                if f in seen_f: continue          # dedup duplicate spine hrefs
                seen_f.add(f)
                lab = label_for_file.get(f, '') or head.get(f, '') or '(untitled)'
                chapters.append({'title': lab, 'file': posixpath.basename(f), 'frag': '', 'key': f, 'words': words[f]})
            tiny = [c for c in chapters if c.get('words', 999) < 200]
            if tiny:
                flags.append(f"{len(tiny)} tiny (<200w) body docs = section/Part dividers, merge into next chapter "
                             f"(e.g. {tiny[0]['title']!r})")
        if trailing_lic:
            flags.append(f"{len(trailing_lic)} chapter file(s) have the PG license appended -> trim at text level, keep the chapter (e.g. {trailing_lic[0]})")
        untitled = [c for c in chapters if c['title'] in ('(untitled)', '')]
        if untitled: flags.append(f"{len(untitled)} chapters had no ToC label/heading")
        if not toc: flags.append("no ToC (nav/ncx) -> would fall back to spine + heading detection")
        covered = {c['file'] for c in chapters}
        uncovered = sorted({posixpath.basename(f) for f in body_files} - covered)
        if uncovered:
            flags.append(f"COVERAGE GAP: {len(uncovered)} body file(s) produced no chapter (silent-loss risk): {uncovered[:4]}")
        return {'slug': slug, 'mode': mode, 'count': len(chapters), 'expected': expected,
                'front': front, 'back': back, 'chapters': chapters, 'flags': flags}


def main():
    print("LIT-4 segmentation v2 - validation\n")
    for slug, (path, exp) in BOOKS.items():
        if not os.path.exists(path):
            print(f"{slug}: MISSING {path}\n"); continue
        r = segment(slug, path, exp)
        ok = "OK" if abs(r['count'] - exp) <= max(2, exp * 0.05) else "REVIEW"
        print(f"{'='*72}\n{slug}: {r['mode']}")
        print(f"  front stripped ({len(r['front'])}): {r['front']}")
        print(f"  back  stripped ({len(r['back'])}): {r['back']}")
        print(f"  CHAPTERS: {r['count']}   expected ~{exp}   [{ok}]")
        for c in r['chapters'][:3]: print(f"     first: {c['title']!r}")
        for c in r['chapters'][-3:]: print(f"     last : {c['title']!r}")
        for fl in r['flags']: print(f"  FLAG: {fl}")
        print()


if __name__ == '__main__':
    main()
