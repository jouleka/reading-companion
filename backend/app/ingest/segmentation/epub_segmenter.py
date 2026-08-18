"""LIT-4 EPUB segmentation, productionized from spikes/lit-4-segmentation/segment.py (ADR 0001) with
the ADR-0007 D-A8 divider-merge implemented for real, and parsing hardened with lxml.

Hardening vs the spike's ElementTree: a recovering lxml parser (so imperfect XHTML yields a chapter
instead of silently degrading to empty = chapter-loss), with entity resolution / network / DTD loading
OFF (an EPUB is untrusted input — defends against XXE and entity-expansion). A missing/unparseable OPF
fails LOUD; a body doc that won't parse falls back to a lenient HTML parse before being flagged.
LIT-11 adds bounded central-directory/upload/decompression validation, canonical member identity, and
an explicit reject-DRM/allow-font-obfuscation boundary before any segmentation work.
"""
import io
import posixpath
import stat
import struct
import unicodedata
import zipfile
from collections import Counter
from urllib.parse import unquote, urlsplit

from lxml import etree
from lxml import html as lxml_html

from app.ingest.segmentation import signals as S
from app.ingest.segmentation.models import ChapterAtom, SegmentResult
from app.language import normalize_content_language

# Untrusted-input hardening: recover from imperfect markup, but NO entity resolution, NO network, NO
# external/loaded DTD, and the default huge_tree=False caps entity expansion (billion-laughs).
_XML_PARSER = etree.XMLParser(recover=True, resolve_entities=False, no_network=True,
                              load_dtd=False, dtd_validation=False, huge_tree=False)
_STRICT_XML_PARSER = etree.XMLParser(recover=False, resolve_entities=False, no_network=True,
                                     load_dtd=False, dtd_validation=False, huge_tree=False)
# Per-entry decompressed read cap — a zip-bomb guard for untrusted EPUBs (pass-1 review). Generous for a
# real chapter; bounds memory because ZipExtFile.read(n) decompresses incrementally, not all-at-once.
MAX_DOC_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_MEMBER_NAME_BYTES = 1024
MAX_COMPRESSION_RATIO = 200
MIN_RATIO_CHECK_BYTES = 1024 * 1024

_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}
_FONT_SUFFIXES = (".otf", ".ttf", ".woff", ".woff2")
_EOCD = struct.Struct("<4s4H2LH")


class EpubSafetyError(ValueError):
    """The archive crosses a fail-closed EPUB safety or DRM boundary."""


class EpubDrmError(EpubSafetyError):
    """The archive declares content encryption outside allowed font obfuscation."""


def _ln(tag):
    return tag.rsplit('}', 1)[-1].lower() if isinstance(tag, str) else ''


def _metadata_text(element, limit=512):
    """Collapse and cap untrusted OPF display metadata before it reaches logs/UI/catalog rows."""
    value = ' '.join(''.join(element.itertext()).split())
    return value[:limit] or None


