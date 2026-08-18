#!/usr/bin/env python3
"""LIT-6 helper — extract clean per-chapter plain text from an EPUB, reusing the
ACCEPTED LIT-4 segmentation (ADR 0001) so the chapter atoms match exactly.

Also performs the text-layer trim that ADR 0001 routed to LIT-6: strip the Project
Gutenberg START header (above the marker) and the END/license tail (below the marker)
so a chapter's text is the prose only — never the boilerplate. Stdlib only.

CLI:  python3 chapter_text.py [N]    -> dump first N body chapters (default 6) with word counts
API:  chapter_texts(epub_path, count) -> [{ordinal, key, title, text, words}]
"""
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lit-4-segmentation"))
import segment as seg  # noqa: E402

KARAMAZOV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "books", "pg28054.epub")

START_RE = re.compile(r".*START OF (?:THE|THIS) PROJECT GUTENBERG[^\n]*\n", re.I | re.S)
END_RE = re.compile(r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG.*", re.I | re.S)
DIVIDER_WORDS = 200   # ADR 0001: a <200-word, label-only divider merges into the next chapter


def _body_text(zf, path):
    """Plain text of the <body> ONLY (drops the <head><title> PG boilerplate)."""
    try:
        root = ET.fromstring(seg.read(zf, path))
    except ET.ParseError:
        whole, _ = seg.doctext(zf, path)
        return whole
    body = next((el for el in root.iter() if seg.ln(el.tag) == "body"), root)
    return " ".join(body.itertext())


def _clean(text):
    text = START_RE.sub("", text, count=1)          # drop everything up to & incl. the START line
    text = END_RE.sub("", text)                      # drop the license tail
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def chapter_texts(epub_path=KARAMAZOV, count=6, skip_dividers=True):
    r = seg.segment("karamazov", epub_path, 95)
    out = []
    with zipfile.ZipFile(epub_path) as zf:
        for ch in r["chapters"]:
            text = _clean(_body_text(zf, ch["key"]))
            words = len(text.split())
            if skip_dividers and words < DIVIDER_WORDS:
                continue                              # pure Part/Book divider -> merge-skip
            out.append({"key": f"karamazov:{ch['key']}", "title": ch["title"],
                        "text": text, "words": words})
            if len(out) >= count:
                break
    for i, c in enumerate(out, start=1):
        c["ordinal"] = i                              # revealed_at among included chapters
    return out


def dump(count=5):
    """Write chapters/chNN.txt + chapters_meta.json so extraction agents can Read each
    chapter directly (keeps full text out of the orchestrator context and out of args)."""
    import json
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, "chapters")
    os.makedirs(outdir, exist_ok=True)
    meta = []
    for c in chapter_texts(count=count):
        fn = f"ch{c['ordinal']:02d}.txt"
        with open(os.path.join(outdir, fn), "w", encoding="utf-8") as f:
            f.write(c["text"])
        meta.append({"ordinal": c["ordinal"], "key": c["key"], "title": c["title"],
                     "words": c["words"], "path": f"spikes/lit-6-extraction/chapters/{fn}"})
    with open(os.path.join(here, "chapters_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"wrote {len(meta)} chapters to {outdir} + chapters_meta.json")
    for m in meta:
        print(f"  ordinal {m['ordinal']}: {m['title']!r} ({m['words']}w) -> {m['path']}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        dump(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
        return
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    for c in chapter_texts(count=n):
        print(f"=== ordinal {c['ordinal']}  |  {c['title']!r}  |  {c['words']} words  |  {c['key']}")
        print(c["text"][:300].replace("\n", " ") + " ...\n")


if __name__ == "__main__":
    main()
