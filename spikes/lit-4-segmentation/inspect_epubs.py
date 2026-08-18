#!/usr/bin/env python3
"""LIT-4 spike: inspect real EPUB structure to ground the chapter-segmentation
algorithm. Stdlib only. Downloads a structurally-varied public-domain corpus
from Project Gutenberg, dumps each book's spine / nav / front-matter reality,
applies a first-cut segmentation algorithm, and writes report.json.

Run:  python3 spikes/lit-4-segmentation/inspect_epubs.py
"""
import json, os, posixpath, re, sys, time, urllib.request, urllib.error, zipfile
import xml.etree.ElementTree as ET

BOOKS_DIR = "books"
CORPUS = [
    (28054, "karamazov",   "Brothers Karamazov (Dostoevsky/Garnett) - intro + 12 Books + Epilogue"),
    (1524,  "hamlet",      "Hamlet (Shakespeare) - Dramatis Personae + Acts/Scenes"),
    (1342,  "pride",       "Pride and Prejudice (Austen) - 61 chapters"),
    (1661,  "holmes",      "Adventures of Sherlock Holmes (Doyle) - 12 standalone stories"),
    (84,    "frankenstein","Frankenstein (Shelley) - letters frame + chapters"),
]
UA = "reading-companion-spike/0.1 (+https://github.com/jouleka/reading-companion)"

FRONT_PAT = re.compile(r"(cover|title.?page|halftitle|toc|contents|copyright|colophon|imprint|dedication|epigraph|frontmatter|preface|foreword|introduction|^pg-?header|^wrapper)", re.I)
BACK_PAT  = re.compile(r"(license|gutenberg|pg-?footer|backmatter|advert|colophon|the-?end|appendix|endnotes|footnotes|index)", re.I)
PG_MARK   = re.compile(r"PROJECT GUTENBERG", re.I)
HEADINGISH = re.compile(r"\b(contents|dramatis personae|characters|persons represented|table of contents)\b", re.I)


def ln(tag):  # local-name, namespace-agnostic
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def download(gid):
    path = os.path.join(BOOKS_DIR, f"pg{gid}.epub")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    cands = [
        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.epub",
        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}-images.epub",
        f"https://www.gutenberg.org/ebooks/{gid}.epub3.images",
        f"https://www.gutenberg.org/ebooks/{gid}.epub.images",
    ]
    for url in cands:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if data[:2] == b"PK":
                with open(path, "wb") as f:
                    f.write(data)
                print(f"  downloaded {url} -> {path} ({len(data)//1024} KB)")
                time.sleep(2)  # Gutenberg robot policy
                return path
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"  miss {url}: {e}")
    print(f"  FAILED to download gid {gid}")
    return None


def read(zf, name):
    try:
        return zf.read(name)
    except KeyError:
        return b""


def norm(base, href):
    href = (href or "").split("#")[0]
    if not href:
        return ""
    return posixpath.normpath(posixpath.join(base, href)).lstrip("/")


def parse_opf(zf):
    cont = read(zf, "META-INF/container.xml")
    opf_path = None
    for el in ET.fromstring(cont).iter():
        if ln(el.tag) == "rootfile" and el.get("full-path"):
            opf_path = el.get("full-path"); break
    base = posixpath.dirname(opf_path)
    root = ET.fromstring(read(zf, opf_path))
    version = root.get("version", "?")
    title = None
    manifest = {}   # id -> {href, type, props}
    nav_id = ncx_id = None
    spine = []      # ordered list of (idref, linear)
    spine_toc = None
    guide = []      # (type, href)
    for el in root.iter():
        t = ln(el.tag)
        if t == "title" and title is None:
            title = "".join(el.itertext()).strip()
        elif t == "item":
            iid = el.get("id"); href = norm(base, el.get("href")); mt = el.get("media-type", "")
            props = (el.get("properties") or "")
            manifest[iid] = {"href": href, "type": mt, "props": props}
            if "nav" in props.split():
                nav_id = iid
            if mt == "application/x-dtbncx+xml":
                ncx_id = iid
        elif t == "spine":
            spine_toc = el.get("toc")
            for c in el:
                if ln(c.tag) == "itemref":
                    spine.append((c.get("idref"), c.get("linear", "yes")))
        elif t == "reference":  # guide
            guide.append((el.get("type", ""), norm(base, el.get("href"))))
    if ncx_id is None and spine_toc:
        ncx_id = spine_toc
    return {"opf": opf_path, "base": base, "version": version, "title": title,
            "manifest": manifest, "spine": spine, "guide": guide,
            "nav_id": nav_id, "ncx_id": ncx_id}


