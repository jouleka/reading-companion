"""Build a real, deterministic, NO-API spoiler-gate fixture store.

The 5 committed Karamazov chapters (real ``gpt-4o-mini`` extractions from the LIT-6 close-out + their
raw prose) are ingested through the PRODUCTION Module C pipeline into the PRODUCTION store, with the
offline stub ``LLMClient`` providing the (lexical) embeddings. No network and no key — but with the
GENUINE reveal structure (Sofya Ivanovna@3, the elder Zossima@4, the Superior / Optin Monastery /
Russia@5) that makes the gate's falsifiability checks non-vacuous (a stub-EXTRACTED store would have
no late-revealed proper nouns to hide).

This is the ADR 0007 D-A9 "build it by ingesting Karamazov via Module C on the stub" path: the
extraction CONTENT is the real committed model output (a test fixture), the STORE + PIPELINE are
production, the EMBEDDINGS are the deterministic stub.
"""
import json
import os

from app.ingest.extraction.pipeline import all_entities, ingest_chapter, prepare_chapter
from app.llm.client import LLMClient
from app.memory.store import Store

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BOOK_ID = "karamazov"
_META = dict(title="The Brothers Karamazov", author="Dostoevsky", source="gutenberg", source_id="28054")


def load_chapters():
    """The 5 chapters as ingest-ready dicts: {ordinal, key, title, text, extraction}."""
    with open(os.path.join(FIX, "chapters_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(FIX, "extractions.json"), encoding="utf-8") as f:
        exts = json.load(f)
    exts = exts["extractions"] if isinstance(exts, dict) else exts
    by_ord = {x["ordinal"]: x for x in exts if x.get("extraction")}
    chapters = []
    for m in sorted(meta, key=lambda x: x["ordinal"]):
        o = m["ordinal"]
        if o not in by_ord:
            continue
        item = by_ord[o]
        path = os.path.join(FIX, f"ch{o:02d}.txt")
        text = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
        chapters.append({"ordinal": o, "key": item["key"], "title": item.get("title", ""),
                         "text": text, "extraction": item["extraction"]})
    return chapters


def build_fixture_store(data_dir):
    """Ingest the chapters into a fresh per-book store under ``data_dir``. Returns
    (store, client, max_bm). Read via ``with store.book(BOOK_ID) as mem:`` — the gate functions take
    that ``mem`` (a sole-owned MemoryDB) as their ``db`` and run under the per-book lock."""
    store = Store(data_dir=str(data_dir))
    client = LLMClient(provider="stub", allow_stub=True)
    chapters = load_chapters()
    for ch in chapters:
        with store.book(BOOK_ID, meta=_META) as mem:
            roster = all_entities(mem.view(max(ch["ordinal"] - 1, 0)))
        prepared = prepare_chapter(ch, ch["extraction"], client, roster=roster)
        with store.book(BOOK_ID) as mem:
            ingest_chapter(mem, ch, prepared)
    return store, client, max(c["ordinal"] for c in chapters)
