#!/usr/bin/env python3
"""Offline smoke test: exercise the full LIT-6 pipeline with the deterministic stub backend
(no network) to prove the plumbing — extraction -> validation -> resolution -> LIT-5 DAL ->
spoiler-safe read-back — works end to end. Quality validation uses a real backend separately."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chapter_text  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402
from extract_schema import EXTRACTION_SCHEMA, EXTRACT_SYSTEM, extract_user_prompt  # noqa: E402

client = llm.LLMClient(provider="stub")
print("provider:", client.version)
# embed_fn contract: texts -> list[vector]  (strip the usage tuple from client.embed)
EMBED = lambda ts: client.embed(ts)[0]  # noqa: E731

tmp = tempfile.mkdtemp(prefix="lit6_")
db = pipeline.dal.MemoryDB(os.path.join(tmp, "m.db"), "karamazov", title="The Brothers Karamazov")

chs = chapter_text.chapter_texts(count=3)
for ch in chs:
    roster = pipeline.all_entities(db.view(max(ch["ordinal"] - 1, 0)))
    obj, usage = client.complete(
        EXTRACT_SYSTEM, extract_user_prompt(ch["title"], roster, ch["text"]),
        tier="cheap", schema=EXTRACTION_SCHEMA)
    r = pipeline.ingest_chapter(db, ch, obj, client, chunk_embed=EMBED)
    print(f"ch{ch['ordinal']}: ents={r['entities']} unresolved_rel_refs={r['unresolved_rel_refs']}")

# idempotent re-run of chapter 1 -> skip
again = pipeline.ingest_chapter(db, chs[0], {"chapter_summary": "x", "entities": [],
                                "relationships": [], "events": [], "themes": []}, client)
print("re-ingest ch1 skipped?", again["skipped"])

v = db.view(3)
print("characters@3:", [c["canonical_name"] for c in v.characters()])
print("catch_me_up@3:", v.catch_me_up())
print("search 'Karamazov':", [(round(s, 2), t[:40]) for s, t, *_ in v.search(EMBED(["Karamazov family father"])[0], k=2)])
print("\nSMOKE OK" if v.characters() else "\nSMOKE EMPTY (check stub)")