def parse_nav(zf, opf, nav_href, navbase):
    """EPUB3 nav.xhtml -> {href_no_frag: label} for toc, plus landmark roles."""
    toc, landmarks = {}, {}
    try:
        root = ET.fromstring(read(zf, nav_href))
    except ET.ParseError:
        return toc, landmarks
    for nav in root.iter():
        if ln(nav.tag) != "nav":
            continue
        navtype = ""
        for k, v in nav.attrib.items():
            if ln(k) == "type":
                navtype = v
        for a in nav.iter():
            if ln(a.tag) == "a" and a.get("href"):
                h = norm(navbase, a.get("href"))
                label = " ".join("".join(a.itertext()).split())
                if "landmark" in navtype:
                    landmarks[h] = (a.get("epub:type") or navtype or "").lower()
                else:
                    toc.setdefault(h, label)
    return toc, landmarks


def parse_ncx(zf, ncx_href, ncxbase):
    toc = {}
    try:
        root = ET.fromstring(read(zf, ncx_href))
    except ET.ParseError:
        return toc
    for np in root.iter():
        if ln(np.tag) != "navpoint":
            continue
        label = ""; src = ""
        for c in np.iter():
            if ln(c.tag) == "text" and not label:
                label = " ".join("".join(c.itertext()).split())
            if ln(c.tag) == "content" and c.get("src"):
                src = norm(ncxbase, c.get("src"))
        if src:
            toc.setdefault(src, label)
    return toc


def doc_info(zf, path):
    raw = read(zf, path)
    heading = ""; words = 0; pg = False
    try:
        root = ET.fromstring(raw)
        texts = list(root.itertext())
        whole = " ".join(texts)
        words = len(whole.split())
        pg = bool(PG_MARK.search(whole))
        for el in root.iter():
            if ln(el.tag) in ("h1", "h2", "h3", "title") and not heading:
                heading = " ".join("".join(el.itertext()).split())
            if heading and ln(el.tag) in ("h1", "h2", "h3"):
                break
    except ET.ParseError:
        txt = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore"))
        words = len(txt.split()); pg = bool(PG_MARK.search(txt))
        m = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", raw.decode("utf-8", "ignore"), re.I | re.S)
        if m:
            heading = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())
    return heading[:80], words, pg


def role(href, heading, words, idx, n, pg, guide_fronts, guide_backs):
    base = posixpath.basename(href)
    near_start = idx <= 2
    near_end = idx >= n - 3
    if href in guide_fronts:
        return "front"
    if href in guide_backs:
        return "back"
    if HEADINGISH.search(heading):
        return "front"  # ToC / dramatis personae
    if FRONT_PAT.search(base) and near_start:
        return "front"
    if BACK_PAT.search(base) and near_end:
        return "back"
    if pg and near_start:
        return "front"
    if pg and near_end:
        return "back"
    if words < 120 and (near_start or near_end) and not heading:
        return "front" if near_start else "back"
    return "chapter"


def analyze(path, gid, slug, note):
    out = {"gid": gid, "slug": slug, "note": note, "file": path}
    with zipfile.ZipFile(path) as zf:
        opf = parse_opf(zf)
        out.update({k: opf[k] for k in ("version", "title")})
        navbase = posixpath.dirname(opf["manifest"].get(opf["nav_id"], {}).get("href", opf["base"]))
        toc = {}
        nav_kind = "none"
        if opf["nav_id"]:
            nav_href = opf["manifest"][opf["nav_id"]]["href"]
            toc, _ = parse_nav(zf, opf, nav_href, posixpath.dirname(nav_href))
            nav_kind = "epub3-nav"
        if not toc and opf["ncx_id"] and opf["ncx_id"] in opf["manifest"]:
            ncx_href = opf["manifest"][opf["ncx_id"]]["href"]
            toc = parse_ncx(zf, ncx_href, posixpath.dirname(ncx_href))
            nav_kind = "ncx" if nav_kind == "none" else nav_kind + "+ncx"
        out["nav_kind"] = nav_kind
        guide_fronts = {h for (t, h) in opf["guide"] if t.lower() in
                        ("cover", "title-page", "toc", "copyright-page", "foreword", "preface")}
        guide_backs = {h for (t, h) in opf["guide"] if t.lower() in ("copyright-page",) and False}
        # build ordered spine docs (drop non-linear, drop the nav doc itself)
        items = []
        order = [idref for idref, lin in opf["spine"] if lin != "no"]
        n = len(order)
        # how many distinct toc entries point into each spine href (multi-chapter-per-doc signal)
        toc_per_href = {}
        for h in toc:
            toc_per_href[h] = toc_per_href.get(h, 0) + 1
        for idx, idref in enumerate(order):
            it = opf["manifest"].get(idref)
            if not it:
                continue
            href = it["href"]
            heading, words, pg = doc_info(zf, href)
            label = toc.get(href, "")
            r = role(href, heading or label, words, idx, n, pg, guide_fronts, guide_backs)
            items.append({"idx": idx, "href": href, "base": posixpath.basename(href),
                          "type": it["type"], "label": label[:60], "heading": heading,
                          "words": words, "pg": pg, "role": r,
                          "is_nav": idref == opf["nav_id"]})
        out["spine_len"] = len(items)
        out["items"] = items
        # toc anchors (with fragments) to detect many-chapters-in-one-file
        toc_full = {}
        # re-read nav with fragments preserved for the multi-chapter signal
    out["toc_entries"] = len(toc)
    out["guide"] = opf["guide"]
    return out, segment(out)