def _safe_member_name(name):
    """Return a canonical ZIP member name, rejecting ambiguous/extraction-style paths."""
    if not name or "\\" in name or name.startswith("/"):
        raise EpubSafetyError("EPUB contains an unsafe member path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise EpubSafetyError("EPUB contains a control character in a member path")
    if len(name.encode("utf-8")) > MAX_MEMBER_NAME_BYTES:
        raise EpubSafetyError("EPUB member path is too long")
    path = name[:-1] if name.endswith("/") else name
    if not path or posixpath.normpath(path) != path or ".." in path.split("/"):
        raise EpubSafetyError("EPUB contains a non-canonical member path")
    return name


def _join(base, href):
    """Resolve an EPUB URI to a safe, canonical ZIP member name."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        raise EpubSafetyError("EPUB contains a non-local resource URI")
    path = unquote(parts.path)
    if not path or "\\" in path or path.startswith("/"):
        raise EpubSafetyError("EPUB contains an unsafe resource URI")
    return _safe_member_name(posixpath.normpath(posixpath.join(base, path)))


def _read(zf, name, max_bytes=MAX_DOC_BYTES):
    try:
        info = zf.getinfo(name)
        if info.file_size > max_bytes:
            raise EpubSafetyError("EPUB member exceeds the decompressed read limit")
        with zf.open(info) as fp:
            raw = fp.read(max_bytes + 1)           # detect, rather than silently accept, truncation
        if len(raw) > max_bytes or len(raw) != info.file_size:
            raise EpubSafetyError("EPUB member did not match its declared safe size")
        return raw
    except KeyError:
        return b''
    except EpubSafetyError:
        raise
    except (zipfile.BadZipFile, NotImplementedError, OSError, RuntimeError) as e:
        raise EpubSafetyError("EPUB contains an unreadable or encrypted member") from e


def _preflight_zip(src):
    """Count a bounded central directory before ZipFile allocates one ZipInfo per member."""
    owned = not isinstance(src, io.BytesIO)
    fp = open(src, "rb") if owned else src
    original = fp.tell()
    try:
        fp.seek(0, 2)
        archive_size = fp.tell()
        tail_size = min(archive_size, _EOCD.size + 65535)
        fp.seek(archive_size - tail_size)
        tail = fp.read(tail_size)
        search_at = len(tail)
        relative = -1
        while search_at:
            candidate = tail.rfind(b"PK\x05\x06", 0, search_at)
            if candidate < 0:
                break
            if candidate + _EOCD.size <= len(tail):
                candidate_comment = _EOCD.unpack_from(tail, candidate)[-1]
                if candidate + _EOCD.size + candidate_comment == len(tail):
                    relative = candidate
                    break
            search_at = candidate
        if relative < 0:
            raise EpubSafetyError("EPUB has no valid ZIP end record")
        eocd_offset = archive_size - tail_size + relative
        (_sig, disk, central_disk, disk_entries, entries, central_size, central_offset,
         comment_size) = _EOCD.unpack_from(tail, relative)
        if (eocd_offset + _EOCD.size + comment_size != archive_size
                or disk or central_disk or disk_entries != entries):
            raise EpubSafetyError("EPUB uses an unsupported split or ambiguous ZIP structure")
        if entries == 0xffff or central_size == 0xffffffff or central_offset == 0xffffffff:
            raise EpubSafetyError("ZIP64 EPUBs are outside the import safety limits")
        if entries > MAX_ARCHIVE_ENTRIES or central_size > MAX_CENTRAL_DIRECTORY_BYTES:
            raise EpubSafetyError("EPUB central directory exceeds the safety limits")
        if central_offset + central_size != eocd_offset:
            raise EpubSafetyError("EPUB has an ambiguous central-directory offset")
        fp.seek(central_offset)
        central = fp.read(central_size)
        if len(central) != central_size:
            raise EpubSafetyError("EPUB central directory is truncated")
        offset = count = 0
        while offset < len(central):
            if offset + 46 > len(central) or central[offset:offset + 4] != b"PK\x01\x02":
                raise EpubSafetyError("EPUB central directory is malformed")
            name_len, extra_len, entry_comment_len = struct.unpack_from("<HHH", central, offset + 28)
            offset += 46 + name_len + extra_len + entry_comment_len
            count += 1
            if offset > len(central) or count > MAX_ARCHIVE_ENTRIES:
                raise EpubSafetyError("EPUB central directory exceeds the safety limits")
        if offset != len(central) or count != entries:
            raise EpubSafetyError("EPUB central-directory count is inconsistent")
    finally:
        if owned:
            fp.close()
        else:
            fp.seek(original)


def _validate_archive(zf):
    """Bound metadata/decompression work and reject ambiguous or encrypted ZIP structures."""
    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise EpubSafetyError("EPUB contains too many archive members")
    seen, portable_seen = set(), set()
    total = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        portable = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
        if name in seen or portable in portable_seen:
            raise EpubSafetyError("EPUB contains duplicate or case-colliding member paths")
        seen.add(name)
        portable_seen.add(portable)
        if info.flag_bits & 0x1:
            raise EpubDrmError("encrypted ZIP members are not supported")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise EpubSafetyError("EPUB uses an unsupported compression method")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise EpubSafetyError("EPUB contains a symbolic-link member")
        if info.file_size < 0 or info.compress_size < 0:
            raise EpubSafetyError("EPUB contains invalid member sizes")
        if not info.is_dir():
            total += info.file_size
            if info.file_size > MAX_DOC_BYTES:
                raise EpubSafetyError("EPUB member exceeds the decompressed read limit")
            if info.file_size and not info.compress_size:
                raise EpubSafetyError("EPUB member has an invalid compression ratio")
            if (info.file_size >= MIN_RATIO_CHECK_BYTES
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
                raise EpubSafetyError("EPUB member exceeds the compression-ratio limit")
    if total > MAX_ARCHIVE_BYTES:
        raise EpubSafetyError("EPUB exceeds the total decompressed-size limit")
    if "mimetype" in seen and _read(zf, "mimetype") != b"application/epub+zip":
        raise EpubSafetyError("EPUB has an invalid mimetype member")


def _validate_encryption(zf):
    """Allow EPUB font obfuscation, but reject DRM and malformed encryption metadata."""
    names = {info.filename.casefold(): info.filename for info in zf.infolist()}
    enc_name = names.get("meta-inf/encryption.xml")
    if enc_name is None:
        return
    try:
        root = etree.fromstring(_read(zf, enc_name, MAX_METADATA_BYTES), _STRICT_XML_PARSER)
    except etree.XMLSyntaxError as e:
        raise EpubSafetyError("EPUB has malformed encryption metadata") from e
    encrypted = [el for el in root.iter() if _ln(el.tag) == "encrypteddata"] if root is not None else []
    if not encrypted:
        raise EpubSafetyError("EPUB has malformed encryption metadata")
    archive_names = {info.filename for info in zf.infolist()}
    for data in encrypted:
        algorithms = [(el.get("Algorithm") or el.get("algorithm") or "").strip()
                      for el in data.iter() if _ln(el.tag) == "encryptionmethod"]
        targets = [(el.get("URI") or el.get("uri") or "").strip()
                   for el in data.iter() if _ln(el.tag) == "cipherreference"]
        if len(algorithms) != 1 or algorithms[0] not in _FONT_OBFUSCATION or not targets:
            raise EpubDrmError("DRM-protected EPUBs are not supported")
        for target in targets:
            path = _join("", target)
            if path not in archive_names or not path.lower().endswith(_FONT_SUFFIXES):
                raise EpubDrmError("DRM-protected EPUBs are not supported")


def _parse(raw):
    if not raw:
        return None
    try:
        return etree.fromstring(raw, _XML_PARSER)
    except etree.XMLSyntaxError:
        return None


# ---- OPF / ToC ------------------------------------------------------------

def _opf_path(zf):
    root = _parse(_read(zf, 'META-INF/container.xml', MAX_METADATA_BYTES))
    if root is None:
        raise ValueError("EPUB has no parseable META-INF/container.xml")
    for el in root.iter():
        if _ln(el.tag) == 'rootfile' and el.get('full-path'):
            return _join('', el.get('full-path'))
    raise ValueError("EPUB container.xml declares no rootfile full-path")


def _load(zf):
    op = _opf_path(zf)
    base = posixpath.dirname(op)
    root = _parse(_read(zf, op, MAX_METADATA_BYTES))
    if root is None:
        raise ValueError(f"EPUB OPF {op!r} is not parseable")
    man, spine, guide = {}, [], set()
    nav_id = ncx_id = spine_toc = None
    ver = root.get('version', '?')
    title = author = language = None
    for el in root.iter():
        t = _ln(el.tag)
        if t == 'title' and not title:
            title = _metadata_text(el)
        elif t == 'creator' and not author:
            author = _metadata_text(el)
        elif t == 'language' and not language:
            language = ''.join(el.itertext()).strip()
        elif t == 'item':
            item_id, href = el.get('id'), el.get('href')
            if not item_id or not href or item_id in man:
                raise ValueError(f"EPUB OPF {op!r} has an invalid or duplicate manifest item")
            man[item_id] = {'href': _join(base, href),
                            'props': el.get('properties') or '',
                            'type': el.get('media-type', '')}
            if 'nav' in (el.get('properties') or '').split():
                nav_id = el.get('id')
            if el.get('media-type') == 'application/x-dtbncx+xml':
                ncx_id = el.get('id')
        elif t == 'spine':
            spine_toc = el.get('toc')
            for c in el:
                if _ln(c.tag) == 'itemref':
                    spine.append((c.get('idref'), (c.get('linear') or 'yes').lower()))
        elif t == 'reference':
            if (el.get('type') or '').lower() in ('cover', 'title-page', 'toc', 'copyright-page'):
                guide.add(_join(base, (el.get('href') or '').split('#', 1)[0]))
    if not ncx_id and spine_toc:
        ncx_id = spine_toc
    # A recover=True parse can salvage a <package> root from a corrupt OPF yet yield no manifest/spine;
    # that is a parse FAILURE, not a valid zero-chapter book -> fail LOUD rather than silently produce 0
    # atoms (pass-1 review MEDIUM). An all-front book has a non-empty spine and is handled downstream.
    if not man or not spine:
        raise ValueError(f"EPUB OPF {op!r} parsed to an empty manifest/spine — corrupt or unsupported")
    missing = [item_id for item_id, _linear in spine if not item_id or item_id not in man]
    if missing:
        raise ValueError(f"EPUB OPF {op!r} spine references missing manifest items")
    return {'base': base, 'man': man, 'spine': spine, 'guide': guide,
            'nav_id': nav_id, 'ncx_id': ncx_id, 'ver': ver, 'title': title, 'author': author,
            'language': normalize_content_language(language)}


def _ncx_walk(parent, nb, out):
    for np in [c for c in parent if _ln(c.tag) == 'navpoint']:
        lab = src = ''
        for c in np:
            if _ln(c.tag) == 'navlabel':
                for t in c:
                    if _ln(t.tag) == 'text' and not lab:
                        lab = ' '.join(''.join(t.itertext()).split())
            elif _ln(c.tag) == 'content' and c.get('src'):
                src = c.get('src')
        kids = [c for c in np if _ln(c.tag) == 'navpoint']
        if src:
            f = _join(nb, src.split('#')[0])
            fr = src.split('#')[1] if '#' in src else ''
            out.append((f, fr, lab, len(kids) == 0))
        _ncx_walk(np, nb, out)


def _nav_walk(ol, nb, out):
    for li in [c for c in ol if _ln(c.tag) == 'li']:
        a = sub = None
        for c in li:
            if _ln(c.tag) == 'a':
                a = c
            elif _ln(c.tag) == 'ol':
                sub = c
        if a is not None and a.get('href'):
            h = a.get('href')
            f = _join(nb, h.split('#')[0])
            fr = h.split('#')[1] if '#' in h else ''
            out.append((f, fr, ' '.join(''.join(a.itertext()).split()), sub is None))
        if sub is not None:
            _nav_walk(sub, nb, out)


def _toc_ordered(zf, info):
    """Ordered [(file, frag, label, is_leaf)] preserving fragments + hierarchy (EPUB3 nav, else NCX)."""
    out = []
    if info['nav_id'] and info['nav_id'] in info['man']:
        navh = info['man'][info['nav_id']]['href']
        nb = posixpath.dirname(navh)
        root = _parse(_read(zf, navh, MAX_METADATA_BYTES))
        if root is not None:
            for nav in root.iter():
                if _ln(nav.tag) != 'nav':
                    continue
                ntype = ''
                for k, v in nav.attrib.items():
                    if _ln(k) == 'type':
                        ntype = v
                if ntype and 'toc' not in ntype:
                    continue
                for ol in nav:
                    if _ln(ol.tag) == 'ol':
                        _nav_walk(ol, nb, out)
                if out:
                    break
    if not out and info['ncx_id'] and info['ncx_id'] in info['man']:
        ncxh = info['man'][info['ncx_id']]['href']
        nb = posixpath.dirname(ncxh)
        root = _parse(_read(zf, ncxh, MAX_METADATA_BYTES))
        if root is not None:
            for nm in root.iter():
                if _ln(nm.tag) == 'navmap':
                    _ncx_walk(nm, nb, out)
                    break
    return out


# ---- per-doc text / heading / epub:type -----------------------------------

def _heading(root):
    for el in root.iter():
        if _ln(el.tag) in ('h1', 'h2', 'h3'):
            return ' '.join(''.join(el.itertext()).split())
    return ''


def _doctext(zf, path):
    """(text, heading). Hardened: a recovering XML parse first; if it yields nothing usable, a lenient
    HTML parse (entity-aware, still no network/XXE) so an imperfect doc is not silently empty. A doc that
    yields no text (genuinely empty, image-only, or pathologically nested) is told apart from a real
    chapter downstream by its LABEL, not by byte size (pass-2)."""
    raw = _read(zf, path)
    root = _parse(raw)
    if root is not None:
        whole = ' '.join(root.itertext())
        if whole.strip():
            return whole, _heading(root)
    try:
        hroot = lxml_html.fromstring(raw) if raw else None
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        hroot = None
    if hroot is not None:
        return ' '.join(hroot.itertext()), _heading(hroot)
    return '', ''


def _doc_epubtype(zf, path):
    """EPUB3 epub:type tokens on the doc's body/section/article (authoritative front/back/body)."""
    root = _parse(_read(zf, path))
    if root is None:
        return set()
    toks = set()
    for el in root.iter():
        if _ln(el.tag) in ('body', 'section', 'article'):
            for k, v in el.attrib.items():
                if _ln(k) == 'type' and v:
                    toks.update(x.lower() for x in v.split())
        if toks & {'frontmatter', 'bodymatter', 'backmatter'}:
            break
    return toks


# ---- the segmenter --------------------------------------------------------

def segment_epub(epub, book_id):
    """Segment an EPUB (path/Path or raw bytes) into POST-MERGE chapter atoms. Returns a SegmentResult.
    `book_id` prefixes every chapter_key (content-identity, not positional)."""
    src = io.BytesIO(bytes(epub)) if isinstance(epub, (bytes, bytearray)) else str(epub)
    _preflight_zip(src)
    with zipfile.ZipFile(src) as zf:
        _validate_archive(zf)
        _validate_encryption(zf)
        return _segment(zf, book_id)


def _segment(zf, book_id):
    info = _load(zf)
    toc = _toc_ordered(zf, info)
    navh = info['man'].get(info['nav_id'], {}).get('href')
    toc_files = {f for f, _fr, _lab, _leaf in toc}
    included_non_linear = [info['man'][item_id]['href'] for item_id, linear in info['spine']
                           if linear == 'no' and info['man'][item_id]['href'] in toc_files]
    excluded_non_linear = [info['man'][item_id]['href'] for item_id, linear in info['spine']
                           if linear == 'no' and info['man'][item_id]['href'] not in toc_files]
    order = [info['man'][item_id]['href'] for item_id, linear in info['spine']
             if linear != 'no' or info['man'][item_id]['href'] in toc_files]
    archive_names = {member.filename for member in zf.infolist()}
    if any(path not in archive_names for path in order):
        raise ValueError("EPUB spine references a document missing from the archive")
    linear_flags = []
    if included_non_linear:
        linear_flags.append(f"included {len(included_non_linear)} non-linear spine item(s) because "
                            f"the ToC references them: {included_non_linear[:4]}")
    if excluded_non_linear:
        linear_flags.append(f"excluded {len(excluded_non_linear)} non-linear spine item(s) not "
                            f"referenced by the ToC: {excluded_non_linear[:4]}")
    label_for_file = {}
    for f, fr, lab, _leaf in toc:
        if not fr:
            label_for_file.setdefault(f, lab)
    cls, words, chars, head = {}, {}, {}, {}
    trailing_lic = []
    for f in order:
        txt, h = _doctext(zf, f)
        words[f], chars[f], head[f] = len(txt.split()), len(txt), h
        cls[f] = S.classify(f, txt, h, label_for_file.get(f, ''), info['guide'], f == navh,
                            _doc_epubtype(zf, f))
        if cls[f] == 'body' and S.LICENSE_RE.search(txt):
            trailing_lic.append(posixpath.basename(f))
    body_idx = [k for k, f in enumerate(order) if cls[f] == 'body']
    if not body_idx:
        return SegmentResult(
            book_id, 'none', (), tuple(posixpath.basename(f) for f in order), (),
            tuple(linear_flags + ['no body detected']), info['language'], info['title'], info['author'],
        )
    lo, hi = body_idx[0], body_idx[-1]
    body_files = [order[k] for k in range(lo, hi + 1)]
    front = tuple(posixpath.basename(order[k]) for k in range(0, lo))
    back = tuple(posixpath.basename(order[k]) for k in range(hi + 1, len(order)))
    bodyset = set(body_files)
    toc_leaves = [(f, fr, lab, leaf) for (f, fr, lab, leaf) in toc if f in bodyset and leaf]

    if len(toc_leaves) > max(1, len(body_files)) * 1.3:
        mode = 'anchor-driven'
        atoms, flags = _anchor_atoms(book_id, toc_leaves, chars)
    else:
        mode = 'file-driven'
        atoms, flags = _file_atoms(book_id, body_files, label_for_file, head, words, chars)
    flags = linear_flags + flags

    covered = {f for a in atoms for f in a.source_files}
    uncovered = sorted({posixpath.basename(f) for f in body_files}
                       - {posixpath.basename(f) for f in covered})
    if uncovered:
        # ADR 0001 #4 decided flag-not-fail; ingestion (LIT-6/7) MUST gate on this flag, because an
        # uncovered body file means the frontier's bounds do not span it and a reader inside it would
        # be mis-mapped as having finished the book (pass-1 review MEDIUM).
        flags = flags + [f"COVERAGE GAP: {len(uncovered)} body file(s) produced no atom — do NOT ingest "
                         f"until resolved (silent-loss / frontier mis-map risk): {uncovered[:4]}"]
    if trailing_lic:
        flags = flags + [f"{len(trailing_lic)} chapter file(s) have the PG license appended -> trim at "
                         f"the text layer in extraction (LIT-6), e.g. {trailing_lic[0]}"]
    # Auditability: an intro/preface/appendix label is AMBIGUOUS (it could be a real in-story chapter).
    # Flag any doc stripped to front/back via those leading heuristics so an over-strip is visible, not
    # silent — mirroring the anchor-mode dropped-leaf flag (pass-2b).
    amb_front = [posixpath.basename(order[k]) for k in range(0, lo)
                 if S.is_ambiguous_front(label_for_file.get(order[k], ''), head.get(order[k], ''))]
    amb_back = [posixpath.basename(order[k]) for k in range(hi + 1, len(order))
                if S.is_ambiguous_back(label_for_file.get(order[k], ''), head.get(order[k], ''))]
    if amb_front:
        flags = flags + [f"stripped {len(amb_front)} doc(s) as front via an intro/preface label "
                         f"(verify not a real opening chapter): {amb_front[:4]}"]
    if amb_back:
        flags = flags + [f"stripped {len(amb_back)} doc(s) as back via an appendix/notes label "
                         f"(verify not a real chapter): {amb_back[:4]}"]
    return SegmentResult(
        book_id, mode, tuple(atoms), front, back, tuple(flags), info['language'],
        info['title'], info['author'],
    )


def _file_atoms(book_id, body_files, label_for_file, head, words, chars):
    """File-driven (one spine doc = one chapter) + the D-A8 divider-merge. A doc folds into the FOLLOWING
    chapter (absorbing its span + part_label) when it is a real divider, OR has no text AND no chapter
    label (a genuinely blank / decorative page — nothing to leak). A no-text doc that DOES carry a
    chapter label is a real chapter we couldn't extract (image-only or pathologically nested): it is NOT
    folded away (that would drop a chapter) — it is kept as a flagged content-less atom (pass-2: the
    decision keys on the LABEL, not byte size, so it misfires neither way)."""
    flags = []
    raw, seen = [], set()
    for f in body_files:
        if f in seen:                                     # dedup duplicate spine hrefs (ADR 0001 #7)
            continue
        seen.add(f)
        lab = label_for_file.get(f, '') or head.get(f, '') or '(untitled)'
        raw.append({'href': f, 'title': lab, 'words': words[f], 'chars': chars[f],
                    'heading': head.get(f, '')})
    merged, pending = [], []
    for ch in raw:
        is_div = S.is_divider(ch['words'], ch['title'], ch['heading'])
        no_text = ch['words'] == 0
        has_ch = S._has_chapter_evidence(ch['title'], ch['heading'])
        if is_div or (no_text and not has_ch):            # divider, or a blank/decorative unlabeled page
            pending.append((ch, 'divider' if is_div else 'blank'))
            continue
        if no_text:                                       # no text but a real chapter label -> keep + flag
            flags.append(f"body doc {posixpath.basename(ch['href'])!r} (label {ch['title']!r}) produced "
                         f"NO extractable text -> kept as a content-less atom (image-only or unparseable; "
                         f"yields no facts)")
        part_labels = [c['title'] for c, kind in pending
                       if kind == 'divider' and c['title'] and c['title'] != '(untitled)']
        if pending:
            flags.append("folded " + str([posixpath.basename(c['href']) for c, _ in pending])
                         + " into " + repr(posixpath.basename(ch['href'])))
        merged.append({'href': ch['href'], 'title': ch['title'], 'part_label': ' · '.join(part_labels),
                       'char_len': max(1, ch['chars']) + sum(c['chars'] for c, _ in pending),
                       'source_files': tuple(c['href'] for c, _ in pending) + (ch['href'],)})
        pending = []
    for ch, kind in pending:                              # trailing foldable(s) with no successor
        if kind == 'divider' and ch['chars'] > 0:
            merged.append({'href': ch['href'], 'title': ch['title'], 'part_label': '',
                           'char_len': ch['chars'], 'source_files': (ch['href'],)})
            flags.append(f"trailing divider {posixpath.basename(ch['href'])!r} has no following chapter "
                         f"-> kept as its own atom")
        else:
            flags.append(f"trailing empty/blank body doc {posixpath.basename(ch['href'])!r} dropped "
                         f"(no following chapter)")
    out = tuple(ChapterAtom(revealed_at=i, chapter_key=f"{book_id}:{a['href']}", href=a['href'],
                            frag='', title=a['title'], part_label=a['part_label'],
                            char_len=a['char_len'], source_files=a['source_files'])
                for i, a in enumerate(merged, start=1))
    return out, flags


def _anchor_atoms(book_id, toc_leaves, chars):
    """Anchor-driven (many chapters per file: one atom per leaf ToC navPoint). char_len is an even-split
    approximation per file (the real reader, LIT-13, supplies exact CFI ranges). The D-A8 divider-merge
    is file-driven only; anchor-driven divider handling is a routed follow-up."""
    flags = ["anchor-driven char_len is an even-split approximation (refined by LIT-13 CFI ranges)"]
    picked, seen, dropped = [], set(), []
    for f, fr, lab, _leaf in toc_leaves:
        if (f, fr) in seen:
            continue
        seen.add((f, fr))
        if (S.is_front_label(lab, '') or S.LICENSE_RE.search(lab or '')
                or 'project gutenberg' in (lab or '').lower()):
            dropped.append(lab or '(untitled)')          # auditable: an over-strip is visible, not silent
            continue
        picked.append({'href': f, 'frag': fr, 'title': lab or '(untitled)'})
    if dropped:
        flags.append(f"dropped {len(dropped)} ToC leaf(s) as front/license-matter: {dropped[:4]}")
    per_file = Counter(p['href'] for p in picked)
    out = []
    for i, p in enumerate(picked, start=1):
        clen = max(1, chars.get(p['href'], 1) // per_file[p['href']])
        key = f"{book_id}:{p['href']}" + (f"#{p['frag']}" if p['frag'] else "")
        out.append(ChapterAtom(revealed_at=i, chapter_key=key, href=p['href'], frag=p['frag'],
                               title=p['title'], part_label='', char_len=clen, source_files=(p['href'],)))
    return tuple(out), flags