def segment(book):
    """First-cut algorithm: leading front run + trailing back run stripped;
    middle linear docs become chapters in spine order; nav label preferred,
    else heading, else 'Untitled'. Flags ambiguous cases."""
    items = book["items"]
    flags = []
    # leading front
    i = 0
    while i < len(items) and items[i]["role"] == "front":
        i += 1
    j = len(items) - 1
    while j >= 0 and items[j]["role"] == "back":
        j -= 1
    middle = items[i:j + 1]
    # any front/back detected INSIDE the middle? (interleaved -> ambiguous)
    inner_nonchapter = [m for m in middle if m["role"] != "chapter"]
    if inner_nonchapter:
        flags.append(f"{len(inner_nonchapter)} non-chapter item(s) interleaved inside body "
                     f"(e.g. {inner_nonchapter[0]['base']} role={inner_nonchapter[0]['role']})")
    chapters = []
    for k, m in enumerate(middle):
        title = m["label"] or m["heading"] or "(untitled)"
        chapters.append({"key": f"ch_{k+1:04d}", "title": title[:70],
                         "href": m["href"], "words": m["words"]})
    if not book.get("nav_kind") or book["nav_kind"] == "none":
        flags.append("no nav/ncx ToC - chapter titles rely on in-doc headings only")
    untitled = [c for c in chapters if c["title"] == "(untitled)"]
    if untitled:
        flags.append(f"{len(untitled)} chapter(s) had no nav label and no heading")
    big = [c for c in chapters if c["words"] > 18000]
    if big:
        flags.append(f"{len(big)} chapter(s) > 18k words (likely many chapters in one spine doc -> needs heading-split)")
    tiny = [c for c in chapters if c["words"] < 200]
    if tiny:
        flags.append(f"{len(tiny)} 'chapter(s)' < 200 words (likely mis-split section dividers)")
    return {"chapter_count": len(chapters), "front_stripped": i,
            "back_stripped": len(items) - 1 - j, "flags": flags,
            "chapters": chapters}


def main():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    report = []
    for gid, slug, note in CORPUS:
        print(f"\n{'='*78}\n{slug}: {note}")
        path = download(gid)
        if not path:
            continue
        try:
            book, seg = analyze(path, gid, slug, note)
        except Exception as e:
            print(f"  ANALYZE ERROR: {type(e).__name__}: {e}")
            continue
        print(f"  title={book['title']!r} epub={book['version']} nav={book['nav_kind']} "
              f"spine={book['spine_len']} toc_entries={book['toc_entries']}")
        print(f"  roles: front={sum(x['role']=='front' for x in book['items'])} "
              f"chapter={sum(x['role']=='chapter' for x in book['items'])} "
              f"back={sum(x['role']=='back' for x in book['items'])}")
        shown = book["items"][:60]
        for it in shown:
            print(f"    [{it['idx']:>3}] {it['role']:<7} w={it['words']:>6} "
                  f"{it['base'][:26]:<26} toc={it['label'][:30]!r} h={it['heading'][:30]!r}")
        if len(book["items"]) > 60:
            print(f"    ... (+{len(book['items'])-60} more spine items)")
        print(f"  --> SEGMENTED: {seg['chapter_count']} chapters "
              f"(stripped {seg['front_stripped']} front / {seg['back_stripped']} back)")
        for c in seg["chapters"][:6]:
            print(f"        {c['key']} w={c['words']:>6} {c['title']!r}")
        if seg["chapter_count"] > 6:
            print(f"        ... +{seg['chapter_count']-6} more")
        if seg["flags"]:
            print("  FLAGS:")
            for fl in seg["flags"]:
                print(f"      - {fl}")
        report.append({"book": {k: book[k] for k in ("gid","slug","note","title","version","nav_kind","spine_len","toc_entries")},
                       "segmentation": seg, "items": book["items"]})
    with open("spikes/lit-4-segmentation/report.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nWrote spikes/lit-4-segmentation/report.json ({len(report)} books)")


if __name__ == "__main__":
    main()
